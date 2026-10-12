"""Private worker; JSON stdin carries ephemeral media URLs, JSON stdout is safe.

Run with the existing SubtitleService environment so yt-dlp, requests and PyAV
are reused. This file has no Qt dependency and does not modify the service.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse


def emit(kind: str, **data):
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def allowed_url(url, platform):
    try:
        value = urlparse(url)
        host = (value.hostname or "").lower()
        domains = ("googlevideo.com",) if platform == "youtube" else ("bilivideo.com", "bilivideo.cn", "hdslb.com", "bilibili.com")
        return (value.scheme == "https" and not value.username and not value.password and value.port in (None, 443)
                and any(host == d or host.endswith("." + d) for d in domains))
    except (TypeError, ValueError):
        return False


def extractor_urls(video):
    from yt_dlp import YoutubeDL
    class Quiet:
        def debug(self, *_): pass
        def warning(self, *_): pass
        def error(self, *_): pass
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "logger": Quiet(), "cachedir": False,
                        "noplaylist": True, "skip_download": True, "socket_timeout": 15,
                        "retries": 1, "extractor_retries": 1}) as ydl:
            info = ydl.extract_info(video["url"], download=False)
        # Returning a different video is never accepted as a successful fallback.
        if video["platform"] == "youtube" and info.get("id") != video["video_id"]:
            raise RuntimeError("YouTube 返回的视频编号与当前任务不一致")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise RuntimeError("正在进行或尚未开始的网页直播请使用系统声音实时字幕模式")
        tracks = [f for f in info.get("formats", []) if f.get("vcodec") == "none"
                  and str(f.get("protocol", "https")) in {"http", "https"}
                  and allowed_url(f.get("url", ""), video["platform"])]
        tracks.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
        return [f["url"] for f in tracks]
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        message = str(exc).lower()
        if any(x in message for x in ("sign in", "login", "private video", "members-only", "age-restricted")):
            raise RuntimeError("YouTube 音频需要登录或访问授权；请在已登录网页播放后重试，或使用系统声音实时模式") from None
        if any(x in message for x in ("signature", "challenge", "nsig", "javascript runtime")):
            raise RuntimeError("YouTube 音频签名解析失败；请更新 SubtitleService 的 yt-dlp，或使用系统声音实时模式") from None
        raise RuntimeError("视频音频解析失败，可能是网络、站点限制或解析器版本；可使用系统声音实时模式") from None


def verify_audio(path: Path, expected: float):
    import av
    try:
        with av.open(str(path)) as container:
            audio_streams = list(container.streams.audio)
            if not audio_streams:
                return False
            duration = container.duration / av.time_base if container.duration else 0
            # A first range/chunk must never masquerade as a complete long video.
            if expected > 5 and duration and duration < expected * .8:
                return False
            for frame in container.decode(audio=0):
                if frame.samples:
                    return True
    except Exception:
        return False
    return False


def download_url(url, output, video, number):
    import requests
    if not allowed_url(url, video["platform"]):
        return None
    target = output / f"audio-{number}.media"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
               "Referer": "https://www.bilibili.com/" if video["platform"] == "bilibili" else "https://www.youtube.com/"}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(12, 25), allow_redirects=False) as response:
            # Only full, allowlisted responses are used. No cookie or redirects.
            if response.status_code != 200 or response.headers.get("Content-Range"):
                return None
            length = int(response.headers.get("Content-Length", "0"))
            if length > 2_000_000_000 or "text/html" in response.headers.get("Content-Type", ""):
                return None
            size, last_progress = 0, 0
            with target.open("xb") as stream:
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > 2_000_000_000:
                        raise RuntimeError("音频文件超过 2 GB")
                    stream.write(chunk)
                    if time.monotonic() - last_progress > .7:
                        amount = f"{size / 1048576:.1f} MB"
                        if length:
                            amount += f" / {length / 1048576:.1f} MB"
                        emit("progress", message="正在下载音频：" + amount)
                        last_progress = time.monotonic()
        if size < 1024 or (length and size != length):
            return None
        if verify_audio(target, float(video.get("duration", 0))):
            return target
    except Exception:
        # Requests/FFmpeg errors may contain signed URLs. Never echo them.
        return None
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    service = Path(args.service_root).resolve()
    sys.path.insert(0, str(service))
    output = Path(args.output).resolve()
    if not output.is_dir() or not re.fullmatch(r"media-[a-f0-9]{32}", output.name):
        raise RuntimeError("下载目录无效")
    video = json.loads(sys.stdin.readline(393216))
    platform = video.get("platform")
    if platform not in {"bilibili", "youtube"}:
        raise RuntimeError("只支持 B 站和 YouTube")
    candidates = list(video.get("audio_urls", []))
    client = None
    if platform == "bilibili":
        from service.bilibili import Bilibili
        client = Bilibili()  # No user cookies exported from the browser.
        if not video.get("cid"):
            video.update(client.describe(video["bvid"], int(video["page"])))
        candidates = client.audio_urls(video, candidates)
    seen, attempt = set(), 0
    def attempt_urls(urls):
        nonlocal attempt
        for url in urls:
            if not isinstance(url, str) or url in seen:
                continue
            seen.add(url)
            attempt += 1
            result = download_url(url, output, video, attempt)
            if result:
                return result
        return None
    try:
        result = attempt_urls(candidates)
        if not result:
            emit("progress", message="网页音轨不可用，正在尝试视频解析器")
            extra = client.extractor_audio(video) if client else extractor_urls(video)
            result = attempt_urls(extra)
        if not result:
            raise RuntimeError("当前视频音频无法下载，可能已过期、需要登录或受站点限制；请在原网页播放后重试，或使用系统声音实时模式")
        emit("progress", message="音频已下载并校验，正在准备语音识别")
        emit("result", path=str(result))
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Only our fixed user-facing RuntimeErrors are surfaced. Other exceptions
        # can contain URLs or a server response and must remain private.
        emit("error", message=str(exc) if isinstance(exc, RuntimeError) else "网页音频获取失败，请检查网络和 SubtitleService 环境")
        raise SystemExit(1)
