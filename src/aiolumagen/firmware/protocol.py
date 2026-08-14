"""Updater-mode command formatting, reply parsing, flash map, and timings.

The Lumagen's firmware-update command set is a *different protocol* from the
normal-mode one in :mod:`aiolumagen.protocol`: entered with ``M0931``, framed
without terminators, built on four sticky registers, and capable of destroying
the unit. It gets its own module rather than being folded in.

Pure sync, no I/O. Everything here is a string builder or a bytes parser, which
is what lets the whole command sequence for an update be generated and asserted
against recorded vendor transcripts without a device present.

Three framing rules that are easy to get wrong and expensive to debug:

* **Commands have no terminator** and are 1-6 ASCII characters. The device
  identifies them by length and leading character.
* **``C``'s reply has no terminator either** — exactly 11 characters,
  ``CS=%8x``. Waiting for a line burns the whole timeout on a reply the device
  already sent.
* **``R``'s reply *is* CRLF-terminated**, and is ASCII hex, two characters per
  byte. Read to the delimiter. Taking only ``L`` *bytes* leaves ``L`` characters
  in the buffer and shifts every subsequent reply by one response — which
  produces a full run of plausible-looking wrong answers rather than an error.

And one that isn't about framing: the device **echoes commands** back as a prefix
on replies, so every parser here scans for its marker instead of assuming
offset 0. That's the same rule :mod:`aiolumagen.protocol` follows for ``!``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Flash map
# ---------------------------------------------------------------------------

SECTOR_SIZE: Final = 0x20000
"""128 KiB — the flash erase granularity, and therefore the repair unit.

NOR flash programming can only clear bits, so a wrong byte can't be patched in
place. Rewriting anything means erasing the whole sector containing it.
"""

BOOT_SECTOR_LEN: Final = SECTOR_SIZE
"""The bootloader occupies flash from ``0x0``. Never written on the normal path.

Equal to one sector, and that coincidence is load-bearing: ``section0`` is an
image of flash from address ``0x0``, so stripping its first *sector* is exactly
stripping its bootloader. See :attr:`~aiolumagen.firmware.extract.FirmwareImage.wire_bytes`.
"""

ADDR_LIVE: Final = 0x20000
"""Live ``section0`` firmware. Read freely; only ever written by the device's own copy."""

SCRATCH_ADDR: Final = 0xB00000
"""Staging area for ``section0``. Writing here is always safe and always reversible."""

SCRATCH_SECTOR: Final = SCRATCH_ADDR // SECTOR_SIZE  # 88 (0x58)

LIVE_PROBE_ADDRS: Final = (ADDR_LIVE, ADDR_LIVE + 0x30000, ADDR_LIVE + 0x60000)
"""Addresses used to qualify the link after a rate change.

Live firmware specifically: it's populated, stable, and nothing this subsystem
does can invalidate it as a reference. See
:meth:`~aiolumagen.firmware.session.FirmwareSession.rate_check` for why
checking the *target* region instead would be circular.
"""

CHIP_REGIONS: Final[dict[str, int]] = {
    "hdmi_rx": 0x7C0000,
    "hdmi_tx": 0x820000,
    "hdmi_ntx": 0x880000,
}
"""Where the SiI9777 images live. Present for reads; there is no write path."""


@dataclass(frozen=True, slots=True)
class Section1Slot:
    """One of the two A/B slots section 1 is written into."""

    code: str
    """The ``Z35`` reply naming this slot."""

    first_sector: int
    address: int


SECTION1_SLOTS: Final[dict[str, Section1Slot]] = {
    "00": Section1Slot(code="00", first_sector=0x08, address=0x100000),
    "99": Section1Slot(code="99", first_sector=0x60, address=0xC00000),
}
"""Section-1 slots by ``Z35`` code.

``Z35`` names the slot the device is **not** currently running from, which is the
only safety mechanism section 1 has — there is no scratch area and no staging
step, the write goes straight into a live slot.

Note what the A/B pair does *not* buy you. Slot election at boot reads the
container magic and generation tag and **nothing else**, so a half-written slot
carrying a valid header wins the election and yields no video. The redundancy
protects against a *failed* write only if the header is withheld until the
payload is verified — which is what
:meth:`~aiolumagen.firmware.session.FirmwareSession.stage_image`'s
``header_last`` does, and why it defaults on.
"""


def other_slot(code: str) -> Section1Slot | None:
    """The slot `code` doesn't name — i.e. the one currently running.

    Used to cross-check ``Z35`` against the generation tags before erasing
    anything.

    ``None`` for a `code` that names no known slot, rather than "whichever slot
    isn't this string". An unrecognised ``Z35`` reply means we don't know the flash
    layout, and confidently returning a slot address in that state is how you erase
    the wrong half of flash.
    """
    if code not in SECTION1_SLOTS:
        return None
    return next((slot for slot in SECTION1_SLOTS.values() if slot.code != code), None)


# ---------------------------------------------------------------------------
# Transfer geometry
# ---------------------------------------------------------------------------

BLOCK_SIZE: Final = 0x1000
"""4096 — the write block in normal (non-bootloader) updater mode."""

BLOCK_SIZE_BOOTLOADER: Final = 0x800
"""2048, for the bootloader-mode path this subsystem deliberately refuses."""

AUDIT_CHUNK_BLOCKS: Final = SECTOR_SIZE // BLOCK_SIZE  # 32
"""Blocks per coarse audit chunk — deliberately exactly one erase sector.

The device will checksum any ``[addr, len)``, so a disagreement can be *located*
rather than guessed at: sum coarse chunks first, then subdivide only the ones
that disagree. That is ~25 checksums to survey a 3 MB region plus ~32 per bad
chunk, against 772 if every block were checked individually.

One sector per chunk is what makes the arithmetic line up: a failing chunk maps
to exactly the one sector a repair would have to erase, because the sector is
the smallest thing NOR flash can rewrite.
"""

DEFAULT_CHUNK: Final = BLOCK_SIZE
MAX_SAFE_CHUNK: Final = 0x1000
"""Hard ceiling on bytes per host write. Do not raise.

One whole block per write is what the vendor does and what the USB captures
show — 4096-byte bulk-OUT URBs, never split. Matching it collapses 64
noise-encrypted API frames per block into one, which matters because ESPHome's
API ingests at most ``MAX_MESSAGES_PER_LOOP = 10`` messages per main-loop
iteration; 64 small frames couple the data path to main-loop scheduling and one
large frame doesn't.

The ceiling is the real constraint. ESPHome's output pool is ``buffer_size``
bytes (8192 in ``esphome-lumagen``) carved into 64-byte chunks, and
``write_array()`` **discards** whatever doesn't fit while logging only locally:
``usb_uart.cpp:158  "Output pool full - lost %zu bytes"``. At 4096 half the pool
is headroom; at 8192 the pool would have to be completely empty every time.
"""

SUPPORTED_BAUDS: Final = (9600, 57600, 115200, 230400)
"""Rates the device accepts via ``B``. 230400 is the vendor's own "fast" setting.

115200 is *not* recommended despite being on this list: it was called qualified
off a single clean run and later failed roughly one attempt in four. 230400 with
the flush barrier in place has proven more reliable than 115200 without it.
"""

SESSION_BAUD: Final = 9600
"""The rate the device always listens on at power-up, and the rate to hand back at."""

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

BLOCK_DELAY: Final = 0.150
"""Dead time after each block while the device programs it into flash.

A *device* requirement, and the only one left once the flush barrier handles
delivery: the barrier guarantees bytes reached the FT232R, but nothing can make
the Lumagen read its UART while it is writing flash. Anything arriving in that
window is lost with no error anywhere.

150 ms comes from the vendor's own margin rather than guesswork. Pairing bulk-OUT
URBs by ``irpId`` puts the vendor's deliberate post-write silence at ~122 ms,
with its next burst ~208 ms after the last payload byte. Adding our ~86 ms of
command framing puts our next burst 236 ms out — 13% more headroom than the
vendor takes. Verified at 230400 across both a 113-block and a 772-block
transfer, byte-perfect by the device's checksum *and* a per-block audit.

Do not read the often-quoted "247-257 ms" as a device requirement; that figure is
cadence minus wire time and double-counts the command framing.
"""

POST_ERASE_DELAY: Final = 5.0
"""Settle after an erase run. The vendor observes it; flash needs it."""

ERASE_TOKEN_TIMEOUT: Final = 30.0
"""Ceiling on a whole erase run. Each sector takes roughly 340 ms."""

CHECKPOINT_SETTLE: Final = 2.0
CHECKPOINT_TRIES: Final = 4
"""Settle and retry budget for the first command after a bulk transfer.

:data:`BLOCK_DELAY` covers block-to-block writes but *not* the transition from
bulk data back to a command. A 230400 section-1 run delivered all 771 payload
blocks with more slack than the vendor uses, then failed to answer the very first
checkpoint read — the device was busy, not broken. Retrying costs nothing at the
point this is used, because the commit header is still unwritten.
"""

FLUSH_TIMEOUT: Final = 60.0
"""Ceiling on a single flush barrier before calling it a failure.

Generous on purpose: at 9600 one 4096-byte block is 4.3 s of wire time and the
barrier legitimately blocks for all of it.
"""

FLUSH_RETRY_DELAY: Final = 0.05
"""Pause between flush re-issues.

The ESP's own ``flush_timeout`` (100 ms by default, 1 s in ``esphome-lumagen``)
is shorter than a block's wire time at every rate worth using, so it answers
``TIMEOUT`` while still draining. Re-issuing is the correct response, and is what
makes the barrier work regardless of how the channel is configured.

That budget must not be raised past ~5 s on the ESP side:
``USBUartChannel::flush()`` spins on ESPHome's ``yield()``, a bare
``vPortYield()`` that does not feed the task watchdog, while ``esp32/hal.cpp``
subscribes the loop task to it with ``TIMEOUT_S=5, PANIC=y``. A longer flush
panic-reboots the bridge mid-block.
"""

PROMOTE_ACK_TIMEOUT: Final = 90.0
"""How long to wait for ``G39``'s ``Ok``. Observed ~6.6 s for a 458 KB copy."""

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

CMD_ENTER_UPDATE: Final = "M0931"
CMD_PING: Final = "e"
CMD_IDENTIFY: Final = "I"
CMD_BOOTLOADER_PROBE: Final = "H0"
CMD_BOOTLOADER_VERSION: Final = "H1"
CMD_SCRATCH_PROBE: Final = "Z6a"
CMD_SCRATCH_ACK: Final = "K"
CMD_FLASH_LAYOUT: Final = "Z35"
CMD_READ: Final = "R"
CMD_CHECKSUM: Final = "C"
CMD_BEGIN_BLOCK: Final = "D"
CMD_COPY: Final = "G39"
CMD_FINISH: Final = "Z97"
"""Leave updater mode by finishing. **Powers the unit down.**

Correct after a promotion — the power cycle is how newly promoted firmware gets
loaded — and merely inconvenient otherwise. Use :data:`CMD_ABORT` for read-only
and aborted runs.
"""

CMD_ABORT: Final = "X"
"""Leave updater mode by aborting. Returns to normal operation, power still on."""

CMD_POWER_QUERY: Final = "ZQS02"
"""Normal-mode power query, issued *before* ``M0931``.

Worth doing first: in standby the Lumagen doesn't service the updater command
set at all, so ``M0931`` and everything after it time out with no indication of
why.
"""

REPLY_OK: Final = "Ok"
REPLY_ERASE_DONE: Final = "OK"
"""Case matters. Pings answer ``Ok``; an erase run finishes with ``OK``."""

ERASE_SECTOR_TOKEN: Final = "x"
CHECKSUM_PREFIX: Final = "CS="
CHECKSUM_REPLY_LEN: Final = len(CHECKSUM_PREFIX) + 8


def set_address(addr: int) -> str:
    """``A%06X`` — the destination address register (sticky)."""
    return f"A{addr:06X}"


def set_source_address(addr: int) -> str:
    """``a%06X`` — the *source* address register for a copy (sticky).

    Lowercase, and distinct from :func:`set_address`. It appears exactly once in
    an entire update, in the promotion sequence, which makes it a useful sanity
    check that a generated command stream is right.
    """
    return f"a{addr:06X}"


def set_length(value: int) -> str:
    """``L%06X`` — byte length, or sector *count* for an erase run (sticky)."""
    return f"L{value:06X}"


def set_baud(rate: int) -> str:
    """``B%06d`` — decimal, unlike every other numeric argument."""
    return f"B{rate:06d}"


def erase_run(sector: int) -> str:
    """``S79`` + sector + its one's complement — erase ``L`` sectors from `sector`.

    Case is significant and asymmetric: the sector is uppercase hex, the
    complement lowercase. Verified against captures — sector ``0x58`` is
    ``S7958a7`` and sector ``0x08`` is ``S7908f7``.
    """
    return f"S79{sector:02X}{(~sector) & 0xFF:02x}"


def erase_sector(sector: int) -> str:
    """``S37`` + sector + complement — erase exactly one sector."""
    return f"S37{sector:02X}{(~sector) & 0xFF:02x}"


def pad_to_even_end(start: int, data: bytes) -> bytes:
    """Append one ``0x00`` when ``start + len(data)`` is odd, as the vendor does.

    ``download_section0`` rounds its end address up to even, so an odd-length
    region gets exactly one extra byte. Confirmed by capture: all three chip
    containers (286,737 bytes, odd end) went out as 286,738 with a trailing null.
    """
    if (start + len(data)) & 1:
        return data + b"\x00"
    return data


def sectors_for(length: int) -> int:
    """Sectors needed to hold `length` bytes, rounded up."""
    return -(-length // SECTOR_SIZE)


def blocks_for(length: int) -> int:
    """Blocks needed to transfer `length` bytes, rounded up."""
    return -(-length // BLOCK_SIZE)


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------

DEVICE_IDS: Final[dict[int, str]] = {
    0x16: "RadiancePro",
    0x15: "Radiance21XX",
    0x14: "Radiance20XX",
    0x13: "RadianceMini",
    0x12: "RadianceXS",
    0x11: "RadianceXE",
    0x10: "RadianceXD",
}

RADIANCE_PRO_ID: Final = 0x16


@dataclass(frozen=True, order=True, slots=True)
class FirmwareRevision:
    """A Lumagen firmware revision, ordered chronologically.

    The device reports its revision as six digits in ``MMDDYY`` order, and **this
    must never be compared as an integer**. ``030225`` (2 March 2025) is
    numerically *smaller* than ``101524`` (15 October 2024) but chronologically
    later, so a naive ``<`` inverts the comparison for eight months of every year.

    The field order below is the fix, and it's deliberately structural rather than
    a hand-written comparator: ``order=True`` on ``(year, month, day)`` makes the
    dataclass generate chronological comparisons, so there is no ``__lt__`` for a
    later edit to get subtly wrong.
    """

    year: int
    month: int
    day: int

    @classmethod
    def parse(cls, text: str) -> FirmwareRevision | None:
        """Parse six ``MMDDYY`` digits, or ``None`` if that isn't what this is.

        Non-raising: callers feed it filenames and device replies, where "not a
        revision" is an ordinary outcome rather than an error.
        """
        digits = text.strip()
        if len(digits) != 6 or not digits.isdigit():
            return None
        month, day, year = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return cls(year=2000 + year, month=month, day=day)

    @classmethod
    def from_identify(cls, value: int) -> FirmwareRevision | None:
        """Parse the revision field of an ``I`` reply.

        That field is hex-encoded but reads as decimal: ``0x030326`` formats to
        ``030326``, i.e. 3 March 2026. Formatting it back to hex digits before
        parsing is the whole trick.
        """
        return cls.parse(f"{value:06X}")

    @property
    def mmddyy(self) -> str:
        """The device's own six-digit spelling."""
        return f"{self.month:02d}{self.day:02d}{self.year % 100:02d}"

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """A parsed ``I`` reply."""

    revision_raw: int
    device_id: int
    serial: int
    revision: FirmwareRevision | None

    @property
    def model(self) -> str:
        return DEVICE_IDS.get(self.device_id, f"Unknown({self.device_id:#04x})")

    @property
    def is_radiance_pro(self) -> bool:
        return self.device_id == RADIANCE_PRO_ID

    def __str__(self) -> str:
        revision = str(self.revision) if self.revision else f"{self.revision_raw:06X}"
        return f"{self.model} rev {revision} serial {self.serial}"


def parse_identity(text: str) -> DeviceIdentity | None:
    """Parse ``<rev6>.<id2>.<serial4>``, all hex, or ``None``.

    Tolerates a command echo and surrounding whitespace by locating the
    dot-separated triple rather than trusting position.
    """
    for candidate in text.replace("\r", " ").replace("\n", " ").split():
        parts = candidate.strip().split(".")
        if len(parts) < 3:
            continue
        try:
            revision_raw = int(parts[0], 16)
            device_id = int(parts[1], 16)
            serial = int(parts[2], 16)
        except ValueError:
            continue
        return DeviceIdentity(
            revision_raw=revision_raw,
            device_id=device_id,
            serial=serial,
            revision=FirmwareRevision.from_identify(revision_raw),
        )
    return None


def parse_checksum(text: str) -> int | None:
    """Parse a ``CS=%8x`` reply, or ``None`` if it isn't there.

    ``None`` means *no answer*, which is a genuinely different outcome from a
    wrong answer: mid-update it usually means the device is still busy
    programming, and the correct response is to wait and retry rather than to
    declare a mismatch. Callers must not collapse the two.
    """
    if CHECKSUM_PREFIX not in text:
        return None
    digits = text.split(CHECKSUM_PREFIX, 1)[1][:8].strip()
    try:
        return int(digits, 16)
    except ValueError:
        return None


def decode_hex_reply(line: bytes) -> bytes | None:
    """Decode an ``R`` reply — ASCII hex, two characters per byte — or ``None``.

    Returns ``None`` rather than raising when the line isn't valid hex, so a
    caller can report the raw text. Note the decoded result is *binary*: don't
    strip it, and don't hex-decode it twice. Both mistakes turned a successful
    verify into a false failure during bring-up, because ``.strip()`` on binary
    silently eats legitimate ``0x20``/``0x09``/``0x0a``/``0x0d`` bytes.
    """
    try:
        return bytes.fromhex(line.decode("ascii", "replace").strip())
    except ValueError:
        return None


def parse_power_state(text: str) -> bool | None:
    """Parse a normal-mode ``ZQS02`` reply into on/standby, or ``None``.

    ``None`` means the main firmware didn't answer at all — the unit is already
    in updater or bootloader mode, which is information the caller needs and
    must not conflate with standby.
    """
    marker = "!S02,"
    index = text.find(marker)
    if index < 0:
        return None
    payload = text[index + len(marker) :].strip()
    if not payload:
        return None
    return payload[0] == "1"
