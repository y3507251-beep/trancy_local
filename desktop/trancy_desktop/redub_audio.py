"""Offline re-dubbing cue planning and bounded-memory audio rendering.

Importing this module needs only the standard library. Audio/model packages are
loaded inside worker-only functions, never when opening the PyQt6 application.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from pathlib import Path
import re


SAMPLE_RATE = 44_100
VOICE_MODEL = "Qwen3-TTS-12Hz-1.7B-Base"
PIPELINE_VERSION = "single-speaker-demucs-qwen-tts-v2-min075"
MIN_VIDEO_FACTOR = 0.75
ERRORS = {
    "invalid_job": "配音准备任务无效，请重新选择 B 站视频",
    "missing_english": "仍有字幕缺少英文译文，请完成翻译后再准备配音",
    "missing_models": "本地配音模型未安装，请运行 desktop/tools/setup_redubbing.py",
    "no_cuda": "本地音色配音需要可用的 NVIDIA CUDA 显卡",
    "audio_decode": "无法读取视频音轨，请重新获取当前视频音频",
    "separation": "背景与人声分离失败，已保留已有缓存",
    "reference": "未找到足够清晰且字幕匹配的人声片段，请尝试其他单人讲解视频",
    "synthesis": "本地英文音色配音生成失败，已保留已完成的语音缓存",
    "memory": "显存不足，请关闭其他占用显卡的模型任务后重试",
    "render": "背景与配音混合失败，已保留已完成的缓存",
    "output": "配音缓存无法写入，请检查磁盘空间和文件夹权限",
}


class RedubError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "invalid_job"
        super().__init__(ERRORS[self.code])


def clean_text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).split())


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise RedubError("invalid_job") from None
    if not math.isfinite(value):
        raise RedubError("invalid_job")
    return value


def english_text(cue):
    source = str(cue.get("source_language", "")).lower()
    target = str(cue.get("target_language", "")).lower()
    original, translated = clean_text(cue.get("text")), clean_text(cue.get("translation"))
    if source.startswith("en"):
        result = original
    elif target.startswith("en") or translated:
        result = translated
    elif original and not re.search(r"[\u3400-\u9fff]", original):
        result = original
    else:
        result = ""
    if not re.search(r"[A-Za-z]", result) or re.search(r"[\u3400-\u9fff]", result):
        raise RedubError("missing_english")
    return result


def normalized_cues(cues, duration):
    if not isinstance(cues, list) or not cues or len(cues) > 50_000:
        raise RedubError("invalid_job")
    duration = _number(duration)
    result = []
    for cue in cues:
        if not isinstance(cue, dict):
            raise RedubError("invalid_job")
        start, end = max(0.0, _number(cue.get("start"))), _number(cue.get("end"))
        if end <= start:
            raise RedubError("invalid_job")
        if start >= duration:
            continue
        text = clean_text(cue.get("text"))
        if not text:
            continue
        target = english_text(cue)
        if len(text) > 10_000 or len(target) > 10_000:
            raise RedubError("invalid_job")
        result.append({"start": start, "end": min(duration, end), "text": text,
                       "english": target})
    result.sort(key=lambda item: (item["start"], item["end"]))
    if not result:
        raise RedubError("invalid_job")
    return result


def join_caption_text(first, second):
    """Collapse repeated rolling captions without dropping separate sentences."""
    if first == second:
        return first
    # Word overlap is safer than character overlap ("a" + "apple"). Chinese
    # rolling captions are compared by character only when both contain Han.
    if re.search(r"[\u3400-\u9fff]", first + second):
        if second in first:
            return first
        if first in second:
            return second
        for length in range(min(len(first), len(second)), 1, -1):
            if first[-length:] == second[:length]:
                return first + second[length:]
    else:
        left, right = first.split(), second.split()
        if right and any(left[i:i + len(right)] == right for i in range(len(left) - len(right) + 1)):
            return first
        if left and any(right[i:i + len(left)] == left for i in range(len(right) - len(left) + 1)):
            return second
        for length in range(min(len(left), len(right)), 0, -1):
            if left[-length:] == right[:length]:
                return " ".join(left + right[length:])
    return (first + " " + second).strip()


def merge_overlapping(cues):
    groups = []
    for cue in cues:
        if groups and cue["start"] < groups[-1]["end"] - 0.00001:
            current = groups[-1]
            current["end"] = max(current["end"], cue["end"])
            current["text"] = join_caption_text(current["text"], cue["text"])
            current["english"] = join_caption_text(current["english"], cue["english"])
        else:
            groups.append(dict(cue))
    return groups


def reference_candidates(cues):
    """Only whole subtitle groups are used, so reference text matches audio."""
    candidates = []
    for index, first in enumerate(cues):
        text, end = "", first["start"]
        for cue in cues[index:]:
            if cue["start"] - end > 0.6 or cue["end"] - first["start"] > 12.0:
                break
            text = join_caption_text(text, cue["text"]) if text else cue["text"]
            end = cue["end"]
            length = end - first["start"]
            if length >= 3.0:
                candidates.append({"start": first["start"], "end": end, "text": text,
                                   "preferred": 5.0 <= length <= 12.0})
            if length >= 8.0:
                break
    return candidates


def merge_short_phrases(cues):
    """Use nearby fragmented caption windows as a sentence-sized speech slot."""
    groups = []
    for cue in cues:
        previous = groups[-1] if groups else None
        if previous:
            duration = previous["end"] - previous["start"]
            crowded = duration < 2.0 or len(previous["english"].split()) > duration * 2.8
            adjacent = cue["start"] - previous["end"] <= .25
            bounded = cue["end"] - previous["start"] <= 12 and len(previous["english"] + cue["english"]) <= 420
            if crowded and adjacent and bounded:
                previous["end"] = cue["end"]
                previous["text"] += " " + cue["text"]
                previous["english"] += " " + cue["english"]
                continue
        groups.append(dict(cue))
    return groups


def split_utterance(text, max_characters=280):
    """Keep each generation bounded; never silently truncate a long sentence."""
    text = clean_text(text)
    parts = re.split(r"(?<=[.!?;])\s+", text)
    chunks, pending = [], ""
    for part in parts:
        words = part.split()
        for word in words:
            if pending and len(pending) + len(word) + 1 > max_characters:
                chunks.append(pending)
                pending = ""
            if len(word) > max_characters:
                if pending:
                    chunks.append(pending)
                    pending = ""
                chunks.extend(word[i:i + max_characters] for i in range(0, len(word), max_characters))
            else:
                pending = (pending + " " + word).strip()
        if pending and pending.endswith((".", "!", "?", ";")):
            chunks.append(pending)
            pending = ""
    if pending:
        chunks.append(pending)
    return chunks


def build_segments(groups, speech_durations, source_duration, sample_rate=SAMPLE_RATE):
    """Generate sample-exact continuous source/audio timelines, without cuts."""
    total = round(_number(source_duration) * sample_rate)
    if total <= 0 or len(groups) != len(speech_durations):
        raise RedubError("invalid_job")
    segments, source_frame, output_frame = [], 0, 0

    def add(end, length, kind, cue_index=None, speech_duration=None, padding=0):
        nonlocal source_frame, output_frame
        item = {"source_start": source_frame / sample_rate, "source_end": end / sample_rate,
                "audio_start": output_frame / sample_rate, "audio_end": (output_frame + length) / sample_rate,
                "kind": kind}
        if cue_index is not None:
            item["cue_index"] = cue_index
            item["natural_duration"] = speech_duration
            item["speech_padding"] = padding
            item["speech_tempo"] = max(1.0, speech_duration / max(1 / sample_rate, length / sample_rate - padding))
        item["video_factor"] = (end - source_frame) / length
        segments.append(item)
        source_frame, output_frame = end, output_frame + length

    for index, (group, speech_duration) in enumerate(zip(groups, speech_durations)):
        start = max(source_frame, min(total, round(group["start"] * sample_rate)))
        end = max(start, min(total, round(group["end"] * sample_rate)))
        if end <= start:
            continue
        if start > source_frame:
            add(start, start - source_frame, "gap")
        natural = _number(speech_duration)
        padding = min(.15, max(.025, (end - start) / sample_rate * .03))
        desired = math.ceil((natural + padding) * sample_rate)
        # Use a short immediately following subtitle gap before slowing video.
        next_start = round(groups[index + 1]["start"] * sample_rate) if index + 1 < len(groups) else total
        if desired > end - start and next_start > end:
            end += min(next_start - end, round(.6 * sample_rate), desired - (end - start))
        source_length = end - start
        length = min(max(source_length, desired), math.floor(source_length / MIN_VIDEO_FACTOR))
        add(end, length, "speech", index, natural, padding)
    if source_frame < total:
        add(total, total - source_frame, "gap")
    return segments


def validate_segments(segments, source_duration, output_duration, tolerance=1e-5):
    if not segments:
        raise RedubError("invalid_job")
    source = audio = 0.0
    for segment in segments:
        values = [_number(segment.get(key)) for key in
                  ("source_start", "source_end", "audio_start", "audio_end")]
        ss, se, os_, oe = values
        if (abs(ss - source) > tolerance or abs(os_ - audio) > tolerance or
                se <= ss or oe <= os_ or segment.get("kind") not in ("speech", "gap")):
            raise RedubError("invalid_job")
        source, audio = se, oe
    if abs(source - source_duration) > tolerance or abs(audio - output_duration) > tolerance:
        raise RedubError("invalid_job")


def fingerprint(source, cues):
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    digest.update(json.dumps(cues, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest.update(PIPELINE_VERSION.encode("ascii"))
    return digest.hexdigest()


def time_stretch_background(samples, target_frames, sample_rate=SAMPLE_RATE):
    """FFmpeg atempo preserves pitch; pad/trim only its tiny algorithmic tail.

    PyAV ships FFmpeg, so no system-wide ffmpeg installation is necessary. Each
    speech window is processed on its own and never holds the full video in RAM.
    """
    import av
    import numpy as np
    if len(samples) == target_frames:
        return samples.astype("float32", copy=False)
    if len(samples) < 2 or target_frames <= 0:
        return np.zeros((max(0, target_frames), 2), dtype="float32")
    speed = len(samples) / target_frames
    graph = av.filter.Graph()
    source = graph.add("abuffer", f"sample_rate={sample_rate}:sample_fmt=fltp:channel_layout=stereo")
    previous = source
    # Every atempo factor remains in its high quality 0.5..2.0 range.
    while speed < 0.5:
        current = graph.add("atempo", "0.5")
        previous.link_to(current)
        previous, speed = current, speed / 0.5
    while speed > 2.0:
        current = graph.add("atempo", "2.0")
        previous.link_to(current)
        previous, speed = current, speed / 2.0
    current = graph.add("atempo", str(speed))
    previous.link_to(current)
    planar = graph.add("aformat", "sample_fmts=fltp:channel_layouts=stereo")
    sink = graph.add("abuffersink")
    current.link_to(planar)
    planar.link_to(sink)
    graph.configure()
    output = []

    def drain():
        while True:
            try:
                frame = sink.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                break
            output.append(frame.to_ndarray().T)

    for start in range(0, len(samples), 16_384):
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[start:start + 16_384].T),
                                          format="fltp", layout="stereo")
        frame.sample_rate = sample_rate
        source.push(frame)
        drain()
    source.push(None)
    drain()
    audio = np.concatenate(output) if output else np.zeros((0, 2), dtype="float32")
    if len(audio) < target_frames:
        audio = np.pad(audio, ((0, target_frames - len(audio)), (0, 0)))
    return audio[:target_frames].astype("float32", copy=False)


def render_mix(background, speech_paths, segments, target, progress=None):
    import numpy as np
    import soundfile as sf
    with sf.SoundFile(str(background)) as bed, sf.SoundFile(str(target), mode="w", samplerate=SAMPLE_RATE,
                                                          channels=2, subtype="PCM_16", format="WAV") as out:
        for index, segment in enumerate(segments):
            start = round(segment["source_start"] * SAMPLE_RATE)
            end = round(segment["source_end"] * SAMPLE_RATE)
            length = round((segment["audio_end"] - segment["audio_start"]) * SAMPLE_RATE)
            bed.seek(start)
            # Long silent gaps need no transformation and are streamed in chunks.
            if segment["kind"] == "gap":
                remaining = end - start
                while remaining:
                    block = bed.read(min(remaining, SAMPLE_RATE * 10), dtype="float32", always_2d=True)
                    if not len(block):
                        raise RedubError("render")
                    out.write(block)
                    remaining -= len(block)
            else:
                music = bed.read(end - start, dtype="float32", always_2d=True)
                music = time_stretch_background(music, length)
                voice, rate = sf.read(str(speech_paths[segment["cue_index"]]), dtype="float32", always_2d=True)
                if rate != SAMPLE_RATE:
                    raise RedubError("render")
                padding = round(float(segment.get("speech_padding", .15)) * SAMPLE_RATE)
                available = max(1, length - padding)
                if len(voice) > available:
                    # Preserve a quiet tail through atempo's filter latency.
                    # The added silence absorbs any sample-count correction.
                    tail = min(padding, round(SAMPLE_RATE * .1))
                    protected = np.pad(voice, ((0, tail), (0, 0)))
                    stereo = np.repeat(protected, 2, axis=1) if protected.shape[1] == 1 else protected
                    target_frames = min(length, available + tail)
                    voice = time_stretch_background(stereo, target_frames)[:, :1]
                # Keep music modest under speech; use the same gain throughout
                # each segment, with short ramps to avoid sudden level changes.
                gain = np.full(length, 0.65, dtype="float32")
                fade = min(round(SAMPLE_RATE * 0.04), length // 2)
                if fade:
                    gain[:fade] = np.linspace(1, 0.65, fade)
                    gain[-fade:] = np.linspace(0.65, 1, fade)
                music *= gain[:, None]
                mixed = music
                mixed[:len(voice)] += voice
                peak = float(np.max(np.abs(mixed), initial=0))
                if peak > 0.98:
                    mixed *= 0.98 / peak
                out.write(mixed)
            if progress:
                progress((index + 1) / len(segments))
