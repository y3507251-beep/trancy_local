"""Offscreen integration regressions without a microphone, service or network.

Real MainWindow callbacks and Qt signals are exercised with controlled backends.
No test deletes files; the recycle boundary is checked with a mocked send2trash.
"""
from __future__ import annotations

from collections import deque
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from trancy_desktop import window as controller
from trancy_desktop.settings import DEFAULTS
from trancy_desktop.timeline import Timeline, browser_identity


# unittest discovery imports this module before running QCoreApplication tests.
# QApplication is also a QCoreApplication, making a shared suite safe.
_APP = QApplication.instance() or QApplication([])


class MemorySettings:
    def __init__(self):
        self.values = dict(DEFAULTS)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, **values):
        self.values.update(values)


class MemoryCache:
    def __init__(self, root):
        self.writes = []

    def read(self, *unused):
        return []

    def write(self, *args):
        self.writes.append(args)
        return True


class FakeOverlay(QObject):
    moved = pyqtSignal(int, int)

    def __init__(self, settings):
        super().__init__()
        self.last = ("", "")

    def show_caption(self, original, translation=""):
        self.last = (original, translation)

    def preview(self):
        self.last = ("preview", "preview")

    def set_locked(self, unused): pass
    def setVisible(self, unused): pass
    def set_enabled(self, unused): pass
    def apply_settings(self): pass
    def show(self): pass
    def hide(self): pass


class FakeASR(QObject):
    updated = pyqtSignal(dict)
    idle = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests = []
        self.cancel_count = 0
        self.behavior = "accepted"

    def submit(self, source, job_id, **options):
        self.requests.append((source, job_id, options))
        if self.behavior == "synchronous_error":
            self.updated.emit({"event": "error", "job_id": job_id, "message": "Invalid service directory"})
            return False
        return self.behavior != "busy"

    def cancel(self):
        self.cancel_count += 1

    def shutdown(self):
        self.closed.emit()


class FakeCapture(QObject):
    chunk_ready = pyqtSignal(str, float, float)
    level = pyqtSignal(float)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.started = []
        self.stop_count = 0

    @staticmethod
    def devices(): return []

    def start(self, directory, **options):
        if self.running:
            self.error.emit("系统声音捕获正在运行，请先停止")
            return
        self.running = True
        self.started.append(directory)

    def stop(self):
        self.stop_count += 1

    def finish_stop(self):
        self.running = False
        self.stopped.emit()


class FakeBridge(QObject):
    video_changed = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []

    def videos(self): return list(self.items)
    def start(self, **unused): pass
    def stop(self): pass


class FakeTranslator(QObject):
    translated = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    status = pyqtSignal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.jobs = []

    def reset(self, generation):
        self.jobs.clear()

    def enqueue(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    def shutdown(self): pass


class FakePot(QObject):
    state_changed = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = {"connected": False, "path": None, "position": None, "duration": None, "paused": None, "rate": None}

    def snapshot(self): return dict(self.state)
    def start(self): pass
    def stop(self): pass


class FakeDownloader(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str)
    idle = pyqtSignal()

    def __init__(self, parent=None, **unused):
        super().__init__(parent)
        self.running = False
        self.requests = []
        self.cancel_count = 0

    def download(self, video, output_dir):
        if self.running:
            self.failed.emit("已有网页音频下载任务，请等待或取消后重试")
            return
        self.running = True
        self.requests.append((dict(video), output_dir))

    def cancel(self):
        self.cancel_count += 1

    def finish_cancel(self):
        self.running = False
        self.failed.emit("网页音频下载已取消")
        self.idle.emit()


def video(identity="abcdefghijk", *, tab=1, ad=False):
    return {"platform": "youtube", "video_id": identity, "page": 1, "cid": "", "tab_id": tab,
            "url": "https://www.youtube.com/watch?v=" + identity, "title": identity,
            "position": 3, "duration": 30, "rate": 1, "paused": True, "ad_playing": ad}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.scheduled = deque()
        self.patches = [
            patch.object(controller, "ASRSession", FakeASR),
            patch.object(controller, "SystemAudioCapture", FakeCapture),
            patch.object(controller, "BrowserBridge", FakeBridge),
            patch.object(controller, "TranslationQueue", FakeTranslator),
            patch.object(controller, "PotPlayerAdapter", FakePot),
            patch.object(controller, "MediaDownloader", FakeDownloader),
            patch.object(controller, "SubtitleOverlay", FakeOverlay),
            patch.object(controller, "CaptionCache", MemoryCache),
            patch.object(controller.MainWindow, "_setup_tray", lambda owner: setattr(owner, "tray", MagicMock())),
            patch.object(QTimer, "singleShot", lambda delay, callback: self.scheduled.append((delay, callback))),
            patch.object(Path, "mkdir"),
        ]
        for item in self.patches:
            item.start()
        self.window = controller.MainWindow(MemorySettings())
        self.window.tick.stop()
        self.scheduled.clear()
        self.recycled = []
        self.window._recycle = lambda paths: self.recycled.extend(paths)

    def tearDown(self):
        self.window.tick.stop()
        self.window.hide()
        self.window.deleteLater()
        self.scheduled.clear()
        for item in reversed(self.patches):
            item.stop()

    def flush_scheduled(self, maximum=10):
        for unused in range(min(maximum, len(self.scheduled))):
            _, callback = self.scheduled.popleft()
            callback()

    def test_stale_asr_result_is_cleaned_without_rewriting_new_source(self):
        w = self.window
        w.generation, w.mode = 4, "potplayer"
        old_source = str(controller.TEMP / "live-old" / "chunk.wav")
        w._active = {"job_id": "old", "generation": 3, "source": old_source, "temporary": True}
        w._asr_event({"event": "result", "job_id": "old", "cues": [{"start": 0, "end": 2, "text": "old"}]})
        self.assertEqual(w.timeline.cues, [])
        self.assertIsNone(w._active)
        self.assertIn(old_source, self.recycled)

    def test_stale_translation_cannot_overwrite_new_source(self):
        w = self.window
        w.generation = 8
        w._translated({"generation": 7, "cues": [{"start": 0, "end": 2, "text": "stale", "translation": "过期"}]})
        self.assertEqual(w.timeline.cues, [])

    def test_unknown_potplayer_fields_hide_captions_without_exception(self):
        w = self.window
        w.mode = "potplayer"
        w.pot.state.update(connected=True)
        w.overlay.show_caption("old", "旧")
        w._displayed_key = "previous"
        w._pot_update(w.pot.snapshot())
        w._tick()
        self.assertEqual(w.overlay.last, ("", ""))

    def test_old_live_chunk_is_not_assigned_to_new_capture_session(self):
        w = self.window
        w.start_live()
        old = str(controller.TEMP / "live-previous-session" / "chunk.wav")
        w._chunk_ready(old, 20, 24)
        self.assertEqual(w.asr.requests, [], "An old-session WAV must not be transcribed in the new generation")
        self.assertIn(old, self.recycled)

    def test_live_restart_waits_for_old_capture_thread_to_finish(self):
        w = self.window
        w.start_live()
        first = w.capture.started[-1]
        w.start_live()
        w.capture.finish_stop()
        self.flush_scheduled()
        self.assertEqual(w.mode, "live")
        self.assertEqual(len(w.capture.started), 2)
        self.assertNotEqual(first, w.capture.started[-1])

    def test_synchronous_asr_error_is_terminal_instead_of_requeued(self):
        w = self.window
        w._new_source("potplayer", "test", "test")
        w.asr.behavior = "synchronous_error"
        source = str(controller.TEMP / "web-test" / "audio.wav")
        w._queue_file(source, temporary=True)
        self.assertIsNone(w._active)
        self.assertIsNone(w._pending_file, "A terminal validation error must not requeue a recycled source")
        self.assertEqual(len(w._chunks), 0)
        self.assertIn(source, self.recycled)

    def test_closing_still_cleans_matching_asr_terminal_input(self):
        w = self.window
        source = str(controller.TEMP / "live-close" / "chunk.wav")
        w._active = {"job_id": "closing", "generation": w.generation, "source": source, "temporary": True}
        w._closing = True
        w._asr_event({"event": "cancelled", "job_id": "closing"})
        self.assertIsNone(w._active)
        self.assertIn(source, self.recycled)

    def test_close_waits_for_capture_and_downloader_not_just_asr(self):
        w = self.window
        w._closing, w._backend_closed = True, True
        w._close_started = time.monotonic()
        w.capture.running = True
        w.downloader.running = True
        w._finish_close = MagicMock()
        w._tick()
        w._finish_close.assert_not_called()
        w.capture.running = w.downloader.running = False
        w._tick()
        w._finish_close.assert_called_once()

    def test_close_waits_for_queued_download_completion_after_thread_exit(self):
        w = self.window
        w._closing, w._backend_closed = True, True
        w._download_context = {"generation": w.generation - 1, "directory": str(controller.TEMP / "web-old")}
        w.downloader.running = False
        w._finish_close = MagicMock()
        w._tick()
        w._finish_close.assert_not_called()
        w._download_failed("cancelled")
        w._tick()
        w._finish_close.assert_called_once()

    def test_browser_switch_eventually_starts_new_video_after_old_cancel(self):
        w = self.window
        first, second = video("abcdefghijk"), video("lmnopqrstuv")
        w._start_browser_video(first)
        w._start_browser_video(second)
        w.downloader.finish_cancel()
        self.flush_scheduled()
        w._tick()
        self.assertEqual([item[0]["video_id"] for item in w.downloader.requests], ["abcdefghijk", "lmnopqrstuv"])

    def test_old_same_video_download_completion_cannot_consume_new_request(self):
        w = self.window
        current = video()
        w._start_browser_video(current)
        old_video, old_directory = w.downloader.requests[-1]
        # The old worker has ended, but its Qt completion signal is queued.
        w.downloader.running = False
        w._start_browser_video(current)
        old_path = str(Path(old_directory) / "media-old" / "audio.wav")
        w._downloaded(old_path, old_video)
        self.flush_scheduled()
        w._tick()
        self.assertEqual(len(w.downloader.requests), 2)
        self.assertEqual(w.asr.requests, [], "A late result from the same URL still belongs to its original generation")
        self.assertTrue(w.downloader.running, "The new download must remain active after the old result")
        self.assertIn(old_path, self.recycled)

    def test_active_asr_result_is_not_cached_under_new_model_selection(self):
        w = self.window
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), title="test-media")
        w.start_potplayer()
        task = dict(w._active)
        w.model.setCurrentIndex(w.model.findData("large-v3"))
        w._asr_event({"event": "result", "job_id": task["job_id"], "cues": [
            {"start": 0, "end": 1, "text": "hello", "translation": "你好"}
        ]})
        self.assertTrue(all(write[1] == "small" for write in w.cache.writes),
                        "Small-model output must not be stored under the newly selected large-v3 model")

    def test_browser_ad_hides_main_video_captions(self):
        w = self.window
        current = video(ad=True)
        w.mode, w.identity = "browser", browser_identity(current)
        w._last_video = dict(current, received_at=time.monotonic())
        w.timeline.add([{"start": 0, "end": 10, "text": "main-video", "translation": "正片字幕"}])
        w._displayed_key = "previous"
        w._tick()
        self.assertEqual(w.overlay.last, ("", ""))

    def test_other_browser_tab_does_not_change_active_timeline(self):
        w = self.window
        first, other = video(tab=1), video("lmnopqrstuv", tab=2)
        w.mode, w.identity = "browser", browser_identity(first)
        w._bound_tab = w._browser_key(first) if hasattr(w, "_browser_key") else 1
        w._last_video = dict(first, received_at=time.monotonic())
        w.bridge.items = [first, other]
        w._browser_update(other)
        self.assertEqual(w.identity, browser_identity(first))
        self.assertEqual(w._last_video["tab_id"], 1)

    def test_same_tab_number_from_other_browser_client_is_not_bound_source(self):
        w = self.window
        first, other = video(tab=1), video("lmnopqrstuv", tab=1)
        first.update(source_id="a" * 16, bridge_key="a" * 16 + ":1")
        other.update(source_id="b" * 16, bridge_key="b" * 16 + ":1")
        w.bridge.items = [first, other]
        w._start_browser_video(first)
        w._browser_update(other)
        self.assertEqual(w.identity, browser_identity(first))
        self.assertEqual(w._last_video["bridge_key"], first["bridge_key"])

    def test_closed_browser_source_does_not_start_invalid_followup_download(self):
        w = self.window
        current = video()
        current.update(source_id="a" * 16, bridge_key="a" * 16 + ":1")
        w._start_browser_video(current)
        w.bridge.items = []
        w._browser_update({"key": "youtube:abcdefghijk", "tab_id": 1,
                           "source_id": "a" * 16, "bridge_key": "a" * 16 + ":1", "closed": True})
        self.assertIsNone(w._last_video)
        self.assertEqual(w.mode, "idle")
        self.assertEqual(len(w.downloader.requests), 1)

    def test_recycle_rejects_paths_outside_owned_temp_root(self):
        w = self.window
        owned = str(controller.TEMP / "live-owned" / "chunk.wav")
        external = str(Path(__file__).resolve())
        class InlineThread:
            def __init__(self, target, **unused): self.target = target
            def start(self): self.target()
        with patch.object(Path, "exists", return_value=True), patch.object(controller.threading, "Thread", InlineThread), patch("send2trash.send2trash") as recycle:
            controller.MainWindow._recycle(w, [owned, external])
            recycle.assert_called_once_with([str(Path(owned).resolve())])

    def test_live_chunk_preserves_all_sentences_and_their_translations(self):
        w = self.window
        w._new_source("live", "live-test", "test")
        cues = [{"start": 4, "end": 5, "text": "Hello everyone.", "_job_id": "current"},
                {"start": 5, "end": 8, "text": "Welcome to the test.", "_job_id": "current"}]
        w.timeline.add([{"start": 0, "end": 3, "text": "Old chunk", "_job_id": "previous"}, *cues])
        w._refresh_live_caption()
        self.assertEqual(w._latest_live["text"], "Hello everyone. Welcome to the test.")
        w._translated({"generation": w.generation,
                       "cues": [{**cues[0], "translation": "大家好。"}, {**cues[1], "translation": "欢迎测试。"}]})
        self.assertEqual(w._latest_live["translation"], "大家好。 欢迎测试。")


class TimelineIntegrationTests(unittest.TestCase):
    def test_translation_updates_same_cue_in_place_without_duplicate(self):
        timeline = Timeline()
        original = {"start": 2, "end": 5, "text": "hello"}
        timeline.add([original])
        timeline.add([{**original, "translation": "你好"}])
        self.assertEqual(len(timeline.cues), 1)
        self.assertEqual(timeline.at(3)["translation"], "你好")
        self.assertIsNone(timeline.at(5))

    def test_caption_is_not_lost_behind_expired_overlapping_cue(self):
        timeline = Timeline()
        timeline.add([{"start": 0, "end": 300, "text": "long"}, {"start": 20, "end": 21, "text": "short"}])
        self.assertEqual(timeline.at(200)["text"], "long")

    def test_late_translation_does_not_resurrect_replaced_provisional_cue(self):
        timeline = Timeline()
        old = {"start": 0, "end": 2, "text": "hello", "_job_id": "job"}
        final = {"start": .5, "end": 2.5, "text": "hello", "_job_id": "job"}
        timeline.add([old])
        timeline.replace_job("job", [final])
        self.assertEqual(timeline.update_translations([{**old, "translation": "你好"}]), [])
        self.assertEqual(len(timeline.cues), 1)
        self.assertEqual(timeline.cues[0]["start"], .5)

    def test_final_cue_retains_completed_translation_when_boundaries_match(self):
        timeline = Timeline()
        cue = {"start": 0, "end": 2, "text": "hello", "_job_id": "job", "translation": "你好"}
        timeline.add([cue])
        added = timeline.replace_job("job", [{k: v for k, v in cue.items() if k != "translation"}])
        self.assertEqual(added, [])
        self.assertEqual(timeline.at(1)["translation"], "你好")


def live_integration(source: str) -> dict:
    """Opt-in real MainWindow -> PotPlayer -> SubtitleService -> Google test.

    The input must be a generated test WAV with at least four silent seconds at
    the beginning. Only a new, PID-verified PotPlayer instance is controlled.
    SystemAudioCapture.start is never called. Artifacts and the input are kept.
    """
    import ctypes
    from PyQt6.QtGui import QFont, QFontDatabase
    from trancy_desktop.potplayer import (
        _WindowsBackend, find_potplayer_executable, local_media_path,
        POT_GET_PLAY_STATUS, POT_SET_PLAY_STATUS, POT_GET_CURRENT_TIME,
        POT_GET_TOTAL_TIME,
    )
    from trancy_desktop.settings import Settings

    # The Windows offscreen Qt backend has no automatic font discovery. Load
    # genuine installed fonts for faithful test renders; production uses Qt's
    # normal Windows platform and discovers these itself.
    if not QFontDatabase.families():
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        _APP.setFont(QFont("Microsoft YaHei UI", 10))

    media = Path(source).resolve(strict=True)
    if not media.name.startswith("synthetic-desktop-") or media.suffix.lower() != ".wav":
        raise ValueError("Use a newly generated synthetic-desktop-*.wav fixture")
    artifact = media.parent
    executable = find_potplayer_executable()
    if not executable:
        raise RuntimeError("PotPlayer is not installed")
    process = subprocess.Popen([executable, str(media), "/new", "/autoplay"], close_fds=True)
    backend = _WindowsBackend()
    hwnd = None
    window = None
    report = {"source": str(media), "potplayer_pid": process.pid, "executable": executable,
              "system_audio_recorded": False, "completed": False}

    def spin(predicate, timeout, description):
        deadline = time.monotonic() + timeout
        next_report = time.monotonic() + 5
        while time.monotonic() < deadline:
            _APP.processEvents()
            if predicate():
                return
            if time.monotonic() >= next_report:
                state = window.status.text() if window is not None else "waiting"
                print(json.dumps({"stage": description, "status": state}, ensure_ascii=False), flush=True)
                next_report = time.monotonic() + 5
            time.sleep(.03)
        state = window.status.text() if window is not None else "waiting"
        raise TimeoutError(f"{description}: {state}")

    try:
        def paused_owned_window():
            nonlocal hwnd
            info = backend.find_window(preferred_pid=process.pid)
            if not info or info["pid"] != process.pid:
                return False
            hwnd = info["hwnd"]
            backend.query(hwnd, POT_SET_PLAY_STATUS, 1)
            return (local_media_path(backend.read_file(hwnd)) == str(media)
                    and (backend.query(hwnd, POT_GET_TOTAL_TIME) or 0) > 4000
                    and backend.query(hwnd, POT_GET_PLAY_STATUS) == 1)

        spin(paused_owned_window, 15, "pause generated fixture")
        pause_position = backend.query(hwnd, POT_GET_CURRENT_TIME)
        if pause_position is None or pause_position >= 4000:
            raise AssertionError(f"Fixture was not paused inside its silent prefix: {pause_position}")
        report["paused_at_ms"] = pause_position
        settings = Settings(artifact / "integration-settings.json")
        settings.update(model="small", device="cpu", translation_mode="google", overlay_locked=False)
        window = controller.MainWindow(settings)
        window.pot._preferred_pid = process.pid
        window.show()
        spin(lambda: window.pot.snapshot().get("pid") == process.pid and window.pot.snapshot().get("path") == str(media),
             10, "attach MainWindow to owned PotPlayer")
        events = []
        window.asr.updated.connect(lambda event: events.append({k: v for k, v in event.items() if k in
                                     ("event", "status", "message", "model", "device", "detected_language")}))
        window.start_potplayer()
        window.tabs.setCurrentIndex(1)
        spin(lambda: window._source_complete and len(window.timeline.cues) >= 2
             and all(cue.get("translation") for cue in window.timeline.cues), 300, "recognize and translate generated speech")
        report["asr_events"] = events
        report["cues"] = list(window.timeline.cues)
        report["snapshots"] = []
        for index, cue in enumerate((window.timeline.cues[0], window.timeline.cues[-1]), 1):
            target = (cue["start"] + cue["end"]) / 2
            if not window.pot.seek(target):
                raise AssertionError("PotPlayer rejected seek")
            spin(lambda: window.pot.snapshot().get("position") is not None
                 and abs(window.pot.snapshot()["position"] - target) < .4, 5, "seek subtitle " + str(index))
            spin(lambda: window.overlay.primary.text() == cue["text"] and window.overlay.secondary.text() == cue["translation"],
                 5, "synchronize bilingual overlay " + str(index))
            state = window.pot.snapshot()
            assert state["paused"] is True, state
            assert window.overlay.isVisible(), "Non-empty enabled subtitles should be visible"
            image_path = artifact / f"overlay-{index}.png"
            assert window.overlay.grab().save(str(image_path))
            report["snapshots"].append({"position": state["position"], "original": window.overlay.primary.text(),
                                        "translation": window.overlay.secondary.text(), "image": str(image_path)})
        screenshot = artifact / "settings-complete.png"
        assert window.grab().save(str(screenshot))
        report["settings_screenshot"] = str(screenshot)
        if window.timeline.at(1.0) is None:
            assert window.pot.seek(1.0)
            spin(lambda: window.pot.snapshot().get("position") is not None and abs(window.pot.snapshot()["position"] - 1) < .2,
                 5, "seek silent gap")
            spin(lambda: not window.overlay.isVisible(), 3, "hide empty overlay")
            report["silent_gap_overlay_hidden"] = True
        report["completed"] = True
        return report
    except Exception as error:
        report["error"] = str(error)
        if window is not None:
            report["last_status"] = window.status.text()
            report["cues"] = list(window.timeline.cues)
        raise
    finally:
        if window is not None:
            window.close()
            try:
                spin(lambda: window._can_close(), 15, "close owned desktop worker")
                window.close()
                report["desktop_closed"] = True
            except Exception as error:
                report["close_error"] = str(error)
        if hwnd:
            info = backend.find_window(preferred_pid=process.pid)
            if info and info["pid"] == process.pid and info["hwnd"] == hwnd:
                backend.user32.SendMessageTimeoutW(hwnd, 0x0010, 0, 0, 0x0002, 500, ctypes.byref(ctypes.c_size_t()))
        backend.close()
        (artifact / "integration-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if "--live-integration" in sys.argv:
        fixture = sys.argv[sys.argv.index("--live-integration") + 1]
        print(json.dumps(live_integration(fixture), ensure_ascii=False, indent=2), flush=True)
    else:
        unittest.main()
