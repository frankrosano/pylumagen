"""Byte-level transport for aiolumagen.

Wraps :mod:`serialx`'s :func:`create_serial_connection` so the library can
open any serial URL scheme serialx supports:

* ``/dev/tty.usbserial-*`` — direct USB/RS-232 serial
* ``esphome://<host>:<port>/?port_name=<name>&key=<psk>`` — ESPHome
  serial proxy
* ``socket://<host>:<port>`` — raw TCP bridge (e.g. ser2net)
* anything else serialx grows support for

The ``esphome://`` scheme needs ``aioesphomeapi``, which this package does
*not* depend on directly — see the note on ``dependencies`` in
``pyproject.toml``. Inside Home Assistant it's already present; elsewhere,
install ``aiolumagen[esphome]``. serialx registers its platforms
conditionally, so the other schemes work fine without it — and a missing
install surfaces as a :class:`~aiolumagen.exceptions.LumagenConnectionError`
from :meth:`connect` naming the extra (serialx's own message for this is
"No handler registered for URI scheme", which doesn't hint at the cause; see
:meth:`LumagenTransport._connect_error_message`).

:class:`LumagenTransport` is deliberately thin: it opens the URL, pipes
inbound bytes to a callback, and exposes :meth:`write`. All Lumagen
protocol concerns live in :class:`~aiolumagen.protocol.LumagenProtocol`.

For tests we use a lightweight in-memory stub (see ``tests/conftest.py``)
that implements the same three public methods (``connect`` / ``disconnect``
/ ``write``) without touching serialx. No ABC is exposed — duck typing
keeps the surface small.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Callable
from typing import Any, Final

import serialx

from aiolumagen.exceptions import LumagenConnectionError

_LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 9600

FLUSH_MODE_STATUS: Final = "status"
FLUSH_MODE_BLIND: Final = "blind"
FLUSH_MODE_NONE: Final = "none"

_FLUSH_STATUS_BY_VALUE: Final[dict[int, str]] = {
    0: "OK",
    1: "ASSUMED_SUCCESS",
    2: "ERROR",
    3: "TIMEOUT",
    4: "NOT_SUPPORTED",
}
"""aioesphomeapi's ``SerialProxyStatus``, by value.

Duplicated rather than imported so this module still works against
``socket://`` and plain serial on a host with no ``aioesphomeapi`` — the same
reason the package doesn't depend on it (see ``pyproject.toml``).
"""


def normalize_flush_status(status: object) -> str:
    """Normalise a ``SerialProxyStatus`` to a bare name like ``OK``/``TIMEOUT``.

    Total by construction. Callers branch on the result, and a value this
    failed to recognise would read as a hard error and disable the flush
    barrier for the rest of a transfer — so every input maps to *something*.
    Accepts the enum (whose ``.name`` is already bare; the prefix strip covers
    the protobuf spelling), a raw int, a plain string, and ``None``.

    ``None`` is treated as success: it's what ``SerialProxyStatus.convert()``
    returns both for an absent field and for a value it doesn't know, and in
    either case the flush request itself completed.
    """
    if status is None:
        return "OK"
    name = getattr(status, "name", None)
    if isinstance(name, str):
        return name.removeprefix("SERIAL_PROXY_STATUS_")
    if isinstance(status, str):
        return status.strip().upper().removeprefix("SERIAL_PROXY_STATUS_")
    if isinstance(status, int):
        return _FLUSH_STATUS_BY_VALUE.get(status, f"status {status}")
    return repr(status)


class _BridgeProtocol(asyncio.Protocol):
    """asyncio.Protocol that fans inbound bytes to the owning transport."""

    def __init__(self, owner: LumagenTransport) -> None:
        self._owner = owner

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        _LOGGER.debug("Serial connection established via %s", self._owner.url)

    def data_received(self, data: bytes) -> None:
        self._owner._emit(data)

    def connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.debug("Serial connection closed (%s): %s", self._owner.url, exc)
        self._owner._handle_connection_lost()


class LumagenTransport:
    """Open a serialx URL and expose read-callback + write-bytes API.

    :param url: Any URL accepted by :func:`serialx.serial_for_url`. For
        a Lumagen attached to an ESPHome bridge, this will typically be
        ``esphome://<host>:<port>/?port_name=<name>&key=<psk>``.
    :param baudrate: Defaults to 9600 (the Lumagen's hardcoded rate).
    """

    def __init__(self, url: str, *, baudrate: int = DEFAULT_BAUDRATE) -> None:
        self.url = url
        self.baudrate = baudrate
        self._on_data: Callable[[bytes], None] | None = None
        self._transport: asyncio.Transport | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None:
        """Register the inbound-data callback. Must be set before :meth:`connect`."""
        self._on_data = callback

    async def connect(self) -> None:
        if self._connected:
            return
        loop = asyncio.get_running_loop()
        try:
            transport, _protocol = await serialx.create_serial_connection(
                loop,
                lambda: _BridgeProtocol(self),
                url=self.url,
                baudrate=self.baudrate,
            )
        except Exception as err:
            raise LumagenConnectionError(self._connect_error_message(err)) from err
        self._transport = transport
        self._connected = True

    async def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._connected = False

    async def write(self, data: bytes) -> None:
        if not self._connected or self._transport is None:
            raise LumagenConnectionError("Transport is not connected")
        self._transport.write(data)

    # -- Flow control and line rate ----------------------------------------
    #
    # Both of these exist for the firmware flasher in :mod:`aiolumagen.firmware`
    # and are useless to the normal-mode client, which writes six bytes at a time
    # at a rate that never changes. They live here anyway because they are
    # byte-plumbing concerns, not protocol ones: "have the bytes I handed you
    # actually left" and "change the line rate" would mean the same thing for any
    # payload. The firmware subsystem owns the *policy* built on top (how long to keep
    # retrying a flush, what a TIMEOUT means, when a rate change is safe).
    #
    # They reach into serialx internals, which is deliberate and confined to
    # these three methods so there is exactly one place to fix when serialx moves.

    def resolve_flush_mode(self) -> str:
        """Report how — or whether — this transport can be made to block on drain.

        Decided by inspection, once, because the answer can't change for the life
        of a connection:

        ``status``
            The ESPHome proxy, reached through ``APIClient.serial_proxy_flush`` so
            the reply's ``SerialProxyStatus`` is visible. ``TIMEOUT`` is the value
            that matters — it means the ESP's own flush budget expired with bytes
            still queued, i.e. we are outrunning it — and it is exactly the value
            serialx's public wrapper discards, which is why this goes around it.
        ``blind``
            serialx's public :meth:`flush`. Still a real barrier (POSIX waits for
            the hardware buffer; ESPHome round-trips), but with the status thrown
            away a TIMEOUT is indistinguishable from success.
        ``none``
            No barrier available. The caller must fall back to open-loop pacing
            and accept that byte loss is no longer detectable.
        """
        transport = self._transport
        if transport is None:
            return FLUSH_MODE_NONE
        ser: Any = getattr(transport, "serial", None)
        api: Any = getattr(ser, "_api", None)
        instance: Any = getattr(ser, "_instance_id", None)
        if api is not None and instance is not None and hasattr(api, "serial_proxy_flush"):
            return FLUSH_MODE_STATUS
        if hasattr(transport, "flush"):
            return FLUSH_MODE_BLIND
        return FLUSH_MODE_NONE

    async def flush(self, *, timeout: float, mode: str | None = None) -> str:
        """Issue one flush and return a :func:`normalize_flush_status` name.

        One round trip, no retries: a ``TIMEOUT`` here is normal and re-issuing
        is the caller's job (see
        :meth:`~aiolumagen.firmware.session.FirmwareSession.barrier`), because
        only the caller knows how long the whole transfer may take.

        Returns ``NOT_SUPPORTED`` rather than raising when there is no barrier to
        issue, so a caller can treat "no flush available" as just another status.
        """
        transport = self._transport
        if not self._connected or transport is None:
            raise LumagenConnectionError("Transport is not connected")
        if mode is None:
            mode = self.resolve_flush_mode()

        if mode == FLUSH_MODE_STATUS:
            ser: Any = transport.serial  # type: ignore[attr-defined]
            # Private on purpose: serialx's public transport.flush() drops the
            # SerialProxyStatus, and TIMEOUT is the one value worth having.
            coro = ser._api.serial_proxy_flush(instance=ser._instance_id, timeout=timeout)
            # ESPHomeSerial may run its API client on a different loop; when it
            # exposes the hop, use it rather than awaiting the coroutine here.
            hop = getattr(ser, "_call_on_client_loop", None)
            response: Any = await (hop(coro) if hop is not None else coro)
            return normalize_flush_status(getattr(response, "status", None))

        if mode == FLUSH_MODE_BLIND:
            blind: Any = transport.flush()  # type: ignore[attr-defined]
            await asyncio.wait_for(blind, timeout=timeout)
            return "OK"

        return "NOT_SUPPORTED"

    async def set_baudrate(self, baudrate: int) -> str:
        """Reconfigure the line rate in place, returning how it was done.

        No reconnect is needed, including over the ESPHome proxy — which is what
        makes the Lumagen's ``B<rate>`` renegotiation usable at all.

        Prefers serialx's *async* configure path. Its sync ``baudrate`` setter
        funnels through ``_call_on_loop``, which does
        ``run_coroutine_threadsafe(...).result()``; called from the loop's own
        thread that is an instant self-deadlock, so serialx raises instead of
        hanging. Falling back to the sync setter in a worker thread reaches the
        same coroutine legitimately, since off-loop callers are precisely what
        ``_call_on_loop`` is built for — that path covers platforms without the
        guard, such as ``socket://``.
        """
        transport = self._transport
        if not self._connected or transport is None:
            raise LumagenConnectionError("Transport is not connected")
        ser: Any = getattr(transport, "serial", None)
        if ser is None:
            raise LumagenConnectionError(
                f"{self.url!r} exposes no underlying serial object, so its line "
                "rate cannot be changed"
            )
        try:
            if hasattr(ser, "_async_configure_port") and hasattr(ser, "_baudrate"):
                ser._baudrate = baudrate
                await ser._async_configure_port()
                how = "async"
            else:
                await asyncio.to_thread(setattr, ser, "baudrate", baudrate)
                how = "threaded sync"
        except Exception as err:
            raise LumagenConnectionError(
                f"Could not reconfigure {self.url!r} to {baudrate} baud: {err}"
            ) from err
        self.baudrate = baudrate
        _LOGGER.debug("Line rate for %s set to %d (%s)", self.url, baudrate, how)
        return how

    # -- Internal plumbing -------------------------------------------------

    def _connect_error_message(self, err: Exception) -> str:
        """Explain a connect failure, translating the missing-extra case.

        serialx registers its platforms *conditionally*: with ``aioesphomeapi``
        absent, the ``esphome://`` scheme simply never registers, and the
        failure surfaces as "No handler registered for URI scheme" rather than
        an ImportError. That phrasing gives a user no idea what to install, so
        detect the real cause — an ``esphome://`` URL with the package missing
        — and say so.

        Checked with :func:`importlib.util.find_spec` rather than a real
        import: this runs on a failure path and there's no reason to pay for
        loading a heavyweight compiled package just to prove it exists.
        """
        base = f"Could not open {self.url!r}: {err}"
        if not self.url.lower().startswith("esphome://"):
            return base
        if importlib.util.find_spec("aioesphomeapi") is not None:
            return base
        return (
            f"Could not open {self.url!r}: the ESPHome transport needs the "
            "'aioesphomeapi' package, which isn't installed — so serialx never "
            "registered the esphome:// scheme, which is what the underlying "
            f"error ({err}) actually means. Install aiolumagen[esphome], or "
            "inside Home Assistant make sure the ESPHome integration is set "
            "up; it provides that package, and aiolumagen deliberately doesn't "
            "depend on it so it can't fight Home Assistant's pinned version."
        )

    def _emit(self, data: bytes) -> None:
        if self._on_data is not None and data:
            self._on_data(data)

    def _handle_connection_lost(self) -> None:
        self._connected = False
        self._transport = None
