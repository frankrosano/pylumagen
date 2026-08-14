"""The ``0xBABABEBE`` firmware container.

Every firmware image the vendor ships except ``section0`` is wrapped in a
16-byte header:

.. code-block:: text

    offset  size  field
      0      4    magic     = 0xBABABEBE  (little-endian on disk: be be ba ba)
      4      4    tag       = 0xFFFFFFFF as shipped; STAMPED BY THE DEVICE
      8      4    checksum  = additive 32-bit sum of the payload
     12      4    size      = payload length
     16    size   payload

Two properties of this format drive most of the code in this subsystem.

**The checksum is a plain additive byte sum, not a CRC.** That looks like a
weakness and is actually the thing that makes safe flashing possible: it's the
same arithmetic the device's ``C`` command performs over a flash range, so the
host can compute what the device *should* answer for a region and compare. Every
verification path in :mod:`aiolumagen.firmware` is built on that equality.

**The header is flashed, not stripped.** It is not EXE packaging — the container
goes onto the wire and into flash intact, and the device reads it back at boot.
For section 1 the header is load-bearing: the bootloader elects which of the two
A/B slots to run by reading the magic and the generation tag, and *nothing else*.
A slot whose payload is garbage but whose header is intact wins the election.
That is why :func:`~aiolumagen.firmware.session.FirmwareSession.stage_image`
can defer the header to the end of the transfer and treat it as a commit record.

Pure sync, no I/O — feed it bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from aiolumagen.exceptions import LumagenFirmwareImageError

MAGIC: Final = 0xBABABEBE
"""Container magic, as an integer. On disk it's little-endian: ``be be ba ba``."""

MAGIC_BYTES: Final = b"\xbe\xbe\xba\xba"
"""Container magic in wire order, for scanning and for comparing a device read."""

HEADER_LEN: Final = 16

TAG_UNWRITTEN: Final = 0xFFFFFFFF
"""What sits at ``+4`` in a shipped image, and in an erased (all-``0xFF``) slot.

The double duty is convenient rather than confusing: a slot reading
``0xFFFFFFFF`` here is uncommitted either way, and that's the only question
anyone asks of this field.
"""

GENERATION_PREFIX: Final = 0xFADE
"""Top 16 bits of a committed tag. The bottom 16 are a monotonic generation."""

MASK32: Final = 0xFFFFFFFF


def additive_checksum(data: bytes) -> int:
    """Sum every byte, truncated to 32 bits.

    The device's ``C`` command computes exactly this over a flash range, which is
    what makes host-side verification possible. Deliberately named for what it
    does — calling it a CRC (as an early draft of the research notes did) invites
    someone to "fix" it with a real CRC and break every comparison at once.
    """
    return sum(data) & MASK32


@dataclass(frozen=True, slots=True)
class ContainerHeader:
    """A decoded 16-byte container header."""

    magic: int
    tag: int
    checksum: int
    size: int

    @property
    def has_magic(self) -> bool:
        return self.magic == MAGIC

    @property
    def committed(self) -> bool:
        """True when the device has stamped this slot — i.e. it can win a boot.

        Requires the magic *and* a written tag. An erased slot has neither, and a
        slot mid-write under the header-last scheme has payload but neither.
        """
        return self.has_magic and self.tag != TAG_UNWRITTEN

    @property
    def generation(self) -> int | None:
        """The monotonic generation from a ``0xFADExxxx`` tag, or ``None``.

        ``None`` means "this slot has no generation to compare" — unwritten,
        or carrying a tag in some shape this code doesn't recognise. Callers must
        treat that as *unknown* rather than as zero: ordering a real generation
        against a fabricated 0 is how you conclude the running slot is the older
        one and overwrite live firmware.
        """
        if (self.tag >> 16) != GENERATION_PREFIX:
            return None
        return self.tag & 0xFFFF

    @classmethod
    def unpack(cls, raw: bytes) -> ContainerHeader | None:
        """Decode 16 bytes, or ``None`` if there aren't 16 bytes to decode.

        Non-raising because the usual caller is a device read that may have come
        back short, and "the device didn't answer properly" is a different
        problem from "these bytes aren't a container" — the session needs to tell
        those apart to know whether retrying is worthwhile.
        """
        if len(raw) < HEADER_LEN:
            return None
        magic, tag, checksum, size = struct.unpack_from("<IIII", raw, 0)
        return cls(magic=magic, tag=tag, checksum=checksum, size=size)


@dataclass(frozen=True, slots=True)
class Container:
    """A shipped container: validated header, payload, and the raw bytes."""

    header: ContainerHeader
    payload: bytes
    raw: bytes
    """Header + payload. This is what goes on the wire — see the module docstring."""

    @property
    def checksum_ok(self) -> bool:
        return additive_checksum(self.payload) == self.header.checksum


def parse_container(blob: bytes, *, name: str = "image") -> Container:
    """Validate and unwrap a shipped container.

    Strict, because this only ever runs against a file the user supplied and a
    bad file is the one failure mode that is completely free to catch. Requires
    the magic, an unstamped ``tag``, a ``size`` that fits, and a payload whose
    additive sum matches the recorded checksum.

    The unstamped-``tag`` requirement is what distinguishes a *shipped* container
    from one read back off a device: the device stamps that field on commit, so
    insisting on ``0xFFFFFFFF`` here would reject perfectly good flash contents.
    Use :meth:`ContainerHeader.unpack` for device reads.

    :raises LumagenFirmwareImageError: on any of the above.
    """
    header = ContainerHeader.unpack(blob)
    if header is None:
        raise LumagenFirmwareImageError(
            f"{name}: too short to be a firmware container "
            f"({len(blob)} bytes, need at least {HEADER_LEN})"
        )
    if not header.has_magic:
        raise LumagenFirmwareImageError(
            f"{name}: bad container magic {header.magic:#010x}, expected {MAGIC:#010x}"
        )
    if header.tag != TAG_UNWRITTEN:
        raise LumagenFirmwareImageError(
            f"{name}: container tag is {header.tag:#010x}, expected "
            f"{TAG_UNWRITTEN:#010x}. A shipped image has this field unwritten; a "
            "stamped value means these bytes were read back off a device rather "
            "than extracted from an updater."
        )
    available = len(blob) - HEADER_LEN
    if header.size > available:
        raise LumagenFirmwareImageError(
            f"{name}: container declares {header.size} payload bytes but only "
            f"{available} are present"
        )
    payload = blob[HEADER_LEN : HEADER_LEN + header.size]
    container = Container(
        header=header,
        payload=payload,
        raw=blob[: HEADER_LEN + header.size],
    )
    if not container.checksum_ok:
        raise LumagenFirmwareImageError(
            f"{name}: payload checksum mismatch — container records "
            f"{header.checksum:#010x}, payload sums to "
            f"{additive_checksum(payload):#010x}. Refusing to treat this as "
            "flashable; the file is corrupt or was truncated in transit."
        )
    return container


def find_containers(data: bytes) -> list[tuple[int, Container]]:
    """Scan `data` for every valid shipped container, returning ``(offset, ...)``.

    A brute-force fallback for when the structured route through
    :mod:`aiolumagen.firmware.extract` can't identify something — it finds
    containers wherever they sit, without needing the PE resource tree to be
    intact. Only containers whose checksum actually validates are returned, which
    makes a false positive on the 4-byte magic essentially impossible.

    It cannot find ``section0``, which has no container at all. That's not a
    limitation to work around, it's the reason
    :func:`~aiolumagen.firmware.extract.extract_images` exists.
    """
    found: list[tuple[int, Container]] = []
    start = 0
    while True:
        idx = data.find(MAGIC_BYTES, start)
        if idx < 0:
            return found
        start = idx + 1
        try:
            found.append((idx, parse_container(data[idx:], name=f"offset {idx:#x}")))
        except LumagenFirmwareImageError:
            continue


def expected_stored_checksum(wire_image: bytes, tag: int) -> int:
    """What the device's ``C`` should answer for a byte-perfect committed slot.

    The correction that makes section-1 comparison work at all. The device
    overwrites the four bytes at ``+4`` when it commits a slot, so a slot holding
    *exactly* `wire_image` can never sum to `wire_image`'s own additive
    checksum — comparing against the raw sum reports a mismatch on byte-perfect
    firmware, and any "is an update needed?" test built on it answers "yes,
    always".

    Verified against real slots: raw sum - 1020 (four ``0xFF`` bytes as shipped)
    + 500 (a stamped ``0xFADE001C``) is what the device reports.

    :param wire_image: the full container as flashed, header included.
    :param tag: the tag the device stamped, from :meth:`ContainerHeader.unpack`.
    """
    return (
        additive_checksum(wire_image)
        - additive_checksum(wire_image[4:8])
        + additive_checksum(tag.to_bytes(4, "little"))
    ) & MASK32
