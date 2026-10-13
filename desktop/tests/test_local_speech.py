"""Offline synthesis lifecycle and cache checks without a model or network."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
import wave
from array import array

from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop.speech import LocalSpeech, playback_wave, speech_cache_key, wave_info


class PlaybackWaveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "data" / "validation" / "dubbing" / ("padding-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def write_wave(self, values):
        source = self.root / "original.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(10000)
            audio.writeframes(array("h", values).tobytes())
        return source

    def test_only_outer_padding_is_removed_and_inner_pause_and_source_survive(self):
        data = [0] * 2000 + [1000] * 3000 + [0] * 1500 + [-1000] * 3000 + [0] * 7000
        source = self.write_wave(data)
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        prepared = playback_wave(source)
        self.assertAlmostEqual(prepared["duration"], .95)
        self.assertAlmostEqual(prepared["raw_duration"], 1.65)
        with wave.open(prepared["path"], "rb") as audio:
            kept = array("h")
            kept.frombytes(audio.readframes(audio.getnframes()))
        self.assertEqual(list(kept), data[1200:10700])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(playback_wave(source)["path"], prepared["path"])

    def test_silence_or_already_tight_sentence_is_retained(self):
        source = self.write_wave([0] * 2000)
        self.assertEqual(playback_wave(source)["path"], str(source.resolve()))
        source = self.write_wave([1000] * 2000)
        self.assertEqual(playback_wave(source)["path"], str(source.resolve()))

    def test_soft_edges_are_preserved_by_padding(self):
        data = [0] * 2000 + [15] * 500 + [1000] * 2000 + [15] * 500 + [0] * 7000
        source = self.write_wave(data)
        prepared = playback_wave(source)
        with wave.open(prepared["path"], "rb") as audio:
            kept = array("h")
            kept.frombytes(audio.readframes(audio.getnframes()))
        self.assertEqual(list(kept).count(15), 1000)

    def test_damaged_playback_copy_is_rebuilt_without_rerendering_or_deleting(self):
        source = self.write_wave([0] * 2000 + [1000] * 3000 + [0] * 7000)
        prepared = playback_wave(source)
        target = Path(prepared["path"])
        target.write_bytes(b"damaged playback copy")
        repaired = playback_wave(source)
        self.assertEqual(repaired["path"], prepared["path"])
        self.assertEqual(repaired["duration"], prepared["duration"])
        retained = list(target.parent.glob("invalid-*.wav"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"damaged playback copy")
        self.assertTrue(source.is_file())


def spin_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    QCoreApplication.processEvents()
    return predicate()


FAKE_WORKER = r'''
import json, os, struct, sys, time, wave
for line in sys.stdin:
    request = json.loads(line)
    if request['command'] == 'shutdown': break
    rid = request['request_id']
    if request['text'] == 'delay': time.sleep(5)
    if request['voice'] == 'missing':
        print(json.dumps(dict(event='failed', request_id=rid, code='voice_unavailable')), flush=True)
        continue
    if request['text'] == 'invalid audio':
        with open(request['output'], 'wb') as stream: stream.write(b'invalid')
    else:
        with wave.open(request['output'], 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            audio.writeframes(struct.pack('<h', 4000) * 2205)
    print(json.dumps(dict(event='ready', request_id=rid, path=request['output'],
                         voice='controlled English voice', render_ms=1)), flush=True)
'''


class FakeBackend:
    def __init__(self, script):
        self.script = script
        self.launches = 0

    def command(self, cache_dir):
        self.launches += 1
        return sys.executable, ["-B", "-u", str(self.script)]


class LocalSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        # Retained artifacts, never auto-unlinked by TemporaryDirectory.
        self.root = Path(__file__).resolve().parents[1] / "data" / "validation" / "dubbing" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        script = self.root / "fake_speech_worker.py"
        script.write_text(FAKE_WORKER, encoding="utf-8", newline="\n")
        self.backend = FakeBackend(script)
        self.session = LocalSpeech(cache_dir=self.root / "cache", backend=self.backend)
        self.ready, self.failed, self.closed = [], [], []
        self.session.ready.connect(self.ready.append)
        self.session.failed.connect(self.failed.append)
        self.session.closed.connect(lambda: self.closed.append(True))

    def tearDown(self):
        self.session.shutdown()
        self.assertTrue(spin_until(lambda: self.session.is_closed))
        self.session.deleteLater()
        QCoreApplication.processEvents()

    def test_disabled_or_invalid_input_never_starts_worker(self):
        self.assertIsNone(self.session.process)
        self.assertFalse((self.root / "cache").exists())
        self.assertFalse(self.session.request("", "empty", 0))
        self.assertFalse(self.session.request("hello", "bad-rate", 0, rate=11))
        self.assertIsNone(self.session.process)
        self.assertEqual(self.backend.launches, 0)
        self.assertEqual({event["code"] for event in self.failed}, {"invalid_request"})

    def test_renders_and_reuses_worker_and_exact_pcm_cache(self):
        self.assertTrue(self.session.request("Hello world.", "one", 1))
        self.assertTrue(spin_until(lambda: len(self.ready) == 1))
        first = self.ready[0]
        self.assertEqual((first["token"], first["generation"], first["cached"]), ("one", 1, False))
        self.assertAlmostEqual(first["duration"], .1)
        pid = self.session.process.processId()
        self.assertTrue(self.session.request("Another sentence.", "two", 1))
        self.assertTrue(spin_until(lambda: len(self.ready) == 2))
        self.assertEqual(self.session.process.processId(), pid)
        self.assertEqual(self.backend.launches, 1)
        self.assertTrue(self.session.request("Hello  world.\n", "cached", 1))
        self.assertTrue(spin_until(lambda: len(self.ready) == 3))
        self.assertTrue(self.ready[-1]["cached"])
        self.assertEqual(self.ready[-1]["path"], first["path"])
        self.assertEqual(self.ready[-1]["voice"], "controlled English voice")
        self.assertEqual(self.backend.launches, 1)
        self.assertFalse(self.failed)

    def test_restart_can_hit_cache_without_a_render_process(self):
        self.assertTrue(self.session.request("Keep this cached.", "old", 1))
        self.assertTrue(spin_until(lambda: bool(self.ready)))
        cached_path = Path(self.ready[0]["path"])
        self.session.shutdown()
        self.assertTrue(spin_until(lambda: self.session.is_closed))
        self.session = LocalSpeech(cache_dir=self.root / "cache", backend=self.backend)
        self.ready = []
        self.session.ready.connect(self.ready.append)
        self.assertTrue(self.session.request("Keep this cached.", "new", 2))
        self.assertTrue(spin_until(lambda: bool(self.ready)))
        self.assertTrue(self.ready[0]["cached"])
        self.assertEqual(Path(self.ready[0]["path"]), cached_path)
        self.assertIsNone(self.session.process)
        self.assertEqual(self.backend.launches, 1)

    def test_reset_discards_pending_and_kills_old_active_generation(self):
        self.assertTrue(self.session.request("delay", "old-active", 2))
        self.assertTrue(self.session.request("Should not render.", "old-pending", 2))
        self.assertTrue(spin_until(lambda: self.session.process is not None and self.session.process.processId() > 0))
        before = time.monotonic()
        self.session.reset(3)
        self.assertLess(time.monotonic() - before, .1)
        self.assertFalse(self.session.request("Late request.", "late", 2))
        self.assertTrue(self.session.request("New position.", "new", 3))
        self.assertTrue(spin_until(lambda: bool(self.ready)))
        self.assertEqual([(event["token"], event["generation"]) for event in self.ready], [("new", 3)])
        self.assertFalse(self.failed)
        self.assertEqual(self.backend.launches, 2)
        self.assertFalse((self.root / "cache" / (speech_cache_key("Should not render.") + ".wav")).exists())

    def test_reset_before_process_launch_skips_old_work(self):
        self.assertTrue(self.session.request("Old position.", "old", 1))
        self.session.reset(2)
        self.assertTrue(self.session.request("New position.", "new", 2))
        self.assertTrue(spin_until(lambda: bool(self.ready)))
        self.assertEqual([event["token"] for event in self.ready], ["new"])
        self.assertEqual(self.backend.launches, 1)

    def test_bounded_queue_reuses_same_pending_token(self):
        self.session.max_queue = 2
        self.assertTrue(self.session.request("delay", "one", 0))
        self.assertTrue(self.session.request("Two.", "two", 0))
        self.assertTrue(self.session.request("Two.", "two", 0))
        self.assertFalse(self.session.request("Three.", "three", 0))
        self.assertEqual(self.session.pending_count, 2)
        self.assertEqual(self.failed[-1]["code"], "queue_full")

    def test_missing_voice_and_invalid_audio_have_fixed_diagnostics(self):
        self.assertTrue(self.session.request("Hello.", "missing", 0, voice="missing"))
        self.assertTrue(self.session.request("invalid audio", "bad", 0))
        self.assertTrue(self.session.request("Next valid sentence.", "good", 0))
        self.assertTrue(spin_until(lambda: len(self.failed) == 2 and len(self.ready) == 1))
        self.assertEqual([event["code"] for event in self.failed], ["voice_unavailable", "invalid_audio"])
        self.assertEqual([event["token"] for event in self.ready], ["good"])
        self.assertEqual(self.backend.launches, 1)

    def test_timeout_terminates_worker_then_runs_next_request(self):
        self.session.timeout_ms = 250
        self.assertTrue(self.session.request("delay", "slow", 0))
        self.assertTrue(self.session.request("Resume.", "fast", 0))
        self.assertTrue(spin_until(lambda: bool(self.failed) and bool(self.ready)))
        self.assertEqual(self.failed[0]["code"], "render_timeout")
        self.assertEqual(self.ready[0]["token"], "fast")
        self.assertEqual(self.backend.launches, 2)

    def test_shutdown_active_render_is_nonblocking_and_emits_once(self):
        self.assertTrue(self.session.request("delay", "closing", 0))
        self.assertTrue(spin_until(lambda: self.session.process is not None and self.session.process.processId() > 0))
        before = time.monotonic()
        self.session.shutdown()
        self.session.shutdown()
        self.assertLess(time.monotonic() - before, .1)
        self.assertTrue(spin_until(lambda: self.session.is_closed, 2))
        self.assertEqual(self.closed, [True])
        self.assertFalse(self.ready)
        self.assertFalse(self.failed)

    def test_engine_start_failure_is_fixed_and_queue_is_drained(self):
        self.backend.command = lambda cache: (str(self.root / "missing.exe"), [])
        self.assertTrue(self.session.request("First.", "one", 0))
        self.assertTrue(self.session.request("Second.", "two", 0))
        self.assertTrue(spin_until(lambda: len(self.failed) == 2 and not self.session.busy))
        self.assertEqual([event["code"] for event in self.failed],
                         ["engine_unavailable", "engine_unavailable"])
        self.assertFalse(self.ready)

    def test_close_before_queued_dispatch_never_starts_worker(self):
        self.assertTrue(self.session.request("Do not start.", "cancelled", 0))
        self.session.shutdown()
        self.assertTrue(self.session.is_closed)
        QCoreApplication.processEvents()
        self.assertEqual(self.backend.launches, 0)
        self.assertFalse(self.ready)
        self.assertFalse(self.failed)

    def test_corrupt_cache_is_retained_when_valid_wav_is_published(self):
        self.session.cache_dir.mkdir()
        target = self.session.cache_dir / (speech_cache_key("Recover.") + ".wav")
        target.write_bytes(b"preserve damaged cache")
        self.assertTrue(self.session.request("Recover.", "recover", 0))
        self.assertTrue(spin_until(lambda: bool(self.ready)))
        self.assertAlmostEqual(wave_info(target)["duration"], .1)
        kept = list(self.session.cache_dir.glob("invalid-*.wav"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"preserve damaged cache")

    def test_cache_identity_covers_voice_and_synthesis_rate(self):
        values = [speech_cache_key("Hello.", voice="auto", rate=0),
                  speech_cache_key("Hello.", voice="David", rate=0),
                  speech_cache_key("Hello.", voice="auto", rate=1),
                  speech_cache_key("Another sentence.", voice="auto", rate=0)]
        self.assertEqual(len(set(values)), 4)


@unittest.skipUnless(os.name == "nt", "Windows local System.Speech integration")
class RealSystemSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_real_english_wav_and_no_such_voice(self):
        root = Path(__file__).resolve().parents[1] / "data" / "validation" / "dubbing" / ("real-" + uuid.uuid4().hex)
        session = LocalSpeech(cache_dir=root)
        ready, failed = [], []
        session.ready.connect(ready.append)
        session.failed.connect(failed.append)
        try:
            text = "Hello. This is a local English voice. Playback follows the video when you pause or seek."
            started = time.monotonic()
            self.assertTrue(session.request(text, "sample", 1))
            self.assertTrue(spin_until(lambda: bool(ready) or bool(failed), 15))
            self.assertFalse(failed, failed)
            event = ready[0]
            elapsed = time.monotonic() - started
            self.assertIn(event["voice"], ("Microsoft Zira Desktop", "Microsoft David Desktop", "Microsoft Hazel Desktop"))
            self.assertGreater(event["duration"], 2)
            self.assertLess(event["duration"], 15)
            with wave.open(event["path"], "rb") as audio:
                samples = audio.readframes(audio.getnframes())
                self.assertTrue(any(samples), "Synthesizer returned only silence")
                self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()), (1, 22050, 2))
            self.assertTrue(session.request("This voice does not exist.", "absent", 1,
                                            voice="Trancy missing English voice"))
            self.assertTrue(spin_until(lambda: bool(failed)))
            self.assertEqual(failed[0]["code"], "voice_unavailable")
            (root / "sample-report.json").write_text(json.dumps(dict(
                voice=event["voice"], duration_seconds=event["duration"],
                elapsed_seconds=round(elapsed, 4), render_ms=event.get("render_ms"),
                wave_bytes=Path(event["path"]).stat().st_size, sample_rate=event["sample_rate"],
                cache_path=event["path"], unavailable_voice_code=failed[0]["code"]),
                indent=2) + "\n", encoding="utf-8", newline="\n")
        finally:
            session.shutdown()
            self.assertTrue(spin_until(lambda: session.is_closed))
            session.deleteLater()
            QCoreApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
