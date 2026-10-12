"""Explicit, audible end-to-end WASAPI -> SubtitleService -> Google smoke test.

Run using the desktop Python environment with ``--record``. The script first
checks the output peak meter without recording. If other audio is audible it
stops. It then plays its own short SAPI sentence twice, captures 4-second chunks,
recognizes and translates them. It never changes volume/default devices or
closes other applications. Every generated audio/task file goes to the Recycle
Bin. A small statistics-only report may be retained with ``--report PATH``.

The endpoint captures a mix: a notification or other app starting during the
test can still contaminate it. Any unrecognized/non-test transcription is not
sent for translation and is not printed or saved.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes
import json
import re
import sys
import tempfile
import threading
import time
import wave
import winsound
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


EXPECTED = "This is a subtitle test."


def endpoint_peak_meter():
    """Read-only IAudioMeterInformation on the default render endpoint.

    Interface ordering follows the Windows MMDevice/EndpointVolume headers.
    Only the methods needed by this diagnostic are exposed.
    """
    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown, CoCreateInstance
    from ctypes import HRESULT, POINTER, c_float, c_void_p
    from ctypes.wintypes import DWORD

    class Device(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                              (["in"], DWORD, "context"), (["in"], c_void_p, "params"),
                              (["out"], POINTER(POINTER(IUnknown)), "result"))]

    class Enumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD, "flow"),
                      (["in"], DWORD, "mask"), (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD, "flow"),
                      (["in"], DWORD, "role"), (["out"], POINTER(POINTER(Device)), "device")),
        ]

    class Meter(IUnknown):
        _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        _methods_ = [COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "peak"))]

    enumerator = CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
                                  interface=Enumerator, clsctx=CLSCTX_ALL)
    device = enumerator.GetDefaultAudioEndpoint(0, 0)
    unknown = device.Activate(Meter._iid_, CLSCTX_ALL, None)
    return unknown.QueryInterface(Meter)


def make_test_audio(root: Path) -> tuple[Path, float]:
    import win32com.client
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    # Select an installed English voice without changing any system preference.
    for token in voice.GetVoices():
        if "english" in token.GetDescription().lower():
            voice.Voice = token
            break
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = 22  # 22.05 kHz, mono, signed 16-bit PCM.
    source = root / "test-sentence.wav"
    stream.Open(str(source), 3, False)
    try:
        voice.AudioOutputStream = stream
        voice.Rate = 1
        voice.Speak(EXPECTED)
    finally:
        stream.Close()
    with wave.open(str(source), "rb") as audio:
        parameters = audio.getparams()
        pcm = audio.readframes(audio.getnframes())
    rate, width, channels = parameters.framerate, parameters.sampwidth, parameters.nchannels
    frame_width = width * channels
    phrase_frames = len(pcm) // frame_width
    if phrase_frames / rate > 3.2:
        raise RuntimeError("测试语音长度超过预设分段范围")
    prefix = bytes(round(rate * .20) * frame_width)
    cycle = prefix + pcm
    cycle += bytes(round(rate * 4) * frame_width - len(cycle))
    playback = root / "test-playback.wav"
    with wave.open(str(playback), "wb") as audio:
        audio.setparams(parameters)
        audio.writeframes(cycle * 2)
    return playback, 8.0


def run(report_path: Path | None) -> dict:
    from PyQt6.QtCore import QCoreApplication, QTimer
    from send2trash import send2trash
    from trancy_desktop.audio_capture import SystemAudioCapture
    from trancy_desktop.recognition import ASRSession
    from trancy_desktop.translation import TranslationClient

    meter = endpoint_peak_meter()
    peaks = []
    for _ in range(20):
        peaks.append(float(meter.GetPeakValue()))
        time.sleep(.10)
    if max(peaks) > .001:
        report = dict(test="controlled-wasapi-asr-translation", success=False,
                      status="preflight_output_busy", preflight_peak=round(max(peaks), 6),
                      preflight_observation_seconds=2.0, recording_started=False, test_audio_played=False,
                      device_configuration_changed=False,
                      message="输出设备当前有其他声音；本次未录音，请暂停其他音频后重试")
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    print("Preflight: output peak meter quiet; will play only the generated test sentence.", flush=True)

    root = Path(tempfile.mkdtemp(prefix="trancy-loopback-smoke-"))
    report = dict(test="controlled-wasapi-asr-translation", expected=EXPECTED,
                  preflight_peak=max(peaks), preflight_observation_seconds=2.0, chunk_seconds=4,
                  device_configuration_changed=False, mixed_endpoint=True,
                  limitation="Other apps or notifications starting during capture could mix into loopback audio.",
                  models=[], chunks=[], translation=[], problems=[], success=False)
    app = QCoreApplication.instance() or QCoreApplication([])
    session = ASRSession(runtime_dir=root / "jobs")
    capture = SystemAudioCapture()
    queue = deque()
    arrivals, submitted = {}, {}
    capture_stopped = False
    asr_closed = False
    closing = False
    player_started = 0.0
    translator = TranslationClient("google", timeout=15)
    translation_threads = []
    translation_lock = threading.Lock()
    unique_id = 0

    def maybe_complete():
        if capture_stopped and not queue and not session.busy:
            session.shutdown()
        if asr_closed and capture_stopped:
            app.quit()

    def submit_next():
        if not session.busy and queue and not closing:
            path, start, end, job_id = queue.popleft()
            submitted[job_id] = time.perf_counter()
            if not session.submit(path, job_id, model="small", device="cpu", language="en", offset_seconds=start):
                report["problems"].append("ASR rejected a captured chunk")
                stop_all()
        maybe_complete()

    def received(path, start, end):
        nonlocal unique_id
        unique_id += 1
        job_id = f"loopback-{unique_id}"
        arrived = time.perf_counter()
        arrivals[job_id] = arrived
        with wave.open(path, "rb") as audio:
            import numpy as np
            pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean((pcm / 32768) ** 2))) if len(pcm) else 0
            report["chunks"].append(dict(job_id=job_id, start=start, end=end, bytes=Path(path).stat().st_size,
                                         rate=audio.getframerate(), rms=round(rms, 6),
                                         arrival_after_playback=round(arrived - player_started, 3)))
        if len(queue) >= 3:
            report["problems"].append("Capture backlog exceeded diagnostic limit")
            stop_all()
            return
        queue.append((path, start, end, job_id))
        submit_next()

    def translate_result(job_id, cue_texts, recognized_at):
        started = time.perf_counter()
        entry = dict(job_id=job_id)
        try:
            values = translator.translate(cue_texts, "en", "zh-CN")
            entry.update(text=" ".join(cue_texts), translated=" ".join(values),
                         translation_seconds=round(time.perf_counter() - started, 3),
                         capture_to_translation_seconds=round(time.perf_counter() - arrivals[job_id], 3),
                         playback_to_translation_seconds=round(time.perf_counter() - player_started, 3))
        except Exception as error:
            entry["error"] = type(error).__name__ + ": " + str(error)
        with translation_lock:
            report["translation"].append(entry)

    def asr_event(event):
        kind = event.get("event")
        if kind == "model":
            report["models"].append({key: event.get(key) for key in
                                     ("job_id", "model", "device", "worker_pid", "model_instance", "load_count", "reused")})
        elif kind == "result":
            job_id = event["job_id"]
            text = " ".join(cue["text"] for cue in event.get("cues", []))
            normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
            # Do not expose or translate non-test content from this mixed endpoint.
            expected_words = set(re.sub(r"[^a-z ]", "", EXPECTED.lower()).split())
            if not normalized or not set(normalized.split()).issubset(expected_words | {"the", "a"}):
                report["problems"].append("Captured speech did not match the controlled vocabulary; content discarded")
                stop_all()
                return
            for chunk in report["chunks"]:
                if chunk["job_id"] == job_id:
                    chunk["recognized_text"] = text
                    chunk["asr_seconds"] = event.get("elapsed")
                    chunk["capture_to_asr_seconds"] = round(time.perf_counter() - arrivals[job_id], 3)
                    chunk["submitted_to_asr_seconds"] = round(time.perf_counter() - submitted[job_id], 3)
            thread = threading.Thread(target=translate_result,
                                      args=(job_id, [cue["text"] for cue in event["cues"]], time.perf_counter()))
            translation_threads.append(thread)
            thread.start()
        elif kind == "error":
            report["problems"].append("ASR: " + event.get("message", "error"))
            stop_all()

    def stopped():
        nonlocal capture_stopped
        capture_stopped = True
        maybe_complete()

    def closed():
        nonlocal asr_closed
        asr_closed = True
        maybe_complete()

    def failure(message):
        report["problems"].append("Capture: " + message)
        stop_all()

    def stop_all():
        nonlocal closing
        closing = True
        winsound.PlaySound(None, 0)
        capture.stop()
        queue.clear()
        session.shutdown()

    def finish_playback():
        winsound.PlaySound(None, 0)
        capture.stop()

    capture.chunk_ready.connect(received)
    capture.error.connect(failure)
    capture.stopped.connect(stopped)
    session.updated.connect(asr_event)
    session.idle.connect(submit_next)
    session.closed.connect(closed)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: (report["problems"].append("diagnostic timeout"), stop_all()))
    try:
        playback, duration = make_test_audio(root)
        def play():
            nonlocal player_started
            player_started = time.perf_counter()
            winsound.PlaySound(str(playback), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            QTimer.singleShot(round((duration + .5) * 1000), finish_playback)
        capture.start(str(root / "chunks"), chunk_seconds=4)
        QTimer.singleShot(500, play)
        deadline.start(60000)
        app.exec()
        deadline.stop()
        for thread in translation_threads:
            thread.join(timeout=35)
        if any(thread.is_alive() for thread in translation_threads):
            report["problems"].append("translation did not finish before cleanup deadline")
        models = report["models"]
        report["model_reused"] = (len(models) >= 2 and models[1]["reused"] is True and
                                   models[0]["model_instance"] == models[1]["model_instance"])
        report["success"] = (len(report["chunks"]) >= 2 and report["model_reused"] and not report["problems"]
                             and len(report["translation"]) >= 2
                             and all(re.search(r"[\u3400-\u9fff]", item.get("translated", ""))
                                     for item in report["translation"]))
    finally:
        winsound.PlaySound(None, 0)
        capture.stop()
        session.shutdown()
        # All tasks and the endpoint normally finish before app.exec returns.
        # If an exception intervened, pump Qt until owned resources close.
        until = time.monotonic() + 8
        while (capture.running or session.process is not None or session.busy) and time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)
        if capture.running or session.process is not None or session.busy:
            report["problems"].append(f"Owned resources are still closing; temporary files retained: {root}")
            report["success"] = False
        else:
            send2trash(str(root))
            report["temporary_audio_recycled"] = True
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="Explicitly play the test speech and capture system loopback")
    parser.add_argument("--report", type=Path, help="Optional JSON with controlled text and timings only, no audio")
    options = parser.parse_args()
    if not options.record:
        parser.error("Recording is opt-in: pass --record when ready for the audible 8-second test")
    result = run(options.report)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
