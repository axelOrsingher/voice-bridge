"""Core Audio I/O and in-process system capture with verified self-exclusion.

Requires macOS 14.2+, Xcode Command Line Tools (first capture only), and sounddevice.
VOZTRAD_MAC_CAPTURE=tap|sck selects the capture API for comparative measurements.
No device-loopback fallback: capturing our own synthesized speech is unsafe.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import platform
from pathlib import Path
import queue
import subprocess
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import soxr

from .config import ROOT
from .i18n import text


class PyAudio:
    def __init__(self):
        self._registration = None
        # Register this PID as an audio client before resolving its tap exclusion ID.
        # The stream emits silence only, and remains open for the capture lifetime.
        if sd.default.device[1] >= 0:
            self._registration = sd.OutputStream(channels=1, dtype="float32", latency="low",
                callback=lambda output, frames, timing, status: output.fill(0))
            self._registration.start()

    def terminate(self):
        if self._registration:
            self._registration.close()
            self._registration = None


def _device(name: str | None, kind: str) -> dict:
    devices = sd.query_devices()
    if name is None:
        index = sd.default.device[0 if kind == "input" else 1]
        if index < 0:
            raise RuntimeError(f"No default {kind} device")
    else:
        matches = [i for i, d in enumerate(devices)
                   if d[f"max_{kind}_channels"] and name.casefold() in d["name"].casefold()]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one {kind} device matching {name!r}; found {len(matches)}. "
                               "List devices with python -m sounddevice.")
        index = matches[0]
    d = dict(devices[index])
    d["index"] = index
    return d


def find_input(pa, name: str | None) -> dict:
    return _device(name, "input")


def find_loopback(pa, name: str | None) -> dict:
    backend = name or os.environ.get("VOZTRAD_MAC_CAPTURE", "tap")
    if backend not in ("tap", "sck"):
        raise ValueError("macOS --loopback must be tap or sck (both exclude engine audio)")
    return {"name": f"macOS {backend} (engine excluded)", "backend": backend}


def find_output(pa, name: str | None) -> dict:
    try:
        return _device("BlackHole 2ch" if name in (None, "CABLE Input") else name, "output")
    except RuntimeError as e:
        raise RuntimeError(text(
            "Virtual microphone missing. Install BlackHole 2ch, restart audio apps, and choose "
            "BlackHole 2ch as the call app microphone. Use --cable for another virtual device.",
            "Falta el micrófono virtual. Instalá BlackHole 2ch, reiniciá las apps de audio y elegilo "
            "como micrófono de la llamada. Usá --cable para otro dispositivo virtual.")) from e


def find_default_output(pa) -> dict:
    device = _device(None, "output")
    if "blackhole" in device["name"].casefold():
        raise RuntimeError(text("Select headphones as the macOS default output before enabling listen-back.",
                                "Elegí auriculares como salida predeterminada antes de activar la escucha."))
    return device


def _native():
    if tuple(map(int, platform.mac_ver()[0].split(".")[:2])) < (14, 2):
        raise RuntimeError("System capture requires macOS 14.2 or newer")
    source = Path(__file__).with_name("native") / "capture_mac.m"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    directory = ROOT / "out" / "native"
    directory.mkdir(parents=True, exist_ok=True)
    library = directory / f"capture-{os.uname().machine}-{digest}.dylib"
    if not library.exists():
        tmp = library.with_suffix(f".{os.getpid()}.dylib")
        try:
            subprocess.run(["xcrun", "clang", "-dynamiclib", "-fobjc-arc", "-fblocks", "-O2",
                            "-mmacosx-version-min=14.2", str(source), "-framework", "Foundation",
                            "-framework", "CoreAudio", "-framework", "ScreenCaptureKit",
                            "-framework", "CoreMedia", "-o", str(tmp)],
                           check=True, capture_output=True, text=True)
            tmp.replace(library)
        except (OSError, subprocess.CalledProcessError) as e:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(text("Cannot build macOS capture. Install Xcode Command Line Tools: xcode-select --install.",
                                    "No se pudo compilar la captura. Instalá Xcode Command Line Tools: xcode-select --install.")
                               + f" ({e})") from e
    lib = ctypes.CDLL(str(library))
    lib.vb_close.argtypes = []
    lib.vb_close.restype = None
    lib.vb_start.argtypes = [ctypes.c_int]
    lib.vb_start.restype = ctypes.c_int
    lib.vb_read.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.vb_read.restype = ctypes.c_int
    lib.vb_rate.restype = ctypes.c_double
    lib.vb_channels.restype = ctypes.c_uint
    lib.vb_dropped.restype = ctypes.c_uint
    lib.vb_error.restype = ctypes.c_char_p
    return lib


class Capture:
    def __init__(self, pa, device: dict):
        self.device = device
        self.q: queue.Queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._stream = None
        self._lib = None
        self._thread = None
        self.dropped = 0
        if "backend" in device:
            # Start before Framer/voice.py read rate and channels; device format may vary.
            self._lib = _native()
            if self._lib.vb_start(device["backend"] == "sck"):
                raise RuntimeError(self._lib.vb_error().decode())
            self.rate = int(self._lib.vb_rate())
            self.channels = self._lib.vb_channels()
        else:
            self.rate = int(device["default_samplerate"])
            self.channels = min(2, device["max_input_channels"])

    def _enqueue(self, data: bytes):
        try:
            self.q.put_nowait((time.perf_counter(), data))
        except queue.Full:
            self.dropped += 1

    def start(self):
        if self._lib:
            self._thread = threading.Thread(target=self._reader, name="coreaudio-in", daemon=True)
            self._thread.start()
        else:
            try:
                self._stream = sd.RawInputStream(device=self.device["index"], samplerate=self.rate,
                    channels=self.channels, dtype="float32", blocksize=0, latency="low",
                    callback=lambda data, frames, timing, status: self._enqueue(bytes(data)))
                self._stream.start()
            except sd.PortAudioError as e:
                raise RuntimeError(text("Microphone unavailable. Allow Microphone access in System Settings > Privacy & Security and restart.",
                                        "Micrófono no disponible. Habilitá Micrófono en Ajustes > Privacidad y seguridad y reiniciá.")) from e

    def _reader(self):
        buf = ctypes.create_string_buffer(65536)
        while not self._stop.is_set():
            size = self._lib.vb_read(buf, len(buf))
            if size > 0:
                self._enqueue(buf.raw[:size])
            else:
                self._stop.wait(0.002)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._lib:
            self._lib.vb_close()
            self._lib = None
        if self._stream:
            self._stream.close()
            self._stream = None


class CablePlayer:
    """Streaming Core Audio output; resampling runs on the producer, never the callback."""
    def __init__(self, pa, device: dict):
        self.device = device
        self.rate = int(device["default_samplerate"])
        self.channels = min(2, device["max_output_channels"])
        self._blocks = deque()
        self._pending = 0
        self._offset = 0
        self._lock = threading.Lock()
        self._rs = None
        self._rs_sr = 0
        self._last_audio_end = 0.0
        self._stream = sd.OutputStream(device=device["index"], samplerate=self.rate,
            channels=self.channels, dtype="float32", blocksize=0, latency="low", callback=self._callback)
        self._stream.start()

    def play(self, audio: np.ndarray, sr: int, last: bool = True):
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if sr != self.rate:
            if self._rs is None or self._rs_sr != sr:
                self._rs = soxr.ResampleStream(sr, self.rate, 1, dtype="float32")
                self._rs_sr = sr
            audio = self._rs.resample_chunk(audio, last=last)
            if last:
                self._rs = None
        if len(audio):
            block = np.repeat(audio[:, None], self.channels, axis=1)
            with self._lock:
                self._blocks.append(block)
                self._pending += len(block)

    def _callback(self, out, frames, timing, status):
        out.fill(0)
        cursor = 0
        with self._lock:
            while self._blocks and cursor < frames:
                block = self._blocks[0]
                n = min(len(block) - self._offset, frames - cursor)
                out[cursor:cursor + n] = block[self._offset:self._offset + n]
                self._offset += n
                cursor += n
                self._pending -= n
                if self._offset == len(block):
                    self._blocks.popleft()
                    self._offset = 0
            if cursor:
                self._last_audio_end = time.perf_counter() + self._stream.latency + cursor / self.rate

    @property
    def queued_s(self) -> float:
        with self._lock:
            return self._pending / self.rate + max(0.0, self._last_audio_end - time.perf_counter())

    def close(self):
        self._stream.close()
