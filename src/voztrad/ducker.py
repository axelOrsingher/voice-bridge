"""Lower the call app's volume while the Spanish translation plays. Windows uses
pycaw/WASAPI sessions; Linux uses pactl sink-input volumes."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .ducker_win import Ducker  # noqa: F401
elif sys.platform == "darwin":
    from .ducker_mac import Ducker  # noqa: F401,E402
elif sys.platform == "linux":
    from .ducker_linux import Ducker  # noqa: F401

else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
