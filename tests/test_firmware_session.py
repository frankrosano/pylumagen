"""Tests for the async firmware-update session.

Driven against ``FakeFirmwareTransport``, which models the device's updater-mode
protocol closely enough to run a whole update: sticky registers, byte-counted
payload, real checksums, real erases. So these are behavioural tests — "did the
right bytes land at the right address" — rather than call-sequence assertions.
"""

from __future__ import annotations

import pytest

from aiolumagen.exceptions import LumagenFirmwareAbortError, LumagenFirmwareError
from aiolumagen.firmware.container import additive_checksum, expected_stored_checksum
from aiolumagen.firmware.extract import (
    SECTION0,
    SECTION1,
    FirmwareBundle,
    extract_images,
)
from aiolumagen.firmware.plan import DeviceStatus, SectionAction, plan_update
from aiolumagen.firmware.protocol import (
    ADDR_LIVE,
    BLOCK_SIZE,
    BOOT_SECTOR_LEN,
    SCRATCH_ADDR,
    SCRATCH_SECTOR,
    SECTION1_SLOTS,
)
from aiolumagen.firmware.session import FirmwareSession, UpdatePhase, UpdateProgress
from tests.conftest import FakeFirmwareTransport, build_container, build_updater_exe

pytestmark = pytest.mark.usefixtures("no_delays")

SECTION1_PAYLOAD = b"section-one" * 1024


def make_bundle(*, source_name: str | None = None) -> FirmwareBundle:
    swdata = bytes(range(256)) * (BOOT_SECTOR_LEN // 256) + b"\xc3" * 0x2800
    return extract_images(
        build_updater_exe(swdata=swdata, resources={131: build_container(SECTION1_PAYLOAD)}),
        source_name=source_name,
    )


async def opened(transport: FakeFirmwareTransport, **kwargs: object) -> FirmwareSession:
    session = FirmwareSession(transport=transport, **kwargs)  # type: ignore[arg-type]
    await session.connect()
    return session


class TestPreflight:
    async def test_passes_on_a_healthy_device(self, fake_firmware: FakeFirmwareTransport) -> None:
        session = await opened(fake_firmware)
        identity = await session.preflight()
        assert identity.is_radiance_pro
        assert "M0931" in fake_firmware.commands
        assert session.entered_update_mode

    async def test_refuses_standby(self) -> None:
        """In standby the device ignores the updater command set entirely, so every
        later command would time out without saying why."""
        transport = FakeFirmwareTransport(power=False)
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="standby"):
            await session.preflight()
        assert "M0931" not in transport.commands

    async def test_refuses_bootloader_mode(self) -> None:
        """That path is unverified and brick-capable."""
        transport = FakeFirmwareTransport(bootloader_mode=True)
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="BOOTLOADER mode"):
            await session.preflight()

    async def test_refuses_when_no_scratch_region_is_available(self) -> None:
        transport = FakeFirmwareTransport(scratch_ok=False)
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="scratch-region probe"):
            await session.preflight()

    async def test_refuses_when_the_ping_goes_unanswered(self) -> None:
        transport = FakeFirmwareTransport(ping_answers=False)
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="no 'Ok' from the ping"):
            await session.preflight()

    async def test_refuses_an_empty_identify(self) -> None:
        transport = FakeFirmwareTransport(identity="")
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="identify"):
            await session.preflight()


class TestFraming:
    async def test_commands_go_first_character_then_remainder(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Mirrors the vendor byte for byte; it's the only framing ever validated
        against hardware."""
        session = await opened(fake_firmware)
        await session.send("A020000")
        assert fake_firmware.writes == [b"A", b"020000"]
        assert fake_firmware.commands == ["A020000"]

    async def test_single_character_commands_are_one_write(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        session = await opened(fake_firmware)
        await session.send("e")
        assert fake_firmware.writes == [b"e"]

    async def test_read_at_decodes_the_hex_reply(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        fake_firmware.memory[0x20000:0x20010] = bytes(range(16))
        session = await opened(fake_firmware)
        assert await session.read_at(0x20000, 16) == bytes(range(16))

    async def test_checksum_matches_the_devices_own_arithmetic(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        fake_firmware.memory[0x30000:0x30100] = bytes(range(256))
        session = await opened(fake_firmware)
        got = await session.device_checksum(0x30000, 256)
        assert got == additive_checksum(bytes(range(256)))


class TestFlushBarrier:
    async def test_barriers_once_per_chunk(self) -> None:
        transport = FakeFirmwareTransport()
        session = await opened(transport, chunk=BLOCK_SIZE)
        await session.write_raw(b"\x5a" * (BLOCK_SIZE * 3))
        assert len(transport.flush_calls) == 3
        assert session.flush_mode == "status"

    async def test_timeout_is_retried_not_fatal(self) -> None:
        """The ESP's own flush budget is shorter than a block's wire time, so it
        answers TIMEOUT while still draining. Re-issuing is the correct response."""
        transport = FakeFirmwareTransport(flush_statuses=["TIMEOUT", "TIMEOUT", "TIMEOUT", "OK"])
        session = await opened(transport, flush_retry_delay=0.0)
        assert await session.barrier(BLOCK_SIZE)
        assert len(transport.flush_calls) == 4

    async def test_endless_timeout_aborts(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A proxy that never drains must abort, not retry forever.

        Continuing would write into a queue that is silently discarding bytes,
        which is the one failure mode this whole mechanism exists to prevent.
        """

        async def _always_timeout(**_kwargs: object) -> str:
            return "TIMEOUT"

        monkeypatch.setattr(fake_firmware, "flush", _always_timeout)
        session = await opened(fake_firmware, flush_retry_delay=0.0, flush_timeout=0.02)
        with pytest.raises(LumagenFirmwareAbortError, match="did not finish flushing"):
            await session.barrier(BLOCK_SIZE)

    @pytest.mark.parametrize("status", ["ERROR", "NOT_SUPPORTED"])
    async def test_structural_failure_degrades_to_pacing(self, status: str) -> None:
        """A broken barrier must not abort a fourteen-minute transfer by itself."""
        transport = FakeFirmwareTransport(flush_statuses=[status])
        session = await opened(transport)
        assert not await session.barrier(BLOCK_SIZE)
        assert not session.use_flush
        # Subsequent writes proceed open-loop rather than raising.
        await session.write_raw(b"\x01" * 64)
        assert len(transport.flush_calls) == 1

    async def test_exception_degrades_to_pacing(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(**_kwargs: object) -> str:
            raise RuntimeError("proxy went away")

        monkeypatch.setattr(fake_firmware, "flush", _boom)
        session = await opened(fake_firmware)
        assert not await session.barrier(BLOCK_SIZE)
        assert not session.use_flush

    async def test_no_flush_available_falls_back_silently(self) -> None:
        transport = FakeFirmwareTransport(flush_mode="none")
        session = await opened(transport)
        assert not await session.barrier(BLOCK_SIZE)
        assert not session.use_flush
        assert transport.flush_calls == []

    async def test_use_flush_false_never_barriers(self) -> None:
        transport = FakeFirmwareTransport()
        session = await opened(transport, use_flush=False)
        await session.write_raw(b"\x00" * BLOCK_SIZE)
        assert transport.flush_calls == []

    async def test_chunk_is_capped_at_the_safe_maximum(self) -> None:
        """ESPHome's output pool discards anything that doesn't fit, telling nobody."""
        session = await opened(FakeFirmwareTransport(), chunk=0x10000)
        assert session.chunk == BLOCK_SIZE


class TestStageImage:
    async def test_writes_every_byte_to_the_right_address(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = bytes(range(256)) * 40  # 10,240 bytes: two full blocks plus a tail
        session = await opened(fake_firmware)
        await session.stage_image(image, SCRATCH_ADDR)
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image

    async def test_length_register_is_sticky(self, fake_firmware: FakeFirmwareTransport) -> None:
        """L is re-sent only when the block length changes, matching the capture."""
        session = await opened(fake_firmware)
        await session.stage_image(b"\x11" * (BLOCK_SIZE * 3 + 7), SCRATCH_ADDR)
        lengths = [c for c in fake_firmware.commands if c.startswith("L")]
        assert lengths == [f"L{BLOCK_SIZE:06X}", "L000007"]

    async def test_every_block_is_individually_addressed(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """This is what makes the header-last reordering possible at all."""
        session = await opened(fake_firmware)
        await session.stage_image(b"\x22" * (BLOCK_SIZE * 3), SCRATCH_ADDR)
        addresses = [c for c in fake_firmware.commands if c.startswith("A")]
        assert addresses == [
            f"A{SCRATCH_ADDR:06X}",
            f"A{SCRATCH_ADDR + BLOCK_SIZE:06X}",
            f"A{SCRATCH_ADDR + 2 * BLOCK_SIZE:06X}",
        ]

    async def test_header_last_is_a_pure_permutation(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Same bytes, same addresses, block 0 written last.

        If this were not an exact permutation, deferring the header would corrupt
        the image rather than just reorder it.
        """
        image = bytes(range(256)) * 40
        session = await opened(fake_firmware)
        await session.stage_image(image, SCRATCH_ADDR, header_last=True)
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image
        addresses = [c for c in fake_firmware.commands if c.startswith("A")]
        assert addresses[-1] == f"A{SCRATCH_ADDR:06X}"
        assert addresses[0] == f"A{SCRATCH_ADDR + BLOCK_SIZE:06X}"
        assert len(addresses) == len(set(addresses)) == 3

    async def test_header_last_handles_a_tiny_tail_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A real release ends section 0 in an 18-byte block."""
        image = b"\x5a" * (BLOCK_SIZE * 2 + 18)
        session = await opened(fake_firmware)
        await session.stage_image(image, SCRATCH_ADDR, header_last=True)
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image

    async def test_header_last_is_a_no_op_for_a_single_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        session = await opened(fake_firmware)
        await session.stage_image(b"\x99" * 100, SCRATCH_ADDR, header_last=True)
        assert fake_firmware.region(SCRATCH_ADDR, 100) == b"\x99" * 100

    async def test_before_header_runs_after_the_payload(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The commit hook must see the whole payload down and the header absent."""
        image = b"\xab" * (BLOCK_SIZE * 3)
        seen: dict[str, object] = {}

        async def hook() -> None:
            seen["payload"] = fake_firmware.region(SCRATCH_ADDR + BLOCK_SIZE, BLOCK_SIZE * 2)
            seen["header"] = fake_firmware.region(SCRATCH_ADDR, 4)

        session = await opened(fake_firmware)
        await session.stage_image(image, SCRATCH_ADDR, header_last=True, before_header=hook)
        assert seen["payload"] == b"\xab" * (BLOCK_SIZE * 2)
        assert seen["header"] == b"\xff\xff\xff\xff"  # still erased

    async def test_progress_is_reported_per_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        seen: list[tuple[int, int]] = []
        session = await opened(fake_firmware)
        await session.stage_image(
            b"\x00" * (BLOCK_SIZE * 2 + 5),
            SCRATCH_ADDR,
            on_block=lambda done, total: seen.append((done, total)),
        )
        assert seen == [
            (BLOCK_SIZE, BLOCK_SIZE * 2 + 5),
            (BLOCK_SIZE * 2, BLOCK_SIZE * 2 + 5),
            (BLOCK_SIZE * 2 + 5, BLOCK_SIZE * 2 + 5),
        ]


class TestEraseAndRates:
    async def test_erase_run_clears_the_sectors(self, fake_firmware: FakeFirmwareTransport) -> None:
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + 64] = b"\x00" * 64
        session = await opened(fake_firmware)
        await session.erase_run(SCRATCH_SECTOR, 2)
        assert fake_firmware.erases == [(SCRATCH_SECTOR, 2)]
        assert fake_firmware.region(SCRATCH_ADDR, 64) == b"\xff" * 64
        assert session.erased

    async def test_erase_command_is_the_recorded_byte_string(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        session = await opened(fake_firmware)
        await session.erase_run(SCRATCH_SECTOR, 1)
        assert "S7958a7" in fake_firmware.commands

    async def test_set_baud_sends_then_reconfigures_then_pings(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Order matters: B has to clear the wire at the OLD rate before switching."""
        session = await opened(fake_firmware)
        await session.set_baud(230400)
        assert fake_firmware.baud_commands == [230400]
        assert fake_firmware.baud_reconfigures == [230400]
        assert session.baudrate == 230400
        assert fake_firmware.commands.count("e") >= 5

    async def test_set_baud_aborts_when_pings_go_unanswered(self) -> None:
        transport = FakeFirmwareTransport(ping_answers=False)
        session = await opened(transport)
        with pytest.raises(LumagenFirmwareAbortError, match="unanswered at 230400"):
            await session.set_baud(230400)

    async def test_set_baud_is_a_no_op_at_the_current_rate(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        session = await opened(fake_firmware)
        await session.set_baud(9600)
        assert fake_firmware.baud_commands == []

    async def test_rate_check_reads_live_firmware_twice(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        fake_firmware.memory[ADDR_LIVE : ADDR_LIVE + 0x70000] = b"\x3c" * 0x70000
        session = await opened(fake_firmware)
        assert await session.rate_check()
        assert fake_firmware.commands.count("R") == 6  # three addresses, twice each

    async def test_rate_check_fails_when_reads_disagree(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Byte loss is stochastic, so it shows up as two reads disagreeing."""
        calls = {"n": 0}
        original = fake_firmware.region

        def flaky(addr: int, length: int) -> bytes:
            calls["n"] += 1
            data = original(addr, length)
            return data[:-1] + b"\x00" if calls["n"] % 2 == 0 else data

        monkeypatch.setattr(fake_firmware, "region", flaky)
        session = await opened(fake_firmware)
        assert not await session.rate_check()


class TestResync:
    async def test_pads_a_block_of_pings_and_confirms(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The vendor's own recovery: run the device's payload counter out with 'e'.

        Safe in both states — as payload it lands in a region about to be
        rewritten, as commands it's just pings.
        """
        session = await opened(fake_firmware)
        assert await session.resync(64)
        assert b"e" * 64 in b"".join(fake_firmware.writes)

    async def test_reports_failure_when_the_device_stays_silent(self) -> None:
        transport = FakeFirmwareTransport(ping_answers=False)
        session = await opened(transport)
        assert not await session.resync(16)


class TestReadStatus:
    async def test_reports_the_live_slot(self, fake_firmware: FakeFirmwareTransport) -> None:
        """Z35 names the slot to write; the LIVE slot is the other one."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        wire = section1.wire_bytes
        live = SECTION1_SLOTS["99"]  # Z35 == "00", so 99 is running
        fake_firmware.commit(live.address, wire, 0xFADE0011)

        session = await opened(fake_firmware)
        status = await session.read_status()
        assert status.section1_target_code == "00"
        assert status.section1_live_header is not None
        assert status.section1_live_header.tag == 0xFADE0011
        assert status.section1_live_checksum == expected_stored_checksum(wire, 0xFADE0011)

    async def test_feeds_a_skip_decision(self, fake_firmware: FakeFirmwareTransport) -> None:
        """End to end: a device already holding the image plans no section-1 write."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        session = await opened(fake_firmware)
        plan = plan_update(bundle, await session.read_status())
        entry = next(s for s in plan.sections if s.name == SECTION1)
        assert entry.action is SectionAction.SKIP

    async def test_erased_live_slot_yields_no_header(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        session = await opened(fake_firmware)
        status = await session.read_status()
        assert status.section1_live_header is None
        assert status.section1_live_checksum is None


class TestRunUpdate:
    async def test_dry_run_writes_nothing(self, fake_firmware: FakeFirmwareTransport) -> None:
        session = await opened(fake_firmware)
        result = await session.run_update(make_bundle(), dry_run=True)
        assert result.dry_run
        assert result.written == ()
        assert fake_firmware.erases == []
        assert fake_firmware.payload_writes == []
        assert not result.promoted

    async def test_stages_and_promotes_section0(self, fake_firmware: FakeFirmwareTransport) -> None:
        """The whole section-0 path: erase scratch, stage, verify, promote.

        Driven through the context manager so the hand-back is covered too — a
        promoted run must exit with Z97, because that power-down is how the newly
        promoted firmware gets loaded.
        """
        bundle = make_bundle()
        section0, section1 = bundle.section0, bundle.section1
        assert section0 is not None and section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle)

        wire = section0.wire_bytes
        assert result.written == (SECTION0,)
        assert result.promoted and result.powered_down
        assert fake_firmware.region(SCRATCH_ADDR, len(wire)) == wire
        assert fake_firmware.region(ADDR_LIVE, len(wire)) == wire
        assert fake_firmware.copies == [(ADDR_LIVE, SCRATCH_ADDR, len(wire))]
        assert fake_firmware.left == "Z97"
        assert fake_firmware.baud_reconfigures[-1] == 9600

    async def test_promote_false_stages_without_touching_live_firmware(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        bundle = make_bundle()
        section0, section1 = bundle.section0, bundle.section1
        assert section0 is not None and section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        session = await opened(fake_firmware)
        result = await session.run_update(bundle, promote=False)
        assert not result.promoted
        assert fake_firmware.copies == []
        assert fake_firmware.region(ADDR_LIVE, 16) == b"\xff" * 16
        assert any("NOT promoted" in n for n in result.notes)

    async def test_writes_section1_into_the_inactive_slot(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Section 1 goes to the slot Z35 names, never the running one."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        target, live = SECTION1_SLOTS["00"], SECTION1_SLOTS["99"]
        # The live slot holds something else, so section 1 must be written.
        fake_firmware.commit(live.address, build_container(b"old firmware" * 700), 0xFADE0005)

        session = await opened(fake_firmware)
        result = await session.run_update(bundle)

        wire = section1.wire_bytes
        assert SECTION1 in result.written
        assert fake_firmware.region(target.address, len(wire)) == wire
        assert fake_firmware.erases[0][0] == target.first_sector

    async def test_section1_powers_down_even_without_a_promotion(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A committed section-1 slot needs a reboot to be elected, so Z97 is right.

        Section 1 has no promotion step, but that is an implementation detail — the
        write is just as dormant as a promoted section 0 until the device restarts.
        Keying the power-down off ``promoted`` left the unit running with its new
        firmware inactive, which is the bug this pins.
        """
        bundle = make_bundle()
        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle, only=[SECTION1])

        assert result.written == (SECTION1,)
        assert not result.promoted  # no scratch-to-live copy happened
        assert result.powered_down  # but a restart is still required
        assert fake_firmware.left == "Z97"
        assert any("next powers on" in note for note in result.notes)

    async def test_staging_without_promoting_does_not_power_down(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Nothing boots from scratch, so there is nothing pending activation.

        The other half of the rule: powering the unit off here would interrupt the
        user for a write that changed nothing they are running.
        """
        bundle = make_bundle()
        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle, only=[SECTION0], promote=False)

        assert result.written == (SECTION0,)
        assert not result.promoted
        assert not result.powered_down
        assert fake_firmware.left == "X"

    async def test_dry_run_does_not_power_down(self, fake_firmware: FakeFirmwareTransport) -> None:
        bundle = make_bundle()
        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle, dry_run=True)
        assert not result.powered_down
        assert fake_firmware.left == "X"

    async def test_section1_is_written_before_section0(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Section 0 finishes with a promotion that powers the unit down, so it
        must go last."""
        fake_firmware.commit(
            SECTION1_SLOTS["99"].address, build_container(b"old" * 2000), 0xFADE0005
        )
        session = await opened(fake_firmware)
        result = await session.run_update(make_bundle())
        assert result.written == (SECTION1, SECTION0)

    async def test_reports_nothing_to_do_when_already_current(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section 0 is always written, so an empty plan needs a plan with no writes."""
        bundle = make_bundle()
        session = await opened(fake_firmware)
        empty = plan_update(bundle, DeviceStatus())
        monkeypatch.setattr(type(empty), "to_write", property(lambda _self: ()), raising=False)
        result = await session.run_update(bundle, plan=empty)
        assert result.written == ()
        assert any("already up to date" in n for n in result.notes)

    async def test_full_upgrade_powers_down_only_after_section0(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A both-sections upgrade must not power down between them.

        Section 1's commit sets ``requires_restart``, but the power-down belongs to
        session teardown, not to the section that set the flag. Powering down after
        section 1 would abandon section 0 entirely and leave the unit booting a new
        section 1 against old CPU firmware.
        """
        bundle = make_bundle()
        section0 = bundle.section0
        assert section0 is not None
        # Live slot holds a different section 1, so BOTH sections get planned.
        fake_firmware.commit(
            SECTION1_SLOTS["99"].address, build_container(b"old firmware" * 700), 0xFADE0005
        )

        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle)

        assert result.written == (SECTION1, SECTION0)
        commands = fake_firmware.commands
        assert commands.count("Z97") == 1, "the unit must be powered down exactly once"
        assert commands[-1] == "Z97", "the power-down must be the very last command"
        # G39 is section 0's promotion; it has to happen before the power-down.
        assert commands.index("G39") < commands.index("Z97")
        # And the last payload written was section 0's, into scratch.
        assert fake_firmware.payload_writes[-1][0] >= SCRATCH_ADDR
        assert fake_firmware.region(ADDR_LIVE, len(section0.wire_bytes)) == section0.wire_bytes

    async def test_updater_mode_is_entered_once_and_left_once(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A whole update is one continuous updater-mode session.

        No exiting and re-entering to write the commit header — one ``M0931`` at
        preflight, one ``X``/``Z97`` at the end, everything else in between. The
        phase labels once made it look otherwise, so pin the real behaviour.
        """
        bundle = make_bundle()
        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            await session.run_update(bundle, only=[SECTION1])

        commands = fake_firmware.commands
        assert commands.count("M0931") == 1
        assert commands.count("Z97") + commands.count("X") == 1
        assert commands[-1] in ("Z97", "X")  # the exit is the last thing sent

    async def test_commit_block_is_reported_as_committing(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The header block IS the commit, so it must not report as WRITING.

        Otherwise the phase sequence reads WRITING -> VERIFYING -> COMMITTING ->
        WRITING, which looks like a mode round-trip that never happens.
        """
        bundle = make_bundle()
        seen: list[UpdateProgress] = []
        session = await opened(fake_firmware)
        await session.run_update(bundle, only=[SECTION1], progress=seen.append)

        phases = [p.phase for p in seen]
        first_commit = phases.index(UpdatePhase.COMMITTING)
        assert UpdatePhase.WRITING not in phases[first_commit:], (
            f"phase sequence implies leaving and re-entering: {phases[first_commit:]}"
        )
        carrying = [p for p in seen if p.bytes_total]
        assert carrying[-1].phase is UpdatePhase.COMMITTING
        assert carrying[-1].fraction == 1.0

    async def test_progress_covers_the_phases(self, fake_firmware: FakeFirmwareTransport) -> None:
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        seen: list[UpdateProgress] = []
        session = await opened(fake_firmware)
        await session.run_update(bundle, progress=seen.append)

        phases = {p.phase for p in seen}
        assert {
            UpdatePhase.PREFLIGHT,
            UpdatePhase.PLANNING,
            UpdatePhase.ERASING,
            UpdatePhase.WRITING,
            UpdatePhase.VERIFYING,
            UpdatePhase.PROMOTING,
            UpdatePhase.DONE,
        } <= phases
        writing = [p for p in seen if p.phase is UpdatePhase.WRITING and p.bytes_total]
        assert writing[-1].fraction == 1.0

    async def test_aborts_when_the_link_loses_bytes_at_the_new_rate(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed rate check must stop before anything is erased."""
        session = await opened(fake_firmware)

        async def _bad_rate_check(*_a: object, **_k: object) -> bool:
            return False

        monkeypatch.setattr(session, "rate_check", _bad_rate_check)
        with pytest.raises(LumagenFirmwareAbortError, match="losing bytes"):
            await session.run_update(make_bundle())
        assert fake_firmware.erases == []

    async def test_verification_failure_aborts_before_promoting(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt the staged region behind the session's back; it must not promote."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)
        session = await opened(fake_firmware)

        real_stage = session.stage_image

        async def corrupting(*args: object, **kwargs: object) -> None:
            await real_stage(*args, **kwargs)  # type: ignore[arg-type]
            fake_firmware.memory[SCRATCH_ADDR + 32] ^= 0xFF

        monkeypatch.setattr(session, "stage_image", corrupting)
        with pytest.raises(LumagenFirmwareError, match="checksum mismatch"):
            await session.run_update(bundle)
        assert fake_firmware.copies == []


class TestHandBack:
    async def test_context_manager_restores_the_rate_and_leaves(self) -> None:
        """A device stranded in updater mode at 230400 answers nobody at 9600."""
        transport = FakeFirmwareTransport()
        async with FirmwareSession(transport=transport) as session:  # type: ignore[arg-type]
            await session.preflight()
            await session.set_baud(230400)
        assert transport.baud_reconfigures[-1] == 9600
        assert transport.left == "X"  # nothing promoted, so abort and stay powered

    async def test_rate_restore_renegotiates_with_the_device(self) -> None:
        """The device must be told about the rate change, not just our transport.

        Reconfiguring only the host would send the trailing ``X`` at 9600 to a
        device still listening at 230400. It would never leave updater mode, and
        the next client connecting at 9600 would see a device that answers
        nothing — indistinguishable from dead hardware.
        """
        transport = FakeFirmwareTransport()
        async with FirmwareSession(transport=transport) as session:  # type: ignore[arg-type]
            await session.preflight()
            await session.set_baud(230400)
        # B009600 on the wire, and it must precede the X.
        assert transport.baud_commands == [230400, 9600]
        assert transport.commands.index("B009600") < transport.commands.index("X")
        assert transport.baudrate == 9600

    async def test_promoted_path_does_not_renegotiate(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """After promotion the unit powers off on Z97 and returns at 9600 itself.

        So there is nothing to renegotiate — only our own side needs aligning, and
        pinging a unit that is switching off would just fail.
        """
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            result = await session.run_update(bundle)

        assert result.promoted
        assert fake_firmware.baud_commands == [230400]  # no B009600 to a dying device
        assert fake_firmware.baud_reconfigures[-1] == 9600  # our side realigned
        assert fake_firmware.left == "Z97"

    async def test_z97_is_sent_before_the_host_drops_to_9600(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Z97 must reach the device at the TRANSFER rate, not after we've moved.

        Regression test for a bug found on hardware. The device does not power
        itself down when the copy finishes — ``Z97`` is what powers it down. An
        earlier version realigned the host to 9600 first, so ``Z97`` went out at
        9600 to a device still listening at 230400: the unit stayed up in updater
        mode, and the garbage was partly parsed as commands, leaving it dumping
        flash. Ordering is the whole fix.
        """
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            await session.run_update(bundle)

        events = fake_firmware.events
        assert "Z97" in events, "the unit never received the command that powers it down"
        assert events.index("Z97") < events.index("host_baud=9600"), (
            "Z97 was sent after the host dropped to 9600 — the device is still at "
            "230400 at that point and would never receive it"
        )
        # And the last rate the device itself was told about is the transfer rate.
        assert fake_firmware.baud_commands == [230400]

    async def test_unpromoted_run_hands_back_at_the_session_rate(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The --no-promote testing path has to leave the device reachable."""
        bundle = make_bundle()
        async with FirmwareSession(transport=fake_firmware) as session:  # type: ignore[arg-type]
            await session.run_update(bundle, only=[SECTION0], promote=False)

        assert fake_firmware.baud_commands == [230400, 9600]
        assert fake_firmware.left == "X"
        assert fake_firmware.baudrate == 9600

    async def test_hand_back_runs_even_after_a_failure(self) -> None:
        transport = FakeFirmwareTransport()
        with pytest.raises(LumagenFirmwareAbortError):
            async with FirmwareSession(transport=transport) as session:  # type: ignore[arg-type]
                await session.preflight()
                raise LumagenFirmwareAbortError("simulated mid-run failure")
        assert transport.left == "X"

    async def test_a_failed_run_does_not_power_down_a_partial_update(self) -> None:
        """Section 1 committed, then section 0 failed: leave the unit POWERED ON.

        A committed slot is elected at the *next* boot, so the unit is still running
        old, self-consistent firmware. Staying up keeps it that way and reachable
        for a retry that can finish section 0 and then power down with a matched
        pair. Powering down instead would boot a new section 1 against old CPU
        firmware — a combination no vendor session produces.
        """
        transport = FakeFirmwareTransport()
        with pytest.raises(LumagenFirmwareAbortError):
            async with FirmwareSession(transport=transport) as session:  # type: ignore[arg-type]
                await session.preflight()
                session.requires_restart = True  # as a section-1 commit would set
                raise LumagenFirmwareAbortError("section 0 failed after section 1 committed")

        assert transport.left == "X", "a partial update must not trigger a reboot"
        assert "Z97" not in transport.commands

    async def test_a_successful_run_still_powers_down(self) -> None:
        """The control for the test above: no exception means the reboot happens."""
        transport = FakeFirmwareTransport()
        async with FirmwareSession(transport=transport) as session:  # type: ignore[arg-type]
            await session.preflight()
            session.requires_restart = True
        assert transport.left == "Z97"

    async def test_hand_back_never_raises(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup must not mask the real error that caused it to run."""
        session = await opened(fake_firmware)
        await session.preflight()

        async def _boom(_rate: int) -> str:
            raise RuntimeError("transport is gone")

        monkeypatch.setattr(fake_firmware, "set_baudrate", _boom)
        session.baudrate = 230400
        await session.hand_back()  # must not raise

    async def test_does_not_leave_twice(self, fake_firmware: FakeFirmwareTransport) -> None:
        session = await opened(fake_firmware)
        await session.preflight()
        await session.leave(finish=False)
        await session.hand_back()
        assert fake_firmware.commands.count("X") == 1


class TestConstruction:
    async def test_requires_a_url_or_a_transport(self) -> None:
        with pytest.raises(LumagenFirmwareError, match="url or a transport"):
            FirmwareSession()
