"""Qt coordinator for the shared SubtitleService recognition environments.

There is one owned worker and at most one accepted job. The worker, and therefore
its model weights, survive completed, failed and normally cancelled jobs. Native
imports/decoding/inference and recycling all happen outside the GUI thread.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QThread, QTimer, pyqtSignal


DEFAULT_SERVICE_ROOT = r"D:\dev\SubtitleService"
MODELS = ("small", "large-v3", "qwen3-asr-1.7b")


def shifted_cues(cues: list, offset: float) -> list[dict]:
    """Validate the service's time axis, then shift a live-audio chunk once."""
    result = []
    for cue in cues:
        try:
            start, end = float(cue["start"]), float(cue["end"])
            text = str(cue.get("text", "")).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and end > start >= 0 and text):
            continue
        result.append(dict(cue, start=round(start + offset, 4), end=round(end + offset, 4), text=text))
    return result


def language_code(value: str) -> str:
    return {"chinese": "zh", "english": "en"}.get(str(value).lower(), str(value).lower())


class _Recycler(QThread):
    """Only a newly created task directory may be moved to the Recycle Bin."""
    problem = pyqtSignal(str)

    def __init__(self, directory: Path, root: Path, parent=None):
        super().__init__(parent)
        self.directory, self.root = directory, root

    def run(self):
        try:
            target, root = self.directory.resolve(), self.root.resolve()
            if target.parent != root or not target.name.startswith("job-"):
                raise ValueError("临时任务目录不在允许回收的范围内")
            if target.exists():
                from send2trash import send2trash
                send2trash(str(target))
        except Exception as exc:
            self.problem.emit(f"临时文件回收失败，文件已保留：{self.directory}（{exc}）")


class ASRSession(QObject):
    updated = pyqtSignal(dict)  # QObject.event is a virtual method, not a signal name.
    idle = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent=None, *, runtime_dir: str | Path | None = None):
        super().__init__(parent)
        self.runtime_dir = Path(runtime_dir) if runtime_dir else Path(__file__).resolve().parents[1] / "data" / "asr"
        self.process: QProcess | None = None
        self.key = None
        self.model_info: dict = {}
        self._active: dict | None = None
        self._pending: dict | None = None
        self._buffer = b""
        self._error_tail = ""
        self._cleaners: set[_Recycler] = set()
        self._stopping = False
        self._closing = False
        self._closed_emitted = False

    @property
    def busy(self) -> bool:
        return self._active is not None or self._pending is not None or bool(self._cleaners)

    def is_busy(self) -> bool:
        return self.busy

    def submit(self, source: str, job_id: str, *, model="small", device="auto", language="auto",
               offset_seconds=0.0, service_root=DEFAULT_SERVICE_ROOT) -> bool:
        if self._closing or self.busy:
            return False
        try:
            root = Path(service_root).resolve(strict=True)
            media = Path(source).resolve(strict=True)
            offset = float(offset_seconds)
            if not media.is_file():
                raise ValueError("识别来源必须是音频或视频文件")
            if model not in MODELS or device not in ("auto", "cpu", "cuda"):
                raise ValueError("不支持的识别模型或设备")
            if language not in ("auto", "zh", "en", "ja", "ko"):
                raise ValueError("不支持的识别语言")
            if not job_id or not math.isfinite(offset) or offset < 0:
                raise ValueError("无效的任务标识或字幕起始时间")
            python = self._interpreter(root, model)
            if not python.is_file() or not (root / "service" / "engine.py").is_file():
                raise ValueError(f"没有找到 SubtitleService 识别环境：{python}")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            directory = self.runtime_dir / ("job-" + uuid.uuid4().hex)
            directory.mkdir()
        except (OSError, TypeError, ValueError) as exc:
            self.updated.emit(dict(event="error", job_id=job_id, message=str(exc)))
            return False
        task = dict(job_id=job_id, request_id=uuid.uuid4().hex, source=str(media), service_root=str(root),
                    job_directory=str(directory), offset_seconds=offset, cancelled=False,
                    options=dict(model=model, device=device, language=language, batch_size=1, asr_context=""))
        if self.process is not None and not self._stopping and self._compatible(task):
            self._active = task
            self._send_request(task)
        else:
            self._pending = task
            if self.process is not None:
                self.updated.emit(dict(event="state", job_id=job_id, status="switching", progress=0,
                                     message="正在切换识别模型", indeterminate=True))
                self._stop_process()
            else:
                self._launch_pending()
        return True

    @staticmethod
    def _interpreter(root: Path, model: str) -> Path:
        return root / ("runtimes/qwen3/Scripts/python.exe" if model == "qwen3-asr-1.7b" else ".venv/Scripts/python.exe")

    def _compatible(self, task: dict) -> bool:
        if self.key is None:
            return False
        root, model, device = self.key
        options = task["options"]
        return (root == task["service_root"] and model == options["model"]
                and options["device"] in ("auto", self.model_info.get("device", device)))

    def _launch_pending(self):
        task, self._pending = self._pending, None
        if task is None:
            return
        if self._closing or task["cancelled"]:
            self._finish(task, dict(event="cancelled", message="已取消识别任务"))
            return
        self._active = task
        options, root = task["options"], Path(task["service_root"])
        self.key = (str(root), options["model"], options["device"])
        python = self._interpreter(root, options["model"])
        process = QProcess(self)
        self.process = process
        self._buffer, self._error_tail, self._stopping = b"", "", False
        process.setProgram(str(python))
        process.setArguments(["-B", "-u", str(Path(__file__).with_name("asr_worker.py"))])
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        environment = QProcessEnvironment.systemEnvironment()
        for name, value in dict(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1").items():
            environment.insert(name, value)
        if options["model"] == "qwen3-asr-1.7b":
            # Do not let the other runtime's CUDA/PyTorch DLL directories leak in.
            excluded = [os.path.normcase(str(root / ".venv")) + os.sep,
                        os.path.normcase(r"D:\dev\DouyinSubtitleService\.venv") + os.sep]
            entries = [entry for entry in environment.value("PATH").split(os.pathsep) if not any(
                os.path.normcase(os.path.abspath(entry)).startswith(prefix) for prefix in excluded)]
            environment.insert("PATH", str(python.parent) + os.pathsep + os.pathsep.join(entries))
        process.setProcessEnvironment(environment)
        process.started.connect(lambda p=process: self._started(p))
        process.readyReadStandardOutput.connect(lambda p=process: self._read(p))
        process.readyReadStandardError.connect(lambda p=process: self._read_error(p))
        process.finished.connect(lambda code, status, p=process: self._exited(p, code))
        process.errorOccurred.connect(lambda error, p=process: self._process_error(p, error))
        process.start()

    def _send(self, command):
        if self.process is not None and self.process.state() == QProcess.ProcessState.Running:
            self.process.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))

    def _send_request(self, task):
        self._error_tail = ""
        request = {key: value for key, value in task.items() if key not in ("cancelled", "offset_seconds", "job_id")}
        self._send(dict(request, command="transcribe"))

    def _started(self, process):
        if process is not self.process:
            return
        if self._active is not None:
            self._send_request(self._active)
            if self._active["cancelled"]:
                self._send(dict(command="cancel", request_id=self._active["request_id"]))
        if self._stopping:
            self._send(dict(command="shutdown"))
            process.closeWriteChannel()
        elif self._closing:
            self._stop_process()

    def _read_error(self, process):
        data = bytes(process.readAllStandardError())
        if process is self.process:
            self._error_tail = (self._error_tail + data.decode("utf-8", errors="replace"))[-4000:]

    def _read(self, process):
        if process is not self.process:
            return
        self._buffer += bytes(process.readAllStandardOutput())
        if len(self._buffer) > 16 * 1024 * 1024:
            self._error_tail = "识别进程输出超过协议上限"
            self._force_stop(process)
            return
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                payload = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            task = self._active
            if task is None or payload.get("request_id") != task["request_id"]:
                continue
            self._handle_event(task, payload)

    def _handle_event(self, task, payload):
        payload = dict(payload)
        payload.pop("request_id", None)
        payload["job_id"] = task["job_id"]
        kind = payload.get("event")
        if "detected_language" in payload:
            payload["detected_language"] = language_code(payload["detected_language"])
        if kind == "model":
            self.model_info = dict(payload)
            self.updated.emit(payload)
        elif kind == "state" and not task["cancelled"]:
            cues = payload.pop("new_cues", [])
            reset = payload.pop("reset_cues", False)
            self.updated.emit(payload)
            if cues or reset:
                self.updated.emit(dict(event="cues", job_id=task["job_id"],
                                     cues=shifted_cues(cues, task["offset_seconds"]), reset_cues=reset,
                                     detected_language=payload.get("detected_language", "")))
        elif kind in ("result", "error", "cancelled"):
            if task["cancelled"]:
                payload = dict(event="cancelled", message="已停止当前任务，模型继续保留")
            elif kind == "result":
                payload["cues"] = shifted_cues(payload.get("cues", []), task["offset_seconds"])
            self._active = None
            self._finish(task, payload)

    def _finish(self, task, payload):
        payload = dict(payload, job_id=task["job_id"])
        cleaner = _Recycler(Path(task["job_directory"]), self.runtime_dir, self)
        self._cleaners.add(cleaner)
        cleaner.problem.connect(lambda message, task_id=task["job_id"]: self.updated.emit(
            dict(event="state", job_id=task_id, status="cleanup_warning", message=message)))
        cleaner.finished.connect(lambda c=cleaner: self._cleaned(c))
        self.updated.emit(payload)
        cleaner.start()

    def _cleaned(self, cleaner):
        self._cleaners.discard(cleaner)
        cleaner.deleteLater()
        self._notify_ready()

    def _notify_ready(self):
        if self._closing:
            if self.process is None and not self.busy and not self._closed_emitted:
                self._closed_emitted = True
                self.closed.emit()
        elif not self.busy:
            self.idle.emit()

    def _process_error(self, process, error):
        if process is self.process and error == QProcess.ProcessError.FailedToStart:
            self._error_tail = "无法启动 SubtitleService：" + process.errorString()
            self._exited(process, 1)

    def _exited(self, process, code):
        if process is not self.process:
            return
        self._read(process)
        self._read_error(process)
        self.process, self.key, self.model_info, self._stopping = None, None, {}, False
        process.deleteLater()
        task, self._active = self._active, None
        if task is not None:
            terminal = (dict(event="cancelled", message="已停止识别") if task["cancelled"] or self._closing else
                        dict(event="error", message=f"识别进程结束（退出码 {code}）：{self._error_tail}"))
            self._finish(task, terminal)
        if self._pending is not None:
            QTimer.singleShot(0, self._launch_pending)
        self._notify_ready()

    def cancel(self):
        if self._pending is not None:
            task, self._pending = self._pending, None
            task["cancelled"] = True
            self._finish(task, dict(event="cancelled", message="已取消识别任务"))
        task = self._active
        if task is not None and not task["cancelled"]:
            task["cancelled"] = True
            self._send(dict(command="cancel", request_id=task["request_id"]))
            process = self.process
            QTimer.singleShot(60000, lambda: self._cancel_timeout(process, task))

    def _cancel_timeout(self, process, task):
        if self._active is task and task["cancelled"] and process is self.process:
            self.updated.emit(dict(event="state", job_id=task["job_id"], status="stopping",
                                 message="识别进程未响应停止，正在结束本应用的识别进程"))
            self._force_stop(process)

    def _stop_process(self):
        process = self.process
        if process is None or self._stopping:
            return
        self._stopping = True
        if process.state() == QProcess.ProcessState.Running:
            self._send(dict(command="shutdown"))
            process.closeWriteChannel()
        QTimer.singleShot(3500, lambda p=process: self._force_stop(p))

    def _force_stop(self, process):
        if process is self.process and process is not None and process.state() != QProcess.ProcessState.NotRunning:
            # QProcess keeps the owned process handle; never kill by a global PID/name.
            process.kill()

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.cancel()
        self._stop_process()
        self._notify_ready()
