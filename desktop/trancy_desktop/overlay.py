from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QLabel, QVBoxLayout, QWidget


class SubtitleOverlay(QWidget):
    moved = pyqtSignal(int, int)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Trancy 桌面双语字幕")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._drag = None
        self._opacity = 76
        self._locked = False
        self._has_caption = False
        self._enabled = True
        self.primary = QLabel()
        self.secondary = QLabel()
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(26, 14, 26, 16)
        self.layout.setSpacing(7)
        for label in (self.primary, self.secondary):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            effect = QGraphicsDropShadowEffect(label)
            effect.setColor(QColor(0, 0, 0, 230))
            effect.setBlurRadius(9)
            effect.setOffset(1, 2)
            label.setGraphicsEffect(effect)
            self.layout.addWidget(label)
        self.apply_settings()
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = settings.get("overlay_x"), settings.get("overlay_y")
        if x is None or y is None or not any(s.availableGeometry().contains(int(x), int(y)) for s in QApplication.screens()):
            x, y = screen.center().x() - self.width() // 2, screen.bottom() - 220
        self.move(int(x), int(y))

    def apply_settings(self):
        self.setFixedWidth(max(420, min(1900, int(self.settings.get("overlay_width", 940)))))
        self._opacity = max(0, min(100, int(self.settings.get("opacity", 76))))
        for label, size_key, color_key in ((self.primary, "primary_size", "primary_color"),
                                            (self.secondary, "secondary_size", "secondary_color")):
            font = QFont("Microsoft YaHei UI", int(self.settings.get(size_key)))
            font.setWeight(QFont.Weight.DemiBold if label is self.primary else QFont.Weight.Normal)
            label.setFont(font)
            label.setStyleSheet(f"color: {self.settings.get(color_key)}; background: transparent;")
        self.adjustSize()
        self.update()

    def set_locked(self, locked: bool):
        self._locked = locked
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.SizeAllCursor)
        if was_visible:
            self.show()

    def show_caption(self, original: str, translation: str = ""):
        self._has_caption = bool(original or translation)
        self.primary.setText(original)
        self.secondary.setText(translation)
        self.secondary.setVisible(bool(translation))
        self.adjustSize()
        self.update()
        self.setVisible(self._enabled and self._has_caption)

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.setVisible(self._enabled and self._has_caption)

    def preview(self):
        self.show_caption("让每一个声音，都有中英双语字幕。",
                          "Bring Chinese and English captions to every voice.")

    def paintEvent(self, event):
        if not self._has_caption:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(17, 18, 25, round(255 * self._opacity / 100)))
        painter.setPen(QPen(QColor(220, 206, 255, 55), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)

    def mousePressEvent(self, event):
        if not self._locked and event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.moved.emit(self.x(), self.y())

    def closeEvent(self, event):
        self.hide()
        event.ignore()
