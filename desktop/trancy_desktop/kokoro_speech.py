"""Lightweight UI client for a separately installed, offline Kokoro worker."""
from __future__ import annotations

import os
from pathlib import Path

from .settings import DATA
from .speech import LocalSpeech


KOKORO_ENGINE = "kokoro-v1.0-int8-cpu-24000-v1"
DEFAULT_KOKORO_VOICE = "af_heart"
KOKORO_VOICES = (("女声 · Heart（美式英语）", "af_heart"),
                 ("男声 · Michael（美式英语）", "am_michael"))


class KokoroUnavailable(OSError):
    code = "kokoro_unavailable"


class KokoroBackend:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("TRANCY_KOKORO_ROOT", r"D:\dev\TrancyKokoro"))

    def command(self, cache_dir):
        # Run the actual interpreter, not Windows' venv redirector. This keeps
        # processId/kill bound to the model worker rather than a launcher.
        try:
            config = (self.root / ".venv/pyvenv.cfg").read_text(encoding="utf-8-sig")
            home = next(line.partition("=")[2].strip() for line in config.splitlines()
                        if line.partition("=")[0].strip() == "home")
        except (OSError, StopIteration):
            raise KokoroUnavailable("Missing offline runtime") from None
        program = Path(home) / "python.exe"
        packages = self.root / ".venv/Lib/site-packages"
        model = self.root / "models/kokoro-v1.0.int8.onnx"
        voices = self.root / "models/voices-v1.0.bin"
        worker = Path(__file__).with_name("kokoro_worker.py")
        if not all(path.is_file() for path in (program, model, voices, worker)) or not packages.is_dir():
            raise KokoroUnavailable("Missing offline runtime")
        return str(program), ["-B", "-u", "-X", "utf8", str(worker), "--cache-root", str(cache_dir),
                              "--model", str(model), "--voices", str(voices), "--site-packages", str(packages)]


class KokoroSpeech(LocalSpeech):
    engine_version = KOKORO_ENGINE

    def __init__(self, parent=None, *, cache_dir=None, backend=None, **kwargs):
        super().__init__(parent, cache_dir=cache_dir or DATA / "dubbing-cache/kokoro-v1",
                         backend=backend or KokoroBackend(), **kwargs)

    def request(self, text, token, generation, *, voice=DEFAULT_KOKORO_VOICE, rate=0):
        if not isinstance(voice, str) or voice not in {value for _, value in KOKORO_VOICES} or rate != 0:
            if not self._closing:
                self.failed.emit(dict(token=token, generation=generation, code="invalid_request",
                                      message="请选择 Kokoro 男声或女声"))
            return False
        return super().request(text, token, generation, voice=voice, rate=rate)
