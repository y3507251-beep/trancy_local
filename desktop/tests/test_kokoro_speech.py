"""Offline TTS protocol, voice identity and lazy runtime checks."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import unittest
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication
from trancy_desktop.kokoro_speech import (
    KokoroBackend, KokoroSpeech, KokoroUnavailable, KOKORO_ENGINE,
)
from trancy_desktop.kokoro_worker import serve, validate_request
from trancy_desktop.speech import speech_cache_key, ENGINE_VERSION


class Engine:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice):
        self.calls.append((text, voice))
        # Diagnostic output from an inference library cannot enter the protocol.
        print("test diagnostic")
        return struct.pack("<h", 6000) * 2400, 24000


class KokoroProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path(__file__).resolve().parents[1] / "data/validation/dubbing" /
                     ("kokoro-protocol-" + uuid.uuid4().hex))
        self.root.mkdir(parents=True)
        self.root = self.root.resolve()

    def request(self, text="Read this complete sentence naturally, including its final word.", voice="af_heart"):
        return dict(command="synthesize", request_id=uuid.uuid4().hex, text=text, voice=voice,
                    rate=0, output=str(self.root / ("pending-" + uuid.uuid4().hex + ".wav")))

    def run_worker(self, requests, engine=None):
        output, errors = io.StringIO(), io.StringIO()
        engine = engine or Engine()
        stream = io.StringIO("\n".join(json.dumps(request) for request in requests))
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            serve(self.root, engine, stream)
        return engine, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_both_voices_receive_full_sentences_and_reuse_one_engine(self):
        requests = [self.request(), self.request(voice="am_michael")]
        engine, events = self.run_worker(requests)
        self.assertEqual(engine.calls, [(request["text"], request["voice"]) for request in requests])
        self.assertEqual(events[0], dict(event="voices", voices=["af_heart", "am_michael"]))
        for request, event in zip(requests, events[1:]):
            self.assertEqual((event["event"], event["request_id"], event["voice"]),
                             ("ready", request["request_id"], request["voice"]))
            with wave.open(event["path"], "rb") as audio:
                self.assertEqual((audio.getnframes(), audio.getframerate(), audio.getsampwidth()), (2400, 24000, 2))
                self.assertEqual(audio.readframes(2400), struct.pack("<h", 6000) * 2400)

    def test_outside_output_and_existing_files_are_rejected_without_synthesis(self):
        outside = self.request()
        outside["output"] = str(self.root.parent / Path(outside["output"]).name)
        existing = self.request()
        Path(existing["output"]).write_bytes(b"preserve original")
        engine, events = self.run_worker([outside, existing])
        self.assertEqual(engine.calls, [])
        self.assertEqual([event["code"] for event in events[1:]], ["invalid_request"] * 2)
        self.assertEqual(Path(existing["output"]).read_bytes(), b"preserve original")

    def test_invalid_voice_empty_sentence_and_old_sapi_rates_never_generate(self):
        requests = [self.request(voice="auto"), self.request(text=""), dict(self.request(), rate=1)]
        engine, events = self.run_worker(requests)
        self.assertEqual(engine.calls, [])
        self.assertTrue(all(event["event"] == "failed" for event in events[1:]))

    def test_render_failure_returns_json_and_can_continue_with_a_new_request(self):
        class RecoveringEngine(Engine):
            def synthesize(self, text, voice):
                if text == "fail":
                    raise RuntimeError("controlled synthesis failure")
                return super().synthesize(text, voice)
        _, events = self.run_worker([self.request(text="fail"), self.request()], RecoveringEngine())
        self.assertEqual(events[1]["code"], "render_failed")
        self.assertEqual(events[2]["event"], "ready")

    def test_worker_rejects_malformed_request_ids_and_non_pending_file_names(self):
        for request in (dict(self.request(), request_id=None),
                        dict(self.request(), output=str(self.root / "original.wav"))):
            with self.assertRaises(ValueError):
                validate_request(request, self.root)

    def test_voice_and_engine_are_independent_cache_identities(self):
        keys = [speech_cache_key("Same sentence.", voice, engine=engine)
                for voice in ("af_heart", "am_michael") for engine in (KOKORO_ENGINE, ENGINE_VERSION)]
        self.assertEqual(len(set(keys)), 4)

    def test_missing_install_has_specific_diagnostic_before_starting_a_process(self):
        with self.assertRaises(KokoroUnavailable):
            KokoroBackend(self.root).command(self.root)

    def test_runtime_command_uses_local_files_and_separate_python(self):
        for name in ("base/python.exe", "models/kokoro-v1.0.int8.onnx", "models/voices-v1.0.bin"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"controlled fixture")
        (self.root / ".venv/Lib/site-packages").mkdir(parents=True)
        (self.root / ".venv/pyvenv.cfg").write_text("home = " + str(self.root / "base"), encoding="utf-8")
        program, arguments = KokoroBackend(self.root).command(self.root)
        self.assertEqual(Path(program), self.root / "base/python.exe")
        self.assertIn(str(self.root / ".venv/Lib/site-packages"), arguments)
        self.assertIn(str(self.root / "models/kokoro-v1.0.int8.onnx"), arguments)
        self.assertNotIn("http", " ".join(arguments))


class KokoroClientTests(unittest.TestCase):
    def test_constructing_or_selecting_a_voice_does_not_load_the_model(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        session = KokoroSpeech()
        failed = []
        session.failed.connect(failed.append)
        self.assertIsNone(session.process)
        self.assertEqual(session.engine_version, KOKORO_ENGINE)
        self.assertFalse(session.request("Sentence.", "one", 0, voice="missing"))
        self.assertIsNone(session.process)
        self.assertEqual(failed[0]["code"], "invalid_request")
        session.shutdown()
        self.assertTrue(session.is_closed)
        session.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
