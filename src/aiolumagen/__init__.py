"""aiolumagen — async Python library for the Lumagen Radiance Pro RS-232 protocol.

The names re-exported here are the API contract; renaming or removing one is a
breaking change.

Firmware updating lives in :mod:`aiolumagen.firmware` and is deliberately *not*
flattened into this namespace. It speaks a different protocol from everything
here, it is the only part of the library that can leave a device unbootable, and
most consumers never touch it — so importing it should be an explicit act::

    from aiolumagen.firmware import update_firmware

Its exceptions are the exception, so to speak: they're re-exported below, because
a caller has to be able to catch them without importing the subsystem that raises
them.
"""

from __future__ import annotations

from aiolumagen.client import LumagenClient
from aiolumagen.commands import (
    INPUT_LABEL_MAX_LENGTH,
    OSD_LINE_COUNT,
    OSD_LINE_LENGTH,
    OSD_PERSIST_DURATION,
    Aspect,
    Input,
    Memory,
    Misc,
    Navigation,
    Power,
)
from aiolumagen.exceptions import (
    LumagenCommandError,
    LumagenConnectionError,
    LumagenError,
    LumagenFirmwareAbortError,
    LumagenFirmwareError,
    LumagenFirmwareImageError,
)
from aiolumagen.formatting import (
    decode_aspect_ratio,
    decode_output_mask,
    decode_vertical_rate,
    derive_horizontal_resolution,
)
from aiolumagen.state import (
    AutoAspectStatus,
    Colorspace,
    HdrGammaMode,
    HdrStatus,
    InputStatus,
    LumagenState,
    SharpnessSensitivity,
    SourceMode,
    SubtitleShift,
)
from aiolumagen.transport import LumagenTransport

__all__ = [
    # Device limits consumers need for their own input validation — exported so
    # nobody has to hardcode 10/30/2 or reach into an internal module for them.
    "INPUT_LABEL_MAX_LENGTH",
    "OSD_LINE_COUNT",
    "OSD_LINE_LENGTH",
    "OSD_PERSIST_DURATION",
    "Aspect",
    "AutoAspectStatus",
    "Colorspace",
    "HdrGammaMode",
    "HdrStatus",
    "Input",
    "InputStatus",
    "LumagenClient",
    "LumagenCommandError",
    "LumagenConnectionError",
    "LumagenError",
    "LumagenFirmwareAbortError",
    "LumagenFirmwareError",
    "LumagenFirmwareImageError",
    "LumagenState",
    "LumagenTransport",
    "Memory",
    "Misc",
    "Navigation",
    "Power",
    "SharpnessSensitivity",
    "SourceMode",
    "SubtitleShift",
    "decode_aspect_ratio",
    "decode_output_mask",
    "decode_vertical_rate",
    "derive_horizontal_resolution",
]

__version__ = "0.11.0"
