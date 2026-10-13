r"""Optional offline worker: separation -> one-speaker English clone -> full mix.

    D:\dev\TrancyDubbing\.venv\Scripts\python.exe -u redub_worker.py --job job.json

stdout is JSONL only. Models are loaded only here, after an explicit UI request,
and released before the ready result. Network access is disabled for inference;
install public weights with tools/setup_redubbing.py before invoking this worker.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

if __package__:
    from .redub_audio import (ERRORS, PIPELINE_VERSION, SAMPLE_RATE, VOICE_MODEL, RedubError,
                             build_segments, fingerprint, merge_overlapping, merge_short_phrases, normalized_cues,
                             reference_candidates, render_mix, split_utterance, validate_segments)
else:
    from redub_audio import (ERRORS, PIPELINE_VERSION, SAMPLE_RATE, VOICE_MODEL, RedubError,
                            build_segments, fingerprint, merge_overlapping, merge_short_phrases, normalized_cues,
                            reference_candidates, render_mix, split_utterance, validate_segments)


PROTOCOL_STREAM = sys.stdout


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), file=PROTOCOL_STREAM, flush=True)


def progress(message, amount):
    emit("progress", message=message, progress=round(max(0.0, min(1.0, amount)), 4))


def reserve_file(path):
    """Retain an incomplete older file instead of permanently removing it."""
    if path.exists():
        path.rename(path.with_name("previous-" + uuid.uuid4().hex + "-" + path.name))


def save_json(path, payload):
    pending = path.with_name("pending-" + uuid.uuid4().hex + ".json")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reserve_file(path)
    pending.rename(path)


def valid_wave(path, channels=None):
    import soundfile as sf
    try:
        info = sf.info(str(path))
        return (info.samplerate == SAMPLE_RATE and info.frames > 0 and
                (channels is None or channels == info.channels))
    except (OSError, RuntimeError):
        return False


def decode_audio(source, target, wanted_duration=None):
    import av
    import numpy as np
    import soundfile as sf
    if valid_wave(target, 2):
        return sf.info(str(target)).duration
    reserve_file(target)
    pending = target.with_name("decode-" + uuid.uuid4().hex + ".wav")
    try:
        with av.open(str(source)) as container, sf.SoundFile(str(pending), "w", samplerate=SAMPLE_RATE,
                                                            channels=2, subtype="FLOAT") as output:
            if not container.streams.audio:
                raise RedubError("audio_decode")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=SAMPLE_RATE)
            desired = round(float(wanted_duration) * SAMPLE_RATE) if wanted_duration else None
            count = 0
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    samples = converted.to_ndarray().T
                    if desired is not None:
                        samples = samples[:max(0, desired - count)]
                    output.write(samples)
                    count += len(samples)
                if desired is not None and count >= desired:
                    break
            for converted in resampler.resample(None):
                samples = converted.to_ndarray().T
                if desired is not None:
                    samples = samples[:max(0, desired - count)]
                output.write(samples)
                count += len(samples)
            if desired is not None and count < desired:
                if desired - count > SAMPLE_RATE * 2:
                    raise RedubError("audio_decode")
                output.write(np.zeros((desired - count, 2), dtype="float32"))
        if not valid_wave(pending, 2):
            raise RedubError("audio_decode")
        pending.rename(target)
        return sf.info(str(target)).duration
    except RedubError:
        raise
    except Exception:
        raise RedubError("audio_decode") from None


def separate_vocals(source, background, vocals, model_root):
    """Run overlapping 20-second chunks and crossfade their common region."""
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    if valid_wave(background, 2) and valid_wave(vocals, 2):
        expected = sf.info(str(source)).frames
        if sf.info(str(background)).frames == expected and sf.info(str(vocals)).frames == expected:
            return
    checkpoint = model_root / "demucs" / "955717e8-8726e21a.th"
    if not checkpoint.is_file():
        raise RedubError("missing_models")
    # Demucs' official release is a trusted Python checkpoint. Verify its
    # published hash prefix before allowing torch's legacy deserializer.
    digest = hashlib.sha256()
    with checkpoint.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if not digest.hexdigest().startswith("8726e21a"):
        raise RedubError("missing_models")
    os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    model = None
    try:
        progress("正在加载本地人声分离模型", 0.06)
        model = get_model("htdemucs", repo=model_root / "demucs").to("cuda").eval()
        vocal_index = model.sources.index("vocals")
        chunk, overlap = SAMPLE_RATE * 20, SAMPLE_RATE
        step = chunk - overlap
        previous_bed = previous_voice = None
        reserve_file(background)
        reserve_file(vocals)
        bed_pending = background.with_name("separate-bed-" + uuid.uuid4().hex + ".wav")
        vocal_pending = vocals.with_name("separate-voice-" + uuid.uuid4().hex + ".wav")
        with sf.SoundFile(str(source)) as inp, \
                sf.SoundFile(str(bed_pending), "w", samplerate=SAMPLE_RATE, channels=2, subtype="FLOAT") as bed, \
                sf.SoundFile(str(vocal_pending), "w", samplerate=SAMPLE_RATE, channels=2, subtype="FLOAT") as voice:
            total = inp.frames
            for offset in range(0, total, step):
                inp.seek(offset)
                audio = inp.read(chunk, dtype="float32", always_2d=True)
                tensor = torch.from_numpy(np.ascontiguousarray(audio.T))
                reference = tensor.mean(0)
                std = float(reference.std())
                if std < 1e-6:
                    clean_voice = np.zeros_like(audio)
                    clean_bed = audio.copy()
                else:
                    mean = float(reference.mean())
                    normalized = (tensor - mean) / std
                    with torch.inference_mode():
                        separated = apply_model(model, normalized[None], device="cuda", shifts=0,
                                                split=True, overlap=0.25, progress=False, num_workers=0)[0]
                    separated = separated.cpu() * std + mean
                    clean_voice = separated[vocal_index].T.numpy()
                    # Sum accompaniment stems, rather than subtracting vocals,
                    # to avoid reinserting residual original speech.
                    clean_bed = torch.stack([separated[i] for i in range(len(model.sources))
                                             if i != vocal_index]).sum(0).T.numpy()
                    del separated, normalized
                if previous_bed is not None:
                    common = min(len(previous_bed), len(clean_bed))
                    ramp = np.linspace(0, 1, common, dtype="float32")[:, None]
                    clean_bed[:common] = previous_bed[:common] * (1 - ramp) + clean_bed[:common] * ramp
                    clean_voice[:common] = previous_voice[:common] * (1 - ramp) + clean_voice[:common] * ramp
                last = offset + len(audio) >= total
                keep = len(audio) if last else len(audio) - overlap
                bed.write(clean_bed[:keep])
                voice.write(clean_voice[:keep])
                previous_bed, previous_voice = clean_bed[keep:].copy(), clean_voice[keep:].copy()
                progress("正在分离原人物语音与背景音乐", 0.08 + 0.27 * min(1, (offset + len(audio)) / total))
                if last:
                    break
        bed_pending.rename(background)
        vocal_pending.rename(vocals)
    except RedubError:
        raise
    except torch.cuda.OutOfMemoryError:
        raise RedubError("memory") from None
    except Exception:
        raise RedubError("separation") from None
    finally:
        if model is not None:
            del model
        gc.collect()
        torch.cuda.empty_cache()


def choose_reference(vocals, cues, target):
    import numpy as np
    import soundfile as sf
    candidates = reference_candidates(cues)
    best = None
    with sf.SoundFile(str(vocals)) as source:
        for candidate in candidates:
            source.seek(round(candidate["start"] * SAMPLE_RATE))
            audio = source.read(round((candidate["end"] - candidate["start"]) * SAMPLE_RATE),
                                dtype="float32", always_2d=True).mean(1)
            rms = float(np.sqrt(np.mean(audio * audio)))
            if rms < 0.003 or np.mean(np.abs(audio) > 0.98) > 0.01:
                continue
            window = max(1, SAMPLE_RATE // 20)
            frames = len(audio) // window
            energy = np.sqrt(np.mean(audio[:frames * window].reshape(frames, window) ** 2, axis=1))
            activity = float(np.mean(energy > max(0.002, rms * 0.15)))
            if activity < 0.35:
                continue
            score = activity + (0.3 if candidate["preferred"] else 0) - abs(len(audio) / SAMPLE_RATE - 8) * .01
            if best is None or score > best[0]:
                best = (score, candidate, audio)
    if best is None:
        raise RedubError("reference")
    _, selected, audio = best
    peak = float(np.max(np.abs(audio)))
    audio *= min(1.0, 0.95 / max(peak, 1e-6))
    reserve_file(target)
    sf.write(str(target), audio, SAMPLE_RATE, subtype="PCM_16")
    return {key: selected[key] for key in ("start", "end", "text")}, audio


def synthesize(groups, reference, reference_audio, directory, model_root):
    import numpy as np
    import soundfile as sf
    import soxr
    import torch
    from qwen_tts import Qwen3TTSModel
    paths, missing = [], []
    voice_identity = hashlib.sha256(reference_audio.tobytes()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    for index, group in enumerate(groups):
        key = hashlib.sha256((PIPELINE_VERSION + VOICE_MODEL + voice_identity + group["english"]).encode("utf-8")).hexdigest()
        path = directory / (key + ".wav")
        paths.append(path)
        if not valid_wave(path, 1):
            missing.append(index)
    if not missing:
        return paths
    model = prompt = None
    try:
        progress("正在加载本地英文音色模型", 0.38)
        model = Qwen3TTSModel.from_pretrained(
            str(model_root / VOICE_MODEL), device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa", local_files_only=True,
        )
        prompt = model.create_voice_clone_prompt(ref_audio=(reference_audio, SAMPLE_RATE),
                                                ref_text=reference["text"], x_vector_only_mode=False)
        for count, index in enumerate(missing):
            progress(f"正在生成英文配音 {count + 1}/{len(missing)}", 0.40 + .47 * count / len(missing))
            parts = []
            for text in split_utterance(groups[index]["english"]):
                # One short utterance at a time bounds KV-cache memory. The full
                # film is still prepared before playback, with a shared voice.
                wavs, rate = model.generate_voice_clone(
                    text=text, language="English", voice_clone_prompt=prompt,
                    non_streaming_mode=True, max_new_tokens=1536,
                    temperature=0.7, top_p=0.9, repetition_penalty=1.08,
                )
                audio = np.asarray(wavs[0], dtype="float32").reshape(-1)
                if not np.isfinite(audio).all() or len(audio) / rate < .15 or len(audio) / rate > 110:
                    raise RedubError("synthesis")
                if float(np.sqrt(np.mean(audio * audio))) < 0.0001:
                    raise RedubError("synthesis")
                if rate != SAMPLE_RATE:
                    audio = soxr.resample(audio, rate, SAMPLE_RATE).astype("float32")
                parts.append(audio)
                parts.append(np.zeros(round(SAMPLE_RATE * .10), dtype="float32"))
            joined = np.concatenate(parts[:-1])
            peak = float(np.max(np.abs(joined), initial=0))
            if peak > .90:
                joined *= .90 / peak
            pending = directory / ("speech-" + uuid.uuid4().hex + ".wav")
            sf.write(str(pending), joined, SAMPLE_RATE, subtype="PCM_16")
            reserve_file(paths[index])
            pending.rename(paths[index])
    except RedubError:
        raise
    except torch.cuda.OutOfMemoryError:
        raise RedubError("memory") from None
    except Exception:
        raise RedubError("synthesis") from None
    finally:
        if prompt is not None:
            del prompt
        if model is not None:
            del model
        gc.collect()
        torch.cuda.empty_cache()
    return paths


def process_job(job):
    import soundfile as sf
    import torch
    started = time.monotonic()
    if job.get("schema") != 1:
        raise RedubError("invalid_job")
    source = Path(str(job.get("source_audio", "")))
    output = Path(str(job.get("output_dir", "")))
    models = Path(str(job.get("model_root", r"D:\dev\TrancyDubbing\models")))
    if not source.is_absolute() or not source.is_file() or not output.is_absolute() or not models.is_absolute():
        raise RedubError("invalid_job")
    if not (models / VOICE_MODEL / "config.json").is_file() or not (models / "demucs" / "htdemucs.yaml").is_file():
        raise RedubError("missing_models")
    if not torch.cuda.is_available():
        raise RedubError("no_cuda")
    torch.cuda.reset_peak_memory_stats()
    torch.set_num_threads(min(4, max(1, os.cpu_count() or 1)))
    # No confidential audio/text is in this fingerprint; it is only a local key.
    key = fingerprint(source, {"cues": job.get("cues"), "source_duration": job.get("source_duration")})
    directory = output / key
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_segments(old["segments"], old["source_duration"], old["output_duration"])
            if (old.get("pipeline_version") == PIPELINE_VERSION and old.get("mix_path") == "english-with-background.wav"
                    and valid_wave(directory / old["mix_path"], 2)
                    and abs(sf.info(str(directory / old["mix_path"])).duration - old["output_duration"]) < 2 / SAMPLE_RATE):
                if old.get("generation") != job.get("generation"):
                    old["generation"] = job.get("generation")
                    save_json(manifest_path, old)
                return manifest_path
        except (KeyError, ValueError, OSError, RedubError):
            pass
    progress("正在读取完整视频音轨", 0.02)
    decoded = directory / "source-stereo.wav"
    duration = decode_audio(source, decoded, job.get("source_duration"))
    cues = normalized_cues(job.get("cues"), duration)
    original_groups = merge_overlapping(cues)
    groups = merge_short_phrases(original_groups)
    bed, vocals = directory / "background.wav", directory / "original-vocals.wav"
    separate_vocals(decoded, bed, vocals, models)
    progress("正在选择原人物清晰音色片段", 0.36)
    reference, reference_audio = choose_reference(vocals, original_groups, directory / "voice-reference.wav")
    speech = synthesize(groups, reference, reference_audio, directory / "speech", models)
    lengths = [sf.info(str(path)).duration for path in speech]
    segments = build_segments(groups, lengths, duration)
    mix = directory / "english-with-background.wav"
    reserve_file(mix)
    pending = directory / ("mix-" + uuid.uuid4().hex + ".wav")
    progress("正在将背景与英文配音合成完整音轨", 0.88)
    try:
        render_mix(bed, speech, segments, pending,
                   lambda p: progress("正在合成背景音乐与英文配音", .88 + .11 * p))
        pending.rename(mix)
    except RedubError:
        raise
    except Exception:
        raise RedubError("render") from None
    output_duration = sf.info(str(mix)).duration
    validate_segments(segments, duration, output_duration, tolerance=2 / SAMPLE_RATE)
    manifest = {"schema": 1, "generation": job.get("generation"), "pipeline_version": PIPELINE_VERSION,
                "mix_path": mix.name, "source_duration": duration, "output_duration": output_duration,
                "segments": segments, "voice_model": VOICE_MODEL,
                "reference": {"start": reference["start"], "end": reference["end"]},
                "sample_rate": SAMPLE_RATE, "prepared_at": time.time(),
                "metrics": {"preparation_seconds": round(time.monotonic() - started, 3),
                            "cuda_peak_allocated_mb": round(torch.cuda.max_memory_allocated() / 1024 ** 2, 1)}}
    save_json(manifest_path, manifest)
    progress("整片英文音色配音已准备完成", 1.0)
    return manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    # Hugging Face telemetry, model fetching and tokenizer network fallback are
    # disabled in the inference subprocess, not in the user's global environment.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="4")
    try:
        if args.job.stat().st_size > 20 * 1024 * 1024:
            raise RedubError("invalid_job")
        job = json.loads(args.job.read_text(encoding="utf-8-sig"))
        if not isinstance(job, dict):
            raise RedubError("invalid_job")
        # Third-party progress/warnings must not break our JSONL protocol.
        with redirect_stdout(sys.stderr):
            path = process_job(job)
        emit("ready", manifest=str(path.resolve()))
        return 0
    except RedubError as error:
        emit("failed", code=error.code, message=str(error))
    except (ImportError, ModuleNotFoundError):
        emit("failed", code="missing_models", message=ERRORS["missing_models"])
    except (OSError, ValueError, TypeError):
        emit("failed", code="invalid_job", message=ERRORS["invalid_job"])
    except Exception:
        emit("failed", code="render", message=ERRORS["render"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
