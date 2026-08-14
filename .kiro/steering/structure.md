# Project Structure

```
aiolumagen/
├── pyproject.toml              # hatchling build, deps, ruff/mypy/pytest config
├── README.md
├── LICENSE
├── src/
│   └── aiolumagen/
│       ├── __init__.py         # public API re-exports + __version__
│       ├── py.typed            # marker — library is fully typed
│       ├── client.py           # LumagenClient — protocol + transport, handshake, poll loop, response correlation
│       ├── protocol.py         # LumagenProtocol — pure-sync line buffer, ! scan, CSV split, state merge
│       ├── transport.py        # LumagenTransport — thin wrapper over serialx
│       ├── state.py            # LumagenState dataclass + Colorspace/HdrStatus/InputStatus/SourceMode enums
│       ├── commands.py         # Aspect, Input, Memory enums + command-formatting helpers
│       ├── formatting.py       # wire-code decoders (rate, aspect, derived width, output mask)
│       ├── exceptions.py       # LumagenError hierarchy
│       └── firmware/           # firmware updating — a SEPARATE protocol, opt-in import
│           ├── __init__.py     # public surface + update_firmware() / load_updater()
│           ├── container.py    # 0xBABABEBE container parse/verify — pure sync
│           ├── extract.py      # vendor EXE parsing (PE walk + swdata code scan) — pure sync
│           ├── protocol.py     # updater commands, replies, flash map, timings — pure sync
│           ├── plan.py         # which sections need flashing — pure sync
│           └── session.py      # async orchestration; the only module here that does I/O
├── tests/                      # pytest suite (asyncio_mode=auto)
└── examples/
    ├── via_url.py              # CLI example: pass a serialx URL, print state updates
    └── send_raw.py             # CLI example: send an arbitrary command
```

## Architectural Layers

```
LumagenClient        ← I/O orchestration, handshake, poll loop, subscriber dispatch
├── LumagenProtocol  ← pure-sync byte → state translation
└── LumagenTransport ← async byte plumbing over serialx

FirmwareSession      ← firmware updating; own transport, own protocol
└── firmware/*.py    ← pure-sync container / EXE / command / planning layers
```

**Strict separation:**

- `protocol.py` does **no I/O**. It accepts bytes, emits parsed events, mutates the state model. Tests feed it recorded byte streams; no fixtures need a serial port.
- `transport.py` does **no protocol work**. It opens a serialx URL and pipes bytes to/from a callback. Swapping transports must not touch parsing code.
- `client.py` is the only place where async + protocol + transport meet. Handshake and poll-loop logic belong here, not in the protocol or transport layers.

If you find yourself adding `await` to `protocol.py` or string parsing to `transport.py`, stop and reconsider the layer boundary.

**The firmware subsystem mirrors that rule one level down:** only `session.py` does I/O, and everything else in `firmware/` is pure functions over bytes. It stays a separate subsystem, not an extension of the normal-mode layers:

- **Never engaged by `LumagenClient`.** Updater mode is entered with `M0931` and speaks a different protocol; its replies must never reach `LumagenProtocol`. `FirmwareSession` owns its own transport for that reason — and because `serial_proxy` serves one subscriber at a time.
- **Not re-exported from the package root** (its exceptions aside). Importing something that can leave a device unbootable should be an explicit act.
- **Flush-barrier and baud-change plumbing lives in `transport.py`**, because "have the bytes left" and "change the line rate" are byte-plumbing concerns. The *policy* built on them — how long to retry a flush, what `TIMEOUT` means — belongs to `session.py`.

## Conventions

- **`from __future__ import annotations`** at the top of every module.
- **Public API is `aiolumagen.__init__`'s `__all__`.** Anything not re-exported is internal — refactor freely.
- **State fields default to `None`** until first observation. Never fabricate a default value to avoid `None` — it would silently misrepresent the device.
- **Equality matters.** `LumagenState` must implement `__eq__` so HA's coordinator can dedupe with `always_update=False`. If you add a field, make sure it participates in equality.
- **Slots on dataclasses.** `LumagenState` is slotted; new fields go in the dataclass declaration, not as ad-hoc attributes.
- **Enum values mirror Lumagen's wire vocabulary** where it's stable (`Rec.601`, `Rec.709`, `Rec.2020`, `Rec.2100` for `Colorspace`). Don't translate to display strings here — that's the integration's job.
- **No HA imports.** Ever. If a function would benefit from `homeassistant.exceptions.X`, the right move is to raise a `LumagenError` subclass and document the mapping in `tech.md`.

## Testing

- Tests live in `tests/`, run via `uv run pytest`.
- Prefer protocol tests fed with recorded byte streams over end-to-end transport tests.
- `asyncio_mode = "auto"` — async tests don't need the `@pytest.mark.asyncio` decorator.

## What Belongs Here vs. Elsewhere

| Concern | Location |
|---|---|
| Byte-level Lumagen parsing | `protocol.py` |
| Background poll cadence, handshake order | `client.py` |
| serialx URL handling, flush barrier, baud changes | `transport.py` |
| Vendor EXE parsing, firmware container format | `firmware/extract.py`, `firmware/container.py` |
| Updater-mode commands, flash map, transfer timings | `firmware/protocol.py` |
| Which firmware sections need flashing | `firmware/plan.py` |
| Erase/write/verify/promote orchestration | `firmware/session.py` |
| Block-level audit and sector-granular repair | `firmware/session.py` |
| Firmware-update **entity**, progress in the UI | `ha-lumagen` (downstream) |
| HA entities, config flow, coordinator | `ha-lumagen` (downstream) |
| Serial bridge firmware (USB host → serial_proxy) | `esphome-lumagen` (downstream) |
| Vendor PDFs, USB captures, the original PoC scripts | `lumagen-research` (private) |

When something feels like it could go in either `protocol` or `client`: if it can be expressed as "given these bytes, what's the new state?", it belongs in `protocol`. Otherwise it's `client`.

Within `firmware/`, the same test applies to `session` vs everything else: if it can be expressed as "given these bytes, what should we conclude or send?", it's pure and belongs in one of the other modules.

## Firmware-Update Invariants

Non-obvious rules that are load-bearing. Each one has a corresponding test, and each was learned from a real failure — see `references.md` for the source documents.

- **`section0` is an image of flash from `0x0`, so its first sector is the bootloader.** It's staged as `payload[0x20000:]` and copied to `0x20000` by the device. Writing the whole image there shifts firmware by a sector and bricks the unit.
- **The `0xBABABEBE` header is flashed, not stripped.** It's how the device elects an A/B slot at boot.
- **A committed slot never matches its image's raw checksum**, because the device stamps four bytes at `+4`. Compare with `expected_stored_checksum()` or the "is an update needed?" test answers *yes, always*.
- **The flush barrier is not optional.** Pacing alone reproduces the original silent byte loss at high baud. `BLOCK_DELAY` is a separate, additional device requirement.
- **`Z35` is queried fresh, never cached**, and cross-checked against the slots' generation tags before anything is erased.
- **The commit header is written last** so an abort leaves the old firmware bootable.
- **Revisions are `MMDDYY` and must be compared chronologically**, never as integers.
- **`Z97` powers the unit down; `X` leaves it on.** Which one a session ends with is decided by `requires_restart` — see the bullet on that below, and do not shortcut it to "did we promote?".
- **Any command intended for the device must go out at the rate the device is currently listening on. Changing our own rate is not communication.** This has bitten twice, in both directions, and admits no exceptions:
  - **Un-promoted exit:** `hand_back()` must renegotiate *with the device* via `set_baud()` (which sends `B009600` first) before sending `X`. Reconfiguring only the host sends `X` at 9600 to a device at 230400, stranding it in updater mode.
  - **Promoted exit:** `Z97` must be sent **first, at the transfer rate**, and the host realigned to 9600 only afterwards. The device does *not* power itself down when `G39` completes — `Z97` is what powers it down, which is why the vendor sends it 62 ms after `G39` at the negotiated rate. Getting this backwards on hardware left the unit up in updater mode *and* dumping flash, because the mistimed bytes were partly parsed as commands.
- **`G39` completing is not the end of a promotion.** The copy acks and the unit stays up. Don't write code (or comments) that assume the device powers itself off.
- **The `Z97` power-down is owed to anything that needs a reboot to take effect, not to a promotion.** `hand_back()` keys off `requires_restart`, set by *both* a promoted section 0 (which must be loaded) and a committed section-1 slot (which must win a boot election). Keying it off `promoted` was a bug: a section-1-only write left the unit running with its new firmware dormant. Staging to scratch without promoting is the one write that correctly does *not* power down — nothing boots from scratch, so interrupting the user would buy nothing.
- **`force` overrides the up-to-date comparison and nothing else.** Correctness gates (descriptor recovery, device model, container checksums, `Z35` cross-check, header-last) still apply. Don't "improve" it into a general override.
- **`audit()` is read-only and must stay that way.** Its value is being safe to run against a region left half-written by a failed run — evidence the next attempt destroys. It also deliberately does not change the line rate: checksums are computed on the device and replies are 11 bytes, so a faster link buys nothing and only adds a failure mode.
- **The audit chunk is one erase sector (32 blocks) by design.** A failing chunk then maps to exactly the one sector a repair must erase.
- **`repair()` rewrites whole sectors, not individual blocks**, and takes no `stamped_tag`: erasing the header's sector would un-commit a live slot, and rewriting block 0 from the file would restore an *unstamped* header. Damaged committed slots get re-staged, not patched.
- **Auditing a committed slot needs the tag correction too.** Same trap as the planner — without it, block 0 of byte-perfect firmware reports as bad.
