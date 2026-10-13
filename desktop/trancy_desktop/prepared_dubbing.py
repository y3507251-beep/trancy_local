"""Play an entirely prepared English/background mix against a Bilibili clock.

The video remains the source clock used by subtitles.  A piecewise linear map
slows long source intervals down, while the prepared mix plays at the user's
original speed.  No sentence transition replaces or stops the audio source.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from functools import cached_property
import json
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import time
import uuid
import wave

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .dubbing import QtSpeechPlayer
from .timeline import browser_identity


MAX_DURATION = 604800.
TIME_EPSILON = .001
MIN_VIDEO_FACTOR = .75


def _number(value, label, *, minimum=0., maximum=MAX_DURATION):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"配音文件的 {label} 无效")
    return float(value)


def _without_links(path):
    """Reject symlinks and Windows junction/reparse paths before resolving."""
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("配音文件不允许使用符号链接或目录联接")
    return path.resolve(strict=True)


@dataclass(frozen=True)
class PreparedDubbingSegment:
    source_start: float
    source_end: float
    audio_start: float
    audio_end: float
    kind: str

    @property
    def video_factor(self):
        return (self.source_end - self.source_start) / (self.audio_end - self.audio_start)

    def audio_position(self, source_position):
        position = min(self.source_end, max(self.source_start, source_position))
        return self.audio_start + (position - self.source_start) / self.video_factor


@dataclass(frozen=True)
class PreparedDubbingManifest:
    path: Path
    mix_path: Path
    source_duration: float
    output_duration: float
    segments: tuple[PreparedDubbingSegment, ...]
    voice_model: str
    reference: tuple[float, float]

    @classmethod
    def load(cls, path):
        try:
            manifest_path = _without_links(path)
            if not manifest_path.is_file() or manifest_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("配音索引文件无效或过大")
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
                raise ValueError("不支持此配音索引版本")
            relative = data.get("mix_path")
            if not isinstance(relative, str) or not relative or "\x00" in relative:
                raise ValueError("配音音频路径无效")
            # Check Windows paths even when a test or preparation runs elsewhere.
            windows = PureWindowsPath(relative)
            if (windows.drive or windows.root or ".." in windows.parts or
                    ":" in relative or Path(relative).is_absolute()):
                raise ValueError("配音音频必须位于索引所在目录内")
            mix_path = _without_links(manifest_path.parent / relative.replace("\\", "/"))
            if (not mix_path.is_relative_to(manifest_path.parent) or not mix_path.is_file() or
                    mix_path.suffix.lower() != ".wav"):
                raise ValueError("配音音频必须是索引目录内的 WAV 文件")
            source_duration = _number(data.get("source_duration"), "原视频时长", minimum=.001)
            output_duration = _number(data.get("output_duration"), "配音时长", minimum=.001)
            raw_segments = data.get("segments")
            if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 100000:
                raise ValueError("配音时间映射为空或过大")
            segments = []
            source_end = audio_end = 0.
            for raw in raw_segments:
                if not isinstance(raw, dict) or raw.get("kind") not in {"speech", "gap"}:
                    raise ValueError("配音时间段类型无效")
                values = [_number(raw.get(key), key) for key in
                          ("source_start", "source_end", "audio_start", "audio_end")]
                start, end, a_start, a_end = values
                if (end <= start or a_end <= a_start or end <= source_end or a_end <= audio_end or
                        abs(start - source_end) > TIME_EPSILON or abs(a_start - audio_end) > TIME_EPSILON or
                        end > source_duration + TIME_EPSILON or a_end > output_duration + TIME_EPSILON or
                        a_end - a_start + TIME_EPSILON < end - start):
                    raise ValueError("配音时间映射必须连续覆盖视频，且不能压缩原视频时长")
                if (end - source_end) / (a_end - audio_end) < MIN_VIDEO_FACTOR - .0001:
                    raise ValueError("此配音文件需要将视频降到 0.75 倍以下，请重新准备完整英文配音")
                # Roundoff below one millisecond must not leave lookup holes.
                segments.append(PreparedDubbingSegment(source_end, end, audio_end, a_end, raw["kind"]))
                source_end, audio_end = end, a_end
            if abs(source_end - source_duration) > TIME_EPSILON or abs(audio_end - output_duration) > TIME_EPSILON:
                raise ValueError("配音时间映射未覆盖完整视频")
            if output_duration + TIME_EPSILON < source_duration:
                raise ValueError("配音时长短于原视频")
            model = data.get("voice_model")
            if not isinstance(model, str) or not model.strip() or len(model) > 300:
                raise ValueError("配音音色模型信息无效")
            reference = data.get("reference")
            if not isinstance(reference, dict):
                raise ValueError("原人物音色参考片段无效")
            ref_start = _number(reference.get("start"), "音色参考起点", maximum=source_duration)
            ref_end = _number(reference.get("end"), "音色参考终点", maximum=source_duration)
            if ref_end <= ref_start:
                raise ValueError("原人物音色参考片段为空")
            with wave.open(str(mix_path), "rb") as audio:
                if audio.getcomptype() != "NONE" or audio.getframerate() <= 0 or audio.getnchannels() not in (1, 2):
                    raise ValueError("配音混音必须是 PCM WAV")
                actual_duration = audio.getnframes() / audio.getframerate()
                if audio.getnframes() * audio.getnchannels() * audio.getsampwidth() + 44 > mix_path.stat().st_size:
                    raise ValueError("配音音频内容不完整")
            if abs(actual_duration - output_duration) > .05:
                raise ValueError("配音音频时长与时间映射不一致")
            return cls(manifest_path, mix_path, source_duration, output_duration,
                       tuple(segments), model.strip(), (ref_start, ref_end))
        except (OSError, EOFError, wave.Error, UnicodeError, json.JSONDecodeError, TypeError, OverflowError) as exc:
            raise ValueError("配音索引或音频无法读取") from exc

    @cached_property
    def source_starts(self):
        return tuple(segment.source_start for segment in self.segments)

    def segment_at(self, source_position):
        position = _number(source_position, "播放位置")
        index = max(0, min(len(self.segments) - 1, bisect_right(self.source_starts, position) - 1))
        return self.segments[index]

    def map_position(self, source_position):
        segment = self.segment_at(source_position)
        return segment.audio_position(source_position), segment.video_factor

    def validate_rate(self, base_rate):
        base_rate = _number(base_rate, "播放速度", minimum=.1, maximum=4.)
        return base_rate


class PreparedDubbingSession(QObject):
    status = pyqtSignal(str)
    disabled = pyqtSignal(str)
    closed = pyqtSignal()

    STALE_SECONDS = 2.
    RENEW_SECONDS = .8
    RESTORE_GRACE = 1.8

    def __init__(self, bridge, parent=None, *, player=None, player_factory=None, clock=None):
        super().__init__(parent)
        self.bridge = bridge
        self.audio = player or (player_factory or QtSpeechPlayer)(parent=self)
        self._clock = clock or time.monotonic
        self.enabled = False
        self._closing = self._closed = False
        self._source = self._video = self.manifest = None
        self._generation = 0
        self._media_token = None
        self._media_ready = self._audio_started = self._audio_ended = False
        self._audio_running = False
        self._last_audio_rate = None
        self._lease = self._request_id = None
        self._active = self._pending = False
        self._rate_change_pending = False
        self._requested_rate = self._confirmed_rate = self._base_rate = self._lease_original_rate = None
        self._rate_report_pending = False
        self._rate_report_deadline = None
        self._renewed = self._last_ack = -math.inf
        self._restoring = {}
        self._seek_serial = self._ack_guard = None
        self._force_position = True
        self._finish_deadline = None
        self._dispatching = False
        self._deferred_acks = []
        self._last_status = ""
        self.audio.prepared.connect(self._media_prepared)
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

    @staticmethod
    def _source_key(video):
        if not video or video.get("platform") != "bilibili" or not video.get("cid"):
            return None
        return (video.get("bridge_key") or (video.get("source_id", ""), video.get("tab_id")),
                video.get("document_start", 0), browser_identity(video))

    def _status(self, message):
        if message != self._last_status:
            self._last_status = message
            self.status.emit(message)

    def _playback_rates(self, factor):
        # Never lower normal playback below 0.75x. A user who deliberately
        # chose an even slower speed keeps it; the mix follows that timeline.
        video_rate = max(min(self._base_rate, MIN_VIDEO_FACTOR), self._base_rate * factor)
        return video_rate, video_rate / factor

    def _pause_audio(self):
        if self._audio_running:
            self.audio.pause()
            self._audio_running = False

    def _continue_audio(self, rate=None):
        """An active rate transition must not pause, restart or seek the mix."""
        if rate is not None and (self._last_audio_rate is None or abs(rate - self._last_audio_rate) > .0001):
            # finish() changes rate without seeking, and does not call play()
            # again when QMediaPlayer is already playing this same source.
            self.audio.finish(rate, False)
            self._last_audio_rate = rate
        self._status("完整英文配音播放中 · 保留背景音乐，长句自动放慢视频")

    def _disable(self, message):
        was_enabled = self.enabled
        self.stop()
        self._status(message)
        if was_enabled:
            self.disabled.emit(message)

    def set_source(self, generation, video):
        source = self._source_key(video)
        if generation == self._generation and source == self._source:
            self._video = dict(video) if video else None
            return
        if self.enabled:
            self._disable("视频已切换 · 已关闭完整英文配音并恢复原声和原速")
        else:
            self.stop()
        self._generation, self._source = generation, source
        self._video = dict(video) if source else None
        self.manifest = None

    def set_manifest(self, path):
        manifest = PreparedDubbingManifest.load(path)
        if self._source is None:
            raise ValueError("请先选择要配音的 B 站视频")
        duration = self._video.get("duration")
        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
            if abs(duration - manifest.source_duration) > max(1., duration * .002):
                raise ValueError("已准备配音的时长与所选视频不一致，请重新准备")
        if self.enabled:
            self._disable("完整英文配音已更新 · 请重新开启")
        else:
            self.stop()
        self.manifest = manifest
        self._status("完整英文配音已准备 · 开启后保留背景音乐并跟随视频")
        return manifest

    def set_enabled(self, enabled):
        if self._closing:
            return
        if not enabled:
            self.stop()
            self._status("完整英文配音已关闭 · 已恢复原声和原速")
            return
        if self.enabled:
            return
        self.enabled = True
        if not self._source or self.manifest is None:
            self._disable("请先为所选 B 站视频准备完整英文配音")
            return
        try:
            self._base_rate = self.manifest.validate_rate(self._video.get("rate", 1.))
        except ValueError as exc:
            self._disable(str(exc))
            return
        self._status("完整英文配音已开启 · 正在载入背景音乐与英文混音")

    def _release(self):
        lease, video = self._lease, self._video
        self._lease = self._request_id = None
        self._active = self._pending = False
        self._rate_change_pending = False
        self._confirmed_rate = self._requested_rate = self._lease_original_rate = None
        self._rate_report_pending = False
        self._rate_report_deadline = None
        self._ack_guard = None
        self._renewed = self._last_ack = -math.inf
        if lease and video:
            self._restoring[lease] = self._clock() + self.RESTORE_GRACE
            try:
                self.bridge.set_dubbing(video, False, lease)
            except Exception:
                # The page lease also restores mute/rate when the desktop dies.
                pass

    def stop(self):
        self.enabled = False
        self._release()
        self.audio.stop()
        self._media_token = None
        self._media_ready = self._audio_started = self._audio_ended = False
        self._audio_running = False
        self._last_audio_rate = None
        self._base_rate = None
        self._seek_serial = self._ack_guard = None
        self._force_position = True
        self._finish_deadline = None

    def tick(self, video, position, paused, rate):
        if not self.enabled or self._closing:
            return
        now = self._clock()
        if (self._source_key(video) != self._source or not self._source or not video or
                video.get("closed") or video.get("ad_playing") or video.get("is_live") or
                type(video.get("received_at")) not in (int, float) or
                not math.isfinite(video["received_at"]) or now - video["received_at"] > self.STALE_SECONDS):
            self._disable("B 站连接已断开或视频已切换 · 已请求恢复原声和原速")
            return
        self._video = dict(video)
        if video.get("dubbing_protocol") != 1 or video.get("rate_control_protocol") != 1:
            self._disable("请重新加载最新版桌面字幕连接器并刷新 B 站网页，再开启完整英文配音")
            return
        try:
            position = _number(position, "播放位置", maximum=self.manifest.source_duration + .5)
            rate = _number(rate, "播放速度", minimum=.1, maximum=4.)
        except ValueError as exc:
            self._disable(str(exc) + " · 已请求恢复原声和原速")
            return
        if self._lease and (now - self._renewed > self.STALE_SECONDS if not self._active
                            else now - self._last_ack > self.STALE_SECONDS):
            self._disable("网页配音确认已超时 · 已请求恢复原声和原速")
            return
        serial = video.get("seek_serial", 0)
        serial = serial if type(serial) is int and serial >= 0 else 0
        waiting = bool(video.get("buffering") or video.get("seeking"))
        paused = bool(paused or waiting)
        if paused and (not video.get("ended") or waiting):
            self._pause_audio()
        jumped = self._seek_serial is not None and serial != self._seek_serial
        # The new page protocol explicitly increments seek_serial. Ordinary
        # browser-clock jitter, especially around rate changes, is not a seek;
        # QtSpeechPlayer handles sustained drift without repeated forced jumps.
        self._seek_serial = serial
        if jumped:
            self._pause_audio()
            self._active = False
            self._force_position = True
            self._audio_ended = False
            self._finish_deadline = None
        audio_position, factor = self.manifest.map_position(position)
        desired, audio_rate = self._playback_rates(factor)
        if self._audio_ended and not jumped:
            if video.get("ended") or position >= self.manifest.source_duration:
                self._release()
                self._status("完整英文配音播放完毕 · 已恢复原声和原速")
            elif self._active:
                self._renew(desired)
            return
        if video.get("ended") and not self._audio_started:
            self._release()
            return
        if not self._media_ready:
            if self._media_token is None:
                self._media_token = uuid.uuid4().hex
                self.audio.prepare(self.manifest.mix_path, self._media_token)
            return
        if self._ack_guard:
            guard = self._ack_guard
            if video["received_at"] <= guard["stamp"] or serial < guard["serial"]:
                self._pause_audio()
                return
            self._ack_guard = None
        if paused and not self._active and not self._lease:
            self._pause_audio()
            return
        # Once this mix owns an acknowledged mute lease, normal segment rate
        # changes keep its existing samples flowing while the command is in
        # flight. First acquisition and actual seeks still require a fresh ACK.
        changed_rate = self._requested_rate is None or abs(desired - self._requested_rate) > .0001
        if jumped or changed_rate or (self._active and not self._pending and
                                     abs(desired - self._confirmed_rate) > .0001):
            self._renew(desired, force=True, transition=self._active and not jumped)
        else:
            self._renew(desired)
        if (not self.enabled or not self._active or not self._lease or
                self._confirmed_rate is None or
                abs(self._playback_rates(factor)[0] - desired) > .0001 or
                (self._pending and jumped)):
            self._pause_audio()
            return
        if self._ack_guard:
            self._pause_audio()
            return
        continuous = (self._audio_running and self._audio_started and not self._force_position and
                      (not paused or (video.get("ended") and not waiting)))
        if self._rate_change_pending or abs(desired - self._confirmed_rate) > .0001:
            if continuous:
                self._continue_audio()
            else:
                self._pause_audio()
            return
        if self._rate_report_pending:
            if abs(rate - self._confirmed_rate) > .0001:
                if continuous:
                    # The ACK confirms the applied rate; an older progress
                    # report can catch up without interrupting or seeking audio.
                    self._continue_audio(audio_rate)
                else:
                    self._pause_audio()
                return
            self._rate_report_pending = False
            self._rate_report_deadline = None
        elif not self._pending and abs(rate - self._confirmed_rate) > .0001:
            self._disable("已关闭完整英文配音 · 保留你调整后的视频速度")
            return
        if video.get("ended") and self._audio_started:
            # EndOfMedia, not a predicted timestamp, proves the last samples
            # were consumed. Natural video end must not cut off the last word.
            if self._finish_deadline is None:
                remaining = self.audio.remaining_seconds()
                self._finish_deadline = now + max(3., float(remaining or 0.) + 1.)
            self.audio.finish(audio_rate, waiting)
            self._audio_running = not waiting
            self._last_audio_rate = audio_rate
            self._status("完整英文配音正在读完片尾")
            return
        self.audio.align(audio_position * 1000., audio_rate, paused, force=self._force_position)
        self._audio_running = not paused
        self._last_audio_rate = audio_rate
        self._force_position = False
        if not paused:
            self._audio_started = True
        self._status("完整英文配音已暂停" if paused else
                     "完整英文配音播放中 · 保留背景音乐，长句自动放慢视频")

    def _renew(self, desired, *, force=False, transition=False):
        now = self._clock()
        if not self.enabled or self._video is None or (not force and
                (self._pending or now - self._renewed < self.RENEW_SECONDS)):
            return
        if self._lease is None:
            self._lease = uuid.uuid4().hex
        self._pending = True
        self._rate_change_pending = bool(transition)
        self._renewed = now
        self._requested_rate = desired
        self._dispatching = True
        try:
            self._request_id = self.bridge.set_dubbing(self._video, True, self._lease, playback_rate=desired)
        except Exception:
            self._dispatching = False
            self._deferred_acks.clear()
            self._disable("网页声音和速度控制未就绪 · 已请求恢复原声和原速")
            return
        self._dispatching = False
        pending, self._deferred_acks = self._deferred_acks, []
        for event in pending:
            self._ack(event)

    def _ack(self, event):
        if self._dispatching:
            self._deferred_acks.append(dict(event))
            return
        lease, status = event.get("lease_id"), event.get("status")
        if lease in self._restoring:
            if status in {"restored", "expired", "video_changed", "user_override"}:
                self._restoring.pop(lease, None)
                self._finish_shutdown()
            return
        if (lease != self._lease or self._source_key(event) != self._source or
                not self.enabled or self._closing):
            return
        if status in {"active", "error", "timeout"} and event.get("request_id") != self._request_id:
            return
        if status != "active":
            message = ("已关闭完整英文配音 · 保留你调整后的视频声音和速度" if status == "user_override" else
                       "完整英文配音连接中断 · 已请求恢复原声和原速")
            self._disable(message)
            return
        try:
            actual = _number(event.get("rate"), "网页确认速度", minimum=.1, maximum=4.)
            original = self.manifest.validate_rate(event.get("original_rate"))
        except ValueError as exc:
            self._disable(str(exc) + " · 已请求恢复原声和原速")
            return
        if abs(actual - self._requested_rate) > .0001:
            self._disable("网页未能应用所需播放速度 · 已请求恢复原声和原速")
            return
        if self._lease_original_rate is not None and abs(original - self._lease_original_rate) > .0001:
            self._disable("网页原始速度确认发生变化 · 已请求恢复原声和原速")
            return
        self._lease_original_rate = self._base_rate = original
        if self._confirmed_rate is None or abs(actual - self._confirmed_rate) > .0001:
            self._rate_report_pending = True
            self._rate_report_deadline = self._clock() + self.STALE_SECONDS
        self._confirmed_rate = actual
        self._pending = False
        self._rate_change_pending = False
        self._active = True
        self._last_ack = self._clock()
        serial = event.get("seek_serial", 0)
        if ((event.get("paused") and not event.get("ended")) or event.get("seeking") or event.get("buffering") or
                (self._seek_serial is not None and serial > self._seek_serial)):
            self._ack_guard = dict(stamp=self._clock(), serial=serial)
            self._pause_audio()
        if self._seek_serial is not None and serial < self._seek_serial:
            self._active = False
            self._renewed = -math.inf
            self._pause_audio()

    def _media_prepared(self, event):
        if self._media_token and event.get("token") == self._media_token and not self._closing:
            self._media_ready = True

    def _media_failed(self, event):
        if self._media_token and event.get("token") == self._media_token:
            self._disable((event.get("message") or "完整英文混音播放失败") + " · 已请求恢复原声和原速")

    def _media_ended(self, event):
        if self._media_token and event.get("token") == self._media_token:
            self._audio_ended = True
            self._audio_running = False
            self._finish_deadline = None
            if self._video and self._video.get("ended"):
                self._release()
                self._status("完整英文配音播放完毕 · 已恢复原声和原速")

    def _watch(self):
        now = self._clock()
        for lease, deadline in tuple(self._restoring.items()):
            if now >= deadline:
                self._restoring.pop(lease, None)
        if self.enabled and self._lease and self._video:
            stamp = self._last_ack if self._active else self._renewed
            if (now - self._video.get("received_at", -math.inf) > self.STALE_SECONDS or
                    now - stamp > self.STALE_SECONDS):
                self._disable("B 站连接已断开 · 已请求恢复原声和原速")
            elif self._finish_deadline is not None and now >= self._finish_deadline:
                self._disable("完整英文配音结束确认超时 · 已请求恢复原声和原速")
            elif self._rate_report_deadline is not None and now >= self._rate_report_deadline:
                self._disable("网页播放速度未能保持同步 · 已请求恢复原声和原速")
        self._finish_shutdown()

    def shutdown(self):
        if self._closing:
            return
        self.stop()
        self._closing = True
        self._finish_shutdown()

    def _finish_shutdown(self):
        if self._closing and not self._closed and not self._restoring:
            self._closed = True
            self._watchdog.stop()
            self.closed.emit()
