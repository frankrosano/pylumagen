"""Update a Lumagen Radiance Pro's firmware from a vendor updater EXE.

This is the on-device test harness for ``aiolumagen.firmware``, and a usable
tool in its own right.

    uv run python examples/update_firmware.py <url> <updater.exe> [options]

**Read this before the first run on real hardware.** The stages below escalate
in consequence, and each is a superset of the one before it. Do them in order;
the first three cannot damage the unit.

1. ``--offline`` then ``--status`` — parse the EXE, then read the device. No
   writes at all. ``--status`` shows both A/B slots and their generations, which
   is how you tell afterwards what actually changed.
2. ``--dry-run`` — reads the device, prints the plan, writes nothing.
3. ``--only section0 --no-promote`` — the full erase/write/verify path into the
   scratch region. Live firmware is untouched, so this is repeatable and free.
   **This is the run to repeat**, and to repeat at each baud rate you care
   about. It exercises everything except the promotion.
4. ``--only section0 --audit`` — confirm the staged region block by block, as a
   second and independent check on the whole-region checksum the write already
   did. Read-only. If it finds damage, ``--repair`` rewrites just the affected
   sectors.
5. ``--only section0 --no-promote --baudrate 9600`` then 57600, then 230400 —
   one variable at a time, auditing after each. A rate that fails here would
   have failed a real update.
6. ``--only section0`` — as above, then promotes. First irreversible step. The
   unit powers itself down when the internal copy completes; that is correct.
7. ``--only section1`` — writes a live A/B slot. Protected by the header-last
   commit (an abort leaves the old slot bootable), but there is no scratch area
   and no undo once the header lands. Do this last. Audit it afterwards with
   ``--audit --base <slot address>``; ``--status`` reports the addresses, and
   note ``Z35`` flips away from a slot once it is committed, which is why the
   address has to be explicit.
8. No flags — the real thing: plan, then write what differs.

Notes:

* ``serial_proxy`` serves one subscriber at a time. Disconnect Home Assistant
  (or any other client) from the bridge first, or the connection will fail.
* Section 1 is ~7x the bytes of section 0, so it is the test that finds
  marginal links. A setting is not qualified until it has survived it.
* ``--force`` bypasses the "does the device already have this?" comparison. It
  does not bypass correctness checks; if one of those refuses, it is telling you
  something real.
* ``--audit`` is read-only and safe at any time, *including* against a region
  left half-written by a failed run. Do it before retrying — the evidence is
  destroyed by the next attempt.
* If a run desyncs (the device goes silent mid-transfer), ``--resync`` recovers
  it without a power cycle. Match ``--baudrate`` to the rate it failed at.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from aiolumagen.exceptions import (
    LumagenConnectionError,
    LumagenFirmwareAbortError,
    LumagenFirmwareError,
)
from aiolumagen.firmware import (
    DEFAULT_UPDATE_BAUDRATE,
    WRITABLE_SECTIONS,
    FirmwareSession,
    UpdatePhase,
    UpdateProgress,
    load_updater,
    plan_update,
    update_firmware,
)
from aiolumagen.firmware.extract import SECTION0
from aiolumagen.firmware.protocol import (
    ADDR_LIVE,
    AUDIT_CHUNK_BLOCKS,
    SCRATCH_ADDR,
    SECTION1_SLOTS,
    SECTOR_SIZE,
    SESSION_BAUD,
    SUPPORTED_BAUDS,
    pad_to_even_end,
)


class ProgressPrinter:
    """Prints progress, throttling the per-block flood to something readable."""

    def __init__(self, interval: float = 5.0) -> None:
        self._interval = interval
        self._last = 0.0
        self._started = time.monotonic()
        self._phase: UpdatePhase | None = None

    def __call__(self, progress: UpdateProgress) -> None:
        now = time.monotonic()
        elapsed = now - self._started
        final_block = progress.bytes_total and progress.bytes_done >= progress.bytes_total

        # Always announce a phase change; otherwise throttle.
        if progress.phase is not self._phase:
            self._phase = progress.phase
            print(f"\n[{elapsed:7.1f}s] == {progress.phase.upper()} ==", flush=True)
        elif progress.bytes_total and not final_block and now - self._last < self._interval:
            return
        self._last = now

        where = f"{progress.section}: " if progress.section else ""
        if not progress.bytes_total:
            print(f"[{elapsed:7.1f}s] {where}{progress.message}", flush=True)
            return

        done, total = progress.bytes_done, progress.bytes_total
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / rate / 60 if rate > 0 else 0.0
        print(
            f"[{elapsed:7.1f}s] {where}{100.0 * done / total:5.1f}%  "
            f"{done:,}/{total:,} bytes  {rate / 1024:.1f} KiB/s  eta {eta:.1f} min",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update a Lumagen Radiance Pro's firmware.",
        epilog="See the module docstring for the recommended on-device test order.",
    )
    parser.add_argument(
        "url", help="serialx URL, e.g. esphome://host:6053/?port_name=Lumagen&key=…"
    )
    parser.add_argument(
        "exe",
        type=Path,
        nargs="?",
        help="vendor updater .exe (not needed for --status or --resync)",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=DEFAULT_UPDATE_BAUDRATE,
        choices=SUPPORTED_BAUDS,
        help=f"rate to negotiate for a bulk TRANSFER (default: {DEFAULT_UPDATE_BAUDRATE}). "
        f"Also the rate --resync opens at, to match where a failed run left the "
        f"device. --status and --audit ignore it and use {SESSION_BAUD}, the rate the "
        f"device always listens on at power-up.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read the device, print the plan, write nothing",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="stage section 0 to scratch and verify it, but do NOT copy it over "
        "live firmware. The safest way to exercise the full write path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write even if the device already holds the image (bypasses the "
        "up-to-date comparison only, never a correctness check)",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=list(WRITABLE_SECTIONS),
        metavar="SECTION",
        help=f"restrict to a section ({'/'.join(WRITABLE_SECTIONS)}); repeatable",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="parse the EXE and print what it contains, without connecting",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="read-only: identity, flash layout, both A/B slot headers and "
        "generations, and the scratch region. Writes nothing. Run before and "
        "after every test.",
    )
    parser.add_argument(
        "--resync",
        action="store_true",
        help="recover a desynced device without a power cycle: pad a block of 'e' "
        "bytes to run its payload counter out. Use --baudrate to match the rate "
        "the run desynced at.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="read-only: locate exactly which blocks of a region disagree with the "
        "image, by coarse-then-fine device checksums. Writes nothing. Pair with "
        "--only to choose the image and --base to choose the region.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="audit, then erase and rewrite ONLY the sectors containing bad blocks, "
        "then re-audit. For uncommitted regions (scratch, or a slot whose header "
        "is still withheld).",
    )
    parser.add_argument(
        "--base",
        type=lambda v: int(v, 0),
        metavar="ADDR",
        help="region to audit/repair, e.g. 0xB00000 (scratch), 0x20000 (live "
        "section 0), 0x100000 / 0xC00000 (section-1 slots). Defaults to where "
        "the chosen section would be written. Needed to audit a slot you just "
        "committed, because Z35 flips away from it.",
    )
    parser.add_argument(
        "--audit-chunk",
        type=int,
        default=AUDIT_CHUNK_BLOCKS,
        metavar="N",
        help=f"blocks per coarse audit chunk (default: {AUDIT_CHUNK_BLOCKS}, one sector)",
    )
    parser.add_argument(
        "--header-last",
        action="store_true",
        help="with --audit/--repair: treat block 0 as deliberately unwritten (a "
        "section-1 slot staged but not yet committed)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser


def _audit_target(args: argparse.Namespace, bundle: object) -> tuple[str, int]:
    """Resolve which image and which address an audit/repair should operate on."""
    sections = args.only or [SECTION0]
    if len(sections) != 1:
        raise SystemExit("--audit/--repair need exactly one --only section")
    name = sections[0]
    if args.base is not None:
        return name, args.base
    # Sensible defaults: where that section would have been written.
    if name == SECTION0:
        return name, SCRATCH_ADDR
    raise SystemExit(
        "auditing section1 needs an explicit --base (the slot address). Run "
        "--status to see both slots; Z35 names the next write target, which is "
        "NOT the slot you just committed."
    )


async def run_audit(args: argparse.Namespace) -> int:
    """Audit — or audit-then-repair — a flash region against an extracted image."""
    bundle = await load_updater(args.exe)
    name, base = _audit_target(args, bundle)
    image = bundle.images.get(name)
    if image is None:
        print(f"error: this updater has no {name} image", file=sys.stderr)
        return 2
    wire = pad_to_even_end(base, image.wire_bytes)

    # SESSION_BAUD, not --baudrate. An audit issues only tiny commands: the
    # checksums are computed on the device and each reply is 11 bytes, so a faster
    # link buys nothing here and only adds a way to fail.
    async with FirmwareSession(args.url, baudrate=SESSION_BAUD) as session:
        await session.preflight()

        # A committed container slot carries a device-stamped tag at +4, so its
        # block 0 can never match the file. Detect that and correct for it rather
        # than reporting a spurious mismatch on byte-perfect firmware.
        stamped: int | None = None
        if image.container is not None and not args.header_last:
            header = await session.read_container_header(base)
            if header is not None and header.committed:
                stamped = header.tag
                print(
                    f"note: {base:#08x} is a committed slot (tag {header.tag:#010x}, "
                    f"generation {header.generation}); correcting block 0 for it"
                )

        print(
            f"\nauditing {name} ({len(wire):,} bytes) at {base:#08x} "
            f"in chunks of {args.audit_chunk} blocks"
        )
        result = await session.audit(
            wire,
            base,
            chunk_blocks=args.audit_chunk,
            skip_block0=args.header_last,
            stamped_tag=stamped,
        )
        print(f"\n{result.describe()}")

        if result.ok or not args.repair:
            return 0 if result.ok else 1

        if stamped is not None:
            print(
                "\nrefusing to repair a committed slot: erasing the header's sector "
                "would un-commit it, and rewriting block 0 from the file would "
                "restore an unstamped header. Re-stage the section instead.",
                file=sys.stderr,
            )
            return 1

        print(
            f"\nrepairing {len(result.bad_sectors)} sector(s) "
            f"({len(result.bad_sectors) * SECTOR_SIZE // 1024} KiB) instead of "
            f"{len(wire) // 1024} KiB"
        )
        after = await session.repair(
            wire, base, chunk_blocks=args.audit_chunk, skip_block0=args.header_last
        )
        print(f"\n{after.describe()}")
        if not after.ok:
            print(
                "\nrepair did not converge. Re-run it, or re-stage at a lower "
                "--baudrate. A header-last slot is unaffected either way — its "
                "header is still unwritten, so the device boots the other slot.",
                file=sys.stderr,
            )
            return 1
        if args.header_last:
            print("\nthe region matches but is NOT committed: block 0 is still erased.")
        return 0


async def show_status(url: str, baudrate: int) -> int:
    """Read-only device and flash inspection. Leaves via ``X``, so power stays on."""
    async with FirmwareSession(url, baudrate=baudrate) as session:
        identity = await session.preflight()
        print(f"device      : {identity}")
        print(f"model id    : {identity.device_id:#04x}  serial {identity.serial}")

        code = await session.flash_layout()
        target = SECTION1_SLOTS.get(code)
        print(f"\nZ35         : {code!r} -> next section-1 write targets ", end="")
        print(f"{target.address:#08x}" if target else "UNKNOWN SLOT")

        print(f"\n{'slot':<6} {'address':>10} {'magic':>10} {'tag':>12} {'gen':>6} {'size':>12}")
        for slot_code, slot in sorted(SECTION1_SLOTS.items()):
            header = await session.read_container_header(slot.address)
            role = "write" if slot_code == code else "LIVE"
            if header is None:
                print(f"{slot_code:<6} {slot.address:>#10x} {'(no reply)':>10}")
                continue
            generation = header.generation
            print(
                f"{slot_code:<6} {slot.address:>#10x} {header.magic:>#10x} "
                f"{header.tag:>#12x} {generation if generation is not None else '-':>6} "
                f"{header.size:>12,}  {role}"
            )

        scratch = await session.read_at(SCRATCH_ADDR, 16)
        print(f"\nscratch     : {SCRATCH_ADDR:#08x} {scratch.hex() or '(no reply)'}")
        live = await session.read_at(ADDR_LIVE, 16)
        print(f"live sect 0 : {ADDR_LIVE:#08x} {live.hex() or '(no reply)'}")
    return 0


async def run_resync(url: str, baudrate: int) -> int:
    """Run a desynced device's payload counter out with harmless ``e`` bytes.

    Deliberately skips preflight: a device mid-block is counting payload and will
    not answer ``M0931`` or a ping, so a preflight would fail before the recovery
    could run.
    """
    print(
        f"resyncing at {baudrate} baud — padding one block of 'e' bytes to run the "
        "device's payload counter out"
    )
    async with FirmwareSession(url, baudrate=baudrate) as session:
        if await session.resync():
            print("device answers 'Ok' again; it is back in command state")
            return 0
    print(
        "no response after padding. Power-cycle the Lumagen (disconnect AC, reconnect, power on).",
        file=sys.stderr,
    )
    return 1


async def show_offline(exe: Path) -> int:
    """Parse the EXE and report its contents without touching a device."""
    bundle = await load_updater(exe)
    print(f"updater : {exe.name}")
    print(f"release : {bundle.release or 'unknown (filename carries no MMDDYY)'}")
    print(f"\n{'image':<10} {'payload':>12} {'wire':>12} {'sum':>12}  source")
    for name in ("section0", "section1", "hdmi_rx", "hdmi_tx", "hdmi_ntx"):
        image = bundle.images.get(name)
        if image is None:
            print(f"{name:<10} {'-':>12} {'-':>12} {'-':>12}  (absent)")
            continue
        print(
            f"{name:<10} {image.size:>12,} {len(image.wire_bytes):>12,} "
            f"{image.checksum:>#12x}  {image.source}"
        )
    # No device, so the plan can only assume section 1 is needed.
    plan = plan_update(bundle)
    print(f"\nplan without a device (assumes section 1 is needed):\n{plan.describe()}")
    print(f"\nestimated at 230400: {plan.estimated_seconds(230400) / 60:.1f} min")
    return 0


async def run_update(args: argparse.Namespace) -> int:
    """The update path: plan, then write whatever the plan says."""
    if args.no_promote and args.only == ["section1"]:
        print(
            "note: --no-promote only affects section 0 (section 1 is written "
            "directly into its slot and has no promotion step).",
            file=sys.stderr,
        )

    started = time.monotonic()
    result = await update_firmware(
        args.url,
        args.exe,
        baudrate=args.baudrate,
        dry_run=args.dry_run,
        promote=not args.no_promote,
        force=args.force,
        only=args.only,
        progress=ProgressPrinter(),
    )
    elapsed = time.monotonic() - started
    print(f"\n{'=' * 68}")
    print(result.plan.describe())
    print(f"\nwritten      : {', '.join(result.written) or 'nothing'}")
    print(f"promoted     : {result.promoted}")
    print(f"powered down : {result.powered_down}")
    print(f"elapsed      : {elapsed / 60:.1f} min")
    print(f"flush mode   : {result.flush_mode or 'n/a'}")
    if result.flush_calls:
        # A high retry ratio means the ESP was consistently behind the host --
        # useful evidence when judging whether a rate is comfortable or marginal.
        share = 100.0 * result.flush_retries / result.flush_calls
        print(
            f"flush calls  : {result.flush_calls:,} "
            f"({result.flush_retries:,} retries, {share:.1f}%)"
        )
    for note in result.notes:
        print(f"note         : {note}")
    if result.powered_down:
        print(
            "\nThe unit was powered down with Z97, because what was written only "
            "takes effect on the next boot. Turn it back on and check the reported "
            "version."
        )
    return 0


async def dispatch(args: argparse.Namespace) -> int:
    """Pick the operation. Every path shares main()'s error handling."""
    if args.status and args.resync:
        print("error: --status and --resync are separate operations", file=sys.stderr)
        return 2
    if args.status:
        # SESSION_BAUD, not --baudrate: the device always listens at 9600 at
        # power-up, and a read-only inspection never negotiates anything else.
        # Opening at 230400 against a device at 9600 makes every command garbage.
        return await show_status(args.url, SESSION_BAUD)
    if args.resync:
        # --baudrate here on purpose: a desynced device is still at whatever rate
        # the failed run had negotiated.
        return await run_resync(args.url, args.baudrate)

    if args.exe is None:
        print("error: an updater .exe is required unless using --status/--resync", file=sys.stderr)
        return 2
    if not args.exe.is_file():
        print(f"error: {args.exe} is not a file", file=sys.stderr)
        return 2
    if args.offline:
        return await show_offline(args.exe)
    if args.audit or args.repair:
        return await run_audit(args)
    return await run_update(args)


async def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    try:
        return await dispatch(args)
    except LumagenFirmwareAbortError as err:
        # The good failure: live firmware is untouched.
        print(f"\nABORTED — nothing was changed:\n  {err}", file=sys.stderr)
        print(
            "\nThe device still boots exactly what it booted before. Power-cycle it "
            "and it will come up normally.",
            file=sys.stderr,
        )
        return 1
    except LumagenConnectionError as err:
        # Before LumagenFirmwareError: this is a sibling of it, not a subclass,
        # but ordering the narrow cases first keeps the intent obvious.
        print(f"\nCONNECTION FAILED:\n  {err}", file=sys.stderr)
        print(
            "\nIf this is an ESPHome bridge, check that nothing else is subscribed "
            "to the serial proxy — it serves one client at a time.",
            file=sys.stderr,
        )
        return 1
    except LumagenFirmwareError as err:
        print(f"\nFAILED:\n  {err}", file=sys.stderr)
        print(
            "\nRead the message above before power-cycling — it says whether the "
            "outcome was confirmed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
