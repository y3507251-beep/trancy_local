"""Regression checks for original ASR word seconds through the desktop protocol."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop import asr_worker, recognition


def whisper_word(text, start, end):
    return SimpleNamespace(word=text, start=start, end=end)


def aligned_word(text, start, end):
    return SimpleNamespace(text=text, start_time=start, end_time=end)


class WordTimingTests(unittest.TestCase):
    def test_whisper_irregular_word_times_survive_sentence_split(self):
        segment = SimpleNamespace(words=[whisper_word(" Hello", .2, .6),
                                         whisper_word(" world!", 1.1, 1.9),
                                         whisper_word(" Next", 2.0, 2.2),
                                         whisper_word(" line.", 2.4, 3.1)])
        cues = asr_worker.normalize_timed_cues(asr_worker.segment_timed_cues(segment), 10)
        self.assertEqual([cue["text"] for cue in cues], ["Hello world!", "Next line."])
        self.assertEqual(cues[0]["words"], [dict(text=" Hello", start=.2, end=.6),
                                           dict(text=" world!", start=1.1, end=1.9)])
        self.assertEqual(cues[0]["timing_source"], "whisper")
        # The second word starts much later than a uniform half-sentence step.
        self.assertEqual(cues[0]["words"][1]["start"], 1.1)

    def test_sentence_split_keeps_original_gap_and_length_rules(self):
        segment = SimpleNamespace(words=[whisper_word(" one", .1, .3),
                                         whisper_word(" two", .5, .8),
                                         whisper_word(" three", 1.6, 2.0),
                                         whisper_word(" four", 2.1, 2.5)])
        cues = asr_worker.segment_timed_cues(segment, max_chars=9)
        self.assertEqual([cue["text"] for cue in cues], ["one two", "three", "four"])
        self.assertEqual([len(cue["words"]) for cue in cues], [2, 1, 1])

    def test_sentence_only_whisper_output_does_not_claim_word_alignment(self):
        segment = SimpleNamespace(start=2.0, end=4.5, text=" sentence only ", words=None)
        cue = asr_worker.normalize_timed_cues(asr_worker.segment_timed_cues(segment))[0]
        self.assertEqual(cue["words"], [])
        self.assertEqual(cue["timing_source"], "cue")
        self.assertEqual((cue["start"], cue["end"], cue["text"]), (2, 4.5, "sentence only"))

    def test_word_validation_clips_to_duration_and_filters_invalid_times(self):
        cue = dict(start=1, end=5, text="valid words", timing_source="whisper", words=[
            dict(text="valid", start=.5, end=1.5),
            dict(text=" words", start=2.2, end=4.6),
            dict(text="zero", start=3, end=3),
            dict(text="nan", start=math.nan, end=2),
            dict(text="negative", start=-1, end=2),
            dict(text="reverse", start=2, end=1),
            dict(text="outside", start=4, end=5),
        ])
        cleaned = asr_worker.normalize_timed_cues([cue], duration=4)[0]
        self.assertEqual(cleaned["words"], [dict(text="valid", start=1, end=1.5),
                                            dict(text=" words", start=2.2, end=4),
                                            dict(text="zero", start=3, end=3)])
        self.assertEqual(cleaned["end"], 4)
        self.assertEqual(cue["words"][0]["start"], .5)

    def test_all_invalid_words_downgrade_to_sentence_timing(self):
        cue = dict(start=0, end=3, text="hello", timing_source="qwen_aligner",
                   words=[dict(text="hello", start=math.inf, end=3)])
        self.assertEqual(asr_worker.normalize_timed_cues([cue])[0]["timing_source"], "cue")
        self.assertEqual(recognition.shifted_cues([cue], 10)[0]["timing_source"], "cue")

    def test_qwen_original_punctuation_and_chunk_offset_survive(self):
        text = "Hello, world! 下一句。"
        cues = asr_worker.aligned_timed_cues(text, [aligned_word("hello", .15, .7),
                                                  aligned_word("world", 1.1, 1.8),
                                                  aligned_word("下一句", 2.3, 2.8)],
                                             offset=40, duration=100)
        self.assertEqual([cue["text"] for cue in cues], ["Hello, world!", "下一句。"])
        self.assertEqual(cues[0]["words"], [dict(text="Hello,", start=40.15, end=40.7),
                                           dict(text=" world!", start=41.1, end=41.8)])
        self.assertEqual(cues[1]["words"][0]["start"], 42.3)
        self.assertEqual(cues[1]["timing_source"], "qwen_aligner")

    def test_qwen_merged_alignment_token_keeps_transcript(self):
        cues = asr_worker.aligned_timed_cues("am/is/are.", [aligned_word("amisare", .2, 1.7)])
        self.assertEqual(cues[0]["text"], "am/is/are.")
        self.assertEqual(cues[0]["words"], [dict(text="am/is/are.", start=.2, end=1.7)])

    def test_qwen_rejects_mismatched_or_missing_alignment(self):
        for text, words in [("Hello world", [aligned_word("Hello", 0, 1)]),
                            ("Hello", [aligned_word("different", 0, 1)]),
                            ("Hello", [aligned_word("Hello", math.nan, 1)]),
                            ("Hello", [])]:
            with self.subTest(text=text, words=words), self.assertRaises(ValueError):
                asr_worker.aligned_timed_cues(text, words)

    def test_source_offset_shifts_sentence_and_words_once_without_mutation(self):
        cue = dict(start=40.1, end=42, text="hello world", timing_source="qwen_aligner",
                   words=[dict(text="hello", start=40.1, end=40.7),
                          dict(text=" world", start=41.3, end=42)])
        shifted = recognition.shifted_cues([cue], 12.25)[0]
        self.assertEqual((shifted["start"], shifted["end"]), (52.35, 54.25))
        self.assertEqual(shifted["words"][1], dict(text=" world", start=53.55, end=54.25))
        self.assertEqual(cue["words"][1]["start"], 41.3)

    def test_protocol_partial_and_final_shift_words_identically(self):
        session = recognition.ASRSession()
        task = dict(job_id="speech", offset_seconds=17, cancelled=False)
        session._active = task
        updates = []
        session.updated.connect(updates.append)
        cue = dict(start=.5, end=2.2, text="hello world", timing_source="whisper",
                   words=[dict(text="hello", start=.5, end=.8),
                          dict(text=" world", start=1.7, end=2.2)])
        session._handle_event(task, dict(event="state", new_cues=[cue], detected_language="English"))
        with patch.object(session, "_finish", side_effect=lambda job, payload: updates.append(payload)):
            session._handle_event(task, dict(event="result", cues=[cue], detected_language="English"))
        partial = next(update for update in updates if update.get("event") == "cues")
        final = next(update for update in updates if update.get("event") == "result")
        self.assertEqual(partial["cues"], final["cues"])
        self.assertEqual(final["cues"][0]["words"][1]["start"], 18.7)
        self.assertEqual(final["detected_language"], "en")


class DesktopEnginePathTests(unittest.TestCase):
    def test_whisper_actual_desktop_adapter_retains_partial_and_final_words(self):
        """Drive the adapter with a model double, rather than just testing helpers."""
        segment = SimpleNamespace(end=2.2, words=[whisper_word(" Hello", .4, .8),
                                                   whisper_word(" world.", 1.4, 2.2)])
        calls = []

        class Model:
            def transcribe(self, waveform, **kwargs):
                calls.append(kwargs)
                return iter([segment]), SimpleNamespace(language="en", duration=5)

        model = Model()

        class SharedManagerDouble:
            def load_model(self, job_id, name, device):
                self.models[(name, device)] = model
                return model

            def update(self, job_id, **values):
                self.jobs[job_id].update(values)
                self.save(self.jobs[job_id])

            def transcribe(self, job_id, audio, options, device):
                with self.model_lock:
                    self.check_cancel(job_id)
                    return self._transcribe(job_id, audio, options, device)

        service = ModuleType("service")
        service.engine = SimpleNamespace(JobManager=SharedManagerDouble)
        audio = ModuleType("service.audio")
        audio.decode_audio = lambda path, cancel: [0.0] * 80000
        modules = {"service": service, "service.audio": audio, "faster_whisper": ModuleType("faster_whisper")}
        events = []
        options = dict(model="small", device="cpu", language="auto", batch_size=1, asr_context="")
        with patch.dict(sys.modules, modules):
            engine = asr_worker.WhisperEngine("small", events.append)
            engine.cancel = threading.Event()
            with patch.object(engine, "begin", return_value=(Path("controlled.wav"),
                                                            dict(duration=5, audio_duration=5), options)):
                result = engine.transcribe({}, engine.cancel)
        partial = next(event for event in events if event.get("new_cues"))
        self.assertEqual(result["cues"], partial["new_cues"])
        self.assertEqual(result["cues"][0]["words"][1]["start"], 1.4)
        self.assertEqual(result["cues"][0]["timing_source"], "whisper")
        self.assertTrue(calls[0]["word_timestamps"])

    def test_qwen_adapter_offset_and_missing_spans_are_honest(self):
        transcripts = iter([SimpleNamespace(text="Hello.", language="English",
                                             time_stamps=SimpleNamespace(items=[aligned_word("Hello", .5, 1.3)])),
                            SimpleNamespace(text="Aligned next.", language="English",
                                             time_stamps=SimpleNamespace(items=[aligned_word("Aligned", .1, .5),
                                                                                aligned_word("next", .7, 1.4)])),
                            SimpleNamespace(text="Sentence without spans.", language="English", time_stamps=None)])

        class Model:
            def transcribe(self, **kwargs):
                self.kwargs = kwargs
                return [next(transcripts)]

        audio = ModuleType("service.audio")
        audio.decode_audio = lambda path, check: [0] * 128000
        qwen_cues = ModuleType("service.qwen_cues")
        qwen_cues.audio_ranges = lambda waveform: iter([(0, 64000), (64000, 96000), (96000, 128000)])
        engine = asr_worker.QwenEngine.__new__(asr_worker.QwenEngine)
        events = []
        asr_worker.EngineBase.__init__(engine, "qwen3-asr-1.7b", events.append)
        engine.cancel, engine.model = threading.Event(), Model()
        engine.torch = SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=type("ControlledOOM", (Exception,), {})))
        engine.load = lambda device: None
        with patch.dict(sys.modules, {"service.audio": audio, "service.qwen_cues": qwen_cues}):
            cues, language = engine.recognize(Path("controlled.wav"), dict(duration=8),
                                               dict(language="auto", batch_size=1), "cpu")
        self.assertEqual(cues[0]["words"], [dict(text="Hello.", start=.5, end=1.3)])
        self.assertEqual(cues[1]["words"], [dict(text="Aligned", start=4.1, end=4.5),
                                           dict(text=" next.", start=4.7, end=5.4)])
        self.assertEqual(cues[1]["timing_source"], "qwen_aligner")
        self.assertEqual(cues[2]["timing_source"], "cue")
        self.assertEqual(cues[2]["words"], [])
        self.assertEqual((cues[2]["start"], cues[2]["end"]), (6, 8))
        self.assertEqual(language, "English")
        self.assertTrue(engine.model.kwargs["return_time_stamps"])


if __name__ == "__main__":
    unittest.main()
