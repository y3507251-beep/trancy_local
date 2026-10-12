"""One resident engine per desktop session; individual jobs never own model lifetime.

Whisper calls the shared JobManager. Qwen uses the same installed model classes,
weights, audio ranges and forced-alignment helpers as the shared Qwen worker, but
retains both model objects across requests. It runs in the shared Qwen interpreter
so its PyTorch DLLs stay isolated from the Whisper interpreter.
"""
from __future__ import annotations

import gc
import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace

_JOB_HANDLE = None


def normalize_timed_cues(cues, duration=None):
    """Keep the shared service's sentence boundaries and original word times.

    The service's export normalizer deliberately writes only start/end/text.
    Desktop playback needs the model's words as well; no word timings are
    invented here when an engine returns only a sentence.
    """
    result = []
    for cue in cues:
        start, end = float(cue["start"]), float(cue["end"])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("字幕时间必须是有限数字")
        text = str(cue["text"]).replace("\x00", "").strip()
        if not text or end <= start:
            continue
        start = max(0.0, start)
        if duration:
            end = min(end, duration)
        if end <= start:
            continue
        words = []
        for word in cue.get("words") or []:
            try:
                word_start, word_end = float(word["start"]), float(word["end"])
                word_text = str(word["text"]).replace("\x00", "")
            except (KeyError, TypeError, ValueError):
                continue
            if (not word_text.strip() or not all(math.isfinite(v) for v in (word_start, word_end))
                    or word_start < 0 or word_end < word_start or word_end < start or word_start >= end):
                continue
            words.append(dict(text=word_text, start=round(max(start, word_start), 3),
                              end=round(min(end, word_end), 3)))
        words.sort(key=lambda word: (word["start"], word["end"]))
        result.append(dict(cue, start=round(start, 3), end=round(end, 3), text=text,
                           words=words, timing_source=cue.get("timing_source", "cue") if words else "cue"))
    result.sort(key=lambda cue: (cue["start"], cue["end"]))
    return result


def segment_timed_cues(segment, max_chars=42, *, timing_source="whisper"):
    """Match service.subtitles.segment_cues while retaining its input words."""
    words = getattr(segment, "words", None)
    if not words:
        return [dict(start=segment.start, end=segment.end, text=segment.text.strip(),
                     words=[], timing_source="cue")]
    result, group = [], []

    def flush():
        if group:
            result.append(dict(start=group[0].start, end=group[-1].end,
                               text="".join(word.word for word in group).strip(),
                               words=[dict(text=word.word, start=word.start, end=word.end) for word in group],
                               timing_source=timing_source))
            group.clear()

    for word in words:
        if group and (word.start - group[-1].end > .65 or
                      len("".join(item.word for item in group)) + len(word.word) > max_chars):
            flush()
        group.append(word)
        if re.search(r"[。！？.!?]$", word.word.strip()):
            flush()
    flush()
    return result


def aligned_timed_cues(text, items, offset=0.0, duration=None):
    """Map Qwen forced-aligner spans to the original transcript, without loss.

    This follows service.qwen_cues.aligned_cues: punctuation stays attached to
    its original word, and normalization never fabricates evenly spaced times.
    """
    def kept(character):
        return character == "'" or unicodedata.category(character)[0] in ("L", "N")

    normalized, positions = [], []
    for index, character in enumerate(text):
        if kept(character):
            value = unicodedata.normalize("NFKC", character).casefold()
            normalized.extend(value)
            positions.extend([index] * len(value))
    canonical = "".join(normalized)
    words, cursor, canonical_cursor = [], 0, 0
    for item in items:
        token = str(item.text).strip()
        if not token:
            continue
        start, end = float(item.start_time), float(item.end_time)
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end < start:
            raise ValueError("Invalid forced alignment timestamp")
        cleaned = "".join(unicodedata.normalize("NFKC", c).casefold() for c in token if kept(c))
        if not cleaned or not canonical.startswith(cleaned, canonical_cursor):
            raise ValueError("Forced alignment text does not match transcript")
        begin = positions[canonical_cursor]
        canonical_cursor += len(cleaned)
        finish = positions[canonical_cursor - 1] + 1
        prefix = text[cursor:begin]
        if words and prefix.strip():
            words[-1].word += prefix.rstrip()
            prefix = prefix[len(prefix.rstrip()):]
        words.append(SimpleNamespace(word=prefix + text[begin:finish],
                                     start=offset + start, end=offset + end))
        cursor = finish
    if not words:
        if text.strip():
            raise ValueError("Nonempty transcript has no word timestamps")
        return []
    if canonical_cursor != len(canonical):
        raise ValueError("Forced alignment omitted transcript words")
    words[-1].word += text[cursor:]
    return normalize_timed_cues(segment_timed_cues(SimpleNamespace(words=words),
                                                  timing_source="qwen_aligner"), duration)


def own_process_tree():
    """Windows closes this job handle on exit and terminates owned Qwen children."""
    global _JOB_HANDLE
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = EXTENDED()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    _JOB_HANDLE = handle  # Intentionally retained until process exit.


def extract_audio(source: Path, destination: Path, check_cancel, on_progress, limit_seconds=None) -> dict:
    """Decode any supported container to mono PCM, preserving gaps on its timeline."""
    import av
    import numpy as np

    rate, written, decoded = 16000, 0, 0
    last_event = 0.0
    silence = bytes(rate * 2)
    with av.open(str(source), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("这个文件没有音频轨道，无法生成语音字幕")
        stream = container.streams.audio[0]
        origin = (container.start_time or 0) / av.time_base
        duration = (container.duration or 0) / av.time_base
        if not duration and stream.duration is not None:
            duration = float(stream.duration * stream.time_base)
        if not math.isfinite(duration) or duration < 0:
            duration = 0
        if limit_seconds:
            duration = min(duration, limit_seconds) if duration else limit_seconds
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        maximum = round(limit_seconds * rate) if limit_seconds else None
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)

            def write_frame(frame):
                nonlocal written, last_event
                check_cancel()
                samples = frame.to_ndarray().reshape(-1).astype("<i2", copy=False)
                target = round((frame.time - origin) * rate) if frame.time is not None else written
                if duration and target / rate > duration + 60:
                    raise ValueError("音频时间戳超出视频时长，不能安全生成对齐字幕")
                # Container timestamps can be rounded to milliseconds (WMV/RMVB).
                gap = target - written
                if written == 0 or abs(gap) > rate * .005:
                    if gap > 0:
                        if maximum is not None:
                            gap = min(gap, maximum - written)
                        while gap > 0:
                            check_cancel()
                            count = min(gap, rate)
                            output.writeframesraw(silence[:count * 2])
                            written += count
                            gap -= count
                    elif gap < 0:
                        samples = samples[min(-gap, len(samples)):]
                if maximum is not None:
                    samples = samples[:max(0, maximum - written)]
                if len(samples):
                    output.writeframesraw(samples.tobytes())
                    written += len(samples)
                if time.monotonic() - last_event >= .25:
                    on_progress(written / rate, duration)
                    last_event = time.monotonic()

            for frame in container.decode(stream):
                check_cancel()
                decoded += frame.samples
                for item in resampler.resample(frame):
                    write_frame(item)
                if maximum is not None and written >= maximum:
                    break
            else:
                for item in resampler.resample(None):
                    write_frame(item)
        if not decoded or not written:
            raise ValueError("音频轨道为空或无法解码")
        on_progress(written / rate, duration)
        return {"duration": max(duration, written / rate), "audio_duration": written / rate,
                "codec": stream.codec_context.name, "sample_rate": rate}




class Cancelled(Exception):
    pass


class EngineBase:
    def __init__(self, model_name, emit):
        self.model_name, self.emit = model_name, emit
        self.instance_id = ""
        self.load_count = 0
        self.device = None
        self.cancel = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def state(self, **values):
        self.emit(dict(event="state", **values))

    def model_event(self, reused):
        self.emit(dict(event="model", model=self.model_name, device=self.device,
                       worker_pid=os.getpid(), model_instance=self.instance_id,
                       load_count=self.load_count, reused=reused))

    def begin(self, request, cancel):
        self.cancel = cancel
        self.check()
        options = request["options"]
        if options["model"] != self.model_name:
            raise ValueError("模型已变化，需要先切换识别进程")
        if options["device"] not in ("auto", "cpu", "cuda"):
            raise ValueError("未知的运算设备")
        if options["language"] not in ("auto", "zh", "en", "ja", "ko"):
            raise ValueError("未知的识别语言")
        if options["batch_size"] not in (1, 2, 4, 8, 16):
            raise ValueError("无效的批量大小")
        if len(options.get("asr_context", "")) > 1000:
            raise ValueError("识别提示不能超过1000字")
        if self.instance_id and options["device"] not in ("auto", self.device):
            raise ValueError("运算设备已变化，需要先切换识别进程")
        source = Path(request["source"]).resolve(strict=True)
        directory = Path(request["job_directory"]).resolve(strict=True)
        audio = directory / "audio.wav"

        def progress(position, duration):
            self.state(status="extracting", duration=duration,
                       progress=min(20, position / duration * 20) if duration else 0,
                       message=f"正在提取音频：{int(position) // 60:02}:{int(position) % 60:02}",
                       indeterminate=not duration)

        self.state(status="extracting", progress=0, indeterminate=False, message="正在读取视频并提取音频")
        media = extract_audio(source, audio, self.check, progress, request.get("test_limit_seconds"))
        self.check()
        return audio, media, options

    def clear_job(self):
        self.cancel = None
        gc.collect()


class WhisperEngine(EngineBase):
    def __init__(self, name, emit):
        super().__init__(name, emit)
        from service import engine
        import faster_whisper  # Load native modules before blocking on the control pipe.
        self.engine = engine
        owner = self

        class MemoryManager(engine.JobManager):
            def __init__(self):
                self.lock = threading.RLock()
                self.model_lock = threading.RLock()
                self.models, self.jobs = {}, {}
                self.emitted_count = 0

            def _release_weights(self):
                # This adapter owns Whisper only; Qwen lives in a separate
                # desktop worker, not the shared service's browser session.
                self.models.clear()
                gc.collect()

            def check_cancel(self, job_id):
                owner.check()

            def save(self, job):
                reset = len(job["cues"]) < self.emitted_count
                if reset:
                    self.emitted_count = 0
                snapshot = {key: value for key, value in job.items() if key != "cues"}
                snapshot.update(new_cues=job["cues"][self.emitted_count:], reset_cues=reset)
                self.emitted_count = len(job["cues"])
                owner.state(**snapshot)

            def _transcribe(self, job_id, audio, options, device):
                # The shared service's export helpers discard segment.words.
                # Keep the same inference settings, model cache and sentence
                # segmentation here, preserving words only in desktop results.
                from service.audio import decode_audio
                model = self.load_model(job_id, options["model"], device)
                self.update(job_id, status="transcribing", device=device, progress=25,
                            message="正在逐段识别，生成的字幕会立即出现在预览中")
                waveform = decode_audio(audio, lambda: self.check_cancel(job_id))
                kwargs = dict(language=None if options["language"] == "auto" else options["language"],
                              task="transcribe", vad_filter=True, word_timestamps=True,
                              beam_size=5, condition_on_previous_text=False)
                batch_size = options.get("batch_size", 8) if device == "cuda" else 1
                if batch_size > 1:
                    from faster_whisper import BatchedInferencePipeline
                    segments, info = BatchedInferencePipeline(model=model).transcribe(
                        waveform, batch_size=batch_size, **kwargs)
                else:
                    segments, info = model.transcribe(waveform, **kwargs)
                self.update(job_id, batch_size=batch_size, detected_language=info.language)
                duration = self.jobs[job_id]["duration"] or info.duration
                for segment in segments:
                    self.check_cancel(job_id)
                    cues = normalize_timed_cues(segment_timed_cues(segment), duration)
                    with self.lock:
                        self.check_cancel(job_id)
                        job = self.jobs[job_id]
                        job["cues"].extend(cues)
                        job.update(progress=min(98, 25 + round(segment.end / max(duration, 1) * 73, 1)),
                                   message=f"已识别至 {int(segment.end) // 60:02d}:{int(segment.end) % 60:02d}",
                                   updated_at=time.time())
                        self.save(job)

            def load_model(self, job_id, name, device):
                key = (name, device)
                previous = self.models.get(key)
                try:
                    return super().load_model(job_id, name, device)
                finally:
                    # The shared loader checks cancellation after native loading.
                    # Record an already-loaded model even if that check cancelled
                    # the video, so the next task can reuse it and report it.
                    loaded = self.models.get(key)
                    if loaded is not None:
                        reused = loaded is previous and bool(owner.instance_id)
                        owner.device = device
                        if not reused:
                            owner.instance_id = uuid.uuid4().hex
                            owner.load_count += 1
                        owner.model_event(reused)

        self.manager = MemoryManager()

    def transcribe(self, request, cancel):
        started = time.monotonic()
        audio, media, options = self.begin(request, cancel)
        self.manager.jobs = {"local": dict(id="local", status="extracting", progress=20,
                                            cues=[], message="音频提取完成", indeterminate=False, **media)}
        self.manager.emitted_count = 0
        device = self.device or options["device"]
        if device == "auto":
            import ctranslate2
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
            except Exception:
                device = "cpu"
        fallback = False
        try:
            self.manager.transcribe("local", audio, options, device)
        except (RuntimeError, OSError):
            if options["device"] != "auto" or device != "cuda":
                raise
            fallback = True
        if fallback:
            self.manager.models.clear()
            self.instance_id = ""
            self.device = None
            self.manager.update("local", cues=[], device="cpu", message="GPU暂不可用，改用CPU识别")
            gc.collect()
            self.manager.transcribe("local", audio, options, "cpu")
        self.check()
        job = self.manager.jobs["local"]
        cues = normalize_timed_cues(job["cues"], job["duration"])
        return dict(cues=cues, no_speech=not bool(cues), duration=job["duration"], model=self.model_name, device=self.device,
                    detected_language=job.get("detected_language", ""), elapsed=round(time.monotonic() - started, 3),
                    worker_pid=os.getpid(), model_instance=self.instance_id, load_count=self.load_count)

    def clear_job(self):
        self.manager.jobs.clear()
        super().clear_job()


class QwenEngine(EngineBase):
    def __init__(self, name, emit):
        super().__init__(name, emit)
        import torch
        from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
        self.torch = torch
        self.asr_class, self.aligner_class = Qwen3ASRModel, Qwen3ForcedAligner
        torch.set_num_threads(min(8, os.cpu_count() or 4))
        self.model = None

    def load(self, device):
        if self.model is not None:
            self.model_event(True)
            return
        from service.qwen_models import ensure_models

        def model_progress(event):
            label = "识别模型" if event.get("model") == "asr-1.7b" else "时间对齐模型"
            if event["event"] == "download":
                self.state(status="model_downloading", indeterminate=False,
                           progress=event["received"] / max(1, event["total"]) * 100,
                           message=f"正在下载千问{label}")
            else:
                self.state(status="loading", indeterminate=True, message=f"正在校验千问{label}")

        paths = ensure_models(model_progress, self.check)
        if device == "cuda" and not self.torch.cuda.is_available():
            raise RuntimeError("Qwen未能使用NVIDIA GPU")
        kwargs = dict(dtype=self.torch.bfloat16 if device == "cuda" else self.torch.float32,
                      device_map="cuda:0" if device == "cuda" else "cpu",
                      attn_implementation="sdpa", local_files_only=True)
        self.state(status="loading", progress=22, indeterminate=True, message="正在加载千问识别模型（完成后可连续复用）")
        model = self.asr_class.from_pretrained(str(paths["asr-1.7b"]),
                                               max_inference_batch_size=1, max_new_tokens=1536, **kwargs)
        self.state(status="loading", indeterminate=True, message="正在加载千问时间对齐模型")
        model.forced_aligner = self.aligner_class.from_pretrained(str(paths["aligner-0.6b"]), **kwargs)
        # Finish loading both objects, even if a cancellation arrived during native loading.
        # A cancelled video must not throw away an otherwise usable resident model.
        self.model, self.device = model, device
        self.instance_id = uuid.uuid4().hex
        self.load_count += 1
        self.model_event(False)

    def recognize(self, audio, media, options, device):
        from service.audio import decode_audio
        from service.qwen_cues import audio_ranges
        reused = self.model is not None
        self.load(device)
        self.check()
        waveform = decode_audio(audio, self.check)
        ranges = list(audio_ranges(waveform))
        batch_size = options["batch_size"] if device == "cuda" else 1
        language = {"auto": None, "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean"}[options["language"]]
        self.state(status="transcribing", progress=25, indeterminate=False, device=device,
                   batch_size=batch_size, message="复用已加载的千问模型，正在识别并对齐字幕" if reused else "千问正在识别并对齐字幕")
        cues, index, detected = [], 0, ""
        while index < len(ranges):
            self.check()
            current = ranges[index:index + batch_size]
            self.model.max_inference_batch_size = batch_size
            try:
                results = self.model.transcribe(
                    audio=[(waveform[start:end], 16000) for start, end in current],
                    context=options.get("asr_context", ""), language=language, return_time_stamps=True)
                if len(results) != len(current):
                    raise ValueError("千问没有返回完整的音频批次")
            except self.torch.cuda.OutOfMemoryError:
                if batch_size == 1:
                    raise
                batch_size = max(1, batch_size // 2)
                self.torch.cuda.empty_cache()
                self.state(status="transcribing", batch_size=batch_size,
                           message=f"当前显存不足，本次改用{batch_size}段批量继续识别")
                continue
            self.check()
            for (start, end), result in zip(current, results):
                self.check()
                items = result.time_stamps.items if result.time_stamps else []
                if items:
                    part = aligned_timed_cues(result.text, items, offset=start / 16000,
                                              duration=media["duration"])
                else:
                    # A model can return a transcript with no aligner spans.
                    # Only the audio range is known; leave the words absent so
                    # the overlay can explicitly use its sentence estimate.
                    part = normalize_timed_cues([dict(start=start / 16000, end=end / 16000,
                                                     text=result.text, words=[], timing_source="cue")],
                                                media["duration"])
                cues.extend(part)
                detected = result.language
                self.state(status="transcribing", new_cues=part, detected_language=detected,
                           progress=min(98, 25 + end / 16000 / max(media["duration"], 1) * 73),
                           message=f"千问已识别并对齐至 {int(end / 16000) // 60:02}:{int(end / 16000) % 60:02}")
            index += len(current)
        return cues, detected

    def transcribe(self, request, cancel):
        started = time.monotonic()
        audio, media, options = self.begin(request, cancel)
        device = self.device or options["device"]
        if device == "auto":
            device = "cuda" if self.torch.cuda.is_available() else "cpu"
        fallback = False
        try:
            cues, detected = self.recognize(audio, media, options, device)
        except RuntimeError as error:
            gpu_error = isinstance(error, self.torch.cuda.OutOfMemoryError) or any(
                token in str(error).lower() for token in ("cuda", "cudnn", "cublas", "nvidia gpu"))
            if options["device"] != "auto" or device != "cuda" or not gpu_error:
                raise
            fallback = True
        if fallback:
            # After the exception scope is gone, release only the failed GPU instance.
            self.model = self.device = None
            self.instance_id = ""
            gc.collect()
            self.torch.cuda.empty_cache()
            self.state(status="loading", new_cues=[], reset_cues=True, message="GPU运行失败，改用CPU模型")
            cues, detected = self.recognize(audio, media, options, "cpu")
        self.check()
        return dict(cues=cues, no_speech=not bool(cues), duration=media["duration"], model=self.model_name, device=self.device,
                    detected_language=detected, elapsed=round(time.monotonic() - started, 3),
                    worker_pid=os.getpid(), model_instance=self.instance_id, load_count=self.load_count)

    def clear_job(self):
        super().clear_job()
        # Release only unused activation buffers. Model weights remain referenced.
        if self.device == "cuda":
            self.torch.cuda.empty_cache()


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr
    active_id = ""

    def emit(event):
        print(json.dumps(dict(event, request_id=active_id), ensure_ascii=False), file=protocol, flush=True)

    own_process_tree()
    first_line = sys.stdin.readline()
    if not first_line:
        return 0
    first = json.loads(first_line)
    active_id = first["request_id"]
    service_root = Path(first["service_root"]).resolve(strict=True)
    sys.path.insert(0, str(service_root))
    os.environ.setdefault("HF_HOME", str(service_root / "models" / "huggingface"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    emit(dict(event="state", status="starting", indeterminate=True, message="正在启动识别运行环境"))
    # Native imports before the control reader prevent Windows NumPy/stdin deadlock.
    import numpy
    import av
    name = first["options"]["model"]
    if name not in ("small", "large-v3", "qwen3-asr-1.7b"):
        raise ValueError("未知的识别模型")
    try:
        engine = QwenEngine(name, emit) if name == "qwen3-asr-1.7b" else WhisperEngine(name, emit)
    except Exception as error:
        traceback.print_exc()
        emit(dict(event="error", message=f"{type(error).__name__}：{error}"))
        return 1
    requests = queue.Queue(maxsize=1)
    requests.put(first)
    stopping = threading.Event()
    cancelled_ids = set()
    active_cancel = None
    lock = threading.Lock()

    def controls():
        nonlocal active_cancel
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(command, dict):
                    continue
                with lock:
                    if command.get("command") == "cancel":
                        cancelled_ids.add(command.get("request_id"))
                        if command.get("request_id") == active_id and active_cancel is not None:
                            active_cancel.set()
                    elif command.get("command") == "shutdown":
                        break
                    elif command.get("command") == "transcribe":
                        try:
                            requests.put_nowait(command)
                        except queue.Full:
                            pass  # The GUI admits only one request; never accumulate work.
        finally:
            stopping.set()
            with lock:
                if active_cancel is not None:
                    active_cancel.set()
            try:
                requests.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=controls, daemon=True)
    reader.start()
    while not stopping.is_set():
        request = requests.get()
        if request is None or stopping.is_set():
            break
        with lock:
            active_id = request["request_id"]
            active_cancel = threading.Event()
            if active_id in cancelled_ids or stopping.is_set():
                active_cancel.set()
        try:
            result = engine.transcribe(request, active_cancel)
            terminal = dict(event="result", **result)
        except Cancelled:
            terminal = dict(event="cancelled", message="已停止当前任务，模型继续保留")
        except Exception as error:
            traceback.print_exc()
            terminal = dict(event="error", message=f"{type(error).__name__}：{error}")
        finally:
            engine.clear_job()
        emit(terminal)
        terminal = result = request = None
        with lock:
            cancelled_ids.discard(active_id)
            active_id = ""
            active_cancel = None
    reader.join(timeout=1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
