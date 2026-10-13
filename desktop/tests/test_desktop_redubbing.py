"""Real desktop callbacks for optional whole-video dubbing; no model or network."""
from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_desktop_controller as fixtures
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from trancy_desktop import dubbing, prepared_dubbing, redub_preparation
from trancy_desktop.timeline import browser_identity


CUES = [dict(start=0, end=4, text="你好世界", source_language="zh-CN",
             translation="Hello world", target_language="en")]


def bili_video(*, cid="12345", protocol=1):
    value = dict(platform="bilibili", video_id="BV1xx411c7mD", page=1, cid=cid,
                 url="https://www.bilibili.com/video/BV1xx411c7mD/", tab_id=1,
                 bridge_key="fixture:1", document_start=100, dubbing_protocol=1,
                 rate_control_protocol=protocol, position=1, duration=4, paused=True, rate=1)
    value["key"] = browser_identity(value)
    return value


class FakePreparation(QObject):
    progress = pyqtSignal(str)
    captions_ready = pyqtSignal(dict)
    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)
    closed = pyqtSignal()
    instances = []
    cached_on_start = False

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.starts = []
        self.shutdown_called = False
        self.running = False
        self.is_closed = False
        self._closing = False
        self.instances.append(self)

    def start(self, generation, video, cues, **options):
        self.starts.append((generation, dict(video), list(cues), options))
        self.running = not self.cached_on_start
        if self.cached_on_start:
            self.captions_ready.emit(dict(generation=generation, cues=CUES))
            self.complete(cached=True)

    def shutdown(self):
        self.shutdown_called = self._closing = True

    def complete(self, **changes):
        generation, video, _, _ = self.starts[-1]
        event = dict(generation=generation, identity=browser_identity(video),
                     manifest="C:/fixture/prepared/manifest.json", cached=False)
        self.ready.emit(dict(event, **changes))

    def finish_close(self):
        self.running = False
        self.is_closed = True
        self.closed.emit()


class FakeBasic(fixtures.FakeDubbing):
    instances = []
    kind = "basic"

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)
        self.bridge = bridge
        self.instances.append(self)

    def set_enabled(self, enabled):
        super().set_enabled(enabled)
        if not enabled:
            self.stop()

    def stop(self):
        super().stop()
        self.bridge.lifecycle.append("restore_" + self.kind)

    def shutdown(self):
        super().shutdown()
        self.stop()

    def finish_close(self):
        self.is_closed = True
        self.closed.emit()


class FakePrepared(FakeBasic):
    instances = []
    kind = "prepared"

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)
        self.manifests = []

    def set_manifest(self, path):
        self.manifests.append(path)


class DesktopRedubbingTests(unittest.TestCase):
    def setUp(self):
        FakePreparation.instances, FakeBasic.instances, FakePrepared.instances = [], [], []
        FakePreparation.cached_on_start = False
        self.redub_patches = [patch.object(redub_preparation, "RedubPreparation", FakePreparation),
                             patch.object(prepared_dubbing, "PreparedDubbingSession", FakePrepared),
                             patch.object(dubbing, "DubbingSession", FakeBasic)]
        for item in self.redub_patches: item.start()
        fixtures.ControllerTests.setUp(self)
        self.window.bridge.lifecycle = []
        original_stop = self.window.bridge.stop
        def stop_bridge():
            self.window.bridge.lifecycle.append("bridge_stopped")
            original_stop()
        self.window.bridge.stop = stop_bridge

    def tearDown(self):
        for item in FakePreparation.instances:
            if not item.is_closed: item.finish_close()
        fixtures.ControllerTests.tearDown(self)
        for item in reversed(self.redub_patches): item.stop()

    def bind(self, video=None, captions=None):
        current = video or bili_video()
        self.window.bridge.items = [current]
        self.window.bridge.caption_result = captions if captions is not None else dict(status="ready", cues=CUES)
        self.window._start_browser_video(current)
        return current

    def prepare(self, *, enable=True):
        self.bind()
        if enable:
            self.window.prepared_dubbing.setChecked(True)
        else:
            self.window._prepare_redub()
        return FakePreparation.instances[-1]

    def test_startup_keeps_both_modes_off_and_creates_no_preparation_or_audio_backend(self):
        w = self.window
        self.assertFalse(w.english_dubbing.isChecked())
        self.assertFalse(w.prepared_dubbing.isChecked())
        self.assertIsNone(w._dubbing)
        self.assertIsNone(w._prepared_dubbing)
        self.assertIsNone(w._redub_preparation)
        self.assertEqual(FakePreparation.instances + FakeBasic.instances + FakePrepared.instances, [])
        self.assertEqual(w.asr.requests, [])

    def test_basic_and_prepared_audio_modes_are_mutually_exclusive(self):
        w = self.window
        self.bind()
        w._redub_manifest = "C:/fixture/manifest.json"
        w.english_dubbing.setChecked(True)
        basic = w._dubbing
        w.prepared_dubbing.setChecked(True)
        self.assertFalse(w.english_dubbing.isChecked())
        self.assertFalse(basic.enabled)
        self.assertTrue(w._prepared_dubbing.enabled)
        w.english_dubbing.setChecked(True)
        self.assertFalse(w.prepared_dubbing.isChecked())
        self.assertFalse(w._prepared_dubbing.enabled)
        self.assertTrue(basic.enabled)

    def test_switching_to_basic_cancels_running_preparation_and_ignores_its_late_ready(self):
        worker = self.prepare()
        self.window.english_dubbing.setChecked(True)
        self.assertTrue(worker.shutdown_called)
        worker.complete()
        self.assertIsNone(self.window._redub_manifest)
        self.assertIsNone(self.window._prepared_dubbing)
        self.assertTrue(self.window._dubbing.enabled)

    def test_bilibili_and_rate_capability_are_required_before_preparation(self):
        for current in (fixtures.video(), bili_video(protocol=0)):
            self.bind(current)
            self.window.prepared_dubbing.setChecked(True)
            self.assertFalse(self.window.prepared_dubbing.isChecked())
            self.assertEqual(FakePreparation.instances, [])
        self.bind(bili_video())
        self.window.prepared_dubbing.setChecked(True)
        self.assertEqual(len(FakePreparation.instances), 1)

    def test_complete_native_track_passes_full_corpus_and_current_translation_engine(self):
        w = self.window
        self.bind()
        w.engine.setCurrentIndex(w.engine.findData("plugin"))
        w.prepared_dubbing.setChecked(True)
        generation, video, cues, options = FakePreparation.instances[0].starts[0]
        self.assertEqual(generation, w.generation)
        self.assertEqual(cues, CUES)
        self.assertTrue(options["complete"])
        self.assertEqual(options["mode"], "plugin")
        self.assertEqual(w.asr.requests, [])
        self.assertIsNone(w._prepared_dubbing)

    def test_pending_failed_or_unconfirmed_native_track_never_starts_full_asr_preparation(self):
        w = self.window
        cases = [None, dict(status="error", cues=[], native_track_available=True),
                 dict(status="unavailable", cues=[])]
        for result in cases:
            w.bridge.items = [bili_video()]
            w.bridge.caption_result = result
            w._start_browser_video(bili_video())
            w.prepared_dubbing.setChecked(True)
            self.assertEqual(FakePreparation.instances, [])
            self.assertEqual(w.asr.requests, [])
            self.assertFalse(w.prepared_dubbing.isChecked())

    def test_confirmed_absent_native_track_permits_whole_video_preparation(self):
        w = self.window
        self.bind(captions=dict(status="unavailable", native_track_available=False, cues=[]))
        previous_asr = w.asr
        w.prepared_dubbing.setChecked(True)
        _, _, cues, options = FakePreparation.instances[-1].starts[0]
        self.assertEqual(cues, [])
        self.assertFalse(options["complete"])
        self.assertIsNot(w.asr, previous_asr)
        self.assertEqual(w.asr.requests, [])
        self.assertTrue(w._recognition_blocked)

    def test_superseded_short_window_asr_cannot_replace_prepared_full_corpus(self):
        w = self.window
        self.bind(captions=dict(status="unavailable", native_track_available=False, cues=[]))
        w._source_kind = "asr"
        w._active = dict(job_id="old-window", generation=w.generation, source="C:/fixture/window.wav",
                         window=(0, 4), temporary=False)
        w._pending_file = dict(source="C:/fixture/pending.wav", window=(2, 4))
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        worker.captions_ready.emit(dict(generation=w.generation, cues=CUES))
        label = w.model_info.text()
        self.assertIsNone(w._pending_file)
        w._asr_event(dict(event="model", job_id="old-window", model="old model", device="cpu"))
        w._asr_event(dict(event="cues", job_id="old-window", cues=[dict(start=0, end=2, text="Old window")]))
        w._asr_event(dict(event="result", job_id="old-window", cues=[dict(start=0, end=2, text="Old final window")]))
        self.assertEqual(w.timeline.cues, CUES)
        self.assertEqual(w.model_info.text(), label)
        self.assertIsNone(w._active)

    def bind_missing_track_with_downloaded_audio(self):
        w = self.window
        current = dict(bili_video(), duration=120)
        self.bind(current, captions=dict(status="unavailable", native_track_available=False, cues=[]))
        w._media_source = dict(source="C:/fixture/whole-source.wav", generation=w.generation, temporary=False)
        w._buffer.set_duration(120)
        return current

    def test_seek_during_preparation_and_cancellation_does_not_restart_short_window_asr(self):
        w = self.window
        self.bind_missing_track_with_downloaded_audio()
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        w._last_playback = (1, time.monotonic(), True, 1)
        w._sync_playback(40, False, 1)
        self.assertTrue(w._recognition_blocked)
        self.assertTrue(w.translator.positions[-1][1]["seek"])
        self.assertEqual(w.asr.requests, [])
        self.assertIsNone(w._pending_file)
        w._prepare_redub()
        self.assertTrue(worker.running)
        w._sync_playback(80, False, 1)
        self.assertTrue(w._recognition_blocked)
        self.assertEqual(w.asr.requests, [])
        worker.finish_close()
        w._sync_playback(90, False, 1)
        self.assertFalse(w._recognition_blocked)
        self.assertEqual(len(w.asr.requests), 1)

    def test_queued_scheduler_and_retry_cannot_bypass_full_video_preparation_owner(self):
        w = self.window
        self.bind_missing_track_with_downloaded_audio()
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        w.retry_translation()
        self.assertEqual(w.asr.requests, [])
        self.assertIsNone(w._pending_file)
        pending = dict(source="C:/fixture/queued-source.wav", generation=w.generation,
                       offset=0, window=(0, 12), temporary=False, options=dict(w._source_config))
        w._pending_file = pending
        w._schedule()
        self.assertEqual(w.asr.requests, [])
        self.assertIs(w._pending_file, pending)
        w._prepare_redub()
        w._schedule()
        self.assertEqual(w.asr.requests, [])
        worker.finish_close()
        w._schedule()
        self.assertEqual(len(w.asr.requests), 1)

    def test_ordinary_seek_without_preparation_still_retries_short_window_recognition(self):
        w = self.window
        self.bind_missing_track_with_downloaded_audio()
        w._recognition_blocked = True
        w._last_playback = (1, time.monotonic(), True, 1)
        w._sync_playback(40, False, 1)
        self.assertFalse(w._recognition_blocked)
        self.assertEqual(len(w.asr.requests), 1)

    def hold_resident_asr(self):
        previous = self.window.asr
        previous.process = object()
        previous.shutdown = MagicMock()
        return previous

    def test_whole_preparation_waits_for_resident_model_exit_and_keeps_fresh_asr_lazy(self):
        w = self.window
        self.bind()
        previous = self.hold_resident_asr()
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        previous.shutdown.assert_called_once_with()
        self.assertIsNot(w.asr, previous)
        self.assertIsNone(getattr(w.asr, "process", None))
        self.assertEqual(w.asr.requests, [])
        self.assertEqual(worker.starts, [])
        self.assertIn(previous, w._retiring_asr)
        self.assertIn("正在释放", w.model_info.text())
        previous.process = None
        previous.closed.emit()
        self.assertEqual(len(worker.starts), 1)
        self.assertFalse(w._backend_closed, "The old closed event cannot close the fresh ASR coordinator")
        self.assertIn("已释放", w.model_info.text())
        previous.closed.emit()
        self.assertEqual(len(worker.starts), 1)

    def test_cancel_while_resident_model_is_exiting_never_starts_queued_preparation(self):
        w = self.window
        self.bind()
        previous = self.hold_resident_asr()
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        w.prepared_dubbing.setChecked(False)
        worker.finish_close()
        self.assertTrue(worker.shutdown_called)
        previous.process = None
        previous.closed.emit()
        self.assertEqual(worker.starts, [])
        self.assertIsNone(w._redub_start_context)
        self.assertEqual(w.asr.requests, [])
        self.assertIsNone(w._prepared_dubbing)

    def test_video_switch_while_asr_is_exiting_does_not_start_old_preparation(self):
        w = self.window
        self.bind()
        previous = self.hold_resident_asr()
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        self.bind(bili_video(cid="99999"))
        previous.process = None
        previous.closed.emit()
        self.assertEqual(worker.starts, [])
        self.assertTrue(worker.shutdown_called)
        self.assertEqual(w.timeline.cues, CUES)
        self.assertIsNone(w._redub_manifest)

    def test_restarted_preparation_waits_for_all_retired_asr_sessions(self):
        w = self.window
        self.bind()
        previous = self.hold_resident_asr()
        w.prepared_dubbing.setChecked(True)
        first = FakePreparation.instances[-1]
        w.prepared_dubbing.setChecked(False)
        first.finish_close()
        w.prepared_dubbing.setChecked(True)
        second = FakePreparation.instances[-1]
        self.assertIsNot(first, second)
        self.assertEqual(first.starts + second.starts, [])
        self.assertIn(previous, w._retiring_asr)
        previous.process = None
        previous.closed.emit()
        self.assertEqual(first.starts, [])
        self.assertEqual(len(second.starts), 1)
        self.assertEqual(w._retiring_asr, set())

    def test_close_waits_for_retired_model_and_keeps_its_input_until_exit(self):
        w = self.window
        self.bind()
        previous = self.hold_resident_asr()
        source = str(fixtures.controller.TEMP / "redub-retiring-input.wav")
        w._active = dict(job_id="retiring", generation=w.generation, source=source,
                         window=(0, 4), temporary=True)
        w.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        w._finish_close = MagicMock()
        w.closeEvent(QCloseEvent())
        worker.finish_close()
        self.assertTrue(w._backend_closed)
        self.assertFalse(w._can_close())
        self.assertEqual(w.bridge.stop_count, 0)
        self.assertNotIn(source, self.recycled)
        self.assertEqual(worker.starts, [])
        previous.process = None
        previous.closed.emit()
        self.assertIn(source, self.recycled)
        self.assertIsNone(w._active)
        self.assertTrue(w._can_close())
        self.assertEqual(w.bridge.stop_count, 1)
        w._finish_close.assert_called_once_with()

    def test_cancellation_does_not_wait_for_threads_and_late_ready_cannot_enable_playback(self):
        worker = self.prepare()
        w = self.window
        before = list(w.timeline.cues)
        began = time.perf_counter()
        w._prepare_redub()  # The same button now means cancel.
        self.assertLess(time.perf_counter() - began, .25)
        self.assertTrue(worker.shutdown_called)
        self.assertTrue(worker.running)
        worker.captions_ready.emit(dict(generation=w.generation, cues=[dict(start=0, end=2, text="Late caption")]))
        worker.progress.emit("late progress")
        worker.complete()
        self.assertEqual(w.timeline.cues, before)
        self.assertIsNone(w._redub_manifest)
        self.assertIsNone(w._prepared_dubbing)
        self.assertIn("取消", w.redub_status.text())

    def test_failed_unchecked_preparation_cannot_install_queued_manifest(self):
        worker = self.prepare(enable=False)
        worker.failed.emit("fixture preparation failed")
        self.assertTrue(worker.shutdown_called)
        worker.complete()
        self.assertIsNone(self.window._redub_manifest)
        self.assertIsNone(self.window._prepared_dubbing)

    def test_video_change_rejects_old_ready_and_captions_without_unblocking_new_caption_read(self):
        worker = self.prepare()
        old_generation = worker.starts[0][0]
        w = self.window
        w.bridge.caption_result = None
        current = bili_video(cid="99999")
        w.bridge.items = [current]
        w._start_browser_video(current)
        self.assertTrue(w._recognition_blocked)
        worker.captions_ready.emit(dict(generation=old_generation, cues=[dict(start=0, end=2, text="Old video")]))
        worker.complete()
        self.assertEqual(w.timeline.cues, [])
        self.assertIsNone(w._redub_manifest)
        worker.finish_close()
        self.assertTrue(w._recognition_blocked)

    def test_old_same_generation_preparation_cannot_replace_restarted_preparation(self):
        old = self.prepare(enable=False)
        w = self.window
        w._prepare_redub()
        old.finish_close()
        w._prepare_redub()
        current = FakePreparation.instances[-1]
        self.assertIsNot(current, old)
        before = list(w.timeline.cues)
        old.captions_ready.emit(dict(generation=w.generation, cues=[dict(start=0, end=2, text="Old preparation")]))
        old.complete()
        self.assertEqual(w.timeline.cues, before)
        self.assertIsNone(w._redub_manifest)

    def test_completed_or_cached_mix_is_installed_only_after_ready(self):
        worker = self.prepare()
        w = self.window
        self.assertIsNone(w._prepared_dubbing)
        worker.captions_ready.emit(dict(generation=w.generation, cues=CUES))
        self.assertIsNone(w._prepared_dubbing)
        worker.complete()
        worker.finish_close()
        self.assertTrue(w._prepared_dubbing.enabled)
        self.assertEqual(w._prepared_dubbing.manifests[-1], "C:/fixture/prepared/manifest.json")
        self.assertTrue(worker.shutdown_called)

    def test_cache_hit_can_finish_synchronously_without_leaving_preparation_running(self):
        self.bind()
        FakePreparation.cached_on_start = True
        self.window.prepared_dubbing.setChecked(True)
        worker = FakePreparation.instances[-1]
        worker.finish_close()
        self.assertIsNone(self.window._redub_preparation)
        self.assertTrue(self.window._prepared_dubbing.enabled)
        self.assertEqual(self.window.asr.requests, [])

    def test_closing_restores_both_audio_modes_and_waits_for_preparation_before_bridge_stop(self):
        w = self.window
        worker = self.prepare()
        worker.complete()
        w.english_dubbing.setChecked(True)
        basic, advanced = w._dubbing, w._prepared_dubbing
        w._finish_close = MagicMock()
        event = QCloseEvent()
        w.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(worker.shutdown_called)
        self.assertTrue(basic.shutdown_called)
        self.assertTrue(advanced.shutdown_called)
        self.assertIn("restore_basic", w.bridge.lifecycle)
        self.assertIn("restore_prepared", w.bridge.lifecycle)
        self.assertEqual(w.bridge.stop_count, 0)
        basic.finish_close()
        self.assertEqual(w.bridge.stop_count, 0)
        advanced.finish_close()
        self.assertEqual(w.bridge.stop_count, 0)
        self.assertFalse(w._can_close())
        worker.finish_close()
        self.assertEqual(w.bridge.stop_count, 1)
        self.assertEqual(w.bridge.lifecycle[-1], "bridge_stopped")
        self.assertTrue(w._can_close())


if __name__ == "__main__":
    unittest.main()
