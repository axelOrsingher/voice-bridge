"""macOS inference provider selection. CPU is the portable baseline, CoreML is opt-in."""
import os


def providers(available: list[str]) -> list:
    name = os.environ.get("VOZTRAD_ASR_PROVIDER", "cpu").lower()
    if name == "cpu":
        return ["CPUExecutionProvider"]
    if name != "coreml":
        raise ValueError("VOZTRAD_ASR_PROVIDER must be cpu or coreml")
    if "CoreMLExecutionProvider" not in available:
        raise RuntimeError("This ONNX Runtime build does not include CoreMLExecutionProvider")
    return [("CoreMLExecutionProvider", {"ModelFormat": "MLProgram",
                                        "MLComputeUnits": "ALL"}), "CPUExecutionProvider"]
