"""Existing caption tracks should avoid recognition and needless translation."""
from __future__ import annotations

from pathlib import Path
import subprocess
import struct
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop import native_subtitles as captions


def spin_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class NativeSubtitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "data" / "validation" / "native-subtitles" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.source = self.root / "lesson.mp4"
        self.source.write_bytes(b"Read-only video fixture; sidecar mode does not decode it.")

    def tearDown(self):
        pass  # Keep owned read-only fixtures/results available for inspection.

    def write(self, name, text, encoding="utf-8"):
        path = self.root / name
        path.write_text(text, encoding=encoding)
        return path

    def test_existing_two_language_tracks_match_times_instead_of_indexes(self):
        english = self.write("lesson.en.srt", "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
                             "2\n00:00:03,000 --> 00:00:05,000\nHow are you?\n\n"
                             "3\n00:00:10,000 --> 00:00:12,000\nUntranslated next line.\n")
        chinese = self.write("lesson.zh.srt", "1\n00:00:01,100 --> 00:00:05,100\n你好，你好吗？\n\n"
                             "2\n00:00:20,000 --> 00:00:22,000\n不能错配这一句。\n", "gb18030")
        originals = {path: path.read_bytes() for path in (self.source, english, chinese)}
        with patch.object(captions, "_embedded_tracks", side_effect=AssertionError("Native tracks must skip ASR/video extraction")):
            result = captions.read_native_subtitles(self.source)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["origin"], "sidecar")
        self.assertEqual(result["source_language"], "en")
        self.assertEqual([cue.get("translation") for cue in result["cues"]], ["你好，你好吗？", "你好，你好吗？", None])
        self.assertEqual(result["cues"][0]["target_language"], "zh")
        self.assertEqual(result["cues"][0]["words"], [])
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_requested_chinese_track_reuses_english_translation(self):
        self.write("lesson.en.srt", "1\n00:00:01,000 --> 00:00:03,000\nHello.\n")
        self.write("lesson.zh-cn.srt", "1\n00:00:01,000 --> 00:00:03,000\n你好。\n", "utf-16")
        result = captions.read_native_subtitles(self.source, language="zh")
        self.assertEqual((result["cues"][0]["text"], result["cues"][0]["translation"]), ("你好。", "Hello."))
        self.assertEqual(result["source_language"], "zh")

    def test_bilingual_sidecar_does_not_require_another_translation(self):
        self.write("lesson.srt", "1\n00:00:00,000 --> 00:00:02,500\n<i>Hello, world.</i>\n你好，世界。\n")
        cue = captions.read_native_subtitles(self.source, language="en")["cues"][0]
        self.assertEqual((cue["text"], cue["translation"]), ("Hello, world.", "你好，世界。"))
        self.assertEqual((cue["source_language"], cue["target_language"]), ("en", "zh"))

    def test_webvtt_cue_settings_and_ass_overrides_are_readable(self):
        vtt = "WEBVTT\n\nNOTE ignore this\nmetadata\n\nidentifier\n00:01.250 --> 00:03.500 position:10%\n<c.red>Hello &amp; goodbye.</c>\n"
        self.assertEqual(captions.parse_subtitle_text(vtt, "vtt")[0]["text"], "Hello & goodbye.")
        ass = r"""[Script Info]
Title: test
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.25,0:00:03.50,Default,,0,0,0,,{\k20}Hello,{\b1} world!\N你好，世界！
Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}
"""
        cues = captions.parse_subtitle_text(ass, "ass")
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (1.25, 3.5))
        self.assertEqual(cues[0]["text"], "Hello, world!\n你好，世界！")

    def test_unrelated_file_is_not_treated_as_same_video_subtitles(self):
        self.write("lesson-extra.srt", "1\n00:00:01,000 --> 00:00:03,000\nExtra alternate edition.\n")
        self.write("lesson2.srt", "1\n00:00:01,000 --> 00:00:03,000\nOther video.\n")
        paths = captions.sidecar_paths(self.source)
        self.assertNotIn(self.root / "lesson2.srt", paths)
        self.assertNotIn(self.root / "lesson-extra.srt", paths)

    def test_missing_text_tracks_are_an_unavailable_result_with_media_duration(self):
        with patch.object(captions, "_embedded_tracks", return_value=dict(tracks=[], source_duration=123.5)):
            result = captions.read_native_subtitles(self.source)
        self.assertEqual(result["cues"], [])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["duration"], 123.5)

    def test_large_sidecar_is_bounded_and_source_preserved(self):
        sidecar = self.write("lesson.srt", "x" * 100)
        with patch.object(captions, "MAX_SUBTITLE_BYTES", 80), patch.object(
                captions, "_embedded_tracks", return_value=dict(tracks=[], source_duration=1)):
            result = captions.read_native_subtitles(self.source)
        self.assertEqual(result["cues"], [])
        self.assertTrue(result["warnings"])
        self.assertEqual(sidecar.read_text(), "x" * 100)

    def test_reader_cancellation_terminates_owned_process_without_gui_wait(self):
        owned = []
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            owned.append(process)
            return process

        def blocked_read(*args, check_cancel, **kwargs):
            captions._run_owned([sys.executable, "-B", "-c", "import time; time.sleep(30)"], check_cancel)
            return dict(cues=[])

        reader = captions.NativeSubtitleReader(self.source, "cancel-native")
        events = []
        reader.updated.connect(events.append)
        try:
            with patch.object(captions, "read_native_subtitles", side_effect=blocked_read), patch.object(
                    captions.subprocess, "Popen", side_effect=start):
                reader.start()
                self.assertTrue(spin_until(lambda: bool(owned)))
                before = time.monotonic()
                reader.cancel()
                self.assertLess(time.monotonic() - before, .05)
                self.assertTrue(spin_until(lambda: not reader.isRunning(), 3))
                self.assertTrue(any(event["event"] == "cancelled" for event in events))
                self.assertIsNotNone(owned[0].poll())
        finally:
            reader.cancel()
            reader.wait(5000)
            reader.deleteLater()
            QCoreApplication.processEvents()

    def test_native_reader_emits_existing_cues_asynchronously(self):
        self.write("lesson.srt", "1\n00:00:01,000 --> 00:00:02,000\nNative caption.\n")
        reader = captions.NativeSubtitleReader(self.source, "native-job")
        events = []
        reader.updated.connect(events.append)
        try:
            reader.start()
            self.assertTrue(spin_until(lambda: any(e["event"] == "result" for e in events)))
            result = next(event for event in events if event["event"] == "result")
            self.assertEqual(result["job_id"], "native-job")
            self.assertEqual(result["cues"][0]["text"], "Native caption.")
        finally:
            reader.wait(5000)
            reader.deleteLater()
            QCoreApplication.processEvents()


def mkv_text_fixture():
    """Minimal Matroska fixture with two UTF-8 text tracks and explicit times."""
    def element(identifier, payload):
        width = next(n for n in range(1, 9) if len(payload) < (1 << (7 * n)) - 1)
        size = ((1 << (7 * width)) | len(payload)).to_bytes(width, "big")
        return bytes.fromhex(identifier) + size + payload

    def integer(identifier, number):
        return element(identifier, number.to_bytes(max(1, (number.bit_length() + 7) // 8), "big"))

    header = element("1a45dfa3", integer("4286", 1) + integer("42f7", 1) + integer("42f2", 4) +
                     integer("42f3", 8) + element("4282", b"matroska") + integer("4287", 4) + integer("4285", 2))
    info = element("1549a966", integer("2ad7b1", 1000000) + element("4489", struct.pack(">d", 7100.0)))
    tracks = element("1654ae6b", b"".join(element("ae", integer("d7", number) + integer("73c5", number) +
                     integer("83", 17) + element("86", b"S_TEXT/UTF8") + element("22b59c", language))
                     for number, language in [(1, b"eng"), (2, b"zho")]))

    def block(number, offset, duration, text):
        payload = bytes([0x80 | number]) + struct.pack(">h", offset) + b"\x00" + text.encode("utf-8")
        return element("a0", element("a1", payload) + integer("9b", duration))

    clusters = element("1f43b675", integer("e7", 0) + block(1, 0, 2500, "Embedded English.") +
                       block(2, 100, 2500, "内嵌中文字幕。"))
    clusters += element("1f43b675", integer("e7", 5000) + block(1, 0, 2000, "Second line.") +
                        block(2, 100, 2000, "第二句。"))
    return header + element("18538067", info + tracks + clusters)


class EmbeddedSubtitleTests(unittest.TestCase):
    setUp = NativeSubtitleTests.setUp
    tearDown = NativeSubtitleTests.tearDown
    def test_actual_pyav_embedded_tracks_keep_unicode_and_subsecond_times(self):
        python = Path(captions.DEFAULT_SERVICE_ROOT) / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            self.skipTest("Existing SubtitleService PyAV interpreter unavailable")
        source = self.root / "embedded.mkv"
        source.write_bytes(mkv_text_fixture())
        before = source.read_bytes()
        with patch.object(captions, "_find_tool", return_value=None):
            found = captions.read_native_subtitles(source)
        self.assertEqual(found["origin"], "embedded")
        self.assertEqual(found["status"], "ready")
        self.assertEqual(found["cues"][0]["text"], "Embedded English.")
        self.assertEqual(found["cues"][0]["translation"], "内嵌中文字幕。")
        self.assertAlmostEqual(found["cues"][1]["start"], 5, places=3)
        self.assertEqual(found["source_language"], "en")
        self.assertGreaterEqual(found["source_duration"], 7)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
