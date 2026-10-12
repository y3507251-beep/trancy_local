from __future__ import annotations

import html
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget, QMenu,
)

from .audio_capture import SystemAudioCapture
from .browser_bridge import BrowserBridge
from .media_download import MediaDownloader
from .overlay import SubtitleOverlay
from .pipeline import TranslationQueue
from .potplayer import PotPlayerAdapter
from .recognition import ASRSession
from .settings import CACHE, DATA, ROOT, TEMP, Settings
from .timeline import CaptionCache, Timeline, browser_identity, cue_key, file_identity
from .translation import detect_target


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
QProgressBar { border: none; background: #252b39; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #9870ed; border-radius: 4px; }
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
        self.setWindowTitle("Trancy 桌面字幕 · 系统声音 / PotPlayer / B站 / YouTube")
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
        self._chunks = deque()
        self._capture_dir = None
        self._pending_capture = None
        self._pending_download = None
        self._download_context = None
        self._source_config = {}
        self._translation_mode = "google"
        self._source_complete = False
        self._latest_live = None
        self._live_received = 0.0
        self._live_started = None
        self._live_clock_job = None
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
        self.capture = SystemAudioCapture(self)
        self.capture.chunk_ready.connect(self._chunk_ready)
        self.capture.level.connect(lambda value: self.level.setValue(min(100, round(value * 100))))
        self.capture.error.connect(self._capture_error)
        self.capture.stopped.connect(self._start_pending_capture)
        self.bridge = BrowserBridge(self)
        self.bridge.video_changed.connect(self._browser_update)
        self.bridge.status_changed.connect(self.web_status.setText)
        self.translator = TranslationQueue(self.bridge, self)
        self.translator.translated.connect(self._translated)
        self.translator.failed.connect(lambda e: self._translation_error(e))
        self.translator.status.connect(self._status)
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
        self._refresh_devices()
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
        hint = QLabel("语音识别 · 中英互译 · 自由拖动的桌面字幕")
        hint.setObjectName("subtitle")
        layout.addWidget(hint)
        self.status = QLabel("准备就绪 · 选择声音来源后开始")
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
        for name, value in (("Whisper Small · 实时优先", "small"), ("Whisper Large v3", "large-v3"),
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
        form.addRow("当前模型", self.model_info)
        for box in (self.model, self.device, self.engine):
            box.currentIndexChanged.connect(self._save_engine)
        left.addWidget(engine_group)

        self.tabs = QTabWidget()
        left.addWidget(self.tabs)
        live = QWidget()
        live_form = QVBoxLayout(live)
        text = QLabel("捕获 Windows 声音输出，分段识别并持续显示中英字幕。")
        text.setWordWrap(True)
        live_form.addWidget(text)
        device_row = QHBoxLayout()
        self.audio_device = QComboBox()
        device_row.addWidget(self.audio_device, 1)
        device_row.addWidget(self._button("刷新", self._refresh_devices))
        live_form.addLayout(device_row)
        chunk_row = QHBoxLayout()
        chunk_row.addWidget(QLabel("每段音频"))
        self.chunk_seconds = QSpinBox()
        self.chunk_seconds.setRange(2, 12)
        self.chunk_seconds.setSuffix(" 秒")
        self.chunk_seconds.setValue(int(self.settings.get("chunk_seconds", 4)))
        chunk_row.addWidget(self.chunk_seconds)
        chunk_row.addStretch()
        live_form.addLayout(chunk_row)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setValue(0)
        self.level.setTextVisible(False)
        live_form.addWidget(self.level)
        live_form.addWidget(self._button("开始实时双语字幕", self.start_live, True))
        self.tabs.addTab(live, "系统声音")

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
        controls.addWidget(self._button("重试未完成翻译", self.retry_translation))
        left.addLayout(controls)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setPlaceholderText("识别出的原文和译文将在这里显示。")
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

    def _refresh_devices(self):
        if not hasattr(self, "audio_device"):
            return
        self.audio_device.clear()
        self.audio_device.addItem("系统默认输出（开始时）", None)
        try:
            for device in SystemAudioCapture.devices():
                self.audio_device.addItem(str(device.get("name", "声音输出")), device.get("index"))
        except Exception as error:
            self._status(f"系统取音尚未就绪：{error}")

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
        self._displayed_key = None
        self._latest_live = None
        self._live_received = 0.0
        self._live_started = None
        self._live_clock_job = None
        self.history.clear()
        self.overlay.show_caption("")
        self.settings.update(service_root=self.service_root.text().strip(),
                             chunk_seconds=self.chunk_seconds.value(),
                             follow_potplayer=self.follow_pot.isChecked(), follow_browser=self.follow_web.isChecked())

    def start_live(self):
        self._new_source("live", f"live:{uuid.uuid4().hex}", "系统声音")
        directory = TEMP / self.identity.replace(":", "-")
        directory.mkdir(parents=True, exist_ok=True)
        self._capture_dir = directory.resolve()
        self._pending_capture = dict(generation=self.generation, directory=str(directory),
                                     chunk_seconds=float(self.chunk_seconds.value()),
                                     device_index=self.audio_device.currentData())
        self._status("正在准备系统声音捕获…")
        self._start_pending_capture()

    def _start_pending_capture(self):
        pending = self._pending_capture
        if pending is None or self._closing or self.capture.running:
            return
        self._pending_capture = None
        if self.mode != "live" or pending["generation"] != self.generation:
            return
        try:
            self.capture.start(pending["directory"], chunk_seconds=pending["chunk_seconds"],
                               device_index=pending["device_index"])
            if self.mode == "live":
                self._status("正在捕获系统声音 · 有语音时生成中英字幕")
        except Exception as error:
            self._capture_error(str(error))

    def _capture_error(self, text):
        if self._pending_capture is not None or self.mode != "live":
            return
        if self.mode == "live":
            self.stop()
        self._status(text)

    def _chunk_ready(self, path, start, end):
        if (self.mode != "live" or self._closing or self._capture_dir is None
                or Path(path).resolve().parent != self._capture_dir):
            self._recycle([path])
            return
        self._chunks.append(dict(source=path, offset=start, generation=self.generation, temporary=True,
                                 options=dict(self._source_config)))
        while len(self._chunks) > 3:
            dropped = self._chunks.popleft()
            self._recycle([dropped["source"]])
            self._status("识别速度暂时低于播放速度，正在优先处理最新声音；可选择更小模型或 GPU。")
        self._schedule()

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
        self._queue_file(path, temporary=False)

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
            self._last_video = dict(video, received_at=time.monotonic())
            if browser_identity(video) != self.identity:
                if self.follow_web.isChecked():
                    self._start_browser_video(video)
                else:
                    self.overlay.show_caption("")
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
        cached = self._cached_cues()
        if cached:
            self.timeline.add(cached)
            self._source_complete = True
            self._render_history()
            self._status("已加载缓存字幕，正在跟随网页播放进度。")
            return
        directory = TEMP / ("web-" + uuid.uuid4().hex)
        directory.mkdir(parents=True, exist_ok=True)
        self._pending_download = dict(generation=self.generation, video=dict(video), directory=str(directory),
                                      options=dict(self._source_config))
        self._status("正在获取网页视频音频…")
        self._start_pending_download()

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
            self._status(f"网页音频获取失败：{message}。也可切换到系统声音模式。")

    def _cached_cues(self):
        # The extension's current engine can change without a desktop event.
        if self._translation_mode == "plugin":
            return []
        return self.cache.read(self.identity, self._source_config["model"], self._translation_mode)

    def _queue_file(self, path, temporary):
        cached = self._cached_cues()
        if cached:
            self.timeline.add(cached)
            self._source_complete = True
            self._render_history()
            self._status("已加载缓存双语字幕，正在同步播放。")
            if temporary:
                self._recycle([path])
            return
        self._pending_file = dict(source=str(path), offset=0.0, generation=self.generation, temporary=temporary,
                                  options=dict(self._source_config))
        self._status("正在准备识别音频…")
        self._schedule()

    def _schedule(self):
        if self._closing or self._active is not None:
            return
        item = self._pending_file
        if item is not None:
            self._pending_file = None
        elif self._chunks:
            item = self._chunks.popleft()
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
            accepted = self.asr.submit(item["source"], item["job_id"], offset_seconds=item["offset"],
                                       **item.get("options", self._source_config))
        except Exception as error:
            self._status(f"无法启动识别：{error}")
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
            if self.mode == "live":
                self._chunks.appendleft(item)
            else:
                self._pending_file = item
            QTimer.singleShot(500, self._schedule)

    def _asr_event(self, event):
        kind = event.get("event")
        if kind == "model" and not self._closing:
            self.model_info.setText(f"{event.get('model', self.model.currentText())} · {event.get('device', self.device.currentText())}")
        task = self._active
        if task is None or event.get("job_id") != task["job_id"]:
            return
        current = task["generation"] == self.generation and not self._closing
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
            if self.mode == "live":
                self._refresh_live_caption()
                if cues:
                    self._live_received = time.monotonic()
                if self._latest_live:
                    self.timeline.trim_before(self._latest_live["end"] - 120)
            if added:
                self.translator.enqueue(self.generation, added, event.get("detected_language", ""),
                                        self._translation_mode, realtime=self.mode == "live")
            self._displayed_key = None
            self._render_history()
        if kind in ("result", "error", "cancelled"):
            if current:
                if kind == "result":
                    self._source_complete = self.mode != "live"
                    self._status("字幕识别完成，正在补齐译文并同步播放。" if self.mode != "live" else "正在识别系统声音…")
                    self._save_cache()
                elif kind == "error":
                    self._status("识别失败：" + str(event.get("message", "未知错误")))
            if task["temporary"]:
                self._recycle([task["source"]])
            self._active = None
            QTimer.singleShot(0, self._schedule)

    def _translated(self, event):
        if event["generation"] != self.generation or self._closing:
            return
        updated = self.timeline.update_translations(event["cues"])
        if self.mode == "live" and updated:
            self._refresh_live_caption()
            if self._latest_live and any(c.get("_job_id") == self._latest_live.get("_job_id") for c in updated):
                self._live_received = time.monotonic()
        self._displayed_key = None
        self._render_history()
        self._save_cache()

    def _refresh_live_caption(self):
        if not self.timeline.cues:
            self._latest_live = None
            self._live_started = None
            self._live_clock_job = None
            return
        latest = self.timeline.cues[-1]
        # A short chunk can produce several cues at once. Show the entire chunk
        # so earlier words are not lost just because inference returned quickly.
        job = latest.get("_job_id")
        cues = [c for c in self.timeline.cues if c.get("_job_id") == job] if job else [latest]
        source = latest.get("source_language") or detect_target(latest["text"])[0]
        self._latest_live = dict(start=cues[0]["start"], end=cues[-1]["end"], _job_id=job,
                                 text=" ".join(c["text"] for c in cues),
                                 translation=" ".join(c["translation"] for c in cues if c.get("translation")),
                                 source_language=source, words=[w for c in cues for w in (c.get("words") or [])],
                                 timing_source=latest.get("timing_source", "cue"))
        clock_job = job or (cues[0]["start"], cues[-1]["end"])
        if clock_job != self._live_clock_job:
            self._live_clock_job = clock_job
            self._live_started = None
        english_ready = not str(source).startswith("zh") or bool(self._latest_live["translation"])
        if english_ready and self._live_started is None:
            self._live_started = time.monotonic()

    def _translation_error(self, event):
        if event["generation"] == self.generation:
            self._status("翻译失败，原文已保留：" + event["message"])

    def retry_translation(self):
        cues = [c for c in self.timeline.cues if not c.get("translation")]
        if cues:
            self.translator.enqueue(self.generation, cues, "", self._translation_mode, self.mode == "live")
            self._status("正在重试未完成的译文…")

    def _save_cache(self):
        if (self._source_complete and self.identity and self.mode in ("potplayer", "browser")
                and self._translation_mode != "plugin"):
            try:
                self.cache.write(self.identity, self._source_config["model"], self._translation_mode, self.timeline.cues)
            except OSError as error:
                self._status(f"字幕可继续显示，但缓存未保存：{error}")

    def _render_history(self):
        lines = []
        for cue in self.timeline.cues[-80:]:
            lines.append(f"<p><span style='color:#929cb4'>{self._time(cue['start'])}</span> "
                         f"{html.escape(cue['text'])}<br><span style='color:#c2a6f0'>"
                         f"{html.escape(cue.get('translation') or '译文生成中…')}</span></p>")
        self.history.setHtml("".join(lines))
        self.history.verticalScrollBar().setValue(self.history.verticalScrollBar().maximum())

    def _tick(self):
        if self._closing:
            if self._can_close():
                self._finish_close()
            return
        self._start_pending_capture()
        self._start_pending_download()
        cue = None
        position = None
        if self.mode == "live":
            if time.monotonic() - self._live_received < 10:
                cue = self._latest_live
                if cue and self._live_started is not None:
                    position = min(cue["end"], cue["start"] + max(0, time.monotonic() - self._live_started))
        elif self.mode == "potplayer":
            state = self.pot.snapshot()
            path = state.get("path")
            if state.get("connected") and path and Path(path).is_file() and state.get("position") is not None and file_identity(path) == self.identity:
                position = float(state["position"])
                cue = self.timeline.at(position)
        elif self.mode == "browser" and self._last_video:
            video = self._last_video
            elapsed = time.monotonic() - video["received_at"]
            if (elapsed < 4 and not video.get("ad_playing") and not video.get("closed")
                    and browser_identity(video) == self.identity):
                position = float(video.get("position") or 0)
                if not video.get("paused", True):
                    position += min(elapsed, 1.5) * float(video.get("rate") or 1)
                cue = self.timeline.at(position)
        key = (cue_key(cue), cue.get("translation", ""),
               tuple((w.get("text"), w.get("start"), w.get("end")) for w in (cue.get("words") or []))) if cue else None
        if key != self._displayed_key:
            self._displayed_key = key
            if cue:
                self.overlay.show_cue(cue, position)
            else:
                self.overlay.show_caption("")
        if cue:
            self.overlay.set_position(position)

    def stop(self):
        self.generation += 1
        if hasattr(self, "translator"):
            self.translator.reset(self.generation)
        self.mode = "idle"
        self._source_complete = False
        self._bound_tab = None
        self._last_video = None
        self._live_started = None
        self._live_clock_job = None
        self._pending_capture = None
        self._capture_dir = None
        if hasattr(self, "capture"):
            self.capture.stop()
        if self._pending_download:
            self._recycle([self._pending_download["directory"]])
            self._pending_download = None
        if hasattr(self, "downloader") and (self.downloader.running or self._download_context is not None):
            self.downloader.cancel()
        pending = list(self._chunks)
        self._chunks.clear()
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
            if task["temporary"]:
                self._recycle([task["source"]])

    def _can_close(self):
        return (self._backend_closed and self._temp_recycling == 0
                and not self.capture.running and not self.downloader.running
                and self._download_context is None)

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
        self.status.setText("正在关闭 · 停止取音并释放本应用的识别进程…")
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
