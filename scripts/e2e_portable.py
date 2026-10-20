"""Live, backend-neutral synthetic audio injection against an already running engine.

A: a separate playback process is audible to process-excluding system capture.
B: requires a SECOND virtual cable for input injection; never feed the engine's
   output back into its input. Hold/release PTT via the existing overlay protocol.
Reports engine event latency (speech-end -> subtitle/first generated TTS chunk).
These are NOT DAC/audible latencies. Hardware onset needs external loopback validation.

Examples:
  python scripts/e2e_portable.py --direction A --rounds 3
  python scripts/e2e_portable.py --direction B --inject-output 'BlackHole 16ch'
Start B's engine with --mic 'BlackHole 16ch' --cable 'BlackHole 2ch'.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def inject(path: Path, output: str | None):
    import numpy as np
    import soundfile as sf
    from voztrad.audio import PyAudio, CablePlayer, find_output, find_default_output
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    pa = PyAudio()
    player = None
    try:
        dev = find_output(pa, output) if output else find_default_output(pa)
        player = CablePlayer(pa, dev)
        player.play(np.asarray(audio, np.float32), sr)
        while player.queued_s > 0:
            time.sleep(0.005)
        time.sleep(0.1)
    finally:
        if player:
            player.close()
        pa.terminate()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--direction", choices=["A", "B"], default="A")
    ap.add_argument("--inject-output")
    ap.add_argument("--play", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--dir", type=Path, default=ROOT / "samples/synth")
    ap.add_argument("--output", type=Path, default=ROOT / "out/e2e-portable.json")
    args = ap.parse_args()
    if args.play:
        inject(args.play, args.inject_output)
        return
    if args.direction == "B" and not args.inject_output:
        ap.error("B requires --inject-output for a second virtual cable")
    if args.rounds < 1 or args.limit < 1:
        ap.error("--rounds and --limit must be positive")
    clips = sorted(args.dir.glob(f"{args.direction}_*.wav"))[:args.limit]
    if not clips:
        ap.error("No matching synthetic clips")
    try:
        sock = socket.create_connection(("127.0.0.1", args.port), timeout=3)
    except OSError as e:
        ap.exit(2, f"Engine unavailable on port {args.port}: {e}. Start python -m voztrad run first.\n")
    sock.settimeout(None)
    events = queue.Queue()
    disconnected = threading.Event()
    def reader():
        try:
            with sock.makefile("r", encoding="utf-8") as lines:
                for line in lines:
                    events.put(json.loads(line))
        finally:
            disconnected.set()
    threading.Thread(target=reader, daemon=True).start()
    def send(payload):
        sock.sendall((json.dumps(payload) + "\n").encode())
    def wait_for(predicate):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not disconnected.is_set():
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if predicate(event):
                return event
        raise TimeoutError("No matching engine event; check capture, routing and permissions")
    rows = []
    try:
        send({"cmd": "state"})
        state = wait_for(lambda e: e.get("type") == "state")
        if not state.get("enable_a" if args.direction == "A" else "enable_b"):
            ap.error("Requested engine direction is unavailable; check devices and permissions")
        if args.direction == "B" and args.inject_output.casefold() in (state.get("virtual_output") or "").casefold():
            ap.error("Injection cable must differ from the engine virtual output")
        if args.direction == "B" and state.get("b_mode") not in (None, "ptt"):
            ap.error("B injection requires engine --b-mode ptt")
        for _ in range(args.rounds):
            for clip in clips:
                while not events.empty():
                    events.get_nowait()
                if args.direction == "B":
                    send({"cmd": "ptt", "down": True})
                    time.sleep(0.1)
                command = [sys.executable, str(Path(__file__).resolve()), "--play", str(clip)]
                if args.inject_output:
                    command += ["--inject-output", args.inject_output]
                try:
                    subprocess.run(command, check=True, timeout=args.timeout)
                finally:
                    if args.direction == "B":
                        send({"cmd": "ptt", "down": False})
                phrase = wait_for(lambda e: e.get("type") == "phrase" and e.get("dir") == args.direction)
                row = {"clip": clip.name, "lat": phrase["lat"]}
                rows.append(row)
                print(clip.name, json.dumps(row["lat"]), flush=True)
                time.sleep(2)
    finally:
        sock.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        import numpy as np
        totals = [r["lat"]["total"] for r in rows]
        report = {"scope": "engine events; excludes DAC/audible onset", "direction": args.direction,
                  "n": len(rows), "rows": rows}
        if totals:
            report.update(p50_ms=float(np.percentile(totals, 50)), p95_ms=float(np.percentile(totals, 95)))
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}))


if __name__ == "__main__":
    main()
