"""Explicit whole-video preparation for the separate background/voice mode.

The ordinary subtitle scheduler keeps its small playback window. This job owns
its download, optional full-file ASR, translation client and model process.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .dubbing import english_text
from .media_download import MediaDownloader
from .recognition import ASRSession
from .settings import DATA
from .timeline import browser_identity, clean_cues
from .translation import TranslationClient, detect_target


RUNTIME_ROOT = Path(r"D:\dev\TrancyDubbing")


def preparation_key(video, mode):
    value = json.dumps(["single-speaker-redub-v1", browser_identity(video), mode],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def translate_all(cues, client, cancelled, progress):
    """Keep the existing engine, translating only missing English in batches."""
    result = clean_cues(cues)
    if not result:
        raise ValueError("没有可用的整段字幕")
    missing = []
    for cue in result:
        source, target = detect_target(cue["text"], cue.get("source_language", ""))
        cue["source_language"] = source
        if source.startswith("zh") and not english_text(cue):
            missing.append(cue)
    for start in range(0, len(missing), 30):
        if cancelled.is_set():
            raise InterruptedError()
        batch = missing[start:start+30]
        translations = client.translate([c["text"] for c in batch], "zh-CN", "en")
        if len(translations) != len(batch):
            raise ValueError("英文译文数量不匹配")
        for cue, text in zip(batch, translations):
            cue.update(translation=text, target_language="en")
            if not english_text(cue):
                raise ValueError("存在无法生成英文配音的译文，请切换翻译引擎后重试")
        progress(f"正在准备整段英文译文 · {min(start+30, len(missing))}/{len(missing)}")
    if cancelled.is_set():
        raise InterruptedError()
    if any(not english_text(cue) for cue in result):
        raise ValueError("存在缺少英文文本的字幕")
    return result


class _PrepareWork(QThread):
    progress = pyqtSignal(str)
    captions = pyqtSignal(list)
    ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, job, *, bridge, mode, runtime_root, parent=None, client_factory=None):
        super().__init__(parent)
        self.job, self.bridge, self.mode = job, bridge, mode
        self.runtime_root = Path(runtime_root)
        self.client_factory = client_factory or TranslationClient
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self):
        client = process = None
        try:
            client = self.client_factory(self.mode, self.bridge)
            cues = translate_all(self.job["cues"], client, self.cancelled, self.progress.emit)
            self.captions.emit(cues)
            output = Path(self.job["output_dir"])
            job_path = output / "job.json"
            job = dict(self.job, cues=cues, model_root=str(self.runtime_root / "models"))
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            if self.cancelled.is_set():
                return
            python = self.runtime_root / ".venv/Scripts/python.exe"
            worker = Path(__file__).with_name("redub_worker.py")
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                               PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="4", MKL_NUM_THREADS="4")
            # Each audio environment owns its CUDA DLLs. Never inherit an ASR
            # environment's site-packages entries through PATH.
            environment["PATH"] = str(python.parent) + os.pathsep + os.pathsep.join(
                value for value in environment.get("PATH", "").split(os.pathsep)
                if "site-packages" not in value.lower())
            process = subprocess.Popen([str(python), "-B", "-u", str(worker), "--job", str(job_path)],
                cwd=output, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                self._process = process
            if self.cancelled.is_set():
                process.terminate()
                return
            manifest, message = None, "本地配音准备失败，请查看模型环境或重试"
            for line in process.stdout:
                if self.cancelled.is_set():
                    break
                if len(line) > 65536:
                    raise ValueError("配音进程返回的数据过大")
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "progress":
                    self.progress.emit(str(event.get("message", "正在生成整段配音"))[:400])
                elif event.get("event") == "ready":
                    manifest = Path(event.get("manifest", "")).resolve()
                elif event.get("event") == "failed":
                    message = str(event.get("message", message))[:400]
            process.wait(timeout=10)
            if self.cancelled.is_set():
                return
            if process.returncode or manifest is None:
                raise ValueError(message)
            if not manifest.is_file() or not manifest.is_relative_to(output.resolve()):
                raise ValueError("配音结果不在本次任务目录中")
            self.ready.emit(str(manifest))
        except InterruptedError:
            pass
        except Exception as error:
            if not self.cancelled.is_set():
                self.failed.emit(str(error) if isinstance(error, ValueError) else "本地配音准备进程失败，原视频继续正常播放")
        finally:
            if client:
                client.close()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                process.stdout.close()
            with self._lock:
                self._process = None


class RedubPreparation(QObject):
    progress = pyqtSignal(str)
    ready = pyqtSignal(dict)
    captions_ready = pyqtSignal(dict)
    failed = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, bridge, parent=None, *, runtime_root=RUNTIME_ROOT, cache_root=None,
                 downloader_factory=None, asr_factory=None, work_factory=None):
        super().__init__(parent)
        self.bridge = bridge
        self.runtime_root = Path(runtime_root)
        self.cache_root = Path(cache_root) if cache_root else DATA / "redubbing-cache"
        self._downloader_factory = downloader_factory or MediaDownloader
        self._asr_factory = asr_factory or ASRSession
        self._work_factory = work_factory or _PrepareWork
        self._downloader = self._asr = self._work = None
        self._closing = self._closed = False
        self._job = None
        self._translated = []
        self._asr_complete = False
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check_closed)

    @property
    def running(self):
        return bool((self._downloader and self._downloader.running) or self._asr
                    or (self._work and self._work.isRunning()))

    @property
    def is_closed(self):
        return self._closed

    def start(self, generation, video, cues, *, complete, mode, options, force=False):
        if self._job is not None or self._closing:
            raise ValueError("本次准备任务已经启动")
        if video.get("platform") != "bilibili" or not video.get("cid") or video.get("is_live") or video.get("ad_playing"):
            raise ValueError("当前新模式支持 B 站的单人录播视频")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._key = preparation_key(video, mode)
        self._index = self.cache_root / (self._key + ".json")
        self._generation, self._video = generation, dict(video)
        self._options, self._mode = dict(options), mode
        self._complete = bool(complete and cues)
        try:
            if force:
                raise ValueError("Explicit regeneration")
            saved = json.loads(self._index.read_text(encoding="utf-8"))
            manifest = Path(saved["manifest"]).resolve()
            if manifest.is_relative_to(self.cache_root.resolve()) and manifest.is_file():
                from .prepared_dubbing import PreparedDubbingManifest
                PreparedDubbingManifest.load(manifest)
                self.captions_ready.emit(dict(generation=generation, cues=clean_cues(saved["cues"])))
                self.ready.emit(dict(generation=generation, identity=browser_identity(video), manifest=str(manifest), cached=True))
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not (self.runtime_root / ".venv/Scripts/python.exe").is_file():
            raise ValueError("尚未安装本地音色匹配环境，请先运行 desktop/tools/setup_redubbing.py")
        directory = self.cache_root / (self._key[:12] + "-" + uuid.uuid4().hex)
        directory.mkdir()
        self._job = dict(schema=1, generation=generation, cues=clean_cues(cues), output_dir=str(directory.resolve()))
        if video.get("duration", 0) > 0:
            self._job["source_duration"] = float(video["duration"])
        self._downloader = self._downloader_factory(self, service_root=options["service_root"])
        self._downloader.progress.connect(self.progress.emit)
        self._downloader.finished.connect(self._downloaded)
        self._downloader.failed.connect(self._fail)
        self._downloader.download(video, str(directory / "source"))

    def _downloaded(self, path, video):
        if self._closing or browser_identity(video) != browser_identity(self._video):
            return
        self._job["source_audio"] = path
        if self._complete:
            self._launch_work()
            return
        self.progress.emit("正在识别整段音频 · 首次准备完成后可反复播放")
        self._asr = self._asr_factory(self)
        self._asr.updated.connect(self._asr_event)
        self._asr.closed.connect(self._asr_closed)
        self._asr_job = uuid.uuid4().hex
        accepted = self._asr.submit(path, self._asr_job, **self._options)
        if not accepted and self._asr:
            asr = self._asr
            self._fail("整段语音识别未能启动")
            asr.shutdown()

    def _asr_event(self, event):
        if self._closing or event.get("job_id") != self._asr_job or self._asr is None:
            return
        asr = self._asr
        if event.get("event") == "result":
            language = event.get("detected_language", "")
            self._job["cues"] = [dict(c, source_language=c.get("source_language") or language)
                                 for c in clean_cues(event.get("cues", []))]
            self._asr_complete = bool(self._job["cues"])
            if not self._asr_complete:
                self._fail("整段音频没有识别到可配音的字幕")
            asr.shutdown()
        elif event.get("event") in {"error", "cancelled"}:
            self._fail("整段语音识别失败，原视频继续正常播放")
            asr.shutdown()
        elif event.get("message"):
            self.progress.emit(str(event["message"])[:300])

    def _asr_closed(self):
        asr, self._asr = self._asr, None
        if asr is None:
            return
        asr.deleteLater()
        if self._asr_complete and not self._closing:
            self._launch_work()
        self._check_closed()

    def _launch_work(self):
        if self._closing:
            return
        self._work = self._work_factory(self._job, bridge=self.bridge, mode=self._mode,
                                        runtime_root=self.runtime_root, parent=self)
        self._work.progress.connect(self.progress.emit)
        self._work.captions.connect(self._captions)
        self._work.ready.connect(self._ready)
        self._work.failed.connect(self._fail)
        self._work.finished.connect(self._check_closed)
        self._work.start()

    def _captions(self, cues):
        if not self._closing:
            self._translated = cues
            self.captions_ready.emit(dict(generation=self._generation, cues=cues))

    def _ready(self, manifest):
        if self._closing:
            return
        from .prepared_dubbing import PreparedDubbingManifest
        try:
            PreparedDubbingManifest.load(manifest)
            saved = dict(manifest=manifest, cues=self._translated, identity=browser_identity(self._video))
            self._index.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            self.ready.emit(dict(generation=self._generation, identity=saved["identity"], manifest=manifest, cached=False))
        except (OSError, ValueError) as error:
            self._fail("生成的配音时间轴或音轨校验失败：" + str(error))

    def _fail(self, message):
        if not self._closing:
            self.failed.emit(str(message))

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self._downloader:
            self._downloader.cancel()
        if self._asr:
            self._asr.shutdown()
        if self._work:
            self._work.cancel()
        self._timer.start()
        self._check_closed()

    def _check_closed(self):
        if self._closing and not self.running and not self._closed:
            self._closed = True
            self._timer.stop()
            self.closed.emit()
