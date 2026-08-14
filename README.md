# aiolumagen

Async Python library for the Lumagen Radiance Pro RS-232 protocol.

The library is protocol-only — no Home Assistant imports, no UI, no opinion about how the state should be surfaced. The [`ha-lumagen`](../ha-lumagen) custom integration consumes it.

Transport is handled by [`serialx`](https://github.com/puddly/serialx), so aiolumagen talks to a Lumagen over whatever URL scheme serialx supports — direct USB/RS-232, raw TCP (ser2net), or an ESPHome `serial_proxy` (see [`esphome-lumagen`](../esphome-lumagen)).

> **Formerly `pylumagen`.** This project was renamed to `aiolumagen` (see [Status](#status)) to avoid a naming collision with an unrelated `pylumagen` package already published on PyPI.

## Requirements

- Python 3.14+
- `serialx >= 1.7`

### Installing for `esphome://` URLs

The ESPHome serial-proxy transport additionally needs `aioesphomeapi`, which
is **not** a direct dependency here:

```bash
pip install aiolumagen[esphome]      # standalone use
pip install aiolumagen               # inside Home Assistant
```

Home Assistant already ships `aioesphomeapi` — pinned exactly — for its own
ESPHome integration, and installs custom-integration requirements into the
same site-packages. If aiolumagen also declared it (at a necessarily looser
range), the resolver could move HA's pinned version out from under the
running ESPHome integration; because it's a Cython package, that leaves
mismatched `.so` files behind and breaks it with errors like
`APIConnection size changed` or `does not export expected C function
make_noise_packets`. So HA stays the single owner of that pin.

Nothing is lost by the split: serialx imports `aioesphomeapi` lazily per URL
scheme, so direct-serial and `socket://` URLs never touch it, and an
`esphome://` URL inside HA implies the ESPHome integration is set up — which
guarantees the package is there. Outside HA without the extra, `connect()`
raises `LumagenConnectionError` naming what to install.

## Quick start

```python
import asyncio
from aiolumagen import LumagenClient, LumagenTransport


async def main():
    transport = LumagenTransport("esphome://10.0.0.42:6053/?port_name=Lumagen&key=<base64-psk>")
    client = LumagenClient(transport)

    def on_update(state, codes):
        print(state)

    client.subscribe(on_update)
    await client.start()
    await asyncio.sleep(60)
    await client.stop()


asyncio.run(main())
```

Or via the included script:

```bash
uv run python examples/via_url.py \\
    'esphome://10.105.1.42:6053/?port_name=Lumagen&key=<base64-psk>'
```

## Architecture

```
LumagenClient
├── LumagenProtocol       # line buffer + ! scan + CSV split + state model
└── LumagenTransport      # thin wrapper over serialx.create_serial_connection

aiolumagen.firmware       # firmware updates — separate protocol, opt-in import
├── container / extract   # 0xBABABEBE containers, vendor EXE parsing
├── protocol / plan       # updater commands, and what needs flashing
└── session               # the only part that does I/O
```

The **protocol** is pure sync code — no I/O, easy to test with recorded bytes. The **transport** is deliberately thin: it opens a serialx URL and pipes bytes to a callback. The **client** composes the two, runs the startup handshake (`ZE2` + initial queries), and owns a background poll loop.

The **firmware** subsystem is independent of all three and never engaged by `LumagenClient` — see [Firmware updates](#firmware-updates).

## State model

`LumagenState` is a slotted dataclass with typed fields for every documented status value — `power_on`, `current_input`, `source_resolution`, `is_hdr`, `colorspace` (enum: `Rec.601`/`709`/`2020`/`2100`), etc. It implements `__eq__` so an HA `DataUpdateCoordinator` can run with `always_update=False` and skip redundant writes.

Fields remain `None` until the corresponding response has been seen at least once — the Lumagen sends partial updates and the library merges them into one authoritative state snapshot.

## Lumagen-side prerequisite: enabling unsolicited reporting

For real-time status pushes (rather than just polling), configure the Lumagen to emit reports when state changes. **Full v5** pushes power transitions and memory swaps in addition to v4's coverage, so HA entities update without any post-command polling:

1. On the Lumagen remote or OSD, press `MENU`.
2. Navigate: **Other → I/O Setup → RS-232 Setup → Report mode changes**.
3. Cycle to **Full v5**.
4. Press `OK` to confirm, then `SAVE` to persist.

This writes to the Lumagen's NVRAM and persists across reboots. Without it, the library still works — it just relies on its polling loop to catch state changes.

If the reporting mode is left at **Full v4**, pushes still parse (the `!I21`–`!I24` handlers are retained) — you simply don't get power and memory changes in real time.

## Firmware requirement: Full v5

`ZQI25` (Full v5 status) is the only status query this library issues, so firmware old enough not to implement it is **not supported**. Such a device won't error: per Lumagen's protocol, any syntactically valid `ZQ` code is answered with an empty payload, so status fields would silently stay unset. The startup handshake detects this — a device that answers `ZQS01` but never `ZQI25` gets a warning naming the requirement — but the fix is a firmware update.

## Firmware updates

> ## ⚠️ Use at your own risk
>
> Flashing firmware can render a device unusable. This is an independent,
> reverse-engineered implementation with **no association with or endorsement by
> Lumagen, Inc.**, and it comes with no warranty. If you are not prepared to
> recover the unit yourself, use the vendor's own updater.
>
> Some deliberate limits reduce — but do not eliminate — the risk:
>
> **The bootloader is never written.** Flash from `0x0` to `0x20000` is left
> untouched on every path. `section0` images are written with that first sector
> stripped precisely so the bootloader survives, which is what keeps the vendor's
> own updater available as a recovery route. The library also *refuses* to run
> when the device is already in bootloader mode (`H0` → `Ok`) rather than
> attempting that unverified, brick-capable path.
>
> **HDMI chip firmware is never written.** `hdmi_rx`, `hdmi_tx` and `hdmi_ntx` are
> extracted and reported, never flashed. They were byte-identical across the five
> releases sampled during development, and no observed vendor session writes them
> in Auto mode — so there was nothing available to test a write path against.
> Lumagen publishes many more releases than those five; a bundle that *does* change
> them may well exist. This is a limit of what was sampled, not a claim about the
> firmware line as a whole.
>
> **Only tested on a Lumagen Radiance Pro** (a 4242, over an ESPHome
> `serial_proxy` at 230400 baud). Other Radiance models are not merely untested:
> the planner *refuses* any device that doesn't report the Radiance Pro device id
> `0x16`, because an updater's images are not valid for a different model.
>
> **A failed update is usually recoverable, and the order matters.** Read
> [When the unit powers down](#when-the-unit-powers-down) before power-cycling a
> unit after a failure — retrying the update first is normally the right move.

`aiolumagen.firmware` updates a Radiance Pro's firmware from the vendor's own
Windows updater `.exe`, over any transport the library supports — including the
ESPHome `serial_proxy`. It is **hardware-validated**; see
[Validation status](#validation-status) for exactly what has and hasn't been
exercised on a real device.

```python
from aiolumagen.firmware import update_firmware

result = await update_firmware(
    "esphome://10.0.0.42:6053/?port_name=Lumagen&key=<base64-psk>",
    "radiance_pro030326.exe",
    progress=lambda p: print(p.phase, p.message),
)
print(result.written)  # ('section1', 'section0')
```

It extracts the firmware images from the EXE, asks the device what it currently
holds, works out which sections actually differ, and writes only those. Pass
`dry_run=True` first to get the plan without changing anything — worth doing,
because the answer varies by roughly a factor of four:

```python
plan = (await update_firmware(url, exe, dry_run=True)).plan
print(plan.describe())
print(plan.estimated_seconds(230400) / 60, "minutes")
```

### What gets written

| Image | Decision |
|---|---|
| **section 0** (main CPU firmware) | **Always written.** Changes every release, and it stages to a scratch region — so a redundant write costs about a minute and risks nothing. |
| **section 1** | **Only when the live A/B slot demonstrably differs**, by size or by the device's own checksum (tag-corrected). |
| **HDMI chip images** | **Never.** Extracted and reported only — unchanged across every release sampled here, so nothing was available to test a write path against. See the warning above. |

Measured on hardware, this is the difference between a **1.8 min** update and a
**7.8 min** one — the same command, two correct decisions:

```
112325 -> 120325   section 1 differs  ->  884 blocks, 7.8 min
120325 -> 030326   section 1 matches  ->  113 blocks, 1.8 min
```

Section 1 is written **before** section 0, so the reboot that follows loads a
matched pair from one release rather than a new section 1 against old CPU
firmware.

### When the unit powers down

A successful update ends with `Z97`, which powers the unit off. That's the
device's own behaviour and how the newly written firmware gets loaded — the
Lumagen does *not* power itself down when a copy completes.

The power-down is owed to **anything that needs a reboot to take effect**, not
just to a promotion:

| Outcome | `promoted` | `powered_down` | exit |
|---|---|---|---|
| section 0 written and promoted | ✅ | ✅ | `Z97` |
| section 1 committed (no promotion step exists) | ❌ | ✅ | `Z97` |
| section 0 staged with `promote=False` | ❌ | ❌ | `X` |
| dry run / already up to date | ❌ | ❌ | `X` |
| **failed after section 1 committed** | ❌ | ❌ | `X` + warning |

That last row matters. A committed section-1 slot is elected at the *next* boot,
so after a partial update the unit is still running its old, self-consistent
firmware. Leaving it powered on keeps it that way and reachable for a retry that
can finish section 0 and *then* reboot with a matched pair. **If an update fails,
re-run it — don't power-cycle first.**

### Overriding the plan

Two escape hatches, for deliberate re-flashes, recovery, and testing:

```python
# Rewrite regardless of what the device already holds.
await update_firmware(url, exe, force=True)

# Flash one section only.
await update_firmware(url, exe, only=["section1"])

# Both: rewrite exactly one section, unconditionally.
await update_firmware(url, exe, only=["section0"], force=True)
```

`force` overrides **exactly one thing** — the "does the device already have
this?" comparison. It deliberately does *not* relax any correctness gate:

- an unrecoverable `swdata` descriptor (section 0's length would be guessed)
- a device that isn't a Radiance Pro
- container checksums, validated during extraction
- `Z35` cross-checking and the header-last commit, both in the session

So `force` makes an update *unconditional*, never *unchecked*. If a correctness
gate refuses, it is telling you something real.

`only` accepts `"section0"` and/or `"section1"`. Chip images are rejected with an
explanation rather than a generic error, and an unwritable or misspelled name
fails loudly instead of quietly planning nothing. An overridden plan reports
itself — `plan.overridden`, and `describe()` says so — because "no update needed"
and "you told me to write this anyway" should never look the same to a user.

### Auditing and repair

A whole-region checksum tells you *whether* a write landed. An audit tells you
*where* it didn't:

```python
async with FirmwareSession(url) as session:
    await session.preflight()
    result = await session.audit(image.wire_bytes, 0xB00000)
    print(result.describe())
    if not result.ok:
        result = await session.repair(image.wire_bytes, 0xB00000)
```

`audit()` is read-only, so it's safe against a region left half-written by a
failed run — which matters, because that evidence is destroyed by the next
attempt. It sums coarse chunks (one erase sector each) and subdivides only the
ones that disagree, so surveying 3 MB costs ~25 checksums plus ~32 per bad chunk
rather than 772.

`AuditResult` separates bad blocks that read back **erased** (the write never
arrived — a flow-control problem) from ones with **wrong content** (bytes arrived
corrupted — a framing problem), and collapses them into contiguous runs, because
one long run reads very differently from scattered singles.

`repair()` erases and rewrites only the sectors containing bad blocks, then
re-audits. The sector is the unit because NOR programming only clears bits — a
written block can't be patched in place — and erasing a sector destroys the good
blocks sharing it, so all of them go back down. It's for **uncommitted** regions
only; a damaged committed slot should be re-staged rather than patched.

Auditing a *committed* section-1 slot needs `stamped_tag=` (from
`read_container_header()`), or block 0 reports a false mismatch — the same
device-stamped tag that the planner's comparison has to correct for.

### Command-line tool

`examples/update_firmware.py` exposes all of it, and is the harness used for
on-device qualification.

```bash
uv run python examples/update_firmware.py <url> [updater.exe] [options]
```

| Flag | Effect |
|---|---|
| *(none)* | The real thing: plan, then write what differs |
| `--dry-run` | Read the device, print the plan, write nothing |
| `--offline` | Parse the EXE and report its contents; never connects |
| `--status` | Read-only device + flash inspection: identity, `Z35`, both A/B slot headers and generations, scratch and live heads |
| `--only SECTION` | Restrict to `section0` and/or `section1`; repeatable |
| `--force` | Write even if the device already holds the image |
| `--no-promote` | Stage section 0 to scratch and verify, but don't copy it over live firmware |
| `--audit` | Read-only: locate which blocks of a region differ from the image |
| `--repair` | Audit, then erase and rewrite only the sectors containing bad blocks |
| `--base ADDR` | Region to audit/repair, e.g. `0x20000`, `0xB00000`, `0xC00000` |
| `--audit-chunk N` | Blocks per coarse audit chunk (default 32 = one sector) |
| `--header-last` | Treat block 0 as deliberately unwritten (a staged, uncommitted slot) |
| `--resync` | Recover a desynced device without a power cycle |
| `--baudrate N` | Transfer rate (default 230400). Also the rate `--resync` opens at |
| `--verbose` | Debug logging |

```bash
# Inspect an EXE without connecting to anything
uv run python examples/update_firmware.py <url> updater.exe --offline

# Read-only device + flash inspection
uv run python examples/update_firmware.py <url> --status

# Full write path into scratch — live firmware untouched, repeatable
uv run python examples/update_firmware.py <url> updater.exe --only section0 --no-promote

# Locate damage block by block, then rewrite only the affected sectors
uv run python examples/update_firmware.py <url> updater.exe --only section0 --audit
uv run python examples/update_firmware.py <url> updater.exe --only section0 --repair

# Audit a committed section-1 slot (Z35 flips away from it once committed,
# so the address has to be explicit — --status prints both)
uv run python examples/update_firmware.py <url> updater.exe \
    --only section1 --audit --base 0xC00000

# Recover a desynced device without a power cycle
uv run python examples/update_firmware.py <url> --resync --baudrate 230400
```

**`--status` and `--audit` ignore `--baudrate`** and use 9600, the rate the
device always listens on at power-up. `--baudrate` is for bulk transfers, and for
`--resync` (where it must match the rate the failed run had negotiated).

The module docstring documents the escalation order used to qualify this on
hardware: read-only inspection, then scratch-only writes with an audit after
each, then a promotion, and section 1 last.

### Before you use it

- **`serial_proxy` serves one subscriber at a time.** Disconnect any
  `LumagenClient` on the same bridge first; in Home Assistant, unload the config
  entry.
- **Percent-encode the PSK.** ESPHome noise keys are base64 and routinely contain
  `+`, which a URL query string decodes as a *space* — corrupting the key while
  preserving its length, and surfacing as aioesphomeapi's misleading
  `Malformed PSK (length=44)`. Use `urllib.parse.quote(key, safe="")` when
  building the URL.
- **Bootloader mode is refused.** `H0` answering `Ok` aborts preflight. That path
  uses a different block size, is unverified, and is brick-capable; use the
  vendor's updater for bootloader recovery.
- **It's a separate import** — `aiolumagen.firmware`, never engaged by
  `LumagenClient`. It speaks a different protocol and is the only part of the
  library that can leave a device unbootable.

### Validation status

The protocol was recovered by reverse-engineering the vendor updater, then this
implementation was qualified on a **Lumagen Radiance Pro 4242** over an ESPHome
`serial_proxy` at 230400 baud. That is the only hardware it has ever run against,
and the only model the planner will accept.

**Exercised on hardware:**

- Extraction from five updater releases (`030225`, `092025`, `112325`, `120325`,
  `030326`), byte-identical to the reference implementation for both payload and
  wire form. These are the releases that happened to span the test unit's starting
  firmware through the then-current one — **not** a survey of Lumagen's catalogue,
  which is considerably larger
- Section 0: repeated writes to scratch, plus promotions (`G39` → `Z97`)
- Section 1: 772-block writes into a live A/B slot, header-last commit
- A genuine two-release upgrade chain — one where section 1 changed and one where
  it was correctly skipped
- Block-level audit of 113- and 772-block regions, including a committed slot
  with tag correction
- The flush barrier: `flush mode: status`, **zero retries across ~1,900 blocks**

**Not exercised on hardware** (unit-tested only):

- `repair()` — no run ever produced damage to repair
- The flush-barrier **retry** path. With `flush_timeout: 1s` on the bridge, a
  4096-byte block at 230400 drains inside the ESP's budget, so `TIMEOUT` never
  occurs. It *is* the normal path on a bridge left at the 100 ms default
- Rates other than 230400
- Bootloader mode (deliberately refused) and chip-image writes (out of scope)
- Any Radiance model other than the Pro, and any Pro other than a 4242

## Exception mapping (for Home Assistant integrators)

| Library exception | Suggested HA mapping |
|---|---|
| `LumagenConnectionError` | `ConfigEntryNotReady` from `async_setup_entry`, or mark device unavailable from the coordinator |
| `LumagenCommandError` | Log a warning; don't surface to the user. Also subclasses `ValueError`, so existing `except ValueError` handlers still catch it |
| `LumagenFirmwareImageError` | The user's file is unusable, and no device was contacted. Report it as a bad input. Also subclasses `ValueError` |
| `LumagenFirmwareAbortError` | A safety gate refused, or the update stopped before committing. **Live firmware is unchanged** — say so, and offer a retry |
| `LumagenFirmwareError` | Base class for the two above, and what an unconfirmable outcome raises. Surface the message rather than a generic failure |

All three firmware exceptions are importable from the package root, so catching
them doesn't require importing the firmware subsystem.

`LumagenFirmwareAbortError` is the *good* failure and worth distinguishing in a
UI: it guarantees live firmware is unchanged and a power cycle is the entire
recovery. Bare `LumagenFirmwareError` does not — read its message before
power-cycling, because it also covers outcomes that could not be confirmed.

No `LumagenAuthError` — the Lumagen itself has no authentication. Any transport-layer auth errors (e.g. wrong ESPHome PSK) surface as `LumagenConnectionError` via serialx.

No timeout exception either: nothing in the client is request/response, so there's no outstanding request for a deadline to apply to. Impose your own with `asyncio.timeout` and catch the builtin `TimeoutError` (this is what `ha-lumagen`'s config flow does while waiting for `!S01`).

The firmware subsystem is the one documented exception, and it doesn't change that rule. A firmware session *is* request/response — `C` returns a checksum, an erase streams a token per sector — so a reply that never arrives is a real failure with a real deadline. Rather than adding a general timeout exception only one subsystem could raise, those deadlines surface as `LumagenFirmwareError` naming what was being waited on.

## Development

```bash
uv sync                      # install deps + dev tools
uv sync --extra esphome      # ...plus aioesphomeapi, to exercise esphome:// URLs
uv run pytest                # run tests
uv run ruff check .          # lint
uv run mypy src              # type check
```

The test suite needs no extras — it drives a fake transport, never a real
serial port. `--extra esphome` is only for pointing `examples/via_url.py` at
a live ESPHome bridge.

## Status

Alpha / prototype. Not yet published to PyPI — install from git:

```
aiolumagen @ git+https://github.com/frankrosano/aiolumagen.git@v0.8.0
```

The normal-mode client is safe to experiment with. **Firmware updating is not** —
read the warning at the top of [Firmware updates](#firmware-updates) first. This
project is independent of Lumagen, Inc. and carries no warranty.

## License

MIT. See [`LICENSE`](LICENSE).
