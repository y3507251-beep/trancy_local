"""Controlled pipeline checks. These do not record system audio or load a model."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QCoreApplication
from send2trash import send2trash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop import recognition
from trancy_desktop import audio_capture
from trancy_desktop.audio_capture import PCMChunker, _loopback_devices


def spin_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    QCoreApplication.processEvents()
    return predicate()


class PCMChunkerTests(unittest.TestCase):
    def test_stereo_partitioning_silence_and_continuous_clock(self):
        rate = 8000
        chunker = PCMChunker(rate, 2, .5)
        silence = np.zeros((rate // 2, 2), dtype="<i2")
        voiced = np.full((rate // 2, 2), 1000, dtype="<i2")
        raw = np.concatenate([silence, voiced]).tobytes()
        chunks = []
        for start in range(0, len(raw), 148):
            chunks.extend(chunker.push(raw[start:start + 148]))
        self.assertEqual(len(chunks), 2)
        self.assertFalse(chunks[0].has_audio)
        self.assertTrue(chunks[1].has_audio)
        self.assertEqual((chunks[1].start, chunks[1].end), (.5, 1.0))
        self.assertEqual(len(chunks[1].pcm), rate)
        self.assertTrue(np.all(np.frombuffer(chunks[1].pcm, dtype="<i2") == 1000))
        self.assertIsNone(chunker.finish())

    def test_missing_silent_callbacks_preserve_capture_timeline(self):
        chunker = PCMChunker(8000, 1, .5)
        voice = np.full(4000, 1000, dtype="<i2").tobytes()
        first = chunker.push(voice, start_frame=0)[0]
        later = chunker.push(voice, start_frame=80000)[0]
        self.assertEqual((first.start, first.end), (0, .5))
        self.assertEqual((later.start, later.end), (10, 10.5))

    def test_short_speech_in_otherwise_quiet_chunk_is_retained(self):
        chunker = PCMChunker(8000, 1, 4)
        values = np.zeros(32000, dtype="<i2")
        values[800:2400] = 300
        self.assertTrue(chunker.push(values.tobytes())[0].has_audio)

    def test_invalid_frame_alignment_and_unbounded_chunks_rejected(self):
        with self.assertRaises(ValueError):
            PCMChunker(48000, 2, 120)
        with self.assertRaises(ValueError):
            PCMChunker(48000, 2, 4).push(b"\x00\x00")

    def test_device_inventory_rejects_nonloopback_inputs(self):
        class Manager:
            def get_default_wasapi_loopback(self):
                return dict(index=7)

            def get_loopback_device_info_generator(self):
                return iter([
                    dict(index=3, name="microphone", maxInputChannels=1, defaultSampleRate=48000),
                    dict(index=7, name="speaker loopback", maxInputChannels=2,
                         defaultSampleRate=48000, isLoopbackDevice=True),
                ])
        devices = _loopback_devices(Manager())
        self.assertEqual([item["index"] for item in devices], [7])
        self.assertTrue(devices[0]["is_default"])


FAKE_WORKER = r'''
import json, os, queue, sys, threading, time
requests = queue.Queue()
cancelled = set()
stopping = threading.Event()
def controls():
    for line in sys.stdin:
        command = json.loads(line)
        if command.get("command") == "cancel":
            cancelled.add(command["request_id"])
        elif command.get("command") == "shutdown":
            stopping.set()
            requests.put(None)
            return
        else:
            requests.put(command)
    stopping.set()
    requests.put(None)
threading.Thread(target=controls, daemon=True).start()
count = 0
while not stopping.is_set():
    request = requests.get()
    if request is None: break
    rid = request["request_id"]
    def emit(event, **data):
        print(json.dumps(dict(event=event, request_id=rid, **data)), flush=True)
    count += 1
    emit("model", device="cpu", model_instance="retained", load_count=1,
         worker_pid=os.getpid(), reused=count > 1)
    if "slow" in request["source"]:
        until = time.monotonic() + 3
        while time.monotonic() < until and rid not in cancelled and not stopping.is_set():
            time.sleep(.01)
    if rid in cancelled or stopping.is_set():
        emit("cancelled", message="controlled cancel")
    else:
        cues = [dict(start=.1, end=1.2, text="hello")]
        emit("state", new_cues=cues, detected_language="English", progress=90)
        emit("result", cues=cues, detected_language="English")
'''


class ASRSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="trancy-asr-tests-"))
        (self.root / "service").mkdir()
        (self.root / "service" / "engine.py").write_text("# controlled test double\n", encoding="utf-8")
        (self.root / "asr_worker.py").write_text(FAKE_WORKER, encoding="utf-8")
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"source owned by caller")
        self.slow = self.root / "slow.wav"
        self.slow.write_bytes(b"controlled slow job")
        self.session = recognition.ASRSession(runtime_dir=self.root / "jobs")
        self.events, self.idles, self.closes = [], [], []
        self.session.updated.connect(self.events.append)
        self.session.idle.connect(lambda: self.idles.append(True))
        self.session.closed.connect(lambda: self.closes.append(True))
        self.worker_patch = patch.object(recognition, "__file__", str(self.root / "recognition.py"))
        self.interpreter_patch = patch.object(recognition.ASRSession, "_interpreter", return_value=Path(sys.executable))
        self.worker_patch.start()
        self.interpreter_patch.start()

    def tearDown(self):
        self.session.shutdown()
        self.assertTrue(spin_until(lambda: bool(self.closes)), "Owned worker/recycling did not finish")
        self.worker_patch.stop()
        self.interpreter_patch.stop()
        self.session.deleteLater()
        QCoreApplication.processEvents()
        if self.root.exists():
            send2trash(str(self.root))

    def submit(self, job, source=None, offset=0):
        return self.session.submit(str(source or self.source), job, offset_seconds=offset,
                                   service_root=str(self.root), device="cpu")

    def test_reuses_worker_and_offsets_incremental_and_final_cues(self):
        self.assertTrue(self.submit("first", offset=8))
        self.assertFalse(self.submit("refused-while-busy"))
        self.assertTrue(spin_until(lambda: not self.session.busy))
        partial = next(e for e in self.events if e["event"] == "cues")
        result = next(e for e in self.events if e["event"] == "result")
        self.assertEqual(partial["cues"], result["cues"])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (8.1, 9.2))
        self.assertEqual(result["detected_language"], "en")
        self.assertTrue(self.submit("second"))
        self.assertTrue(spin_until(lambda: not self.session.busy))
        models = [e for e in self.events if e["event"] == "model"]
        self.assertEqual(models[0]["worker_pid"], models[1]["worker_pid"])
        self.assertTrue(models[1]["reused"])
        self.assertEqual(list((self.root / "jobs").iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b"source owned by caller")

    def test_cancel_retains_worker_and_shutdown_is_nonblocking(self):
        self.assertTrue(self.submit("cancel-me", self.slow))
        self.assertTrue(spin_until(lambda: any(e["event"] == "model" for e in self.events)))
        original_pid = self.session.process.processId()
        self.session.cancel()
        self.assertTrue(spin_until(lambda: not self.session.busy))
        self.assertTrue(any(e["event"] == "cancelled" for e in self.events))
        self.assertFalse(any(e["event"] == "result" for e in self.events))
        self.assertEqual(original_pid, self.session.process.processId())
        self.assertTrue(self.submit("close-me", self.slow))
        before = time.monotonic()
        self.session.shutdown()
        self.assertLess(time.monotonic() - before, .2)
        self.assertTrue(spin_until(lambda: bool(self.closes)))
        self.assertIsNone(self.session.process)

    def test_invalid_input_creates_no_worker_or_job_directory(self):
        self.assertFalse(self.submit("missing", self.root / "missing.wav"))
        self.assertIsNone(self.session.process)
        self.assertFalse((self.root / "jobs").exists())
        self.assertEqual(self.events[-1]["event"], "error")


class CaptureThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_threaded_capture_writes_chunks_and_releases_endpoint(self):
        root = Path(tempfile.mkdtemp(prefix="trancy-capture-tests-"))
        chunks, errors, finished = [], [], []

        class Stream:
            def __init__(self, callback):
                self.callback = callback
                self.halt = threading.Event()
                self.thread = None
                self.closed = False

            def get_time(self):
                return 100.0

            def start_stream(self):
                def produce():
                    frames = 0
                    pcm = np.full(160, 1000, dtype="<i2").tobytes()
                    while not self.halt.wait(.005):
                        _, status = self.callback(pcm, 160, {"input_buffer_adc_time": 100 + frames / 8000}, 0)
                        frames += 160
                        if status != 0:
                            return
                self.thread = threading.Thread(target=produce)
                self.thread.start()

            def is_active(self):
                return self.thread is not None and self.thread.is_alive()

            def stop_stream(self):
                self.halt.set()
                if self.thread is not None:
                    self.thread.join(timeout=1)

            def close(self):
                self.stop_stream()
                self.closed = True

        class Manager:
            terminated = False
            stream = None

            def get_default_wasapi_loopback(self):
                return dict(index=7)

            def get_loopback_device_info_generator(self):
                return iter([dict(index=7, name="controlled loopback", maxInputChannels=1,
                                  defaultSampleRate=8000, isLoopbackDevice=True)])

            def get_device_info_by_index(self, index):
                return dict(isLoopbackDevice=True)

            def open(self, **options):
                self.stream = Stream(options["stream_callback"])
                return self.stream

            def terminate(self):
                self.terminated = True

        manager = Manager()

        class Module:
            paContinue, paComplete, paAbort, paInputOverflow, paInt16 = 0, 1, 2, 4, 8

            def PyAudio(self):
                return manager

        capture = audio_capture.SystemAudioCapture()
        capture.chunk_ready.connect(lambda *args: chunks.append(args))
        capture.error.connect(errors.append)
        capture.stopped.connect(lambda: finished.append(True))
        try:
            with patch.object(audio_capture, "_pyaudio_module", return_value=Module()):
                capture.start(str(root), chunk_seconds=.5)
                self.assertTrue(spin_until(lambda: bool(chunks)))
                started = time.monotonic()
                capture.stop()
                self.assertLess(time.monotonic() - started, .1)
                self.assertTrue(spin_until(lambda: bool(finished)))
            self.assertEqual(errors, [])
            self.assertTrue(manager.terminated)
            self.assertTrue(manager.stream.closed)
            self.assertFalse(manager.stream.is_active())
            self.assertFalse(capture.running)
            with wave.open(chunks[0][0], "rb") as recording:
                self.assertEqual(recording.getframerate(), 8000)
                self.assertEqual(recording.getnchannels(), 1)
                self.assertEqual(recording.getnframes(), 4000)
            self.assertEqual(chunks[0][1:], (0.0, .5))
        finally:
            capture.stop()
            spin_until(lambda: not capture.running)
            capture.deleteLater()
            QCoreApplication.processEvents()
            send2trash(str(root))


if __name__ == "__main__":
    unittest.main()
