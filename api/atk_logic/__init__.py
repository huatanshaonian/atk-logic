"""Cross-platform (Windows / Linux) Python API for the ATK-Logic (ALIENTEK DL16)
logic analyzer, plus offline tools for its .atkdl project files.

Quick start::

    from atk_logic import AtkLogic, list_devices, load, decode

    print(list_devices())
    with AtkLogic.open() as la:
        cap = la.capture(channels=[0, 1], samplerate=10e6, duration=0.01)
    cap.save("cap.vcd")
    print(cap.summary())
"""

__version__ = "0.1.0"

from .capture import Capture, Channel  # noqa: E402
from .atkdl import read_atkdl  # noqa: E402
from .decode import decode, list_decoders  # noqa: E402
from .device import (  # noqa: E402
    AtkError, AtkLogic, CaptureError, DeviceAccessError, DeviceInfo, DeviceNotFound,
    DeviceStateError, list_devices,
)



def load(path: str) -> Capture:
    """Load a capture file: .atkdl (saved by the ATK-Logic GUI) or .atkjson (saved by this API)."""
    import os
    low = path.lower()
    if not os.path.exists(path):
        raise FileNotFoundError("file not found: %s" % path)
    if low.endswith(".atkdl"):
        return read_atkdl(path)
    if low.endswith(".atkjson"):
        return Capture.from_atkjson(path)
    raise ValueError("unsupported input %r (use .atkdl from the GUI or .atkjson from this tool)" % path)


__all__ = [
    "load", "AtkLogic", "list_devices", "Capture", "Channel", "read_atkdl", "decode", "list_decoders",
    "DeviceInfo", "AtkError", "DeviceNotFound", "DeviceAccessError", "DeviceStateError",
    "CaptureError", "__version__",
]
