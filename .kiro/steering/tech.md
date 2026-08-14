# Tech Stack

## Language & Runtime

- **Python 3.14+** (required, not "or newer than 3.10" — the lock file pins 3.14)
- Async / `asyncio` throughout the client and transport
- `from __future__ import annotations` at the top of every module

## Dependencies

Runtime:
- `serialx >= 1.7` — universal serial transport (direct, TCP, ESPHome proxy)

**Do not add the `[esphome]` extra to the base dependency.** It only pulls
`aioesphomeapi`, which Home Assistant pins *exactly* for its own esphome
integration and installs into the same site-packages. Any range we declare
lets the resolver move HA's pin; because aioesphomeapi is Cython, that
rewrite leaves mismatched `.so` files and kills the ESPHome integration
("APIConnection size changed", "does not export expected C function
make_noise_packets").

Omitting the extra costs nothing. serialx imports the platform lazily by URL
scheme, and in HA it's guaranteed present regardless: the `usb` integration
imports `serialx.platforms.serial_esphome` at module scope via
`serial_proxy_stub`, so any install that brings up `usb` — which
`ha-lumagen` requires — has already loaded it. Standalone users opt in with
`pip install aiolumagen[esphome]`.

### HA's pins are moving targets — don't hard-code them

Every version below is a snapshot, not a constant. HA bumps these with most
releases, so treat any number written down here as stale and read the live
value from `homeassistant/components/<domain>/manifest.json` in the target
install.

| | HA 2026.5.1 | HA 2026.7.4 |
|---|---|---|
| `aioesphomeapi` | `==44.21.0` | `==45.3.1` |
| `serialx` | `==1.7.1` | `==1.8.2` |

A whole major version of `aioesphomeapi` inside two minor HA releases. This
is the argument against "just match HA's pin": whatever we wrote would be
wrong by the next release, which is why the dependency is dropped entirely
rather than pinned.

#### `serialx` is a different case — the floor is guaranteed

serialx has the same superficial shape (HA pins it exactly, and it ships a
compiled extension, `_serialx_rust.abi3.so`), and unlike `aioesphomeapi` it
can't be dropped — it's the transport. But the exposure is narrower than it
looks, and the safe half is by design rather than coincidence:

- **Floor — guaranteed.** `ha-lumagen`'s `hacs.json` sets a minimum HA of
  `2026.5.0`, and that release pins `serialx==1.7.1`. So every supported HA
  already ships a serialx satisfying our `>=1.7`, and pip never has to
  *upgrade* serialx on our account. Raising the HACS minimum can only raise
  HA's serialx, never lower it — so this can't regress.
- **Ceiling — unbounded, currently inert.** `>=1.7` has no upper bound, so
  nothing in our metadata prevents a resolver installing *above* HA's exact
  pin. Today nothing can: `1.8.2` is simultaneously HA 2026.7.4's pin and the
  newest release on PyPI, so there is nothing higher to install. That half is
  circumstantial and changes the day serialx 1.9 ships before HA adopts it.

Leaving it unbounded is deliberate. Capping to `<2` buys nothing (it still
permits 1.9 over 1.8.2), and pinning exactly would recreate the same drift
problem that made us drop `aioesphomeapi` in the first place. The residual
window — a serialx release HA hasn't picked up yet — is small, since HA
tracks it closely (2026.5 → 1.7.1, 2026.7 → 1.8.2).

Dev (in the `dev` group of `pyproject.toml`):
- `pytest >= 8`, `pytest-asyncio >= 0.24`, `pytest-cov >= 5`
- `ruff >= 0.7` for lint + format
- `mypy >= 1.11` in **strict** mode

### `uv.lock` is deliberately not tracked

Both repos gitignore it. The dev environment is *meant* to float: this stack
is developed against whatever HA is current, because that's what the author
runs in production. A test env that tracks HA's latest surfaces HA
regressions at the same time they'd bite live, which is the point.

The tradeoff is accepted, not overlooked: a green run isn't reproducible
months later, and a CI failure may come from an HA bump rather than a code
change. Don't "fix" this by committing a lock or pinning
`pytest-homeassistant-custom-component`.

No other runtime deps. Keep it that way unless there's a clear reason — every added dep is a dep `ha-lumagen` will inherit.

## Build / Packaging

- Build backend: `hatchling`
- Layout: `src/aiolumagen/` (src layout, not flat)
- `py.typed` is shipped — the library is fully type-checked downstream
- Wheel includes `src/aiolumagen`; sdist also includes `tests`, `README.md`, `LICENSE`, `pyproject.toml`

## Tooling Configuration

- **Ruff**: `line-length = 100`, `target-version = "py314"`, lint rules: `E F W I N UP B A C4 SIM RUF`
- **Mypy**: `python_version = "3.14"`, **strict = true**, `warn_unreachable = true`
- **Pytest**: `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `addopts = "-ra --strict-markers --strict-config"`

Strict mypy means every public function must be fully annotated. Don't disable strict mode — fix the types.

## Common Commands

```bash
uv sync                # install runtime + dev deps into .venv
uv run pytest          # run the test suite
uv run pytest --cov    # with coverage
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy src        # type check (strict)

# Run the example against a real device
uv run python examples/via_url.py 'esphome://10.0.0.42:6053/?port_name=Lumagen&key=<base64-psk>'
```

## Versioning & Distribution

- Version lives in `pyproject.toml` and `aiolumagen/__init__.py` (`__version__`). Keep them in sync.
- Not on PyPI yet — `ha-lumagen`'s manifest installs it from git (`aiolumagen@git+https://github.com/frankrosano/aiolumagen.git@<tag>`). Pin the tag you cut rather than `@main` so an install resolves to a known artifact.
- Local development: `ha-lumagen`'s `pyproject.toml` uses `[tool.uv.sources]` to point at this repo via path with `editable = true`.

## Lumagen Protocol Notes

- 9600 8N1 ASCII; commands 1–6 chars, no CR terminator; queries start with `ZQ`; responses start with `!`.
- The Lumagen **may echo the sent command** as a prefix on the response line. The protocol layer must scan for `!` rather than assuming it's at position 0.
- Unsolicited reports require user setup on the device: **Menu → Other → I/O Setup → RS-232 Setup → Report mode changes → Full v5 → Save**. Without this, the library still works via polling — keep that path correct. A device left on Full v4 also works: the `!I21`–`!I24` parsers are retained, so pushes still land, minus power/memory in real time.
- **Full v5 is the supported firmware floor.** `ZQI25` is the only status query issued; there is deliberately no `ZQI24` query. Don't add one back — if pre-v5 support is ever needed, make it an explicit client option rather than a probe, because the device answers any valid `ZQ` code with an empty payload so "is v5 supported" can't be reliably detected at runtime.

## Exception Mapping (contract with `ha-lumagen`)

| Library exception | HA mapping (in the integration) |
|---|---|
| `LumagenConnectionError` | `ConfigEntryNotReady`, or device unavailable from the coordinator |
| `LumagenCommandError` | Log a warning; don't surface to the user. Also subclasses `ValueError` |
| `LumagenFirmwareImageError` | Bad input file; no device was contacted. Also subclasses `ValueError` |
| `LumagenFirmwareAbortError` | Update refused or stopped **before touching live firmware**. Tell the user nothing changed and offer a retry |
| `LumagenFirmwareError` | Base of the two above; also what an unconfirmable outcome raises. Surface the message |

There is no `LumagenAuthError` — the Lumagen has no auth. Transport-layer auth failures (e.g. wrong ESPHome PSK) surface as `LumagenConnectionError` via serialx.

There is no timeout exception either. Nothing in the *client* is request/response, so there's no outstanding request a deadline could apply to; consumers impose their own with `asyncio.timeout` and catch the builtin `TimeoutError`. Don't re-add one unless the library grows a real `wait_for_state` waiter.

`aiolumagen.firmware` is the one documented exception and does **not** change that rule. A firmware session genuinely is request/response, so its deadlines surface as `LumagenFirmwareError` naming what was awaited, rather than as a general timeout exception only one subsystem could raise.

The three firmware exceptions are re-exported from the package root even though the rest of the firmware API is not, so a consumer can catch them without importing the subsystem that raises them.

## Firmware Updating

`aiolumagen.firmware` implements the vendor's firmware-update protocol — `M0931`, baud renegotiation, 4096-byte block writes, erase/verify/promote. Ported from the hardware-validated proof-of-concept in the private `lumagen-research` repo, whose `FIRMWARE_UPDATE_PROTOCOL.md` remains the reference for *why* each constant is what it is.

- **No new runtime dependencies.** PE parsing is hand-rolled rather than taking `pefile`, because `ha-lumagen` would inherit that dependency into every HA install for one optional feature.
- **Vendor EXEs, USB captures and PDFs stay in `lumagen-research`.** They're Lumagen, Inc.'s copyright and this repo is public. Tests build a synthetic PE instead; the extractor is cross-checked against the real releases outside the suite.
- **Chip images (`hdmi_rx`/`tx`/`ntx`) are extracted but never written.** Beyond "no observed vendor session writes them in Auto mode": they were **byte-identical across every release sampled** (five of them), so nothing was available to test a write path against. Don't add one speculatively — it would ship untestable code on the one path that can brick an HDMI board. Note this is a limit of the sample: Lumagen publishes far more releases than were examined, and a bundle that changes a chip image may exist. If one turns up, that is the trigger to reconsider — with a real diff to validate against.
- **The bootloader is never written, and that is a recovery guarantee, not an omission.** Flash `0x0`–`0x20000` is untouched on every path (`section0` is written with its first sector stripped for exactly this reason), which is what keeps the vendor's updater viable as a recovery route. Bootloader mode (`H0` → `Ok`) is also *refused* at preflight rather than supported: that path uses a different block size, is unverified, and is brick-capable.
- **Only the Radiance Pro is accepted.** `plan_update()` refuses any device not reporting id `0x16`; an updater's images are not valid for a different model. The one unit ever tested is a Pro 4242. Don't relax this gate to "probably fine".
- The transfer timings in `firmware/protocol.py` are measured, not guessed. See the invariants list in `structure.md` before changing any of them.

### Testing it on hardware

`examples/update_firmware.py` is the harness, and its module docstring holds the
escalation order. The rules the research campaign settled on, which still apply:

- **Qualify on scratch, never on section 1.** `--only section0 --no-promote` runs
  the entire erase/write/verify path with live firmware untouched, so a failure
  costs a retry. Repeat it; don't treat one pass as evidence.
- **Change one variable at a time.** A `--header-last` test once failed at 115200
  and passed immediately at 57600, confounding "is the idea sound?" with "is this
  rate sound?".
- **Escalate block count, not just rate.** Section 0 is ~112 blocks, section 1 is
  772. A marginal setting passes the first and fails the second. This is the
  escalation that matters, and it is what the qualification campaign actually did
  — the rate walk was deliberately skipped as the weaker test.
- **Only 230400 is qualified.** It is the vendor's own rate and the one the flush
  barrier was designed for, and it held with zero retries across ~1,900 blocks.
  9600 / 57600 / 115200 are accepted by `SUPPORTED_BAUDS` but untested here.
  Historical note if you ever need a lower rate: 115200 is the worst of the three
  — it was once called qualified off a single clean run and later failed ~1 in 4.
- **`flush_timeout: 1s` on the bridge is why the retry path never fires.** A
  4096-byte block at 230400 is 177.8 ms of wire time, which drains inside that
  budget, so the ESP answers `OK` rather than `TIMEOUT`. On a bridge left at the
  100 ms default the retry loop becomes the *normal* path on every block. So a
  zero-retry result is evidence about the firmware config, not just the link.
- **Verify by a second, independent mechanism.** A whole-region `CS=` is one
  32-bit sum over megabytes; `--audit` checks each block separately and says
  *where* the damage is. Both agreeing is meaningfully stronger than either alone.
- Run `--status` before and after each test: it shows both A/B slot headers and
  generations, which is how you confirm what actually changed.
- `--audit` is read-only and safe against a half-written region — run it *before*
  retrying, because the next attempt destroys the evidence. `--repair` then
  rewrites only the affected sectors instead of re-rolling the whole transfer.
- `--resync` recovers a desynced device without a power cycle. Desync is *the*
  documented failure mode — the protocol has no framing and the device has no
  inter-byte timeout, so one lost byte leaves it consuming commands as payload.
