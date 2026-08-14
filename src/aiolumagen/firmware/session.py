"""Async orchestration of a firmware update over a :class:`LumagenTransport`.

The only module in :mod:`aiolumagen.firmware` that does I/O. Everything it
depends on — command formatting, container parsing, the decision about what to
write — is pure and tested without a device.

Two things here are worth understanding before changing anything.

**The flush barrier is not optional.** See :meth:`FirmwareSession.barrier`. A
reimplementation that only *paces* writes reproduces the original failure, and
does so as silent byte loss at high baud rather than as an error.

**The header is the commit record.** Section 1 is written straight into a live
A/B slot with no staging area, and the device elects a slot at boot from its
container header alone. :meth:`FirmwareSession.stage_image` therefore writes
block 0 — which holds that header — *last*, turning an all-or-nothing write into
validate-then-commit. Until the header lands, the slot's magic is erased
``0xFF``, it cannot win an election, and an abort, crash or power cut all leave
the unit booting exactly what it booted before.

One transport, one subscriber. ESPHome's ``serial_proxy`` accepts a single
subscriber at a time, so a running :class:`~aiolumagen.client.LumagenClient`
against the same bridge must be disconnected before a session opens. This class
owns its own transport rather than borrowing the client's for that reason, and
because updater-mode replies must never reach the normal-mode parser.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from aiolumagen.exceptions import (
    LumagenConnectionError,
    LumagenFirmwareAbortError,
    LumagenFirmwareError,
    LumagenFirmwareImageError,
)
from aiolumagen.firmware.container import (
    HEADER_LEN,
    ContainerHeader,
    additive_checksum,
    expected_stored_checksum,
)
from aiolumagen.firmware.extract import SECTION0, SECTION1, FirmwareBundle
from aiolumagen.firmware.plan import DeviceStatus, PlannedSection, UpdatePlan, plan_update
from aiolumagen.firmware.protocol import (
    ADDR_LIVE,
    AUDIT_CHUNK_BLOCKS,
    BLOCK_DELAY,
    BLOCK_SIZE,
    CHECKPOINT_SETTLE,
    CHECKPOINT_TRIES,
    CHECKSUM_REPLY_LEN,
    CMD_ABORT,
    CMD_BEGIN_BLOCK,
    CMD_BOOTLOADER_PROBE,
    CMD_CHECKSUM,
    CMD_COPY,
    CMD_ENTER_UPDATE,
    CMD_FINISH,
    CMD_FLASH_LAYOUT,
    CMD_IDENTIFY,
    CMD_PING,
    CMD_POWER_QUERY,
    CMD_READ,
    CMD_SCRATCH_ACK,
    CMD_SCRATCH_PROBE,
    DEFAULT_CHUNK,
    ERASE_TOKEN_TIMEOUT,
    FLUSH_RETRY_DELAY,
    FLUSH_TIMEOUT,
    LIVE_PROBE_ADDRS,
    MAX_SAFE_CHUNK,
    POST_ERASE_DELAY,
    PROMOTE_ACK_TIMEOUT,
    REPLY_ERASE_DONE,
    REPLY_OK,
    SCRATCH_ADDR,
    SCRATCH_SECTOR,
    SECTION1_SLOTS,
    SECTOR_SIZE,
    SESSION_BAUD,
    DeviceIdentity,
    Section1Slot,
    blocks_for,
    erase_run,
    other_slot,
    pad_to_even_end,
    parse_checksum,
    parse_identity,
    parse_power_state,
    sectors_for,
    set_address,
    set_baud,
    set_length,
    set_source_address,
)
from aiolumagen.transport import FLUSH_MODE_NONE, LumagenTransport

_LOGGER = logging.getLogger(__name__)

BITS_PER_BYTE: Final = 10
MIN_SLEEP: Final = 0.020
"""Never ask the event loop for a sleep shorter than the OS timer can honour.

A coarse timer rounds a 5.6 ms request up to its granularity, which turned a
178 ms block into 512 ms during bring-up and built a silent backlog. The pacer
accumulates debt instead and only sleeps when it exceeds this.
"""


def wire_time(nbytes: int, baudrate: int) -> float:
    """Seconds `nbytes` occupies on the wire at `baudrate`, 8N1."""
    if baudrate <= 0:
        return 0.0
    return nbytes * BITS_PER_BYTE / baudrate


class _WirePacer:
    """Deadline-based pacing for the open-loop fallback.

    Tracks an absolute wire clock and lets debt accumulate rather than sleeping
    once per chunk, so overshoot from a coarse OS timer self-corrects instead of
    compounding. Only used when no flush barrier is available — with a barrier
    the transport itself provides backpressure and this is bypassed entirely.
    """

    def __init__(self, baudrate: int, pace: float = 1.15) -> None:
        self._baudrate = baudrate
        self._pace = pace
        self._deadline = 0.0

    def set_baud(self, baudrate: int) -> None:
        self._baudrate = baudrate

    def add(self, nbytes: int, now: float) -> float:
        needed = wire_time(nbytes, self._baudrate) * self._pace
        self._deadline = max(self._deadline, now) + needed
        debt = self._deadline - now
        return debt if debt > MIN_SLEEP else 0.0


class UpdatePhase(StrEnum):
    """Coarse stage of an update, for progress reporting."""

    CONNECTING = "connecting"
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    RATE_CHANGE = "rate_change"
    ERASING = "erasing"
    WRITING = "writing"
    VERIFYING = "verifying"
    AUDITING = "auditing"
    REPAIRING = "repairing"
    COMMITTING = "committing"
    PROMOTING = "promoting"
    FINISHING = "finishing"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Where a flash region disagrees with an image, block by block."""

    base: int
    total_blocks: int
    bad_blocks: tuple[int, ...] = ()
    erased_blocks: tuple[int, ...] = ()
    """Bad blocks that read back as entirely ``0xFF``.

    Diagnostically distinct from a bad block with content: erased means the write
    never arrived (a delivery failure — the ESP dropped it, or the device was deaf
    while programming), whereas a mismatch with real content means bytes arrived
    corrupted or shifted. The first points at flow control, the second at framing.
    """

    unanswered_blocks: tuple[int, ...] = ()
    """Blocks the device never answered a checksum for.

    Counted as bad, because an unverified block cannot be called good — but
    tracked separately, since a run of these usually means the device stopped
    responding rather than that the flash is wrong.
    """

    chunks_checked: int = 0
    checksums_issued: int = 0
    skipped_block0: bool = False

    @property
    def ok(self) -> bool:
        return not self.bad_blocks

    @property
    def bad_sectors(self) -> tuple[int, ...]:
        """Absolute device sector numbers containing at least one bad block.

        The unit a repair has to erase: NOR programming only clears bits, so a
        written block cannot be patched in place.
        """
        return tuple(
            sorted({(self.base + block * BLOCK_SIZE) // SECTOR_SIZE for block in self.bad_blocks})
        )

    @property
    def runs(self) -> tuple[tuple[int, int], ...]:
        """Bad blocks collapsed into contiguous ``(first, last)`` runs.

        Shape matters when reading a failure: one long run looks like the transfer
        stopped, while many isolated singles look like sporadic byte loss.
        """
        if not self.bad_blocks:
            return ()
        out: list[tuple[int, int]] = []
        start = previous = self.bad_blocks[0]
        for block in self.bad_blocks[1:]:
            if block == previous + 1:
                previous = block
                continue
            out.append((start, previous))
            start = previous = block
        out.append((start, previous))
        return tuple(out)

    def describe(self) -> str:
        lines = [
            f"audit of {self.base:#08x}: {self.total_blocks} blocks, "
            f"{self.checksums_issued} checksums issued"
        ]
        if self.skipped_block0:
            lines.append("  block 0 skipped (commit header deliberately unwritten)")
        if self.ok:
            lines.append("  every block matches the image")
            return "\n".join(lines)
        lines.append(f"  {len(self.bad_blocks)} of {self.total_blocks} blocks disagree")
        if self.erased_blocks:
            lines.append(f"  {len(self.erased_blocks)} read as erased 0xff (never written)")
        if self.unanswered_blocks:
            lines.append(f"  {len(self.unanswered_blocks)} unanswered (device silent)")
        shown = ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in self.runs[:12])
        more = f" ... and {len(self.runs) - 12} more" if len(self.runs) > 12 else ""
        lines.append(f"  runs: {shown}{more}")
        lines.append(
            f"  spanning {len(self.bad_sectors)} sector(s): "
            + ", ".join(f"{s * SECTOR_SIZE:#08x}" for s in self.bad_sectors[:12])
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class UpdateProgress:
    """A progress notification.

    Byte counts are **host-side only**. The device sends nothing during a block,
    so there is no device-confirmed progress to report; ``bytes_done`` means
    "handed to the transport and flushed", which is as close as this protocol
    gets.
    """

    phase: UpdatePhase
    message: str
    section: str | None = None
    bytes_done: int = 0
    bytes_total: int = 0

    @property
    def fraction(self) -> float | None:
        if self.bytes_total <= 0:
            return None
        return min(1.0, self.bytes_done / self.bytes_total)


ProgressCallback = Callable[[UpdateProgress], None]


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Outcome of :meth:`FirmwareSession.run_update`."""

    plan: UpdatePlan
    written: tuple[str, ...] = ()
    promoted: bool = False
    powered_down: bool = False
    """Whether the session ended by powering the unit off with ``Z97``.

    True whenever anything was written that needs a restart to take effect — a
    promoted section 0 *or* a committed section-1 slot. Distinct from
    :attr:`promoted`, which is specifically about section 0's scratch-to-live copy.
    """

    dry_run: bool = False
    flush_mode: str | None = None
    flush_calls: int = 0
    flush_retries: int = 0
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.written)


@dataclass
class _Stats:
    flush_calls: int = 0
    flush_retries: int = 0
    notes: list[str] = field(default_factory=list)


class FirmwareSession:
    """A firmware-update session against one Lumagen.

    Usually driven through :func:`update_firmware`. Use it directly when you need
    the individual steps — reading the flash map, auditing a region, staging
    without promoting.

    ::

        async with FirmwareSession(url) as session:
            identity = await session.preflight()
            status = await session.read_status(bundle)
            plan = plan_update(bundle, status)

    The context manager guarantees the hand-back: it always returns the line rate
    to 9600 and leaves updater mode, because a device stranded in updater mode at
    230400 will not answer the next client that connects at 9600.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        transport: Any = None,
        baudrate: int = SESSION_BAUD,
        chunk: int = DEFAULT_CHUNK,
        block_delay: float = BLOCK_DELAY,
        use_flush: bool = True,
        flush_timeout: float = FLUSH_TIMEOUT,
        flush_retry_delay: float = FLUSH_RETRY_DELAY,
    ) -> None:
        """
        :param url: serialx URL. For an ESPHome bridge,
            ``esphome://<host>:6053/?port_name=Lumagen&key=<psk>``.
        :param transport: a pre-built transport, used instead of `url`. Mainly for
            tests; must implement the same surface as :class:`LumagenTransport`.
        :param chunk: bytes per host write. Capped at
            :data:`~aiolumagen.firmware.protocol.MAX_SAFE_CHUNK` — larger writes
            are silently discarded by the ESP's output pool.
        :param use_flush: set False only to reproduce the pre-barrier behaviour.
            Byte loss then becomes undetectable.
        :param flush_timeout: ceiling on a single barrier.
        :param flush_retry_delay: pause between barrier re-issues. Injectable so
            tests can exercise the retry loop without waiting on wall clock.
        """
        if url is None and transport is None:
            raise LumagenFirmwareError("FirmwareSession needs either a url or a transport")
        self._transport: Any = transport or LumagenTransport(url or "", baudrate=baudrate)
        self._owns_transport = transport is None
        self.baudrate = baudrate
        self.chunk = min(chunk, MAX_SAFE_CHUNK)
        self.block_delay = block_delay
        self.use_flush = use_flush
        self.flush_timeout = flush_timeout
        self.flush_retry_delay = flush_retry_delay

        self._buffer = bytearray()
        self._pacer = _WirePacer(baudrate)
        self._stats = _Stats()
        self.flush_mode: str | None = None
        self.commands_sent: list[str] = []
        self.entered_update_mode = False
        self.exited = False
        self.erased = False
        self.promoted = False
        self.requires_restart = False
        """True once something was written that only takes effect on the next boot.

        What :meth:`hand_back` keys the ``Z97`` power-down off — deliberately *not*
        :attr:`promoted`. Both kinds of write need a restart to become active:
        a promoted section 0 has to be loaded, and a committed section-1 slot has
        to win a boot election. Using ``promoted`` meant a section-1-only write
        left the device up with its new firmware sitting dormant.

        Staging to scratch without promoting does **not** set this: nothing boots
        from scratch, so there is nothing pending activation and no reason to
        interrupt the user's viewing.
        """
        self._progress: ProgressCallback | None = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> FirmwareSession:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        # Whether the body raised decides how to exit — see hand_back(failed=...).
        await self.hand_back(failed=exc_type is not None)
        await self.close()

    async def connect(self) -> None:
        self._transport.set_data_callback(self._feed)
        await self._transport.connect()

    async def close(self) -> None:
        if self._owns_transport:
            await self._transport.disconnect()

    async def hand_back(self, *, failed: bool = False) -> None:
        """Return the device to a state the next client can talk to.

        :param failed: the session is unwinding from an error. Suppresses the
            ``Z97`` power-down even when something was written, because a reboot is
            the *worst* next step after a partial update — see below.

        Bounded and non-raising, and called from every exit path including
        failures. Restores 9600 and leaves updater mode; a session that aborted
        mid-write still has to do both, or the unit sits at 230400 in updater mode
        answering nobody.

        Which exit is used turns on :attr:`requires_restart` — whether anything was
        written that only takes effect on the next boot — **not** on whether a
        promotion happened. A promoted section 0 has to be loaded and a committed
        section-1 slot has to win a boot election; both need the restart, so both
        get ``Z97``. Keying this off ``promoted`` was a bug: a section-1-only write
        left the unit running with its new firmware sitting dormant.

        The two paths are deliberately asymmetric in the *order* of their steps:

        **Something needs activating** — ``Z97`` goes out **first, at the transfer
        rate**, and only then is our own side realigned to 9600. That ordering is
        load-bearing.

        The device does *not* power itself down when a copy completes: ``G39``
        acks and the unit stays up in updater mode. ``Z97`` is what powers it
        down, which is why the vendor sends it 62 ms after ``G39`` — at the
        negotiated rate, without renegotiating anything. The unit then comes back
        at 9600 on its own, so there is nothing to hand back *to* and no reason to
        renegotiate: pinging a unit that is switching off would only fail and log
        alarming warnings on a successful update.

        An earlier version realigned the host to 9600 *before* sending ``Z97``, on
        the mistaken belief that the copy itself triggered the power-down. On
        hardware that sent ``Z97`` at 9600 to a device still listening at 230400:
        the unit never powered down, and the garbage was partly parsed as commands
        — it began dumping flash, and recovering it took a drain, four pings and a
        ``Z97`` re-sent at the correct rate. Do not reorder these two statements.

        **Nothing needs activating** — a dry run, an abort, or a stage-to-scratch
        with no promotion. Nothing boots from scratch, so powering the unit off
        would interrupt the user for no reason. The device is still listening, so
        the rate must be renegotiated *with it* via :meth:`set_baud` (which sends
        ``B009600`` before switching our own side), not merely reconfigured
        locally. Changing only the host's rate would leave the following ``X``
        going out at 9600 to a device still at 230400, so it would never leave
        updater mode — precisely the state this method exists to avoid. Then
        ``X``, which returns it to normal operation with the power still on.

        **A failed run never powers down**, even with `requires_restart` set. The
        case that matters is section 1 committing and section 0 then failing. A
        committed slot is elected at the *next* boot, not immediately, so the unit
        is still running its old, self-consistent firmware — and leaving it powered
        on keeps it that way and reachable for an immediate retry, which can finish
        section 0 and then power down with a matched pair. Sending ``Z97`` instead
        would reboot straight into a new section 1 paired with old CPU firmware,
        which is the one combination no vendor session ever produces.
        """
        if self.exited or not self._transport.connected:
            return
        try:
            if self.requires_restart and not failed:
                if self.entered_update_mode:
                    await asyncio.wait_for(self.leave(finish=True), timeout=10.0)
                if self.baudrate != SESSION_BAUD:
                    await self._set_transport_baud(SESSION_BAUD)
                return

            if self.requires_restart:
                _LOGGER.warning(
                    "The update did not complete, so the unit has been left POWERED "
                    "ON rather than restarted. Something was already written that "
                    "only takes effect on reboot, so do not power-cycle yet — retry "
                    "the update first, or the device will come up with mismatched "
                    "firmware sections."
                )

            if self.baudrate != SESSION_BAUD:
                await asyncio.wait_for(self.set_baud(SESSION_BAUD, pings=2), timeout=30.0)
            if self.entered_update_mode:
                await asyncio.wait_for(self.leave(finish=False), timeout=10.0)
        except Exception as err:
            _LOGGER.warning(
                "Could not hand the device back cleanly: %s. It may still be in "
                "updater mode at %d baud — recover by disconnecting AC power, "
                "reconnecting, and powering on.",
                err,
                self.baudrate,
            )

    # -- framing primitives ------------------------------------------------

    def _feed(self, data: bytes) -> None:
        self._buffer += data

    def _emit(self, progress: UpdateProgress) -> None:
        if self._progress is not None:
            self._progress(progress)

    @staticmethod
    def _command_pace(nbytes: int) -> float:
        """``min((n-1)*50 + 150, 1000) / 8`` ms — the vendor's post-write pause."""
        return (min((nbytes - 1) * 50 + 150, 1000) // 8) / 1000.0

    async def _write(self, data: bytes) -> None:
        await self._transport.write(data)
        await asyncio.sleep(self._command_pace(len(data)))

    async def send(self, command: str) -> None:
        """Send a command: first character alone, then the remainder, no terminator.

        The split mirrors the vendor byte-for-byte. It looks pointless and is kept
        anyway — the device's command dispatch keys off that first byte, this is
        the only framing ever validated against hardware, and the cost is one
        extra write per command.
        """
        self.commands_sent.append(command)
        raw = command.encode("ascii")
        await self._write(raw[:1])
        if len(raw) > 1:
            await self._write(raw[1:])

    async def read_bytes(self, count: int, tries: int = 50, interval: float = 0.010) -> bytes:
        """Wait up to ``tries * interval`` for `count` bytes, then take what arrived."""
        for _ in range(tries):
            if len(self._buffer) >= count:
                break
            await asyncio.sleep(interval)
        if not self._buffer:
            return b""
        take = min(len(self._buffer), count)
        out = bytes(self._buffer[:take])
        del self._buffer[:take]
        return out

    async def read_token(self, count: int = 8) -> str:
        return (await self.read_bytes(count)).decode("ascii", "replace").strip("\r\n\x00")

    async def read_line(self, timeout: float = 1.0) -> bytes:
        """Accumulate until CRLF and return the line without it.

        Only ``R`` replies and normal-mode ``ZQ`` responses are CRLF-terminated;
        the short updater tokens (``Ok``, ``00``, ``99``) are not. Reading to the
        delimiter where one exists is self-synchronising, and stops a partial read
        from shifting every subsequent reply by one response.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\r\n" in self._buffer:
                break
            await asyncio.sleep(0.005)
        index = self._buffer.find(b"\r\n")
        if index < 0:
            line = bytes(self._buffer)
            self._buffer.clear()
            return line
        line = bytes(self._buffer[:index])
        del self._buffer[: index + 2]
        return line

    async def drain_rx(self, settle: float = 0.08, cap: float = 1.0) -> None:
        """Discard pending input until the line has been quiet for `settle`.

        The settle window has to exceed the tail of a reply still arriving at 9600,
        or replies end up misattributed to the following command.
        """
        deadline = time.monotonic() + cap
        await asyncio.sleep(0.005)
        while time.monotonic() < deadline:
            self._buffer.clear()
            await asyncio.sleep(settle)
            if not self._buffer:
                return

    # -- flow control ------------------------------------------------------

    async def barrier(self, nbytes: int) -> bool:
        """Block until the proxy has handed every queued byte to the UART.

        **This is the piece the ESP path was missing, and the reason high baud
        rates failed.**

        The vendor never needed it: its write is flow-controlled by hardware end
        to end. A 4096-byte bulk-OUT URB completes in 172.0 ms against 177.8 ms of
        wire time at 230400 (median over 111 blocks), and that 5.8 ms deficit is
        exactly the FT232R's 128-byte TX FIFO — so the chip NAKs the host and
        ``WriteFile`` returns only once the bytes are physically gone. The vendor
        *cannot* outrun the Lumagen.

        Our path had no such property. ``serial_proxy`` calls ``write_array()``
        with a void return, ESPHome's output pool discards whatever doesn't fit
        while logging only locally, and serialx reports a write buffer size of 0
        unconditionally so asyncio's own flow control never fires. Every byte
        travelled on dead reckoning.

        What made the failures look like a *pacing* problem is that the pool's
        runway measured in time collapses as baud rises — 8.5 s at 9600, 356 ms at
        230400 — against ESP stalls that are roughly constant in wall clock. So
        the failure rate tracked baud, while inter-block delay (already more
        generous than the vendor's) made no difference. Anyone tempted to replace
        this with a ``sleep`` will rediscover that.

        :returns: True when the barrier held; False when none is available and the
            caller should fall back to pacing.
        :raises LumagenFirmwareAbortError: if the proxy never finishes draining.
            Continuing would write into a queue that is discarding bytes.
        """
        if not self.use_flush:
            return False
        if self.flush_mode is None:
            self.flush_mode = self._transport.resolve_flush_mode()
            if self.flush_mode == FLUSH_MODE_NONE:
                _LOGGER.warning(
                    "Transport exposes no flush; falling back to open-loop pacing. "
                    "Byte loss will not be detectable."
                )
                self.use_flush = False
                return False
            _LOGGER.debug("Flow control: proxy flush barrier (%s)", self.flush_mode)

        deadline = time.monotonic() + self.flush_timeout
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LumagenFirmwareAbortError(
                    f"the proxy did not finish flushing {nbytes} byte(s) within "
                    f"{self.flush_timeout:.0f}s ({attempts} attempts). It is not "
                    "draining to the UART, so anything sent now would be dropped "
                    "silently. Nothing further has been written — check the ESP log "
                    "for 'Output pool full' and power-cycle the bridge."
                )
            try:
                status = await self._transport.flush(
                    timeout=max(5.0, min(remaining, self.flush_timeout)),
                    mode=self.flush_mode,
                )
            except Exception as err:
                # A broken barrier must not abort a fourteen-minute transfer by
                # itself; a genuinely dead link will surface at the next command
                # read anyway. Warn once, then continue open-loop.
                _LOGGER.warning(
                    "Flush failed (%r); reverting to open-loop pacing. "
                    "Byte loss is no longer detectable.",
                    err,
                )
                self.use_flush = False
                return False

            attempts += 1
            self._stats.flush_calls += 1
            if status == "TIMEOUT":
                # Expected, not an error. The ESP's own flush budget is shorter
                # than a block's wire time, so it reports TIMEOUT while still
                # draining. Re-issue until the queue empties.
                self._stats.flush_retries += 1
                await asyncio.sleep(self.flush_retry_delay)
                continue
            if status in ("OK", "ASSUMED_SUCCESS"):
                return True

            # ERROR / NOT_SUPPORTED are structural rather than transient.
            _LOGGER.warning(
                "Proxy flush returned %s; reverting to open-loop pacing. "
                "Byte loss is no longer detectable.",
                status,
            )
            self.use_flush = False
            return False

    async def write_raw(self, data: bytes) -> None:
        """Push `data` to the wire in :attr:`chunk` pieces, barriered per piece."""
        barriered = True
        for start in range(0, len(data), self.chunk):
            piece = data[start : start + self.chunk]
            await self._transport.write(piece)
            if await self.barrier(len(piece)):
                continue
            barriered = False
            delay = self._pacer.add(len(piece), time.monotonic())
            if delay:
                await asyncio.sleep(delay)
        if not barriered:
            # Open-loop only: let the last chunk clear the wire before the next
            # command is framed. A held barrier already guarantees this.
            await asyncio.sleep(wire_time(min(len(data), self.chunk), self.baudrate))

    # -- device operations -------------------------------------------------

    async def power_check(self) -> bool | None:
        """Normal-mode power state, queried *before* entering updater mode."""
        await self.drain_rx()
        await self.send(CMD_POWER_QUERY)
        line = await self.read_line(timeout=1.5)
        return parse_power_state(line.decode("ascii", "replace"))

    async def preflight(self) -> DeviceIdentity:
        """Refuse to touch flash unless the device is in a known-good state.

        Five gates, each of which caused a real failure before it existed. All of
        them run before anything is erased, so every refusal here is free.

        :raises LumagenFirmwareAbortError: on any gate. Nothing was written.
        """
        power = await self.power_check()
        if power is False:
            raise LumagenFirmwareAbortError(
                "the Lumagen reports standby. It must be powered on — in standby it "
                "does not service the updater command set at all, so every "
                "subsequent command would time out without explaining why."
            )

        await self.send(CMD_ENTER_UPDATE)
        await self.read_bytes(2)  # CRLF, when entering from normal operation
        self.entered_update_mode = True
        await self.drain_rx()

        await self.send(CMD_PING)
        if (await self.read_token()) != REPLY_OK:
            raise LumagenFirmwareAbortError(
                "no 'Ok' from the ping after M0931 — the device did not enter "
                "updater command mode. Nothing was written."
            )
        await self.drain_rx()

        await self.send(CMD_IDENTIFY)
        identity = parse_identity(await self.read_token(32))
        if identity is None:
            raise LumagenFirmwareAbortError(
                "the device did not answer the identify query. Nothing was written."
            )
        await self.drain_rx()

        # H0 answers 'Ok' only in bootloader mode. Everything here assumes normal
        # mode: the bootloader path uses a different block size, is unverified, and
        # is brick-capable, so refuse rather than guess.
        await self.send(CMD_BOOTLOADER_PROBE)
        if (await self.read_token()) == REPLY_OK:
            raise LumagenFirmwareAbortError(
                "the device is in BOOTLOADER mode, which this library deliberately "
                "does not support — that path is unverified and can leave the unit "
                "unbootable. Use the vendor's updater for bootloader recovery."
            )
        await self.drain_rx()

        await self.send(CMD_SCRATCH_PROBE)
        scratch = await self.read_token()
        if scratch != REPLY_OK:
            raise LumagenFirmwareAbortError(
                f"the scratch-region probe returned {scratch!r}, not 'Ok'. There is "
                "nowhere safe to stage section 0, so nothing was written."
            )
        await self.send(CMD_SCRATCH_ACK)
        await self.drain_rx()

        _LOGGER.debug("Preflight passed: %s", identity)
        return identity

    async def flash_layout(self) -> str:
        """``Z35`` — the code naming the slot the next section-1 write should use.

        Always queried immediately before writing and **never cached**. It reports
        the slot the device is *not* running from, which is section 1's entire
        safety mechanism; a stale value points at live firmware.
        """
        await self.drain_rx()
        await self.send(CMD_FLASH_LAYOUT)
        code = await self.read_token(2)
        _LOGGER.debug("Z35 -> %r", code)
        return code

    async def read_at(self, addr: int, size: int) -> bytes:
        """Read `size` bytes via ``R``. Reply is ASCII hex, CRLF-terminated."""
        await self.drain_rx()
        await self.send(set_address(addr))
        await self.send(set_length(size))
        await self.send(CMD_READ)
        line = await self.read_line(timeout=max(2.0, size * 2 * 0.002 + 1.0))
        try:
            return bytes.fromhex(line.decode("ascii", "replace").strip())
        except ValueError:
            return line

    async def read_container_header(self, addr: int) -> ContainerHeader | None:
        """Read and decode the 16-byte container header at `addr`."""
        return ContainerHeader.unpack(await self.read_at(addr, HEADER_LEN))

    async def device_checksum(self, addr: int, length: int, timeout: float = 15.0) -> int | None:
        """``C`` over ``[addr, addr+length)``.

        The reply is ``CS=%8x`` with **no terminator**, so exactly 11 characters
        are read. Waiting for a line here burns the entire timeout on a reply the
        device already sent — the origin of a "60 s checksum" the device had
        actually answered in 165 ms.

        ``None`` means no answer, which mid-update usually means "still busy
        programming" rather than "wrong". Callers must not read it as a mismatch.
        """
        await self.send(set_length(length))
        await self.send(set_address(addr))
        await self.send(CMD_CHECKSUM)
        raw = await self.read_bytes(
            CHECKSUM_REPLY_LEN, tries=max(1, int(timeout / 0.01)), interval=0.01
        )
        await self.drain_rx()  # swallow a trailing CRLF, should any build add one
        return parse_checksum(raw.decode("ascii", "replace"))

    async def erase_run(self, first_sector: int, sectors: int) -> None:
        """Erase `sectors` sectors from `first_sector`, then settle.

        The device streams one ``x`` per sector (~340 ms each) and finishes with
        ``OK``. The settle afterwards is not padding — the vendor observes it and
        flash needs it.
        """
        await self.send(set_length(sectors))
        await self.send(erase_run(first_sector))

        seen = 0
        deadline = time.monotonic() + ERASE_TOKEN_TIMEOUT
        done = False
        while time.monotonic() < deadline:
            token = await self.read_bytes(1, tries=20, interval=0.05)
            if not token:
                continue
            char = token.decode("ascii", "replace")
            if char == "x":
                seen += 1
                self._emit(
                    UpdateProgress(
                        phase=UpdatePhase.ERASING,
                        message=f"erased sector {seen}/{sectors}",
                        bytes_done=seen,
                        bytes_total=sectors,
                    )
                )
            elif char == REPLY_ERASE_DONE[0]:
                if (await self.read_bytes(1)) == REPLY_ERASE_DONE[1:].encode():
                    done = True
                    break
        if not done:
            raise LumagenFirmwareAbortError(
                f"the erase did not report '{REPLY_ERASE_DONE}' within "
                f"{ERASE_TOKEN_TIMEOUT:.0f}s ({seen}/{sectors} sectors seen)."
            )
        if seen != sectors:
            raise LumagenFirmwareAbortError(
                f"the erase reported {seen} sector(s), expected {sectors}. Refusing "
                "to write into a region that may not be fully erased."
            )

        self.erased = True
        await asyncio.sleep(POST_ERASE_DELAY)

    async def _set_transport_baud(self, rate: int) -> None:
        await self._transport.set_baudrate(rate)
        self.baudrate = rate
        self._pacer.set_baud(rate)

    async def set_baud(self, rate: int, pings: int = 5) -> None:
        """Renegotiate the line rate, then prove the link before trusting it.

        Order matters: the ``B`` command has to clear the wire at the *old* rate
        before either end switches. Unlike the vendor we aren't racing a 375 ms
        port-reopen window, so the settles can be generous.

        Then ping. The vendor fires 21 pings straight after its own reopen for
        exactly this reason — an unanswered ping means a failed rate change, and
        finding that out now beats discovering it halfway through a write.
        """
        if rate == self.baudrate:
            return
        previous = self.baudrate
        await self.send(set_baud(rate))
        await asyncio.sleep(0.4)  # let 7 bytes clear the wire at the old rate

        try:
            await self._set_transport_baud(rate)
        except LumagenConnectionError as err:
            raise LumagenFirmwareAbortError(
                f"could not reconfigure the transport to {rate} baud: {err}. Nothing "
                "has been erased."
            ) from err
        _LOGGER.debug("Line rate %d -> %d", previous, rate)
        await asyncio.sleep(0.3)

        await self.drain_rx()
        for attempt in range(1, pings + 1):
            await self.send(CMD_PING)
            if (await self.read_token()) != REPLY_OK:
                raise LumagenFirmwareAbortError(
                    f"ping {attempt}/{pings} unanswered at {rate} baud — the rate "
                    "change failed. Nothing has been erased."
                )

    async def rate_check(self, addrs: tuple[int, ...] = LIVE_PROBE_ADDRS, size: int = 64) -> bool:
        """Prove the link at the current rate by reading each address twice.

        Byte loss is stochastic, so a dropped byte shows up as two reads of the
        same address disagreeing.

        Deliberately does **not** compare against the image being written. That
        check is circular: an aborted run leaves the target region partial, so
        gating the corrective rewrite on the target already being correct blocks
        the one operation that would fix it. This reads *live* firmware instead,
        which is populated, stable, and unaffected by anything here.
        """
        ok = True
        for addr in addrs:
            first = await self.read_at(addr, size)
            second = await self.read_at(addr, size)
            if len(first) != size or len(second) != size:
                _LOGGER.warning(
                    "%#08x: short read (%d/%d of %d bytes)", addr, len(first), len(second), size
                )
                ok = False
            elif first != second:
                _LOGGER.warning("%#08x: two reads disagree — bytes are being lost", addr)
                ok = False
        return ok

    async def resync(self, blocksize: int = BLOCK_SIZE) -> bool:
        """Run a desynced device's payload counter out with harmless ``e`` bytes.

        The protocol has no framing and the device has no inter-byte timeout: it
        counts payload bytes and waits indefinitely. So one lost byte leaves it
        consuming the following ``A<addr>`` and ``D`` as data, returning to command
        state somewhere inside a later block — at which point firmware bytes get
        parsed as commands. That is a real recorded failure, where the device
        answered a checksum for an address the host had moved on from long before.

        The vendor's recovery is to send ``e`` exactly ``blocksize`` times, padding
        out the block the device is still waiting on. ``e`` is safe in *both*
        states: as payload it lands in a region about to be rewritten anyway, and
        as a command it's just a ping.
        """
        _LOGGER.debug("Resync: padding %d 'e' bytes to run out the counter", blocksize)
        await self.write_raw(b"e" * blocksize)
        # Worst case the device was in command state and answered every byte, so
        # two bytes back per byte sent. Drain for as long as that takes.
        await self.drain_rx(settle=0.15, cap=wire_time(2 * blocksize, self.baudrate) + 5.0)
        for _ in range(3):
            await self.send(CMD_PING)
            if (await self.read_token()) == REPLY_OK:
                return True
            await self.drain_rx()
        return False

    # -- audit and repair --------------------------------------------------

    @staticmethod
    def _expected_sum(image: bytes, start: int, end: int, stamped_tag: int | None) -> int:
        """Image checksum over ``[start, end)``, corrected for a stamped tag.

        The device overwrites the four container bytes at ``+4`` when it commits a
        slot, so any range covering them sums differently on the device than in the
        file. Without this correction an audit of a byte-perfect *committed* slot
        reports block 0 — and the coarse chunk containing it — as bad, every time.
        Exactly the trap that makes the planner's comparison need the same fix.
        """
        total = additive_checksum(image[start:end])
        if stamped_tag is not None and start == 0 and end >= 8:
            total = (
                total
                - additive_checksum(image[4:8])
                + additive_checksum(stamped_tag.to_bytes(4, "little"))
            )
        return total & 0xFFFFFFFF

    async def audit(
        self,
        image: bytes,
        base: int,
        *,
        chunk_blocks: int = AUDIT_CHUNK_BLOCKS,
        skip_block0: bool = False,
        stamped_tag: int | None = None,
    ) -> AuditResult:
        """Locate where flash at `base` disagrees with `image`. **Read-only.**

        A whole-region checksum says only "something is wrong somewhere in 3 MB".
        This narrows it down: sum coarse chunks, then subdivide only the ones that
        disagree — see :data:`~aiolumagen.firmware.protocol.AUDIT_CHUNK_BLOCKS` for
        why that is dramatically cheaper than checking every block.

        Nothing is erased or written, so this is safe against a slot left
        mid-write. That is the point: the evidence disappears on the next attempt,
        so it has to be collectable before anything else happens.

        Deliberately does not change the line rate. The checksums are computed on
        the device and each reply is 11 bytes, so a faster link buys nothing here
        and only adds a failure mode.

        :param skip_block0: block 0 is expected to be unwritten — the header-last
            case, mid-write and pre-commit.
        :param stamped_tag: the tag a committed slot carries at ``+4``, from
            :meth:`read_container_header`. Required to audit a committed section-1
            slot without a spurious block-0 mismatch.
        """
        if chunk_blocks < 1:
            raise LumagenFirmwareError("chunk_blocks must be at least 1")
        total_blocks = blocks_for(len(image))
        bad: list[int] = []
        erased: list[int] = []
        unanswered: list[int] = []
        issued = 0
        chunks = 0

        async def compare(first: int, count: int) -> tuple[int | None, int]:
            nonlocal issued
            start = first * BLOCK_SIZE
            end = min(len(image), (first + count) * BLOCK_SIZE)
            want = self._expected_sum(image, start, end, stamped_tag)
            got = await self.device_checksum(base + start, end - start, timeout=90.0)
            issued += 1
            return got, want

        for first in range(0, total_blocks, chunk_blocks):
            count = min(chunk_blocks, total_blocks - first)
            chunks += 1
            got, want = await compare(first, count)
            self._emit(
                UpdateProgress(
                    phase=UpdatePhase.AUDITING,
                    message=(
                        f"blocks {first}-{first + count - 1} {'ok' if got == want else 'MISMATCH'}"
                    ),
                    bytes_done=min(len(image), (first + count) * BLOCK_SIZE),
                    bytes_total=len(image),
                )
            )
            if got == want:
                continue
            # Only a failing chunk is worth subdividing.
            for block in range(first, first + count):
                if skip_block0 and block == 0:
                    continue
                got_one, want_one = await compare(block, 1)
                if got_one == want_one:
                    continue
                bad.append(block)
                if got_one is None:
                    unanswered.append(block)
                    continue
                size = len(image[block * BLOCK_SIZE : (block + 1) * BLOCK_SIZE])
                if got_one == (0xFF * size) & 0xFFFFFFFF:
                    erased.append(block)

        return AuditResult(
            base=base,
            total_blocks=total_blocks,
            bad_blocks=tuple(bad),
            erased_blocks=tuple(erased),
            unanswered_blocks=tuple(unanswered),
            chunks_checked=chunks,
            checksums_issued=issued,
            skipped_block0=skip_block0,
        )

    async def repair(
        self,
        image: bytes,
        base: int,
        *,
        chunk_blocks: int = AUDIT_CHUNK_BLOCKS,
        skip_block0: bool = False,
        passes: int = 1,
    ) -> AuditResult:
        """Erase and rewrite only the sectors containing bad blocks, then re-audit.

        The right response to a link that dropped a few blocks out of 772 is not to
        rewrite the whole region and hope — that re-rolls the same dice across the
        entire transfer. An :meth:`audit` already says which blocks are wrong, and
        NOR flash sets the granularity: programming only clears bits, so a written
        block cannot be patched in place and the unit of repair is the 128 KiB
        sector. Erasing a sector destroys the good blocks sharing it, so *all* of
        its blocks are rewritten, not just the bad ones.

        **For uncommitted regions only** — the scratch area, or a section-1 slot
        whose header is still withheld. It takes no ``stamped_tag`` for that
        reason: erasing the sector holding the container header would un-commit an
        already-live slot, and rewriting block 0 from the file would restore an
        *unstamped* header. If a committed slot is damaged, re-stage it properly
        rather than patching it.

        Throughout a header-last repair the header stays unwritten, so the slot
        cannot be elected and every step is reversible by simply running it again.

        :param passes: how many erase-rewrite-audit rounds to attempt before
            giving up. More than one is rarely useful — a repair that doesn't
            converge on the first pass usually means the link is still dropping
            bytes, and the answer is a lower rate rather than another attempt.
        :returns: the audit from the final pass. Check :attr:`AuditResult.ok`.
        """
        blocks_per_sector = SECTOR_SIZE // BLOCK_SIZE
        total_blocks = blocks_for(len(image))
        result = await self.audit(image, base, chunk_blocks=chunk_blocks, skip_block0=skip_block0)

        for attempt in range(1, max(1, passes) + 1):
            if result.ok:
                return result
            self._emit(
                UpdateProgress(
                    phase=UpdatePhase.REPAIRING,
                    message=(
                        f"pass {attempt}: rewriting {len(result.bad_sectors)} sector(s) "
                        f"holding {len(result.bad_blocks)} bad block(s)"
                    ),
                )
            )
            for sector in result.bad_sectors:
                sector_offset = sector * SECTOR_SIZE - base
                first_block = sector_offset // BLOCK_SIZE
                last_block = min(total_blocks, first_block + blocks_per_sector)
                await self.erase_run(sector, 1)

                current_length = -1
                for block in range(first_block, last_block):
                    if skip_block0 and block == 0:
                        continue  # the commit block stays withheld
                    offset = block * BLOCK_SIZE
                    payload = image[offset : offset + BLOCK_SIZE]
                    if len(payload) != current_length:
                        await self.send(set_length(len(payload)))
                        current_length = len(payload)
                    await self.send(set_address(base + offset))
                    await self.send(CMD_BEGIN_BLOCK)
                    await self.write_raw(payload)
                    if self.block_delay:
                        await asyncio.sleep(self.block_delay)

            await asyncio.sleep(CHECKPOINT_SETTLE)
            await self.drain_rx()
            result = await self.audit(
                image, base, chunk_blocks=chunk_blocks, skip_block0=skip_block0
            )

        return result

    async def stage_image(
        self,
        image: bytes,
        base: int,
        *,
        header_last: bool = False,
        before_header: Callable[[], Awaitable[None]] | None = None,
        on_block: Callable[[int, int], None] | None = None,
    ) -> None:
        """Write `image` to `base` in :data:`BLOCK_SIZE` chunks.

        Command order and the sticky ``L`` register match the recorded vendor
        session exactly: ``L<len>`` only when the length changes, then ``A<addr>``,
        then ``D`` and the payload.

        `header_last` defers block 0 to the end. Every block carries its own
        ``A<addr>``, so this is a pure reordering — the device is never told the
        blocks are sequential and no new command is introduced. Block 0 holds the
        container header, and a section-1 slot is elected at boot on that header
        alone, so writing it first makes a section-1 write uninterruptible from
        about 2% onwards. Writing it last turns those bytes into a commit record.

        `before_header` runs once the whole payload is down and before the commit.
        It is the only genuinely safe point in a section-1 write to issue a
        command: if it wedges the device, the header never lands and the unit
        boots exactly as before.
        """
        total = len(image)
        count = blocks_for(total)
        order = list(range(count))
        if header_last and count > 1:
            order = [*order[1:], 0]

        current_length = -1
        written = 0
        for index in order:
            offset = index * BLOCK_SIZE
            block = image[offset : offset + BLOCK_SIZE]

            if header_last and index == 0 and before_header is not None:
                await before_header()
                current_length = -1  # the hook framed its own L/A registers

            if len(block) != current_length:
                await self.send(set_length(len(block)))
                current_length = len(block)
            await self.send(set_address(base + offset))
            await self.send(CMD_BEGIN_BLOCK)
            await self.write_raw(block)

            # The one thing the barrier cannot cover: the device stops reading its
            # UART while programming a block, and anything arriving in that window
            # is lost with no error anywhere.
            if self.block_delay:
                await asyncio.sleep(self.block_delay)

            written += len(block)
            if on_block is not None:
                on_block(written, total)

    async def promote(self, dest: int, source: int, length: int) -> None:
        """Copy staged bytes over live firmware. **This is the irreversible step.**

        Command order is verbatim from the recorded session: ``A<dest>``,
        ``a<source>``, ``L<length>``, ``G39``. The lowercase ``a`` appears exactly
        once in an entire update — only here — which makes its presence a useful
        check that the sequence is right.
        """
        await self.send(set_address(dest))
        await self.send(set_source_address(source))
        await self.send(set_length(length))
        await self.send(CMD_COPY)
        self.promoted = True
        self.requires_restart = True

    async def await_promote_ack(self, timeout: float = PROMOTE_ACK_TIMEOUT) -> bool:
        """Wait for ``G39``'s ``Ok``, which arrives when the internal copy finishes.

        The vendor sends ``Z97`` 62 ms after ``G39`` and never reads this, so it's
        easy to conclude nothing answers. It does — observed ~6.6 s after ``G39``
        for a 458 KB copy. Consuming it matters twice over: it's a precise
        completion signal, far better than polling a device busy writing flash;
        and left in the buffer it collides with the next reply (seen once as
        ``OkCS=025baa``, which misparsed).
        """
        raw = await self.read_bytes(2, tries=max(1, int(timeout / 0.05)), interval=0.05)
        return raw.decode("ascii", "replace") == REPLY_OK

    async def leave(self, *, finish: bool) -> None:
        """Leave updater mode.

        `finish` sends ``Z97``, which **powers the unit down** — correct after a
        promotion, because that power cycle is how the new firmware gets loaded.
        Otherwise ``X``, which returns to normal operation with the power on.
        """
        await self.send(CMD_FINISH if finish else CMD_ABORT)
        self.exited = True

    # -- planning ----------------------------------------------------------

    async def read_status(self) -> DeviceStatus:
        """Snapshot what the device reports, for :func:`plan_update`.

        Read-only and safe at any time. Reads the *live* section-1 slot — the one
        ``Z35`` doesn't name — sizing the checksum from that slot's own container
        header so the read is independent of any candidate image.
        """
        identity = parse_identity(await self._identify_raw())
        code = await self.flash_layout()
        target = SECTION1_SLOTS.get(code)
        live = other_slot(code) if target is not None else None

        header: ContainerHeader | None = None
        checksum: int | None = None
        if live is not None:
            header = await self.read_container_header(live.address)
            if header is not None and header.has_magic and 0 < header.size <= 0x400000:
                checksum = await self.device_checksum(
                    live.address, HEADER_LEN + header.size, timeout=60.0
                )
            else:
                header = None
        return DeviceStatus(
            identity=identity,
            section1_target_code=code if target is not None else None,
            section1_live_header=header,
            section1_live_checksum=checksum,
        )

    async def _identify_raw(self) -> str:
        await self.drain_rx()
        await self.send(CMD_IDENTIFY)
        return await self.read_token(32)

    # -- section writers ---------------------------------------------------

    async def _verify_region(self, addr: int, image: bytes, *, label: str) -> None:
        """Whole-region checksum plus three spot reads.

        Two independent mechanisms on purpose. The checksum is one 32-bit additive
        sum over megabytes, which is strong against the failure that matters here
        (lost or shifted bytes) but is still a single number; the spot reads
        confirm actual content at the ends and middle. Both agreeing is
        meaningfully better than either alone.
        """
        expected = additive_checksum(image)
        got = await self.device_checksum(addr, len(image), timeout=90.0)
        if got is None:
            raise LumagenFirmwareAbortError(
                f"{label}: the device did not answer the verification checksum."
            )
        if got != expected:
            raise LumagenFirmwareAbortError(
                f"{label}: checksum mismatch — device reports {got:#010x}, expected "
                f"{expected:#010x}. The region was not written correctly."
            )
        for offset in (0, len(image) // 2, max(0, len(image) - 16)):
            want = image[offset : offset + 16]
            if not want:
                continue
            got_bytes = await self.read_at(addr + offset, len(want))
            if got_bytes != want:
                raise LumagenFirmwareAbortError(
                    f"{label}: spot check at +{offset:#x} differs "
                    f"(read {got_bytes.hex()}, expected {want.hex()})."
                )

    async def _write_section1(self, section: PlannedSection) -> None:
        """Write section 1 into the inactive A/B slot, header last.

        There is no scratch area for section 1 and no abort point once the header
        lands, so every safety property here comes from ordering: query ``Z35``
        fresh, cross-check it against the generation tags, withhold the header
        until the payload verifies, and only then commit.
        """
        assert section.image is not None
        code = await self.flash_layout()
        target = SECTION1_SLOTS.get(code)
        if target is None:
            raise LumagenFirmwareAbortError(
                f"Z35 returned {code!r}, which names no known section-1 slot. "
                "Refusing to guess where to write. Nothing was erased."
            )
        await self._cross_check_slot(target)

        image = pad_to_even_end(target.address, section.image.wire_bytes)
        self._emit(
            UpdateProgress(
                phase=UpdatePhase.ERASING,
                message=f"erasing section 1 slot {code} at {target.address:#08x}",
                section=SECTION1,
            )
        )
        await self.erase_run(target.first_sector, sectors_for(len(image)))

        async def _checkpoint() -> None:
            await self._precommit_checkpoint(target.address, image)

        await self._stage_with_progress(
            image, target.address, SECTION1, header_last=True, before_header=_checkpoint
        )

        self._emit(
            UpdateProgress(
                phase=UpdatePhase.VERIFYING, message="verifying section 1", section=SECTION1
            )
        )
        await asyncio.sleep(CHECKPOINT_SETTLE)
        await self.drain_rx()
        header = await self.read_container_header(target.address)
        if header is None:
            raise LumagenFirmwareError(
                "section 1: the device did not answer a header read after the commit. "
                "The payload verified before the header was written, so the slot is "
                "most likely fine — power-cycle the unit and re-read before retrying."
            )
        if not header.has_magic:
            raise LumagenFirmwareError(
                f"section 1: the commit header read back as {header.magic:#010x}, not "
                "valid container magic, so the slot is not committed. The unit still "
                "boots its previous firmware; power-cycle and retry the update."
            )
        # Note the tag read back here may still be 0xFFFFFFFF: the bootloader
        # stamps the generation at boot, not at write time, so a freshly committed
        # slot is unstamped. expected_stored_checksum handles both — with an
        # unstamped tag the correction is a no-op and this reduces to the raw sum.
        expected = expected_stored_checksum(image, header.tag)
        got = await self.device_checksum(target.address, len(image), timeout=90.0)
        if got is not None and got != expected:
            raise LumagenFirmwareError(
                f"section 1: post-commit checksum {got:#010x}, expected "
                f"{expected:#010x} (tag {header.tag:#010x}). The slot is committed "
                "but may be wrong — do not power-cycle without re-flashing."
            )

        # A committed slot is not yet the running one: it wins the next boot
        # election on its container magic and is branded with a generation then. So
        # this needs a restart to take effect just as much as a promoted section 0
        # does, and hand_back must power the unit down for it.
        self.requires_restart = True
        self._stats.notes.append(
            f"section 1 committed at {target.address:#08x} and verified; it becomes "
            "active when the unit next powers on."
        )

    async def _cross_check_slot(self, target: Section1Slot) -> None:
        """Independently confirm ``Z35`` nominated the slot that is *not* running.

        ``Z35`` is trusted for the address but not blindly: the container tags
        carry a monotonic generation, so the slot being written must be the one
        with the *lower* one. If ``Z35`` ever named the running slot, this catches
        it before anything is erased.

        An unknown generation on either side is not treated as zero. Ordering a
        real generation against a fabricated zero is precisely how you conclude
        the live slot is the older one and overwrite working firmware, so an
        unknown simply skips the comparison.
        """
        live = other_slot(target.code)
        if live is None:
            return
        target_header = await self.read_container_header(target.address)
        live_header = await self.read_container_header(live.address)
        if target_header is None or live_header is None:
            return
        target_gen, live_gen = target_header.generation, live_header.generation
        if target_gen is None or live_gen is None:
            return
        if target_gen > live_gen:
            raise LumagenFirmwareAbortError(
                f"Z35 nominated the slot with the HIGHER generation "
                f"({target_gen:#06x} > {live_gen:#06x}), which looks like the one in "
                "use. Refusing to write. Nothing was erased."
            )

    async def _precommit_checkpoint(self, addr: int, image: bytes) -> None:
        """Verify the payload before writing the commit header.

        Reached with ``header_last`` once every block but block 0 is down. The
        slot's magic is still erased, so the device is still running the other
        slot: if anything wedges here, or the checksum is wrong, or the process is
        killed, the header never lands and the unit boots exactly as before. That
        makes this the one point in a section-1 write where issuing commands is
        free — and therefore where verification belongs.

        The tag lives at ``+4``, inside the withheld block 0, so this region
        carries no device-stamped bytes and its expected sum is exact — no tag
        arithmetic needed, unlike the post-commit check.
        """
        self._emit(
            UpdateProgress(
                phase=UpdatePhase.VERIFYING,
                message="pre-commit checkpoint (header still unwritten)",
                section=SECTION1,
            )
        )
        # The block delay covers block-to-block writes but not the switch from
        # bulk data to a command; a run once delivered every payload block with
        # more slack than the vendor uses, then failed the very first read here.
        await asyncio.sleep(CHECKPOINT_SETTLE)
        await self.drain_rx()

        payload_addr = addr + BLOCK_SIZE
        payload_len = len(image) - BLOCK_SIZE
        expected = additive_checksum(image[BLOCK_SIZE:])
        got: int | None = None
        for attempt in range(1, CHECKPOINT_TRIES + 1):
            got = await self.device_checksum(payload_addr, payload_len, timeout=90.0)
            if got is not None:
                break
            if attempt < CHECKPOINT_TRIES:
                await asyncio.sleep(CHECKPOINT_SETTLE)
                await self.drain_rx()
        if got is None:
            raise LumagenFirmwareAbortError(
                "the device did not answer the pre-commit checksum after "
                f"{CHECKPOINT_TRIES} attempts. The header was NOT written, so the "
                "slot has no container magic, cannot be elected at boot, and the "
                "unit still boots its previous firmware — power-cycle and it comes "
                "up normally. Nothing needs recovering."
            )
        if got != expected:
            raise LumagenFirmwareAbortError(
                f"pre-commit payload checksum {got:#010x}, expected {expected:#010x}. "
                "Refusing to write the commit header, so the slot stays uncommitted "
                "and the unit keeps booting its previous firmware. Retry the update."
            )
        self._emit(
            UpdateProgress(
                phase=UpdatePhase.COMMITTING,
                message="payload verified; writing commit header",
                section=SECTION1,
            )
        )

    async def _write_section0(self, section: PlannedSection, *, promote: bool) -> None:
        """Stage section 0 to scratch, verify, then have the device promote it.

        Everything up to the promotion is free: scratch is not live firmware, so a
        failure at any point before ``G39`` costs a retry and nothing else.
        """
        assert section.image is not None
        image = pad_to_even_end(SCRATCH_ADDR, section.image.wire_bytes)

        self._emit(
            UpdateProgress(
                phase=UpdatePhase.ERASING, message="erasing scratch region", section=SECTION0
            )
        )
        await self.erase_run(SCRATCH_SECTOR, sectors_for(len(image)))
        await self._stage_with_progress(image, SCRATCH_ADDR, SECTION0)

        self._emit(
            UpdateProgress(
                phase=UpdatePhase.VERIFYING, message="verifying staged image", section=SECTION0
            )
        )
        await asyncio.sleep(CHECKPOINT_SETTLE)
        await self.drain_rx()
        await self._verify_region(SCRATCH_ADDR, image, label="section 0 (scratch)")

        if not promote:
            self._stats.notes.append(
                "section 0 was staged to scratch and verified but NOT promoted; live "
                "firmware is unchanged."
            )
            return

        self._emit(
            UpdateProgress(
                phase=UpdatePhase.PROMOTING,
                message="copying scratch over live firmware",
                section=SECTION0,
            )
        )
        await self.promote(ADDR_LIVE, SCRATCH_ADDR, len(image))
        acked = await self.await_promote_ack()

        # Post-promotion reads are advisory, not a verdict. The unit powers itself
        # down as soon as the copy completes, so a *missing* reply here is
        # inconclusive rather than a failure — measured: G39 acked at t=11.32, the
        # whole-region checksum answered at t=11.73, and every read from t=12.07
        # returned nothing. Only a numeric mismatch proves the copy went wrong.
        # An earlier version folded these reads into the verdict and reported
        # "promotion did not verify" on byte-perfect firmware.
        got = await self.device_checksum(ADDR_LIVE, len(image), timeout=30.0)
        expected = additive_checksum(image)
        if got is not None and got != expected:
            raise LumagenFirmwareError(
                f"section 0: live firmware checksums {got:#010x} after promotion, "
                f"expected {expected:#010x}. Do NOT power-cycle — re-run the update."
            )
        if got is None:
            self._stats.notes.append(
                "promotion completed but the device stopped answering before it could "
                "be re-read, which is expected — it powers down as soon as the copy "
                f"finishes (G39 ack: {'yes' if acked else 'not seen'})."
            )

    async def _stage_with_progress(
        self,
        image: bytes,
        base: int,
        section: str,
        *,
        header_last: bool = False,
        before_header: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        total = len(image)
        self._emit(
            UpdateProgress(
                phase=UpdatePhase.WRITING,
                message=f"writing {total:,} bytes to {base:#08x}",
                section=section,
                bytes_total=total,
            )
        )

        # Under header_last the final block is block 0 — the container header, i.e.
        # the commit record itself. Reporting it as WRITING made the phase sequence
        # read WRITING -> VERIFYING -> COMMITTING -> WRITING, which looks like the
        # session left updater mode and re-entered it to write the header. It never
        # does: one M0931 at preflight, one X/Z97 at exit, everything else in
        # between. Label the commit block as COMMITTING so the output says so.
        committing = False

        async def hook() -> None:
            nonlocal committing
            if before_header is not None:
                await before_header()
            committing = True

        def on_block(done: int, size: int) -> None:
            self._emit(
                UpdateProgress(
                    phase=UpdatePhase.COMMITTING if committing else UpdatePhase.WRITING,
                    message=f"{100.0 * done / size:.1f}%",
                    section=section,
                    bytes_done=done,
                    bytes_total=size,
                )
            )

        await self.stage_image(
            image,
            base,
            header_last=header_last,
            before_header=hook if header_last else None,
            on_block=on_block,
        )

    # -- top level ---------------------------------------------------------

    async def run_update(
        self,
        bundle: FirmwareBundle,
        *,
        baudrate: int = 230400,
        dry_run: bool = False,
        promote: bool = True,
        force: bool = False,
        only: Iterable[str] | None = None,
        plan: UpdatePlan | None = None,
        progress: ProgressCallback | None = None,
    ) -> UpdateResult:
        """Preflight, plan, and write whatever the plan says needs writing.

        Assumes the session is already connected. Section 1 is written before
        section 0, matching the vendor: section 0 finishes with a promotion whose
        completion powers the unit down, so it has to go last.

        :param baudrate: rate to negotiate for the transfer. 230400 is the
            vendor's own fast setting and is what the barrier was qualified at.
        :param dry_run: plan and report, then leave without writing anything.
        :param promote: when False, section 0 is staged to scratch and verified but
            not copied over live firmware. Useful for exercising the whole path
            with nothing at stake.
        :param force: write selected sections without checking whether the device
            already holds them. See :func:`~aiolumagen.firmware.plan.plan_update`
            for the correctness gates this does *not* override.
        :param only: restrict to specific sections, e.g. ``["section1"]``.
        :param plan: a pre-built plan, which skips planning entirely. Mutually
            exclusive with `force` / `only`, since those are inputs to the
            planning this would replace.
        """
        if plan is not None and (force or only is not None):
            raise LumagenFirmwareError(
                "pass either a pre-built plan or force/only, not both — force and "
                "only are inputs to the planning that `plan` replaces, so honouring "
                "both is ambiguous on an operation this destructive."
            )

        self._progress = progress
        self._emit(UpdateProgress(phase=UpdatePhase.PREFLIGHT, message="checking device state"))
        await self.preflight()

        self._emit(UpdateProgress(phase=UpdatePhase.PLANNING, message="reading current firmware"))
        if plan is None:
            # Still read the device even when forcing: the status is what tells the
            # user (and the log) what was replaced, and read_status is read-only.
            status = await self.read_status()
            plan = plan_update(bundle, status, force=force, only=only)
        _LOGGER.info("Firmware update plan:\n%s", plan.describe())

        if dry_run or plan.is_empty:
            reason = "dry run" if dry_run else "already up to date"
            self._emit(UpdateProgress(phase=UpdatePhase.DONE, message=reason))
            return UpdateResult(
                plan=plan,
                dry_run=dry_run,
                flush_mode=self.flush_mode,
                notes=(*self._stats.notes, f"nothing written ({reason})"),
            )

        if baudrate != self.baudrate:
            self._emit(
                UpdateProgress(
                    phase=UpdatePhase.RATE_CHANGE, message=f"negotiating {baudrate} baud"
                )
            )
            await self.set_baud(baudrate)
            if not await self.rate_check():
                raise LumagenFirmwareAbortError(
                    f"the link is losing bytes at {baudrate} baud — two reads of the "
                    "same live-firmware address disagreed. Nothing has been erased. "
                    "Retry at a lower rate."
                )

        written: list[str] = []
        ordered = sorted(plan.to_write, key=lambda s: s.name != SECTION1)
        for section in ordered:
            if section.name == SECTION1:
                await self._write_section1(section)
            elif section.name == SECTION0:
                await self._write_section0(section, promote=promote)
            else:
                raise LumagenFirmwareImageError(
                    f"no write path for section {section.name!r}. Chip images are "
                    "deliberately out of scope."
                )
            written.append(section.name)

        self._emit(UpdateProgress(phase=UpdatePhase.DONE, message="update complete"))
        return UpdateResult(
            plan=plan,
            written=tuple(written),
            promoted=self.promoted,
            powered_down=self.requires_restart,
            flush_mode=self.flush_mode,
            flush_calls=self._stats.flush_calls,
            flush_retries=self._stats.flush_retries,
            notes=tuple(self._stats.notes),
        )
