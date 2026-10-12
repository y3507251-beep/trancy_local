"""Bilibili-only, opt-in local dubbing with acknowledged mute leases."""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import math
import time
import uuid

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal

from .timeline import Timeline, browser_identity, cue_key
from .translation import detect_target


def english_text(cue):
    source, _ = detect_target(cue.get("text", ""), cue.get("source_language", ""))
    if source == "en":
        text = cue.get("text", "")
    elif source.startswith("zh"):
        text = cue.get("translation", "")
        target = cue.get("target_language", "en")
        if target and target != "en":
            return ""
    else:
        return ""
    text = " ".join(str(text).split())
    # A translator can echo Chinese or leave an empty result. Never speak that
    # as an English replacement just because its target field says "en".
    return text if text and detect_target(text)[0] == "en" else ""


def speech_token(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QtSpeechPlayer(QObject):
    """Load silently first; stale media signals belong to retired instances."""
    prepared = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    ended = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = None
        self._token = None
        self._prepared = False

    def prepare(self, path, token):
        from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
        self.stop()
        if not QMediaDevices.audioOutputs():
            self.failed.emit(dict(token=token, message="未找到可用的声音输出设备"))
            return
        player = QMediaPlayer(self)
        output = QAudioOutput(player)
        player.setAudioOutput(output)
        self._player, self._token, self._prepared = player, token, False
        player.mediaStatusChanged.connect(lambda status, p=player: self._media_status(p, status))
        player.errorOccurred.connect(lambda *_args, p=player: self._failed(p))
        player.setSource(QUrl.fromLocalFile(str(path)))

    def _media_status(self, player, status):
        if player is not self._player:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if not self._prepared:
                self._prepared = True
                self.prepared.emit(dict(token=self._token))
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._failed(player)
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.ended.emit(dict(token=self._token))

    def _failed(self, player):
        if player is self._player:
            self.failed.emit(dict(token=self._token, message="本地英文配音播放失败"))

    def align(self, position_ms, rate, paused, *, force=False):
        player = self._player
        if player is None or not self._prepared:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        if abs(player.playbackRate() - rate) > .01:
            player.setPlaybackRate(rate)
        if force or abs(player.position() - position_ms) > 260:
            player.setPosition(max(0, int(position_ms)))
        if paused:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                player.pause()
        elif player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            player.play()

    def pause(self):
        if self._player is not None:
            self._player.pause()

    def stop(self):
        player, self._player = self._player, None
        self._token, self._prepared = None, False
        if player is not None:
            player.stop()
            player.deleteLater()


class DubbingSession(QObject):
    status = pyqtSignal(str)
    disabled = pyqtSignal(str)
    closed = pyqtSignal()

    PREFETCH = 5
    MAX_STRETCH = 1.5
    STALE_SECONDS = 2.0
    RENEW_SECONDS = .8
    RESTORE_GRACE = 1.8

    def __init__(self, bridge, parent=None, *, speech_factory=None, player_factory=None, clock=None):
        super().__init__(parent)
        if speech_factory is None:
            from .speech import LocalSpeech
            speech_factory = LocalSpeech
        self.bridge = bridge
        self.speech = speech_factory(parent=self)
        self.audio = (player_factory or QtSpeechPlayer)(parent=self)
        self._clock = clock or time.monotonic
        self.enabled = False
        self._closing = False
        self._closed = False
        self._speech_closed = False
        self._source = None
        self._video = None
        self._generation = 0
        self._epoch = 0
        self._timeline = Timeline()
        self._cache = {}
        self._pending = set()
        self._failures = set()
        self._lease = None
        self._active = False
        self._lease_pending = False
        self._renewed = -math.inf
        self._last_ack = -math.inf
        self._restoring = {}
        self._clip = None
        self._clip_token = None
        self._media_token = None
        self._loading_token = None
        self._media_ready = False
        self._ended = False
        self._frame = None
        self._ack_guard = None
        self._seek_serial = None
        self._last_status = ""
        self.speech.ready.connect(self._speech_ready)
        self.speech.failed.connect(self._speech_failed)
        self.speech.closed.connect(self._speech_finished)
        self.audio.prepared.connect(self._media_ready_event)
        self.audio.failed.connect(self._media_failed)
        self.audio.ended.connect(self._media_ended)
        self.bridge.dubbing_changed.connect(self._ack)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(100)
        self._watchdog.timeout.connect(self._watch)
        self._watchdog.start()

    @property
    def is_closed(self):
        return self._closed

    def _status(self, text):
        if text != self._last_status:
            self._last_status = text
            self.status.emit(text)

    def set_enabled(self, enabled):
        if self._closing:
            return
        self.enabled = bool(enabled)
        if not self.enabled:
            self.stop()
            self._status("英文配音已关闭 · 播放原声")
        else:
            self._failures.clear()
            self._status("英文配音已开启 · 等待 B 站字幕")

    def set_source(self, generation, video):
        key = self._source_key(video)
        if generation == self._generation and key == self._source:
            self._video = dict(video) if video else None
            return
        self.stop()
        self._generation, self._source = generation, key
        self._failures.clear()
        self._video = dict(video) if key else None
        self._timeline.clear()

    @staticmethod
    def _source_key(video):
        if not video or video.get("platform") != "bilibili" or not video.get("cid"):
            return None
        return (video.get("bridge_key") or (video.get("source_id", ""), video.get("tab_id")),
                video.get("document_start", 0), browser_identity(video))

    def set_cues(self, generation, cues):
        if generation != self._generation:
            return
        self._timeline.clear()
        self._timeline.add(cues)

    def _reset_speech(self):
        self._epoch += 1
        self._pending.clear()
        self.speech.reset(self._epoch)

    def stop(self):
        self._release()
        self.audio.stop()
        self._clip = self._clip_token = self._frame = None
        self._media_token = self._loading_token = None
        self._media_ready = self._ended = False
        self._seek_serial = None
        self._reset_speech()

    def _release(self):
        lease, video = self._lease, self._video
        self._lease = None
        self._active = self._lease_pending = False
        self._ack_guard = None
        self._renewed = -math.inf
        self._last_ack = -math.inf
        if lease and video:
            self._restoring[lease] = self._clock() + self.RESTORE_GRACE
            try:
                self.bridge.set_dubbing(video, False, lease)
            except Exception:
                # The page independently restores after its short lease ends.
                pass

    def tick(self, video, position, paused, rate):
        if not self.enabled or self._closing:
            return
        if (self._source is None or self._source_key(video) != self._source or position is None
                or video.get("closed") or video.get("ad_playing")
                or self._clock() - video.get("received_at", -math.inf) > self.STALE_SECONDS):
            self._release()
            self.audio.pause()
            self._status("英文配音等待 B 站播放连接 · 已恢复原声")
            return
        self._video = dict(video)
        if self._active and self._clock() - self._last_ack > self.STALE_SECONDS:
            self._release()
            self.audio.pause()
            self._status("网页配音确认已超时 · 已请求恢复原声")
            return
        if video.get("dubbing_protocol") != 1:
            self._release()
            self.audio.pause()
            self._status("请重新加载 0.1.4 桌面字幕连接器并刷新 B 站网页")
            return
        position, rate = float(position), float(rate or 1)
        if not math.isfinite(position) or not math.isfinite(rate) or rate <= 0:
            self._release()
            self.audio.pause()
            return
        if self._ack_guard:
            guard = self._ack_guard
            if (video.get("received_at", -math.inf) <= guard["stamp"]
                    or video.get("seek_serial", 0) < guard["serial"]):
                self.audio.pause()
                return
            self._ack_guard = None
        paused = bool(paused or video.get("buffering") or video.get("seeking"))
        serial = video.get("seek_serial", 0)
        jumped = serial != self._seek_serial and self._seek_serial is not None
        if self._frame is not None:
            old_position, stamp, old_paused, old_rate = self._frame
            expected = old_position if old_paused else old_position + max(0., self._clock() - stamp) * old_rate
            jumped = jumped or abs(position - expected) > .7
        self._frame = position, self._clock(), paused, rate
        self._seek_serial = serial
        if jumped:
            self.audio.stop()
            self._clip = self._clip_token = None
            self._media_token = self._loading_token = None
            self._media_ready = self._ended = False
            self._reset_speech()
        cue = self._timeline.at(position)
        if cue and position >= self._voice_end(cue):
            cue = None
        self._prepare_ahead(cue, position)
        if not self.enabled:
            return
        key = cue_key(cue) if cue else None
        text = english_text(cue) if cue else ""
        token = speech_token(text) if text else None
        if (key, token) != (self._clip, self._clip_token):
            self.audio.stop()
            self._clip, self._clip_token = key, token
            self._media_token = self._loading_token = None
            self._media_ready = self._ended = False
        if not cue:
            self.audio.pause()
            if paused and position >= float(video.get("duration") or math.inf) - .1:
                self._release()
            elif self._active:
                self._renew()
            return
        if token is None or token in self._failures:
            self._release()
            self.audio.pause()
            self._status("等待可用英文字幕 · 当前播放原声")
            return
        sample = self._cache.get(token)
        if sample is None:
            self._release()
            self.audio.pause()
            self._status("正在准备本地英文配音 · 当前播放原声")
            return
        if not self._media_ready:
            if getattr(self, "_loading_token", None) != (key, token):
                self._loading_token = key, token
                self._media_token = uuid.uuid4().hex
                self.audio.prepare(sample["path"], self._media_token)
            return
        if self._ended:
            if self._active:
                self._renew()
            return
        if paused and not self._active:
            return
        if not self._active:
            self._renew()
            return
        self._renew()
        if not self._active or not self._lease or not self.enabled:
            self.audio.pause()
            return
        stretch = max(.85, min(self.MAX_STRETCH, sample["duration"] / max(.1, self._voice_end(cue) - cue["start"])))
        audio_seconds = max(0., position - cue["start"]) * stretch
        if audio_seconds >= sample["duration"]:
            self.audio.pause()
            self._ended = True
            return
        self.audio.align(audio_seconds * 1000, max(.25, min(4., rate * stretch)), paused, force=jumped)
        self._status("英文配音已暂停" if paused else "英文配音播放中 · 跟随 B 站播放进度")

    def _voice_end(self, cue):
        index = bisect_right(self._timeline._starts, cue["start"])
        following = self._timeline._starts[index] if index < len(self._timeline._starts) else math.inf
        return min(cue["end"], following)

    def _prepare_ahead(self, current, position):
        cues = self._timeline.cues
        index = bisect_right(self._timeline._starts, position) - 1
        candidates = ([current] if current else []) + cues[max(0, index + 1):max(0, index + 1) + self.PREFETCH]
        for cue in candidates:
            if not self.enabled or self._closing:
                return
            if cue is None or self._voice_end(cue) <= position:
                continue
            text = english_text(cue)
            if not text:
                continue
            token = speech_token(text)
            if token in self._cache or token in self._pending or token in self._failures:
                continue
            self._pending.add(token)
            if not self.speech.request(text, token, self._epoch):
                self._pending.discard(token)

    def _renew(self):
        if (self._lease_pending or self._video is None
                or self._clock() - self._renewed < self.RENEW_SECONDS):
            return
        if self._lease is None:
            self._lease = uuid.uuid4().hex
        self._lease_pending = True
        self._renewed = self._clock()
        try:
            self.bridge.set_dubbing(self._video, True, self._lease)
        except Exception:
            self._lease_pending = False
            self._release()
            self.audio.pause()
            self._status("网页静音连接未就绪 · 当前播放原声")

    def _ack(self, event):
        lease = event.get("lease_id")
        status = event.get("status")
        if lease in self._restoring:
            if status in {"restored", "expired", "video_changed", "user_override"}:
                self._restoring.pop(lease, None)
                self._finish_shutdown()
            return
        if (lease != self._lease or self._source_key(event) != self._source or not self.enabled or self._closing):
            return
        self._lease_pending = False
        if status == "active":
            self._active = True
            self._last_ack = self._clock()
            serial = event.get("seek_serial", 0)
            if (event.get("paused") or event.get("seeking") or event.get("buffering")
                    or (self._seek_serial is not None and serial > self._seek_serial)):
                self._ack_guard = dict(stamp=self._clock(), serial=serial)
                self.audio.pause()
        else:
            self.audio.pause()
            self._release()
            if status == "user_override":
                self.enabled = False
                self.disabled.emit("英文配音已关闭 · 保留你调整后的原视频声音设置")
            else:
                self._status("英文配音连接中断 · 已请求恢复原声")

    def _speech_ready(self, event):
        if event.get("generation") != self._epoch or self._closing:
            return
        token = event.get("token")
        self._pending.discard(token)
        duration = float(event.get("duration", 0))
        if token and event.get("path") and math.isfinite(duration) and duration > 0:
            self._cache[token] = event
            while len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))

    def _speech_failed(self, event):
        if event.get("generation") != self._epoch or self._closing:
            return
        token = event.get("token")
        self._pending.discard(token)
        self._failures.add(token)
        if event.get("code") in {"no_english_voice", "voice_unavailable", "engine_unavailable"}:
            self.enabled = False
            self.stop()
            self.disabled.emit(event.get("message") or "本地英文语音不可用 · 播放原声")
            return
        if token == self._clip_token:
            self._release()
            self.audio.stop()
            self._status("本地英文语音生成失败 · 当前播放原声")

    def _media_ready_event(self, event):
        if event.get("token") == self._media_token and self._media_token and not self._closing:
            self._media_ready = True

    def _media_failed(self, event):
        if event.get("token") == self._media_token and self._media_token:
            self._failures.add(self._clip_token)
            self._release()
            self.audio.stop()
            self._status("英文配音播放失败 · 已请求恢复原声")

    def _media_ended(self, event):
        if event.get("token") == self._media_token and self._media_token:
            self._ended = True

    def _watch(self):
        now = self._clock()
        for lease, deadline in list(self._restoring.items()):
            if now >= deadline:
                self._restoring.pop(lease, None)
        if (self.enabled and self._video and self._lease
                and (now - self._video.get("received_at", -math.inf) > self.STALE_SECONDS
                     or (self._active and now - self._last_ack > self.STALE_SECONDS))):
            self._release()
            self.audio.pause()
            self._status("B 站播放连接已断开 · 已请求恢复原声")
        self._finish_shutdown()

    def shutdown(self):
        if self._closing:
            return
        self.stop()
        self.enabled = False
        self._closing = True
        self.speech.shutdown()
        self._speech_closed = bool(self.speech.is_closed)
        self._finish_shutdown()

    def _speech_finished(self):
        self._speech_closed = True
        self._finish_shutdown()

    def _finish_shutdown(self):
        if self._closing and not self._closed and self._speech_closed and not self._restoring:
            self._closed = True
            self._watchdog.stop()
            self.closed.emit()
