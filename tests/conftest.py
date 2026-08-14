"""Shared pytest fixtures for aiolumagen tests."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from aiolumagen.firmware.container import MAGIC, additive_checksum

S01_RESPONSE = b"!S01,FakeModel,000000,0000,000000\r\n"
"""Device-info reply: model name, software rev, model number, serial."""

I25_RESPONSE = b"!I25,0,000,0000,0,0,000,000\r\n"
"""Deliberately minimal Full v5 status reply — same zeroed shape the protocol
tests use for a neutral status line.

The fake has to answer ``ZQI25`` at all because Full v5 is the library's
supported floor: a transport that stays silent is simulating unsupported
firmware, and the startup handshake would (correctly) wait out its status
window in every test that calls ``start()``.

It answers *minimally* on purpose. The trailing v5 fields — power at index 24,
input memory at 23 — are omitted rather than set, because these are
client-orchestration tests: several of them establish their own power state
via ``feed()`` and assert on the transitions. A fixture that declared the
device powered on would silently invalidate those premises (the protocol
layer dedupes no-op updates, so a later ``!S02,1`` would stop firing a
callback at all). Rich, realistic payloads belong in ``test_protocol.py``,
which feeds recorded lines; here the only requirement is a non-empty status
payload so the handshake knows v5 answered.
"""


class FakeTransport:
    """In-memory transport stub that implements the duck-typed client contract.

    Tests push inbound bytes with :meth:`feed` and inspect outbound writes
    via :attr:`sent`. No asyncio scheduling involved — everything resolves
    synchronously inside the ``async`` wrapper.
    """

    def __init__(self) -> None:
        self._on_data: Callable[[bytes], None] | None = None
        self.sent: list[bytes] = []
        self._connected = False
        self.connect_hook: Callable[[], Any] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None:
        self._on_data = callback

    async def connect(self) -> None:
        self._connected = True
        if self.connect_hook is not None:
            result = self.connect_hook()
            if asyncio.iscoroutine(result):
                await result

    async def disconnect(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.sent.append(data)
        if self._on_data is None:
            return
        # Auto-respond to the two handshake queries the client waits on, so
        # startup completes immediately instead of burning its retry and
        # status windows in every test.
        if data == b"ZQS01":
            self._on_data(S01_RESPONSE)
        elif data == b"ZQI25":
            self._on_data(I25_RESPONSE)

    def feed(self, data: bytes | str) -> None:
        """Simulate inbound bytes from the Lumagen."""
        if isinstance(data, str):
            data = data.encode("ascii")
        if self._on_data is not None and data:
            self._on_data(data)


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


# ---------------------------------------------------------------------------
# Firmware-update fixtures
# ---------------------------------------------------------------------------
#
# The vendor's updater EXEs are Lumagen, Inc.'s copyrighted binaries and must
# never be committed to this public repo, so the extractor is tested against a
# synthetic PE built here instead. That is a real constraint but also a better
# test: a hand-built executable can be made *deliberately* awkward (an odd-length
# image, a decoy occurrence of the descriptor needle) in ways a real file can't be
# asked to be on demand.
#
# The extractor is separately cross-checked against five real releases outside
# the test suite; those runs confirmed byte-identical output to the
# hardware-proven reference implementation. Five is what happened to be on hand,
# not the extent of Lumagen's catalogue — so treat it as a spot check, not a
# guarantee the extractor generalises to every release.

PE_OFFSET = 0x80
OPTIONAL_HEADER_SIZE = 224  # PE32 with the full 16-entry data directory
IMAGE_BASE = 0x400000
TEXT_RVA = 0x1000
RSRC_RVA = 0x10000
DATA_RVA = 0x20000
SWDATA_VA = IMAGE_BASE + DATA_RVA


def build_container(payload: bytes, *, tag: int = 0xFFFFFFFF) -> bytes:
    """Wrap `payload` in a 0xBABABEBE container."""
    header = struct.pack("<IIII", MAGIC, tag, additive_checksum(payload), len(payload))
    return header + payload


def _resource_blob(resources: dict[int, bytes]) -> tuple[bytes, int]:
    """Build a three-level RT_RCDATA resource tree.

    Returns the ``.rsrc`` bytes and the tree's size, laid out as Windows does it:
    type directory, then one name directory per resource id, then one language
    directory each, then the data-entry descriptors, then the payloads.
    """
    ids = sorted(resources)
    header = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1)  # type dir: 1 id entry (RT_RCDATA)
    type_dir_size = len(header) + 8
    name_dir_off = type_dir_size
    name_dir_size = 16 + 8 * len(ids)
    lang_dirs_off = name_dir_off + name_dir_size
    lang_dir_size = 16 + 8
    entries_off = lang_dirs_off + lang_dir_size * len(ids)
    payloads_off = entries_off + 16 * len(ids)

    out = bytearray()
    out += header
    out += struct.pack("<II", 10, name_dir_off | 0x80000000)  # RT_RCDATA -> name dir

    out += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(ids))
    for index, res_id in enumerate(ids):
        out += struct.pack("<II", res_id, (lang_dirs_off + index * lang_dir_size) | 0x80000000)

    for index, _ in enumerate(ids):
        out += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1)
        out += struct.pack("<II", 1033, entries_off + index * 16)  # language -> data entry

    cursor = payloads_off
    for res_id in ids:
        blob = resources[res_id]
        out += struct.pack("<IIII", RSRC_RVA + cursor, len(blob), 0, 0)
        cursor += len(blob)

    for res_id in ids:
        out += resources[res_id]
    return bytes(out), len(out)


def _swdata_descriptor(swdata_va: int, swdata_len: int, *, decoy: bool = False) -> bytes:
    """Machine code the descriptor scan is meant to find.

    Reproduces the two immediate stores the real updater emits::

        mov dword ptr [reg + 0x210], <swdata VA>     C7 /r 10 02 00 00 <imm32>
        mov dword ptr [ebp - 0x158], <swdata size>   C7 /r <disp32>    <imm32>

    With `decoy`, a bare ``10 02 00 00`` with no ``C7`` before it and an
    out-of-range pointer is planted first. A 5 MB binary contains that byte
    sequence incidentally, so the scan has to walk past non-matches rather than
    give up or accept the first hit.
    """
    code = bytearray(b"\x90" * 16)
    if decoy:
        code += b"\x8b\x45\x10\x02\x00\x00" + struct.pack("<I", 0xDEADBEEF)
        code += b"\x90" * 8
    code += b"\xc7\x45" + b"\x10\x02\x00\x00" + struct.pack("<I", swdata_va)
    code += b"\xc7\x85\xa8\xfe\xff\xff" + struct.pack("<I", swdata_len)
    code += b"\x90" * 16
    return bytes(code)


def build_updater_exe(
    *,
    swdata: bytes | None = None,
    resources: dict[int, bytes] | None = None,
    with_descriptor: bool = True,
    decoy: bool = False,
) -> bytes:
    """Build a synthetic Lumagen updater EXE.

    :param swdata: the section-0 array. Defaults to a recognisable pattern longer
        than one boot sector, so the boot-sector strip is observable.
    :param resources: ``{RT_RCDATA id: bytes}``. Defaults to a section-1 container
        under id 131.
    :param with_descriptor: when False, omit the descriptor stores so the
        extractor is forced onto its fallback constants.
    :param decoy: plant a false descriptor match ahead of the real one.
    """
    if swdata is None:
        swdata = bytes(range(256)) * (0x20000 // 256) + b"\xa5" * 0x800
    if resources is None:
        resources = {131: build_container(b"section-one-payload" * 64)}

    if with_descriptor:
        text = _swdata_descriptor(SWDATA_VA, len(swdata), decoy=decoy)
    else:
        text = b"\x90" * 64
    rsrc, rsrc_size = _resource_blob(resources)

    headers_size = 0x200
    text_off = headers_size
    rsrc_off = text_off + len(text)
    data_off = rsrc_off + len(rsrc)

    sections = [
        (b".text", TEXT_RVA, len(text), text_off, text),
        (b".rsrc", RSRC_RVA, rsrc_size, rsrc_off, rsrc),
        (b".data", DATA_RVA, len(swdata), data_off, swdata),
    ]

    out = bytearray(headers_size)
    out[0:2] = b"MZ"
    struct.pack_into("<I", out, 0x3C, PE_OFFSET)
    out[PE_OFFSET : PE_OFFSET + 4] = b"PE\x00\x00"
    struct.pack_into("<HH", out, PE_OFFSET + 4, 0x014C, len(sections))
    struct.pack_into("<H", out, PE_OFFSET + 20, OPTIONAL_HEADER_SIZE)

    opt = PE_OFFSET + 24
    struct.pack_into("<H", out, opt, 0x010B)
    struct.pack_into("<I", out, opt + 28, IMAGE_BASE)
    struct.pack_into("<I", out, opt + 92, 16)
    struct.pack_into("<II", out, opt + 112, RSRC_RVA, rsrc_size)  # DataDirectory[2]

    table = opt + OPTIONAL_HEADER_SIZE
    for index, (name, rva, size, raw_off, _) in enumerate(sections):
        entry = table + index * 40
        out[entry : entry + 8] = name.ljust(8, b"\x00")
        struct.pack_into("<IIII", out, entry + 8, size, rva, size, raw_off)

    for _, _, _, _, blob in sections:
        out += blob
    return bytes(out)


@pytest.fixture
def updater_exe() -> Callable[..., bytes]:
    """Factory for synthetic updater EXEs. See :func:`build_updater_exe`."""
    return build_updater_exe


FLASH_SIZE = 0x1000000
SECTOR = 0x20000


class FakeFirmwareTransport:
    """Transport stub that models the Lumagen's updater-mode protocol.

    Enough of a device to drive a whole update against: sticky ``A``/``a``/``L``
    registers, a byte-counting ``D`` that writes payload into a flash array,
    checksums computed the way the real device computes them, and erases that
    actually clear sectors.

    The byte-counting matters. The protocol has no framing and the device has no
    inter-byte timeout — it counts payload bytes and waits indefinitely — so a
    fake that parsed commands out of payload would hide exactly the desync class
    of bug this models. Commands are recognised by their leading character and
    fixed length, as the device does, and payload is consumed opaquely.
    """

    _LENGTHS: ClassVar[dict[str, int]] = {
        "e": 1, "I": 1, "R": 1, "C": 1, "D": 1, "K": 1, "X": 1,
        "H": 2,
        "G": 3,
        "M": 5,
        "A": 7, "a": 7, "L": 7, "B": 7, "S": 7,
    }  # fmt: skip

    def __init__(
        self,
        *,
        flush_mode: str = "status",
        flush_statuses: list[str] | None = None,
        power: bool | None = True,
        identity: str = "030326.16.04D2",
        layout: str = "00",
        bootloader_mode: bool = False,
        scratch_ok: bool = True,
        ping_answers: bool = True,
    ) -> None:
        self.memory = bytearray(b"\xff" * FLASH_SIZE)
        self.flush_mode = flush_mode
        self.flush_statuses = list(flush_statuses or [])
        self.power = power
        self.identity = identity
        self.layout = layout
        self.bootloader_mode = bootloader_mode
        self.scratch_ok = scratch_ok
        self.ping_answers = ping_answers
        self.baudrate = 9600

        self.commands: list[str] = []
        self.events: list[str] = []
        """Commands and rate changes on one timeline, so ordering is assertable.

        Needed because a command's correctness can depend on *when* the line rate
        changed relative to it — sending a command after switching our own side but
        before the device knows means the device never receives it.
        """

        self.writes: list[bytes] = []
        self.payload_writes: list[tuple[int, int]] = []
        self.flush_calls: list[int] = []
        self.baud_commands: list[int] = []
        self.baud_reconfigures: list[int] = []
        self.erases: list[tuple[int, int]] = []
        self.copies: list[tuple[int, int, int]] = []
        self.left: str | None = None

        self.addr = 0
        self.source = 0
        self.length = 0
        self._expect = 0
        self._write_addr = 0
        self._inbox = bytearray()
        self._on_data: Callable[[bytes], None] | None = None
        self._connected = False

    # -- transport surface -------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None:
        self._on_data = callback

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        self._inbox += data
        self._process()

    def resolve_flush_mode(self) -> str:
        return self.flush_mode

    async def flush(self, *, timeout: float, mode: str | None = None) -> str:
        self.flush_calls.append(len(self._inbox))
        if self.flush_statuses:
            return self.flush_statuses.pop(0)
        return "OK"

    async def set_baudrate(self, baudrate: int) -> str:
        self.baud_reconfigures.append(baudrate)
        self.events.append(f"host_baud={baudrate}")
        self.baudrate = baudrate
        return "async"

    # -- device model ------------------------------------------------------

    def commit(self, addr: int, wire: bytes, tag: int) -> None:
        """Place `wire` at `addr` with `tag` stamped at +4, as a committed slot."""
        self.memory[addr : addr + len(wire)] = wire
        self.memory[addr + 4 : addr + 8] = tag.to_bytes(4, "little")

    def region(self, addr: int, length: int) -> bytes:
        return bytes(self.memory[addr : addr + length])

    def _reply(self, text: str) -> None:
        if self._on_data is not None:
            self._on_data(text.encode("ascii"))

    def _command_length(self, first: str) -> int | None:
        """Bytes in this command, ``0`` if undecidable yet, ``None`` if unknown."""
        if first == "Z":
            if len(self._inbox) < 2:
                return 0
            return 5 if self._inbox[1:2] == b"Q" else 3
        return self._LENGTHS.get(first)

    def _process(self) -> None:
        while self._inbox:
            if self._expect > 0:
                take = min(len(self._inbox), self._expect)
                chunk = bytes(self._inbox[:take])
                del self._inbox[:take]
                self.memory[self._write_addr : self._write_addr + take] = chunk
                self._write_addr += take
                self._expect -= take
                continue

            first = chr(self._inbox[0])
            length = self._command_length(first)
            if length is None:
                del self._inbox[:1]  # noise; the device ignores it
                continue
            if length == 0 or len(self._inbox) < length:
                return
            command = bytes(self._inbox[:length]).decode("ascii", "replace")
            del self._inbox[:length]
            self.commands.append(command)
            self.events.append(command)
            self._handle(command)

    def _handle(self, command: str) -> None:
        if command == "e":
            if self.ping_answers:
                self._reply("Ok")
        elif command == "M0931":
            self._reply("\r\n")
        elif command == "I":
            self._reply(self.identity)
        elif command == "H0":
            self._reply("Ok" if self.bootloader_mode else "Er")
        elif command == "H1":
            self._reply("boot 1.0")
        elif command == "Z6a":
            self._reply("Ok" if self.scratch_ok else "Er")
        elif command == "Z35":
            self._reply(self.layout)
        elif command == "ZQS02":
            if self.power is not None:
                self._reply(f"!S02,{1 if self.power else 0}\r\n")
        elif command == "K":
            pass
        elif command[0] == "A":
            self.addr = int(command[1:], 16)
        elif command[0] == "a":
            self.source = int(command[1:], 16)
        elif command[0] == "L":
            self.length = int(command[1:], 16)
        elif command[0] == "B":
            self.baud_commands.append(int(command[1:]))
        elif command == "R":
            self._reply(self.region(self.addr, self.length).hex() + "\r\n")
        elif command == "C":
            self._reply(f"CS={sum(self.region(self.addr, self.length)) & 0xFFFFFFFF:08x}")
        elif command == "D":
            self._expect = self.length
            self._write_addr = self.addr
            self.payload_writes.append((self.addr, self.length))
        elif command.startswith("S79"):
            sector, count = int(command[3:5], 16), self.length
            self.erases.append((sector, count))
            start = sector * SECTOR
            self.memory[start : start + count * SECTOR] = b"\xff" * (count * SECTOR)
            self._reply("x" * count + "OK")
        elif command.startswith("S37"):
            sector = int(command[3:5], 16)
            self.erases.append((sector, 1))
            self.memory[sector * SECTOR : (sector + 1) * SECTOR] = b"\xff" * SECTOR
            self._reply("OK")
        elif command == "G39":
            self.copies.append((self.addr, self.source, self.length))
            self.memory[self.addr : self.addr + self.length] = self.region(self.source, self.length)
            self._reply("Ok")
        elif command in ("Z97", "X"):
            self.left = command


@pytest.fixture
def fake_firmware() -> FakeFirmwareTransport:
    return FakeFirmwareTransport()


@pytest.fixture
def no_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the session's wall-clock waits.

    The real timings are load-bearing on hardware — a 5 s post-erase settle and a
    2 s pre-commit settle are there because flash needs them — so they stay as
    module constants and get neutralised here rather than shrunk in the source.
    Patching ``asyncio.sleep`` covers every one of them at once, including the
    per-command pacing, without a test having to know which constants a given code
    path touches.
    """
    import aiolumagen.firmware.session as session_module

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(session_module.asyncio, "sleep", _instant)
