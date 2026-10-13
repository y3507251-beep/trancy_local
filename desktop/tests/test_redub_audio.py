"""Continuity, complete speech and pitch checks for optional prepared dubbing."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop.redub_audio import (SAMPLE_RATE, RedubError, build_segments, english_text,
                                      merge_overlapping, merge_short_phrases, normalized_cues, reference_candidates,
                                      render_mix, split_utterance, time_stretch_background,
                                      validate_segments)


def cue(start, end, text="你好", english="Hello there."):
    return {"start": start, "end": end, "text": text, "translation": english,
            "source_language": "zh", "target_language": "en"}


class PreparedAudioPlanTests(unittest.TestCase):
    def test_original_english_is_used_for_english_video(self):
        item = cue(0, 2, "Original English.", "中文")
        item["source_language"] = "en-US"
        self.assertEqual(english_text(item), "Original English.")

    def test_missing_translation_is_not_silently_skipped(self):
        with self.assertRaises(RedubError):
            normalized_cues([cue(0, 3, english="")], 10)

    def test_chinese_translation_cannot_be_spoken_as_english(self):
        with self.assertRaises(RedubError):
            english_text(cue(0, 2, english="还没有英文"))

    def test_english_without_language_metadata_works(self):
        self.assertEqual(english_text({"text": "A complete English sentence."}), "A complete English sentence.")

    def test_html_is_plain_speech(self):
        self.assertEqual(english_text(cue(0, 2, english="<b>Hello</b> &amp; goodbye.")), "Hello & goodbye.")

    def test_nonfinite_timestamp_rejected(self):
        with self.assertRaises(RedubError):
            normalized_cues([cue(float("nan"), 3)], 10)

    def test_cues_are_clipped_sorted_and_outside_removed(self):
        result = normalized_cues([cue(6, 11), cue(-1, 2), cue(12, 20)], 10)
        self.assertEqual([(c["start"], c["end"]) for c in result], [(0, 2), (6, 10)])

    def test_rolling_overlap_does_not_repeat_words(self):
        groups = merge_overlapping(normalized_cues([
            cue(0, 4, "今天我们", "Today we will"),
            cue(2, 6, "我们学习英语", "we will learn English.")], 10))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["english"], "Today we will learn English.")
        self.assertEqual(groups[0]["text"], "今天我们学习英语")

    def test_neighboring_cues_are_not_mistaken_for_overlap(self):
        self.assertEqual(len(merge_overlapping(normalized_cues([cue(0, 2), cue(2, 4)], 10))), 2)

    def test_english_substring_does_not_drop_a_real_word(self):
        groups = merge_overlapping(normalized_cues([
            cue(0, 2, english="The conversation"), cue(1, 3, english="on this subject")], 4))
        self.assertEqual(groups[0]["english"], "The conversation on this subject")

    def test_long_speech_extends_video_window_instead_of_cutting(self):
        groups = merge_overlapping(normalized_cues([cue(1, 3)], 10))
        result = build_segments(groups, [6], 10)
        self.assertEqual([s["kind"] for s in result], ["gap", "speech", "gap"])
        length = result[1]["audio_end"] - result[1]["audio_start"]
        self.assertAlmostEqual(length, 2.6 / .75, places=4)
        self.assertGreater(result[1]["speech_tempo"], 1)
        self.assertGreaterEqual(result[1]["video_factor"], .75)
        validate_segments(result, 10, result[-1]["audio_end"])

    def test_short_speech_does_not_accelerate_video(self):
        result = build_segments(normalized_cues([cue(0, 8)], 8), [3], 8)
        self.assertEqual(result[0]["audio_end"], 8)

    def test_last_word_gets_complete_tail_without_next_sentence(self):
        result = build_segments(normalized_cues([cue(4, 5)], 5), [2.7], 5)
        self.assertAlmostEqual(result[-1]["audio_end"], 4 + 1 / .75, places=4)
        self.assertGreater(result[-1]["speech_tempo"], 2)
        self.assertEqual(result[-1]["source_end"], 5)

    def test_crowded_short_captions_share_a_sentence_window(self):
        groups = normalized_cues([cue(0, .8, english="We can explain"),
                                  cue(.8, 1.7, english="this idea with a simple example.")], 3)
        combined = merge_short_phrases(groups)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["english"], "We can explain this idea with a simple example.")

    def test_nearby_gap_is_used_before_slowing_video(self):
        groups = normalized_cues([cue(0, 2), cue(2.6, 5)], 5)
        segments = build_segments(groups, [2.3, 1], 5)
        self.assertEqual(segments[0]["video_factor"], 1)
        self.assertGreater(segments[0]["source_end"], 2.3)
        self.assertEqual(segments[-1]["video_factor"], 1)

    def test_minimum_video_factor_holds_for_extreme_long_english(self):
        groups = normalized_cues([cue(0, 1), cue(1, 5)], 5)
        segments = build_segments(groups, [15, 1], 5)
        self.assertTrue(all(segment["video_factor"] >= .75 for segment in segments))
        self.assertGreater(segments[0]["speech_tempo"], 10)

    def test_fractional_timelines_are_continuous_to_one_sample(self):
        groups = normalized_cues([cue(.01003, 1.25899), cue(1.25899, 2.1024)], 3.7894)
        result = build_segments(groups, [2.79803, 1.74927], 3.7894)
        validate_segments(result, round(3.7894 * SAMPLE_RATE) / SAMPLE_RATE, result[-1]["audio_end"])
        for previous, current in zip(result, result[1:]):
            self.assertEqual(previous["source_end"], current["source_start"])
            self.assertEqual(previous["audio_end"], current["audio_start"])

    def test_mapping_gap_is_rejected(self):
        result = build_segments(normalized_cues([cue(1, 3)], 10), [4], 10)
        result[1]["audio_start"] += .1
        with self.assertRaises(RedubError):
            validate_segments(result, 10, result[-1]["audio_end"])

    def test_reference_transcript_uses_complete_caption_boundaries(self):
        groups = normalized_cues([cue(1, 4, "第一句"), cue(4.1, 7, "第二句")], 10)
        candidates = reference_candidates(groups)
        best = next(item for item in candidates if item["preferred"])
        self.assertEqual((best["start"], best["end"], best["text"]), (1, 7, "第一句 第二句"))

    def test_reference_does_not_cross_large_untranscribed_gap(self):
        groups = normalized_cues([cue(0, 2), cue(5, 7)], 10)
        self.assertFalse(reference_candidates(groups))

    def test_long_transcript_is_not_cropped_with_incorrect_text(self):
        self.assertFalse(reference_candidates(normalized_cues([cue(0, 20)], 20)))

    def test_very_long_english_is_split_without_losing_words(self):
        text = " ".join("word" + str(i) for i in range(500))
        parts = split_utterance(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), text)
        self.assertTrue(all(len(part) <= 280 for part in parts))


DSP_AVAILABLE = all(importlib.util.find_spec(name) for name in ("av", "numpy", "soundfile"))


@unittest.skipUnless(DSP_AVAILABLE, "optional re-dubbing DSP packages are not in the UI runtime")
class PreparedAudioRenderTests(unittest.TestCase):
    def test_background_stretch_retains_pitch_and_exact_duration(self):
        import numpy as np
        rate = SAMPLE_RATE
        tone = .1 * np.sin(2 * np.pi * 440 * np.arange(rate * 2) / rate)
        original = np.stack([tone, tone], axis=1).astype("float32")
        longer = time_stretch_background(original, rate * 4)
        self.assertEqual(longer.shape, (rate * 4, 2))
        center = longer[rate:rate * 3, 0]
        peak = np.argmax(np.abs(np.fft.rfft(center))) * rate / len(center)
        self.assertLess(abs(peak - 440), 2)
        self.assertGreater(float(np.sqrt(np.mean(center ** 2))), .05)

    def test_very_long_segment_chains_atempo_without_changing_pitch(self):
        import numpy as np
        tone = .1 * np.sin(2 * np.pi * 330 * np.arange(SAMPLE_RATE) / SAMPLE_RATE)
        result = time_stretch_background(np.stack([tone, tone], axis=1).astype("float32"), SAMPLE_RATE * 5)
        center = result[SAMPLE_RATE:SAMPLE_RATE * 4, 0]
        peak = np.argmax(np.abs(np.fft.rfft(center))) / 3
        self.assertLess(abs(peak - 330), 3)

    def test_render_keeps_complete_speech_and_background_gaps(self):
        import numpy as np
        import soundfile as sf
        root = Path(__file__).resolve().parents[1] / "data" / "validation" / "redubbing" / uuid.uuid4().hex
        root.mkdir(parents=True)
        bed = np.full((SAMPLE_RATE * 3, 2), .03, dtype="float32")
        voice = np.full((SAMPLE_RATE * 2, 1), .2, dtype="float32")
        # Explicit tail marker detects the old last-word truncation behavior.
        voice[-1000:] = .6
        sf.write(str(root / "bed.wav"), bed, SAMPLE_RATE, subtype="FLOAT")
        sf.write(str(root / "voice.wav"), voice, SAMPLE_RATE, subtype="FLOAT")
        segments = build_segments(normalized_cues([cue(1, 2)], 3), [2], 3)
        render_mix(root / "bed.wav", [root / "voice.wav"], segments, root / "mix.wav")
        mixed, rate = sf.read(str(root / "mix.wav"), always_2d=True)
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(len(mixed), round(segments[-1]["audio_end"] * SAMPLE_RATE))
        speech = next(segment for segment in segments if segment["kind"] == "speech")
        end = round(speech["audio_end"] * SAMPLE_RATE)
        self.assertGreater(float(mixed[end - SAMPLE_RATE // 4:end].max()), .45)
        self.assertAlmostEqual(float(mixed[-100, 0]), .03, places=3)


if __name__ == "__main__":
    unittest.main()
