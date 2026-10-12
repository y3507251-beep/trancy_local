"""Qt widget behavior: visibility, desktop flags and drag geometry."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from trancy_desktop.overlay import SubtitleOverlay
from trancy_desktop.settings import DEFAULTS

_APP = QApplication.instance() or QApplication([])


class OverlayTests(unittest.TestCase):
    def setUp(self):
        class MemorySettings:
            def get(self, key, default=None):
                return DEFAULTS.get(key, default)
        self.overlay = SubtitleOverlay(MemorySettings())

    def tearDown(self):
        self.overlay.hide()
        self.overlay.deleteLater()
        _APP.processEvents()

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
