# Cross-Repo Rules (Lumagen Stack)

Three sibling repos work together. This file restates the rules that span them, so they stay consistent when work touches more than one.

```
esphome-lumagen   →   aiolumagen   →   ha-lumagen
  (firmware)         (library)        (HA integration)
```

**Naming note:** this library was formerly `pylumagen`, renamed to avoid a collision with an unrelated `pylumagen` package already published on PyPI. The rename covered everything — GitHub repo, clone path, distribution name and import name are all `aiolumagen`. A surviving `pylumagen` reference anywhere in these repos is stale; the only correct use of the old name is describing the rename itself.

## Boundary Enforcement (the most important rules)

- **`aiolumagen` has zero `homeassistant` imports.** Not in tests, not in helpers, not anywhere. HA-shaped concerns surface via documented `LumagenError` subclasses; the integration translates them.
- **`ha-lumagen` has zero protocol parsing.** No `!` scans, no `ZQ` formatting, no CSV splits in `coordinator.py`/`button.py`/etc. If you'd need to read `Tip0011` to write a line of code in this repo, that line belongs in `aiolumagen`.
- **`esphome-lumagen` has zero Lumagen-specific logic.** It's a serial bridge. The only Lumagen-aware values in the YAML are the startup baud rate (9600 8N1) and the FT232R's VID/PID (implied by `type: ft232`) — everything else (commands, responses, state) is `aiolumagen`'s job. Its `buffer_size` / `flush_timeout` settings are tuned for bulk firmware transfer, which is flow control rather than protocol, so the boundary still holds.

If you violate one of these, stop and move the code.

## API Contract

- **`aiolumagen.__init__`'s `__all__` is the public surface.** Anything re-exported is a contract with `ha-lumagen`. Renaming or removing one is a breaking change and needs a coordinated PR.
- **`LumagenState` field shape is part of the contract.** Adding fields is non-breaking (defaults to `None`). Renaming or retyping a field requires matching changes in `ha-lumagen`'s entities and translations.
- **Enum values are part of the contract.** `Colorspace.REC_709`, `HdrStatus.HDR10`, etc. — `ha-lumagen` may match against them by identity.
- **`aiolumagen.firmware.__all__` is a second, separate contract.** It is *not* flattened into the package root, so `ha-lumagen` imports it explicitly. Its three exceptions are the exception — they *are* re-exported from the root, so a consumer can catch them without importing the subsystem that raises them.

## Firmware Updating Spans All Three Repos

The protocol lives entirely in `aiolumagen`, but the capability has obligations at both ends.

- **`aiolumagen` owns everything protocol-shaped**: EXE parsing, the flash map, which sections need writing, erase/write/verify/promote, audit and repair. `plan_update()` is pure, so a plan can be computed and displayed before anything is committed.
- **`ha-lumagen` owns the user-facing half**: an `update` entity whose availability comes from `plan_update()`, progress from the `UpdateProgress` callback, and the decision to offer an update at all. Two hard requirements:
  - **Unload the coordinator first.** `serial_proxy` serves one subscriber at a time, so a live `LumagenClient` on the same bridge blocks a firmware session.
  - **Percent-encode the PSK** when building the URL. ESPHome noise keys are base64 and routinely contain `+`, which a query string decodes as a space — corrupting the key while preserving its length, and surfacing as aioesphomeapi's misleading `Malformed PSK (length=44)`.
- **`esphome-lumagen` owns the transport's fitness for bulk transfer.** Its `buffer_size: 8192` and `flush_timeout: 1s` are load-bearing: the pool sizing is what the 4096-byte chunk cap is derived from, and the 1 s flush budget is why a block at 230400 drains in one round trip instead of driving the barrier's retry loop on every block. Changing either affects `aiolumagen`'s behaviour without changing a line of its code.

**A successful firmware update powers the unit off.** That is the device's own behaviour (`Z97` is how newly written firmware loads), so `ha-lumagen` must present it as expected rather than as a failure, and must not treat the subsequent unavailability as an error.

## Change Ordering

1. **`aiolumagen` (this repo) first.** Add the API + tests, then merge.
2. **`ha-lumagen` second.** Bump the git pin in `manifest.json` (pinning `aiolumagen@git+https://github.com/frankrosano/aiolumagen.git@<sha-or-tag>` is fine when you need a specific commit; otherwise `@main`), update consumers, ship.
3. **`esphome-lumagen` rarely changes** in lockstep with the others — it ships the wire, not the protocol.

Don't introduce dead API in `aiolumagen` "for `ha-lumagen` to consume later." Ship features in the order users see them.

## Version Coordination

- `aiolumagen`'s version lives in **two places**: `pyproject.toml`'s `version =` and `src/aiolumagen/__init__.py`'s `__version__`. Keep them in sync.
- `ha-lumagen`'s `manifest.json` `version` is the HACS-visible release; bump it when the integration's behavior changes (not just because aiolumagen did).
- `ha-lumagen`'s `manifest.json` `requirements` line pins `aiolumagen` from git. Pin it to the tag you just cut rather than `@main`, so an install resolves to a known artifact.

## Where Bugs File

| Symptom | Likely repo |
|---|---|
| Wrong value parsed from a Lumagen response | `aiolumagen` (`protocol.py` / `state.py`) |
| State stops updating; reconnect storm | `aiolumagen` (`client.py`) |
| Entity shows wrong icon / device class / unit | `ha-lumagen` |
| Config flow can't find the serial port | `ha-lumagen` (or HA's `usb` integration upstream) |
| Bytes never reach the Lumagen / no serial response | `esphome-lumagen` — check USB enumeration, and its log for `Output pool full` |
| Connection drops over the network | usually `serialx` (upstream); occasionally `esphome-lumagen` (Ethernet) |

## Testing Strategy (per repo)

| Repo | Approach |
|---|---|
| `aiolumagen` | Pure-protocol tests fed with recorded byte streams. No serial port, no network. `uv run pytest`. |
| `ha-lumagen` | `pytest-homeassistant-custom-component` with a mocked `LumagenClient`. Test HA wiring, not protocol behavior (already covered upstream). |
| `esphome-lumagen` | `./build.sh config` for YAML validation. Manual smoke test on hardware — no automated tests today. |

## Shared Python Conventions (`aiolumagen` + `ha-lumagen`)

- Python **3.14+**.
- `from __future__ import annotations` at the top of every module.
- Ruff: `line-length = 100`, `target-version = "py314"`.
- Pytest: `asyncio_mode = "auto"`, `addopts = "-ra --strict-markers --strict-config"`.
- Mypy: `aiolumagen` is strict; `ha-lumagen` is not (HA stubs aren't strict-clean yet).

## Local Development Wiring

- `ha-lumagen/pyproject.toml` overrides the manifest's git pin via `[tool.uv.sources]` to point at the sibling `../aiolumagen` checkout (`editable = true`). Edit this repo and the integration sees the change immediately under `uv run pytest`.
- End users always get the git-pinned version from `manifest.json`.

## Secrets, Ignores

- `esphome-lumagen/secrets.yaml` is gitignored and auto-generated by `build.sh` if missing.
- Reference material — Lumagen, Inc.'s copyrighted PDFs, the Crestron sample, vendor updater EXEs, extracted firmware blobs, flash captures — lives in the **private** `lumagen-research` repo. It was previously a gitignored `esphome-lumagen/References/` folder. All three code repos are public: never copy any of it in, and cite by filename instead.
- `.venv/` and `.esphome/` are build artifacts; gitignored everywhere.

## When In Doubt

If a change could plausibly live in two of the three repos, prefer:

- `aiolumagen` over `ha-lumagen` for anything Lumagen-aware
- `aiolumagen` over `esphome-lumagen` for anything reactive to Lumagen state
- `ha-lumagen` over `aiolumagen` for anything HA-shaped (entity descriptions, translations, config flow)
