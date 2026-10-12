"""A small, bounded audio buffer driven by the actual player clock."""
from __future__ import annotations

import math


class PlaybackBuffer:
    first_seconds = 12.0
    chunk_seconds = 20.0
    lead_seconds = 35.0
    paused_lead_seconds = 10.0

    def __init__(self, coverage=(), duration=None):
        self.coverage = []
        self.duration = None
        self._first = True
        self.set_duration(duration)
        for start, end in coverage:
            self.mark(start, end)

    def set_duration(self, duration):
        try:
            value = float(duration)
            if math.isfinite(value) and value > 0:
                self.duration = value
        except (ValueError, TypeError):
            pass

    def seek(self):
        self._first = True

    def mark(self, start, end):
        start, end = float(start), float(end)
        if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end:
            return
        merged = []
        for left, right in sorted([*self.coverage, (start, end)]):
            if merged and left <= merged[-1][1] + .05:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        self.coverage = merged

    @property
    def complete(self):
        return bool(self.duration and self.coverage and self.coverage[0][0] <= .05
                    and self.coverage[0][1] >= self.duration - .05)

    def next_window(self, position, *, paused=False):
        position = float(position)
        if not math.isfinite(position) or position < 0:
            return None
        end = position + (self.paused_lead_seconds if paused else self.lead_seconds)
        if self.duration:
            end = min(end, self.duration)
        cursor = max(0.0, position - 2.0)
        if cursor >= end:
            return None
        for left, right in self.coverage:
            if right <= cursor + .05:
                continue
            if left > cursor + .05:
                end = min(end, left)
                break
            cursor = max(cursor, right)
        if cursor >= end - .05:
            return None
        if (not self._first and end - cursor < 8
                and (self.duration is None or end < self.duration - .05)):
            return None
        length = self.first_seconds if self._first else self.chunk_seconds
        self._first = False
        return round(cursor, 4), round(min(end, cursor + length), 4)
