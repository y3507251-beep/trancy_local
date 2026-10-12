from __future__ import annotations

import threading
from collections import defaultdict, deque

from PyQt6.QtCore import QObject, pyqtSignal

from .translation import TranslationClient, detect_target


class TranslationQueue(QObject):
    translated = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    status = pyqtSignal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._jobs = deque()
        self._condition = threading.Condition()
        self._generation = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="TrancyTranslation", daemon=True)
        self._thread.start()

    def reset(self, generation):
        with self._condition:
            self._generation = generation
            self._jobs.clear()

    def enqueue(self, generation, cues, language, mode, realtime=False):
        with self._condition:
            if self._closed or generation != self._generation:
                return
            for offset in range(0, len(cues), 12):
                self._jobs.append((generation, cues[offset:offset + 12], language, mode))
            if realtime and len(self._jobs) > 3:
                while len(self._jobs) > 3:
                    self._jobs.popleft()
                self.status.emit("翻译暂时跟不上播放速度，正在优先处理最新语音。")
            self._condition.notify()

    def shutdown(self):
        with self._condition:
            self._closed = True
            self._jobs.clear()
            self._condition.notify_all()

    def _run(self):
        clients = {}
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._jobs)
                if self._closed:
                    return
                generation, cues, language, mode = self._jobs.popleft()
            try:
                groups = defaultdict(list)
                for cue in cues:
                    source, target = detect_target(cue["text"], language)
                    groups[(source, target)].append(cue)
                if mode not in clients:
                    clients[mode] = TranslationClient(mode=mode, bridge=self.bridge)
                client = clients[mode]
                output = []
                for (source, target), entries in groups.items():
                    with self._condition:
                        if self._closed or generation != self._generation:
                            break
                    translated = client.translate([c["text"] for c in entries], source, target)
                    if len(translated) != len(entries):
                        raise ValueError("翻译返回数量与字幕数量不一致")
                    output.extend({**cue, "translation": value, "source_language": source,
                                   "target_language": target} for cue, value in zip(entries, translated))
                with self._condition:
                    valid = not self._closed and generation == self._generation
                if valid and output:
                    self.translated.emit({"generation": generation, "cues": output})
            except Exception as error:
                with self._condition:
                    valid = not self._closed and generation == self._generation
                if valid:
                    self.failed.emit({"generation": generation, "message": str(error)[:400], "cues": cues})
