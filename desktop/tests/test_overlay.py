"""Qt widget behavior: visibility, desktop flags and drag geometry."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QFontDatabase, QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication

from trancy_desktop.overlay import SubtitleOverlay
from trancy_desktop.settings import DEFAULTS

_APP = QApplication.instance() or QApplication([])


class OverlayTests(unittest.TestCase):
    def setUp(self):
        class MemorySettings:
            def __init__(self):
                self.values = dict(DEFAULTS)

            def get(self, key, default=None):
                return self.values.get(key, default)
        self.settings = MemorySettings()
        self.overlay = SubtitleOverlay(self.settings)

    def tearDown(self):
        self.overlay.hide()
        self.overlay.deleteLater()
        _APP.processEvents()

    def ensure_render_fonts(self):
        if not QFontDatabase.families():
            for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
                source = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
                if source.is_file():
                    QFontDatabase.addApplicationFont(str(source))
        if not QFontDatabase.families():
            self.skipTest("A real installed font is required for the rendering regression")

    def test_empty_caption_releases_desktop_hit_area(self):
        self.overlay.show_caption("Hello", "你好")
        self.assertTrue(self.overlay.isVisible())
        self.overlay.show_caption("")
        self.assertFalse(self.overlay.isVisible())

    def test_hidden_preference_survives_new_caption(self):
        self.overlay.set_enabled(False)
        self.overlay.show_caption("Hello", "你好")
        self.assertFalse(self.overlay.isVisible())
        self.overlay.set_enabled(True)
        self.assertTrue(self.overlay.isVisible())

    def test_lock_keeps_topmost_and_enables_clickthrough(self):
        self.overlay.show_caption("Hello", "你好")
        self.overlay.set_locked(True)
        flags = self.overlay.windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)
        self.overlay.set_locked(False)
        self.assertFalse(self.overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)

    def test_caption_text_is_plain_and_translation_can_hide(self):
        self.overlay.show_caption("<b>Hello</b>", "你好")
        self.assertEqual(self.overlay.primary.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.overlay.primary.text(), "<b>Hello</b>")
        self.overlay.show_caption("Hello")
        self.assertFalse(self.overlay.secondary.isVisible())

    def test_english_is_above_chinese_for_both_speech_languages(self):
        for original, translation, language in (
            ("Hello, everyone.", "大家好。", "en"),
            ("大家好。", "Hello, everyone.", "zh"),
        ):
            with self.subTest(language=language):
                self.overlay.show_caption(original, translation, source_language=language)
                self.assertEqual(self.overlay.english.text(), "Hello, everyone.")
                self.assertEqual(self.overlay.chinese.text(), "大家好。")
                self.assertIs(self.overlay.layout.itemAt(0).widget(), self.overlay.english)
                self.assertGreater(self.overlay.english.font().pointSizeF(), self.overlay.chinese.font().pointSizeF())

    def test_chinese_source_is_detected_before_translation_metadata_arrives(self):
        self.overlay.show_caption("大家好。", "Hello, everyone.")
        self.assertEqual(self.overlay.english.text(), "Hello, everyone.")
        self.assertEqual(self.overlay.chinese.text(), "大家好。")

    def test_line_order_changes_keep_font_sizes_bound_to_language(self):
        self.overlay.show_caption("大家好。", "Hello, everyone.", source_language="zh")
        for order, first in (("chinese_first", "chinese"), ("original_first", "chinese"),
                             ("translation_first", "english"), ("english_first", "english")):
            with self.subTest(order=order):
                self.settings.values["caption_order"] = order
                self.overlay.apply_settings()
                self.assertIs(self.overlay.layout.itemAt(0).widget(), getattr(self.overlay, first))
                self.assertGreater(self.overlay.english.font().pointSizeF(), self.overlay.chinese.font().pointSizeF())
        self.settings.values["caption_order"] = "translation_first"
        self.overlay.apply_settings()
        self.overlay.show_caption("Hello, everyone.", "大家好。", source_language="en")
        self.assertIs(self.overlay.layout.itemAt(0).widget(), self.overlay.chinese)

    @staticmethod
    def timed_english_cue():
        return {"start": 10, "end": 16, "text": "We learn English.", "translation": "我们学英语。",
                "source_language": "en", "target_language": "zh",
                "words": [{"start": 10.5, "end": 11, "text": "We"},
                          {"start": 12, "end": 13.2, "text": "learn"},
                          {"start": 14.5, "end": 15.5, "text": "English."}]}

    def test_audio_word_boundaries_light_english_cumulatively(self):
        self.overlay.show_cue(self.timed_english_cue(), position=10.1)
        self.assertEqual(self.overlay.english.word_count, 3)
        self.assertEqual(self.overlay.english.timing_source, "words")
        self.assertEqual(self.overlay.english.highlighted_count, 0)
        for position, expected in ((10.5, 1), (11.9, 1), (12, 2), (14.5, 3), (15.9, 3)):
            with self.subTest(position=position):
                self.overlay.set_position(position)
                self.assertEqual(self.overlay.english.highlighted_count, expected)
        self.assertEqual(self.overlay.chinese.text(), "我们学英语。")

    def test_backward_seek_dims_words_again_and_unchanged_clock_does_not_advance(self):
        self.overlay.show_cue(self.timed_english_cue(), position=14.9)
        self.assertEqual(self.overlay.english.highlighted_count, 3)
        self.overlay.set_position(10.7)
        self.assertEqual(self.overlay.english.highlighted_count, 1)
        for unused in range(3):
            _APP.processEvents()
            self.overlay.set_position(10.7)
        self.assertEqual(self.overlay.english.highlighted_count, 1)

    def test_missing_word_axis_uses_sentence_distribution(self):
        cue = self.timed_english_cue()
        cue.pop("words")
        self.overlay.show_cue(cue, position=10)
        self.assertEqual(self.overlay.english.timing_source, "estimated")
        for position, expected in ((10, 1), (11.9, 1), (12, 2), (14, 3)):
            self.overlay.set_position(position)
            self.assertEqual(self.overlay.english.highlighted_count, expected)

    def test_chinese_audio_words_do_not_become_english_translation_word_times(self):
        cue = {"start": 10, "end": 16, "text": "我们学英语。", "translation": "We learn English.",
               "source_language": "zh", "target_language": "en",
               "words": [{"start": 10.1, "end": 10.4, "text": "我们"},
                         {"start": 10.5, "end": 10.8, "text": "学"},
                         {"start": 10.9, "end": 11.5, "text": "英语。"}]}
        self.overlay.show_cue(cue, position=11)
        self.assertEqual(self.overlay.english.timing_source, "estimated")
        self.assertEqual(self.overlay.english.highlighted_count, 1,
                         "The English translation uses an estimated sentence rhythm, not Chinese ASR word times")
        self.overlay.set_position(12)
        self.assertEqual(self.overlay.english.highlighted_count, 2)

    def test_unmatched_words_use_estimate_without_losing_sentence_text(self):
        cue = self.timed_english_cue()
        cue["words"] = [{"start": 10, "end": 11, "text": "Different"}]
        self.overlay.show_cue(cue, position=12)
        self.assertEqual(self.overlay.english.timing_source, "estimated")
        self.assertEqual(self.overlay.english.text(), "We learn English.")
        self.assertEqual(self.overlay.english.highlighted_count, 2)

    def test_word_progress_changes_rendered_pixels_instead_of_only_counters(self):
        self.ensure_render_fonts()

        def frame(position):
            self.overlay.set_position(position)
            deadline = time.monotonic() + .18
            while time.monotonic() < deadline:
                _APP.processEvents()
                time.sleep(.01)
            image = self.overlay.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            return image.constBits().asstring(image.sizeInBytes())

        self.overlay.show_cue(self.timed_english_cue(), position=10.1)
        unread = frame(10.1)
        complete = frame(15.9)
        self.assertTrue(unread != complete,
                        "Changing word state must repaint visible glyphs, not just update highlighted_count")
        self.assertGreater(sum(complete[1::4]), sum(unread[1::4]))

    def test_long_wrapped_caption_keeps_enough_height_for_all_lines(self):
        self.ensure_render_fonts()
        self.settings.values["overlay_width"] = 540
        self.overlay.apply_settings()
        self.overlay.show_cue({"start": 0, "end": 16,
            "text": ("When the subtitle sentence wraps onto several lines, every word should remain visible, "
                     "and the highlighted words should continue to follow the playback position correctly."),
            "translation": "当字幕句子需要换成多行时，每个词都应该完整显示，逐词点亮也应继续跟随视频播放位置。",
            "source_language": "en"}, position=5)
        _APP.processEvents()
        for label in (self.overlay.english, self.overlay.chinese):
            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()),
                                    "The desktop window must not cap wrapped caption height and clip the final lines")

    def test_drag_moves_window_and_emits_saved_position(self):
        self.overlay.move(100, 100)
        saved = []
        self.overlay.moved.connect(lambda x, y: saved.append((x, y)))
        for kind, global_position, button, buttons in (
            (QEvent.Type.MouseButtonPress, QPointF(120, 120), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseMove, QPointF(180, 160), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, QPointF(180, 160), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(20, 20), global_position, button, buttons, Qt.KeyboardModifier.NoModifier)
            _APP.sendEvent(self.overlay, event)
        self.assertEqual((self.overlay.x(), self.overlay.y()), (160, 140))
        self.assertEqual(saved, [(160, 140)])


if __name__ == "__main__":
    unittest.main()
