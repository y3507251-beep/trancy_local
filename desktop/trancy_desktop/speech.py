"""Asynchronous offline English speech with reusable Windows SAPI voices.

Constructing this object starts no worker and creates no cache directory. The
first uncached request starts one owned PowerShell/System.Speech process. Text
is plain JSON data on stdin. Successful PCM WAV files are retained by a hash
of the text, requested voice and synthesis rate; no speech file is deleted.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid
import wave

from PyQt6.QtCore import QObject, QProcess, QThread, QTimer, pyqtSignal

from .settings import DATA


DEFAULT_VOICE = "auto"
ENGINE_VERSION = "windows-system-speech-pcm-22050-v1"
MAX_QUEUE = 64
MAX_TEXT = 4000
RENDER_TIMEOUT_MS = 20_000
_MESSAGES = {
    "invalid_request": "本地配音请求无效",
    "queue_full": "本地配音准备队列已满，请稍后重试",
    "no_english_voice": "Windows 没有可用的本地英语语音，请安装英语语音包",
    "voice_unavailable": "所选 Windows 本地英语音色不可用",
    "engine_unavailable": "无法启动本地英语语音引擎",
    "kokoro_unavailable": "Kokoro 本地模型或运行环境未就绪，请运行 setup-kokoro.ps1",
    "worker_exited": "Windows 本地英语语音引擎意外退出",
    "worker_protocol": "本地英语语音引擎返回无效数据",
    "render_failed": "本地英语配音生成失败",
    "render_timeout": "本地英语配音生成超时，已停止此次生成",
    "invalid_audio": "本地英语配音文件无效",
    "cache_error": "本地英语配音缓存无法写入",
}


class SystemSpeechBackend:
    """Process command provider, replaceable by a controlled test worker."""

    def command(self, cache_dir: Path) -> tuple[str, list[str]]:
        if os.name != "nt":
            raise OSError("Windows System.Speech is unavailable")
        program = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe")
        script = Path(__file__).with_name("speech_worker.ps1")
        if not program.is_file() or not script.is_file():
            raise OSError("Windows System.Speech worker is unavailable")
        return str(program), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-WindowStyle", "Hidden", "-File", str(script),
                              "-CacheRoot", str(cache_dir)]


class _SpeechProcess(QThread):
    """Own synchronous Windows pipes outside Qt's overlapped process handles.

    System.Speech's .NET console reader stalls on repeated reads from QProcess
    pipes on this Windows host. Popen's synchronous pipes support the same
    retained synthesizer, while all I/O and bounded waits stay in this thread.
    """
    message = pyqtSignal(dict)

    def __init__(self, program: str, arguments: list[str], directory: Path, parent=None):
        super().__init__(parent)
        self.program, self.arguments, self.directory = program, arguments, directory
        self._requests = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._owned: subprocess.Popen | None = None

    def submit(self, command: dict):
        self._requests.put(command)

    def state(self):
        if not self.isRunning():
            return QProcess.ProcessState.NotRunning
        return QProcess.ProcessState.Running if self._owned else QProcess.ProcessState.Starting

    def processId(self):
        return self._owned.pid if self._owned else 0

    def kill(self):
        self._stop.set()
        self._requests.put(None)
        with self._lock:
            process = self._owned
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def run(self):
        process = None
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([self.program, *self.arguments], cwd=self.directory,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, creationflags=flags)
            with self._lock:
                self._owned = process
            if self._stop.is_set():
                return
            while not self._stop.is_set():
                command = self._requests.get()
                if command is None or self._stop.is_set():
                    break
                process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
                process.stdin.flush()
                while not self._stop.is_set():
                    line = process.stdout.readline(64 * 1024 + 1)
                    if not line:
                        if not self._stop.is_set():
                            self.message.emit(dict(event="fatal", code="worker_exited"))
                        return
                    try:
                        event = json.loads(line.decode("utf-8-sig"))
                        if len(line) > 64 * 1024 or not isinstance(event, dict):
                            raise ValueError("Invalid worker response")
                    except (UnicodeError, ValueError):
                        self.message.emit(dict(event="fatal", code="worker_protocol"))
                        return
                    self.message.emit(event)
                    if event.get("event") == "fatal":
                        return
                    if (event.get("event") in ("ready", "failed") and
                            event.get("request_id") == command["request_id"]):
                        break
        except (OSError, ValueError):
            if not self._stop.is_set():
                self.message.emit(dict(event="fatal", code="engine_unavailable"))
        finally:
            if process is not None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass


def speech_cache_key(text: str, voice: str = DEFAULT_VOICE, rate: int = 0, *, engine=ENGINE_VERSION) -> str:
    payload = json.dumps([engine, text, voice, rate], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def wave_info(path: str | Path) -> dict:
    """Validate a bounded uncompressed PCM WAV without decoding all samples."""
    path = Path(path)
    size = path.stat().st_size if path.is_file() else 0
    if not 44 < size <= 32 * 1024 * 1024:
        raise ValueError("Invalid WAV file")
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
        if (audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or
                audio.getsampwidth() != 2 or not 8000 <= audio.getframerate() <= 96000 or
                not math.isfinite(duration) or not 0 < duration <= 600 or
                audio.getnframes() * audio.getnchannels() * audio.getsampwidth() > size - 44):
            raise ValueError("Invalid PCM speech")
        return dict(duration=duration, sample_rate=audio.getframerate(),
                    channels=audio.getnchannels(), samples=audio.getnframes())


class LocalSpeech(QObject):
    engine_version = ENGINE_VERSION
    ready = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, parent=None, *, cache_dir: str | Path | None = None,
                 backend=None, max_queue=MAX_QUEUE, timeout_ms=RENDER_TIMEOUT_MS):
        super().__init__(parent)
        self.cache_dir = Path(cache_dir) if cache_dir else DATA / "dubbing-cache"
        self.backend = backend or SystemSpeechBackend()
        self.max_queue = max(1, int(max_queue))
        self.timeout_ms = max(100, int(timeout_ms))
        self.process: _SpeechProcess | None = None
        self.voices: list[str] = []
        self._generation: int | None = None
        self._queue: deque[dict] = deque()
        self._active: dict | None = None
        self._stopping = False
        self._closing = False
        self._closed_emitted = False
        self._drive_scheduled = False
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._timeout)

    @property
    def busy(self) -> bool:
        return bool(self._active or self._queue or self._stopping)

    @property
    def is_closed(self) -> bool:
        return self._closed_emitted

    @property
    def pending_count(self) -> int:
        return len(self._queue) + int(self._active is not None)

    def request(self, text: str, token, generation: int, *, voice=DEFAULT_VOICE, rate=0) -> bool:
        """Queue plain English text; results retain the caller's token/generation.

        ``rate`` is the Windows synthesis rate (-10..10), distinct from the
        video's playback speed. ``voice='auto'`` prefers Zira, then another
        installed English voice. An obsolete generation is silently ignored.
        """
        if self._closing:
            return False
        try:
            if not isinstance(text, str) or not isinstance(token, (str, int)) or isinstance(token, bool):
                raise ValueError("Invalid text or token")
            text = " ".join(text.replace("\x00", "").split())
            if (not text or len(text) > MAX_TEXT or not str(token) or len(str(token)) > 256 or
                    isinstance(generation, bool) or not isinstance(generation, int) or generation < 0 or
                    isinstance(rate, bool) or not isinstance(rate, int) or not -10 <= rate <= 10 or
                    not isinstance(voice, str) or not 0 < len(voice) <= 200):
                raise ValueError("Invalid request")
        except (TypeError, ValueError):
            self.failed.emit(dict(token=token, generation=generation, code="invalid_request",
                                  message=_MESSAGES["invalid_request"]))
            return False
        if self._generation is None:
            self._generation = generation
        if generation != self._generation:
            return False
        if any(task["token"] == token and task["generation"] == generation
               for task in ([self._active] if self._active else []) + list(self._queue)):
            return True
        if self.pending_count >= self.max_queue:
            self.failed.emit(dict(token=token, generation=generation, code="queue_full",
                                  message=_MESSAGES["queue_full"]))
            return False
        key = speech_cache_key(text, voice, rate, engine=self.engine_version)
        task = dict(text=text, token=token, generation=generation, voice=voice, rate=rate,
                    key=key, request_id=uuid.uuid4().hex, accepted_at=time.monotonic())
        self._queue.append(task)
        self._schedule_drive()
        return True

    def reset(self, generation: int | None = None):
        """Discard old queued work and kill an active render without blocking."""
        if self._closing:
            return
        if generation is None:
            generation = 0 if self._generation is None else self._generation + 1
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("Invalid speech generation")
        self._generation = generation
        self._queue.clear()
        self._deadline.stop()
        active, self._active = self._active, None
        if active is not None and self.process is not None:
            self._kill_process()

    def shutdown(self):
        """Nonblocking, idempotent close; an active synthesis is terminated."""
        if self._closing:
            return
        self._closing = True
        self._queue.clear()
        self._active = None
        self._deadline.stop()
        if self.process is None:
            self._emit_closed()
        else:
            self._kill_process()

    def _schedule_drive(self):
        if not self._drive_scheduled:
            self._drive_scheduled = True
            QTimer.singleShot(0, self._drive)

    def _drive(self):
        self._drive_scheduled = False
        if self._closing or self._stopping or self._active:
            return
        while self._queue:
            task = self._queue.popleft()
            if task["generation"] != self._generation:
                continue
            cached = self._cached(task)
            if cached:
                self._emit_ready(task, **cached, cached=True)
                if self._closing or self._stopping or self._active:
                    return
                continue
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self.cache_dir = self.cache_dir.resolve(strict=True)
                task["output"] = str(self.cache_dir / ("pending-" + uuid.uuid4().hex + ".wav"))
            except OSError:
                self._emit_failed(task, "cache_error")
                continue
            self._active = task
            self._deadline.start(self.timeout_ms)
            if self.process is None:
                self._launch()
            elif self.process.state() == QProcess.ProcessState.Running:
                self._send_request()
            return

    def _cached(self, task) -> dict | None:
        path = self.cache_dir / (task["key"] + ".wav")
        try:
            info = wave_info(path)
            voice = task["voice"]
            try:
                metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                if isinstance(metadata, dict) and isinstance(metadata.get("voice"), str):
                    voice = metadata["voice"]
            except (OSError, ValueError):
                pass
            return dict(path=str(path.resolve()), voice=voice, **info)
        except (OSError, ValueError, EOFError, wave.Error, ZeroDivisionError):
            return None

    def _launch(self):
        try:
            program, arguments = self.backend.command(self.cache_dir)
        except (OSError, TypeError, ValueError) as error:
            code = getattr(error, "code", "engine_unavailable")
            self._fail_all(code if code in _MESSAGES else "engine_unavailable")
            return
        process = _SpeechProcess(str(program), [str(value) for value in arguments], self.cache_dir, self)
        self.process = process
        process.started.connect(lambda p=process: self._started(p))
        process.message.connect(lambda event, p=process: self._read(p, event))
        process.finished.connect(lambda p=process: self._exited(p))
        process.start()

    def _started(self, process):
        if process is not self.process:
            return
        if self._stopping or self._closing:
            process.kill()
        elif self._active:
            self._send_request()

    def _send_request(self):
        task = self._active
        if task is None or self.process is None:
            return
        payload = dict(command="synthesize", request_id=task["request_id"], text=task["text"],
                       voice=task["voice"], rate=task["rate"], output=task["output"])
        self.process.submit(payload)

    def _read(self, process, event):
        if process is not self.process or self._stopping:
            return
        if event.get("event") == "voices":
            self.voices = [name for name in event.get("voices", []) if isinstance(name, str)]
            return
        if event.get("event") == "fatal":
            code = event.get("code")
            self._fail_all(code if code in _MESSAGES else "engine_unavailable")
            self._kill_process()
            return
        task = self._active
        if task is None or event.get("request_id") != task["request_id"]:
            return
        if event.get("event") == "ready":
            self._publish(task, event)
        elif event.get("event") == "failed":
            code = event.get("code")
            self._fail_active(code if code in _MESSAGES else "render_failed")

    def _publish(self, task, event):
        try:
            output = Path(task["output"])
            if Path(str(event.get("path", ""))).resolve() != output:
                raise ValueError("Unexpected output")
            info = wave_info(output)
        except (OSError, ValueError, EOFError, wave.Error, ZeroDivisionError):
            self._fail_active("invalid_audio")
            return
        final = self.cache_dir / (task["key"] + ".wav")
        voice = str(event.get("voice") or task["voice"])
        try:
            if final.exists():
                # Retain an invalid cache entry for diagnosis instead of deleting
                # or overwriting it. All paths stay in the owned cache directory.
                try:
                    info = wave_info(final)
                except (OSError, ValueError, EOFError, wave.Error, ZeroDivisionError):
                    final.rename(self.cache_dir / ("invalid-" + uuid.uuid4().hex + ".wav"))
                    output.rename(final)
            else:
                output.rename(final)
            final.with_suffix(".json").write_text(json.dumps(
                dict(engine=self.engine_version, voice=voice, rate=task["rate"], **info),
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        except OSError:
            self._fail_active("cache_error")
            return
        self._active = None
        self._deadline.stop()
        self._emit_ready(task, path=str(final), voice=voice, **info, cached=False,
                         render_ms=event.get("render_ms", 0))
        self._schedule_drive()

    def _emit_ready(self, task, **info):
        if not self._closing and task["generation"] == self._generation:
            self.ready.emit(dict(token=task["token"], generation=task["generation"],
                                 rate=task["rate"], engine=self.engine_version,
                                 elapsed_ms=round((time.monotonic() - task["accepted_at"]) * 1000), **info))

    def _emit_failed(self, task, code):
        if not self._closing and task["generation"] == self._generation:
            self.failed.emit(dict(token=task["token"], generation=task["generation"], code=code,
                                  message=_MESSAGES[code]))

    def _fail_active(self, code, *, kill=False):
        task, self._active = self._active, None
        self._deadline.stop()
        if kill:
            self._kill_process()
        if task:
            self._emit_failed(task, code)
        self._schedule_drive()

    def _fail_all(self, code):
        tasks = ([self._active] if self._active else []) + list(self._queue)
        self._active = None
        self._queue.clear()
        self._deadline.stop()
        for task in tasks:
            self._emit_failed(task, code)

    def _timeout(self):
        self._fail_active("render_timeout", kill=True)

    def _kill_process(self):
        process = self.process
        if process is None:
            return
        self._stopping = True
        if process.state() == QProcess.ProcessState.NotRunning:
            self._exited(process)
        else:
            process.kill()
            # A reset during Starting is also handled by _started. No wait is
            # performed on the UI thread, including during application close.
            QTimer.singleShot(1000, lambda p=process: p.kill() if p is self.process else None)

    def _exited(self, process):
        if process is not self.process:
            return
        self.process = None
        process.deleteLater()
        self._stopping = False
        if self._active:
            self._fail_active("worker_exited")
        if self._closing:
            self._emit_closed()
        else:
            self._schedule_drive()

    def _emit_closed(self):
        if not self._closed_emitted:
            self._closed_emitted = True
            self.closed.emit()
