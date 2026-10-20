"""Optional platform integrations kept out of the engine and UI."""
import sys
import warnings


def keep_awake():
    if sys.platform == "darwin":
        from .runtime_mac import keep_awake as native
        native()
    elif sys.platform == "win32":
        from .winperf import disable_throttling
        disable_throttling()


def configure_overlay(widget):
    if sys.platform == "darwin":
        try:
            from .runtime_mac import configure_overlay as native
            native(widget)
        except Exception as e:
            from .i18n import text
            warnings.warn(text("Could not enable overlay across Spaces: ",
                               "No se pudo habilitar el overlay en todos los Spaces: ") + str(e), stacklevel=2)
