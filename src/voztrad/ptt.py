"""Global push-to-talk key. Windows polls GetAsyncKeyState; Linux reads evdev."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .ptt_win import PushToTalkKey  # noqa: F401
elif sys.platform == "darwin":
    from .ptt_mac import PushToTalkKey  # noqa: F401,E402
elif sys.platform == "linux":
    from .ptt_linux import PushToTalkKey  # noqa: F401

else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
