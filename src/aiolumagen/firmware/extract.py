"""Pull firmware images out of a vendor Radiance Pro updater ``.exe``.

The updater is an ordinary 32-bit Windows PE, and it carries its payload in two
completely different places — which is the whole reason this module is more than
a resource dump:

* **``section1`` and the three HDMI chip images** are ``RT_RCDATA`` resources,
  each wrapped in a :mod:`~aiolumagen.firmware.container`.
* **``section0``**, the main CPU firmware and the only image that changes every
  release, is a plain C array (``swdata``) in the updater's ``.data`` section
  with no container, no resource entry, and nothing marking its extent.

A naive "scan the file for container magic" extractor finds four images and
silently misses the important one. So ``section0``'s address and length are
recovered from the updater's own machine code — see
:func:`find_swdata_descriptor`.

PE walking is hand-rolled rather than delegated to ``pefile``. What's needed is
a few hundred bytes of header parsing, and the alternative is a runtime
dependency that ``ha-lumagen`` would inherit into every Home Assistant install
for the sake of one optional feature.

Pure sync, no I/O: everything takes ``bytes``. Reading the file is the caller's
business, which keeps this testable against a synthetic PE and keeps blocking
disk reads out of the event loop.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from aiolumagen.exceptions import LumagenFirmwareImageError
from aiolumagen.firmware.container import (
    Container,
    additive_checksum,
    parse_container,
)
from aiolumagen.firmware.protocol import BOOT_SECTOR_LEN, FirmwareRevision

SECTION0: Final = "section0"
SECTION1: Final = "section1"

RESOURCE_NAMES: Final[dict[int, str]] = {
    131: SECTION1,
    132: "hdmi_rx",
    133: "hdmi_tx",
    134: "hdmi_ntx",
}
"""``RT_RCDATA`` resource id to logical image name."""

CHIP_NAMES: Final = ("hdmi_rx", "hdmi_tx", "hdmi_ntx")
"""The SiI9777 images. Extracted for reporting; deliberately never flashed.

No observed vendor session has written these in Auto mode, and they were
identical across every release sampled during development — so there was nothing
available to test a write path against. That is a limit of the sample, not a fact
about the firmware line: Lumagen publishes far more releases than were examined,
and one that changes a chip image may well exist. See
:mod:`aiolumagen.firmware.plan`.
"""

RT_RCDATA: Final = 10

SWDATA_VA_FALLBACK: Final = 0x0040A020
SWDATA_LEN_FALLBACK: Final = 0x90012
"""Last-resort ``swdata`` descriptor, used only if the code scan fails.

These held for the 092025/112325/120325/030326 releases, but **the length varies
per release** — a captured 030225 session copies ``0x6FBCA``. Using the fallback
length against a release that disagrees would flash a truncated or over-long
image, so :func:`extract_images` records which route was taken and
:attr:`FirmwareImage.descriptor_recovered` reports it.
"""

_RELEASE_RE: Final = re.compile(r"(\d{6})")

_MAX_REASONABLE_SWDATA: Final = 0x200000
_MIN_REASONABLE_SWDATA: Final = 0x10000


def _u16(data: bytes, off: int) -> int:
    value: int = struct.unpack_from("<H", data, off)[0]
    return value


def _u32(data: bytes, off: int) -> int:
    value: int = struct.unpack_from("<I", data, off)[0]
    return value


@dataclass(frozen=True, slots=True)
class PeSection:
    """One entry from the PE section table."""

    name: str
    virtual_address: int
    virtual_size: int
    raw_address: int
    raw_size: int

    @property
    def span(self) -> int:
        """Bytes this section covers in memory.

        ``max`` of the two sizes because a section's virtual extent can exceed
        its on-disk bytes (BSS-style tail) *and* the on-disk bytes can be rounded
        up past the virtual size by file alignment. Address translation has to
        accept a hit anywhere in the union or it rejects valid lookups.
        """
        return max(self.virtual_size, self.raw_size)


@dataclass(frozen=True, slots=True)
class FirmwareImage:
    """One extracted firmware image, plus how it goes onto the wire."""

    name: str
    payload: bytes
    """The logical firmware bytes: container payload, or the raw ``swdata`` array."""

    source: str
    """Human-readable provenance, for diagnostics and for the update report."""

    container: Container | None = None
    """The container, when this image had one. ``None`` for ``section0``."""

    descriptor_recovered: bool = True
    """False when ``section0`` fell back to hardcoded constants. See below.

    Only meaningful for ``section0``. A false value means the length was *not*
    read out of the updater and may not match this release, which
    :func:`~aiolumagen.firmware.plan.plan_update` treats as a hard refusal
    rather than a warning.
    """

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def checksum(self) -> int:
        """Additive sum of the payload."""
        return additive_checksum(self.payload)

    @property
    def wire_bytes(self) -> bytes:
        """Exactly what the vendor puts on the wire for this image.

        Two rules, both confirmed against USB captures and both easy to get
        backwards:

        * A container image is sent **whole, header included**. The 16 bytes are
          flashed; they are not packaging to be stripped.
        * ``section0`` is sent with its **boot sector removed**. ``swdata`` is an
          image of flash from address ``0x0``, so its first 128 KiB *is* the
          bootloader. The vendor writes ``swdata[0x20000:]`` to scratch and has
          the device copy that over live firmware at ``0x20000``.

        Getting the second one wrong is the single most destructive mistake
        available here: writing the whole image to ``0x20000`` shifts every byte
        of firmware up by one sector and leaves an unbootable unit.
        """
        if self.container is not None:
            return self.container.raw
        if self.name == SECTION0 and len(self.payload) > BOOT_SECTOR_LEN:
            return self.payload[BOOT_SECTOR_LEN:]
        return self.payload


@dataclass(frozen=True, slots=True)
class FirmwareBundle:
    """Everything extracted from one updater EXE."""

    images: dict[str, FirmwareImage] = field(default_factory=dict)
    release: FirmwareRevision | None = None
    """Release date parsed from the filename, when one was supplied.

    Advisory only — nothing decides whether to flash based on it. The byte-level
    comparisons in :mod:`aiolumagen.firmware.plan` are authoritative; this is
    for telling the user which release they're holding.
    """

    source_name: str | None = None

    @property
    def section0(self) -> FirmwareImage | None:
        return self.images.get(SECTION0)

    @property
    def section1(self) -> FirmwareImage | None:
        return self.images.get(SECTION1)

    @property
    def chip_images(self) -> dict[str, FirmwareImage]:
        return {n: self.images[n] for n in CHIP_NAMES if n in self.images}


def _pe_header_offset(data: bytes) -> int:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise LumagenFirmwareImageError(
            "not a Windows executable (no 'MZ' signature). Supply the vendor's "
            "Radiance Pro updater .exe, not an extracted firmware image."
        )
    pe = _u32(data, 0x3C)
    if pe + 24 > len(data) or data[pe : pe + 4] != b"PE\x00\x00":
        raise LumagenFirmwareImageError(
            "file has an MZ stub but no PE header; it may be truncated or a 16-bit executable."
        )
    return pe


def pe_sections(data: bytes) -> list[PeSection]:
    """Parse the PE section table."""
    pe = _pe_header_offset(data)
    count = _u16(data, pe + 6)
    opt_size = _u16(data, pe + 20)
    base = pe + 24 + opt_size
    sections: list[PeSection] = []
    for index in range(count):
        entry = base + index * 40
        if entry + 40 > len(data):
            break
        sections.append(
            PeSection(
                name=data[entry : entry + 8].rstrip(b"\x00").decode("ascii", "replace"),
                virtual_size=_u32(data, entry + 8),
                virtual_address=_u32(data, entry + 12),
                raw_size=_u32(data, entry + 16),
                raw_address=_u32(data, entry + 20),
            )
        )
    return sections


def pe_image_base(data: bytes) -> int:
    """``ImageBase`` from the optional header."""
    return _u32(data, _pe_header_offset(data) + 24 + 28)


def _section_for(sections: list[PeSection], name: str) -> PeSection | None:
    return next((s for s in sections if s.name == name), None)


def va_to_offset(data: bytes, va: int) -> int | None:
    """Map a virtual address to a file offset, or ``None`` if unmapped."""
    base = pe_image_base(data)
    for section in pe_sections(data):
        start = base + section.virtual_address
        if start <= va < start + section.span:
            return section.raw_address + (va - start)
    return None


def rva_to_offset(data: bytes, rva: int) -> int | None:
    """Map a relative virtual address to a file offset, or ``None``."""
    for section in pe_sections(data):
        if section.virtual_address <= rva < section.virtual_address + section.span:
            return section.raw_address + (rva - section.virtual_address)
    return None


def _walk_resource_dir(
    data: bytes,
    rsrc_off: int,
    dir_off: int,
    path: tuple[int, ...],
    depth: int = 0,
) -> Iterator[tuple[tuple[int, ...], int, int]]:
    """Yield ``(path, data_rva, size)`` for every leaf in the resource tree.

    The tree is type → name/id → language, so `path` ends up three deep and
    ``path[0]`` is the resource type. `depth` is bounded purely as a guard
    against a malformed file looping this forever — a real tree is never deeper
    than three.
    """
    if depth > 3:
        return
    base = rsrc_off + dir_off
    if base + 16 > len(data):
        return
    named = _u16(data, base + 12)
    ids = _u16(data, base + 14)
    for index in range(named + ids):
        entry = base + 16 + index * 8
        if entry + 8 > len(data):
            return
        name_or_id = _u32(data, entry)
        offset = _u32(data, entry + 4)
        is_dir = bool(offset & 0x80000000)
        offset &= 0x7FFFFFFF
        entry_id = name_or_id & 0x7FFFFFFF
        if is_dir:
            yield from _walk_resource_dir(data, rsrc_off, offset, (*path, entry_id), depth + 1)
            continue
        leaf = rsrc_off + offset
        if leaf + 8 > len(data):
            continue
        yield (*path, entry_id), _u32(data, leaf), _u32(data, leaf + 4)


def extract_resources(data: bytes) -> dict[int, bytes]:
    """Return ``{resource_id: bytes}`` for every ``RT_RCDATA`` resource."""
    pe = _pe_header_offset(data)
    rsrc_rva = _u32(data, pe + 24 + 112)  # DataDirectory[2] — the resource table
    if not rsrc_rva:
        return {}
    rsrc_off = rva_to_offset(data, rsrc_rva)
    if rsrc_off is None:
        return {}

    found: dict[int, bytes] = {}
    for path, rva, size in _walk_resource_dir(data, rsrc_off, 0, ()):
        if not path or path[0] != RT_RCDATA or len(path) < 2:
            continue
        offset = rva_to_offset(data, rva)
        if offset is None or offset + size > len(data):
            continue
        found[path[1]] = data[offset : offset + size]
    return found


def find_swdata_descriptor(data: bytes) -> tuple[int, int] | None:
    """Recover ``(swdata_va, swdata_len)`` from the updater's own machine code.

    ``section0`` has no container and no resource entry, so its extent has to
    come from somewhere. The updater sets it up with two nearby immediate stores:

    .. code-block:: text

        mov dword ptr [reg + 0x210], <swdata VA>     C7 /r 10 02 00 00 <imm32>
        mov dword ptr [ebp - 0x158], <swdata size>   C7 /r <disp32>    <imm32>

    So: find the ``0x210`` displacement, confirm a ``C7`` opcode two bytes
    before it, take the following immediate as a candidate pointer and require it
    to land inside ``.data``, then look ahead a short window for the size store
    and sanity-check the length. Candidates that fail any test are skipped and
    the scan continues, because ``10 02 00 00`` occurs incidentally in a 5 MB
    binary.

    Scanning code to find data is unusual enough to justify itself: the
    alternative is the hardcoded length in :data:`SWDATA_LEN_FALLBACK`, which is
    already known to be wrong for at least one release. Returns ``None`` if the
    pattern isn't found, and the caller decides how loudly to complain.
    """
    sections = pe_sections(data)
    text = _section_for(sections, ".text")
    data_sec = _section_for(sections, ".data")
    if text is None or data_sec is None:
        return None

    code = data[text.raw_address : text.raw_address + text.raw_size]
    base = pe_image_base(data)
    data_lo = base + data_sec.virtual_address
    data_hi = data_lo + data_sec.span

    needle = b"\x10\x02\x00\x00"  # disp32 == 0x00000210
    pos = 0
    while True:
        idx = code.find(needle, pos)
        if idx < 0:
            return None
        pos = idx + 1
        if idx < 2 or code[idx - 2] != 0xC7:
            continue
        if idx + 8 > len(code):
            continue
        pointer = _u32(code, idx + 4)
        if not (data_lo <= pointer < data_hi):
            continue
        window = code[idx + 8 : idx + 8 + 32]
        for offset in range(max(0, len(window) - 9)):
            if window[offset] != 0xC7:
                continue
            size = _u32(window, offset + 6)
            if (
                _MIN_REASONABLE_SWDATA <= size <= _MAX_REASONABLE_SWDATA
                and pointer + size <= data_hi
            ):
                return pointer, size


def parse_release_name(name: str) -> FirmwareRevision | None:
    """Pull a ``MMDDYY`` release date out of a filename like ``radiance_pro030326``.

    Advisory: used for display, never to decide whether to flash. Returns
    ``None`` when the name carries no six-digit group or the group isn't a
    plausible date, which is why nothing important is allowed to depend on it.
    """
    for candidate in _RELEASE_RE.findall(name):
        revision = FirmwareRevision.parse(candidate)
        if revision is not None:
            return revision
    return None


def extract_images(data: bytes, *, source_name: str | None = None) -> FirmwareBundle:
    """Extract every firmware image from a vendor updater EXE.

    :param data: the whole ``.exe``.
    :param source_name: filename, used only to report the release version.
    :raises LumagenFirmwareImageError: if `data` isn't a PE, or if a container
        resource is present but corrupt. A *missing* image is not an error here —
        deciding whether the bundle is sufficient belongs to
        :func:`~aiolumagen.firmware.plan.plan_update`, which knows which images
        an update actually needs.
    """
    images: dict[str, FirmwareImage] = {}

    for res_id, blob in sorted(extract_resources(data).items()):
        name = RESOURCE_NAMES.get(res_id)
        if name is None:
            continue
        container = parse_container(blob, name=f"{name} (RT_RCDATA {res_id})")
        images[name] = FirmwareImage(
            name=name,
            payload=container.payload,
            source=f"RT_RCDATA {res_id}",
            container=container,
        )

    found = find_swdata_descriptor(data)
    recovered = found is not None
    va, length = found if found is not None else (SWDATA_VA_FALLBACK, SWDATA_LEN_FALLBACK)
    offset = va_to_offset(data, va)
    if offset is not None and offset + length <= len(data):
        how = "recovered from code" if recovered else "FALLBACK constants — unverified"
        images[SECTION0] = FirmwareImage(
            name=SECTION0,
            payload=data[offset : offset + length],
            source=f".data VA {va:#010x} ({how})",
            descriptor_recovered=recovered,
        )

    return FirmwareBundle(
        images=images,
        release=parse_release_name(source_name) if source_name else None,
        source_name=source_name,
    )
