"""The extension's built-in Google translation, or its configured subtitle engine.

Plugin mode keeps account tokens and provider keys inside the extension. Google
mode mirrors background.js -> Cl -> legacy-mt.google: form-encoded repeated q,
whitespace normalization, and the same translate_a/t endpoint.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import re
import threading
import time
from urllib.parse import urlencode

import requests


class TranslationError(RuntimeError):
    pass


def detect_target(text: str, detected_language: str = "") -> tuple[str, str]:
    """Chinese speech -> English; English speech -> simplified Chinese.

Script evidence wins for Chinese sentences containing English terms. ASR's
language is used for a short phrase without enough letters to determine it.
    """
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if chinese >= 2 or (chinese and latin < 4):
        return "zh-CN", "en"
    if latin >= 2:
        return "en", "zh-CN"
    if detected_language.lower().startswith(("zh", "chinese", "cmn")):
        return "zh-CN", "en"
    return "en", "zh-CN"


def _decode_entities(text: str) -> str:
    # Deliberately matches Cl's Nl decoder, without stripping literal <> text.
    for old, new in (("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'"),
                     ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(old, new)
    return text


class TranslationClient:
    # Every worker has its own reusable connection, while rate limiting spans
    # workers: four simultaneous requests must not become four startup bursts.
    _google_rate_lock = threading.Lock()
    _google_next_request = 0.0

    def __init__(self, mode: str = "google", bridge=None, timeout: float | None = None):
        if mode not in {"google", "plugin"}:
            raise ValueError("翻译模式只支持 google 或 plugin")
        self.mode, self.bridge = mode, bridge
        self.timeout = timeout if timeout is not None else (10 if mode == "google" else 30)
        self._cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = threading.Lock()
        self._session = requests.Session()

    def close(self):
        self._session.close()

    @classmethod
    def _wait_google_slot(cls):
        with cls._google_rate_lock:
            delay = cls._google_next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            cls._google_next_request = time.monotonic() + .15

    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        if not texts:
            return []
        if source not in {"en", "zh", "zh-CN", "zh-TW", "auto"} or target not in {"en", "zh-CN", "zh-TW"}:
            raise TranslationError("桌面字幕当前支持中文和英文互译")
        if any(not isinstance(t, str) or len(t) > 30000 for t in texts):
            raise TranslationError("单条字幕文本过长或格式无效")
        if self.mode == "plugin":
            if self.bridge is None:
                raise TranslationError("尚未连接浏览器插件，无法使用插件当前字幕引擎")
            # Do not cache plugin output here: changing its selected engine must
            # take effect immediately. The plugin already has an engine-keyed cache.
            result = []
            for batch in self._batches(texts):
                result.extend(self.bridge.translate(batch, source, target, self.timeout))
            return result
        with self._lock:
            missing = list(dict.fromkeys(t for t in texts if t.strip() and
                                       (source, target, t) not in self._cache))
            for batch in self._batches(missing):
                self._wait_google_slot()
                translated = self._google(batch, source, target)
                for original, translation in zip(batch, translated):
                    self._cache[source, target, original] = translation
                    self._cache.move_to_end((source, target, original))
                while len(self._cache) > 4000:
                    self._cache.popitem(last=False)
            return [self._cache[source, target, t] if t.strip() else "" for t in texts]

    @staticmethod
    def _batches(texts):
        batch, size = [], 0
        for text in texts:
            if batch and (len(batch) >= 30 or size + len(text) > 3000):
                yield batch
                batch, size = [], 0
            batch.append(text)
            size += len(text)
        if batch:
            yield batch

    def _google(self, texts: list[str], source: str, target: str) -> list[str]:
        query = urlencode({"client": "gtx", "dt": "t", "format": "html", "sl": source, "tl": target})
        body = urlencode([("q", re.sub(r"\s+", " ", text).strip()) for text in texts]).encode("utf-8")
        try:
            with self._session.post("https://translate.googleapis.com/translate_a/t?" + query,
                                    data=body, timeout=self.timeout, stream=True,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
                if not response.ok:
                    raise TranslationError(f"Google 翻译接口返回 HTTP {response.status_code}，请稍后重试或切换插件引擎")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise TranslationError("Google 翻译接口返回的数据过大")
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
        except (requests.RequestException, TimeoutError, OSError):
            raise TranslationError("Google 翻译连接失败，请检查网络或切换插件引擎") from None
        except (ValueError, UnicodeError):
            raise TranslationError("Google 翻译接口返回的数据无法解析") from None
        if not isinstance(payload, list) or len(payload) != len(texts) or any(not isinstance(t, str) for t in payload):
            raise TranslationError("Google 翻译返回的字幕数量或格式不匹配")
        if any(original.strip() and not translated.strip() for original, translated in zip(texts, payload)):
            raise TranslationError("Google 翻译返回空译文")
        return [_decode_entities(t) for t in payload]
