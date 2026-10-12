"""Playback-aware, bounded subtitle translation scheduling.

The extension prepares a small current batch first, then translates upcoming
captions. The desktop follows that policy instead of queuing a whole video.
Requests already sent cannot be recalled, but two of the four Google slots are
kept free of prefetch work so a seek need not wait behind that work.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
import math
import re
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from .timeline import clean_cues, cue_key
from .translation import TranslationClient, detect_target


@dataclass
class _Request:
    token: int
    generation: int
    epoch: int
    mode: str
    source: str
    target: str
    keys: list[tuple]
    urgent: bool


class TranslationQueue(QObject):
    translated = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    status = pyqtSignal(str)
    closed = pyqtSignal()

    CURRENT_BATCH = 3
    PREFETCH_BATCH = 30
    WINDOW_SIZE = 90
    CACHE_SIZE = 4000

    def __init__(self, bridge, parent=None, *, client_factory=None, max_workers=4):
        super().__init__(parent)
        self.bridge = bridge
        self._factory = client_factory or TranslationClient
        self._condition = threading.Condition()
        self._generation = 0
        self._epoch = 0
        self._reset_serial = 0
        self._closed = False
        self._remaining_workers = max(1, min(4, int(max_workers)))
        self._prefetch_limit = max(1, self._remaining_workers - 2)
        self._cache = OrderedDict()
        self._inflight = {}
        self._running = {}
        self._next_token = 0
        self._clear_source_locked()
        self._threads = [threading.Thread(target=self._run,
                         name=f"TrancyTranslation-{index + 1}", daemon=True)
                         for index in range(self._remaining_workers)]
        for thread in self._threads:
            thread.start()

    @property
    def epoch(self):
        with self._condition:
            return self._epoch

    @property
    def is_closed(self):
        """True only after all network workers have actually exited."""
        with self._condition:
            return self._closed and self._remaining_workers == 0

    def _clear_source_locked(self):
        self._cues = []
        self._max_ends = []
        self._position = 0.
        self._language = ""
        self._mode = "google"
        self._engine_key = None
        self._desired = OrderedDict()
        self._window_start = None
        self._resolved = {}
        self._delivered = {}
        self._errors = OrderedDict()

    def reset(self, generation):
        """Cancel pending work. Successful Google text cache survives a reset."""
        with self._condition:
            self._generation = generation
            self._epoch += 1
            self._reset_serial += 1
            self._clear_source_locked()
            self._condition.notify_all()

    def _configure_locked(self, language, mode, engine_key):
        if mode not in {"google", "plugin"}:
            raise ValueError("翻译模式只支持 google 或 plugin")
        changed = mode != self._mode or engine_key != self._engine_key
        self._language = str(language or "")
        if changed:
            self._epoch += 1
            self._resolved.clear()
            self._delivered.clear()
            self._errors.clear()
            self._window_start = None
        self._mode, self._engine_key = mode, engine_key

    def update_window(self, generation, cues, position, language, mode, *,
                      seek=False, engine_key=None, retry=False):
        """Replace known cues; translate only the current and next 90 captions.

        Call this when native/ASR cues change, not on every display tick. Text
        cache keys do not include cue times, so forced-alignment corrections
        reuse the translation. ``engine_key`` may identify the plugin's selected
        engine; when absent its cache is restricted to this source generation.
        """
        cleaned = clean_cues(cues)
        try:
            position = float(position)
        except (TypeError, ValueError):
            return
        if not math.isfinite(position):
            return
        with self._condition:
            if self._closed or generation != self._generation:
                return
            self._configure_locked(language, mode, engine_key)
            self._set_position_locked(max(0., position), seek)
            self._cues = cleaned
            self._max_ends = []
            maximum = 0.
            for cue in cleaned:
                maximum = max(maximum, cue["end"])
                self._max_ends.append(maximum)
                if cue.get("translation"):
                    self._resolved[cue_key(cue)] = (self._text_key(cue), cue["translation"])
                    self._delivered[cue_key(cue)] = self._text_key(cue)
            self._refresh_window_locked(force=True)
            if retry:
                for cue in self._desired.values():
                    self._errors.pop(self._text_key(cue), None)
            self._condition.notify_all()

    def set_position(self, generation, position, *, seek=False):
        """Cheap playback tick; a seek discards all unsent old-window work."""
        try:
            position = float(position)
        except (TypeError, ValueError):
            return
        if not math.isfinite(position):
            return
        with self._condition:
            if self._closed or generation != self._generation:
                return
            jumped = self._set_position_locked(max(0., position), seek)
            changed = self._refresh_window_locked(force=jumped)
            if jumped or changed:
                self._condition.notify_all()

    def _set_position_locked(self, position, seek):
        jumped = (seek or position < self._position - .75 or
                  position > self._position + 5)
        self._position = position
        if jumped:
            self._epoch += 1
            self._window_start = None
            self._delivered.clear()
        return jumped

    def _refresh_window_locked(self, force=False):
        index = bisect_right(self._max_ends, self._position)
        if not force and index == self._window_start:
            return False
        self._window_start = index
        candidates = (self._cues[offset] for offset in range(index, len(self._cues))
                      if self._cues[offset]["end"] > self._position)
        self._desired = OrderedDict()
        for cue in candidates:
            self._desired[cue_key(cue)] = cue
            if len(self._desired) >= self.WINDOW_SIZE:
                break
        return True

    def shutdown(self):
        """Stop new work immediately; closed follows bounded network timeouts."""
        with self._condition:
            self._closed = True
            self._desired.clear()
            self._condition.notify_all()

    def wait_closed(self, timeout=2):
        """For controlled tests; UI code should wait for the closed signal."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0., deadline - time.monotonic()))
        return self.is_closed

    def _text_key(self, cue):
        source, target = detect_target(cue["text"], cue.get("source_language") or self._language)
        namespace = (self._mode, self._engine_key)
        if self._mode == "plugin" and self._engine_key is None:
            namespace = ("plugin", self._reset_serial, self._generation)
        text = re.sub(r"\s+", " ", cue["text"]).strip()
        return namespace, source, target, text

    @staticmethod
    def _output(cue, key, translation):
        return {**cue, "translation": translation, "source_language": key[1],
                "target_language": key[2]}

    def _cached_event_locked(self):
        output = []
        for identity, cue in self._desired.items():
            key = self._text_key(cue)
            resolved = self._resolved.get(identity)
            if not resolved or resolved[0] != key:
                if key not in self._cache:
                    continue
                resolved = (key, self._cache[key])
                self._cache.move_to_end(key)
                self._resolved[identity] = resolved
            if self._delivered.get(identity) != key:
                self._delivered[identity] = key
                output.append(self._output(cue, key, resolved[1]))
        if output:
            return {"generation": self._generation, "epoch": self._epoch, "cues": output}
        return None

    def _take_request_locked(self):
        desired = list(self._desired.items())
        urgent_ids = {identity for identity, _ in desired[:self.CURRENT_BATCH]}
        missing = []
        for identity, cue in desired:
            key = self._text_key(cue)
            if ((identity in self._resolved and self._resolved[identity][0] == key) or
                    key in self._cache or key in self._inflight or key in self._errors):
                continue
            missing.append((identity, key))
        urgent = [key for identity, key in missing if identity in urgent_ids]
        if urgent:
            candidates, maximum = urgent, self.CURRENT_BATCH
            is_urgent = True
        else:
            # Match the extension's quick initial batch: start bulk prefetch
            # after those current captions finish, not alongside their request.
            if any(request.urgent and request.generation == self._generation and
                   request.epoch == self._epoch for request in self._running.values()):
                return None
            if sum(not request.urgent for request in self._running.values()) >= self._prefetch_limit:
                return None
            candidates, maximum = [key for _, key in missing], self.PREFETCH_BATCH
            is_urgent = False
        if not candidates:
            return None
        first = candidates[0]
        keys = list(dict.fromkeys(key for key in candidates if key[:3] == first[:3]))[:maximum]
        self._next_token += 1
        request = _Request(self._next_token, self._generation, self._epoch,
                           self._mode, first[1], first[2], keys, is_urgent)
        self._running[request.token] = request
        for key in keys:
            self._inflight[key] = request.token
        return request

    def _emit_if_current(self, signal, event):
        if not event:
            return
        with self._condition:
            valid = (not self._closed and event["generation"] == self._generation and
                     event["epoch"] == self._epoch)
        if valid:
            signal.emit(event)

    def _complete_locked(self, request, translations=None, error=None):
        self._running.pop(request.token, None)
        for key in request.keys:
            if self._inflight.get(key) == request.token:
                self._inflight.pop(key, None)
        if translations is not None:
            for key, value in zip(request.keys, translations):
                self._cache[key] = value
                self._cache.move_to_end(key)
                self._errors.pop(key, None)
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        failure = None
        if (error and not self._closed and request.generation == self._generation and
                request.epoch == self._epoch):
            keys = set(request.keys)
            failed = []
            for cue in self._desired.values():
                key = self._text_key(cue)
                if key in keys:
                    message = str(error)[:400]
                    self._errors[key] = message
                    failed.append({**cue, "source_language": key[1], "target_language": key[2],
                                   "translation_error": message})
            while len(self._errors) > 1000:
                self._errors.popitem(last=False)
            if failed:
                failure = {"generation": self._generation, "epoch": self._epoch,
                           "message": str(error)[:400], "cues": failed}
        self._condition.notify_all()
        return failure

    def _run(self):
        clients = {}
        try:
            while True:
                with self._condition:
                    cached = request = None
                    while not self._closed:
                        cached = self._cached_event_locked()
                        request = self._take_request_locked()
                        if cached or request:
                            break
                        self._condition.wait()
                    if self._closed:
                        return
                self._emit_if_current(self.translated, cached)
                if request is None:
                    continue
                values = error = None
                try:
                    if request.mode not in clients:
                        clients[request.mode] = self._factory(mode=request.mode, bridge=self.bridge,
                                                             timeout=10 if request.mode == "google" else 30)
                    with self._condition:
                        valid = (not self._closed and request.generation == self._generation and
                                 request.epoch == self._epoch)
                        if not valid:
                            self._complete_locked(request)
                    if not valid:
                        continue
                    values = clients[request.mode].translate([key[3] for key in request.keys],
                                                            request.source, request.target)
                    if (len(values) != len(request.keys) or
                            any(not isinstance(value, str) or not value.strip() for value in values)):
                        raise ValueError("翻译返回数量或内容与字幕不一致")
                except Exception as exception:
                    values, error = None, exception
                with self._condition:
                    failure = self._complete_locked(request, values, error)
                    cached = self._cached_event_locked() if not self._closed else None
                self._emit_if_current(self.translated, cached)
                self._emit_if_current(self.failed, failure)
        finally:
            for client in clients.values():
                try:
                    if hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
            with self._condition:
                self._remaining_workers -= 1
                finished = self._closed and self._remaining_workers == 0
                self._condition.notify_all()
            if finished:
                self.closed.emit()
