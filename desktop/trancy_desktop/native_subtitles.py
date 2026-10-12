"""Read existing text subtitles without loading a speech-recognition model.

Sidecar text is parsed directly. Embedded tracks are extracted by an owned
FFmpeg process when available, or by a lightweight PyAV process in the existing
SubtitleService environment. The Qt reader does this work outside the GUI.
Neither path writes to or modifies the video/subtitle originals.
"""
from __future__ import annotations

from bisect import bisect_left
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

DEFAULT_SERVICE_ROOT = r"D:\dev\SubtitleService"
MAX_SUBTITLE_BYTES = 32 * 1024 * 1024
MAX_CUES = 100_000
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
_TIME = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING = re.compile(rf"^\s*({_TIME})\s*-->\s*({_TIME})(?:\s+.*)?$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class SubtitleCancelled(Exception):
    pass


def language_code(value):
    value = str(value or "").strip().lower().replace("_", "-")
    if value in {"en", "eng", "english"} or value.startswith("en-"):
        return "en"
    if value in {"zh", "zho", "chi", "chs", "cht", "chinese", "cn", "中文"} or value.startswith("zh-"):
        return "zh"
    return ""


def text_language(text):
    """Chinese/English hint, also handling Chinese sentences with Latin terms."""
    chinese = len(_CJK.findall(str(text)))
    latin = len(re.findall(r"[A-Za-z]", str(text)))
    return "zh" if chinese and (chinese >= 2 or chinese >= latin / 4) else "en" if latin else ""


def _timestamp(value):
    parts = str(value).replace(",", ".").split(":")
    if len(parts) not in (2, 3):
        raise ValueError("字幕时间格式无效")
    seconds, minutes = float(parts[-1]), int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    if not math.isfinite(seconds) or hours < 0 or minutes < 0 or not 0 <= seconds < 60:
        raise ValueError("字幕时间格式无效")
    return hours * 3600 + minutes * 60 + seconds


def _clean_text(text, *, ass=False):
    if ass:
        # ASS vector drawings are not readable captions. Remove drawing runs
        # before removing normal style/karaoke overrides.
        text = re.sub(r"\{[^}]*\\p[1-9]\d*[^}]*\}.*?(?=\{[^}]*\\p0[^}]*\}|$)", "", text, flags=re.S)
        text = re.sub(r"\{[^}]*\}", "", text)
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    text = html.unescape(re.sub(r"<[^>]*>", "", str(text)))
    return "\n".join(re.sub(r"[\t ]+", " ", line).strip() for line in text.replace("\x00", "").splitlines()).strip()


def _cue(start, end, text):
    if text and math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
        return dict(start=round(start, 4), end=round(end, 4), text=text, words=[], timing_source="cue")
    return None


def parse_subtitle_text(text, extension="srt", *, check_cancel=lambda: None):
    """Parse SRT, WebVTT or ASS/SSA text into sentence-level seconds."""
    result = []
    extension = str(extension).lower().lstrip(".")
    if extension in {"ass", "ssa"}:
        fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        events = False
        for line_number, line in enumerate(text.splitlines()):
            if line_number % 256 == 0:
                check_cancel()
            line = line.strip()
            if line.startswith("["):
                events = line.lower() == "[events]"
            elif events and line.lower().startswith("format:"):
                fields = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
            elif events and line.lower().startswith("dialogue:"):
                parts = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
                values = dict(zip(fields, parts))
                try:
                    item = _cue(_timestamp(values["start"]), _timestamp(values["end"]),
                                _clean_text(values["text"], ass=True))
                except (KeyError, TypeError, ValueError):
                    continue
                if item:
                    result.append(item)
                    if len(result) > MAX_CUES:
                        raise ValueError("字幕条目过多，文件已保留")
    else:
        for block_number, block in enumerate(re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))):
            if block_number % 256 == 0:
                check_cancel()
            lines = block.strip().splitlines()
            if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
                continue
            for index, line in enumerate(lines[:3]):
                timing = _TIMING.match(line)
                if timing:
                    try:
                        item = _cue(_timestamp(timing[1]), _timestamp(timing[2]),
                                    _clean_text("\n".join(lines[index + 1:])))
                    except (TypeError, ValueError):
                        break
                    if item:
                        result.append(item)
                        if len(result) > MAX_CUES:
                            raise ValueError("字幕条目过多，文件已保留")
                    break
    if len(result) > MAX_CUES:
        raise ValueError("字幕条目过多，文件已保留")
    return sorted(result, key=lambda item: (item["start"], item["end"]))


def decode_subtitle_bytes(data):
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("字幕文件超过 32 MB，文件已保留")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data[:256]:
        encoding = "utf-16-le" if data[1:256:2].count(0) >= data[:256:2].count(0) else "utf-16-be"
        return data.decode(encoding)
    for encoding in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别字幕文本编码")


def sidecar_paths(source):
    source = Path(source)
    stem = source.stem.casefold()
    paths = []
    for path in source.parent.iterdir():
        candidate = path.stem.casefold()
        related = candidate == stem or any(candidate.startswith(stem + sep) for sep in (".", "_", "-"))
        label = candidate[len(stem):].strip("._-") if related else ""
        annotation = bool(_sidecar_language(path, source)) or label in {"bilingual", "双语", "default", "original", "字幕"}
        if (path.is_file() and path.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa"} and related and
                (candidate == stem or annotation)):
            paths.append(path)
    return sorted(paths, key=lambda path: (path.stem.casefold() != stem, path.name.casefold()))


def _sidecar_language(path, source):
    label = Path(path).stem[len(Path(source).stem):].strip("._-")
    return language_code(label) or next((language_code(part) for part in re.split(r"[._-]", label)
                                        if language_code(part)), "")


def _bilingual(cue, preferred=""):
    grouped = {"en": [], "zh": []}
    for line in cue["text"].splitlines():
        language = text_language(line)
        if language:
            grouped[language].append(line)
    if grouped["en"] and grouped["zh"]:
        source = preferred if preferred in grouped else "en"
        target = "zh" if source == "en" else "en"
        return dict(cue, text="\n".join(grouped[source]), translation="\n".join(grouped[target]),
                    source_language=source, target_language=target)
    source = text_language(cue["text"]) or preferred or "en"
    return dict(cue, source_language=source, target_language="zh" if source == "en" else "en")


def _track_language(track):
    declared = language_code(track.get("language"))
    text = "\n".join(cue["text"] for cue in track["cues"][:200])
    return declared or text_language(text)


def _merge_tracks(tracks, language="auto"):
    tracks = [track for track in tracks if track.get("cues")]
    if not tracks:
        return [], "", []
    preferred = language_code(language)
    for track in tracks:
        track["language"] = _track_language(track)
    # Explicit recognition language wins. Otherwise prefer the player's
    # default full-dialogue track; forced/sign-only tracks are a last resort.
    tracks.sort(key=lambda track: (bool(preferred) and track["language"] != preferred,
                                   bool(track.get("forced")), not track.get("default", False),
                                   track["language"] not in {"en", "zh"}))
    original = tracks[0]
    primary_language = preferred or original["language"] or "en"
    cues = [_bilingual(cue, primary_language) for cue in original["cues"]]
    candidates = {}
    for track in tracks[1:]:
        code = track["language"]
        if code in {"en", "zh"} and code not in candidates:
            candidates[code] = track["cues"]
    indexes = {code: [cue["start"] for cue in items] for code, items in candidates.items()}
    max_lengths = {code: max((cue["end"] - cue["start"] for cue in items), default=0)
                   for code, items in candidates.items()}
    for cue in cues:
        if cue.get("translation"):
            continue
        target = cue["target_language"]
        items = candidates.get(target, [])
        if not items:
            continue
        # Match overlap, rather than cue index: different subtitle tracks can
        # split one spoken sentence into different numbers of caption lines.
        begin = bisect_left(indexes[target], max(0, cue["start"] - max_lengths[target]))
        texts = []
        for index in range(begin, len(items)):
            alternate = items[index]
            if alternate["start"] >= cue["end"]:
                break
            overlap = min(cue["end"], alternate["end"]) - max(cue["start"], alternate["start"])
            threshold = min(.2, (cue["end"] - cue["start"]) / 4, (alternate["end"] - alternate["start"]) / 4)
            if overlap >= threshold:
                translated = _bilingual(alternate, target)["text"]
                if translated not in texts:
                    texts.append(translated)
        if texts:
            cue["translation"] = "\n".join(texts)
    metadata = [{key: track[key] for key in ("id", "language", "origin", "codec", "default", "forced")
                 if key in track} for track in tracks]
    return cues, primary_language, metadata


def _find_tool(name, service_root):
    filename = name + (".exe" if os.name == "nt" else "")
    for directory in (Path(service_root) / "bin", Path(service_root) / "ffmpeg" / "bin",
                      Path(service_root) / "tools" / "ffmpeg" / "bin", Path(r"D:\dev\ffmpeg\bin")):
        target = directory / filename
        if target.is_file():
            return str(target)
    return shutil.which(name)


def _run_owned(command, check_cancel, *, maximum=MAX_SUBTITLE_BYTES, timeout=90):
    """Bound output and terminate only the specific process we started."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               creationflags=flags)
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()

    def consume(pipe, destination, limit):
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                if len(destination) + len(chunk) > limit:
                    overflow.set()
                    return
                destination.extend(chunk)
        finally:
            pipe.close()

    readers = [threading.Thread(target=consume, args=(process.stdout, output, maximum)),
               threading.Thread(target=consume, args=(process.stderr, errors, 1024 * 1024))]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    try:
        while process.poll() is None:
            check_cancel()
            if overflow.is_set():
                raise ValueError("字幕读取输出超过限制")
            if time.monotonic() - started > timeout:
                raise TimeoutError("读取内嵌字幕超时，请使用外挂字幕或当前播放片段识别")
            time.sleep(.03)
        for reader in readers:
            reader.join(timeout=5)
        check_cancel()
        if overflow.is_set():
            raise ValueError("字幕读取输出超过限制")
        if process.returncode:
            # Do not expose an entire native diagnostic or opaque media path.
            raise ValueError("文件中的文本字幕无法读取")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)


def _embedded_tracks(source, service_root, check_cancel):
    ffprobe, ffmpeg = _find_tool("ffprobe", service_root), _find_tool("ffmpeg", service_root)
    if ffprobe and ffmpeg:
        info = json.loads(_run_owned([ffprobe, "-v", "error", "-select_streams", "s", "-show_streams", "-show_format",
                                      "-of", "json", str(source)], check_cancel, maximum=2 * 1024 * 1024, timeout=30))
        tracks = []
        for stream in info.get("streams", []):
            check_cancel()
            if stream.get("codec_name") not in TEXT_CODECS:
                continue
            text = _run_owned([ffmpeg, "-v", "error", "-threads", "1", "-i", str(source), "-map",
                               f"0:{int(stream['index'])}", "-f", "srt", "-"], check_cancel)
            tracks.append(dict(id=f"embedded:{stream['index']}", origin="embedded", codec=stream["codec_name"],
                               language=stream.get("tags", {}).get("language", ""),
                               default=bool(stream.get("disposition", {}).get("default")),
                               forced=bool(stream.get("disposition", {}).get("forced")),
                               cues=parse_subtitle_text(decode_subtitle_bytes(text), check_cancel=check_cancel)))
        try:
            duration = float(info.get("format", {}).get("duration", 0))
        except (TypeError, ValueError):
            duration = 0
        return dict(tracks=tracks, source_duration=duration if math.isfinite(duration) and duration > 0 else 0)
    python = Path(service_root) / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return dict(tracks=[], source_duration=0)
    data = _run_owned([str(python), "-B", "-u", str(Path(__file__).resolve()), "--embedded-worker", str(source)],
                      check_cancel)
    return json.loads(data)


def read_native_subtitles(source, *, service_root=DEFAULT_SERVICE_ROOT, language="auto", check_cancel=lambda: None):
    """Return ``{cues, source_language, origin, tracks}``; empty cues mean absent.

    This function is synchronous for tests/CLI use. GUI callers must use
    ``NativeSubtitleReader``. All source files are read-only and no files are
    generated, so cancellation has no temporary-directory cleanup to perform.
    """
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError("播放器来源必须是本地文件")
    tracks, warnings, source_duration = [], [], 0
    for path in sidecar_paths(source):
        check_cancel()
        try:
            if path.stat().st_size > MAX_SUBTITLE_BYTES:
                raise ValueError("字幕文件超过 32 MB")
            with path.open("rb") as stream:
                cues = parse_subtitle_text(decode_subtitle_bytes(stream.read(MAX_SUBTITLE_BYTES + 1)), path.suffix,
                                           check_cancel=check_cancel)
            if cues:
                tracks.append(dict(id=path.name, origin="sidecar", codec=path.suffix.lstrip("."),
                                   default=path.stem.casefold() == source.stem.casefold(),
                                   forced="forced" in re.split(r"[._-]", path.stem.casefold()),
                                   language=_sidecar_language(path, source), cues=cues))
        except (OSError, ValueError, UnicodeDecodeError):
            warnings.append(f"无法读取外挂字幕：{path.name}")
    if not tracks:
        check_cancel()
        embedded = _embedded_tracks(source, service_root, check_cancel)
        tracks = embedded.get("tracks", [])
        source_duration = embedded.get("source_duration", 0)
    check_cancel()
    cues, detected, metadata = _merge_tracks(tracks, language)
    return dict(cues=cues, source_language=detected, origin=metadata[0].get("origin", "") if metadata else "",
                status="ready" if cues else "unavailable", source_duration=source_duration, duration=source_duration,
                tracks=metadata, warnings=warnings)


def _read_av_tracks(source):
    """Worker-only lightweight import; no Torch, Whisper or ASR classes."""
    import av
    with av.open(str(source), mode="r") as container:
        origin = (container.start_time or 0) / av.time_base
        streams = [stream for stream in container.streams.subtitles if stream.codec_context.name in TEXT_CODECS]
        tracks = {stream.index: dict(id=f"embedded:{stream.index}", origin="embedded", codec=stream.codec_context.name,
                                      language=stream.metadata.get("language", ""),
                                      default=bool(int(stream.disposition) & 1),
                                      forced=bool(int(stream.disposition) & 64), cues=[]) for stream in streams}
        duration = (container.duration or 0) / av.time_base
        if not streams:
            return dict(tracks=[], source_duration=duration)
        count, size = 0, 0
        for packet in container.demux(*streams):
            if not packet.size:
                continue
            subtitle = packet.stream.decode2(packet)
            if subtitle is None:
                continue
            base = subtitle.pts / av.time_base if subtitle.pts is not None else (
                float(packet.pts * packet.time_base) if packet.pts is not None else None)
            if base is None:
                continue
            start = base + subtitle.start_display_time / 1000 - origin
            end = base + subtitle.end_display_time / 1000 - origin
            if end <= start and packet.duration and packet.time_base:
                end = start + float(packet.duration * packet.time_base)
            texts = []
            for rectangle in subtitle:
                raw = getattr(rectangle, "dialogue", b"") or getattr(rectangle, "text", b"")
                if not raw:
                    raw = getattr(rectangle, "ass", b"")
                    if raw.count(b",") >= 8:
                        raw = raw.split(b",", 8)[-1]
                if raw:
                    texts.append(_clean_text(raw.decode("utf-8", errors="replace"), ass=True))
            item = _cue(max(0, start), end, "\n".join(texts).strip())
            if item:
                tracks[packet.stream.index]["cues"].append(item)
                count += 1
                size += len(item["text"].encode("utf-8"))
                if count > MAX_CUES or size > MAX_SUBTITLE_BYTES // 2:
                    raise ValueError("内嵌字幕超过安全读取范围")
        for track in tracks.values():
            track["cues"].sort(key=lambda cue: (cue["start"], cue["end"]))
        return dict(tracks=list(tracks.values()), source_duration=duration)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 3 or sys.argv[1] != "--embedded-worker":
        sys.exit(2)
    try:
        print(json.dumps(_read_av_tracks(Path(sys.argv[2]).resolve(strict=True)), ensure_ascii=False), flush=True)
    except Exception:
        # Parent turns native failures into a concise diagnostic and can offer
        # bounded ASR. No shared service or model is modified by this worker.
        sys.exit(1)
else:
    from PyQt6.QtCore import QThread, pyqtSignal

    class NativeSubtitleReader(QThread):
        updated = pyqtSignal(dict)

        def __init__(self, source, job_id, *, service_root=DEFAULT_SERVICE_ROOT, language="auto", parent=None):
            super().__init__(parent)
            self.source, self.job_id = source, job_id
            self.service_root, self.language = service_root, language
            self._cancelled = threading.Event()

        def cancel(self):
            self._cancelled.set()

        def _check(self):
            if self._cancelled.is_set():
                raise SubtitleCancelled()

        def run(self):
            try:
                self.updated.emit(dict(event="state", job_id=self.job_id, status="reading_subtitles",
                                       message="正在读取播放器现成字幕"))
                result = read_native_subtitles(self.source, service_root=self.service_root,
                                               language=self.language, check_cancel=self._check)
                self._check()
                self.updated.emit(dict(event="result", job_id=self.job_id, **result))
            except SubtitleCancelled:
                self.updated.emit(dict(event="cancelled", job_id=self.job_id, message="已停止字幕读取"))
            except Exception as exc:
                self.updated.emit(dict(event="error", job_id=self.job_id, message=str(exc)))
