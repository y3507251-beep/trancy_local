"""Measure actual word brightness; counters alone cannot catch skipped glyphs."""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QFont, QFontDatabase, QImage
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget
from trancy_desktop.overlay import WordCaption

_APP = QApplication.instance() or QApplication([])
ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")


class CaptionPaintOrderTests(unittest.TestCase):
    def setUp(self):
        if not QFontDatabase.families():
            for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
                path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))
        if not QFontDatabase.families():
            self.skipTest("An installed font is required for word-pixel verification")
        self.parent = QWidget()
        self.parent.setStyleSheet("background: #000000;")
        self.caption = WordCaption()
        self.caption.setFont(QFont("Microsoft YaHei UI", 25))
        layout = QVBoxLayout(self.parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.caption)

    def tearDown(self):
        if hasattr(self, "parent"):
            self.caption._fade.stop()
            self.parent.hide()
            self.parent.deleteLater()
            _APP.processEvents()

    def show(self, text, width=980):
        self.tokens = list(ENGLISH_WORD.finditer(text))
        self.caption.set_caption(text, 0, len(self.tokens), follow=True)
        self.parent.setFixedWidth(width)
        self.parent.resize(width, self.caption.heightForWidth(width))
        self.parent.show()
        _APP.processEvents()

    def capture(self, position):
        self.caption.set_position(position, seek=True)
        self.caption._fade.stop()
        _APP.processEvents()
        return self.parent.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)

    def brightness(self, image, dim, full):
        layout, height = self.caption._layout_for(self.caption.contentsRect().width())
        offset_y = max(0, (self.caption.height() - height) / 2)
        values = []
        text = self.caption.text()
        for token in self.tokens:
            begin = len(text[:token.start()].encode("utf-16-le")) // 2
            end = len(text[:token.end()].encode("utf-16-le")) // 2
            difference = potential = 0
            for index in range(layout.lineCount()):
                line = layout.lineAt(index)
                left, right = max(begin, line.textStart()), min(end, line.textStart() + line.textLength())
                if left >= right:
                    continue
                x1, x2 = line.cursorToX(left)[0], line.cursorToX(right)[0]
                for y in range(max(0, math.floor(line.y() + offset_y)),
                               min(image.height(), math.ceil(line.y() + line.height() + offset_y))):
                    for x in range(max(0, math.floor(min(x1, x2))), min(image.width(), math.ceil(max(x1, x2)))):
                        low, high = dim.pixelColor(x, y).green(), full.pixelColor(x, y).green()
                        if high > low:
                            difference += image.pixelColor(x, y).green() - low
                            potential += high - low
            self.assertGreater(potential, 0, "The word must contain visible glyph pixels")
            values.append(difference / potential)
        return values

    def test_visible_words_form_one_bright_prefix_for_repeats_contractions_and_wrapping(self):
        for text, width in (
            ("I say that, that I say it again.", 980),
            ("We're slowly lighting don't, isn't, and well-known words.", 980),
            ("😀 First word 🎉 then second word 🚀 ends.", 980),
            ("From the start of this sentence every word should light up in order, across every line.", 410),
            ("First two words.\nThen all the rest.", 680),
        ):
            with self.subTest(text=text):
                self.show(text, width)
                dim, full = self.capture(-1), self.capture(len(self.tokens) + 1)
                count = len(self.tokens) // 2
                image = self.capture(count - .99)
                for index, value in enumerate(self.brightness(image, dim, full)):
                    self.assertAlmostEqual(value, 1 if index < count else 0, delta=.08,
                                           msg=f"Word {index} must follow textual prefix order")

    def test_fade_has_only_one_partly_lit_word_between_bright_prefix_and_dark_tail(self):
        self.show("I say that, that I say it again.")
        dim, full = self.capture(-1), self.capture(9)
        self.capture(2.01)
        now = time.monotonic() + 10
        with patch("trancy_desktop.overlay.time.monotonic", return_value=now):
            self.caption.set_position(3.01)
        with patch("trancy_desktop.overlay.time.monotonic", return_value=now + .06):
            _APP.processEvents()
            image = self.parent.grab().toImage()
        for index, value in enumerate(self.brightness(image, dim, full)):
            self.assertAlmostEqual(value, 1 if index < 3 else .5 if index == 3 else 0, delta=.08)

    def test_small_clock_jitter_does_not_redim_played_words_but_explicit_seek_does(self):
        self.show("one two three four")
        self.caption.set_position(1.01)
        self.assertEqual(self.caption.highlighted_count, 2)
        self.caption.set_position(.99)
        self.assertEqual(self.caption.highlighted_count, 2)
        self.caption.set_position(.99, seek=True)
        self.assertEqual(self.caption.highlighted_count, 1)

    def test_alignment_update_keeps_already_lit_prefix_and_new_sentence_resets(self):
        self.show("one two three")
        early = [{"text": word, "start": index / 2} for index, word in enumerate(("one", "two", "three"))]
        late = [{"text": word, "start": 1 + index / 2} for index, word in enumerate(("one", "two", "three"))]
        self.caption.set_caption("one two three", 0, 3, words=early, follow=True)
        self.caption.set_position(1.2)
        self.assertEqual(self.caption.highlighted_count, 3)
        self.caption.set_caption("one two three", 0, 3, words=late, follow=True)
        self.caption.set_position(1.2)
        self.assertEqual(self.caption.highlighted_count, 3)
        self.caption.set_caption("one two three", 3, 6, follow=True)
        self.caption.set_position(3.1)
        self.assertEqual(self.caption.highlighted_count, 1)


if __name__ == "__main__":
    unittest.main()
