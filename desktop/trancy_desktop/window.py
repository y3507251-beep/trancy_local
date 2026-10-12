from __future__ import annotations

import html
import logging
import math
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget, QMenu,
)

from .browser_bridge import BrowserBridge
from .media_download import MediaDownloader
from .native_subtitles import NativeSubtitleReader
from .overlay import SubtitleOverlay
from .pipeline import TranslationQueue
from .playback import PlaybackBuffer
from .potplayer import PotPlayerAdapter
from .recognition import ASRSession
from .settings import CACHE, DATA, ROOT, TEMP, Settings
from .timeline import CaptionCache, Timeline, browser_identity, cue_key, file_identity
from .translation import detect_target


LOG = logging.getLogger(__name__)


STYLE = """
QMainWindow, QWidget#main { background: #10121a; color: #e8e9ee; }
QWidget { color: #e8e9ee; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QLabel#title { font-size: 25px; font-weight: 700; }
QLabel#subtitle { color: #969eb5; }
QLabel#status { color: #c7b2ff; padding: 9px 12px; background: #272037; border-radius: 7px; }
QGroupBox { border: 1px solid #323647; border-radius: 10px; margin-top: 13px; padding: 16px 12px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #c3c9da; }
QPushButton { background: #292d3d; border: 1px solid #3b4055; border-radius: 6px; padding: 8px 13px; }
QPushButton:hover { background: #363b50; }
QPushButton:disabled { color: #696e80; }
QPushButton#primary { background: #7c48dc; border: 1px solid #9160e8; font-weight: 600; }
QPushButton#primary:hover { background: #8d59e8; }
QComboBox, QLineEdit, QSpinBox { background: #202431; border: 1px solid #3b4055; border-radius: 5px; padding: 6px; }
QComboBox QAbstractItemView { background: #242937; selection-background-color: #7445be; }
QTextEdit { background: #171b26; border: 1px solid #323647; border-radius: 8px; padding: 7px; }
QTabWidget::pane { border: 1px solid #323647; border-radius: 8px; }
QTabBar::tab { background: #202330; padding: 11px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #3c2b56; color: #e8d9ff; }
QSlider::groove:horizontal { background: #353a4e; height: 5px; border-radius: 2px; }
QSlider::handle:horizontal { background: #b491fb; width: 13px; margin: -5px 0; border-radius: 6px; }
QMenu { background: #202431; border: 1px solid #3b4055; }
QMenu::item:selected { background: #6941a4; }
"""


class MainWindow(QMainWindow):
    recycled = pyqtSignal(str)

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or Settings()
        self.setWindowTitle("Trancy 桌面字幕 · PotPlayer / B站 / YouTube")
        icon = ROOT.parent / "assets/icons/ic128.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1040, 810)
        self.setMinimumSize(920, 720)
        self.setStyleSheet(STYLE)
        self.timeline = Timeline()
        self.cache = CaptionCache(CACHE)
        self.generation = 0
        self.mode = "idle"
        self.identity = ""
        self.source_title = ""
        self._active = None
        self._pending_file = None
        self._pending_download = None
        self._download_context = None
        self._source_config = {}
        self._translation_mode = "google"
        self._source_complete = False
        self._source_kind = "asr"
        self._media_source = None
        self._buffer = PlaybackBuffer()
        self._native_readers = {}
        self._caption_context = None
        self._last_playback = None
        self._recognition_blocked = False
        self._history_pending = False
        self._history_updated = 0.0
        self._history_html = ""
        self._cache_pending = False
        self._cache_updated = 0.0
        self._cache_restored = False
        self._native_translation_cache = {}
        self._translation_dirty = True
        self._displayed_key = None
        self._last_video = None
        self._bound_tab = None
        self._closing = False
        self._backend_closed = False
        self._close_started = 0.0
        self._temp_recycling = 0
        self._last_error = ""
        self._build_ui()
        self.overlay = SubtitleOverlay(self.settings)
        self.overlay.moved.connect(lambda x, y: self.settings.update(overlay_x=x, overlay_y=y))
        self.overlay.set_locked(bool(self.settings.get("overlay_locked")))
        self.asr = ASRSession(self)
        self.asr.updated.connect(self._asr_event)
        self.asr.idle.connect(self._schedule)
        self.asr.closed.connect(self._asr_closed)
        self.bridge = BrowserBridge(self)
        self.bridge.video_changed.connect(self._browser_update)
        self.bridge.captions_ready.connect(self._browser_captions)
        self.bridge.status_changed.connect(self.web_status.setText)
        self.translator = TranslationQueue(self.bridge, self)
        self.translator.translated.connect(self._translated)
        self.translator.failed.connect(lambda e: self._translation_error(e))
        self.translator.status.connect(self._status)
        self.translator.closed.connect(self._translation_closed)
        self.pot = PotPlayerAdapter(self)
        self.pot.state_changed.connect(self._pot_update)
        self.pot.error.connect(self._status)
        self.downloader = MediaDownloader(self)
        self.downloader.progress.connect(self._status)
        self.downloader.finished.connect(self._downloaded)
        self.downloader.failed.connect(self._download_failed)
        self.recycled.connect(self._recycle_done)
        self.pot.start()
        try:
            self.bridge.start(port=18766)
        except Exception as error:
            self.web_status.setText(str(error))
        self.tick = QTimer(self)
        self.tick.setInterval(120)
        self.tick.timeout.connect(self._tick)
        self.tick.start()
        self._setup_tray()
        self.overlay.preview()
        QTimer.singleShot(5000, self._end_preview)

    def _button(self, text, callback, primary=False):
        result = QPushButton(text)
        if primary:
            result.setObjectName("primary")
        result.clicked.connect(callback)
        return result

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("main")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        title = QLabel("Trancy 桌面字幕")
        title.setObjectName("title")
        layout.addWidget(title)
        hint = QLabel("本地与网页视频 · 中英互译 · 自由拖动的桌面字幕")
        hint.setObjectName("subtitle")
        layout.addWidget(hint)
        self.status = QLabel("准备就绪 · 选择视频后开始")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        split = QHBoxLayout()
        left = QVBoxLayout()
        right_panel = QWidget()
        right_panel.setObjectName("appearancePanel")
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; } QWidget#appearancePanel { background: #10121a; }")
        right_scroll.setWidget(right_panel)
        split.addLayout(left, 7)
        split.addWidget(right_scroll, 4)
        layout.addLayout(split, 1)

        engine_group = QGroupBox("识别与翻译")
        form = QFormLayout(engine_group)
        self.model = QComboBox()
        for name, value in (("Whisper Small · 轻量识别", "small"), ("Whisper Large v3", "large-v3"),
                            ("千问 Qwen3-ASR 1.7B", "qwen3-asr-1.7b")):
            self.model.addItem(name, value)
        self.model.setCurrentIndex(max(0, self.model.findData(self.settings.get("model"))))
        self.device = QComboBox()
        for name, value in (("自动选择", "auto"), ("NVIDIA GPU", "cuda"), ("CPU", "cpu")):
            self.device.addItem(name, value)
        self.device.setCurrentIndex(max(0, self.device.findData(self.settings.get("device"))))
        self.engine = QComboBox()
        self.engine.addItem("Google · 插件内置引擎", "google")
        self.engine.addItem("浏览器插件当前引擎", "plugin")
        self.engine.setCurrentIndex(max(0, self.engine.findData(self.settings.get("translation_mode"))))
        self.model_info = QLabel("模型尚未加载")
        self.model_info.setWordWrap(True)
        form.addRow("语音识别", self.model)
        form.addRow("计算设备", self.device)
        form.addRow("中英互译", self.engine)
        form.addRow("当前处理", self.model_info)
        for box in (self.model, self.device, self.engine):
            box.currentIndexChanged.connect(self._save_engine)
        left.addWidget(engine_group)

        self.tabs = QTabWidget()
        left.addWidget(self.tabs)
        player = QWidget()
        player_layout = QVBoxLayout(player)
        self.pot_status = QLabel("正在查找 PotPlayer…")
        self.pot_status.setWordWrap(True)
        self.pot_position = QLabel("00:00 / 00:00")
        player_layout.addWidget(self.pot_status)
        player_layout.addWidget(self.pot_position)
        self.follow_pot = QCheckBox("开始后，自动跟随 PotPlayer 切换的视频")
        self.follow_pot.setChecked(bool(self.settings.get("follow_potplayer", True)))
        player_layout.addWidget(self.follow_pot)
        player_layout.addWidget(self._button("选择视频并用 PotPlayer 播放", self._open_pot_file))
        player_layout.addWidget(self._button("生成当前视频双语字幕", self.start_potplayer, True))
        self.tabs.addTab(player, "PotPlayer")

        web = QWidget()
        web_layout = QVBoxLayout(web)
        self.web_status = QLabel("等待浏览器扩展连接")
        self.web_status.setWordWrap(True)
        self.web_videos = QComboBox()
        self.web_position = QLabel("B 站 / YouTube")
        self.follow_web = QCheckBox("开始后，跟随所选标签页切换的视频")
        self.follow_web.setChecked(bool(self.settings.get("follow_browser", False)))
        web_layout.addWidget(self.web_status)
        web_layout.addWidget(self.web_videos)
        web_layout.addWidget(self.web_position)
        web_layout.addWidget(self.follow_web)
        web_layout.addWidget(self._button("生成所选网页视频双语字幕", self.start_browser, True))
        self.tabs.addTab(web, "B 站 / YouTube")

        controls = QHBoxLayout()
        controls.addWidget(self._button("停止当前任务", self.stop))
        controls.addWidget(self._button("重试未完成字幕", self.retry_translation))
        left.addLayout(controls)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setPlaceholderText("字幕原文和译文将在这里显示。")
        self.history.setMinimumHeight(145)
        left.addWidget(self.history, 1)

        appearance = QGroupBox("桌面字幕样式")
        right.addWidget(appearance)
        appearance_form = QFormLayout(appearance)
        self.overlay_visible = QCheckBox("显示桌面字幕")
        self.overlay_visible.setChecked(True)
        self.overlay_visible.toggled.connect(self._toggle_overlay)
        self.locked = QCheckBox("锁定位置，鼠标可穿透字幕")
        self.locked.setChecked(bool(self.settings.get("overlay_locked")))
        self.locked.toggled.connect(self._lock_overlay)
        appearance_form.addRow(self.overlay_visible)
        appearance_form.addRow(self.locked)
        self.caption_order = QComboBox()
        for title, value in (("英文在上 · 中文在下（默认）", "english_first"),
                             ("中文在上 · 英文在下", "chinese_first"),
                             ("原文在上 · 译文在下", "original_first"),
                             ("译文在上 · 原文在下", "translation_first")):
            self.caption_order.addItem(title, value)
        self.caption_order.setCurrentIndex(max(0, self.caption_order.findData(self.settings.get("caption_order"))))
        self.caption_order.currentIndexChanged.connect(lambda: self._style_changed("caption_order", self.caption_order.currentData()))
        appearance_form.addRow("字幕顺序", self.caption_order)
        self.word_follow = QCheckBox("英文逐词点亮")
        self.word_follow.setChecked(bool(self.settings.get("word_follow", True)))
        self.word_follow.toggled.connect(lambda value: self._style_changed("word_follow", value))
        appearance_form.addRow(self.word_follow)
        self.style_controls = {}
        for label, key, low, high, suffix in (("英文字号", "primary_size", 14, 64, ""),
                ("中文字号", "secondary_size", 12, 56, ""),
                ("未读词亮度", "unread_brightness", 10, 100, "%"),
                ("字幕宽度", "overlay_width", 420, 1800, " px"),
                ("背景不透明度", "opacity", 0, 100, "%")):
            widget = QSpinBox()
            widget.setRange(low, high)
            widget.setSuffix(suffix)
            widget.setValue(int(self.settings.get(key)))
            widget.valueChanged.connect(lambda value, k=key: self._style_changed(k, value))
            self.style_controls[key] = widget
            appearance_form.addRow(label, widget)
        appearance_form.addRow("英文颜色", self._button("选择颜色", lambda: self._choose_color("primary_color")))
        appearance_form.addRow("中文颜色", self._button("选择颜色", lambda: self._choose_color("secondary_color")))
        appearance_form.addRow(self._button("预览并调整字幕位置", self._preview))
        note = QLabel("解锁后拖动字幕即可移动位置。\n设置窗口可最小化，字幕仍然显示。")
        note.setObjectName("subtitle")
        note.setWordWrap(True)
        right.addWidget(note)
        advanced = QGroupBox("SubtitleService")
        advanced_layout = QVBoxLayout(advanced)
        self.service_root = QLineEdit(str(self.settings.get("service_root")))
        advanced_layout.addWidget(self.service_root)
        advanced_layout.addWidget(self._button("选择引擎目录", self._choose_service))
        right.addWidget(advanced)
        right.addStretch()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Trancy 桌面字幕")
        menu = QMenu()
        menu.addAction("打开设置", self.showNormal)
        menu.addAction("显示 / 隐藏字幕", lambda: self.overlay_visible.setChecked(not self.overlay_visible.isChecked()))
        menu.addAction("解锁字幕位置", lambda: self.locked.setChecked(False))
        menu.addAction("停止当前任务", self.stop)
        menu.addSeparator()
        menu.addAction("退出", self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def _status(self, text):
        if not self._closing:
            self.status.setText(str(text))

    def _save_engine(self):
        self.settings.update(model=self.model.currentData(), device=self.device.currentData(),
                             translation_mode=self.engine.currentData())
        if self.mode != "idle":
            self._status("引擎设置已保存，将在下次开始任务时生效。")

    def _style_changed(self, key, value):
        self.settings.update(**{key: value})
        if hasattr(self, "overlay"):
            self.overlay.apply_settings()

    def _choose_color(self, key):
        color = QColorDialog.getColor(QColor(self.settings.get(key)), self, "字幕颜色")
        if color.isValid():
            self._style_changed(key, color.name())

    def _choose_service(self):
        path = QFileDialog.getExistingDirectory(self, "选择 SubtitleService 目录", self.service_root.text())
        if path:
            self.service_root.setText(path)
            self.settings.update(service_root=path)


    def _toggle_overlay(self, visible):
        if hasattr(self, "overlay"):
            self.overlay.set_enabled(visible)

    def _lock_overlay(self, locked):
        self.settings.update(overlay_locked=locked)
        if hasattr(self, "overlay"):
            self.overlay.set_locked(locked)

    def _preview(self):
        self.overlay_visible.setChecked(True)
        self.overlay.preview()
        if self.mode != "idle":
            QTimer.singleShot(3500, lambda: setattr(self, "_displayed_key", None))

    def _end_preview(self):
        if self.mode == "idle":
            self.overlay.show_caption("")

    def _source_options(self):
        return dict(model=self.model.currentData(), device=self.device.currentData(), language="auto",
                    service_root=self.service_root.text().strip())

    def _new_source(self, mode, identity, title):
        self.stop()
        self.mode, self.identity, self.source_title = mode, identity, title
        self._source_config = self._source_options()
        self._translation_mode = self.engine.currentData()
        self.timeline.clear()
        self._source_complete = False
        self._source_kind = "asr"
        self._buffer = PlaybackBuffer()
        self._recognition_blocked = False
        self._last_playback = None
        self._cache_pending = False
        self._cache_updated = 0.0
        self._cache_restored = False
        self._native_translation_cache = {}
        self._translation_dirty = True
        self._history_pending = False
        self._history_updated = 0.0
        self._history_html = ""
        self._displayed_key = None
        self.history.clear()
        self.overlay.show_caption("")
        self.settings.update(service_root=self.service_root.text().strip(),
                             follow_potplayer=self.follow_pot.isChecked(), follow_browser=self.follow_web.isChecked())





    def _open_pot_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择视频", "", "视频和音频 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.wav *.mp3 *.m4a);;所有文件 (*)")
        if path:
            try:
                self.pot.open_file(path)
            except Exception as error:
                self._status(str(error))

    def start_potplayer(self):
        state = self.pot.snapshot()
        path = state.get("path")
        if not path or not Path(path).is_file():
            self._status("尚未读取到 PotPlayer 正在播放的本地文件，请先播放视频或使用“选择视频”。")
            return
        identity = file_identity(path)
        self._new_source("potplayer", identity, state.get("title") or Path(path).name)
        self._restore_cache()
        self._read_local_subtitles(path)

    def _pot_update(self, state):
        path = state.get("path") or ""
        self.pot_status.setText((state.get("title") or Path(path).name or "PotPlayer 已连接") if state.get("connected") else "未检测到 PotPlayer")
        self.pot_status.setToolTip(path)
        self.pot_position.setText(f"{self._time(state.get('position', 0))} / {self._time(state.get('duration', 0))}")
        if self.mode == "potplayer" and path and Path(path).is_file():
            current = file_identity(path)
            if current != self.identity:
                if self.follow_pot.isChecked():
                    self.start_potplayer()
                else:
                    self.overlay.show_caption("")

    def _browser_update(self, video):
        selected = self.web_videos.currentData()
        snapshots = self.bridge.videos()
        self.web_videos.blockSignals(True)
        self.web_videos.clear()
        for item in snapshots:
            tab = self._browser_tab(item)
            label = "B 站" if item.get("platform") == "bilibili" else "YouTube"
            self.web_videos.addItem(f"{label} · {item.get('title', '视频')}", tab)
        found = self.web_videos.findData(selected)
        if found >= 0:
            self.web_videos.setCurrentIndex(found)
        self.web_videos.blockSignals(False)
        tab = self._browser_tab(video)
        if self.mode == "browser" and tab == self._bound_tab:
            if video.get("closed"):
                self.stop()
                self._status("网页视频已关闭或连接已断开，请重新选择视频。")
                return
            previous = self._last_video
            self._last_video = dict(video, received_at=time.monotonic())
            if browser_identity(video) != self.identity:
                if self.follow_web.isChecked():
                    self._start_browser_video(video)
                else:
                    self.overlay.show_caption("")
            elif (previous is not None and previous.get("document_start", 0) != video.get("document_start", 0)):
                # A reload replaces the page's pending caption request even
                # when the video ID is unchanged and automatic following is off.
                self.stop()
                self._start_browser_video(video)
        current = self._selected_browser()
        if current:
            self.web_position.setText(f"{self._time(current.get('position', 0))} / {self._time(current.get('duration', 0))}")
        else:
            self.web_position.setText("B 站 / YouTube")

    @staticmethod
    def _browser_tab(video):
        return video.get("bridge_key") or f"{video.get('source_id', '')}:{video.get('tab_id', video.get('tabId', ''))}"

    def _selected_browser(self):
        tab = self.web_videos.currentData()
        return next((v for v in self.bridge.videos() if self._browser_tab(v) == tab), None)

    def start_browser(self):
        video = self._selected_browser()
        if not video:
            self._status("请先加载本项目的浏览器扩展，并打开 B 站或 YouTube 视频。")
            return
        self._start_browser_video(video)

    def _start_browser_video(self, video):
        if video.get("closed"):
            return
        if video.get("ad_playing"):
            self._status("请等网页广告结束后再生成字幕。")
            return
        if video.get("platform") == "bilibili" and not video.get("cid"):
            self._status("正在等待 B 站当前分 P 的视频身份，请播放后再生成字幕。")
            return
        self._new_source("browser", browser_identity(video), video.get("title", "网页视频"))
        self._bound_tab = self._browser_tab(video)
        self._last_video = dict(video, received_at=time.monotonic())
        self._buffer.set_duration(video.get("duration"))
        self._restore_cache()
        self._request_browser_captions(video)

    def _request_browser_captions(self, video, attempt=0):
        # Text retrieval errors never prove that a video has no subtitles.
        # Keep recognition blocked until the page confirms an absent track.
        self._recognition_blocked = True
        context = dict(generation=self.generation, identity=self.identity, tab=self._bound_tab,
                       video=dict(video), request_id=None, attempt=attempt, started=time.monotonic())
        self._caption_context = context
        self.model_info.setText("网页原字幕优先")
        self._status("正在读取网页原字幕 · 有字幕时直接翻译")
        LOG.info("caption_request generation=%s platform=%s attempt=%s connector=%s",
                 self.generation, video.get("platform"), attempt, video.get("connector_version", "unknown"))
        try:
            request_id = self.bridge.request_captions(video, generation=self.generation)
            if self._caption_context is context:
                context["request_id"] = request_id
        except Exception:
            if self._caption_context is context:
                self._caption_read_failed(context, "error", "bridge_unavailable")

    def _browser_captions(self, event):
        context = self._caption_context
        if (context is None or context.get("finished") or self.mode != "browser" or self._closing
                or context["generation"] != self.generation
                or event.get("generation") != self.generation
                or context["identity"] != self.identity
                or (context.get("request_id") and event.get("request_id") != context["request_id"])):
            return
        if event.get("bridge_key") and event["bridge_key"] != self._bound_tab:
            return
        expected = context["video"]
        if (event.get("document_start", 0) != expected.get("document_start", 0)
                or (expected.get("key") and event.get("key") != expected["key"])
                or self._last_video is None
                or self._last_video.get("document_start", 0) != expected.get("document_start", 0)
                or browser_identity(self._last_video) != context["identity"]):
            return
        status = event.get("status")
        LOG.info("caption_result generation=%s platform=%s status=%s cues=%s elapsed_ms=%s error_code=%s",
                 self.generation, expected.get("platform"), status, len(event.get("cues") or []),
                 round((time.monotonic() - context["started"]) * 1000), event.get("error_code", ""))
        if status == "ready" and event.get("cues"):
            context["finished"] = True
            context["ready"] = True
            self._install_native(event["cues"], event.get("source_language", ""))
        elif status == "unavailable" and event.get("native_track_available") is False:
            context["finished"] = True
            self._caption_context = None
            self._recognition_blocked = False
            self._fallback_browser_audio(context["video"], "网站确认没有可用原字幕")
        else:
            self._caption_read_failed(context, status, event.get("error_code", ""))

    def _caption_read_failed(self, context, status, error_code=""):
        context.update(finished=True, failed=True)
        self._recognition_blocked = True
        LOG.info("caption_read_failed generation=%s status=%s error_code=%s attempt=%s",
                 self.generation, status, error_code, context["attempt"])
        reason = {
            "bili_metadata_failed": "未能读取 B 站字幕列表",
            "bili_track_failed": "已找到 B 站字幕，但正文读取失败",
            "observer_unavailable": "网页字幕连接未就绪",
            "bridge_unavailable": "浏览器字幕连接已断开",
            "video_changed": "网页视频已切换",
        }.get(error_code, "等待原字幕超时" if status == "timeout" else "原字幕读取未完成")
        if context["attempt"] == 0 and error_code not in {"observer_unavailable", "video_changed", "bridge_unavailable"}:
            self._status(reason + " · 正在重试原字幕")
            QTimer.singleShot(800, lambda: self._retry_browser_captions(context))
        else:
            self.model_info.setText("原字幕读取失败 · 语音识别未启动")
            self._status(reason + " · 点击“重试未完成字幕”；更新扩展后请重新加载并刷新网页。")

    def _retry_browser_captions(self, context):
        current = self._last_video
        if (self._caption_context is not context or self.mode != "browser" or self._closing
                or context["generation"] != self.generation or current is None
                or browser_identity(current) != context["identity"]
                or current.get("document_start", 0) != context["video"].get("document_start", 0)):
            return
        self._request_browser_captions(current, attempt=context["attempt"] + 1)

    def _fallback_browser_audio(self, video, reason):
        LOG.info("audio_fallback generation=%s platform=%s reason=confirmed_no_native_track",
                 self.generation, video.get("platform"))
        if self._source_complete:
            self._status("已加载缓存字幕，正在跟随网页播放进度。")
            return
        self.model_info.setText("无可用原字幕 · 将使用语音识别")
        directory = TEMP / ("web-" + uuid.uuid4().hex)
        directory.mkdir(parents=True, exist_ok=True)
        self._pending_download = dict(generation=self.generation, video=dict(video), directory=str(directory),
                                      options=dict(self._source_config))
        self._status(reason + " · 正在获取音轨，随后只识别播放位置附近的片段")
        self._start_pending_download()

    def _read_local_subtitles(self, path):
        job_id = uuid.uuid4().hex
        reader = NativeSubtitleReader(path, job_id, service_root=self._source_config["service_root"], parent=self)
        context = dict(reader=reader, generation=self.generation, identity=self.identity, source=path)
        self._native_readers[job_id] = context
        reader.updated.connect(lambda event, key=job_id: self._local_captions(key, event))
        reader.finished.connect(lambda key=job_id: self._local_reader_finished(key))
        self._status("正在读取本地原字幕 · 有字幕时不启动语音识别")
        reader.start()

    def _local_captions(self, job_id, event):
        context = self._native_readers.get(job_id)
        if (context is None or context["generation"] != self.generation or self.mode != "potplayer"
                or context["identity"] != self.identity or self._closing):
            return
        self._buffer.set_duration(event.get("source_duration") or event.get("duration"))
        if event.get("event") == "result" and event.get("cues"):
            self._install_native(event["cues"], event.get("source_language", ""))
        elif event.get("event") in ("result", "error"):
            self._queue_file(context["source"], temporary=False)

    def _local_reader_finished(self, job_id):
        context = self._native_readers.pop(job_id, None)
        if context:
            context["reader"].deleteLater()

    def _install_native(self, cues, language=""):
        if self._source_kind != "native":
            self.timeline.clear()
        self._source_kind, self._source_complete = "native", True
        self.model_info.setText("原字幕模式 · 无需语音识别")
        LOG.info("native_installed generation=%s cues=%s", self.generation, len(cues))
        prepared = []
        for cue in cues:
            source, target = detect_target(cue["text"], cue.get("source_language") or language)
            key = source, target, " ".join(cue["text"].split())
            item = dict(cue, source_language=source)
            if not item.get("translation") and key in self._native_translation_cache:
                item["translation"] = self._native_translation_cache[key]
                item["target_language"] = target
            prepared.append(item)
        self.timeline.add(prepared)
        self._displayed_key = None
        self._render_history()
        self._update_translation_window()
        self._save_cache()
        self._status("已读取原字幕 · 按播放位置提前翻译，无需语音识别")

    def _start_pending_download(self):
        if (self._closing or self._pending_download is None or self._download_context is not None
                or self.downloader.running):
            return
        context, self._pending_download = self._pending_download, None
        if context["generation"] != self.generation or self.mode != "browser":
            self._recycle([context["directory"]])
            return
        self._download_context = context
        self.downloader.service_root = Path(context["options"]["service_root"])
        try:
            self.downloader.download(context["video"], context["directory"])
        except Exception as error:
            self._download_failed(str(error))

    def _downloaded(self, path, video):
        context, self._download_context = self._download_context, None
        if (self._closing or context is None or self.mode != "browser" or context["generation"] != self.generation
                or browser_identity(video) != self.identity):
            self._recycle([path])
            return
        self._queue_file(path, temporary=True)

    def _download_failed(self, message):
        context, self._download_context = self._download_context, None
        if context and context["generation"] == self.generation and self.mode == "browser":
            self._status(f"网页音频获取失败：{message}。请在原网页播放后重试。")

    def _cached_cues(self):
        # The extension's current engine can change without a desktop event.
        if self._translation_mode == "plugin":
            return []
        return self.cache.read(self.identity, self._source_config["model"], self._translation_mode)

    def _restore_cache(self):
        if self._cache_restored or self._translation_mode == "plugin":
            return
        self._cache_restored = True
        native = self.cache.read_bundle(self.identity, "native", self._translation_mode)
        self._remember_native_translations(native)
        bundle = self.cache.read_bundle(self.identity, self._source_config["model"], self._translation_mode)
        # Native subtitles are probed again: a sidecar may have changed while the
        # media file's identity stayed the same. Browser captions have their own cache.
        if not bundle:
            return
        if bundle.get("source_kind") == "native":
            self._remember_native_translations(bundle)
            return
        self.timeline.add(bundle.get("cues", []))
        self._buffer = PlaybackBuffer(bundle.get("coverage", []), bundle.get("duration"))
        self._source_complete = bool(bundle.get("corpus_complete"))
        self._render_history()
        self._update_translation_window()

    def _remember_native_translations(self, bundle):
        for cue in bundle.get("cues", []) if bundle else ():
            if cue.get("translation"):
                source, target = detect_target(cue["text"], cue.get("source_language", ""))
                self._native_translation_cache[source, target, " ".join(cue["text"].split())] = cue["translation"]

    def _queue_file(self, path, temporary):
        self._restore_cache()
        if self._source_complete:
            self._status("已加载缓存双语字幕，正在同步播放。")
            if temporary:
                self._recycle([path])
            return
        self._media_source = dict(source=str(path), generation=self.generation, temporary=temporary)
        self._drive_buffer()

    def _playback_clock(self):
        if self.mode == "potplayer":
            state = self.pot.snapshot()
            path = state.get("path")
            if (state.get("connected") and path and Path(path).is_file()
                    and file_identity(path) == self.identity):
                self._buffer.set_duration(state.get("duration"))
                return state.get("position"), state.get("paused", True), state.get("rate") or 1
        elif self.mode == "browser" and self._last_video:
            video = self._last_video
            elapsed = time.monotonic() - video["received_at"]
            if (elapsed < 4 and not video.get("ad_playing") and not video.get("closed")
                    and browser_identity(video) == self.identity):
                position = float(video.get("position") or 0)
                if not video.get("paused", True):
                    position += min(elapsed, 1.5) * float(video.get("rate") or 1)
                self._buffer.set_duration(video.get("duration"))
                return position, video.get("paused", True), video.get("rate") or 1
        return None, True, 1

    def _update_translation_window(self, *, retry=False):
        if self.mode not in ("potplayer", "browser"):
            return
        position, _, _ = self._playback_clock()
        if position is None:
            self._translation_dirty = True
            return
        self.translator.update_window(self.generation, self.timeline.cues, float(position or 0), "",
                                      self._translation_mode, retry=retry)
        self._translation_dirty = False

    def _drive_buffer(self, position=None, paused=None):
        if (self._closing or self.mode not in ("potplayer", "browser") or self._media_source is None
                or self._media_source["generation"] != self.generation or self._source_complete
                or self._active is not None or self._pending_file is not None or self._recognition_blocked):
            return
        if position is None:
            position, paused, _ = self._playback_clock()
        if position is None:
            return
        window = self._buffer.next_window(float(position or 0), paused=bool(paused))
        if window is None:
            return
        start, end = window
        # The reusable input survives individual windows; stop retires it once
        # the worker no longer reads it. Original local files are never recycled.
        self._pending_file = dict(source=self._media_source["source"], offset=0.0,
                                  generation=self.generation, temporary=False, window=(start, end),
                                  options=dict(self._source_config))
        self._status(f"正在识别播放附近音频 {self._time(start)}–{self._time(end)}")
        self._schedule()

    def _sync_playback(self, position, paused, rate):
        now = time.monotonic()
        seek = False
        if self._last_playback is not None:
            previous, timestamp, was_paused, previous_rate = self._last_playback
            expected = previous if was_paused else previous + max(0, now - timestamp) * previous_rate
            seek = abs(position - expected) > 3
        self._last_playback = (position, now, bool(paused), float(rate))
        if self._translation_dirty:
            self._update_translation_window()
        self.translator.set_position(self.generation, position, seek=seek)
        if seek:
            self._buffer.seek()
            self._recognition_blocked = False
            if self._pending_file and self._pending_file.get("window"):
                self._pending_file = None
            task = self._active
            if task and task.get("window") and not task["window"][0] <= position < task["window"][1]:
                self.asr.cancel()
        self._drive_buffer(position, paused)

    def _schedule(self):
        if self._closing or self._active is not None:
            return
        item = self._pending_file
        if item is not None:
            self._pending_file = None
        else:
            return
        if item["generation"] != self.generation:
            if item["temporary"]:
                self._recycle([item["source"]])
            QTimer.singleShot(0, self._schedule)
            return
        item["job_id"] = uuid.uuid4().hex
        item["mode"] = self.mode
        self._active = item
        try:
            cut = (dict(start_seconds=item["window"][0], duration_seconds=item["window"][1] - item["window"][0])
                   if item.get("window") else {})
            LOG.info("asr_submit generation=%s mode=%s model=%s start=%s duration=%s",
                     self.generation, self.mode, item.get("options", self._source_config).get("model"),
                     cut.get("start_seconds", 0), cut.get("duration_seconds", "full"))
            accepted = self.asr.submit(item["source"], item["job_id"], offset_seconds=item["offset"],
                                       **cut, **item.get("options", self._source_config))
        except Exception as error:
            self._status(f"无法启动识别：{error}")
            self._recognition_blocked = bool(item.get("window"))
            if self._active is item:
                self._active = None
                if item["temporary"]:
                    self._recycle([item["source"]])
            QTimer.singleShot(0, self._schedule)
            return
        if not accepted:
            # Validation can synchronously emit a terminal error from submit().
            if self._active is not item:
                return
            self._active = None
            self._pending_file = item
            QTimer.singleShot(500, self._schedule)

    def _asr_event(self, event):
        kind = event.get("event")
        task = self._active
        if task is None or event.get("job_id") != task["job_id"]:
            return
        current = (task["generation"] == self.generation and not self._closing
                   and not (task.get("window") and self._source_kind == "native"))
        if current and kind == "model":
            self.model_info.setText(f"{event.get('model', self.model.currentText())} · {event.get('device', self.device.currentText())}")
            LOG.info("asr_model_loaded generation=%s model=%s", self.generation, self._source_config.get("model"))
        if current and kind == "state":
            message = event.get("message") or event.get("status")
            if message:
                self._status(message)
        if current and kind in ("cues", "result"):
            cues = [dict(cue, _job_id=task["job_id"],
                         source_language=detect_target(cue["text"], event.get("detected_language", ""))[0])
                    for cue in event.get("cues", [])]
            if kind == "result" or event.get("reset_cues"):
                added = self.timeline.replace_job(task["job_id"], cues)
            else:
                added = self.timeline.add(cues)
            if added:
                self._update_translation_window()
            self._displayed_key = None
            self._render_history()
        if kind in ("result", "error", "cancelled"):
            if current:
                if kind == "result":
                    if task.get("window"):
                        start, end = task["window"]
                        self._buffer.set_duration(event.get("source_duration"))
                        end = min(end, float(event.get("segment_end", end)))
                        if self._buffer.duration:
                            end = min(end, self._buffer.duration)
                        self._buffer.mark(start, end)
                        self._source_complete = self._buffer.complete
                        self._status("播放附近字幕已识别 · 提前准备后续片段，缓冲充足时暂停计算")
                    else:
                        self._source_complete = True
                        self._status("字幕识别完成，正在补齐译文并同步播放。")
                    self._save_cache()
                elif kind == "error":
                    self._recognition_blocked = bool(task.get("window"))
                    self._status("识别失败：" + str(event.get("message", "未知错误")))
            self._release_task_source(task)
            self._active = None
            QTimer.singleShot(0, self._schedule)

    def _translated(self, event):
        epoch = getattr(self.translator, "epoch", event.get("epoch"))
        if (event["generation"] != self.generation or self._closing
                or event.get("epoch", epoch) != epoch):
            return
        LOG.info("translation_result generation=%s cues=%s", self.generation, len(event["cues"]))
        self.timeline.update_translations(event["cues"])
        self._displayed_key = None
        self._render_history()
        self._save_cache()


    def _translation_error(self, event):
        epoch = getattr(self.translator, "epoch", event.get("epoch"))
        if event["generation"] == self.generation and event.get("epoch", epoch) == epoch and not self._closing:
            self.timeline.translation_failed(event.get("cues", []), event["message"])
            self._render_history()
            self._status("翻译失败，原文已保留：" + event["message"])

    def retry_translation(self):
        if self.mode == "browser" and self._caption_context and self._caption_context.get("failed"):
            self._retry_browser_captions(self._caption_context)
            return
        self._recognition_blocked = False
        self._drive_buffer()
        cues = [c for c in self.timeline.cues if not c.get("translation")]
        if cues:
            for cue in cues:
                cue.pop("translation_error", None)
            self._update_translation_window(retry=True)
            self._status("正在重试未完成的译文…")

    def _save_cache(self, *, force=False):
        if (self.identity and self.mode in ("potplayer", "browser")
                and self._translation_mode != "plugin"):
            now = time.monotonic()
            if not force and now - self._cache_updated < 2:
                self._cache_pending = True
                return
            try:
                model = "native" if self._source_kind == "native" else self._source_config["model"]
                self.cache.write_bundle(self.identity, model, self._translation_mode,
                                        self.timeline.cues, coverage=self._buffer.coverage,
                                        duration=self._buffer.duration, corpus_complete=self._source_complete,
                                        source_kind=self._source_kind)
                self._cache_pending, self._cache_updated = False, now
            except OSError as error:
                self._status(f"字幕可继续显示，但缓存未保存：{error}")

    def _render_history(self):
        now = time.monotonic()
        if now - self._history_updated < .3:
            self._history_pending = True
            return
        lines = []
        position, _, _ = self._playback_clock()
        visible = self.timeline.near(float(position or 0)) if self.mode in ("potplayer", "browser") else self.timeline.cues[-80:]
        for cue in visible:
            translation = cue.get("translation") or ("翻译失败 · 点击重试" if cue.get("translation_error")
                                                     else "尚未翻译")
            lines.append(f"<p><span style='color:#929cb4'>{self._time(cue['start'])}</span> "
                         f"{html.escape(cue['text'])}<br><span style='color:#c2a6f0'>"
                         f"{html.escape(translation)}</span></p>")
        value = "".join(lines)
        if value != self._history_html:
            self._history_html = value
            self.history.setHtml(value)
            self.history.verticalScrollBar().setValue(0)
        self._history_pending, self._history_updated = False, now

    def _tick(self):
        if self._closing:
            if self._can_close():
                self._finish_close()
            return
        self._start_pending_download()
        if self._history_pending:
            self._render_history()
        if self._cache_pending:
            self._save_cache()
        cue = None
        position = None
        if self.mode in ("potplayer", "browser"):
            position, paused, rate = self._playback_clock()
            if position is not None:
                self._sync_playback(float(position), paused, rate)
                cue = self.timeline.at(position)
        key = (cue_key(cue), cue.get("translation", ""),
               tuple((w.get("text"), w.get("start"), w.get("end")) for w in (cue.get("words") or []))) if cue else None
        if key != self._displayed_key:
            self._displayed_key = key
            if self.mode in ("potplayer", "browser"):
                self._render_history()
            if cue:
                self.overlay.show_cue(cue, position)
            else:
                self.overlay.show_caption("")
        if cue:
            self.overlay.set_position(position)

    def stop(self):
        self._save_cache(force=True)
        self.generation += 1
        if hasattr(self, "translator"):
            self.translator.reset(self.generation)
        self.mode = "idle"
        self._source_complete = False
        self._bound_tab = None
        self._last_video = None
        self._last_playback = None
        self._cache_pending = False
        self._recognition_blocked = False
        if self._caption_context:
            request_id = self._caption_context.get("request_id")
            if request_id:
                self.bridge.cancel_captions(request_id)
            self._caption_context = None
        for context in self._native_readers.values():
            context["reader"].cancel()
        source, self._media_source = self._media_source, None
        if source and source["temporary"]:
            if self._active and self._active.get("source") == source["source"]:
                self._active["recycle_source"] = True
            else:
                self._recycle([source["source"]])
        if self._pending_download:
            self._recycle([self._pending_download["directory"]])
            self._pending_download = None
        if hasattr(self, "downloader") and (self.downloader.running or self._download_context is not None):
            self.downloader.cancel()
        pending = []
        if self._pending_file:
            pending.append(self._pending_file)
            self._pending_file = None
        self._recycle([p["source"] for p in pending if p["temporary"]])
        if hasattr(self, "asr") and self._active:
            self.asr.cancel()
        if hasattr(self, "overlay"):
            self.overlay.show_caption("")
        self._displayed_key = None
        self._status("已停止 · 已加载模型可用于下次识别")

    def _recycle(self, paths):
        safe = []
        for path in paths:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(TEMP.resolve()) and resolved.exists():
                safe.append(str(resolved))
        if not safe:
            return
        self._temp_recycling += 1
        def work():
            message = ""
            try:
                from send2trash import send2trash
                send2trash(safe)
            except Exception as error:
                message = "临时文件回收失败，文件已保留：" + str(error)
            try:
                self.recycled.emit(message)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="TrancyRecycle", daemon=True).start()

    def _recycle_done(self, message):
        self._temp_recycling = max(0, self._temp_recycling - 1)
        if message:
            self._status(message)

    @staticmethod
    def _time(value):
        try:
            value = max(0, int(float(value or 0)))
        except (ValueError, TypeError):
            value = 0
        return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}" if value >= 3600 else f"{value // 60:02d}:{value % 60:02d}"

    def _asr_closed(self):
        self._backend_closed = True
        if self._closing and self._active:
            task, self._active = self._active, None
            self._release_task_source(task)

    def _release_task_source(self, task):
        if task.get("temporary") or task.get("recycle_source"):
            self._recycle([task["source"]])

    def _translation_closed(self):
        if self._closing and self._can_close():
            self._finish_close()

    def _can_close(self):
        return (self._backend_closed and self._temp_recycling == 0
                and not self.downloader.running
                and self._download_context is None and not self._native_readers
                and self.translator.is_closed)

    def closeEvent(self, event):
        if self._closing and self._can_close():
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self.stop()
        self._closing = True
        self._close_started = time.monotonic()
        self.status.setText("正在关闭 · 取消任务并释放本应用的识别进程…")
        self.centralWidget().setEnabled(False)
        self.overlay.hide()
        self.pot.stop()
        self.translator.shutdown()
        self.bridge.stop()
        self.asr.shutdown()

    def _finish_close(self):
        self.tick.stop()
        self.tray.hide()
        self.overlay.hide()
        self.close()
