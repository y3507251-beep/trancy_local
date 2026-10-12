"""User-triggered browser audio download in SubtitleService's Python runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from PyQt6.QtCore import QObject, pyqtSignal

from .browser_bridge import normalize_video


class MediaDownloader(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str)

    def __init__(self, parent=None, service_root: str = r"D:\dev\SubtitleService"):
        super().__init__(parent)
        self.service_root = Path(service_root)
        self._process = None
        self._thread = None
        self._cancel = threading.Event()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def download(self, video: dict, output_dir: str):
        if self.running:
            self.failed.emit("已有网页音频下载任务，请等待或取消后重试")
            return
        try:
            clean = normalize_video(video)
            for field in ("tab_id", "source_id", "bridge_key", "updated_at"):
                if field in video:
                    clean[field] = video[field]
            if clean["ad_playing"]:
                raise ValueError("请等网页广告结束后再生成字幕")
            if clean["is_live"]:
                raise ValueError("正在进行的网页直播请使用系统声音实时字幕模式")
            if clean["platform"] == "bilibili" and not clean["cid"]:
                raise ValueError("B 站当前分 P 的视频身份尚未准备完成，请在原网页播放后重试")
            directory = Path(output_dir).resolve()
            if directory.is_symlink() or directory.is_junction():
                raise ValueError("临时音频目录不能是链接")
            directory.mkdir(parents=True, exist_ok=True)
            task_directory = directory / ("media-" + uuid.uuid4().hex)
            task_directory.mkdir()
        except (ValueError, OSError) as error:
            self.failed.emit(str(error))
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(clean, task_directory, directory), daemon=True,
                                        name="browser-audio-download")
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        process = self._process
        if process and process.poll() is None:
            # This process was created by this instance; no shared service process
            # or model worker is touched.
            try:
                process.terminate()
            except OSError:
                pass

    def _recycle(self, directory: Path, root: Path):
        try:
            target = directory.resolve()
            if target.parent != root.resolve() or not target.name.startswith("media-") or target.is_symlink() or target.is_junction():
                raise ValueError()
            if target.exists():
                from send2trash import send2trash
                send2trash(str(target))
        except Exception:
            self.progress.emit(f"下载临时文件回收失败，文件已保留：{directory}")

    def _run(self, video: dict, directory: Path, root: Path):
        completed = False
        try:
            python = self.service_root / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                raise RuntimeError("找不到 SubtitleService 的 Python 环境，请检查服务目录")
            self.progress.emit("正在获取当前网页视频音频")
            self._process = subprocess.Popen(
                [str(python), "-B", str(Path(__file__).with_name("media_download_worker.py")),
                 "--service-root", str(self.service_root), "--output", str(directory)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
            # URLs travel through an anonymous pipe, never process arguments or a file.
            self._process.stdin.write(json.dumps(video, ensure_ascii=False) + "\n")
            self._process.stdin.close()
            result_path, error = None, "网页音频获取失败"
            for line in self._process.stdout:
                if self._cancel.is_set():
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "progress":
                    self.progress.emit(str(event.get("message", "")))
                elif event.get("type") == "result":
                    result_path = Path(event["path"]).resolve()
                elif event.get("type") == "error":
                    error = str(event.get("message", error))
            self._process.wait(timeout=10)
            if self._cancel.is_set():
                raise RuntimeError("网页音频下载已取消")
            if self._process.returncode or result_path is None:
                raise RuntimeError(error)
            if result_path.parent != directory or not result_path.is_file():
                raise RuntimeError("下载结果不在本次任务目录内")
            # Recycle failed candidate files, preserving the verified result until
            # the caller finishes ASR. Files are never permanently unlinked.
            from send2trash import send2trash
            for leftover in directory.iterdir():
                if leftover != result_path:
                    try:
                        send2trash(str(leftover))
                    except OSError:
                        self.progress.emit(f"一份失败下载已保留：{leftover.name}")
            completed = True
            public_video = {k: v for k, v in video.items() if k != "audio_urls"}
            self.finished.emit(str(result_path), public_video)
        except Exception as error:
            self.failed.emit(str(error) if isinstance(error, RuntimeError) else "音频下载进程失败，请检查 SubtitleService 环境")
        finally:
            process, self._process = self._process, None
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if not completed:
                self._recycle(directory, root)
