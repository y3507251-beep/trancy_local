"""Bounded recognition and resumable caption cache, without an ASR model."""
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trancy_desktop.playback import PlaybackBuffer
from trancy_desktop.timeline import CaptionCache


class PlaybackBufferTests(unittest.TestCase):
    def test_initial_window_follows_playhead_and_is_short(self):
        buffer = PlaybackBuffer(duration=3600)
        self.assertEqual(buffer.next_window(400), (398, 410))

    def test_forward_buffer_is_bounded_and_does_not_chase_tiny_gaps(self):
        buffer = PlaybackBuffer(duration=3600)
        for expected in ((0, 12), (12, 32)):
            window = buffer.next_window(0)
            self.assertEqual(window, expected)
            buffer.mark(*window)
        self.assertIsNone(buffer.next_window(0))
        self.assertEqual(buffer.next_window(10), (32, 45))
        self.assertLess(buffer.coverage[-1][1], 40)

    def test_pause_stops_computation_after_small_buffer(self):
        buffer = PlaybackBuffer(duration=3600)
        buffer.mark(*buffer.next_window(400, paused=True))
        self.assertTrue(all(buffer.next_window(400, paused=True) is None for _ in range(100)))

    def test_seek_retains_previous_coverage_for_a_return_visit(self):
        buffer = PlaybackBuffer([(398, 410)], duration=3600)
        buffer.seek()
        self.assertEqual(buffer.next_window(900, paused=True), (898, 910))
        self.assertIsNone(buffer.next_window(400, paused=True))

    def test_silent_coverage_and_end_of_file_are_still_cached(self):
        buffer = PlaybackBuffer(duration=9.5)
        self.assertEqual(buffer.next_window(0), (0, 9.5))
        buffer.mark(0, 9.5)
        self.assertTrue(buffer.complete)
        self.assertIsNone(buffer.next_window(9.5))

    def test_final_short_gap_is_processed_without_claiming_the_entire_video(self):
        buffer = PlaybackBuffer([(0, 12)], duration=15)
        self.assertEqual(buffer.next_window(1), (12, 15))
        self.assertFalse(buffer.complete)


class PartialCacheTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures for inspection; no permanent temporary-directory cleanup.
        root = Path(__file__).resolve().parents[1] / "data" / "validation" / "caption-cache" / uuid.uuid4().hex
        self.cache = CaptionCache(root)

    def test_untranslated_original_and_partial_coverage_survive_restart(self):
        cue = {"start": 100, "end": 102, "text": "Hello", "translation_error": "offline", "_job_id": "ephemeral"}
        self.assertTrue(self.cache.write_bundle("movie", "small", "google", [cue],
                                               coverage=[(98, 110)], duration=3600))
        saved = self.cache.read_bundle("movie", "small", "google")
        self.assertEqual(saved["coverage"], [(98, 110)])
        self.assertFalse(saved["corpus_complete"])
        self.assertEqual(saved["cues"], [{"start": 100, "end": 102, "text": "Hello"}])
        self.assertEqual(self.cache.read_bundle("movie", "large-v3", "google"), {})

    def test_silent_recognition_window_is_saved_without_fake_subtitles(self):
        self.assertTrue(self.cache.write_bundle("movie", "small", "google", [], coverage=[(0, 12)]))
        self.assertEqual(self.cache.read_bundle("movie", "small", "google")["coverage"], [(0, 12)])

    def test_older_complete_cache_remains_readable_and_unchanged(self):
        cues = [{"start": 0, "end": 2, "text": "Hello", "translation": "你好"}]
        self.cache.write("movie", "small", "google", cues)
        old = self.cache._path("movie", "small", "google")
        original = old.read_bytes()
        self.assertTrue(self.cache.read_bundle("movie", "small", "google")["corpus_complete"])
        self.cache.write_bundle("movie", "small", "google", cues, coverage=[(0, 2)], duration=2,
                                corpus_complete=True)
        self.assertEqual(old.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
