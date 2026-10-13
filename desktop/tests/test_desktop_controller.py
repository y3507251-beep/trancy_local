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
from trancy_desktop.timeline import Timeline, browser_identity, file_identity


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

    def read_bundle(self, *unused):
        return {}

    def write(self, *args):
        self.writes.append(args)
        return True

    def write_bundle(self, *args, **unused):
        return self.write(*args)


class FakeOverlay(QObject):
    moved = pyqtSignal(int, int)

    def __init__(self, settings):
        super().__init__()
        self.last = ("", "")
        self.cue = None
        self.position = None
        self.position_history = []

    def show_caption(self, original, translation="", *, source_language=""):
        self.last = (original, translation)

    def show_cue(self, cue, position=None):
        self.cue = cue
        self.last = (cue.get("text", ""), cue.get("translation", "")) if cue else ("", "")
        self.set_position(position)

    def set_position(self, position):
        self.position = position
        self.position_history.append(position)

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




class FakeBridge(QObject):
    video_changed = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    captions_ready = pyqtSignal(dict)
    dubbing_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.caption_requests = []
        self.caption_result = {"status": "unavailable", "native_track_available": False, "cues": []}
        self.cancelled_captions = []
        self.stop_count = 0

    def videos(self): return list(self.items)
    def start(self, **unused): pass
    def stop(self): self.stop_count += 1

    def request_captions(self, video, generation=0):
        request_id = str(len(self.caption_requests) + 1)
        self.caption_requests.append((dict(video), generation, request_id))
        if self.caption_result is not None:
            self.captions_ready.emit(dict(self.caption_result, generation=generation, request_id=request_id,
                                         bridge_key=controller.MainWindow._browser_tab(video),
                                         key=video.get("key", ""), document_start=video.get("document_start", 0)))
        return request_id

    def cancel_captions(self, request_id):
        self.cancelled_captions.append(request_id)


class FakeDubbing(QObject):
    status = pyqtSignal(str)
    disabled = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.is_closed = False
        self.sources, self.corpus, self.ticks = [], [], []
        self.stop_count = 0
        self.shutdown_called = False

    def set_enabled(self, value): self.enabled = value
    def set_source(self, generation, video): self.sources.append((generation, video))
    def set_cues(self, generation, cues): self.corpus.append((generation, list(cues)))
    def tick(self, *args): self.ticks.append(args)
    def stop(self): self.stop_count += 1
    def shutdown(self): self.shutdown_called = True


class FakeTranslator(QObject):
    translated = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    status = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.jobs = []
        self.windows = []
        self.positions = []
        self.is_closed = True

    def reset(self, generation):
        self.jobs.clear()

    def enqueue(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    def update_window(self, *args, **kwargs):
        self.windows.append((args, kwargs))

    def set_position(self, *args, **kwargs):
        self.positions.append((args, kwargs))

    def shutdown(self):
        self.is_closed = True
        self.closed.emit()


class FakeNativeReader(QObject):
    updated = pyqtSignal(dict)
    finished = pyqtSignal()
    result = {"event": "result", "status": "unavailable", "cues": []}

    def __init__(self, source, job_id, **unused):
        super().__init__()
        self.source, self.job_id = source, job_id
        self.cancelled = False

    def start(self):
        if self.result is not None:
            self.updated.emit(dict(self.result, job_id=self.job_id))
            self.finished.emit()

    def cancel(self):
        self.cancelled = True


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
            patch.object(controller, "BrowserBridge", FakeBridge),
            patch.object(controller, "TranslationQueue", FakeTranslator),
            patch.object(controller, "PotPlayerAdapter", FakePot),
            patch.object(controller, "MediaDownloader", FakeDownloader),
            patch.object(controller, "SubtitleOverlay", FakeOverlay),
            patch.object(controller, "CaptionCache", MemoryCache),
            patch.object(controller, "NativeSubtitleReader", FakeNativeReader),
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

    def test_bridge_start_failure_is_not_reported_as_missing_extension(self):
        reason = "浏览器连接端口 18766 无法启动，请检查是否还有另一个桌面字幕窗口。"
        with patch.object(FakeBridge, "start", side_effect=OSError(reason)):
            blocked = controller.MainWindow(MemorySettings())
        try:
            blocked.tick.stop()
            blocked.start_browser()
            self.assertEqual(blocked.web_status.text(), reason)
            self.assertEqual(blocked.status.text(), reason)
            self.assertEqual(blocked.mode, "idle")
            self.assertEqual(blocked.asr.requests, [])
        finally:
            blocked.hide()
            blocked.deleteLater()

    def test_stale_asr_result_is_cleaned_without_rewriting_new_source(self):
        w = self.window
        w.generation, w.mode = 4, "potplayer"
        old_source = str(controller.TEMP / "asr-old" / "chunk.wav")
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



    def test_synchronous_asr_error_is_terminal_and_keeps_input_for_explicit_retry(self):
        w = self.window
        w._new_source("potplayer", "test", "test")
        w.asr.behavior = "synchronous_error"
        source = str(controller.TEMP / "web-test" / "audio.wav")
        with patch.object(w, "_playback_clock", return_value=(0, True, 1)):
            w._queue_file(source, temporary=True)
        self.assertIsNone(w._active)
        self.assertIsNone(w._pending_file, "A terminal validation error must not requeue a recycled source")
        self.assertTrue(w._recognition_blocked)
        self.assertNotIn(source, self.recycled)
        w.stop()
        self.assertIn(source, self.recycled)

    def test_closing_still_cleans_matching_asr_terminal_input(self):
        w = self.window
        source = str(controller.TEMP / "asr-close" / "chunk.wav")
        w._active = {"job_id": "closing", "generation": w.generation, "source": source, "temporary": True}
        w._closing = True
        w._asr_event({"event": "cancelled", "job_id": "closing"})
        self.assertIsNone(w._active)
        self.assertIn(source, self.recycled)

    def test_close_waits_for_downloader_and_translation_not_just_asr(self):
        w = self.window
        w._closing, w._backend_closed = True, True
        w._close_started = time.monotonic()
        w.translator.is_closed = False
        w.downloader.running = True
        w._finish_close = MagicMock()
        w._tick()
        w._finish_close.assert_not_called()
        w.translator.is_closed = True
        w.downloader.running = False
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
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), title="test-media", position=0)
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
        owned = str(controller.TEMP / "asr-owned" / "chunk.wav")
        external = str(Path(__file__).resolve())
        class InlineThread:
            def __init__(self, target, **unused): self.target = target
            def start(self): self.target()
        with patch.object(Path, "exists", return_value=True), patch.object(controller.threading, "Thread", InlineThread), patch("send2trash.send2trash") as recycle:
            controller.MainWindow._recycle(w, [owned, external])
            recycle.assert_called_once_with([str(Path(owned).resolve())])


    def test_same_potplayer_cue_receives_each_new_playback_position(self):
        w = self.window
        source = str(Path(__file__).resolve())
        w.mode, w.identity = "potplayer", file_identity(source)
        w.pot.state.update(connected=True, path=source, position=1, paused=False, rate=1)
        w.timeline.add([{"start": 0, "end": 10, "text": "We learn English.", "translation": "我们学英语。"}])
        w._tick()
        self.assertEqual(w.overlay.position, 1)
        w.pot.state["position"] = 2
        w._tick()
        self.assertEqual(w.overlay.position, 2,
                         "Word progress must refresh while the sentence text stays unchanged")
        self.assertEqual(w.overlay.cue["text"], "We learn English.")

    def test_paused_potplayer_and_backward_seek_use_actual_player_clock(self):
        w = self.window
        source = str(Path(__file__).resolve())
        w.mode, w.identity = "potplayer", file_identity(source)
        w.pot.state.update(connected=True, path=source, position=4, paused=True, rate=2, updated_at=100)
        w.timeline.add([{"start": 0, "end": 10, "text": "We learn English.", "translation": "我们学英语。"}])
        with patch.object(controller.time, "monotonic", return_value=200), patch.object(controller.time, "time", return_value=200):
            w._tick()
        self.assertEqual(w.overlay.position, 4)
        w.pot.state["position"] = 1
        w._tick()
        self.assertEqual(w.overlay.position, 1)

    def test_browser_clock_uses_reported_rate_and_freezes_when_paused(self):
        w = self.window
        current = dict(video(), position=1, paused=False, rate=2)
        w.mode, w.identity = "browser", browser_identity(current)
        w._last_video = dict(current, received_at=100)
        w.timeline.add([{"start": 0, "end": 10, "text": "We learn English.", "translation": "我们学英语。"}])
        with patch.object(controller.time, "monotonic", return_value=100.4):
            w._tick()
        self.assertAlmostEqual(w.overlay.position, 1.8)
        w._last_video["paused"] = True
        with patch.object(controller.time, "monotonic", return_value=101):
            w._tick()
        self.assertEqual(w.overlay.position, 1)

    def test_only_video_source_tabs_remain(self):
        self.assertEqual([self.window.tabs.tabText(i) for i in range(self.window.tabs.count())],
                         ["PotPlayer", "B 站 / YouTube"])

    def test_english_dubbing_starts_disabled_and_has_no_speech_backend(self):
        w = self.window
        self.assertFalse(w.english_dubbing.isChecked())
        self.assertIsNone(w._dubbing)
        w.bridge.caption_result = {"status": "ready", "cues": [{"start": 0, "end": 5, "text": "Native"}]}
        w._start_browser_video(video())
        w._tick()
        self.assertIsNone(w._dubbing)
        self.assertEqual(w.asr.requests, [])

    def test_dubbing_checkbox_creates_backend_only_after_opt_in_and_shares_captions(self):
        from trancy_desktop import dubbing
        w = self.window
        current = dict(video(), platform="bilibili", video_id="BV1xx411c7mD", cid="12345",
                       url="https://www.bilibili.com/video/BV1xx411c7mD/?p=1")
        w.bridge.caption_result = {"status": "ready", "cues": [{"start": 0, "end": 5, "text": "Native"}]}
        w._start_browser_video(current)
        with patch.object(dubbing, "DubbingSession", FakeDubbing):
            w.english_dubbing.setChecked(True)
        self.assertTrue(w._dubbing.enabled)
        self.assertEqual(w._dubbing.sources[-1][1]["cid"], "12345")
        self.assertEqual(w._dubbing.corpus[-1][1][0]["text"], "Native")
        w._tick()
        self.assertTrue(w._dubbing.ticks)
        w.english_dubbing.setChecked(False)
        self.assertFalse(w._dubbing.enabled)
        w.stop()
        self.assertGreater(w._dubbing.stop_count, 0)

    def test_close_keeps_browser_bridge_until_dubbing_restore_is_finished(self):
        from trancy_desktop import dubbing
        w = self.window
        with patch.object(dubbing, "DubbingSession", FakeDubbing):
            w.english_dubbing.setChecked(True)
        with patch.object(w, "_finish_close") as finished:
            w.closeEvent(MagicMock())
            self.assertTrue(w._dubbing.shutdown_called)
            self.assertEqual(w.bridge.stop_count, 0)
            self.assertFalse(finished.called)
            w._dubbing.is_closed = True
            w._dubbing.closed.emit()
            self.assertEqual(w.bridge.stop_count, 1)
            self.assertTrue(finished.called)

    def test_native_browser_subtitles_skip_audio_and_asr(self):
        w = self.window
        cues = [{"start": 2, "end": 6, "text": "Hello everyone.", "source_language": "en",
                 "words": [{"start": 2, "end": 3, "text": "Hello"}]}]
        w.bridge.caption_result = {"status": "ready", "cues": cues, "source_language": "en"}
        w._start_browser_video(video())
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])
        self.assertEqual(w.timeline.cues[0]["words"], cues[0]["words"])
        self.assertEqual(w.translator.windows[-1][0][2], 3)
        w._tick()
        self.assertEqual(w.overlay.last[0], "Hello everyone.")

    def test_native_browser_cold_start_does_not_create_a_real_asr_process(self):
        from trancy_desktop.recognition import ASRSession
        with patch.object(controller, "ASRSession", ASRSession):
            w = controller.MainWindow(MemorySettings())
        try:
            w.tick.stop()
            w.model.setCurrentIndex(w.model.findData("qwen3-asr-1.7b"))
            self.assertIsNone(w.asr.process)
            w.bridge.caption_result = {"status": "ready", "source_language": "zh-CN",
                                      "cues": [{"start": 2, "end": 6, "text": "这是已有的中文字幕"}]}
            current = dict(video(), platform="bilibili", video_id="BV1xx411c7mD", cid="12345",
                           url="https://www.bilibili.com/video/BV1xx411c7mD/?p=1")
            w._start_browser_video(current)
            w._tick()
            self.assertIsNone(w.asr.process)
            self.assertEqual(w.downloader.requests, [])
            self.assertEqual(w._source_kind, "native")
            self.assertTrue(w.translator.windows)
        finally:
            w.tick.stop()
            w.asr.shutdown()
            w.hide()
            w.deleteLater()

    def test_potplayer_native_subtitles_skip_asr_and_reuse_platform_translation(self):
        w = self.window
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), position=3, duration=60, paused=True)
        native = {"event": "result", "status": "ready", "cues": [
            {"start": 2, "end": 6, "text": "Hello everyone.", "translation": "大家好。", "source_language": "en"}]}
        with patch.object(FakeNativeReader, "result", native):
            w.start_potplayer()
        w._tick()
        self.assertEqual(w.asr.requests, [])
        self.assertEqual(w.overlay.last, ("Hello everyone.", "大家好。"))
        self.assertEqual(w._native_readers, {})

    def test_no_subtitles_start_near_current_position_instead_of_file_start(self):
        w = self.window
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), position=400, duration=3600, paused=True)
        w.start_potplayer()
        options = w.asr.requests[-1][2]
        self.assertEqual((options["start_seconds"], options["duration_seconds"]), (398, 12))
        job = w._active["job_id"]
        w._asr_event({"event": "result", "job_id": job, "cues": [], "segment_end": 410, "source_duration": 3600})
        for _ in range(20):
            w._tick()
        self.assertEqual(len(w.asr.requests), 1, "Paused video with sufficient coverage must stop computing")

    def test_seek_cancels_wrong_audio_window_and_prioritizes_new_position(self):
        w = self.window
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), position=0, duration=3600, paused=True)
        w.start_potplayer()
        w._tick()
        old_job = w._active["job_id"]
        w.pot.state["position"] = 900
        w._tick()
        self.assertEqual(w.asr.cancel_count, 1)
        w._asr_event({"event": "cancelled", "job_id": old_job})
        w._tick()
        self.assertEqual(w.asr.requests[-1][2]["start_seconds"], 898)
        self.assertTrue(any(kwargs.get("seek") for _, kwargs in w.translator.positions))

    def test_missing_or_disconnected_player_clock_does_not_recognize_from_zero(self):
        w = self.window
        source = str(Path(__file__).resolve())
        w.pot.state.update(connected=True, path=source, position=None, duration=3600)
        w.start_potplayer()
        self.assertEqual(w.asr.requests, [])
        w.pot.state.update(connected=False, position=200)
        w._tick()
        self.assertEqual(w.asr.requests, [])

    def test_web_ad_after_download_does_not_start_recognition(self):
        w = self.window
        current = video()
        w._start_browser_video(current)
        public, directory = w.downloader.requests[-1]
        w.downloader.running = False
        w._last_video["ad_playing"] = True
        w._downloaded(str(Path(directory) / "media-test" / "audio.wav"), public)
        self.assertEqual(w.asr.requests, [])

    def test_native_caption_timeout_retries_text_without_audio_or_model(self):
        w = self.window
        w.bridge.caption_result = None
        current = video()
        w._start_browser_video(current)
        request_id = w._caption_context["request_id"]
        event = dict(generation=w.generation, request_id=request_id, document_start=0,
                     bridge_key=w._bound_tab, status="timeout")
        w._browser_captions(event)
        w._browser_captions(dict(event, status="ready", cues=[{"start": 0, "end": 5, "text": "late"}]))
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])
        self.assertEqual(w.timeline.cues, [])
        self.flush_scheduled()
        self.assertEqual(len(w.bridge.caption_requests), 2)
        w._browser_captions(dict(event, request_id=w._caption_context["request_id"], status="ready",
                                cues=[{"start": 0, "end": 5, "text": "Native caption"}]))
        w._tick()
        self.assertEqual(w.timeline.cues[0]["text"], "Native caption")
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])

    def test_repeated_native_fetch_failure_stays_in_text_mode_and_manual_retry_works(self):
        w = self.window
        w.bridge.caption_result = {"status": "error", "cues": [], "native_track_available": True,
                                  "error_code": "bili_track_failed"}
        w._start_browser_video(video())
        self.flush_scheduled()
        self.flush_scheduled()
        self.assertEqual(len(w.bridge.caption_requests), 2)
        self.assertTrue(w._caption_context["failed"])
        self.assertIn("重试未完成字幕", w.status.text())
        w._tick()
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])
        w.bridge.caption_result = {"status": "ready", "source_language": "en",
                                  "cues": [{"start": 0, "end": 5, "text": "Retry worked"}]}
        w.retry_translation()
        self.assertEqual(len(w.bridge.caption_requests), 3)
        self.assertEqual(w.timeline.cues[0]["text"], "Retry worked")
        self.assertEqual(w.downloader.requests, [])

    def test_known_native_track_cannot_be_misclassified_as_missing(self):
        w = self.window
        w.bridge.caption_result = {"status": "unavailable", "native_track_available": True, "cues": []}
        w._start_browser_video(video())
        self.flush_scheduled()
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])

    def test_old_connector_missing_track_confirmation_does_not_load_model(self):
        w = self.window
        w.bridge.caption_result = {"status": "unavailable", "cues": []}
        w._start_browser_video(video())
        self.flush_scheduled()
        self.assertEqual(w.downloader.requests, [])
        self.assertEqual(w.asr.requests, [])

    def test_previous_source_model_event_does_not_replace_native_mode_label(self):
        w = self.window
        w.bridge.caption_result = {"status": "ready", "cues": [{"start": 0, "end": 5, "text": "Native"}]}
        w._start_browser_video(video())
        w._active = {"generation": w.generation - 1, "job_id": "old-loading-model", "window": (0, 12)}
        before = w.model_info.text()
        w._asr_event({"event": "model", "job_id": "old-loading-model", "model": "qwen3-asr-1.7b", "device": "cuda"})
        self.assertEqual(w.model_info.text(), before)

    def test_disconnected_caption_bridge_does_not_trigger_asr(self):
        w = self.window
        with patch.object(w.bridge, "request_captions", side_effect=ValueError("connection unavailable")):
            w._start_browser_video(video())
        self.assertEqual(w.downloader.requests, [])
        self.assertIn("重试未完成字幕", w.status.text())
        w.bridge.caption_result = {"status": "ready", "cues": [{"start": 0, "end": 5, "text": "Recovered"}]}
        w.retry_translation()
        self.assertEqual(len(w.bridge.caption_requests), 1)
        self.assertEqual(w.timeline.cues[0]["text"], "Recovered")
        self.assertEqual(w.asr.requests, [])

    def test_stop_cancels_scheduled_native_caption_retry(self):
        w = self.window
        w.bridge.caption_result = {"status": "error", "cues": []}
        w._start_browser_video(video())
        w.stop()
        self.flush_scheduled()
        self.assertEqual(len(w.bridge.caption_requests), 1)
        self.assertEqual(w.asr.requests, [])

    def test_confirmed_missing_browser_track_still_uses_short_audio_recognition(self):
        w = self.window
        w.bridge.caption_result = {"status": "unavailable", "native_track_available": False, "cues": []}
        current = dict(video(), position=400, duration=3600)
        w._start_browser_video(current)
        self.assertEqual(len(w.downloader.requests), 1)
        public, directory = w.downloader.requests[0]
        w.downloader.running = False
        path = str(Path(directory) / "media-test" / "audio.wav")
        with patch.object(Path, "is_file", return_value=True):
            w._downloaded(path, public)
        self.assertEqual(len(w.asr.requests), 1)
        options = w.asr.requests[0][2]
        self.assertEqual((options["start_seconds"], options["duration_seconds"]), (398, 12))

    def test_same_video_document_reload_restarts_caption_request_even_without_follow(self):
        w = self.window
        w.bridge.caption_result = None
        first = dict(video(), document_start=100)
        w._start_browser_video(first)
        previous = dict(w._caption_context)
        reloaded = dict(first, document_start=101)
        w.bridge.items = [reloaded]
        w._browser_update(reloaded)
        self.assertEqual(len(w.bridge.caption_requests), 2)
        self.assertIn(previous["request_id"], w.bridge.cancelled_captions)
        w._browser_captions(dict(generation=w.generation, request_id=w._caption_context["request_id"],
                                bridge_key=w._bound_tab, document_start=100, status="ready",
                                cues=[{"start": 0, "end": 5, "text": "old document"}]))
        self.assertEqual(w.timeline.cues, [])

    def test_partial_recognition_cache_does_not_repeat_covered_audio(self):
        w = self.window
        bundle = dict(source_kind="asr", corpus_complete=False, duration=3600, coverage=[(100, 120)],
                      cues=[{"start": 105, "end": 108, "text": "Hello", "translation": "你好"}])
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), position=110, duration=3600, paused=True)
        with patch.object(w.cache, "read_bundle", side_effect=lambda _, model, __: bundle if model == "small" else {}):
            w.start_potplayer()
        self.assertEqual(w.asr.requests, [])
        w.pot.state["position"] = 170
        w._tick()
        self.assertEqual(w.asr.requests[-1][2]["start_seconds"], 168)

    def test_native_subtitle_reread_reuses_text_cache_without_reusing_old_times(self):
        w = self.window
        bundle = dict(source_kind="native", cues=[{"start": 0, "end": 3, "text": "Hello", "translation": "你好"}])
        fresh = dict(event="result", cues=[{"start": 20, "end": 23, "text": "Hello"}])
        w.pot.state.update(connected=True, path=str(Path(__file__).resolve()), position=21, duration=30, paused=True)
        with patch.object(w.cache, "read_bundle", side_effect=lambda _, model, __: bundle if model == "native" else {}), patch.object(FakeNativeReader, "result", fresh):
            w.start_potplayer()
        self.assertEqual([(c["start"], c["translation"]) for c in w.timeline.cues], [(20, "你好")])
        self.assertEqual(w.asr.requests, [])

    def test_old_seek_translation_error_does_not_mark_current_caption_failed(self):
        w = self.window
        w.translator.epoch = 5
        cue = {"start": 1, "end": 4, "text": "Hello"}
        w.timeline.add([cue])
        w._translation_error(dict(generation=w.generation, epoch=4, cues=[cue], message="old error"))
        self.assertNotIn("translation_error", w.timeline.cues[0])
        w._translation_error(dict(generation=w.generation, epoch=5, cues=[cue], message="current error"))
        self.assertEqual(w.timeline.cues[0]["translation_error"], "current error")
        w._translated(dict(generation=w.generation, epoch=5, cues=[dict(cue, translation="你好")]))
        self.assertNotIn("translation_error", w.timeline.cues[0])

    def test_stop_waits_to_recycle_audio_until_current_window_releases_it(self):
        w = self.window
        w._start_browser_video(video())
        public, directory = w.downloader.requests[-1]
        source = str(Path(directory) / "media-test" / "audio.wav")
        w.downloader.running = False
        w._downloaded(source, public)
        job = w._active["job_id"]
        w.stop()
        self.assertNotIn(source, self.recycled)
        w._asr_event({"event": "cancelled", "job_id": job})
        self.assertIn(source, self.recycled)





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

    def test_same_cue_can_receive_word_alignment_after_initial_text(self):
        timeline = Timeline()
        cue = {"start": 0, "end": 2, "text": "hello there", "_job_id": "job"}
        words = [{"start": .2, "end": .7, "text": "hello"}, {"start": .8, "end": 1.4, "text": "there"}]
        timeline.add([cue])
        timeline.add([{**cue, "words": words, "timing_source": "whisper"}])
        self.assertEqual(timeline.at(1)["words"], words)
        self.assertEqual(timeline.at(1)["timing_source"], "whisper")

    def test_late_translation_keeps_current_audio_word_alignment(self):
        timeline = Timeline()
        cue = {"start": 0, "end": 2, "text": "hello there", "_job_id": "job"}
        old = [{"start": 0, "end": .7, "text": "hello"}]
        current = [{"start": .2, "end": .7, "text": "hello"}, {"start": .8, "end": 1.4, "text": "there"}]
        timeline.add([{**cue, "words": current, "timing_source": "whisper"}])
        timeline.update_translations([{**cue, "words": old, "translation": "你们好", "source_language": "en",
                                      "target_language": "zh", "timing_source": "cue"}])
        self.assertEqual(timeline.at(1)["words"], current)
        self.assertEqual(timeline.at(1)["timing_source"], "whisper")
        self.assertEqual(timeline.at(1)["translation"], "你们好")


def player_integration(source: str) -> dict:
    """Opt-in real MainWindow -> PotPlayer -> SubtitleService -> Google test.

    The input must be a generated test WAV with at least four silent seconds at
    the beginning. Only a new, PID-verified PotPlayer instance is controlled.
    Artifacts and the input are kept.
    """
    import ctypes
    from PyQt6.QtGui import QFont, QFontDatabase
    from trancy_desktop.potplayer import (
        _WindowsBackend, find_potplayer_executable, local_media_path,
        POT_GET_PLAY_STATUS, POT_SET_PLAY_STATUS, POT_GET_CURRENT_TIME,
        POT_GET_TOTAL_TIME,
    )
    from trancy_desktop.settings import Settings
    from trancy_desktop.translation import detect_target

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
              "completed": False}

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
        window.tabs.setCurrentIndex(0)
        spin(lambda: window._source_complete and len(window.timeline.cues) >= 2
             and all(cue.get("translation") for cue in window.timeline.cues), 300, "recognize and translate generated speech")
        report["asr_events"] = events
        report["cues"] = list(window.timeline.cues)
        report["snapshots"] = []
        for index, cue in enumerate((window.timeline.cues[0], window.timeline.cues[-1]), 1):
            target = (cue["start"] + cue["end"]) / 2
            source_language = cue.get("source_language") or detect_target(cue["text"])[0]
            english, chinese = ((cue["translation"], cue["text"]) if source_language.startswith("zh")
                                else (cue["text"], cue["translation"]))
            if not window.pot.seek(target):
                raise AssertionError("PotPlayer rejected seek")
            spin(lambda: window.pot.snapshot().get("position") is not None
                 and abs(window.pot.snapshot()["position"] - target) < .4, 5, "seek subtitle " + str(index))
            spin(lambda: window.overlay.english.text() == english and window.overlay.chinese.text() == chinese,
                 5, "synchronize bilingual overlay " + str(index))
            state = window.pot.snapshot()
            assert state["paused"] is True, state
            assert window.overlay.isVisible(), "Non-empty enabled subtitles should be visible"
            image_path = artifact / f"overlay-{index}.png"
            assert window.overlay.grab().save(str(image_path))
            report["snapshots"].append({"position": state["position"], "original": cue["text"],
                                        "translation": cue["translation"], "english": window.overlay.english.text(),
                                        "chinese": window.overlay.chinese.text(), "image": str(image_path)})
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
    if "--player-integration" in sys.argv:
        fixture = sys.argv[sys.argv.index("--player-integration") + 1]
        print(json.dumps(player_integration(fixture), ensure_ascii=False, indent=2), flush=True)
    else:
        unittest.main()
