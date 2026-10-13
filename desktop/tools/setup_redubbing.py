"""Install the optional, isolated Windows offline re-dubbing runtime.

This downloads official public model weights. It never uploads video/audio/text,
changes the existing SubtitleService environment, or deletes an existing file.
Reruns reuse the venv and downloaded model files. Run with Python 3.12 or 3.13:
    python setup_redubbing.py --root D:\\dev\\TrancyDubbing
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import venv

MODEL = "Qwen3-TTS-12Hz-1.7B-Base"
DEMUCS_FILE = "955717e8-8726e21a.th"
DEMUCS_URL = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/" + DEMUCS_FILE


def run(args):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", **options) as process:
        for line in process.stdout:
            print(line.rstrip(), flush=True)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, args)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_models(root):
    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    # ModelScope is an official release location linked by the Qwen project.
    # Range requests avoid proxies buffering an entire multi-GB response.
    api = "https://www.modelscope.cn/api/v1/models/Qwen/" + MODEL
    with urllib.request.urlopen(api + "/repo/files?Revision=master&Recursive=true", timeout=60) as response:
        files = json.load(response)["Data"]["Files"]
    for item in files:
        name = item.get("Path", "")
        if item.get("Type") != "blob" or not name.endswith((".json", ".safetensors", ".txt", ".tiktoken", ".model")):
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid official model file path")
        target = models / MODEL / relative
        url = api + "/repo?Revision=master&FilePath=" + urllib.parse.quote(name, safe="")
        ranged_download(url, target, int(item["Size"]), item.get("Sha256", ""))
    target = models / "demucs" / DEMUCS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not file_hash(target).startswith("8726e21a"):
        target.rename(target.with_name("invalid-" + uuid.uuid4().hex + ".th"))
    if not target.exists():
        print("Downloading official Demucs htdemucs weights", flush=True)
        request = urllib.request.Request(DEMUCS_URL, headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(request, timeout=60) as response:
            size = int(response.headers["Content-Range"].split("/")[-1])
        ranged_download(DEMUCS_URL, target, size, "")
        if not file_hash(target).startswith("8726e21a"):
            raise RuntimeError("Official Demucs checkpoint checksum mismatch")
    bag = target.parent / "htdemucs.yaml"
    if not bag.exists():
        bag.write_text("models: ['955717e8']\n", encoding="utf-8")
    print(json.dumps({"models": str(models), "voice_model": MODEL,
                      "demucs_sha256": file_hash(target)}, ensure_ascii=False), flush=True)


def ranged_download(url, target, size, expected_sha256, workers=4):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == size:
        if not expected_sha256 or file_hash(target) == expected_sha256:
            return
    if target.exists():
        target.rename(target.with_name("previous-" + uuid.uuid4().hex + "-" + target.name))
    pending = target.with_name(target.name + ".download")
    journal = target.with_name(target.name + ".download-parts.json")
    chunk_size = 8 * 1024 * 1024
    parts = (size + chunk_size - 1) // chunk_size
    completed = set()
    if pending.is_file() and journal.is_file():
        try:
            state = json.loads(journal.read_text(encoding="utf-8"))
            if state.get("size") == size and state.get("sha256") == expected_sha256:
                completed = set(state.get("completed", []))
        except (OSError, ValueError):
            pass
    if not pending.exists():
        pending.touch()
    lock = threading.Lock()
    last_report = [0.0]

    def part(index):
        if index in completed:
            return
        start, end = index * chunk_size, min(size, (index + 1) * chunk_size) - 1
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read(end - start + 2)
                    expected_range = f"bytes {start}-{end}/{size}"
                    if len(data) != end - start + 1 or (size > chunk_size and response.headers.get("Content-Range") != expected_range):
                        raise RuntimeError("Unexpected download range")
                with lock:
                    with pending.open("r+b") as output:
                        output.seek(start)
                        output.write(data)
                    completed.add(index)
                    journal.write_text(json.dumps({"size": size, "sha256": expected_sha256,
                                                   "completed": sorted(completed)}), encoding="utf-8")
                    if time.monotonic() - last_report[0] > 10 or len(completed) == parts:
                        print(f"{target.name}: {len(completed)}/{parts} parts", flush=True)
                        last_report[0] = time.monotonic()
                return
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(part, index) for index in range(parts)]):
            future.result()
    if pending.stat().st_size != size or (expected_sha256 and file_hash(pending) != expected_sha256):
        raise RuntimeError("Official model checksum mismatch; partial files retained")
    pending.rename(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"D:\dev\TrancyDubbing"))
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()
    if args.root.is_symlink() or getattr(args.root, "is_junction", lambda: False)():
        raise SystemExit("The runtime root must be an ordinary directory, not a link or junction.")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.models_only:
        download_models(root)
        return
    if sys.version_info[:2] not in ((3, 12), (3, 13)):
        raise SystemExit("Use Python 3.12 or 3.13 to create this isolated optional runtime.")
    env = root / ".venv"
    executable = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = root / "trancy-redubbing-runtime.json"
    if env.is_symlink() or getattr(env, "is_junction", lambda: False)():
        raise SystemExit("The optional runtime must not share another environment through a link.")
    if executable.is_file() and not marker.exists() and root != Path(r"D:\dev\TrancyDubbing").resolve():
        raise SystemExit("Existing unowned environment retained. Choose a fresh --root.")
    if marker.exists():
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("owner") != "trancy-offline-redubbing":
            raise SystemExit("Existing runtime marker belongs to another application.")
    if not executable.is_file():
        if env.exists() and any(env.iterdir()):
            raise SystemExit("Existing incomplete .venv retained. Choose a fresh --root.")
        venv.EnvBuilder(with_pip=True).create(env)
    if not marker.exists():
        marker.write_text(json.dumps({"owner": "trancy-offline-redubbing", "schema": 1}), encoding="utf-8")
    run([str(executable), "-m", "pip", "install", "torch==2.7.1", "torchaudio==2.7.1",
         "--index-url", "https://download.pytorch.org/whl/cu118", "--disable-pip-version-check"])
    requirements = Path(__file__).resolve().parents[1] / "requirements-redubbing.txt"
    run([str(executable), "-m", "pip", "install", "-r", str(requirements),
         "--disable-pip-version-check"])
    if not args.skip_models:
        run([str(executable), str(Path(__file__).resolve()), "--root", str(root), "--models-only"])
    run([str(executable), "-c", "import torch, qwen_tts, demucs, av, soundfile; "
         "print('CUDA:', torch.cuda.is_available(), torch.__version__)"])


if __name__ == "__main__":
    main()
