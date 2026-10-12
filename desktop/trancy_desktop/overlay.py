from __future__ import annotations

import math
import time

from PyQt6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QRegion, QTextLayout, QTextOption
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QLabel, QVBoxLayout, QWidget

from .caption_words import word_spans
from .translation import detect_target


class WordCaption(QLabel):
    """Plain text with per-word alpha, Qt shaping and centered wrapped lines."""

    def __init__(self):
        super().__init__()
        self._layout_key = None
        self._text_layout = None
        self._text_height = 0
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("background: transparent;")
        self._color = QColor("#FFFFFF")
        self._dim = .55
        self._spans = []
        self._position = None
        self._interval = None
        self.highlighted_count = 0
        self.word_count = 0
        self.timing_source = "static"
        self._from_count = self._to_count = 0
        self._changed = 0.0
        self._fade_duration = .12
        self._fade = QTimer(self)
        self._fade.setInterval(30)
        self._fade.timeout.connect(self._animate)

    def set_appearance(self, color, dim):
        self._color = QColor(color)
        self._dim = max(0.05, min(1.0, float(dim)))
        self.update()

    def set_caption(self, text, start=None, end=None, words=None, follow=False):
        spans, source = word_spans(text, start, end, words) if follow else ([], "static")
        if self.text() == text and self._spans == spans and self.timing_source == source:
            return
        interval = (float(start), float(end)) if spans else None
        same_sentence = (self.text() == text and self._interval and interval and
                         max(self._interval[0], interval[0]) < min(self._interval[1], interval[1]))
        now = time.monotonic()
        progress = self._animated_count(now) if same_sentence else 0
        count = self.highlighted_count if same_sentence else 0
        self.setText(text)
        self._spans, self.timing_source = spans, source
        self._interval = interval
        self.word_count = len(spans)
        self._layout_key = None
        self.highlighted_count = self.word_count if source == "static" else min(count, self.word_count)
        self._from_count = min(progress, self.highlighted_count)
        self._to_count = self.highlighted_count
        self._changed, self._fade_duration = now, .12
        self._fade.stop()
        if self._from_count < self._to_count:
            self._fade.start()
        if not same_sentence:
            self._position = None
        self.updateGeometry()
        self.update()

    def set_position(self, position, *, seek=False):
        try:
            position = float(position) if position is not None else None
            if position is not None and not math.isfinite(position):
                position = None
        except (TypeError, ValueError):
            position = None
        previous = self._position
        backward_seek = previous is not None and position is not None and position < previous - .25
        forward_seek = previous is not None and position is not None and position > previous + 3
        count = self.word_count
        if self._spans:
            count = 0
            for span in self._spans:
                if position is None or position < span["start"]:
                    break
                count += 1
            if previous is not None and position is not None and not backward_seek and not seek:
                count = max(count, self.highlighted_count)
        self._position = (max(previous, position) if previous is not None and position is not None
                          and not backward_seek and not seek else position)
        if count != self.highlighted_count:
            now = time.monotonic()
            self._from_count = self._animated_count(now)
            self._to_count = count
            self.highlighted_count = count
            self._changed = now
            self._fade_duration = min(.5, .12 * max(1, count - self._from_count))
            if seek or backward_seek or forward_seek or position is None:
                self._from_count = count
                self._fade.stop()
            else:
                self._fade.start()
            self.update()

    def _animated_count(self, now):
        amount = min(1, max(0, (now - self._changed) / self._fade_duration))
        return self._from_count + (self._to_count - self._from_count) * amount

    def _animate(self):
        self.update()
        if time.monotonic() - self._changed >= self._fade_duration:
            self._fade.stop()

    def _layout_for(self, width):
        width = max(1, int(width))
        key = (self.text(), self.font().toString(), width)
        if key == self._layout_key:
            return self._text_layout, self._text_height
        layout = QTextLayout(self.text().replace("\n", "\u2028"), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(max(0.0, (width - line.naturalTextWidth()) / 2), height))
            height += line.height()
        layout.endLayout()
        self._layout_key, self._text_layout, self._text_height = key, layout, math.ceil(height)
        return layout, self._text_height

    def heightForWidth(self, width):
        return max(self.fontMetrics().height(), self._layout_for(width)[1])

    def sizeHint(self):
        width = max(1, self.width())
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.FontChange:
            self._layout_key = None
            self.updateGeometry()
        super().changeEvent(event)

    def _text_region(self, layout, origin, begin, end):
        region = QRegion()
        begin = len(self.text()[:begin].encode("utf-16-le")) // 2
        end = len(self.text()[:end].encode("utf-16-le")) // 2
        for index in range(layout.lineCount()):
            line = layout.lineAt(index)
            left = max(begin, line.textStart())
            right = min(end, line.textStart() + line.textLength())
            if left >= right:
                continue
            # cursorToX already includes the centered line's x position.
            x1, x2 = line.cursorToX(left)[0], line.cursorToX(right)[0]
            x = math.floor(min(x1, x2) + origin.x())
            y = math.floor(line.y() + origin.y())
            width = math.ceil(max(x1, x2) + origin.x()) - x
            height = math.ceil(line.y() + line.height() + origin.y()) - y
            region |= QRegion(QRect(x, y, width, height))
        return region

    def paintEvent(self, event):
        layout, height = self._layout_for(self.contentsRect().width())
        origin = QPointF(self.contentsRect().left(), max(0, (self.height() - height) / 2))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if not self._spans:
            painter.setPen(self._color)
            layout.draw(painter, origin)
            return
        progress = max(0, min(self.word_count, self._animated_count(time.monotonic())))
        completed = math.floor(progress)
        fraction = progress - completed
        cutoff = self._spans[completed - 1]["end"] if completed else 0
        bright = self._text_region(layout, origin, 0, cutoff)
        current = QRegion()
        if fraction > 0 and completed < self.word_count:
            current = self._text_region(layout, origin, cutoff, self._spans[completed]["end"]).subtracted(bright)
        dark = QRegion(self.rect()).subtracted(bright.united(current))
        # Qt's multiple foreground selections can skip alternating glyph runs.
        # Disjoint clips keep one continuous bright prefix, one fading word and
        # a dark remainder, while reusing the already shaped text layout.
        for region, alpha in ((dark, self._dim), (bright, 1), (current, self._dim + (1 - self._dim) * fraction)):
            if region.isEmpty():
                continue
            painter.save()
            painter.setClipRegion(region, Qt.ClipOperation.IntersectClip)
            color = QColor(self._color)
            color.setAlphaF(color.alphaF() * alpha)
            painter.setPen(color)
            layout.draw(painter, origin)
            painter.restore()


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
        self._cue = None
        self._position = None
        self._source_chinese = False
        self.primary = self.english = WordCaption()
        self.secondary = self.chinese = WordCaption()
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(26, 14, 26, 16)
        self.layout.setSpacing(7)
        for label in (self.english, self.chinese):
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

    def _set_order(self):
        order = self.settings.get("caption_order", "english_first")
        chinese_first = order == "chinese_first" or (order == "original_first" and self._source_chinese) or (order == "translation_first" and not self._source_chinese)
        first, second = (self.chinese, self.english) if chinese_first else (self.english, self.chinese)
        self.layout.removeWidget(first)
        self.layout.removeWidget(second)
        self.layout.addWidget(first)
        self.layout.addWidget(second)

    def apply_settings(self):
        self.setFixedWidth(max(420, min(1900, int(self.settings.get("overlay_width", 940)))))
        self._opacity = max(0, min(100, int(self.settings.get("opacity", 76))))
        for label, size_key, color_key in ((self.english, "primary_size", "primary_color"),
                                            (self.chinese, "secondary_size", "secondary_color")):
            font = QFont("Microsoft YaHei UI", int(self.settings.get(size_key)))
            font.setWeight(QFont.Weight.DemiBold if label is self.english else QFont.Weight.Normal)
            label.setFont(font)
            label.set_appearance(self.settings.get(color_key), self.settings.get("unread_brightness", 55) / 100)
        self._set_order()
        if self._cue is not None:
            self.show_cue(self._cue, self._position)
        self._resize_to_caption()
        self.update()

    def _resize_to_caption(self):
        margins = self.layout.contentsMargins()
        width = max(1, self.width() - margins.left() - margins.right())
        labels = [label for label in (self.english, self.chinese) if not label.isHidden()]
        height = sum(label.heightForWidth(width) for label in labels)
        height += max(0, len(labels) - 1) * self.layout.spacing()
        self.resize(self.width(), max(1, height + margins.top() + margins.bottom()))

    def set_locked(self, locked: bool):
        self._locked = locked
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.SizeAllCursor)
        if was_visible:
            self.show()

    def show_caption(self, original: str, translation: str = "", *, source_language=""):
        self.show_cue({"text": original, "translation": translation, "source_language": source_language})

    def show_cue(self, cue, position=None):
        self._cue = dict(cue)
        original, translation = str(cue.get("text", "")), str(cue.get("translation", ""))
        source = str(cue.get("source_language") or detect_target(original)[0]).lower()
        self._source_chinese = source.startswith(("zh", "cmn", "chinese"))
        english, chinese = (translation, original) if self._source_chinese else (original, translation)
        follow = bool(self.settings.get("word_follow", True))
        words = None if self._source_chinese else cue.get("words")
        self.english.set_caption(english, cue.get("start"), cue.get("end"), words, follow)
        self.chinese.set_caption(chinese)
        self._has_caption = bool(english or chinese)
        self.english.setVisible(bool(english))
        self.chinese.setVisible(bool(chinese))
        self._set_order()
        self.set_position(position)
        self._resize_to_caption()
        self.update()
        self.setVisible(self._enabled and self._has_caption)

    def set_position(self, position, *, seek=False):
        self._position = position
        self.english.set_position(position, seek=seek)

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.setVisible(self._enabled and self._has_caption)

    def preview(self):
        self.show_caption("Bring Chinese and English captions to every voice.",
                          "让每一个声音，都有中英双语字幕。", source_language="en")

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
