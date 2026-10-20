"""Honest macOS fallback: no supported per-app volume control without rerouting audio."""
import warnings
from .i18n import text


class Ducker:
    available = False
    ducked = False

    def __init__(self, apps=(), level: float = 0.35):
        warnings.warn(text("Per-app ducking is unavailable on macOS. Call audio keeps its volume; "
                           "lower it in the call app when using Spanish listen-back.",
                           "La atenuación por app no está disponible en macOS. El audio de llamada conserva "
                           "su volumen; bajalo en la app al escuchar la traducción."), stacklevel=2)

    def duck(self):
        pass

    def restore(self):
        pass

    def restore_now(self):
        pass
