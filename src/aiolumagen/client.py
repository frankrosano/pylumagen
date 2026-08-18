"""High-level Lumagen client.

Composes a :class:`~aiolumagen.transport.LumagenTransport` with a
:class:`~aiolumagen.protocol.LumagenProtocol` and exposes the commands a
typical consumer (the ``ha-lumagen`` HA integration, tests, scripts)
needs. Implements the startup handshake (``ZE2`` + initial status
queries) and runs a background poll loop so unsolicited reports aren't
the only way to stay in sync.

The startup handshake is built around two short retries (2 attempts at
1.5 s apart) rather than a long up-front wait: the RS-232 link is always
up as soon as the transport connects, so in steady-state operation the
first attempt succeeds and the retry only matters when we attach while
the bridge is still booting.

Most traffic here is fire-and-forget, matching the device: commands take no
acknowledgement and status arrives unsolicited. Where an answer is genuinely
required — the handshake's ``!S01`` gate and ``!I25`` support probe, and
per-input label discovery — :meth:`LumagenClient.query_and_wait` correlates
the reply to the query through a future keyed on the response code. See the
"Request/response correlation" section for why that's a narrow waiter rather
than a serialized command queue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from aiolumagen.commands import (
    ECHO_OFF_WITH_STATUS,
    Power,
    Query,
    fan_speed_command,
    game_mode_command,
    hdr_intensity_mapping_command,
    input_command,
    input_label_command,
    input_restart_command,
    osd_block_char_command,
    osd_clear_command,
    osd_message_command,
    reset_auto_aspect_command,
    save_config_command,
    sharpness_command,
    show_aspect_command,
    subtitle_shift_command,
)
from aiolumagen.exceptions import (
    LumagenCommandError,
    LumagenConnectionError,
    LumagenError,
)
from aiolumagen.protocol import LumagenProtocol
from aiolumagen.state import HdrGammaMode, LumagenState, SharpnessSensitivity

_LOGGER = logging.getLogger(__name__)


class _TransportLike(Protocol):
    """Minimal transport contract the client depends on.

    Concrete implementations: :class:`aiolumagen.transport.LumagenTransport`
    for real connections, and a lightweight in-memory fake in
    ``tests/conftest.py`` for unit tests. The contract is intentionally
    duck-typed — no ABC — because there's only ever one real transport.
    """

    @property
    def connected(self) -> bool: ...

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def write(self, data: bytes) -> None: ...


StateListener = Callable[[LumagenState, tuple[str, ...]], None]
"""Synchronous listener. Called inline from the protocol layer.

Listeners are called from the transport's data callback, so they must not
block or await. A consumer needing async work should schedule it itself —
HA's ``async_set_updated_data`` is the model: cheap, synchronous, and it
hands off to the event loop on its own terms.
"""


class LumagenClient:
    """Public entry point for talking to a Lumagen Radiance Pro.

    :param transport: Pre-constructed transport. Its data callback will be
        wired during :meth:`start`.
    :param power_poll_interval: Seconds between background power polls.
        ``None`` disables polling entirely (pure push mode).
    :param status_poll_interval: Seconds between ``ZQI25`` polls when the
        Lumagen reports power on. ``None`` disables.
    :param stale_timeout: Seconds without inbound bytes after which the
        client marks itself unavailable and forces a transport reconnect.
        Must be greater than the longest poll interval — see the
        constructor's ValueError below for details.
    """

    # Delay (seconds, after a control command) at which we re-poll the
    # Lumagen as a backstop. With "Full v5" reporting enabled (the
    # documented happy path — Tip0011, Report mode changes -> Full v5)
    # the device pushes !I25 on every state change INCLUDING power
    # transitions, so this tick is purely a safety net for users still
    # on Full v4 or older, where power on/off can lag a poll cycle.
    # Empty by default since the recommended setup doesn't need it; the
    # tick mechanism stays in place so future Lumagen quirks can re-arm
    # it without code changes.
    REFRESH_TICKS: tuple[float, ...] = ()

    FULL_STATUS_WAIT = 2.0
    """Seconds allowed for the ``!I25`` reply once the device is known to talk.

    The reply is a ~25-field line — roughly 100 ms of wire time at 9600 baud,
    plus device processing, plus (on an ESPHome bridge) a network hop and up
    to one firmware loop iteration. Generous on purpose: this only costs the
    full window when Full v5 genuinely doesn't answer, and being stingy here
    means the pre-v5 warning fires on healthy devices.
    """

    RESPONSE_TIMEOUT = 5.0
    """Default deadline for :meth:`query_and_wait` / :meth:`wait_for_response`."""

    LABEL_QUERY_TIMEOUT = 2.0
    """Per-input deadline for :meth:`query_input_labels`.

    A label reply is ~15 characters, about 15 ms of wire time at 9600 baud, so
    this is mostly slack for the ESPHome/serial path. It's a *deadline*, not a
    delay: a device that answers promptly (the normal case) moves straight to
    the next input, which is why label discovery is now bounded by the
    device's actual latency rather than 8 fixed sleeps.
    """

    def __init__(
        self,
        transport: _TransportLike,
        *,
        power_poll_interval: float | None = 60.0,
        status_poll_interval: float | None = 60.0,
        stale_timeout: float = 90.0,
    ) -> None:
        # Invariant: stale_timeout MUST be greater than the poll interval.
        # The poll loop checks staleness immediately after sending a query,
        # before the device's response can arrive — if the timeout is shorter
        # than one poll cycle, every cycle's elapsed-since-last-response will
        # exceed it and trigger a false-positive reconnect. (Bug observed in
        # 0.1.0 with stale_timeout=45s vs. 60s polls: warnings every 60s in
        # steady state.)
        poll_intervals = [
            iv for iv in (power_poll_interval, status_poll_interval) if iv is not None
        ]
        if poll_intervals and stale_timeout <= max(poll_intervals):
            raise ValueError(
                f"stale_timeout ({stale_timeout}s) must be greater than the "
                f"longest poll interval ({max(poll_intervals)}s); otherwise "
                f"every poll cycle will trip a false-positive reconnect."
            )
        self._transport = transport
        self._power_poll_interval = power_poll_interval
        self._status_poll_interval = status_poll_interval
        self._stale_timeout = stale_timeout
        self._protocol = LumagenProtocol(self._on_protocol_update)
        self._listeners: list[StateListener] = []
        self._poll_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._started = False
        self._last_response_time: float | None = None
        self._available = False
        # Response-code -> futures awaiting that code. Populated by
        # _register_waiter and drained by _on_response; see the
        # "Request/response correlation" section below.
        self._response_waiters: dict[str, list[asyncio.Future[str]]] = {}
        self._protocol.add_response_observer(self._on_response)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect the transport, run the startup handshake, begin polling.

        Safe to call once. Subsequent calls are no-ops until :meth:`stop`.
        """
        if self._started:
            return
        # Wrap the protocol's feed_bytes so we can track liveness at the
        # byte-stream level. The protocol layer suppresses callbacks when
        # state doesn't change (the publish_if_changed optimization), so we
        # can't rely on state-update callbacks alone — a steady-state
        # Lumagen would look unresponsive even while polls succeed.
        self._transport.set_data_callback(self._on_bytes_received)
        await self._transport.connect()
        self._started = True
        await self._send_startup_sequence()
        if self._power_poll_interval is not None or self._status_poll_interval is not None:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="aiolumagen-poll")

    async def stop(self) -> None:
        """Cancel polling and disconnect the transport. Idempotent."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        self._fail_waiters("Client stopped while awaiting a response")
        await self._transport.disconnect()
        self._started = False

    @property
    def state(self) -> LumagenState:
        """Latest accumulated state snapshot."""
        return self._protocol.state

    @property
    def connected(self) -> bool:
        return self._transport.connected

    @property
    def available(self) -> bool:
        """True when the Lumagen is actively responding.

        Becomes True on the first inbound bytes, reverts to False if no
        bytes have arrived within ``stale_timeout`` seconds (default 90s).
        The timeout must be longer than the longest poll interval so a
        normal poll cycle has time to write the query, await the response,
        and update ``_last_response_time`` before the next staleness check
        runs. With the default 60s polls, 90s gives one full cycle of slack
        plus another 30s for transient network delays. Tracks raw bytes
        rather than state changes so a steady-state Lumagen (polls
        succeeding but returning identical data) still counts as alive.
        """
        if not self._available:
            return False
        if self._last_response_time is None:
            return False
        elapsed = asyncio.get_event_loop().time() - self._last_response_time
        return elapsed < self._stale_timeout

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Register a sync listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(listener)

        return _unsubscribe

    # ------------------------------------------------------------------
    # Request/response correlation
    # ------------------------------------------------------------------
    #
    # The Lumagen link is not a request/response protocol: commands are
    # fire-and-forget and status arrives whenever the device feels like
    # sending it. That's the right default and it's what makes the push path
    # work. But a few operations genuinely need an answer before they can
    # proceed, and for those "send, then sleep and hope" is not good enough:
    #
    #   * Label discovery. The Lumagen has no bulk label query and each
    #     !S1x reply names only the memory letter, not the input — so the
    #     queries must be serialized and each reply attributed to the input
    #     that was just asked about. A fixed sleep either wastes time or
    #     misattributes a slow reply to the *next* input.
    #   * The startup handshake, which gates its retry on !S01 arriving and
    #     its Full-v5 warning on !I25 arriving with a non-empty payload.
    #   * Consumers validating a connection (ha-lumagen's config flow) that
    #     want "did this device answer?" as a value, not a state poll.
    #
    # The mechanism is deliberately narrow: a dict of futures keyed by
    # response code, fed by a protocol-layer observer. It is NOT a serialized
    # command queue — commands still go out immediately, and the unsolicited
    # push stream is never blocked behind an in-flight transaction.

    @staticmethod
    def _normalize_code(code: str) -> str:
        """Accept either ``"!S01"`` or ``"S01"`` and return the bare code."""
        return code[1:] if code.startswith("!") else code

    @staticmethod
    def _infer_response_code(command: str) -> str:
        """Derive the response code a query will be answered with.

        Only the plain 5-character ``ZQxxx`` form is inferable (``ZQS01`` ->
        ``S01``). Anything else — notably the label query ``ZQS1A0``, whose
        reply is ``!S1A`` and drops the input digit — must say so explicitly
        via ``expect=``, because guessing there would silently wait on a code
        the device will never send.
        """
        if len(command) == 5 and command.startswith("ZQ"):
            return command[2:]
        raise LumagenCommandError(
            f"cannot infer the response code for {command!r}; pass expect='<code>' explicitly"
        )

    def _register_waiter(self, code: str) -> asyncio.Future[str]:
        """Create a future that resolves with the next ``code`` payload."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._response_waiters.setdefault(code, []).append(future)
        return future

    def _discard_waiter(self, code: str, future: asyncio.Future[str]) -> None:
        """Remove a waiter, whether it resolved, timed out, or was abandoned."""
        waiters = self._response_waiters.get(code)
        if waiters is None:
            return
        with suppress(ValueError):
            waiters.remove(future)
        if not waiters:
            del self._response_waiters[code]

    def _on_response(self, code: str, payload: str) -> None:
        """Protocol observer: resolve every waiter registered for ``code``.

        All waiters on a code are resolved, not just the first — broadcasting
        avoids an arbitrary "who gets this reply" rule when two callers happen
        to await the same code, and each gets the identical payload anyway.
        """
        waiters = self._response_waiters.pop(code, None)
        if not waiters:
            return
        for future in waiters:
            if not future.done():
                future.set_result(payload)

    def _fail_waiters(self, reason: str) -> None:
        """Fail all pending waiters — the reply they're waiting for isn't coming.

        Called on :meth:`stop` and before a forced reconnect. Uses an
        exception rather than cancellation so the failure arrives at the
        caller as a normal, catchable
        :class:`~aiolumagen.exceptions.LumagenConnectionError` instead of a
        ``CancelledError`` that would tear through any surrounding task.
        """
        waiters = self._response_waiters
        self._response_waiters = {}
        for futures in waiters.values():
            for future in futures:
                if not future.done():
                    future.set_exception(LumagenConnectionError(reason))

    async def wait_for_response(self, code: str, *, timeout: float | None = None) -> str:
        """Wait for the next response with ``code`` and return its payload.

        For *unsolicited* responses — use :meth:`query_and_wait` when you're
        also sending the query, since that registers the waiter before the
        write and so can't miss a reply that arrives immediately.

        :param code: Response code, with or without the leading ``!``
            (``"S01"`` and ``"!S01"`` are equivalent).
        :param timeout: Seconds to wait; defaults to :attr:`RESPONSE_TIMEOUT`.
        :raises TimeoutError: the builtin, if nothing arrives in time. This
            library deliberately has no timeout exception of its own — see
            :mod:`aiolumagen.exceptions` — so consumers already catching
            ``TimeoutError`` around ``asyncio.timeout`` need no changes.
        :raises LumagenConnectionError: if the client stops or reconnects
            while waiting.
        """
        normalized = self._normalize_code(code)
        limit = self.RESPONSE_TIMEOUT if timeout is None else timeout
        future = self._register_waiter(normalized)
        try:
            async with asyncio.timeout(limit):
                return await future
        finally:
            self._discard_waiter(normalized, future)

    async def query_and_wait(
        self,
        command: str,
        *,
        expect: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Send ``command`` and return the payload of its matching response.

        The waiter is registered *before* the write, so a device that answers
        within the same event-loop tick (or a transport that echoes
        synchronously, as the test fake does) can't be missed.

        :param command: The query to send, e.g. ``"ZQS01"``.
        :param expect: Response code to wait for. Inferred from the plain
            ``ZQxxx`` form when omitted; required otherwise (see
            :meth:`_infer_response_code`).
        :param timeout: Seconds to wait; defaults to :attr:`RESPONSE_TIMEOUT`.
        :returns: The response payload — the text after the code, ``""`` for
            an empty one. **An empty string is a meaningful result**: the
            Lumagen answers any syntactically valid ``ZQ`` code by echoing it
            with no payload, so ``""`` means "the device replied but doesn't
            support this query", not "no reply".
        :raises TimeoutError: the builtin, if no matching response arrives.
        :raises LumagenConnectionError: on a transport failure, or if the
            client stops or reconnects while waiting.
        :raises LumagenCommandError: if ``expect`` is omitted for a command
            whose response code can't be inferred.
        """
        code = (
            self._normalize_code(expect)
            if expect is not None
            else self._infer_response_code(command)
        )
        limit = self.RESPONSE_TIMEOUT if timeout is None else timeout
        future = self._register_waiter(code)
        try:
            await self.send_command(command, refresh=False)
            async with asyncio.timeout(limit):
                return await future
        finally:
            self._discard_waiter(code, future)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send_command(self, cmd: str, *, cr: bool = False, refresh: bool = True) -> None:
        """Send a raw command string.

        :param cmd: The command characters (no terminator; one is appended
            if ``cr=True``).
        :param cr: Append a carriage return. Only a handful of Lumagen
            commands need this — consult the RS-232 doc before setting it.
        :param refresh: If True (default), call :meth:`request_refresh`
            after writing the command. With Full v5 enabled (the
            documented setup) this is a cheap no-op because
            :attr:`REFRESH_TICKS` is empty by default — the device pushes
            !I25 on every state change including power. Older firmwares
            without v5 can override REFRESH_TICKS to e.g. ``(5.0,)`` to
            re-arm the post-command refresh; see the class attribute's
            docstring for context. Suppressed automatically for query
            commands (anything starting with ``Z``) so we don't recurse,
            and when no poll loop is running. Pass ``refresh=False`` to
            unconditionally suppress.
        """
        if not self._started:
            raise LumagenError("LumagenClient.send_command called before start()")
        payload = cmd.encode("ascii")
        if cr:
            payload += b"\r"
        _LOGGER.debug("TX: %s%s", cmd, "<CR>" if cr else "")
        await self._transport.write(payload)
        # Auto-refresh after control commands. The Z-prefix check skips
        # query commands (ZQS00, ZQS01, ZQS02, ZQI00, ZQI25, ZE2) so this
        # method calling itself indirectly through query_*() doesn't
        # recurse. We also skip when no poll loop is running — in that
        # configuration the caller has explicitly opted out of background
        # polling and presumably doesn't want auto-refresh either.
        if refresh and not cmd.startswith("Z") and self._poll_task is not None:
            self.request_refresh()

    async def power_on(self) -> None:
        await self.send_command(Power.ON)

    async def standby(self) -> None:
        await self.send_command(Power.STANDBY)

    async def set_input(self, n: int) -> None:
        """Select input ``n`` (1-19)."""
        await self.send_command(input_command(n))

    async def query_device_info(self, *, timeout: float | None = None) -> str:
        """Send ``ZQS01`` and return the ``!S01`` payload once it arrives.

        The one query in this family that *awaits* its answer, because it's the
        one callers gate on: "did a Lumagen actually reply on this port?" is the
        question a connection check asks, and polling
        :attr:`state.model <aiolumagen.state.LumagenState.model>` in a loop to
        answer it is a busy-wait that this exists to remove. Everything else
        (:meth:`query_power`, :meth:`query_full_status`, the secondary queries)
        is fire-and-forget, because their values feed state asynchronously and
        nothing blocks on arrival.

        Keeping the ``ZQS01`` literal on this side of the boundary is the other
        reason this method exists rather than leaving callers to reach for
        :meth:`query_and_wait` — a consumer doing that would have to hardcode
        the wire code, which is exactly what ``ha-lumagen`` must not do.

        :param timeout: Seconds to wait; defaults to :attr:`RESPONSE_TIMEOUT`.
        :returns: The ``!S01`` payload — ``model,firmware,model_number,serial``.
            Parsed fields land on :attr:`state` as usual; the raw return is for
            callers that just want proof of life.
        :raises TimeoutError: the builtin, if the device doesn't answer.
        :raises LumagenConnectionError: on a transport failure.
        """
        return await self.query_and_wait(Query.DEVICE_INFO.value, timeout=timeout)

    async def query_power(self) -> None:
        await self.send_command(Query.POWER.value)

    async def query_input_info(self) -> None:
        await self.send_command(Query.INPUT_INFO.value)

    async def query_full_status(self) -> None:
        """Send ``ZQI25`` (Full v5) — the only status poll this library issues.

        Full v5 is the supported floor. A firmware old enough not to know
        ``ZQI25`` answers with an empty payload (see the note in
        :mod:`aiolumagen.protocol` — a response prefix does not imply
        support), so on such a device every status field would simply stay
        ``None``; :meth:`_send_startup_sequence` logs a warning naming the
        requirement rather than letting that look like a wiring fault.

        The ``!I21``-``!I24`` parser branches are deliberately kept: they
        cost nothing and a device whose *reporting* menu is still set to
        Full v4 pushes ``!I24`` unsolicited even though we poll v5. Only
        the v4 *query* was removed.
        """
        await self.send_command(Query.FULL_STATUS_V5.value)

    async def query_sharpness(self) -> None:
        """Send ``ZQI30`` and update :attr:`state.sharpness_*`."""
        await self.send_command(Query.SHARPNESS.value)

    async def query_game_mode(self) -> None:
        """Send ``ZQI53`` and update :attr:`state.game_mode`."""
        await self.send_command(Query.GAME_MODE.value)

    async def query_auto_aspect(self) -> None:
        """Send ``ZQI54`` and update :attr:`state.auto_aspect`."""
        await self.send_command(Query.AUTO_ASPECT.value)

    async def query_display_rec2020(self) -> None:
        """Send ``ZQI50`` and update :attr:`state.display_supports_rec2020`."""
        await self.send_command(Query.DISPLAY_REC2020.value)

    async def query_source_hdr_status(self) -> None:
        """Send ``ZQI52`` and update :attr:`state.hdr_*` mastering fields.

        For SDR sources the Lumagen returns placeholder zeros; the parser
        leaves the structured fields at ``None`` rather than exposing
        misleading "0 nits" readings to consumers.
        """
        await self.send_command(Query.SOURCE_HDR_STATUS.value)

    async def query_output_mode(self) -> None:
        """Send ``ZQO01`` and update the output geometry fields.

        The only query that reports output width outright, landing in
        :attr:`state.output_width_reported` and from there
        :attr:`state.output_width`. Without it, width is inferred from height
        and the output aspect, which is wrong whenever the output is scaled —
        an anamorphic setup reports 5119 for a 4096-wide raster.

        ``!O01`` does not ride the ``!I25`` push stream, so this has to be
        polled; it runs from :meth:`_query_secondary_status`.
        """
        await self.send_command(Query.OUTPUT_MODE.value)

    async def _query_secondary_status(self) -> None:
        """Query the state the Full v5 push stream doesn't carry.

        Sharpness (``!I30``), game mode (``!I53``), display Rec.2020 support
        (``!I50``), source HDR mastering metadata (``!I52``) and output mode
        (``!O01``) are only emitted in response to their explicit ``ZQ``
        queries — none of them ride in the ``!I25`` status push.

        ``!O01`` is here because it carries the only authoritative output width;
        the status push offers height and an aspect code, from which width can
        only be inferred, and that inference breaks on a scaled output.

        **Auto aspect is the exception, and is no longer polled unconditionally.**
        Payload index 26 of the ``!I25`` push carries it, the device pushes on
        every auto-aspect change, and the mapping is confirmed on hardware — so
        ``ZQI54`` is redundant and slower. It is kept only as a fallback for
        firmware that stops the payload before index 26 (the recorded ``030225``
        capture ends at 24), detected by
        :attr:`~aiolumagen.state.LumagenState.auto_aspect_status` still being
        ``None`` after the handshake has already issued ``ZQI25``. On current
        firmware that check is false forever after the first status line, so the
        query never goes out.

        One partial exception, which is why this list still includes auto
        aspect: a *tri-state* auto-aspect field appears to ride the push at
        payload 26, surfacing as
        :attr:`~aiolumagen.state.LumagenState.auto_aspect_status`. That index
        is empirically mapped and absent on firmware ``030225``, so ``ZQI54``
        remains the authoritative source for the
        :attr:`~aiolumagen.state.LumagenState.auto_aspect` boolean and is
        still polled here. Don't drop it on the strength of the push until the
        index is confirmed on hardware. If
        we never issue these, ``state.sharpness_*`` / ``game_mode`` /
        ``auto_aspect`` / ``display_supports_rec2020`` / ``hdr_*`` stay
        ``None`` forever and their HA entities read "unknown" — and any
        compound write that tries to *preserve* an unread field (e.g.
        ``set_sharpness`` keeping enabled+level while changing sensitivity)
        silently falls back to defaults.

        Run at startup and on each powered-on status poll so values changed
        from the front-panel remote stay in sync. Connection errors are
        swallowed at debug level: a blip on a secondary query must not abort
        startup or a poll cycle.
        """
        try:
            await self.query_sharpness()
            await self.query_game_mode()
            if self.state.auto_aspect_status is None:
                # Firmware too old to carry index 26 — fall back to the query.
                await self.query_auto_aspect()
            await self.query_display_rec2020()
            await self.query_source_hdr_status()
            await self.query_output_mode()
        except LumagenConnectionError as err:
            _LOGGER.debug("Secondary status query failed (transport down): %s", err)

    async def query_input_labels(self, memory: str = "A", *, timeout: float | None = None) -> None:
        """Query configured labels for inputs 1-8 into :attr:`state.input_labels`.

        The Lumagen has no bulk label query, and each per-label response
        (``!S1x,<label>``) reports only the memory letter — not the input
        number. So this serializes: it primes the parser with the input it's
        about to ask for (:meth:`LumagenProtocol.expect_input_label`), sends
        the ``ZQS1XY`` query, and **awaits the actual reply** before moving
        on.

        That await replaced a fixed 0.15 s sleep per input. Two things were
        wrong with the sleep: it spent 1.2 s unconditionally even when the
        device answered in 20 ms, and a reply slower than the window landed
        while the *next* input was already primed — silently attributing one
        input's label to another. A deadline can only ever drop a label, never
        misfile one, and the primer is cleared on timeout to guarantee that.

        :param memory: Input memory to read labels from, ``A``-``D``. Labels
            are stored per (input, memory); ``A`` is the default/primary bank.
        :param timeout: Per-input deadline; defaults to
            :attr:`LABEL_QUERY_TIMEOUT`.

        Inputs the device doesn't answer for (unpopulated inputs, or firmware
        without ``ZQS1`` support) are simply left unset — a missing response
        never raises. An invalid ``memory`` raises
        :class:`~aiolumagen.exceptions.LumagenCommandError`, and a transport
        failure propagates as
        :class:`~aiolumagen.exceptions.LumagenConnectionError` (there's no
        point continuing the loop once the link is down).
        """
        # Note: this is NOT the same vocabulary as the Memory enum. Memory
        # holds the lowercase memory-*recall* commands (``a``-``d``); the
        # label query wants an uppercase letter inside ZQS1XY. Keep them
        # separate — coercing through Memory here would send the wrong byte.
        memory = memory.upper()
        if memory not in ("A", "B", "C", "D"):
            raise LumagenCommandError(f"input-label memory must be 'A'-'D', got {memory!r}")
        limit = self.LABEL_QUERY_TIMEOUT if timeout is None else timeout
        for input_number in range(1, 9):
            await self._read_input_label(input_number, memory, limit)

    async def _read_input_label(self, input_number: int, memory: str, timeout: float) -> bool:
        """Query one input's label and wait for it. Returns whether it landed.

        Shared by :meth:`query_input_labels` and :meth:`set_input_label` so the
        prime/send/await/clear-on-timeout sequence exists once. Getting that
        sequence wrong is how a label ends up filed under the wrong input, so
        it should not be written twice.
        """
        self._protocol.expect_input_label(input_number)
        # ZQS1XY: X = memory letter, Y = input - 1 (so '0' for input 1). The
        # reply is !S1X — the input digit is absent from it, which is exactly
        # why expect= can't be inferred from the command here.
        try:
            await self.query_and_wait(
                f"ZQS1{memory}{input_number - 1}",
                expect=f"S1{memory}",
                timeout=timeout,
            )
        except TimeoutError:
            # Clear the primer so a late reply can't be misattributed to
            # whichever input is asked about next.
            self._protocol.expect_input_label(None)
            _LOGGER.debug("No label response for input %d (memory %s)", input_number, memory)
            return False
        return True

    async def set_input_label(self, input_number: int, label: str, *, memory: str = "A") -> None:
        """Write an input's label via ``ZY524``, then read it back.

        The read-back matters more here than for other setters: the device
        silently truncates or rejects a label it won't store, so writing
        without confirming leaves
        :attr:`state.input_labels <aiolumagen.state.LumagenState.input_labels>`
        showing what was *requested* rather than what the Lumagen kept.

        :param input_number: Logical input 1-8.
        :param label: Up to 10 renderable characters.
        :param memory: ``"A"``-``"D"``, or ``"ALL"`` to write every bank.

        With ``memory="ALL"`` the read-back reads bank ``A``, since that's the
        bank :meth:`query_input_labels` tracks by default and all four were
        just written to the same value.

        :raises LumagenCommandError: for an invalid input, memory, or label —
            raised by the command builder before anything is sent.
        """
        await self.send_command(
            input_label_command(input_number, label, memory=memory),
            cr=True,
            refresh=False,
        )
        bank = memory.upper()
        read_bank = "A" if bank in ("ALL", "0") else bank
        await self._read_input_label(input_number, read_bank, self.LABEL_QUERY_TIMEOUT)

    async def restart_input(self, input_number: int | str = "all") -> None:
        """Pulse HDMI hotplug on an input via ``ZY520`` so the source renegotiates.

        The documented fix for a source stuck at the wrong resolution, missing
        an audio format, or showing nothing after a change further down the
        chain — a cable reseat without the cable.

        :param input_number: Logical input 1-8, or ``"all"``.

        Expect a brief signal dropout on the affected input while the source
        re-reads EDID; that's the mechanism working, not a fault.
        """
        await self.send_command(input_restart_command(input_number), cr=True, refresh=False)

    async def show_aspect(self) -> None:
        """``ZY811`` — pop the current input and aspect onto the OSD.

        Reverse-engineered rather than documented in Tip0011. Fails soft: an
        unrecognised ``ZY`` command is ignored, so on firmware without it
        nothing appears and nothing breaks.
        """
        await self.send_command(show_aspect_command(), cr=True, refresh=False)

    async def save_config(self) -> None:
        """``ZY6SAVECONFIG`` — commit the running configuration to flash.

        One shot, where :attr:`~aiolumagen.commands.Misc.SAVE` mirrors the
        remote's SAVE key and needs a follow-up ``OK``. That makes this the
        right choice for automation: a two-keystroke save whose confirmation
        never arrives leaves a prompt on screen.

        Tip0011 asks that any on-screen test pattern be exited first, so the
        pattern isn't captured as configuration. Nothing here enforces that —
        the library can't know a pattern is up, since the device doesn't report
        one.
        """
        await self.send_command(save_config_command(), cr=True, refresh=False)

    # ------------------------------------------------------------------
    # On-screen display messages
    # ------------------------------------------------------------------

    async def show_message(
        self,
        text: str | None = None,
        *,
        line1: str | None = None,
        line2: str | None = None,
        duration: int = 3,
        center: bool = False,
    ) -> None:
        """Print a message on the Lumagen's OSD via ``ZT``.

        Two rows of 30 characters. Pass ``text`` to have it wrapped across
        them, or ``line1``/``line2`` to place rows verbatim — see
        :func:`~aiolumagen.commands.osd_message_command` for the field layout,
        the character range, and how truncation is signalled.

        :param text: Message to wrap.
        :param line1: Explicit first row.
        :param line2: Explicit second row.
        :param duration: ``0``-``9``; ``9`` persists until
            :meth:`clear_message`.
        :param center: Centre each row.

        Nothing is echoed back for this, and the device reports no OSD state,
        so there's no way to confirm a message is on screen — treat it as
        fire-and-forget.

        :raises LumagenCommandError: on a bad duration, on passing both
            ``text`` and explicit rows, or when the message is empty after
            unrenderable characters are removed.
        """
        await self.send_command(
            osd_message_command(
                text=text,
                line1=line1,
                line2=line2,
                duration=duration,
                center=center,
            ),
            cr=True,
            refresh=False,
        )

    async def clear_message(self) -> None:
        """``ZC`` — clear any on-screen message.

        Needed for a message sent with ``duration=9``, which otherwise stays up
        indefinitely. Harmless when nothing is displayed.
        """
        await self.send_command(osd_clear_command(), refresh=False)

    async def set_osd_block_char(self, char: str) -> None:
        """``ZB`` — nominate a character to render as a solid block.

        Repeat that character in a message to draw a bar (volume, progress).

        Global and sticky: every later message renders the nominated character
        as a block too, so pick one that won't appear in ordinary text. The
        device reports no OSD configuration, so this can't be read back — a
        consumer that cares must remember what it set.

        :raises LumagenCommandError: unless ``char`` is one renderable
            character.
        """
        await self.send_command(osd_block_char_command(char), refresh=False)

    async def set_sharpness(
        self,
        *,
        enabled: bool,
        level: int,
        sensitivity: SharpnessSensitivity = SharpnessSensitivity.NORMAL,
    ) -> None:
        """Set sharpness via ``ZY521ELS`` and re-query so state catches up.

        Sharpness is not part of the Full v5 push stream, so a follow-up
        ``ZQI30`` is needed to refresh ``state.sharpness_*`` after the
        write. We issue both back-to-back.
        """
        await self.send_command(
            sharpness_command(
                enabled=enabled,
                level=level,
                sensitivity=sensitivity,
            ),
            cr=True,
            refresh=False,
        )
        await self.query_sharpness()

    async def set_game_mode(self, enabled: bool) -> None:
        """Set game mode via ``ZY551X`` and re-query ``ZQI53``."""
        await self.send_command(game_mode_command(enabled), cr=True, refresh=False)
        await self.query_game_mode()

    async def set_fan_speed(self, speed: int) -> None:
        """Set minimum fan speed via ``ZY552X`` (1-10; higher = faster).

        ``speed`` is in the device's own units — the number the Lumagen
        shows in its menu. The wire digit is one lower; see
        :func:`~aiolumagen.commands.fan_speed_command` for the evidence.

        Reverse-engineered from the firmware (see
        ``lumagen-research/FIRMWARE_REVERSE_ENGINEERING_FINDINGS.md``); not
        documented in the Tip0011 PDF.

        **Write-only, permanently.** There is no fan-speed query, and this
        is settled rather than merely undocumented: it's absent from
        Tip0011 and from the firmware-strings command table (the same
        extraction that *did* surface this setter and the undocumented
        ``ZQI54``), and probing ``ZQI55``-``ZQI57`` plus ``ZQS05``-``ZQS07``
        on a 4242 returned empty payloads — which, per the note in
        :mod:`aiolumagen.protocol`, is indistinguishable from a nonexistent
        code. So ``state`` will never reflect fan speed and consumers must
        track it optimistically. Don't re-probe for this.
        """
        await self.send_command(fan_speed_command(speed), cr=True, refresh=False)

    async def set_subtitle_shift(self, level: int) -> None:
        """Set subtitle shifting via ``ZY553X`` (0/1/2).

        Reverse-engineered from the firmware; not documented in the
        bundled Tip0011 PDF.

        No query exists for this setting, but that isn't the same as no
        feedback: the value appears to ride the Full v5 status push at payload
        25, surfacing as
        :attr:`~aiolumagen.state.LumagenState.subtitle_shift`. That index is
        empirically mapped and absent on firmware ``030225``, so treat the
        field as a bonus rather than a guarantee — a consumer still needs to
        track what it last wrote for the firmwares that stay quiet.
        """
        await self.send_command(subtitle_shift_command(level), cr=True, refresh=False)

    async def reset_auto_aspect(self) -> None:
        """``ZY550`` — reset and reinitiate automatic aspect detection.

        Re-queries ``ZQI54`` after to pick up any state change.
        """
        await self.send_command(reset_auto_aspect_command(), cr=True, refresh=False)
        await self.query_auto_aspect()

    async def set_hdr_intensity_mapping(
        self, *, display_max_nits: int, gamma_mode: HdrGammaMode = HdrGammaMode.AUTO
    ) -> None:
        """Set the active CMS's HDR mapping target via ``ZY417XXXXXG``.

        :param display_max_nits: 0 to disable HDR mapping, or 50-10000 to
            set the display's peak luminance (the target the Lumagen tone-
            maps toward).
        :param gamma_mode: :class:`~aiolumagen.state.HdrGammaMode` —
            ``AUTO`` (recommended), ``HDR`` (force HDR gamma), or ``SDR``
            (force SDR gamma).

        There's no documented query that returns the active mapping
        values, so this is fire-and-forget — the integration tracks the
        last-set values optimistically. The setting is per-CMS and
        persists until the next ``ZY417`` for the same CMS.
        """
        await self.send_command(
            hdr_intensity_mapping_command(
                display_max_nits=display_max_nits,
                gamma_mode=gamma_mode,
            ),
            cr=True,
            refresh=False,
        )

    def request_refresh(self) -> None:
        """Schedule follow-up status queries on the :attr:`REFRESH_TICKS` schedule.

        With Full v5 reporting enabled (the documented happy path), this
        is effectively a no-op: ``REFRESH_TICKS`` defaults to ``()`` and
        every state change — including power — pushes via ``!I25`` in
        real time. The mechanism stays in place as a safety net for
        firmwares without v5 (where ``REFRESH_TICKS`` can be set to e.g.
        ``(5.0,)`` to catch power transitions) and for future Lumagen
        quirks that may want it.

        When ticks are configured, multiple calls coalesce: an in-flight
        schedule is cancelled and restarted, so a burst of commands
        produces one window anchored on the most recent press.

        Called automatically by :meth:`send_command` for non-query
        commands when a poll loop is running.
        """
        if not self._started:
            return
        if not self.REFRESH_TICKS:
            # Empty schedule — nothing to do. Skip the task spawn entirely
            # so we don't churn through cancel/spawn cycles on every press.
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(
            self._refresh_after_command(), name="aiolumagen-refresh"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send_startup_sequence(self) -> None:
        """ZE2 echo-off, then initial queries with retry.

        Two attempts at 1.5 seconds apart is plenty of headroom — once
        serialx reports the transport connected the link is just a byte
        pipe, so the first attempt almost always succeeds. The retry only
        matters when we attach while the ESPHome bridge is still coming up,
        which is rare in practice because ha-lumagen's coordinator setup
        waits for the esphome integration first.
        """
        max_retries = 2
        retry_interval = 1.5  # per-attempt deadline for the !S01 reply

        for attempt in range(max_retries):
            try:
                await self.send_command(ECHO_OFF_WITH_STATUS)
                await asyncio.sleep(0.3)
                # Await !S01 instead of firing every query blind and then
                # polling state: the reply both proves the device is
                # listening and gates the retry. Nothing else is sent until
                # it lands, so a dead link costs one command, not five.
                await self.query_device_info(timeout=retry_interval)
            except LumagenConnectionError:
                _LOGGER.warning("Lumagen startup queries aborted - transport disconnected")
                return
            except TimeoutError:
                if attempt < max_retries - 1:
                    _LOGGER.debug(
                        "Lumagen startup attempt %d/%d got no response, retrying",
                        attempt + 1,
                        max_retries,
                    )
                continue

            _LOGGER.debug("Lumagen startup handshake succeeded on attempt %d", attempt + 1)
            try:
                # Power and input info also ride the !I25 push, so these stay
                # fire-and-forget — awaiting them would add two more deadlines
                # to startup for data that arrives anyway.
                await self.query_power()
                await asyncio.sleep(0.3)
                await self.query_input_info()
                await asyncio.sleep(0.3)
                # An empty payload here is the real signal for pre-v5
                # firmware: the device answers any valid ZQ code by echoing
                # it with nothing after the comma. Correlation gives us that
                # payload directly, so the check is now on the response
                # itself rather than inferred from state truthiness.
                status = await self.query_and_wait(
                    Query.FULL_STATUS_V5.value, timeout=self.FULL_STATUS_WAIT
                )
            except LumagenConnectionError:
                _LOGGER.warning("Lumagen startup queries aborted - transport disconnected")
                return
            except TimeoutError:
                status = ""
            # The Full v5 push doesn't carry sharpness / game mode / auto
            # aspect / HDR-mapping state — pull those once now so entities
            # don't sit at "unknown" until the first poll.
            await self._query_secondary_status()
            if not status:
                self._warn_no_full_status()
            return

        _LOGGER.warning(
            "Lumagen startup handshake: no response after %d attempts "
            "(%.0fs total). The poll loop will continue trying.",
            max_retries,
            max_retries * retry_interval,
        )

    def _warn_no_full_status(self) -> None:
        """Warn that the device identified itself but never reported status.

        Full v5 is this library's supported floor and a firmware predating it
        doesn't fail loudly — per the note in :mod:`aiolumagen.protocol`, any
        syntactically valid ``ZQ`` code is answered by echoing the code with
        an **empty** payload. So the signal is an absent *or blank* status
        payload, not merely a missing line; the caller's predicate tests
        truthiness rather than ``is not None`` for exactly that reason.

        Only ever a log line — never promote this to an exception. Reaching
        here means ``!S01`` arrived, so the link is healthy and the device is
        talking; the integration still works, it just can't populate the
        signal-path sensors.
        """
        state = self._protocol.state
        if state.power_on is False:
            # A Lumagen in standby has no signal to report, so silence here
            # says nothing about firmware support. ZQS02 ran earlier in the
            # handshake, so power_on is known by now.
            _LOGGER.debug(
                "No status payload during startup, but the Lumagen reports "
                "standby — not treating that as missing Full v5 support"
            )
            return
        _LOGGER.warning(
            "Lumagen %s answered ZQS01 but returned no Full v5 status within "
            "%.0fs of ZQI25. This library requires firmware with Full v5 "
            "support; without it the signal sensors (resolution, aspect, "
            "colorspace, HDR) stay unknown. Updating the Lumagen's firmware "
            "is the fix.",
            state.model,
            self.FULL_STATUS_WAIT,
        )

    async def _refresh_after_command(self) -> None:
        """Fire follow-up queries at each REFRESH_TICK delay.

        Runs as a background task spawned by :meth:`request_refresh`. Each
        tick is an absolute delay from t=0 (when the task started); the
        loop sleeps the diff between the previous tick and the next. Each
        tick sends ``ZQS02`` (power) + ``ZQI25`` (full v5 status); these
        cover every state field the buttons / selects can change. Errors
        are logged at debug level but don't abort the schedule — a
        transient transport blip on one tick shouldn't suppress remaining
        ticks.
        """
        previous = 0.0
        for tick in self.REFRESH_TICKS:
            delay = max(0.0, tick - previous)
            previous = tick
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self.query_power()
                await self.query_full_status()
            except LumagenConnectionError as err:
                _LOGGER.debug("Refresh tick failed (transport down): %s", err)

    async def _poll_loop(self) -> None:
        """Poll at the shorter of the two intervals; gate ZQI25 on power.

        Also checks for staleness. If no response arrives within
        ``stale_timeout`` we:
          1. Mark the client as unavailable and notify listeners so the
             coordinator flags entities unavailable in HA.
          2. Force a full transport disconnect + reconnect cycle.

        The reconnect exists because a serial_proxy subscription can go
        half-open: writes still succeed and the ESP's UART keeps working,
        but inbound bytes stop reaching the existing subscriber, and the
        only recovery is a fresh subscription. Reconnecting the transport
        is functionally identical to reloading the integration from the
        ESP's perspective, without the entity churn.

        Caveat on the evidence: this was diagnosed on a USB/FTDI bridge,
        where a cable hot-plug reliably reproduced it. That is once again
        the live path — ``esphome-lumagen`` moved to RS-232 and then back
        to USB host driving the Lumagen's own FT232R — so the trigger is
        realistic rather than hypothetical, though it has not been
        re-confirmed since the switch back. The recovery is cheap and
        idempotent, so it stays.
        """
        p_iv = self._power_poll_interval
        s_iv = self._status_poll_interval
        base = min(v for v in (p_iv, s_iv) if v is not None)
        p_due = asyncio.get_running_loop().time() + (p_iv or 0)
        s_due = asyncio.get_running_loop().time() + (s_iv or 0)
        try:
            while True:
                await asyncio.sleep(base)
                now = asyncio.get_running_loop().time()
                try:
                    if p_iv is not None and now >= p_due:
                        await self.query_power()
                        p_due = now + p_iv
                    if s_iv is not None and now >= s_due and self.state.power_on is True:
                        await self.query_full_status()
                        # Keep the non-pushed fields (sharpness, game mode,
                        # auto aspect, HDR mapping) in sync with front-panel
                        # remote changes.
                        await self._query_secondary_status()
                        s_due = now + s_iv
                except LumagenConnectionError as err:
                    _LOGGER.warning("Lumagen poll failed: %s", err)

                # Staleness check: if we were available but haven't heard
                # back in stale_timeout seconds, mark unavailable, notify,
                # and force a fresh subscription via transport reconnect.
                if self._available and not self.available:
                    self._available = False
                    _LOGGER.warning(
                        "Lumagen is now unavailable (no response in %.0fs), "
                        "forcing transport reconnect",
                        self._stale_timeout,
                    )
                    for listener in list(self._listeners):
                        with suppress(Exception):
                            listener(self._protocol.state, ("_unavailable",))
                    # Anything still awaiting a reply is waiting on a
                    # subscription we're about to throw away.
                    self._fail_waiters("Transport reconnecting; response will not arrive")
                    try:
                        await self._transport.disconnect()
                        await asyncio.sleep(1.0)
                        await self._transport.connect()
                        self._protocol.reset()
                        await self._send_startup_sequence()
                    except LumagenConnectionError as err:
                        _LOGGER.warning("Reconnect failed: %s", err)
        except asyncio.CancelledError:
            raise

    def _on_bytes_received(self, data: bytes) -> None:
        """Called for every inbound chunk — updates liveness + feeds parser.

        This is the liveness source of truth: any bytes arriving from the
        Lumagen (even a no-op poll response that doesn't change state)
        count as "the channel is healthy". The protocol layer's state-
        change callback is strictly narrower — use it for state, not
        liveness.
        """
        self._last_response_time = asyncio.get_event_loop().time()
        if not self._available:
            self._available = True
            _LOGGER.info("Lumagen is now available (first response received)")
        self._protocol.feed_bytes(data)

    def _on_protocol_update(self, state: LumagenState, codes: tuple[str, ...]) -> None:
        for listener in list(self._listeners):
            try:
                listener(state, codes)
            except Exception:
                _LOGGER.exception("Sync state listener raised; dropping it")
                with suppress(ValueError):
                    self._listeners.remove(listener)
