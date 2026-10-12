"""Word-follow spans: real ASR words first, Trancy's uniform fallback otherwise."""
from __future__ import annotations

import math
import re

WORD = re.compile(r"\w+(?:['’\-]\w+)*", re.UNICODE)


def word_spans(text, start, end, words=None):
    tokens = list(WORD.finditer(text))
    if not tokens:
        return [], "static"
    try:
        start, end = float(start), float(end)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            return [], "static"
    except (TypeError, ValueError):
        return [], "static"
    starts = [start + (end - start) * i / len(tokens) for i in range(len(tokens))]
    matched = [False] * len(tokens)
    cursor = 0
    for word in words or []:
        try:
            value = str(word["text"]).strip()
            time = float(word["start"])
            if not value or not math.isfinite(time) or time < start - .1 or time > end + .1:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        match = re.search(re.escape(value), text[cursor:], re.IGNORECASE)
        if match is None:
            continue
        begin, finish = cursor + match.start(), cursor + match.end()
        cursor = finish
        for i, token in enumerate(tokens):
            if begin <= token.start() and token.end() <= finish:
                starts[i], matched[i] = min(end, max(start, time)), True
    if not all(matched) or any(a > b for a, b in zip(starts, starts[1:])):
        matched = [False] * len(tokens)
        starts = [start + (end - start) * i / len(tokens) for i in range(len(tokens))]
    spans = []
    for i, token in enumerate(tokens):
        begin = 0 if i == 0 else token.start()
        finish = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        spans.append({"begin": begin, "end": finish, "start": starts[i]})
    return spans, "words" if all(matched) else "estimated"
