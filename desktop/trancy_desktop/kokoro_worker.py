"""Offline Kokoro sentence synthesis over bounded JSON lines on stdin/stdout.

Runs in the separate TTS environment. The PyQt process imports no model, NumPy
or ONNX runtime. Files are retained; only its owned parent may publish them.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import wave


VOICES = ("af_heart", "am_michael")
MAX_TEXT = 4000


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def validate_request(request, cache_root):
    if not isinstance(request, dict) or request.get("command") != "synthesize":
        raise ValueError("Invalid command")
    text, voice = request.get("text"), request.get("voice")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
        raise ValueError("Invalid request id")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or
            "\x00" in text or voice not in VOICES or request.get("rate") != 0):
        raise ValueError("Invalid sentence or voice")
    output = Path(str(request.get("output", ""))).resolve()
    if (output.parent != cache_root or
            not re.fullmatch(r"pending-[0-9a-f]{32}\.wav", output.name) or output.exists()):
        raise ValueError("Invalid output path")
    return text, voice, output


class KokoroEngine:
    def __init__(self, model_path, voices_path):
        # Bound CPU use and disallow automatic CUDA/DML or network selection.
        os.environ["OMP_NUM_THREADS"] = "2"
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["MKL_NUM_THREADS"] = "2"
        os.environ["NUMEXPR_NUM_THREADS"] = "2"
        os.environ["LOG_LEVEL"] = "ERROR"
        import numpy as np
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        self.np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        session = ort.InferenceSession(str(model_path), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(voices_path))

    def synthesize(self, text, voice):
        samples, sample_rate = self.kokoro.create(text, voice=voice, speed=1., lang="en-us")
        samples = self.np.asarray(samples).reshape(-1)
        if not samples.size or not self.np.isfinite(samples).all():
            raise ValueError("Invalid audio")
        pcm = (self.np.clip(samples, -1., 1.) * 32767).astype("<i2").tobytes()
        return pcm, int(sample_rate)


def serve(cache_root, engine, stream=None):
    emit(dict(event="voices", voices=list(VOICES)))
    for line in stream or sys.stdin:
        request = {}
        try:
            if len(line) > 64 * 1024:
                raise ValueError("Request too large")
            request = json.loads(line)
            if isinstance(request, dict) and request.get("command") == "shutdown":
                return
            text, voice, output = validate_request(request, cache_root)
        except (OSError, TypeError, ValueError):
            emit(dict(event="failed", request_id=request.get("request_id") if isinstance(request, dict) else None,
                      code="invalid_request"))
            continue
        started = time.monotonic()
        try:
            # Third-party diagnostic prints must not corrupt the JSON protocol.
            with contextlib.redirect_stdout(sys.stderr):
                pcm, rate = engine.synthesize(text, voice)
            duration = len(pcm) / (2 * rate)
            if not 8000 <= rate <= 96000 or not math.isfinite(duration) or not 0 < duration <= 600:
                raise ValueError("Invalid audio length")
            with output.open("xb") as stream_out, wave.open(stream_out, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(pcm)
            emit(dict(event="ready", request_id=request["request_id"], path=str(output), voice=voice,
                      render_ms=round((time.monotonic() - started) * 1000), duration=duration))
        except Exception as error:
            print(str(error), file=sys.stderr)
            emit(dict(event="failed", request_id=request.get("request_id"), code="render_failed"))


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--voices", required=True)
    parser.add_argument("--site-packages")
    args = parser.parse_args(argv)
    if args.site_packages:
        sys.path.insert(0, str(Path(args.site_packages).resolve(strict=True)))
    cache_root = Path(args.cache_root).resolve(strict=True)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = KokoroEngine(args.model, args.voices)
    except Exception as error:
        print(str(error), file=sys.stderr)
        emit(dict(event="fatal", code="kokoro_unavailable"))
        return 1
    serve(cache_root, engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
