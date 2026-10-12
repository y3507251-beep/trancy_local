"""Bounded WASAPI loopback recording, never a microphone fallback.

The PortAudio callback only copies PCM into a bounded queue. A QThread converts
and writes short WAV chunks; the GUI receives paths and never handles raw audio.
Chunk files are owned by the caller and must be recycled after ASR completes.
"""
from __future__ import annotations

import math
import os
import queue
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal


def _pyaudio_module():
    if os.name != "nt":
        raise RuntimeError("系统声音捕获目前仅支持 Windows WASAPI")
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError("缺少 PyAudioWPatch，请在桌面程序的 Python 环境安装 PyAudioWPatch") from exc
    return pyaudio


def _loopback_devices(manager) -> list[dict]:
    try:
        default_index = int(manager.get_default_wasapi_loopback()["index"])
    except (OSError, RuntimeError, ValueError):
        default_index = -1
    result = []
    for item in manager.get_loopback_device_info_generator():
        if not item.get("isLoopbackDevice") or int(item.get("maxInputChannels", 0)) < 1:
            continue
        result.append(dict(index=int(item["index"]), name=str(item["name"]),
                           channels=int(item["maxInputChannels"]), sample_rate=int(item["defaultSampleRate"]),
                           is_default=int(item["index"]) == default_index, loopback=True))
    return result


@dataclass
class AudioChunk:
    pcm: bytes
    start: float
    end: float
    has_audio: bool


class PCMChunker:
    """Convert interleaved signed-16 PCM to fixed-length mono chunks.

    Silent chunks are still returned so the caller can keep a continuous clock.
    Only the recorder decides whether to write them. This energy gate is not a
    speech detector; SubtitleService's VAD performs the later speech filtering.
    """

    def __init__(self, sample_rate: int, channels: int, chunk_seconds: float, *, threshold=0.002):
        if not 8000 <= sample_rate <= 384000 or not 1 <= channels <= 32:
            raise ValueError("无效的系统声音格式")
        if not math.isfinite(chunk_seconds) or not 0.5 <= chunk_seconds <= 30:
            raise ValueError("音频分段长度必须在 0.5 到 30 秒之间")
        self.sample_rate, self.channels = sample_rate, channels
        self.chunk_frames = max(1, round(sample_rate * chunk_seconds))
        self.threshold = threshold
        self._buffer = bytearray()
        self._written_frames = 0

    def push(self, pcm: bytes, *, start_frame: int | None = None) -> list[AudioChunk]:
        if len(pcm) % (2 * self.channels):
            raise ValueError("系统声音缓冲区未按完整采样帧对齐")
        chunks = []
        expected = self._written_frames + len(self._buffer) // (2 * self.channels)
        # Some WASAPI endpoints omit silent callbacks. Keep those gaps on the
        # capture clock, instead of making later speech appear too early.
        if start_frame is not None and start_frame - expected > self.sample_rate * .10:
            tail = self.finish()
            if tail is not None:
                chunks.append(tail)
            self._written_frames = int(start_frame)
        self._buffer.extend(pcm)
        size = self.chunk_frames * self.channels * 2
        while len(self._buffer) >= size:
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            chunks.append(self._convert(data))
        return chunks

    def finish(self) -> AudioChunk | None:
        if not self._buffer:
            return None
        data = bytes(self._buffer)
        self._buffer.clear()
        return self._convert(data)

    def _convert(self, pcm: bytes) -> AudioChunk:
        frames = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels).astype(np.float32)
        mono = np.clip(frames.mean(axis=1), -32768, 32767).astype("<i2")
        start = self._written_frames / self.sample_rate
        self._written_frames += len(mono)
        end = self._written_frames / self.sample_rate
        # Evaluate short windows, so a spoken word in an otherwise quiet chunk
        # is retained. At least 100 ms above the noise floor is required.
        window = max(1, self.sample_rate // 50)
        active_frames = 0
        normalized = mono.astype(np.float32) / 32768.0
        for offset in range(0, len(normalized), window):
            part = normalized[offset:offset + window]
            if len(part) and float(np.sqrt(np.mean(part * part))) >= self.threshold:
                active_frames += len(part)
        return AudioChunk(mono.tobytes(), start, end, active_frames >= self.sample_rate * .10)


class _CaptureThread(QThread):
    chunk_ready = pyqtSignal(str, float, float)
    level = pyqtSignal(float)
    error = pyqtSignal(str)

    def __init__(self, output_dir: Path, chunk_seconds: float, device_index, parent=None):
        super().__init__(parent)
        self.output_dir, self.chunk_seconds, self.device_index = output_dir, chunk_seconds, device_index
        self._stop_requested = threading.Event()

    def stop(self):
        self._stop_requested.set()

    def run(self):
        manager = stream = None
        raw_queue = queue.Queue(maxsize=64)
        overflow = threading.Event()
        callback_error = threading.Event()
        try:
            pyaudio = _pyaudio_module()
            manager = pyaudio.PyAudio()
            devices = _loopback_devices(manager)
            if self.device_index is None:
                selected = next((item for item in devices if item["is_default"]), None)
            else:
                selected = next((item for item in devices if item["index"] == int(self.device_index)), None)
            if selected is None:
                raise RuntimeError("找不到所选输出设备的 WASAPI 环回输入，请检查扬声器或耳机")
            # Recheck the actual device immediately before opening it. A normal
            # input device is rejected even if a stale device index was saved.
            actual = manager.get_device_info_by_index(selected["index"])
            if not actual.get("isLoopbackDevice"):
                raise RuntimeError("所选设备不是 WASAPI 环回设备，已停止系统声音捕获")
            sample_rate, channels = selected["sample_rate"], selected["channels"]
            chunker = PCMChunker(sample_rate, channels, self.chunk_seconds)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            generation = uuid.uuid4().hex
            sequence, last_level, last_packet = 0, 0.0, time.monotonic()

            def callback(in_data, frame_count, time_info, status):
                if self._stop_requested.is_set():
                    return None, pyaudio.paComplete
                if status & pyaudio.paInputOverflow:
                    overflow.set()
                    return None, pyaudio.paAbort
                if not in_data or len(in_data) != frame_count * channels * 2:
                    callback_error.set()
                    return None, pyaudio.paAbort
                try:
                    timestamp = float(time_info.get("input_buffer_adc_time", 0.0))
                    raw_queue.put_nowait((bytes(in_data), timestamp))
                except queue.Full:
                    overflow.set()
                    return None, pyaudio.paAbort
                return None, pyaudio.paContinue

            def save_chunk(chunk: AudioChunk):
                nonlocal sequence
                if not chunk.has_audio or self._stop_requested.is_set():
                    return
                path = self.output_dir / f"system-{generation}-{sequence:06d}.wav"
                sequence += 1
                # Unique names never overwrite a source or another session.
                try:
                    with path.open("xb") as raw:
                        with wave.open(raw, "wb") as output:
                            output.setnchannels(1)
                            output.setsampwidth(2)
                            output.setframerate(sample_rate)
                            output.writeframes(chunk.pcm)
                except Exception:
                    self._recycle_unsubmitted(path)
                    raise
                if self._stop_requested.is_set():
                    self._recycle_unsubmitted(path)
                    return
                self.chunk_ready.emit(str(path), chunk.start, chunk.end)

            if self._stop_requested.is_set():
                return
            stream = manager.open(format=pyaudio.paInt16, channels=channels, rate=sample_rate,
                                  input=True, input_device_index=selected["index"], frames_per_buffer=2048,
                                  stream_callback=callback, start=False)
            clock_origin = stream.get_time()
            stream.start_stream()
            while not self._stop_requested.is_set():
                if overflow.is_set():
                    raise RuntimeError("系统声音采集发生积压或溢出，已停止录制，请降低识别负载后重试")
                if callback_error.is_set():
                    raise RuntimeError("WASAPI 返回了不完整的音频缓冲区，已停止录制")
                try:
                    data, timestamp = raw_queue.get(timeout=.10)
                except queue.Empty:
                    if not stream.is_active():
                        raise RuntimeError("系统声音设备已停止，请重新选择输出设备")
                    # Silent endpoints can omit callbacks, so absence of data
                    # alone is not interpreted as a microphone/driver failure.
                    if time.monotonic() - last_packet > 1 and time.monotonic() - last_level > .20:
                        self.level.emit(0.0)
                        last_level = time.monotonic()
                    continue
                last_packet = time.monotonic()
                if last_packet - last_level >= .10:
                    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                    self.level.emit(min(1.0, float(np.sqrt(np.mean(samples * samples))) * 4))
                    last_level = last_packet
                start_frame = (round(max(0, timestamp - clock_origin) * sample_rate)
                               if math.isfinite(timestamp) and timestamp > 0 else None)
                for chunk in chunker.push(data, start_frame=start_frame):
                    save_chunk(chunk)
            # Stopping intentionally discards the in-memory tail. No partial
            # input file is handed to the ASR queue after the user stops.
        except Exception as exc:
            if not self._stop_requested.is_set():
                self.error.emit(str(exc))
        finally:
            self._stop_requested.set()
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if manager is not None:
                try:
                    manager.terminate()
                except Exception:
                    pass
            self.level.emit(0.0)

    def _recycle_unsubmitted(self, path: Path):
        try:
            if path.exists() and path.resolve().parent == self.output_dir.resolve():
                from send2trash import send2trash
                send2trash(str(path))
        except Exception as exc:
            self.error.emit(f"未提交的录音文件回收失败，文件已保留：{path}（{exc}）")


class SystemAudioCapture(QObject):
    chunk_ready = pyqtSignal(str, float, float)
    level = pyqtSignal(float)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: _CaptureThread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    @staticmethod
    def devices() -> list[dict]:
        pyaudio = _pyaudio_module()
        with pyaudio.PyAudio() as manager:
            return _loopback_devices(manager)

    def start(self, output_dir: str, chunk_seconds=4.0, device_index=None):
        if self._thread is not None:
            self.error.emit("系统声音捕获正在运行，请先停止")
            return
        try:
            seconds = float(chunk_seconds)
            if not math.isfinite(seconds) or not 0.5 <= seconds <= 30:
                raise ValueError("音频分段长度必须在 0.5 到 30 秒之间")
            directory = Path(output_dir).resolve()
        except (TypeError, ValueError, OSError) as exc:
            self.error.emit(str(exc))
            return
        thread = _CaptureThread(directory, seconds, device_index, self)
        self._thread = thread
        thread.chunk_ready.connect(self.chunk_ready)
        thread.level.connect(self.level)
        thread.error.connect(self.error)
        thread.finished.connect(lambda t=thread: self._finished(t))
        thread.start()

    def stop(self):
        if self._thread is not None:
            self._thread.stop()

    def _finished(self, thread):
        if self._thread is thread:
            self._thread = None
            thread.deleteLater()
            self.stopped.emit()
