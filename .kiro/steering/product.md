# Product

`aiolumagen` is an async Python library that implements the Lumagen Radiance Pro RS-232 protocol. (Formerly `pylumagen` — renamed to avoid a naming collision with an unrelated `pylumagen` package already published on PyPI. The rename covered everything: GitHub repo, distribution name, and import name are all `aiolumagen`.)

## Scope

Protocol-only. The library knows how to:

- Format commands and queries against the Lumagen
- Parse `!`-prefixed responses (including command echo) into a typed state model
- Run a startup handshake (`ZE2` + initial queries) and a background poll loop
- Surface state changes via a subscription callback

It explicitly does **not** know about Home Assistant, UI, or any specific transport implementation. Transport is delegated to [`serialx`](https://github.com/puddly/serialx), which lets the same client code talk over direct USB/RS-232, raw TCP (ser2net), or an ESPHome `serial_proxy`.

## Sibling Repos

- `esphome-lumagen` — ESPHome firmware that exposes the Lumagen's serial port over the network as a `serial_proxy`. The ESP32-S3 is a USB host driving the FT232R inside the Lumagen (its rear USB-B port), so no level shifter is involved. `aiolumagen` is one possible client of that proxy.
- `ha-lumagen` — Home Assistant custom integration. Thin wrapper over this library; owns config flow, coordinator, and entities.

## Design Goals

- **Pure-sync protocol layer.** `LumagenProtocol` does line buffering, the `!` scan, CSV split, and state merging without any I/O. Easy to test with recorded byte streams.
- **Thin transport.** `LumagenTransport` is a wrapper over `serialx.create_serial_connection` that pipes bytes to a callback. No Lumagen-specific logic lives here.
- **Composed client.** `LumagenClient` glues protocol + transport, runs the handshake, and owns the poll loop.
- **Slotted, equality-comparable state.** `LumagenState` implements `__eq__` so HA's `DataUpdateCoordinator` can run with `always_update=False` and skip redundant writes. Fields stay `None` until the corresponding response has been observed at least once.

## Public Surface

Re-exported from the package root (`aiolumagen.__init__`):

- Client + transport: `LumagenClient`, `LumagenTransport`
- State: `LumagenState`, `Colorspace`, `HdrStatus`, `InputStatus`, `SourceMode`
- Commands: `Aspect`, `Input`, `Memory`
- Errors: `LumagenError`, `LumagenConnectionError`, `LumagenCommandError`

These names are the API contract — renaming or removing one is a breaking change.
