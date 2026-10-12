from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from pathlib import Path


def cue_key(cue: dict) -> str:
    return f"{float(cue['start']):.3f}:{float(cue['end']):.3f}:{cue['text'].strip()}"


def clean_cues(cues) -> list[dict]:
    result = []
    for cue in cues or []:
        try:
            start, end = float(cue["start"]), float(cue["end"])
            text = str(cue["text"]).strip()
            if text and math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                result.append({**cue, "start": start, "end": end, "text": text})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda c: (c["start"], c["end"]))


class Timeline:
    """All times are seconds, matching SubtitleService and desktop players."""

    def __init__(self):
        self._by_key = {}
        self.cues = []
        self._starts = []
        self._max_ends = []

    def clear(self):
        self._by_key.clear()
        self.cues.clear()
        self._starts.clear()
        self._max_ends.clear()

    def _index(self):
        self.cues = sorted(self._by_key.values(), key=lambda c: (c["start"], c["end"]))
        self._starts = [c["start"] for c in self.cues]
        self._max_ends = []
        maximum = 0.0
        for cue in self.cues:
            maximum = max(maximum, cue["end"])
            self._max_ends.append(maximum)

    def add(self, cues) -> list[dict]:
        added = []
        for cue in clean_cues(cues):
            key = cue_key(cue)
            if key not in self._by_key:
                added.append(cue)
                self._by_key[key] = dict(cue)
            else:
                current = self._by_key[key]
                for field, value in cue.items():
                    if field == "translation" and not value:
                        continue
                    if field == "words" and not value and current.get("words"):
                        continue
                    current[field] = value
        if added:
            self._index()
        return added

    def replace_job(self, job_id, cues):
        """Final/aligned cues replace that job's provisional time boundaries."""
        previous = {key: cue for key, cue in self._by_key.items() if cue.get("_job_id") == job_id}
        for key in previous:
            del self._by_key[key]
        added = []
        for cue in clean_cues(cues):
            cue = dict(cue, _job_id=job_id)
            key = cue_key(cue)
            old = previous.get(key)
            if old:
                cue = {**old, **cue}
            else:
                added.append(cue)
            self._by_key[key] = cue
        self._index()
        return added

    def update_translations(self, cues):
        updated = []
        for cue in clean_cues(cues):
            key = cue_key(cue)
            if key in self._by_key and cue.get("translation"):
                current = self._by_key[key]
                for field in ("translation", "source_language", "target_language"):
                    if field in cue:
                        current[field] = cue[field]
                current.pop("translation_error", None)
                updated.append(self._by_key[key])
        return updated

    def translation_failed(self, cues, message):
        for cue in clean_cues(cues):
            current = self._by_key.get(cue_key(cue))
            if current is not None and not current.get("translation"):
                current["translation_error"] = str(message)[:400]

    def trim_before(self, position):
        self._by_key = {key: cue for key, cue in self._by_key.items() if cue["end"] >= position}
        self._index()

    def at(self, position: float) -> dict | None:
        index = bisect_right(self._starts, position) - 1
        while index >= 0:
            cue = self.cues[index]
            if cue["start"] <= position < cue["end"]:
                return cue
            if self._max_ends[index] <= position:
                break
            index -= 1
        return None

    def near(self, position, limit=40):
        index = max(0, bisect_right(self._starts, position) - 1)
        return self.cues[max(0, index - 3):index + limit]


def file_identity(path: str) -> str:
    source = Path(path).resolve()
    stat = source.stat()
    return f"file:{str(source).casefold()}:{stat.st_size}:{stat.st_mtime_ns}"


def browser_identity(video: dict) -> str:
    platform = video.get("platform", "")
    video_id = video.get("video_id") or video.get("id") or video.get("bvid") or ""
    page = video.get("page", 1)
    cid = video.get("cid") or ""
    return f"{platform}:{video_id}:{page}:{cid}"


class CaptionCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, identity, model, translation_mode):
        key = json.dumps([identity, model, translation_mode, "zh-en-words-v2"], ensure_ascii=False)
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def read(self, identity, model, translation_mode):
        try:
            value = json.loads(self._path(identity, model, translation_mode).read_text(encoding="utf-8"))
            return clean_cues(value["cues"]) if value.get("complete") else []
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def write(self, identity, model, translation_mode, cues):
        cleaned = clean_cues(cues)
        if not cleaned or any(not c.get("translation") for c in cleaned):
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(identity, model, translation_mode).write_text(
            json.dumps({"complete": True, "cues": cleaned}, ensure_ascii=False), encoding="utf-8")
        return True

    def _buffer_path(self, identity, model, translation_mode):
        key = json.dumps([identity, model, translation_mode, "zh-en-buffer-v3"], ensure_ascii=False)
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def read_bundle(self, identity, model, translation_mode):
        """Resume recognized portions without requiring the whole film's translation."""
        try:
            path = self._buffer_path(identity, model, translation_mode)
            if path.stat().st_size > 32 * 1024 * 1024:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != 3:
                return {}
            coverage = []
            for start, end in value.get("coverage", []):
                start, end = float(start), float(end)
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                    coverage.append((start, end))
            return dict(cues=clean_cues(value.get("cues")), coverage=coverage,
                        duration=value.get("duration"), corpus_complete=bool(value.get("corpus_complete")),
                        source_kind=value.get("source_kind", "asr"))
        except (OSError, ValueError, KeyError, TypeError):
            old = self.read(identity, model, translation_mode)
            return dict(cues=old, coverage=[], corpus_complete=True, source_kind="asr") if old else {}

    def write_bundle(self, identity, model, translation_mode, cues, *, coverage=(), duration=None,
                     corpus_complete=False, source_kind="asr"):
        cleaned = clean_cues(cues)
        if not cleaned and not coverage:
            return False
        # Job IDs and error/queue status do not belong to a durable caption.
        cleaned = [{k: v for k, v in cue.items() if not k.startswith("_") and k != "translation_error"}
                   for cue in cleaned]
        self.root.mkdir(parents=True, exist_ok=True)
        self._buffer_path(identity, model, translation_mode).write_text(json.dumps(
            dict(schema=3, cues=cleaned, coverage=list(coverage), duration=duration,
                 corpus_complete=bool(corpus_complete), source_kind=source_kind), ensure_ascii=False),
            encoding="utf-8")
        return True
