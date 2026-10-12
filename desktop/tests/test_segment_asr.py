"""Window extraction decodes only nearby audio; ASR keeps absolute cue times."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
import uuid
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop import asr_worker, recognition


def validation_directory(label):
    directory = Path(__file__).resolve().parents[1] / "data" / "validation" / "segment-asr" / (label + "-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    return directory


EXTRACT = r'''
import json, sys, wave
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from trancy_desktop.asr_worker import extract_audio
events = []
metadata = extract_audio(Path(sys.argv[2]), Path(sys.argv[3]), lambda: None,
                         lambda position, duration: events.append([position, duration]),
                         start_seconds=float(sys.argv[4]), duration_seconds=float(sys.argv[5]))
with wave.open(sys.argv[3]) as audio:
    samples = audio.readframes(audio.getnframes())
    values = memoryview(samples).cast('h')
    summary = dict(samples=len(values), minimum=min(values), maximum=max(values), first=values[0], last=values[-1])
print(json.dumps(dict(metadata=metadata, samples=summary, progress=events)))
'''


class SegmentProtocolTests(unittest.TestCase):
    def test_seek_into_long_silent_gap_returns_only_requested_silence(self):
        import numpy as np
        root = validation_directory("silent-window")
        frame = SimpleNamespace(time=180, samples=16000, to_ndarray=lambda: np.full((1, 16000), 500, dtype="<i2"))
        seeks = []

        class Container:
            start_time = 0
            duration = 200000000
            streams = SimpleNamespace(audio=[SimpleNamespace(duration=None, codec_context=SimpleNamespace(name="pcm_s16le"))])
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def decode(self, stream): return iter([frame])
            def seek(self, timestamp, **kwargs): seeks.append(timestamp)

        module = ModuleType("av")
        module.time_base = 1000000
        module.open = lambda *args, **kwargs: Container()
        module.AudioResampler = lambda **kwargs: SimpleNamespace(resample=lambda item: [item] if item is not None else [])
        with patch.dict(sys.modules, {"av": module}):
            metadata = asr_worker.extract_audio(root / "gap.media", root / "window.wav", lambda: None,
                                                lambda position, duration: None,
                                                start_seconds=50, duration_seconds=20)
        self.assertEqual(seeks, [50000000])
        self.assertEqual((metadata["duration"], metadata["segment_start"], metadata["segment_end"]), (20, 50, 70))
        with wave.open(str(root / "window.wav")) as output:
            self.assertEqual(output.getnframes(), 320000)
            self.assertEqual(output.readframes(320000), bytes(640000))

    def test_early_eof_with_unknown_duration_does_not_claim_unread_requested_tail(self):
        import numpy as np
        root = validation_directory("unknown-duration")
        frame = SimpleNamespace(time=0, samples=16000, to_ndarray=lambda: np.full((1, 16000), 500, dtype="<i2"))

        class Container:
            start_time = 0
            duration = None
            streams = SimpleNamespace(audio=[SimpleNamespace(duration=None, codec_context=SimpleNamespace(name="pcm_s16le"))])
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def decode(self, stream): return iter([frame])

        module = ModuleType("av")
        module.time_base = 1000000
        module.open = lambda *args, **kwargs: Container()
        module.AudioResampler = lambda **kwargs: SimpleNamespace(resample=lambda item: [item] if item is not None else [])
        progress = []
        with patch.dict(sys.modules, {"av": module}):
            metadata = asr_worker.extract_audio(root / "unknown.media", root / "window.wav", lambda: None,
                                                lambda position, duration: progress.append((position, duration)),
                                                duration_seconds=20)
        self.assertEqual((metadata["duration"], metadata["segment_end"], metadata["source_duration"]), (1, 1, 0))
        self.assertEqual(progress[-1], (1, 1))
        with wave.open(str(root / "window.wav")) as output:
            self.assertEqual(output.getnframes(), 16000)

    def test_partial_and_final_window_words_reach_original_playback_position(self):
        session = recognition.ASRSession()
        task = dict(job_id="window", start_seconds=320, duration_seconds=20, offset_seconds=5, cancelled=False)
        session._active = task
        events = []
        session.updated.connect(events.append)
        cue = dict(start=.4, end=2.5, text="window words", timing_source="whisper",
                   words=[dict(text="window", start=.4, end=1.1), dict(text=" words", start=1.8, end=2.5)])
        session._handle_event(task, dict(event="state", new_cues=[cue], detected_language="English",
                                         segment_start=320, segment_end=340, duration=20, source_duration=1000))
        with patch.object(session, "_finish", side_effect=lambda job, payload: events.append(payload)):
            session._handle_event(task, dict(event="result", cues=[cue], detected_language="English",
                                             segment_start=320, segment_end=340, duration=20, source_duration=1000))
        partial = next(event for event in events if event["event"] == "cues")
        final = next(event for event in events if event["event"] == "result")
        self.assertEqual(partial["cues"], final["cues"])
        self.assertEqual((final["cues"][0]["start"], final["cues"][0]["end"]), (325.4, 327.5))
        self.assertEqual(final["cues"][0]["words"][1]["start"], 326.8)
        self.assertEqual((final["segment_start"], final["segment_end"], final["duration"]), (325, 345, 20))
        self.assertEqual(cue["words"][1]["start"], 1.8)

    def test_invalid_window_does_not_start_worker_or_create_temporary_job(self):
        root = validation_directory("invalid-window")
        source = root / "source.wav"
        source.write_bytes(b"caller-owned")
        session = recognition.ASRSession(runtime_dir=root / "jobs")
        events = []
        session.updated.connect(events.append)
        for start, duration in [(-1, 20), (math.nan, 20), (0, 0), (0, math.inf)]:
            self.assertFalse(session.submit(str(source), "invalid-window", service_root=str(root),
                                             start_seconds=start, duration_seconds=duration))
        self.assertIsNone(session.process)
        self.assertFalse((root / "jobs").exists())
        self.assertEqual(len(events), 4)


class RealWindowExtractionTests(unittest.TestCase):
    def setUp(self):
        self.python = Path(recognition.DEFAULT_SERVICE_ROOT) / ".venv" / "Scripts" / "python.exe"
        if not self.python.is_file():
            self.skipTest("Existing SubtitleService PyAV interpreter unavailable")
        self.root = validation_directory("real-window")
        self.source = self.root / "long.wav"
        # Distinct sample values reveal accidental decoding/recognition from
        # time zero, rather than merely checking generated command arguments.
        with wave.open(str(self.source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes((1000).to_bytes(2, "little", signed=True) * 16000 * 10)
            audio.writeframes((2000).to_bytes(2, "little", signed=True) * 16000 * 10)

    def extract(self, start, duration):
        target = self.root / f"window-{start}.wav"
        result = subprocess.run([str(self.python), "-B", "-c", EXTRACT, str(Path(__file__).resolve().parents[1]),
                                 str(self.source), str(target), str(start), str(duration)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_late_window_contains_late_audio_and_not_the_start_of_the_file(self):
        before = self.source.read_bytes()
        result = self.extract(15.25, .5)
        self.assertEqual(result["samples"], dict(samples=8000, minimum=2000, maximum=2000, first=2000, last=2000))
        metadata = result["metadata"]
        self.assertEqual((metadata["duration"], metadata["audio_duration"]), (.5, .5))
        self.assertEqual((metadata["segment_start"], metadata["segment_end"], metadata["source_duration"]), (15.25, 15.75, 20))
        self.assertEqual(result["progress"][-1], [.5, .5])
        self.assertEqual(self.source.read_bytes(), before)

    def test_subsecond_window_crossing_boundary_preserves_timing_and_length(self):
        result = self.extract(9.75, .5)
        self.assertEqual(result["samples"]["samples"], 8000)
        self.assertEqual((result["samples"]["first"], result["samples"]["last"]), (1000, 2000))
        self.assertEqual((result["samples"]["minimum"], result["samples"]["maximum"]), (1000, 2000))
        self.assertEqual(result["metadata"]["segment_end"], 10.25)

    def test_final_window_clamps_to_end_without_padding_to_whole_video_duration(self):
        result = self.extract(19.75, 5)
        self.assertEqual(result["samples"]["samples"], 4000)
        self.assertEqual(result["metadata"]["duration"], .25)
        self.assertEqual(result["metadata"]["segment_end"], 20)


if __name__ == "__main__":
    unittest.main()
