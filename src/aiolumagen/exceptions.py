"""Exceptions raised by aiolumagen.

These map onto Home Assistant's ConfigEntryNotReady / etc. in the
`ha-lumagen` integration; see the library's README for the mapping table.

There is deliberately no timeout exception. Nothing in this library's
*normal-mode* path is request/response — commands are fire-and-forget and
state arrives whenever the device sends it — so there is no outstanding
request for a timeout to apply to. Consumers that need a deadline impose it
themselves (see ``ha-lumagen``'s config flow, which wraps a state poll in
``asyncio.timeout`` and catches the builtin ``TimeoutError``). A previous
``LumagenTimeoutError`` was advertised in the mapping table for a while
without ever being raised; if the library grows a real ``wait_for_state``
waiter, add it back then and not before.

:mod:`aiolumagen.firmware` is the one documented exception to that rule,
and it does *not* change it. A firmware-update session genuinely is
request/response — ``C`` returns a checksum, ``e`` returns ``Ok``, an erase
streams one ``x`` per sector — so a reply that never comes is a real
failure with a real deadline. Rather than adding a general timeout
exception that only one subsystem could ever raise, those deadlines surface
as :class:`LumagenFirmwareError` with a message saying what was being
waited on. That keeps the rule above true for the public client API while
still giving the flasher somewhere to put "the device stopped answering".
"""

from __future__ import annotations


class LumagenError(Exception):
    """Base exception for all aiolumagen errors."""


class LumagenConnectionError(LumagenError):
    """Transport could not establish or maintain a connection.

    Typically maps to ``homeassistant.exceptions.ConfigEntryNotReady`` in the
    HA integration — the device may come back, HA will retry automatically.
    """


class LumagenCommandError(LumagenError, ValueError):
    """A command argument was out of range or otherwise unencodable.

    Raised by the command builders in :mod:`aiolumagen.commands` (and the
    client wrappers around them) when a caller passes a value the Lumagen
    has no encoding for — an input outside 1-19, a sharpness level above
    7, an unknown memory letter. These are programmer errors, not device
    or transport failures.

    Also subclasses :class:`ValueError`, which is what these validators
    used to raise. That keeps ``except ValueError`` callers working while
    letting consumers catch the whole library through
    :class:`LumagenError` — the same dual-inheritance trick
    :class:`json.JSONDecodeError` uses. Don't narrow this to
    ``LumagenError`` alone without a major version bump.
    """


class LumagenFirmwareError(LumagenError):
    """A firmware update could not be completed.

    The base class for everything :mod:`aiolumagen.firmware` raises. Also
    where a firmware-update session's *deadlines* land — see the module docstring
    for why that doesn't reintroduce a general timeout exception.

    Catching this bare means "the update failed, and I am not going to reason
    about how badly". That is a defensible thing to do, but prefer the two
    subclasses below where you can: they carry the distinction between "your
    file was no good" and "we stopped before touching live firmware", which is
    the difference between a message the user can act on and a service call.
    """


class LumagenFirmwareImageError(LumagenFirmwareError, ValueError):
    """The supplied firmware image or vendor EXE is unusable.

    Raised entirely before any device I/O: a file that isn't a PE, a container
    whose magic or additive checksum doesn't hold, an updater whose ``swdata``
    descriptor can't be located, a bundle missing the sections an update needs.
    No device was contacted, so nothing can be in a bad state.

    Also subclasses :class:`ValueError` for the same reason
    :class:`LumagenCommandError` does — this is bad input, and callers who
    already funnel input validation through ``except ValueError`` keep working.
    """


class LumagenFirmwareAbortError(LumagenFirmwareError):
    """The update stopped without altering live firmware.

    This is the *good* failure, and the reason it gets its own class is that a
    flasher's worst property is ambiguity about what state it left behind.
    Raised when a preflight gate refuses (device in standby, in bootloader
    mode, no scratch region available, ``Z35`` nominating the running slot), or
    when staging aborts while the commit is still outstanding.

    In every case the guarantee is the same: the device still boots exactly
    what it booted before, and a power cycle is the entire recovery. Surface it
    to the user as "nothing was changed, here's why, try again" — never as
    "your Lumagen may be damaged".

    Note the guarantee is about *live firmware*, not about flash being
    pristine: an abort routinely leaves the scratch region or an uncommitted
    A/B slot half-written. That is by design. Scratch is rewritten from scratch
    on the next attempt, and an uncommitted slot has no valid container magic
    so it cannot win a boot election. Neither is a thing the user needs to
    clean up, which is why they don't downgrade this from "safe".
    """
