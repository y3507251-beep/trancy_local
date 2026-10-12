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
import threading
import time
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QObject, pyqtSignal

from .translation import TranslationError


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
             "paused": bool(raw.get("paused", True)), "rate": finite("rate", 1, .1, 16),
             "active": bool(raw.get("active", False)), "ad_playing": bool(raw.get("ad_playing", False)),
             "is_live": bool(raw.get("is_live", False)),
             "audio_urls": audio}
    if platform == "bilibili":
        video["bvid"] = video_id
    return video


class BrowserBridge(QObject):
    video_changed = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.port = 18766
        self._server = None
        self._lock = threading.RLock()
        self._clients = {}
        self._videos = {}
        self._sequences = {}
        self._jobs = {}
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
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            self._server.daemon_threads = True
            self.port = self._server.server_address[1]
            threading.Thread(target=self._server.serve_forever, name="browser-bridge", daemon=True).start()
            self.status_changed.emit(f"浏览器桥已启动：127.0.0.1:{self.port}，等待插件连接")
        except OSError:
            self._server = None
            self.status_changed.emit(f"浏览器桥端口 {port} 已被占用，请关闭另一个桌面字幕实例")
            raise

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        with self._lock:
            for job in self._jobs.values():
                job["error"] = "浏览器桥已关闭"
                job["done"].set()
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
                return 200, {"jobs": pending}
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
                    if old:
                        self.video_changed.emit({"key": old["key"], "tab_id": tab_id, "source_id": client_id,
                                                 "bridge_key": bridge_key, "closed": True})
                else:
                    video = normalize_video(body["video"])
                    video.update({"tab_id": tab_id, "source_id": client_id, "bridge_key": bridge_key,
                                  "updated_at": time.time(), "_seen": now})
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
                self.video_changed.emit({"key": video["key"], "tab_id": video["tab_id"],
                                         "source_id": video["source_id"], "bridge_key": key, "closed": True})
            return [{k: list(v) if k == "audio_urls" else v for k, v in video.items() if k != "_seen"}
                    for video in sorted(self._videos.values(), key=lambda v: (v["active"], not v["paused"], v["_seen"]), reverse=True)]

    def latest(self) -> dict | None:
        videos = self.videos()
        return videos[0] if videos else None

    def translate(self, texts: list[str], source: str, target: str, timeout: float = 30) -> list[str]:
        if not texts:
            return []
        if len(texts) > 20 or sum(len(t) for t in texts) > 30000:
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
