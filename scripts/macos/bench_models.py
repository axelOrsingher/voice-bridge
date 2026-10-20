"""Repeatable model-only benchmarks. These numbers exclude capture/playback latency.

Run from the repo root: python scripts/macos/bench_models.py --asr --repeat 5
Only repository synthetic fixtures are used. JSON output is local under out/.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HF_HOME", str(ROOT / "out" / "hf-cache"))
import numpy as np
import soundfile as sf
import soxr


def summary(values):
    return {"n": len(values), "p50_ms": round(float(np.percentile(values, 50)), 2),
            "p95_ms": round(float(np.percentile(values, 95)), 2)}


def measure(fn, repeat):
    fn()  # exclude warm-up
    values = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        values.append((time.perf_counter() - start) * 1000)
    return summary(values)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asr", action="store_true")
    ap.add_argument("--whisper-model")
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--voice", type=Path, help="Your own consenting voice reference for Pocket TTS")
    ap.add_argument("--output", type=Path, default=ROOT / "out" / "macos-model-bench.json")
    args = ap.parse_args()
    if args.repeat < 2:
        ap.error("--repeat must be at least 2")
    import onnxruntime as ort
    report = {"machine": platform.machine(), "os": platform.platform(),
              "ort": ort.__version__, "providers": ort.get_available_providers(), "measurements": {}}
    results = report["measurements"]

    def save(label, value):
        results[label] = value
        print(label, json.dumps(value), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    clips = []
    for path in [*sorted((ROOT / "samples/synth").glob("A_*.wav"))[:4],
                 *sorted((ROOT / "samples/synth").glob("B_*.wav"))[:4]]:
        audio, sr = sf.read(path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            audio = soxr.resample(audio, sr, 16000).astype(np.float32)
        clips.append((path.name, audio, "en" if path.name.startswith("A_") else "es"))
    from voztrad.vad import SileroVAD
    vad = SileroVAD()
    save("silero_frame", measure(lambda: vad(clips[0][1][:512]), args.repeat * 10))
    from voztrad.smartturn import SmartTurn
    turn = SmartTurn()
    save("smart_turn", measure(lambda: turn(clips[0][1]), args.repeat))
    from voztrad.mt import Translator
    sentences = {"en": ["Could you share your screen?", "Let's push the launch to next week.",
                        "The client approved the new proposal.", "That makes sense. Go ahead."],
                 "es": ["¿Podés compartir tu pantalla?", "Vamos a pasar el lanzamiento a la semana que viene.",
                        "El cliente aprobó la nueva propuesta.", "Tiene sentido. Adelante."]}
    for lang, texts in sentences.items():
        baseline = None
        for compute in ("float32", "int8"):
            for threads in (1, 2, 4):
                mt = Translator(lang, "es" if lang == "en" else "en", device="cpu",
                                threads=threads, compute_type=compute)
                outputs = [mt.translate(t) for t in texts]
                if baseline is None:
                    baseline = outputs
                timing = measure(lambda: [mt.translate(t) for t in texts], args.repeat)
                timing["scope"] = "batch of four sentences"
                timing["different_from_float32"] = sum(a != b for a, b in zip(outputs, baseline))
                save(f"mt_{lang}_{compute}_{threads}threads", timing)
    if args.asr:
        from voztrad.asr import load
        model = load("whisper", args.whisper_model) if args.whisper_model else load("parakeet", parakeet_quant="int8")
        for name, audio, lang in clips:
            save(f"{model.name}_{name}", {**measure(lambda: model.transcribe(audio, lang), args.repeat),
                "audio_s": round(len(audio) / 16000, 2)})
    if args.voice:
        from voztrad.tts_pocket import PocketTTS
        model = PocketTTS(args.voice)
        def first_audio():
            stream = model.stream("Could you share your screen so we can review the proposal?")
            try:
                next(stream)
            finally:
                stream.close()
        save("pocket_first_audio", measure(first_audio, args.repeat))


if __name__ == "__main__":
    main()
