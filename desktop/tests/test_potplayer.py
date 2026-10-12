"""Protocol and state tests; run with python -B -m unittest discover -s tests.

The explicit --live-smoke mode starts installed PotPlayer on a silent generated
WAV, verifies its native messages, and closes only that new test window. The
test WAV is retained in the Windows temporary directory (no permanent delete).
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trancy_desktop.potplayer import (
    POT_GET_CURRENT_TIME, POT_GET_TOTAL_TIME, POT_GET_PLAY_STATUS, POT_GET_SPEED,
    POT_SET_CURRENT_TIME, POT_SET_PLAY_STATUS, PotPlayerAdapter, _WindowsBackend,
    decode_file_reply, decoded_playback_values, find_potplayer_executable,
    local_media_path,
)


class FakeBackend:
    def __init__(self):
        self.window = {"hwnd": 12, "pid": 34, "title": "current.mp4 - PotPlayer"}
        self.values = {POT_GET_CURRENT_TIME: 3250, POT_GET_TOTAL_TIME: 100000, POT_GET_PLAY_STATUS: 2, POT_GET_SPEED: 1500}
        self.file = None
        self.sent = []

    def find_window(self, hwnd=0, pid=0):
        return self.window

    def query(self, hwnd, command, argument=0):
        self.sent.append((hwnd, command, argument))
        return self.values.get(command)

    def read_file(self, hwnd):
        return self.file

    def close(self):
        pass


class ProtocolTests(unittest.TestCase):
    def test_utf8_reply_preserves_chinese_and_rejects_corrupt_data(self):
        self.assertEqual(decode_file_reply("D:\\视频\\讲课.mp4\0".encode("utf-8")), "D:\\视频\\讲课.mp4")
        self.assertIsNone(decode_file_reply(b"\xff"))
        self.assertIsNone(decode_file_reply(b"valid\0hidden.mp4"))
        self.assertIsNone(decode_file_reply(b"a" * 65537))
        self.assertEqual(decode_file_reply(b""), "")

    def test_native_units_and_state_mapping(self):
        data = decoded_playback_values(4321, 60000, 1, 1250)
        self.assertEqual(data, {"position": 4.321, "duration": 60, "paused": True, "status": "paused", "rate": 1.25})
        self.assertFalse(decoded_playback_values(0, 100, 2, 1000)["paused"])
        self.assertEqual(decoded_playback_values(0, 0, -1, 0)["status"], "stopped")

    def test_failures_and_unsupported_commands_stay_unknown(self):
        data = decoded_playback_values(None, -1, 9, 0)
        self.assertIsNone(data["position"])
        self.assertIsNone(data["duration"])
        self.assertIsNone(data["paused"])
        self.assertIsNone(data["rate"])

    def test_real_paths_only(self):
        own_file = str(Path(__file__).resolve())
        self.assertEqual(local_media_path(own_file), own_file)
        self.assertEqual(local_media_path(Path(own_file).as_uri()), own_file)
        self.assertIsNone(local_media_path("https://example.com/video.mp4"))
        self.assertIsNone(local_media_path("invented-file.mp4"))
        self.assertIsNone(local_media_path(str(Path(own_file).parent / "not-present")))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.adapter = PotPlayerAdapter(backend_factory=lambda: self.backend)
        self.adapter._backend = self.backend

    def test_ipc_identifies_file_and_title_is_not_used_as_path(self):
        state = self.adapter._poll_once()
        self.assertTrue(state["connected"])
        self.assertIsNone(state["path"])
        self.assertEqual(state["position"], 3.25)
        self.backend.file = str(Path(__file__).resolve())
        state = self.adapter._poll_once()
        self.assertEqual(state["path"], self.backend.file)
        self.assertEqual(state["path_source"], "potplayer_ipc")

    def test_switching_file_replaces_identity_without_stale_values(self):
        first = str(Path(__file__).resolve())
        second = str(Path(sys.modules[PotPlayerAdapter.__module__].__file__).resolve())
        self.backend.file = first
        self.adapter._poll_once()
        self.backend.file = second
        self.assertEqual(self.adapter._poll_once()["path"], second)
        self.backend.file = None
        self.assertIsNone(self.adapter._poll_once()["path"])

    def test_manual_binding_is_labelled_and_cleared_on_media_change(self):
        self.adapter._poll_once()
        self.assertTrue(self.adapter.bind_file(str(Path(__file__).resolve())))
        state = self.adapter._poll_once()
        self.assertEqual(state["path_source"], "user_selected")
        self.backend.window["title"] = "another.mp4 - PotPlayer"
        self.assertIsNone(self.adapter._poll_once()["path"])

    def test_manual_binding_cannot_override_known_player_file(self):
        self.backend.file = str(Path(__file__).resolve())
        self.adapter._poll_once()
        other = str(Path(sys.modules[PotPlayerAdapter.__module__].__file__).resolve())
        self.assertFalse(self.adapter.bind_file(other))

    def test_disconnect_clears_media_and_timing(self):
        self.backend.file = str(Path(__file__).resolve())
        self.adapter._poll_once()
        self.backend.window = None
        state = self.adapter._poll_once()
        self.assertFalse(state["connected"])
        self.assertIsNone(state["path"])
        self.assertIsNone(state["position"])

    def test_snapshot_is_a_copy(self):
        self.adapter._poll_once()
        state = self.adapter.snapshot()
        state["position"] = 999
        self.assertEqual(self.adapter.snapshot()["position"], 3.25)

    def test_seek_uses_milliseconds_and_drops_request_after_player_switch(self):
        self.adapter._poll_once()
        self.assertTrue(self.adapter.seek(5.5))
        self.adapter._poll_once()
        self.assertIn((12, POT_SET_CURRENT_TIME, 5500), self.backend.sent)
        self.assertFalse(self.adapter.seek(float("nan")))
        self.assertFalse(self.adapter.seek(-1))
        self.assertTrue(self.adapter.seek(10))
        self.backend.window["hwnd"] = 99
        self.adapter._poll_once()
        self.assertNotIn((99, POT_SET_CURRENT_TIME, 10000), self.backend.sent)

    def test_launch_does_not_publish_requested_path_as_verified(self):
        path = str(Path(__file__).resolve())
        with patch("trancy_desktop.potplayer.find_potplayer_executable", return_value="PotPlayerMini64.exe"), patch("trancy_desktop.potplayer.subprocess.Popen") as popen, patch.object(self.adapter, "start"):
            popen.return_value.pid = 987
            self.assertTrue(self.adapter.open_file(path))
            popen.assert_called_once_with(["PotPlayerMini64.exe", path, "/new", "/autoplay"], close_fds=True)
            self.assertIsNone(self.adapter.snapshot()["path"])


def live_smoke() -> dict:
    """Opt-in validation against the actual installed player, never a mock."""
    executable = find_potplayer_executable()
    if not executable:
        raise RuntimeError("PotPlayer not installed")
    path = Path(tempfile.gettempdir()) / ("trancy_potplayer_测试_" + uuid.uuid4().hex + ".wav")
    with wave.open(str(path), "wb") as media:
        media.setnchannels(1)
        media.setsampwidth(2)
        media.setframerate(16000)
        media.writeframes(b"\0\0" * 16000 * 30)
    process = subprocess.Popen([executable, str(path), "/new", "/autoplay"], close_fds=True)
    backend = _WindowsBackend()
    hwnd = None
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            info = backend.find_window(preferred_pid=process.pid)
            if info and info["pid"] == process.pid:
                raw_file = backend.read_file(info["hwnd"])
                if local_media_path(raw_file) == str(path.resolve()):
                    hwnd = info["hwnd"]
                    if (backend.query(hwnd, POT_GET_TOTAL_TIME) or 0) > 29000 and backend.query(hwnd, POT_GET_PLAY_STATUS) == 2:
                        break
            time.sleep(0.1)
        if not hwnd:
            raise RuntimeError("No verified new PotPlayer window/current-file response")
        duration = backend.query(hwnd, POT_GET_TOTAL_TIME)
        rate = backend.query(hwnd, POT_GET_SPEED)
        position1 = backend.query(hwnd, POT_GET_CURRENT_TIME)
        position2 = position1
        progress_deadline = time.monotonic() + 5
        while time.monotonic() < progress_deadline and position2 == position1:
            time.sleep(0.2)
            position2 = backend.query(hwnd, POT_GET_CURRENT_TIME)
        backend.query(hwnd, POT_SET_PLAY_STATUS, 1)
        time.sleep(0.15)
        paused_status = backend.query(hwnd, POT_GET_PLAY_STATUS)
        seek_result = backend.query(hwnd, POT_SET_CURRENT_TIME, 12500)
        seek_position = backend.query(hwnd, POT_GET_CURRENT_TIME)
        seek_deadline = time.monotonic() + 3
        while time.monotonic() < seek_deadline and (seek_position is None or abs(seek_position - 12500) >= 500):
            time.sleep(0.2)
            seek_position = backend.query(hwnd, POT_GET_CURRENT_TIME)
        result = {"executable": executable, "retained_test_file": str(path), "pid": process.pid, "hwnd": hwnd, "file_reply": raw_file, "duration_ms": duration, "speed_per_mille": rate, "position_before_ms": position1, "position_after_ms": position2, "paused_status": paused_status, "seek_result": seek_result, "seek_position_ms": seek_position}
        assert duration is not None and 29500 <= duration <= 30500, result
        assert rate is not None and 200 <= rate <= 12000, result
        assert position1 is not None and position2 is not None and position2 > position1, result
        assert paused_status == 1, result
        assert seek_position is not None and abs(seek_position - 12500) < 500, result
        adapter = PotPlayerAdapter()
        adapter._preferred_pid = process.pid
        adapter.start()
        try:
            adapter_deadline = time.monotonic() + 3
            state = adapter.snapshot()
            while time.monotonic() < adapter_deadline and state.get("path") != str(path.resolve()):
                time.sleep(0.1)
                state = adapter.snapshot()
            assert state["connected"] and state["path"] == str(path.resolve()), state
            assert state["paused"] is True and state["position"] == 12.5, state
            result["adapter_snapshot"] = state
        finally:
            adapter.stop()
        return result
    finally:
        if hwnd:
            backend.user32.SendMessageTimeoutW(hwnd, 0x0010, 0, 0, 0x0002, 500, ctypes.byref(ctypes.c_size_t()))
        backend.close()


if __name__ == "__main__":
    if "--live-smoke" in sys.argv:
        print(json.dumps(live_smoke(), ensure_ascii=False, indent=2))
    else:
        unittest.main()
