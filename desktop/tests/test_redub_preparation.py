"""Whole-video preparation with controlled download, recognition and processes.

Small WAV/job fixtures are retained under data/validation. These tests do not
contact a service, start an inference model, or permanently delete files.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication
from trancy_desktop import redub_preparation as preparation
from trancy_desktop.timeline import browser_identity

APP = QApplication.instance() or QApplication([])
VIDEO = dict(platform="bilibili", video_id="BV1xx411c7mD", page=1, cid="12345",
             url="https://www.bilibili.com/video/BV1xx411c7mD/", duration=4,
             dubbing_protocol=1, rate_control_protocol=1)
CUES = [dict(start=0, end=4, text="你好世界", source_language="zh-CN",
             translation="Hello world", target_language="en")]
OPTIONS = dict(service_root="C:/fixture/SubtitleService", model="small", device="cpu")


def valid_manifest(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    audio_path = directory / "mix.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * (4 * 8000))
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(dict(schema=1, mix_path="mix.wav", source_duration=4,
        output_duration=4, voice_model="fixture-local-voice", reference=dict(start=0, end=2),
        segments=[dict(source_start=0, source_end=4, audio_start=0, audio_end=4, kind="speech")])), encoding="utf-8")
    return manifest


class FakeDownloader(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str)

    def __init__(self, parent=None, **options):
        super().__init__(parent)
        self.options, self.requests = options, []
        self.running = self.cancelled = False

    def download(self, video, directory):
        self.requests.append((dict(video), directory))
        self.running = True

    def cancel(self):
        self.cancelled = True

    def finish(self, video=None):
        requested, directory = self.requests[-1]
        self.running = False
        self.finished.emit(str(Path(directory) / "audio.wav"), video or requested)


class FakeASR(QObject):
    updated = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests = []
        self.shutdown_called = False

    def submit(self, path, job_id, **options):
        self.requests.append((path, job_id, options))
        return True

    def shutdown(self):
        self.shutdown_called = True

    def finish_close(self):
        self.closed.emit()


class FakeWork(QObject):
    progress = pyqtSignal(str)
    captions = pyqtSignal(list)
    ready = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, job, **options):
        super().__init__(options.pop("parent", None))
        self.job, self.options = dict(job), options
        self.running = self.cancelled = False

    def start(self): self.running = True
    def isRunning(self): return self.running
    def cancel(self): self.cancelled = True

    def finish(self):
        self.running = False
        self.finished.emit()


class PreparationFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "data/validation/redub-preparation"
        root.mkdir(parents=True, exist_ok=True)
        cls.directory = Path(tempfile.mkdtemp(prefix="tests-", dir=root))

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.runtime = self.folder / "runtime"
        (self.runtime / ".venv/Scripts").mkdir(parents=True)
        (self.runtime / ".venv/Scripts/python.exe").write_bytes(b"test marker; never executed")
        self.cache = self.folder / "cache"
        self.downloaders, self.recognizers, self.workers = [], [], []

        def factory(cls, records):
            def create(*args, **kwargs):
                item = cls(*args, **kwargs)
                records.append(item)
                return item
            return create

        self.subject = preparation.RedubPreparation(object(), runtime_root=self.runtime, cache_root=self.cache,
            downloader_factory=factory(FakeDownloader, self.downloaders),
            asr_factory=factory(FakeASR, self.recognizers), work_factory=factory(FakeWork, self.workers))
        self.ready, self.captions, self.failures, self.closed = [], [], [], []
        self.subject.ready.connect(self.ready.append)
        self.subject.captions_ready.connect(self.captions.append)
        self.subject.failed.connect(self.failures.append)
        self.subject.closed.connect(lambda: self.closed.append(True))

    def tearDown(self):
        self.subject.shutdown()
        for item in self.downloaders: item.running = False
        for item in self.recognizers: item.finish_close()
        for item in self.workers: item.finish()
        self.subject._check_closed()
        self.subject._timer.stop()
        self.subject.deleteLater()

    def start(self, *, complete=True, cues=None, mode="plugin", **options):
        self.subject.start(7, VIDEO, CUES if cues is None else cues, complete=complete,
                           mode=mode, options=OPTIONS, **options)

    def test_constructing_default_off_job_creates_no_download_asr_or_model_worker(self):
        self.assertFalse(self.subject.running)
        self.assertEqual(self.downloaders + self.recognizers + self.workers, [])

    def test_complete_native_captions_download_audio_but_skip_asr_and_keep_selected_engine(self):
        self.start(mode="plugin")
        self.assertEqual(len(self.downloaders), 1)
        self.assertEqual(self.downloaders[0].requests[0][0], VIDEO)
        self.downloaders[0].finish()
        self.assertEqual(self.recognizers, [])
        self.assertEqual(len(self.workers), 1)
        worker = self.workers[0]
        self.assertEqual(worker.job["cues"], CUES)
        self.assertEqual(worker.options["mode"], "plugin")
        self.assertIs(worker.options["bridge"], self.subject.bridge)
        self.assertEqual(worker.job["source_duration"], 4)

    def test_incomplete_captions_use_whole_audio_and_release_asr_before_model_preparation(self):
        self.start(complete=False)
        self.downloaders[0].finish()
        asr = self.recognizers[0]
        source, job, options = asr.requests[0]
        self.assertEqual(options, OPTIONS)
        self.assertNotIn("start_seconds", options)
        self.assertNotIn("duration_seconds", options)
        asr.updated.emit(dict(event="result", job_id=job, detected_language="zh-CN", cues=CUES))
        self.assertTrue(asr.shutdown_called)
        self.assertEqual(self.workers, [])
        asr.finish_close()
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.workers[0].job["source_audio"], source)

    def test_other_video_download_or_old_asr_job_does_not_start_preparation(self):
        self.start(complete=False)
        self.downloaders[0].finish(dict(VIDEO, cid="99999"))
        self.assertEqual(self.recognizers, [])
        self.downloaders[0].finish()
        self.recognizers[0].updated.emit(dict(event="result", job_id="old", cues=CUES))
        self.assertEqual(self.workers, [])
        self.assertFalse(self.recognizers[0].shutdown_called)

    def test_cancel_during_download_is_nonblocking_and_waits_for_owned_download_exit(self):
        self.start()
        began = time.perf_counter()
        self.subject.shutdown()
        self.assertLess(time.perf_counter() - began, .25)
        self.assertTrue(self.downloaders[0].cancelled)
        self.assertFalse(self.subject.is_closed)
        self.downloaders[0].finish()
        self.subject._check_closed()
        self.assertTrue(self.subject.is_closed)
        self.assertEqual(self.closed, [True])
        self.assertEqual(self.workers + self.recognizers, [])

    def test_cancel_during_asr_waits_for_process_close_and_ignores_late_result(self):
        self.start(complete=False)
        self.downloaders[0].finish()
        asr = self.recognizers[0]
        self.subject.shutdown()
        self.assertTrue(asr.shutdown_called)
        self.assertFalse(self.subject.is_closed)
        asr.updated.emit(dict(event="result", job_id=asr.requests[0][1], cues=CUES))
        asr.finish_close()
        self.assertTrue(self.subject.is_closed)
        self.assertEqual(self.workers, [])

    def test_asr_failure_can_close_synchronously_without_reusing_a_cleared_backend(self):
        self.start(complete=False)
        self.downloaders[0].finish()
        asr = self.recognizers[0]
        asr.shutdown = asr.finish_close
        self.subject.failed.connect(lambda message: self.subject.shutdown())
        # Direct invocation keeps any regression a normal test failure instead
        # of an uncaught exception escaping a PyQt signal handler.
        self.subject._asr_event(dict(event="error", job_id=asr.requests[0][1]))
        self.assertTrue(self.subject.is_closed)
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.workers, [])

    def test_rejected_asr_submission_can_reentrantly_close_during_failure_callback(self):
        self.start(complete=False)
        original_factory = self.subject._asr_factory
        def immediate_backend(*args, **kwargs):
            asr = original_factory(*args, **kwargs)
            asr.shutdown = asr.finish_close
            asr.submit = lambda *args, **kwargs: False
            return asr
        self.subject._asr_factory = immediate_backend
        self.subject.failed.connect(lambda message: self.subject.shutdown())
        self.downloaders[0].running = False
        self.subject._downloaded("C:/fixture/audio.wav", VIDEO)
        self.assertTrue(self.subject.is_closed)
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.workers, [])

    def test_cancel_during_model_work_ignores_queued_results_until_thread_exits(self):
        self.start()
        self.downloaders[0].finish()
        worker = self.workers[0]
        self.subject.shutdown()
        self.assertTrue(worker.cancelled)
        self.assertFalse(self.subject.is_closed)
        worker.captions.emit(CUES)
        worker.ready.emit(str(self.folder / "late.json"))
        self.assertEqual(self.ready + self.captions + self.failures, [])
        worker.finish()
        self.assertTrue(self.subject.is_closed)

    def test_valid_cache_reuses_whole_audio_without_any_worker_or_installed_runtime(self):
        manifest = valid_manifest(self.cache / "previous")
        key = preparation.preparation_key(VIDEO, "plugin")
        (self.cache / (key + ".json")).write_text(json.dumps(dict(manifest=str(manifest), cues=CUES)), encoding="utf-8")
        self.subject.runtime_root = self.folder / "absent-runtime"
        self.start()
        self.assertEqual(self.downloaders + self.recognizers + self.workers, [])
        self.assertEqual(self.captions[0]["cues"], CUES)
        self.assertEqual(self.ready[0], dict(generation=7, identity=browser_identity(VIDEO), manifest=str(manifest), cached=True))

    def test_force_regeneration_ignores_valid_cache_and_retains_original_files(self):
        manifest = valid_manifest(self.cache / "previous")
        key = preparation.preparation_key(VIDEO, "plugin")
        index = self.cache / (key + ".json")
        index.write_text(json.dumps(dict(manifest=str(manifest), cues=CUES)), encoding="utf-8")
        self.start(force=True)
        self.assertEqual(len(self.downloaders), 1)
        self.assertTrue(index.is_file())
        self.assertTrue(manifest.is_file())
        self.assertEqual(self.ready, [])

    def test_cache_path_outside_preparation_root_is_not_reused(self):
        manifest = valid_manifest(self.folder / "outside-cache")
        self.cache.mkdir()
        key = preparation.preparation_key(VIDEO, "plugin")
        (self.cache / (key + ".json")).write_text(json.dumps(dict(manifest=str(manifest), cues=CUES)), encoding="utf-8")
        self.start()
        self.assertEqual(len(self.downloaders), 1)
        self.assertEqual(self.ready, [])

    def test_completed_manifest_and_captions_are_indexed_for_replay(self):
        self.start()
        self.downloaders[0].finish()
        worker = self.workers[0]
        manifest = valid_manifest(worker.job["output_dir"])
        worker.captions.emit(CUES)
        worker.ready.emit(str(manifest))
        self.assertFalse(self.ready[0]["cached"])
        saved = json.loads(self.subject._index.read_text(encoding="utf-8"))
        self.assertEqual(saved["cues"], CUES)
        self.assertEqual(saved["identity"], browser_identity(VIDEO))
        self.assertEqual(saved["manifest"], str(manifest))


class TranslationPreparationTests(unittest.TestCase):
    def test_only_missing_english_is_translated_in_bounded_batches(self):
        cues = [dict(start=i, end=i+1, text=f"中文句子{i}", source_language="zh-CN") for i in range(33)]
        cues.extend([dict(start=34, end=35, text="Already English", source_language="en"),
                     dict(start=35, end=36, text="已经有译文", translation="Cached English", target_language="en")])
        calls = []
        class Client:
            def translate(self, texts, source, target):
                calls.append((list(texts), source, target))
                return [f"English sentence {i}" for i in range(len(texts))]
        result = preparation.translate_all(cues, Client(), threading.Event(), lambda text: None)
        self.assertEqual([len(c[0]) for c in calls], [30, 3])
        self.assertTrue(all(c[1:] == ("zh-CN", "en") for c in calls))
        self.assertEqual(result[-1]["translation"], "Cached English")
        self.assertNotIn("translation", cues[0])

    def test_cancel_between_batches_does_not_send_further_translation_requests(self):
        cancelled, calls = threading.Event(), []
        class Client:
            def translate(self, texts, *unused):
                calls.append(list(texts))
                cancelled.set()
                return ["English words"] * len(texts)
        cues = [dict(start=i, end=i+1, text="等待翻译") for i in range(31)]
        with self.assertRaises(InterruptedError):
            preparation.translate_all(cues, Client(), cancelled, lambda text: None)
        self.assertEqual([len(c) for c in calls], [30])

    def test_wrong_count_or_non_english_translation_cannot_reach_voice_worker(self):
        for answer in ([], ["还是中文"]):
            class Client:
                def translate(self, *unused): return answer
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                preparation.translate_all([dict(start=0, end=1, text="等待翻译")], Client(), threading.Event(), lambda text: None)


class WorkProcessTests(unittest.TestCase):
    # This separate class reuses only filesystem setup. Preparation behavior is
    # tested above; model subprocesses remain controlled Python objects.
    setUpClass = classmethod(PreparationFixtures.setUpClass.__func__)
    setUp = PreparationFixtures.setUp
    tearDown = PreparationFixtures.tearDown

    def test_model_process_exits_before_ready_and_selected_engine_is_reused(self):
        output = self.folder / "output"
        manifest = valid_manifest(output)
        lifecycle, modes = [], []
        class Client:
            def __init__(self, mode, bridge): modes.append((mode, bridge))
            def translate(self, texts, source, target): return ["Translated English"] * len(texts)
            def close(self): lifecycle.append("client_closed")
        class Process:
            returncode = None
            def __init__(self, *args, **kwargs):
                self.stdout = io.StringIO(json.dumps(dict(event="ready", manifest=str(manifest))) + "\n")
            def poll(self): return self.returncode
            def wait(self, timeout): self.returncode = 0; lifecycle.append("process_exited")
            def terminate(self): lifecycle.append("terminate")
        bridge = object()
        worker = preparation._PrepareWork(dict(cues=[dict(start=0, end=4, text="你好世界")], output_dir=str(output)),
            bridge=bridge, mode="plugin", runtime_root=self.runtime, client_factory=Client)
        worker.ready.connect(lambda path: lifecycle.append("ready"))
        failures = []
        worker.failed.connect(failures.append)
        with patch.object(preparation.subprocess, "Popen", Process): worker.run()
        self.assertEqual(failures, [])
        self.assertEqual(modes, [("plugin", bridge)])
        self.assertLess(lifecycle.index("process_exited"), lifecycle.index("ready"))
        self.assertIn("client_closed", lifecycle)
        self.assertEqual(json.loads((output / "job.json").read_text(encoding="utf-8"))["cues"][0]["translation"], "Translated English")

    def test_cancellation_returns_while_translation_thread_is_still_blocked(self):
        entered, release = threading.Event(), threading.Event()
        class Client:
            def __init__(self, *unused): pass
            def translate(self, texts, *unused):
                entered.set()
                release.wait(3)
                return ["English words"] * len(texts)
            def close(self): pass
        output = self.folder / "output"
        output.mkdir()
        worker = preparation._PrepareWork(dict(cues=[dict(start=0, end=4, text="你好世界")], output_dir=str(output)),
            bridge=object(), mode="google", runtime_root=self.runtime, client_factory=Client)
        try:
            with patch.object(preparation.subprocess, "Popen") as process:
                worker.start()
                self.assertTrue(entered.wait(2))
                began = time.perf_counter()
                worker.cancel()
                self.assertLess(time.perf_counter() - began, .25)
                self.assertTrue(worker.isRunning())
                release.set()
                self.assertTrue(worker.wait(3000))
                process.assert_not_called()
        finally:
            release.set()
            worker.wait(3000)


if __name__ == "__main__":
    unittest.main()
