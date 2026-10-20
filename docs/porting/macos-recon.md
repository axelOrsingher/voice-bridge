# macOS reconnaissance — Intel baseline, experimental port

Date: 2026-10-20. The requested M4 is **not the machine available to this session**.
The owner explicitly approved continuing on this Intel Mac. This is an implementation
and CPU-baseline report, **not evidence that live macOS calls work or match Windows**.

## Environment actually observed

- MacBookPro15,1, Intel Core i7, 2.2 GHz, six physical / twelve logical cores, 16 GiB RAM.
- macOS 15.7.9 (24G830), x86_64; no Rosetta translation flag.
- System Profiler lists Intel UHD 630 and Radeon Pro 555X (4 GiB), both Metal-capable.
- **Inside this executor**, `MTLCopyAllDevices()` returns zero devices and
  `sounddevice.query_devices()` returns an empty list. Hardware capability and executor
  access are different things. GPU inference and audio I/O cannot be validated here.
- `sysctl machdep.cpu.brand_string` is denied here; detection falls back to selected
  System Profiler chip metadata without collecting machine identifiers.
- Local Python 3.11.17, CTranslate2 4.8.2, onnx-asr 0.12.0, ONNX Runtime 1.23.2,
  sounddevice 0.5.6, PySide6 6.11.2; no PyTorch installed.
- ORT advertises CoreML, Azure and CPU providers; provider enumeration alone does not
  prove a graph can execute on CoreML.

Dependency resolution originally selected ORT 1.30, which has no Intel macOS wheel.
`required-environments` now covers both Mac architectures; the lock selects 1.23.2 on
Intel and 1.30.0 on arm64. The existing CUDA-12 ORT dependency remains for Windows/Linux;
CPU ORT is allowed only on Darwin so Piper does not overwrite the CUDA installation.

## Audio API investigation and actual attempts

Both backends are in an Objective-C dylib loaded **inside the engine process**. An external
capture helper with `excludesCurrentProcessAudio` would exclude the helper rather than
Python's translated speech. The native source is packaged with `voztrad`; first capture
builds a source-hashed, architecture-specific binary in ignored `out/native/` using Xcode
Command Line Tools. Minimum OS is checked before loading: macOS 14.2.

| Candidate | Implementation | Observed in this executor | Capture p50/p95 |
|---|---|---|---|
| Core Audio Process Tap | Stereo global tap; resolves this PID to an audio process object; exclusion list is mandatory; private aggregate; unmuted source | PID translation returned OSStatus 0 but no process object. Failed closed, without capturing anything. | Unavailable |
| ScreenCaptureKit | In-process SCStream, `capturesAudio=true`, `excludesCurrentProcessAudio=true`, stereo 48 kHz; only audio output is registered | Shareable-content completion did not arrive; bounded 10-second timeout reports TCC/desktop requirements | Unavailable |

The `tap` default is **provisional**, not the winner of a latency comparison. Select the
other candidate with `VOZTRAD_MAC_CAPTURE=sck` or `--loopback sck`. Neither falls back to
ordinary output-device loopback. A silent Core Audio output stream registers the engine as
an audio client before its PID is resolved; it emits no translated audio. No successful
self-exclusion test has been possible here.

Audio callbacks copy/enqueue into a bounded native ring. A reader thread transfers blocks
to a bounded Python queue; framing, resampling and inference remain off capture callbacks.
Mic input and streaming output use sounddevice/Core Audio; output rate follows the selected
device and soxr flushes its streaming tail. Unit tests cover the stereo/resampling tail and
reject BlackHole as the default Spanish listen-back output. Device disconnects and long
capture reliability still need live testing.

Apple references:
[Process Tap sample](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps),
[CATapDescription](https://developer.apple.com/documentation/coreaudio/catapdescription),
[ScreenCaptureKit exclusion](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/excludescurrentprocessaudio).

## Warm CPU measurements on this Mac

Reproduce from the repository root:

```sh
uv sync --extra ui
.venv/bin/python scripts/fetch_models.py --test-only
.venv/bin/python scripts/macos/bench_models.py --asr --repeat 10 \
  --output out/macos-model-bench-clean.json
```

Fixtures are public repository synthetic audio, never real calls. One warm-up is excluded
per measurement. Percentiles use NumPy linear interpolation. These are wall-clock model
inference measurements under the executor, without capture, endpointing wait, queues or DAC
latency. Thermal state/system load were not controlled. Exploratory overlapping benchmarks
were discarded; the table uses the subsequent single benchmark process. Small background
build/probe activity was not isolated, so tails should be repeated on a quiet desktop.

| Stage / input | n | p50 ms | p95 ms |
|---|---:|---:|---:|
| Silero, one 512-sample frame | 100 | 0.23 | 0.36 |
| Smart Turn, A_en01 | 10 | 103.09 | 105.72 |
| Parakeet int8, A_en01 (1.98 s) | 10 | 183.19 | 205.60 |
| Parakeet int8, A_en02 (2.95 s) | 10 | 252.79 | 278.86 |
| Parakeet int8, A_en03 (3.71 s) | 10 | 323.08 | 442.07 |
| Parakeet int8, A_en04 (3.86 s) | 10 | 318.88 | 396.63 |
| Parakeet int8, B_long (15.98 s) | 10 | 1127.86 | 1206.90 |
| Parakeet int8, B_rio1 (2.28 s) | 10 | 207.05 | 221.88 |
| Parakeet int8, B_rio2 (2.79 s) | 10 | 244.01 | 257.91 |
| Parakeet int8, B_rio3 (3.05 s) | 10 | 273.07 | 492.77 |

MT uses **four sequential sentences per trial**, not one sentence and not a CT2 token batch.
Do not present these values as per-sentence latency.

| MT CPU / threads | en→es p50/p95 ms | es→en p50/p95 ms |
|---|---:|---:|
| float32 / 1 | 603.90 / 616.11 | 613.69 / 642.23 |
| float32 / 2 | 412.68 / 442.61 | 417.95 / 426.82 |
| float32 / 4 | 399.53 / 412.86 | 400.48 / 448.44 |
| int8 / 1 | 248.57 / 260.24 | 246.87 / 261.77 |
| int8 / 2 | 179.67 / 188.01 | 183.10 / 191.27 |
| int8 / 4 | 139.71 / 144.78 | 143.62 / 171.95 |

Keep CPU int8 / four MT threads for the Intel baseline. Int8 differed from float32 on
one of four en→es test sentences and zero of four es→en sentences; identical output is not
a quality metric, and this tiny set does not establish general quality equivalence.
No shared MT defaults were changed.

## Alternatives / dead ends

- CoreML with the default onnx-asr Conv-STFT preprocessor failed graph initialization:
  `axis 2 is not in valid range [-2,1]`. Keep preprocessing/resampling on CPU on macOS.
- Retrying CoreML with CPU preprocessing reached the encoder, then failed to build an
  MLModel execution plan, error -14. No CoreML inference percentile exists. CPU stays the
  default; `VOZTRAD_ASR_PROVIDER=coreml` remains an explicit diagnostic experiment.
  [ORT CoreML options](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html).
- whisper.cpp/llama.cpp Metal could not be benchmarked: zero accessible Metal devices.
  No llama-server is installed in this environment. No refiner speed/memory claim is made.
- parakeet-mlx and mlx-whisper need an Apple Silicon run; this Intel machine cannot provide
  M4/ANE/unified-memory measurements. The arm64 profile is a conservative CPU baseline.
- Pocket TTS needs the owner's consenting voice reference. None is present, and no voice
  was recorded or cloned. `bench_models.py --voice models/voice/me.wav` measures first
  generated audio once the owner records it; quality and audible onset remain unvalidated.

## Integration and remaining work

- Explicit Darwin dispatch for audio, global PTT and ducking; unsupported OSes fail explicitly.
- PTT uses pynput/Quartz. Missing Accessibility/Input Monitoring permissions leave the overlay
  PTT command available and publish a status message. Physical F8/focus behavior is untested.
- There is **no per-app ducking** yet. Native taps can mute their sources, but a muted replay
  path would need separately validated timing, routing and crash restoration. The current
  backend leaves call volume alone, warns clearly and reports ducking unavailable.
- The existing voice guide already uses neutral audio I/O on main; macOS support comes from
  the new backend. Windows-only voice imports described in the task were already removed.
- Existing llama-server lookup and macOS watchdog were already on main. A subprocess test
  confirms a stand-in server receives termination after parent `os._exit`, without running
  an actual llama model. Metal inference and abrupt-kill GPU cleanup remain unverified.
- POSIX file locking now prevents a second overlay; a subprocess test checks lock ownership
  and release. Windows retains its named mutex.
- NSProcessInfo activity prevents App Nap for engine/launcher lifetime. CLI execution tested
  this API; throttling behavior under sleep/idle needs a desktop measurement.
- Native NSWindow collection behaviors request all Spaces/full-screen auxiliary display.
  The draggable Qt implementation is preserved. Actual Spaces/full-screen behavior needs a
  human check. The integration skips non-Cocoa Qt backends for headless tests.
- Finder launcher: `scripts/macos/Voztrad.command`. No system driver, global defaults, TCC
  permissions or user launch agents were installed or modified by this change.

`python -m pytest tests -q`: **34 passed, 1 skipped** after new platform/lifecycle/streaming
tests. The skipped case is the pre-existing optional recording of the user.
The Objective-C bridge compiles against the installed SDK with `-Wall -Wextra -Werror`.
A Qt offscreen smoke opens/closes the overlay; it does not validate native Spaces behavior. Windows/Linux native integration
was not run; profile tests preserve the reference CUDA decisions and their backend files
were not edited. Shared changes affect startup/cleanup and provider selection, not the
segmenter, scheduler or translation hot path.

Live engine attempt (`--no-b --no-listen --no-refine`) loaded models, reported the missing
audio process object, and exited normally with no available direction. The portable live
harness was invoked, but no audio-enabled engine could be kept running in this executor;
there is **no successful live E2E result**. The harness measures engine event latency, not
physical audible onset, and requires a separate injection cable for B.

| Speech end → output | Windows reference p50 | Linux reference p50 | This Intel Mac p50/p95 |
|---|---:|---:|---|
| Subtitle | ~390 ms | ~600 ms | Unavailable: live capture blocked |
| Spanish in headphones | ~530 ms | 720–890 ms | Unavailable: audio devices inaccessible |
| Cloned voice in virtual mic | Not supplied | ~780 ms | Unavailable: driver/voice reference absent |

Reference values come from the task; their p95 values were not supplied. Model-only numbers
above must not be substituted into this table or added together as measured E2E latency.

Before declaring the port complete, run both capture candidates in a normal terminal,
measure injected-audio latency/reliability and self-exclusion, install/validate BlackHole,
record the owner's voice, measure Pocket first audio, benchmark Metal/MLX on Apple Silicon,
and perform the README checklist in a real call.
