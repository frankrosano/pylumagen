# Product

`aiolumagen` is an async Python library that implements the Lumagen Radiance Pro RS-232 protocol. (Formerly `pylumagen` — renamed to avoid a naming collision with an unrelated `pylumagen` package already published on PyPI. The rename covered everything: GitHub repo, distribution name, and import name are all `aiolumagen`.)

## Scope

Protocol-only. The library knows how to:

- Format commands and queries against the Lumagen
- Parse `!`-prefixed responses (including command echo) into a typed state model
- Run a startup handshake (`ZE2` + initial queries) and a background poll loop
- Surface state changes via a subscription callback
- Update the device's firmware from a vendor updater `.exe` (`aiolumagen.firmware`)

It explicitly does **not** know about Home Assistant, UI, or any specific transport implementation. Transport is delegated to [`serialx`](https://github.com/puddly/serialx), which lets the same client code talk over direct USB/RS-232, raw TCP (ser2net), or an ESPHome `serial_proxy`.

Firmware updating is a second, independent protocol rather than an extension of the first: entered with `M0931`, framed differently, and the only part of the library that can leave a device unbootable. It lives in its own subsystem, is never engaged by `LumagenClient`, and is imported explicitly.

### Firmware capabilities

`aiolumagen.firmware` covers the whole flow, not just the write:

| Capability | Entry point |
|---|---|
| Parse a vendor updater EXE into firmware images | `load_updater()` / `extract_images()` |
| Decide which sections actually need writing | `plan_update()` → `UpdatePlan` |
| Update a device end to end | `update_firmware()` |
| Drive the individual steps | `FirmwareSession` |
| Locate damage block by block (read-only) | `FirmwareSession.audit()` → `AuditResult` |
| Rewrite only the sectors containing bad blocks | `FirmwareSession.repair()` |
| Recover a desynced device without a power cycle | `FirmwareSession.resync()` |
| Deliberate re-flash / single-section flash | `force=` / `only=` |
| CLI harness for all of it | `examples/update_firmware.py` |

**Hardware-qualified**, on a Radiance Pro 4242 over an ESPHome `serial_proxy` at 230400: extraction against five updater releases (`030225`, `092025`, `112325`, `120325`, `030326`), repeated section-0 writes and promotions, 772-block section-1 writes with the header-last commit, a two-release upgrade chain covering both the section-1-changes and section-1-skipped decisions, and block-level audits of both region sizes. The flush barrier held with zero retries across ~1,900 blocks.

Those five are simply the releases spanning the test unit's starting firmware through the then-current one. **Lumagen publishes many more.** Don't describe them as "all known releases" or reason as though they characterise the whole firmware line — the extractor's code-scan for the `swdata` descriptor exists precisely because release-to-release variation is expected.

Deliberately **not** hardware-exercised, and unit-tested only: `repair()` (no run ever produced damage), the flush-barrier retry path (with `flush_timeout: 1s` a block drains inside the ESP's budget, so `TIMEOUT` never occurs at 230400), rates other than 230400, bootloader mode (refused by design), and chip-image writes (out of scope). Keep this list honest as it changes.

### Deliberate limits on what can be damaged

Stated in the README as a user-facing warning, and worth keeping true:

- **The bootloader is never written** — flash `0x0`–`0x20000` is untouched on every path, which is what preserves the vendor updater as a recovery route.
- **HDMI chip firmware is never written** — it was unchanged across every release sampled here, so nothing was available to test a write path against. That is a limit of the sample, not a property of the firmware line.
- **Only the Radiance Pro is accepted** — `plan_update()` refuses any other device id. One unit has ever been tested: a Pro 4242.

This is an independent reverse-engineered implementation with no association with or endorsement by Lumagen, Inc. Don't let the README imply otherwise.

## Sibling Repos

- `esphome-lumagen` — ESPHome firmware that exposes the Lumagen's serial port over the network as a `serial_proxy`. The ESP32-S3 is a USB host driving the FT232R inside the Lumagen (its rear USB-B port), so no level shifter is involved. `aiolumagen` is one possible client of that proxy.
- `ha-lumagen` — Home Assistant custom integration. Thin wrapper over this library; owns config flow, coordinator, and entities.

## Design Goals

- **Pure-sync protocol layer.** `LumagenProtocol` does line buffering, the `!` scan, CSV split, and state merging without any I/O. Easy to test with recorded byte streams.
- **Thin transport.** `LumagenTransport` is a wrapper over `serialx.create_serial_connection` that pipes bytes to a callback. No Lumagen-specific logic lives here.
- **Composed client.** `LumagenClient` glues protocol + transport, runs the handshake, and owns the poll loop.
- **Slotted, equality-comparable state.** `LumagenState` implements `__eq__` so HA's `DataUpdateCoordinator` can run with `always_update=False` and skip redundant writes. Fields stay `None` until the corresponding response has been observed at least once.
- **Reviewable firmware updates.** The decision about what to flash is a pure function returning an inspectable plan, so a caller can show a user exactly which sections would be written — and roughly how long that takes — before committing. Byte-level comparison against the device, never a version heuristic.

## Public Surface

Re-exported from the package root (`aiolumagen.__init__`):

- Client + transport: `LumagenClient`, `LumagenTransport`
- State: `LumagenState`, `Colorspace`, `HdrStatus`, `InputStatus`, `SourceMode`
- Commands: `Aspect`, `Input`, `Memory`
- Errors: `LumagenError`, `LumagenConnectionError`, `LumagenCommandError`, `LumagenFirmwareError`, `LumagenFirmwareImageError`, `LumagenFirmwareAbortError`

Firmware updating is namespaced under `aiolumagen.firmware` rather than flattened into the root — chiefly `update_firmware()`, `load_updater()`, `plan_update()`, `FirmwareSession`, `UpdatePlan`, `UpdateProgress`, `UpdateResult`. Its *exceptions* are the exception, re-exported above so consumers can catch them without importing the subsystem.

Both `__all__` lists are the API contract — renaming or removing a name is a breaking change.
