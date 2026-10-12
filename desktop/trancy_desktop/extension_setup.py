"""Build a loadable extension copy without touching the packaged snapshot."""
from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from .settings import DATA, ROOT


def prepare_extension() -> Path:
    source = ROOT.parent.resolve()
    data = DATA.resolve()
    data.mkdir(parents=True, exist_ok=True)
    destination = data / "desktop-connector"
    if destination.is_symlink() or destination.is_junction():
        raise ValueError("扩展运行目录不能是文件链接")
    staging = data / ("extension-build-" + uuid.uuid4().hex)
    staging.mkdir()
    # Deliberately exclude desktop data, Git history and Web Store _metadata.
    for name in ("index.html", "popup.html", "assets", "_locales"):
        item = source / name
        if item.is_symlink() or item.is_junction():
            raise ValueError("扩展源文件不能是链接：" + name)
        if item.is_dir():
            shutil.copytree(item, staging / name)
        else:
            shutil.copy2(item, staging / name)
    shutil.copy2(ROOT / "browser-extension" / "manifest.json", staging / "manifest.json")
    if destination.exists():
        if destination.resolve().parent != data:
            raise ValueError("扩展运行目录不在本应用数据目录内")
        from send2trash import send2trash
        send2trash(str(destination))
    staging.rename(destination)
    return destination
