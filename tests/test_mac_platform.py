"""Platform decisions and lifecycle invariants that do not require TCC/audio hardware."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from voztrad.hwdetect import choose_profile


def hardware(system="darwin", machine="x86_64", **extra):
    return {"platform": system, "machine": machine, "chip": "test fixture",
            "ram_gb": 16, "has_cuda": False, "gpu": None,
            "has_voice_ref": False, "llama_server": None, **extra}


def test_mac_profiles_do_not_request_cuda_or_gpu_keepalive():
    for machine in ("arm64", "x86_64"):
        profile = choose_profile(hardware(machine=machine))
        assert profile["mt_compute_type"] == "int8"
        assert not profile["gpu_warm"]
        assert profile["smart_turn"]
        assert not profile["refine"]


def test_nvidia_profiles_preserve_precision_and_memory_budget():
    for system in ("win32", "linux"):
        small = hardware(system, has_cuda=True,
                         gpu={"name": "Turing fixture", "compute_cap": 7.5, "vram_mib": 4096})
        profile = choose_profile(small)
        assert profile["parakeet_quant"] is None
        assert profile["mt_compute_type"] == "int8"
        assert not profile["refine"]
        assert profile["gpu_warm"]
        large = hardware(system, has_cuda=True, llama_server="fixture",
                         gpu={"name": "Ampere fixture", "compute_cap": 8.6, "vram_mib": 6144})
        profile = choose_profile(large)
        assert profile["parakeet_quant"] == "fp16"
        assert profile["mt_compute_type"] == "float16"
        assert profile["refine"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS provider selection")
def test_mac_coreml_request_is_explicit_and_checks_capability(monkeypatch):
    from voztrad.asr_mac import providers
    monkeypatch.delenv("VOZTRAD_ASR_PROVIDER", raising=False)
    assert providers(["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    monkeypatch.setenv("VOZTRAD_ASR_PROVIDER", "coreml")
    with pytest.raises(RuntimeError, match="CoreML"):
        providers(["CPUExecutionProvider"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX instance locking")
def test_single_instance_releases_after_process_exit():
    from voztrad.__main__ import _single_instance
    name = f"voztrad-test-{os.getpid()}"
    assert _single_instance(name)
    command = [sys.executable, "-c",
               f"from voztrad.__main__ import _single_instance; print(_single_instance({name!r}))"]
    assert subprocess.check_output(command, text=True).strip() == "False"
    # Closing the owner descriptor releases the lock without deleting the inode.
    from voztrad import __main__
    __main__._mutex.close()
    assert subprocess.check_output(command, text=True).strip() == "True"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS child watchdog")
def test_watchdog_stops_child_after_parent_crash(tmp_path):
    import signal
    pidfile = tmp_path / "child.pid"
    stopped = tmp_path / "child.stopped"
    child_code = "\n".join([
        "import os, signal, time",
        "from pathlib import Path",
        f"def stop(*args): Path({str(stopped)!r}).touch(); os._exit(0)",
        "signal.signal(signal.SIGTERM, stop)",
        "time.sleep(60)",
    ])
    code = "\n".join([
        "import os, subprocess, sys, time",
        "from pathlib import Path",
        "from voztrad.refine import _guard_child",
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
        "guard = _guard_child(child)",
        f"Path({str(pidfile)!r}).write_text(str(child.pid))",
        "time.sleep(0.3)",
        "os._exit(23)",
    ])
    parent = subprocess.Popen([sys.executable, "-c", code])
    try:
        assert parent.wait(timeout=10) == 23
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if stopped.exists():
                return
            time.sleep(0.1)
        pytest.fail("llama-server substitute survived parent crash")
    finally:
        if parent.poll() is None:
            parent.kill()
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Audio playback queue")
def test_mac_streaming_resampler_flushes_and_duplicates_channels(monkeypatch):
    import numpy as np
    from voztrad import audio_mac
    class Stream:
        latency = 0
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
        def start(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(audio_mac.sd, "OutputStream", Stream)
    device = {"name": "test output", "default_samplerate": 48000,
              "max_output_channels": 2, "index": 0}
    player = audio_mac.CablePlayer(None, device)
    # Non-final short chunks exercise soxr's buffered tail (Pocket/Piper stream this way).
    audio = np.sin(np.arange(2400, dtype=np.float32) * 0.05)
    for chunk in np.array_split(audio, 7):
        player.play(chunk, 24000, last=False)
    player.play(np.zeros(0, np.float32), 24000, last=True)
    assert player._pending == 4800
    output = np.zeros((5000, 2), np.float32)
    player._callback(output, len(output), None, None)
    np.testing.assert_array_equal(output[:, 0], output[:, 1])
    assert np.max(np.abs(output[:4800])) > 0.9
    assert not np.any(output[4800:])
    assert player._pending == 0
    player.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS listen-back routing")
def test_mac_listen_back_rejects_virtual_default_output(monkeypatch):
    from voztrad import audio_mac
    monkeypatch.setattr(audio_mac, "_device", lambda *args: {"name": "BlackHole 2ch"})
    with pytest.raises(RuntimeError, match="headphones"):
        audio_mac.find_default_output(None)


@pytest.mark.parametrize("flags, expected", [([], False), (["--refine"], True),
    (["--no-refine"], False), (["--no-auto"], True)])
def test_explicit_refiner_flag_overrides_auto_profile(monkeypatch, flags, expected):
    from voztrad import __main__, engine, hwdetect, runtime
    seen = []
    class Engine:
        def __init__(self, cfg, **kwargs):
            seen.append(cfg.refine)
        def run(self):
            pass
    monkeypatch.setattr(engine, "Engine", Engine)
    monkeypatch.setattr(__main__, "_port_open", lambda port: False)
    monkeypatch.setattr(hwdetect, "detect", lambda: hardware())
    monkeypatch.setattr(runtime, "keep_awake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["voztrad", "run", *flags])
    __main__.main()
    assert seen == [expected]
