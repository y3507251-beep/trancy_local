"""Keep one desktop instance and raise its window on repeated launches."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class DesktopInstance(QObject):
    activated = pyqtSignal()

    def __init__(self, name="TrancyLocal-Desktop-18766", parent=None):
        super().__init__(parent)
        self.name = name
        self._handle = None
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel.CreateMutexW.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)

    def acquire(self):
        # Windows named pipes alone permit multiple listeners with the same
        # name. The OS mutex reserves ownership before the bridge is created,
        # and is released automatically even if the process crashes.
        ctypes.set_last_error(0)
        handle = self._kernel.CreateMutexW(None, False, "Local\\" + self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._kernel.CloseHandle(handle)
            self._activate_existing()
            return False
        self._handle = handle
        if not self._server.listen(self.name):
            message = self._server.errorString()
            self.close()
            raise RuntimeError("无法建立桌面窗口连接：" + message)
        return True

    def _activate_existing(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            connection = QLocalSocket()
            connection.connectToServer(self.name)
            if connection.waitForConnected(200):
                connection.write(b"activate\n")
                connection.flush()
                if connection.waitForReadyRead(3000) and bytes(connection.readAll()) == b"ok\n":
                    return
                break
            connection.abort()
            time.sleep(.05)
        raise RuntimeError("桌面字幕已经启动，但窗口暂未响应。请等待现有窗口恢复后再试。")

    def _accept(self):
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            connection.disconnected.connect(connection.deleteLater)
            connection.readyRead.connect(lambda c=connection: self._read(c))
            self._read(connection)

    def _read(self, connection):
        if connection.bytesAvailable() > 32:
            connection.abort()
        elif connection.canReadLine():
            if bytes(connection.readLine(32)) == b"activate\n":
                self.activated.emit()
                connection.write(b"ok\n")
            connection.disconnectFromServer()

    def close(self):
        self._server.close()
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
