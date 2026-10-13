"""In-memory, loopback-only bridge for the bundled Chrome extension.

There is no fetch proxy, file/command endpoint, or remotely triggered ASR here.
Signed media URLs are accepted only for the current allowlisted video and never
written to disk or HTTP access logs. Translation secrets stay in the extension.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QObject, pyqtSignal

from .translation import TranslationError


CAPTION_ERROR_CODES = frozenset({
    "bili_metadata_failed", "bili_track_failed", "youtube_metadata_failed", "youtube_track_failed",
    "observer_unavailable", "video_changed", "invalid_caption_payload", "bridge_upload_failed",
})
DUBBING_STATUSES = frozenset({"active", "restored", "user_override", "expired", "video_changed", "error"})
DUBBING_REASONS = frozenset({"", "disabled", "bridge_disconnected", "lease_expired", "video_changed",
                           "ad_playing", "retired_lease", "replaced", "user_volume", "page_closed",
                           "observer_unavailable", "request_timeout"})
PLAYBACK_EVENTS = frozenset({"play", "pause", "ended", "seeking", "seeked", "waiting", "stalled",
                           "playing", "ratechange", "loadedmetadata", "emptied"})


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR, which on Windows lets a second app
    # silently bind the same port and receive only some connector requests.
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def extension_id() -> str:
    manifest_path = Path(__file__).resolve().parents[1] / "browser-extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    return "".join(chr(97 + int(c, 16)) for c in digest)


def media_url_allowed(url: str, platform: str, cid: str = "") -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        domains = ("googlevideo.com",) if platform == "youtube" else ("bilivideo.com", "bilivideo.cn", "hdslb.com", "bilibili.com")
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            return False
        return platform == "youtube" or bool(cid and re.search(r"(?<!\d)" + re.escape(cid) + r"(?!\d)", parsed.path))
    except (TypeError, ValueError):
        return False


def normalize_video(raw: dict) -> dict:
    """Validate video identity against a canonical public watch URL."""
    if not isinstance(raw, dict):
        raise ValueError("无效的视频信息")
    parsed = urlparse(str(raw.get("url", "")))
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("不支持的视频页面")
    host, path = (parsed.hostname or "").lower(), parsed.path
    query = parse_qs(parsed.query)
    if host in {"www.youtube.com", "youtube.com", "m.youtube.com", "www.youtube-nocookie.com"}:
        video_id = (query.get("v") or [""])[0] if path == "/watch" else path.strip("/").split("/")[-1]
        if path != "/watch" and not re.fullmatch(r"/(?:embed|live|shorts)/[\w-]{11}/?", path):
            raise ValueError("不支持的 YouTube 页面")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("无效的 YouTube 视频编号")
        platform, cid, page = "youtube", "", 1
        canonical = f"https://www.youtube.com/watch?v={video_id}"
        key = f"youtube:{video_id}"
    elif host == "bilibili.com" or host.endswith(".bilibili.com"):
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", path)
        if not match:
            raise ValueError("目前只支持 B 站 BV 视频页面")
        video_id = match[1]
        page = int((query.get("p") or ["1"])[0])
        if not 1 <= page <= 10000:
            raise ValueError("无效的 B 站分 P")
        cid = str(raw.get("cid", ""))
        if cid and not re.fullmatch(r"[1-9]\d{0,19}", cid):
            raise ValueError("无效的 B 站 CID")
        platform = "bilibili"
        canonical = f"https://www.bilibili.com/video/{video_id}/?p={page}"
        key = f"bilibili:{video_id}:{page}:{cid}"
    else:
        raise ValueError("当前只支持 B 站和 YouTube")
    given_id = str(raw.get("video_id", raw.get("id", video_id)))
    if given_id != video_id or raw.get("platform", platform) != platform:
        raise ValueError("视频编号与当前页面不一致")
    if platform == "bilibili" and int(raw.get("page", page)) != page:
        raise ValueError("分 P 与当前页面不一致")
    def finite(name, default, minimum, maximum):
        value = float(raw.get(name, default))
        return max(minimum, min(maximum, value)) if math.isfinite(value) else default
    audio = raw.get("audio_urls", raw.get("audioUrls", []))
    audio = audio if isinstance(audio, list) else []
    audio = list(dict.fromkeys(u for u in audio[:24] if isinstance(u, str) and len(u) <= 16384 and
                               media_url_allowed(u, platform, cid)))
    video = {"key": key, "platform": platform, "video_id": video_id, "id": video_id,
             "url": canonical, "page": page, "cid": cid, "title": str(raw.get("title", ""))[:600],
             "position": finite("position", 0, 0, 604800), "duration": finite("duration", 0, 0, 604800),
             "paused": bool(raw.get("paused", True)), "ended": raw.get("ended") is True,
             "rate": finite("rate", 1, .1, 16),
             "active": bool(raw.get("active", False)), "ad_playing": bool(raw.get("ad_playing", False)),
             "is_live": bool(raw.get("is_live", False)),
             "seeking": bool(raw.get("seeking", False)), "buffering": bool(raw.get("buffering", False)),
             "seek_serial": int(finite("seek_serial", 0, 0, 2147483647)),
             "dubbing_protocol": 1 if type(raw.get("dubbing_protocol")) is int and raw["dubbing_protocol"] == 1 else 0,
             "playback_event": raw.get("playback_event") if isinstance(raw.get("playback_event"), str) and raw["playback_event"] in PLAYBACK_EVENTS else "",
             "document_start": finite("document_start", 0, 0, 1e16),
             "audio_urls": audio}
    if platform == "bilibili":
        video["bvid"] = video_id
    return video


def normalize_caption_cues(raw: list, language: str = "") -> list[dict]:
    """Keep only text/timing metadata; website URLs and credentials never enter cues."""
    if not isinstance(raw, list) or not raw or len(raw) > 20000:
        raise ValueError("invalid website captions")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid website cue")
        start, end = float(item["start"]), float(item["end"])
        text = item.get("text")
        if (not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= 604800 or
                not isinstance(text, str) or not text.strip() or len(text) > 16000):
            raise ValueError("invalid website cue")
        cue = {"start": start, "end": end, "text": text.strip(),
               "source_language": str(item.get("source_language", language))[:32],
               "subtitle_kind": "asr" if item.get("subtitle_kind") == "asr" else "human"}
        translation = item.get("translation", "")
        if not isinstance(translation, str) or len(translation) > 16000:
            raise ValueError("invalid website translation")
        if translation.strip():
            cue.update(translation=translation.strip(), target_language=str(item.get("target_language", ""))[:32],
                       translation_source="platform")
        words = item.get("words", [])
        if not isinstance(words, list) or len(words) > 1000:
            raise ValueError("invalid website word timings")
        valid_words = []
        for word in words:
            try:
                ws, we = float(word["start"]), float(word["end"])
                token = word["text"]
                if (isinstance(token, str) and token.strip() and len(token) <= 500 and
                        math.isfinite(ws) and math.isfinite(we) and start <= ws < we <= end):
                    valid_words.append({"text": token, "start": ws, "end": we})
            except (KeyError, TypeError, ValueError):
                continue
        if valid_words:
            cue["words"] = valid_words
            cue["timing_source"] = "youtube"
        else:
            cue["timing_source"] = "cue"
        result.append(cue)
    return sorted(result, key=lambda c: (c["start"], c["end"]))


class BrowserBridge(QObject):
    video_changed = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    captions_ready = pyqtSignal(dict)
    dubbing_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.port = 18766
        self._server = None
        self._lock = threading.RLock()
        self._clients = {}
        self._videos = {}
        self._sequences = {}
        self._jobs = {}
        self._caption_jobs = {}
        self._dubbing_jobs = {}
        self._dubbing_leases = {}
        self._extension_id = extension_id()

    def start(self, port: int = 18766):
        if self._server:
            return
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log request bodies, origins or signed media URLs.

            def _headers_valid(self, preflight=False):
                origin = self.headers.get("Origin", "")
                allowed = "chrome-extension://" + owner._extension_id
                host = self.headers.get("Host", "")
                if host != f"127.0.0.1:{owner.port}" or (origin and origin != allowed):
                    return False
                if preflight:
                    return origin == allowed
                return (self.headers.get("X-Trancy-Desktop") == "1" and
                        self.headers.get("X-Trancy-Extension") == owner._extension_id)

            def _respond(self, status, body):
                data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if self.headers.get("Origin") == "chrome-extension://" + owner._extension_id:
                    self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                    self.send_header("Vary", "Origin")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_OPTIONS(self):
                if not self._headers_valid(True):
                    self._respond(403, {"error": "origin rejected"})
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Trancy-Desktop, X-Trancy-Extension, X-Trancy-Session")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if not self._headers_valid():
                    # Windows can reset a connection closed with an unread
                    # request body before the client receives the intended 403.
                    # Drain only a bounded body; the same origin/session policy
                    # still rejects it before any endpoint is dispatched.
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if 0 < size <= 393216:
                            self.connection.settimeout(1)
                            self.rfile.read(size)
                    except (ValueError, OSError):
                        pass
                    self._respond(403, {"error": "extension origin rejected"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 393216 or self.headers.get_content_type() != "application/json":
                        raise ValueError()
                    self.connection.settimeout(5)
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError()
                    status, result = owner._dispatch(self.path, body, self.headers.get("X-Trancy-Session", ""))
                    self._respond(status, result)
                except (ValueError, TypeError, KeyError, OSError):
                    self._respond(400, {"error": "invalid request"})

            def do_GET(self):
                self._respond(405, {"error": "POST required"})
        try:
            self._server = ExclusiveHTTPServer(("127.0.0.1", port), Handler)
            self._server.daemon_threads = True
            self.port = self._server.server_address[1]
            threading.Thread(target=self._server.serve_forever, name="browser-bridge", daemon=True).start()
            self.status_changed.emit(f"浏览器桥已启动：127.0.0.1:{self.port}，等待插件连接")
        except OSError as error:
            self._server = None
            message = f"浏览器连接端口 {port} 无法启动，请检查是否还有另一个桌面字幕窗口。"
            self.status_changed.emit(message)
            raise OSError(message) from error

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        with self._lock:
            for job in self._jobs.values():
                job["error"] = "浏览器桥已关闭"
                job["done"].set()
            for job in self._caption_jobs.values():
                job["timer"].cancel()
            self._caption_jobs.clear()
            for job in self._dubbing_jobs.values():
                job["timer"].cancel()
            self._dubbing_jobs.clear()
            self._dubbing_leases.clear()  # Browser watchdog restores even after process exit.
            self._clients.clear()
            self._videos.clear()
            self._sequences.clear()

    def _dispatch(self, path: str, body: dict, token: str):
        client_id = body.get("client_id", "")
        if not isinstance(client_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", client_id):
            raise ValueError("invalid client")
        now = time.monotonic()
        with self._lock:
            if path == "/v1/hello":
                first = client_id not in self._clients
                if first:
                    self._clients[client_id] = {"token": secrets.token_urlsafe(32), "seen": now}
                client = self._clients[client_id]
                client["seen"] = now
                version = body.get("connector_version", "")
                client["version"] = version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown"
                if first:
                    self.status_changed.emit("浏览器插件已连接，可使用插件当前字幕翻译引擎")
                return 200, {"session": client["token"], "protocol": 1}
            client = self._clients.get(client_id)
            if not client or not secrets.compare_digest(client["token"], token):
                return 403, {"error": "session expired"}
            client["seen"] = now
            if path == "/v1/poll":
                pending = []
                for job_id, job in self._jobs.items():
                    if job["client"] == client_id and not job["sent"] and job["deadline"] > now:
                        job["sent"] = True
                        pending.append({"id": job_id, "texts": job["texts"], "source": job["source"], "target": job["target"]})
                        if len(pending) == 4:
                            break
                captions = []
                for job_id, job in self._caption_jobs.items():
                    if job["client"] == client_id and not job["sent"] and job["deadline"] > now:
                        job["sent"] = True
                        captions.append({"id": job_id, "tab_id": job["video"]["tab_id"],
                                         "key": job["video"]["key"], "video": job["video"],
                                         "document_start": job["video"].get("document_start", 0),
                                         "preferred_language": job["preferred_language"]})
                        if len(captions) == 4:
                            break
                dubbing = []
                for job_id, job in self._dubbing_jobs.items():
                    if job["client"] == client_id and not job["sent"] and job["deadline"] > now:
                        job["sent"] = True
                        dubbing.append({"id": job_id, "tab_id": job["video"]["tab_id"], "video": job["video"],
                                        "document_start": job["video"]["document_start"],
                                        "lease_id": job["lease_id"], "enabled": job["enabled"]})
                        if len(dubbing) == 4:
                            break
                return 200, {"jobs": pending, "caption_jobs": captions, "dubbing_jobs": dubbing}
            if path == "/v1/captions":
                return self._receive_captions(body, client_id, now)
            if path == "/v1/dubbing":
                return self._receive_dubbing(body, client_id, now)
            if path == "/v1/result":
                job = self._jobs.get(body.get("id", ""))
                if not job or job["client"] != client_id or job["deadline"] <= now:
                    return 200, {"accepted": False}
                output = body.get("translations")
                if (isinstance(output, list) and len(output) == len(job["texts"]) and
                        all(isinstance(t, str) and len(t) <= 60000 for t in output) and
                        all(t.strip() or not original.strip() for t, original in zip(output, job["texts"]))):
                    job["result"] = output
                else:
                    # Provider responses can echo keys/URLs. The wire error is
                    # deliberately not copied to application logs or settings.
                    job["error"] = "插件字幕翻译失败，请检查插件当前翻译引擎、登录状态及网络"
                job["done"].set()
                return 200, {"accepted": True}
            if path in {"/v1/video", "/v1/closed"}:
                tab_id, seq = int(body["tab_id"]), int(body["seq"])
                if tab_id < 0 or seq < 1:
                    raise ValueError()
                bridge_key = f"{client_id}:{tab_id}"
                if seq <= self._sequences.get(bridge_key, 0):
                    return 200, {"accepted": False}
                self._sequences[bridge_key] = seq
                if path.endswith("closed"):
                    old = self._videos.pop(bridge_key, None)
                    self._cancel_caption_tab(bridge_key)
                    self._cancel_dubbing_tab(bridge_key, "page_closed")
                    if old:
                        self.video_changed.emit({"key": old["key"], "tab_id": tab_id, "source_id": client_id,
                                                 "bridge_key": bridge_key, "closed": True})
                else:
                    video = normalize_video(body["video"])
                    video.update({"tab_id": tab_id, "source_id": client_id, "bridge_key": bridge_key,
                                  "updated_at": time.time(), "_seen": now, "connector_version": client["version"]})
                    old = self._videos.get(bridge_key)
                    if old and (old["key"] != video["key"] or old.get("document_start") != video.get("document_start")):
                        self._cancel_caption_tab(bridge_key)
                        self._cancel_dubbing_tab(bridge_key, "video_changed")
                    self._videos[bridge_key] = video
                    self.video_changed.emit({k: v for k, v in video.items() if k != "_seen"})
                return 200, {"accepted": True}
            return 404, {"error": "unknown endpoint"}

    def videos(self) -> list[dict]:
        with self._lock:
            now = time.monotonic()
            expired = [k for k, v in self._videos.items() if now - v["_seen"] > 8]
            for key in expired:
                video = self._videos.pop(key)
                self._cancel_caption_tab(key)
                self._cancel_dubbing_tab(key, "bridge_disconnected")
                self.video_changed.emit({"key": video["key"], "tab_id": video["tab_id"],
                                         "source_id": video["source_id"], "bridge_key": key, "closed": True})
            return [{k: list(v) if k == "audio_urls" else v for k, v in video.items() if k != "_seen"}
                    for video in sorted(self._videos.values(), key=lambda v: (v["active"], not v["paused"], v["_seen"]), reverse=True)]

    def latest(self) -> dict | None:
        videos = self.videos()
        return videos[0] if videos else None

    def set_dubbing(self, video: dict, enabled: bool, lease_id: str) -> str:
        """Temporarily mute one exact Bilibili video, without blocking Qt.

        Renew an enabled lease every second, using the same 32-hex lease ID.
        An ``active`` acknowledgement confirms the page has muted the video.
        Disable restores the previous mute state; page expiry after 3 seconds
        covers failed requests, disconnected bridges and crashed applications.
        Caller supplies local audio playback; this API never plays or seeks media.
        """
        if type(enabled) is not bool or not isinstance(lease_id, str) or not re.fullmatch(r"[a-f0-9]{32}", lease_id):
            raise ValueError("无效的英文配音租约")
        clean = normalize_video(video)
        if clean["platform"] != "bilibili" or not clean["cid"]:
            raise ValueError("英文配音第一期只支持已识别 CID 的 B 站视频")
        bridge_key = str(video.get("bridge_key", ""))
        with self._lock:
            current = self._videos.get(bridge_key)
            existing = self._dubbing_leases.get(lease_id)
            if (existing and (existing["video"]["key"] != clean["key"] or
                    existing["video"]["bridge_key"] != bridge_key or
                    existing["video"]["document_start"] != clean["document_start"] or
                    int(video.get("tab_id", -1)) != existing["video"]["tab_id"] or
                    video.get("source_id") != existing["video"]["source_id"])):
                raise ValueError("配音租约与所选网页不一致")
            if enabled:
                if (not self._server or not current or current["key"] != clean["key"] or
                        current["document_start"] != clean["document_start"] or
                        int(video.get("tab_id", -1)) != current["tab_id"] or
                        video.get("source_id") != current["source_id"]):
                    raise ValueError("网页视频已切换或浏览器连接已断开")
                if current.get("dubbing_protocol") != 1:
                    raise ValueError("请重新加载 0.1.4 及以上的桌面字幕连接器并刷新 B 站网页，再开启英文配音")
                if current.get("ad_playing") or current.get("is_live"):
                    raise ValueError("广告或直播期间暂不接管原视频声音")
                for other_id, other in list(self._dubbing_leases.items()):
                    if other_id != lease_id and other["enabled"]:
                        self._queue_dubbing(other["video"], False, other_id)
                identity = {k: current[k] for k in ("key", "platform", "video_id", "url", "page", "cid",
                                                   "tab_id", "source_id", "bridge_key", "document_start")}
            else:
                # An old lease can still be released after the tab disappeared
                # from the list, but a forged/stale identity cannot affect a new lease.
                identity = existing["video"] if existing else None
                if identity is None and current and current["key"] == clean["key"] and current["document_start"] == clean["document_start"]:
                    identity = {k: current[k] for k in ("key", "platform", "video_id", "url", "page", "cid",
                                                       "tab_id", "source_id", "bridge_key", "document_start")}
                if not self._server or identity is None:
                    raise ValueError("浏览器连接已断开；网页将在租约到期后恢复原声")
            return self._queue_dubbing(identity, enabled, lease_id)

    def _queue_dubbing(self, identity: dict, enabled: bool, lease_id: str) -> str:
        # A newer command supersedes older acknowledgements for this lease.
        for old_id, old in list(self._dubbing_jobs.items()):
            if old["lease_id"] == lease_id:
                old["timer"].cancel()
                self._dubbing_jobs.pop(old_id)
        request_id = secrets.token_hex(16)
        timer = threading.Timer(2.5, self._dubbing_timeout, args=(request_id,))
        timer.daemon = True
        self._dubbing_jobs[request_id] = {"client": identity["source_id"], "video": dict(identity),
            "lease_id": lease_id, "enabled": enabled, "sent": False, "timer": timer,
            "deadline": time.monotonic() + 2.5}
        previous = self._dubbing_leases.get(lease_id)
        request_ids = ((previous or {}).get("request_ids", []) + [request_id])[-16:]
        self._dubbing_leases[lease_id] = {"video": dict(identity), "enabled": enabled, "request_id": request_id,
                                         "request_ids": request_ids}
        timer.start()
        return request_id

    def _dubbing_timeout(self, request_id: str):
        with self._lock:
            job = self._dubbing_jobs.pop(request_id, None)
            if job:
                job["timer"].cancel()
                self.dubbing_changed.emit({**job["video"], "request_id": request_id, "lease_id": job["lease_id"],
                                          "status": "timeout", "enabled": job["enabled"], "reason": "request_timeout"})
                if not job["enabled"]:
                    self._dubbing_leases.pop(job["lease_id"], None)

    def _cancel_dubbing_tab(self, bridge_key: str, reason: str):
        for lease_id, lease in list(self._dubbing_leases.items()):
            if lease["video"]["bridge_key"] != bridge_key:
                continue
            self._dubbing_leases.pop(lease_id, None)
            for request_id, job in list(self._dubbing_jobs.items()):
                if job["lease_id"] == lease_id:
                    job["timer"].cancel()
                    self._dubbing_jobs.pop(request_id)
            self.dubbing_changed.emit({**lease["video"], "request_id": lease["request_id"], "lease_id": lease_id,
                                      "status": "video_changed", "enabled": False, "reason": reason})

    def _receive_dubbing(self, body: dict, client_id: str, now: float):
        lease_id, request_id = body.get("lease_id", ""), body.get("id", "")
        if not isinstance(lease_id, str) or not isinstance(request_id, str):
            return 200, {"accepted": False}
        lease = self._dubbing_leases.get(lease_id)
        if not lease or lease["video"]["source_id"] != client_id:
            return 200, {"accepted": False}
        job = self._dubbing_jobs.get(request_id)
        status = body.get("status")
        try:
            incoming = normalize_video(body.get("video", {}))
            identity = lease["video"]
            if (incoming["key"] != identity["key"] or int(body.get("tab_id", -1)) != identity["tab_id"] or
                    float(body.get("document_start", -1)) != identity["document_start"] or
                    not re.fullmatch(r"[a-f0-9]{32}", request_id) or request_id not in lease["request_ids"] or
                    status not in DUBBING_STATUSES):
                return 200, {"accepted": False}
            current = self._videos.get(identity["bridge_key"])
            if status == "active" and (not job or request_id != lease["request_id"] or not lease["enabled"] or
                    job["deadline"] <= now or not current or current["key"] != identity["key"] or
                    current["document_start"] != identity["document_start"]):
                return 200, {"accepted": False}
            # Errors are command acknowledgements; terminal page watchdog/user
            # reports may arrive after their successful active acknowledgement.
            if status == "error" and (not job or request_id != lease["request_id"] or job["deadline"] <= now):
                return 200, {"accepted": False}
            if job:
                self._dubbing_jobs.pop(request_id, None)
                job["timer"].cancel()
            reason = body.get("reason", "")
            reason = reason if isinstance(reason, str) and reason in DUBBING_REASONS else ""
            event = {**identity, "request_id": request_id, "lease_id": lease_id, "status": status,
                     "enabled": status == "active", "reason": reason}
            for name, default, low, high in (("position", 0, 0, 604800), ("rate", 1, .1, 16)):
                value = float(body.get(name, default))
                event[name] = max(low, min(high, value)) if math.isfinite(value) else default
            event.update(paused=bool(body.get("paused", True)), ended=body.get("ended") is True,
                         seeking=bool(body.get("seeking", False)),
                         buffering=bool(body.get("buffering", False)))
            serial = body.get("seek_serial", 0)
            event["seek_serial"] = min(2147483647, serial) if type(serial) is int and serial >= 0 else 0
            if status != "active":
                self._dubbing_leases.pop(lease_id, None)
                for pending_id, pending in list(self._dubbing_jobs.items()):
                    if pending["lease_id"] == lease_id:
                        pending["timer"].cancel()
                        self._dubbing_jobs.pop(pending_id)
            self.dubbing_changed.emit(event)
            return 200, {"accepted": True}
        except (KeyError, TypeError, ValueError, OverflowError):
            return 200, {"accepted": False}

    def request_captions(self, video: dict, generation=0, timeout: float = 12,
                         source_language: str = "auto") -> str:
        """Request a site's existing text track without blocking the Qt thread.

        ``captions_ready`` carries the caller's generation/request ID. A missing
        track is ``unavailable``; failed retrieval is ``error`` or ``timeout``.
        Calling cancel_captions suppresses all later results for that request.
        """
        clean = normalize_video(video)
        bridge_key = str(video.get("bridge_key", ""))
        with self._lock:
            current = self._videos.get(bridge_key)
            if (not self._server or not current or current["key"] != clean["key"] or
                    current.get("document_start", 0) != clean.get("document_start", 0)):
                raise ValueError("网页视频已切换或浏览器连接已断开，请重新选择视频")
            timeout = max(.1, min(float(timeout), 30))
            job_id = secrets.token_hex(16)
            # Send only video identity, never the ephemeral audio URL list.
            identity = {k: current[k] for k in ("key", "platform", "video_id", "url", "page", "cid",
                                               "tab_id", "source_id", "bridge_key", "document_start")}
            timer = threading.Timer(timeout, self._caption_timeout, args=(job_id,))
            timer.daemon = True
            self._caption_jobs[job_id] = {"client": current["source_id"], "video": identity,
                "generation": generation, "preferred_language": str(source_language)[:32], "sent": False,
                "deadline": time.monotonic() + timeout, "timer": timer, "parts": {}, "part_count": None,
                "bytes": 0, "language": "", "caption_source": ""}
            timer.start()
            return job_id

    def cancel_captions(self, request_id: str):
        with self._lock:
            job = self._caption_jobs.pop(request_id, None)
            if job:
                job["timer"].cancel()

    def _cancel_caption_tab(self, bridge_key: str):
        for job_id, job in list(self._caption_jobs.items()):
            if job["video"]["bridge_key"] == bridge_key:
                self.cancel_captions(job_id)

    def _finish_captions(self, job_id: str, status: str, cues=None):
        job = self._caption_jobs.pop(job_id, None)
        if not job:
            return
        job["timer"].cancel()
        event = {**job["video"], "request_id": job_id, "generation": job["generation"],
                 "status": status, "source_language": job["language"],
                 "caption_source": job["caption_source"], "cues": cues or []}
        if job.get("error_code") in CAPTION_ERROR_CODES:
            event["error_code"] = job["error_code"]
        if isinstance(job.get("native_track_available"), bool):
            event["native_track_available"] = job["native_track_available"]
        if status == "error":
            event["message"] = "读取网站已有字幕失败，请重试原字幕"
        elif status == "timeout":
            event["message"] = "等待网站原字幕超时，请重试原字幕"
        self.captions_ready.emit(event)

    def _caption_timeout(self, job_id: str):
        with self._lock:
            self._finish_captions(job_id, "timeout")

    def _receive_captions(self, body: dict, client_id: str, now: float):
        job_id = body.get("id", "")
        job = self._caption_jobs.get(job_id)
        if not job or job["client"] != client_id or job["deadline"] <= now:
            return 200, {"accepted": False}
        current = self._videos.get(job["video"]["bridge_key"])
        try:
            incoming = normalize_video(body.get("video", {}))
            if (not current or current["key"] != job["video"]["key"] or incoming["key"] != current["key"] or
                    int(body.get("tab_id", -1)) != current["tab_id"] or
                    float(body.get("document_start", -1)) != current.get("document_start", 0)):
                return 200, {"accepted": False}
            status = body.get("status")
            code = body.get("error_code")
            job["error_code"] = code if isinstance(code, str) and code in CAPTION_ERROR_CODES else ""
            if isinstance(body.get("native_track_available"), bool):
                job["native_track_available"] = body["native_track_available"]
            if status in {"error", "unavailable"}:
                if status == "unavailable" and job.get("native_track_available") is True:
                    status = "error"
                self._finish_captions(job_id, status)
                return 200, {"accepted": True}
            if status != "ready":
                raise ValueError("invalid caption status")
            part, count = int(body.get("part", 0)), int(body.get("part_count", 1))
            if not 0 <= part < count <= 256 or (job["part_count"] not in (None, count)):
                raise ValueError("invalid caption chunks")
            if part in job["parts"]:
                return 200, {"accepted": False}
            language = str(body.get("source_language", ""))[:32]
            if job["parts"] and language != job["language"]:
                raise ValueError("changed caption language")
            cues = normalize_caption_cues(body.get("cues"), language)
            job["bytes"] += len(json.dumps(cues, ensure_ascii=False).encode("utf-8"))
            if job["bytes"] > 8 * 1024 * 1024:
                raise ValueError("captions too large")
            job["part_count"], job["language"] = count, language
            source = str(body.get("caption_source", ""))
            job["caption_source"] = source if source in {"youtube-human", "youtube-asr", "bilibili-human", "bilibili-asr"} else current["platform"]
            job["parts"][part] = cues
            if len(job["parts"]) == count:
                output = [cue for index in range(count) for cue in job["parts"][index]]
                if len(output) > 20000:
                    raise ValueError("captions too long")
                self._finish_captions(job_id, "ready", sorted(output, key=lambda c: (c["start"], c["end"])))
            return 200, {"accepted": True}
        except (KeyError, TypeError, ValueError, OverflowError):
            job["error_code"] = "invalid_caption_payload"
            self._finish_captions(job_id, "error")
            return 200, {"accepted": True}

    def translate(self, texts: list[str], source: str, target: str, timeout: float = 30) -> list[str]:
        if not texts:
            return []
        if len(texts) > 30 or sum(len(t) for t in texts) > 30000:
            raise TranslationError("单次翻译字幕过多，请分批处理")
        with self._lock:
            now = time.monotonic()
            clients = [(k, v) for k, v in self._clients.items() if now - v["seen"] < 10]
            if not self._server or not clients:
                raise TranslationError("浏览器插件尚未连接；请重新加载本插件并打开 B 站或 YouTube 页面")
            client_id = max(clients, key=lambda item: item[1]["seen"])[0]
            job_id = secrets.token_hex(16)
            job = {"client": client_id, "texts": list(texts), "source": source, "target": target,
                   "sent": False, "deadline": now + timeout, "done": threading.Event(), "result": None, "error": ""}
            self._jobs[job_id] = job
        try:
            if not job["done"].wait(timeout):
                raise TranslationError("等待插件翻译超时，请检查插件是否运行以及所选翻译引擎的网络连接")
            if job["error"]:
                raise TranslationError(job["error"])
            return job["result"]
        finally:
            with self._lock:
                self._jobs.pop(job_id, None)
