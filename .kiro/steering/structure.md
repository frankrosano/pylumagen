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
│       └── exceptions.py       # LumagenError hierarchy
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
```

**Strict separation:**

- `protocol.py` does **no I/O**. It accepts bytes, emits parsed events, mutates the state model. Tests feed it recorded byte streams; no fixtures need a serial port.
- `transport.py` does **no protocol work**. It opens a serialx URL and pipes bytes to/from a callback. Swapping transports must not touch parsing code.
- `client.py` is the only place where async + protocol + transport meet. Handshake and poll-loop logic belong here, not in the protocol or transport layers.

If you find yourself adding `await` to `protocol.py` or string parsing to `transport.py`, stop and reconsider the layer boundary.

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
| serialx URL handling | `transport.py` |
| HA entities, config flow, coordinator | `ha-lumagen` (downstream) |
| Serial bridge firmware (USB host → serial_proxy) | `esphome-lumagen` (downstream) |

When something feels like it could go in either `protocol` or `client`: if it can be expressed as "given these bytes, what's the new state?", it belongs in `protocol`. Otherwise it's `client`.
