"""PotPlayer-only Win32 adapter; all times exposed by this module are seconds.

Protocol source (consulted 2026-10-13):
https://github.com/ld3l/PotPlayerControl/blob/main/InternalSimpleCmd.h
POT_COMMAND is WM_USER. String replies use WM_COPYDATA, UTF-8, with
COPYDATASTRUCT.dwData equal to POT_GET_PLAYFILE_NAME. No title parsing, media
history, process-memory scanning, or player preference changes are used.

The installed player's CmdLine64.txt documents ``<file> /new``. A new instance
keeps files opened by this app separate from an already playing instance.
Older versions without the string/speed commands return unknown values. A user
can explicitly bind a local file using bind_file(); that is labelled as manual.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit
import uuid

from PyQt6.QtCore import QObject, pyqtSignal


WM_USER = 0x0400
WM_COPYDATA = 0x004A
POT_GET_TOTAL_TIME = 0x5002
POT_GET_CURRENT_TIME = 0x5004
POT_SET_CURRENT_TIME = 0x5005
POT_GET_PLAY_STATUS = 0x5006
POT_SET_PLAY_STATUS = 0x5007
POT_GET_SPEED = 0x5015
POT_GET_PLAYFILE_NAME = 0x6020
POT_CLASSES = frozenset({"PotPlayer", "PotPlayer64"})
POT_EXECUTABLES = frozenset(
    {"potplayermini.exe", "potplayermini64.exe", "potplayer.exe", "potplayer64.exe"}
)


def decode_file_reply(payload: bytes) -> str | None:
    """Reject malformed replies instead of silently mangling a media filename."""
    if len(payload) > 65536:
        return None
    try:
        decoded = payload.rstrip(b"\0").decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return None if "\0" in decoded else decoded


def local_media_path(raw_path: str | None) -> str | None:
    """Only return an existing absolute local path reported by the player/user."""
    if not raw_path:
        return None
    value = raw_path
    if value.lower().startswith("file://"):
        parsed = urlsplit(value)
        value = unquote(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            value = "//" + parsed.netloc + value
        elif len(value) > 2 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    if "://" in value or not os.path.isabs(value):
        return None
    try:
        path = Path(value)
        return str(path.resolve()) if path.is_file() else None
    except (OSError, ValueError):
        return None


def decoded_playback_values(
    position_ms: int | None,
    duration_ms: int | None,
    status: int | None,
    speed: int | None,
) -> dict[str, Any]:
    """Convert protocol units and preserve unsupported/failed values as None."""
    return {
        "position": position_ms / 1000 if position_ms is not None and position_ms >= 0 else None,
        "duration": duration_ms / 1000 if duration_ms is not None and duration_ms >= 0 else None,
        "paused": True if status in (-1, 0, 1) else False if status == 2 else None,
        "status": {-1: "stopped", 0: "stopped", 1: "paused", 2: "playing"}.get(status, "unknown"),
        "rate": speed / 1000 if speed is not None and 200 <= speed <= 12000 else None,
    }


def find_potplayer_executable(preferred: str | None = None) -> str | None:
    """Read App Paths, PATH and common locations; never write player settings."""
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    configured = os.environ.get("TRANCY_POTPLAYER_EXE")
    if configured:
        candidates.append(configured)
    if sys.platform == "win32":
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        key_name = "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name
                        with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ | view) as key:
                            candidates.append(str(winreg.QueryValueEx(key, "")[0]).strip().strip('"'))
                    except OSError:
                        pass
    for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for root in (r"D:\App", r"D:\dev", os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            for folder in ("PotPlayer", "DAUM\\PotPlayer"):
                for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
                    candidates.append(str(Path(root) / folder / name))
    for value in candidates:
        path = Path(value)
        if path.name.lower() in POT_EXECUTABLES and path.is_file():
            return str(path.resolve())
    return None


class _CopyDataStruct(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


class _WindowsBackend:
    """Created/closed on the polling thread which owns the reply window."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("PotPlayer 同步仅支持 Windows。")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self._enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._configure_functions()
        self._expected_hwnd = 0
        self._reply: str | None = None
        self._wndproc = self._wndproc_type(self._window_proc)

        class WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", self._wndproc_type),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        self._class_name = "TrancyPotPlayerReply_" + uuid.uuid4().hex
        self._instance = self.kernel32.GetModuleHandleW(None)
        wc = WndClass()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._instance
        wc.lpszClassName = self._class_name
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
        self.user32.RegisterClassW.restype = wintypes.WORD
        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._receiver = self.user32.CreateWindowExW(
            0, self._class_name, "", 0, 0, 0, 0, 0, ctypes.c_void_p(-3), None, self._instance, None
        )
        if not self._receiver:
            self.user32.UnregisterClassW(self._class_name, self._instance)
            raise ctypes.WinError(ctypes.get_last_error())

    def _configure_functions(self) -> None:
        signatures = {
            "SendMessageTimeoutW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
            "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "CreateWindowExW": ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            "DestroyWindow": ([wintypes.HWND], wintypes.BOOL),
            "UnregisterClassW": ([wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetForegroundWindow": ([], wintypes.HWND),
            "EnumWindows": ([self._enum_type, wintypes.LPARAM], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self.kernel32, name)
            function.argtypes, function.restype = args, result

    def _window_proc(self, hwnd: int, message: int, sender: int, data: int) -> int:
        if message == WM_COPYDATA and data and self._expected_hwnd:
            # Some versions use zero for WM_COPYDATA.wParam. Other windows are
            # rejected and data is accepted only during our synchronous request.
            if sender not in (0, self._expected_hwnd):
                return 0
            cds = ctypes.cast(data, ctypes.POINTER(_CopyDataStruct)).contents
            if cds.dwData == POT_GET_PLAYFILE_NAME and 0 <= cds.cbData <= 65536:
                if cds.cbData == 0:
                    self._reply = ""
                    return 1
                if cds.lpData:
                    self._reply = decode_file_reply(ctypes.string_at(cds.lpData, cds.cbData))
                    return int(self._reply is not None)
        return int(self.user32.DefWindowProcW(hwnd, message, sender, data))

    def query(self, hwnd: int, command: int, argument: int = 0) -> int | None:
        result = ctypes.c_size_t()
        # Do not use SMTO_BLOCK: PotPlayer must be able to send its synchronous
        # WM_COPYDATA response to this thread while SendMessageTimeout waits.
        ok = self.user32.SendMessageTimeoutW(hwnd, WM_USER, command, argument, 0x0002, 120, ctypes.byref(result))
        return ctypes.c_ssize_t(result.value).value if ok else None

    def read_file(self, hwnd: int) -> str | None:
        self._reply = None
        self._expected_hwnd = hwnd
        try:
            self.query(hwnd, POT_GET_PLAYFILE_NAME, int(self._receiver))
            self.pump_messages()
            return self._reply
        finally:
            self._expected_hwnd = 0

    def pump_messages(self) -> None:
        message = wintypes.MSG()
        while self.user32.PeekMessageW(ctypes.byref(message), self._receiver, 0, 0, 1):
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))

    def _process_executable(self, process_id: int) -> str | None:
        handle = self.kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            return buffer.value if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        finally:
            self.kernel32.CloseHandle(handle)

    def find_window(self, preferred_hwnd: int = 0, preferred_pid: int = 0) -> dict[str, Any] | None:
        found: list[dict[str, Any]] = []

        @self._enum_type
        def visit(hwnd: int, unused: int) -> bool:
            class_name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value not in POT_CLASSES:
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            executable = self._process_executable(pid.value)
            if not executable or Path(executable).name.lower() not in POT_EXECUTABLES:
                return True
            title = ctypes.create_unicode_buffer(2048)
            self.user32.GetWindowTextW(hwnd, title, len(title))
            found.append({"hwnd": int(hwnd), "pid": pid.value, "title": title.value, "executable": executable})
            return True

        self.user32.EnumWindows(visit, 0)
        if not found:
            return None
        for key, value in (("pid", preferred_pid), ("hwnd", preferred_hwnd), ("hwnd", self.user32.GetForegroundWindow())):
            if value:
                match = next((item for item in found if item[key] == value), None)
                if match:
                    return match
        return found[0]

    def close(self) -> None:
        if self._receiver:
            self.user32.DestroyWindow(self._receiver)
            self._receiver = None
            self.user32.UnregisterClassW(self._class_name, self._instance)


def _empty_state() -> dict[str, Any]:
    return {
        "connected": False, "player": "PotPlayer", "hwnd": None, "pid": None,
        "path": None, "title": "", "position": None, "duration": None,
        "paused": None, "rate": None, "status": "disconnected",
        "path_source": "unknown", "path_status": "unknown", "ipc_available": False,
        "updated_at": time.time(),
    }


class PotPlayerAdapter(QObject):
    """Poll in a daemon thread so a hung player never blocks the settings UI."""

    state_changed = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None, executable: str | None = None, *, backend_factory: Any = None) -> None:
        super().__init__(parent)
        self.executable = executable
        self._backend_factory = backend_factory or _WindowsBackend
        self._backend: Any = None
        self._lock = threading.RLock()
        self._state = _empty_state()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._preferred_pid = 0
        self._pending_seek: tuple[int, float] | None = None
        self._manual_binding: dict[str, Any] | None = None
        self._last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="TrancyPotPlayer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _publish(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = state
        self.state_changed.emit(dict(state))

    def _report_error(self, text: str) -> None:
        if text != self._last_error:
            self._last_error = text
            self.error.emit(text)

    def _run(self) -> None:
        try:
            self._backend = self._backend_factory()
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    self._poll_once()
                    self._last_error = ""
                except Exception as exc:
                    self._publish(_empty_state())
                    self._report_error(f"读取 PotPlayer 状态失败：{exc}")
                self._stop_event.wait(max(0.01, 0.25 - (time.monotonic() - started)))
        except Exception as exc:
            self._report_error(str(exc))
        finally:
            if self._backend is not None:
                self._backend.close()
                self._backend = None

    def _poll_once(self) -> dict[str, Any]:
        previous = self.snapshot()
        backend = self._backend
        window = backend.find_window(previous.get("hwnd") or 0, self._preferred_pid)
        if not window:
            with self._lock:
                self._manual_binding = None
            state = _empty_state()
            self._publish(state)
            return state
        hwnd = window["hwnd"]
        with self._lock:
            seek = self._pending_seek
            self._pending_seek = None
        if seek and seek[0] == hwnd:
            if backend.query(hwnd, POT_SET_CURRENT_TIME, round(seek[1] * 1000)) is None:
                self._report_error("PotPlayer 未响应跳转请求。")
        values = {
            command: backend.query(hwnd, command)
            for command in (POT_GET_CURRENT_TIME, POT_GET_TOTAL_TIME, POT_GET_PLAY_STATUS, POT_GET_SPEED)
        }
        raw_file = backend.read_file(hwnd)
        path = local_media_path(raw_file)
        state = _empty_state()
        state.update(window)
        state.update(decoded_playback_values(*(values[c] for c in (POT_GET_CURRENT_TIME, POT_GET_TOTAL_TIME, POT_GET_PLAY_STATUS, POT_GET_SPEED))))
        state.update({
            "connected": True, "path": path,
            "path_source": "potplayer_ipc" if path else "unknown",
            "path_status": "available" if path else "no_file" if raw_file == "" else "not_local" if raw_file is not None else "unknown",
            "ipc_available": any(value is not None for value in values.values()),
        })
        with self._lock:
            manual = self._manual_binding
            if manual:
                same_media = all(state.get(k) == manual.get(k) for k in ("hwnd", "pid", "title", "duration"))
                if not same_media or (raw_file is not None and (not path or os.path.normcase(path) != os.path.normcase(manual["path"]))):
                    self._manual_binding = None
                elif path is None:
                    state.update(path=manual["path"], path_source="user_selected", path_status="manual")
        self._publish(state)
        return state

    def bind_file(self, path: str) -> bool:
        """Explicit fallback for a user-selected file; invalidated on track change.

        This is a user assertion, never an automatic file identification. The UI
        must tell users to select the exact file currently playing in PotPlayer.
        """
        local = local_media_path(path)
        state = self.snapshot()
        if not local or not state.get("connected"):
            self._report_error("请先打开 PotPlayer，并选择它当前播放的本地文件。")
            return False
        if state.get("path_source") == "potplayer_ipc" and os.path.normcase(state["path"]) != os.path.normcase(local):
            self._report_error("所选文件与 PotPlayer 正在播放的文件不一致。")
            return False
        with self._lock:
            self._manual_binding = {key: state.get(key) for key in ("hwnd", "pid", "title", "duration")}
            self._manual_binding["path"] = local
        return True

    def open_file(self, path: str) -> bool:
        local = local_media_path(path)
        if not local:
            self._report_error("视频文件不存在，或不是本地文件。")
            return False
        executable = find_potplayer_executable(self.executable)
        if not executable:
            self._report_error("未找到 PotPlayer。请安装 PotPlayer 或设置 TRANCY_POTPLAYER_EXE。")
            return False
        try:
            process = subprocess.Popen([executable, local, "/new", "/autoplay"], close_fds=True)
        except OSError as exc:
            self._report_error(f"启动 PotPlayer 失败：{exc}")
            return False
        self._preferred_pid = process.pid
        self.start()
        return True

    def seek(self, seconds: float) -> bool:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return False
        state = self.snapshot()
        if not math.isfinite(value) or value < 0 or not state.get("connected") or not state.get("ipc_available"):
            return False
        duration = state.get("duration")
        if duration is not None and duration > 0:
            value = min(value, duration)
        with self._lock:
            self._pending_seek = (int(state["hwnd"]), value)
        return True
