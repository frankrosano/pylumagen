"""Firmware updates for the Lumagen Radiance Pro.

A separate subsystem from the normal-mode client, because the device's
firmware-update command set is a separate protocol: entered with ``M0931``,
unterminated, built on sticky registers, and able to leave the unit unbootable.
It is never engaged by :class:`~aiolumagen.client.LumagenClient`.

The flow, end to end::

    from aiolumagen.firmware import update_firmware

    result = await update_firmware(
        "esphome://10.0.0.42:6053/?port_name=Lumagen&key=<psk>",
        "radiance_pro030326.exe",
        progress=lambda p: print(p.phase, p.message),
    )

That does four things: parses the vendor's updater EXE and extracts the firmware
images; asks the device what it currently holds; decides which sections actually
differ; and writes only those, over whichever transport the URL names — direct
serial, raw TCP, or an ESPHome ``serial_proxy``.

Pass ``dry_run=True`` to get the plan and change nothing. Worth doing first: it
reports which sections would be written and roughly how long that takes, and the
answer varies by an order of magnitude between a section-0-only update and one
that also rewrites section 1.

Layering, mirroring the rule the normal-mode :mod:`aiolumagen.protocol` follows:

* :mod:`~aiolumagen.firmware.container` — the ``0xBABABEBE`` wrapper
* :mod:`~aiolumagen.firmware.extract` — vendor EXE parsing
* :mod:`~aiolumagen.firmware.protocol` — commands, replies, flash map, timings
* :mod:`~aiolumagen.firmware.plan` — which sections need writing
* :mod:`~aiolumagen.firmware.session` — the only module that does I/O

Everything except ``session`` is pure and synchronous, so the whole command
sequence for an update can be generated and checked against recorded vendor
transcripts without a device present.

Two operational constraints worth knowing before you call this:

**One subscriber.** ESPHome's ``serial_proxy`` serves a single subscriber at a
time. A :class:`~aiolumagen.client.LumagenClient` connected to the same bridge
must be disconnected first, and in Home Assistant that means unloading the
config entry.

**A successful update powers the unit off.** That is the device's own behaviour
and not a fault: the final ``Z97`` is what makes the newly promoted firmware
load. Expect the unit to be in standby afterwards.

Chip images (``hdmi_rx``/``hdmi_tx``/``hdmi_ntx``) are extracted and reported but
never written — see :mod:`~aiolumagen.firmware.plan`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from aiolumagen.exceptions import LumagenFirmwareImageError
from aiolumagen.firmware.container import (
    Container,
    ContainerHeader,
    additive_checksum,
    expected_stored_checksum,
    find_containers,
    parse_container,
)
from aiolumagen.firmware.extract import (
    FirmwareBundle,
    FirmwareImage,
    extract_images,
)
from aiolumagen.firmware.plan import (
    WRITABLE_SECTIONS,
    DeviceStatus,
    PlannedSection,
    SectionAction,
    UpdatePlan,
    plan_update,
)
from aiolumagen.firmware.protocol import (
    SESSION_BAUD,
    SUPPORTED_BAUDS,
    DeviceIdentity,
    FirmwareRevision,
)
from aiolumagen.firmware.session import (
    AuditResult,
    FirmwareSession,
    ProgressCallback,
    UpdatePhase,
    UpdateProgress,
    UpdateResult,
)

__all__ = [
    "SUPPORTED_BAUDS",
    "WRITABLE_SECTIONS",
    "AuditResult",
    "Container",
    "ContainerHeader",
    "DeviceIdentity",
    "DeviceStatus",
    "FirmwareBundle",
    "FirmwareImage",
    "FirmwareRevision",
    "FirmwareSession",
    "PlannedSection",
    "ProgressCallback",
    "SectionAction",
    "UpdatePhase",
    "UpdatePlan",
    "UpdateProgress",
    "UpdateResult",
    "additive_checksum",
    "expected_stored_checksum",
    "extract_images",
    "find_containers",
    "load_updater",
    "parse_container",
    "plan_update",
    "update_firmware",
]

DEFAULT_UPDATE_BAUDRATE = 230400
"""The vendor's own "fast" setting, and the rate the flush barrier was qualified at.

Deliberately not 115200, which sits between the two and is the worst of the
three: it was called qualified off a single clean run and later failed roughly
one attempt in four. With the barrier in place 230400 has proven more reliable
than 115200 was without it, so there is no reason to prefer the middle rate. Drop
to 9600 if a link is genuinely marginal.
"""


async def load_updater(source: str | Path | bytes) -> FirmwareBundle:
    """Extract firmware images from a vendor updater EXE.

    :param source: path to the ``.exe``, or its bytes if you've already read it.
    :raises LumagenFirmwareImageError: if it isn't a PE, or a container is corrupt.

    Reading from disk happens in a worker thread — a 5 MB read is not something to
    do on the event loop, and this library is used inside Home Assistant.
    """
    if isinstance(source, bytes):
        return extract_images(source)
    path = Path(source)
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError as err:
        raise LumagenFirmwareImageError(f"could not read {path}: {err}") from err
    return extract_images(data, source_name=path.name)


async def update_firmware(
    url: str,
    source: str | Path | bytes,
    *,
    baudrate: int = DEFAULT_UPDATE_BAUDRATE,
    dry_run: bool = False,
    promote: bool = True,
    force: bool = False,
    only: Iterable[str] | None = None,
    progress: ProgressCallback | None = None,
) -> UpdateResult:
    """Update a Lumagen's firmware from a vendor updater EXE.

    The one call that does the whole job: extract, compare, write only what
    differs, and hand the device back in a state the next client can talk to.

    :param url: serialx URL for the device. For an ESPHome bridge,
        ``esphome://<host>:6053/?port_name=Lumagen&key=<psk>``.
    :param source: the vendor ``.exe`` — path or bytes.
    :param baudrate: transfer rate; see :data:`DEFAULT_UPDATE_BAUDRATE`.
    :param dry_run: report the plan and write nothing.
    :param promote: when False, section 0 is staged and verified but not copied
        over live firmware. The full path with nothing at stake.
    :param force: write regardless of whether the device already holds the image.
        Overrides the up-to-date comparison only — never a correctness gate; see
        :func:`~aiolumagen.firmware.plan.plan_update`.
    :param only: restrict to specific sections, e.g. ``["section0"]`` or
        ``["section1"]``. For deliberate single-section flashes.
    :param progress: optional callable taking an
        :class:`~aiolumagen.firmware.session.UpdateProgress`.
    :raises LumagenFirmwareImageError: the EXE is unusable, or `only` names
        something unwritable. Nothing was contacted.
    :raises LumagenFirmwareAbortError: a safety gate refused, or the run stopped
        before committing. **Live firmware is unchanged** and a power cycle is the
        entire recovery.
    :raises LumagenFirmwareError: anything else, including the cases where the
        outcome could not be confirmed. Read the message before power-cycling.
    :raises LumagenConnectionError: the transport could not be opened.

    On success with ``promote=True`` the unit powers itself down; that is how the
    new firmware gets loaded.
    """
    bundle = await load_updater(source)
    async with FirmwareSession(url, baudrate=SESSION_BAUD) as session:
        return await session.run_update(
            bundle,
            baudrate=baudrate,
            dry_run=dry_run,
            promote=promote,
            force=force,
            only=only,
            progress=progress,
        )
