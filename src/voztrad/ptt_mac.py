"""Global PTT via pynput/Quartz; overlay control survives missing TCC permissions."""
from __future__ import annotations
import time
import warnings
from .i18n import text


class PushToTalkKey:
    def __init__(self, key: str, poll_ms: float = 5.0, on_change=None):
        from pynput import keyboard
        aliases = {"RCTRL": "ctrl_r", "LCTRL": "ctrl_l", "RALT": "alt_r",
                   "LALT": "alt_l", "CAPSLOCK": "caps_lock"}
        name = aliases.get(key.upper(), key.lower())
        self.key = getattr(keyboard.Key, name, None)
        if self.key is None:
            raise ValueError(f"Unsupported PTT key: {key}")
        self.on_change = on_change
        self.pressed = False
        self.t_release = None
        self._listener = None
        self.available = False
        if not keyboard.Listener.IS_TRUSTED:
            warnings.warn(text("Global PTT needs Accessibility/Input Monitoring permission for the launcher. "
                               "Use the overlay PTT button until permission is granted, then restart.",
                               "PTT global necesita permisos de Accesibilidad/Monitoreo de entrada para el lanzador. "
                               "Usá el botón PTT del overlay hasta habilitarlos y reiniciar."), stacklevel=2)
            return
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._listener.start()
        self.available = True

    def _press(self, key):
        if key == self.key:
            self.set_pressed(True)

    def _release(self, key):
        if key == self.key:
            self.set_pressed(False)

    def set_pressed(self, down: bool):
        if self.pressed and not down:
            self.t_release = time.perf_counter()
        changed = self.pressed != down
        self.pressed = down
        if changed and self.on_change:
            self.on_change(down)

    def close(self):
        if self._listener:
            self._listener.stop()
        self.set_pressed(False)
