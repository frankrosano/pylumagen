"""Tests for block-level auditing and sector-granular repair.

Audit is the second, independent verification mechanism: a whole-region checksum
is one 32-bit sum over megabytes, while this checks each block separately and
says *where* the damage is. Repair then rewrites only the sectors that need it.

The interesting properties are the efficiency of the coarse-then-fine search, the
tag correction that stops a committed slot reporting a false mismatch, and the
fact that repair rewrites whole sectors rather than individual blocks.
"""

from __future__ import annotations

import pytest

from aiolumagen.exceptions import LumagenFirmwareError
from aiolumagen.firmware import AuditResult
from aiolumagen.firmware.container import additive_checksum
from aiolumagen.firmware.protocol import (
    AUDIT_CHUNK_BLOCKS,
    BLOCK_SIZE,
    SCRATCH_ADDR,
    SECTOR_SIZE,
)
from aiolumagen.firmware.session import FirmwareSession
from tests.conftest import FakeFirmwareTransport, build_container

pytestmark = pytest.mark.usefixtures("no_delays")

BLOCKS_PER_SECTOR = SECTOR_SIZE // BLOCK_SIZE  # 32


def make_image(blocks: int = 70) -> bytes:
    """A distinctive image: every block differs, so a misplaced block is detected."""
    return b"".join(bytes([index & 0xFF]) * BLOCK_SIZE for index in range(blocks))


async def opened(transport: FakeFirmwareTransport) -> FirmwareSession:
    session = FirmwareSession(transport=transport)  # type: ignore[arg-type]
    await session.connect()
    return session


class TestAuditClean:
    async def test_reports_ok_when_flash_matches(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = make_image()
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.ok
        assert result.bad_blocks == ()
        assert result.total_blocks == 70
        assert result.bad_sectors == ()
        assert "every block matches" in result.describe()

    async def test_writes_nothing(self, fake_firmware: FakeFirmwareTransport) -> None:
        """Read-only is the whole reason audit is safe against a slot mid-write."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        await session.audit(image, SCRATCH_ADDR)
        assert fake_firmware.erases == []
        assert fake_firmware.payload_writes == []

    async def test_only_issues_coarse_checksums_when_clean(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The efficiency claim: ~1 checksum per sector, not per block.

        70 blocks is 3 coarse chunks. Checking every block would be 70 checksums.
        """
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.chunks_checked == 3
        assert result.checksums_issued == 3


class TestAuditFinds:
    async def test_locates_a_single_corrupt_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = make_image()
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 40 * BLOCK_SIZE + 7] ^= 0xFF
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.bad_blocks == (40,)
        assert not result.ok
        assert result.erased_blocks == ()  # it has content, just wrong content

    async def test_subdivides_only_the_failing_chunk(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """One bad block should cost 3 coarse + 32 fine checksums, not 70."""
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 40 * BLOCK_SIZE] ^= 0xFF
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.checksums_issued == 3 + BLOCKS_PER_SECTOR
        assert result.checksums_issued < result.total_blocks

    async def test_distinguishes_an_erased_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Erased means the write never arrived; wrong content means it corrupted.

        The first points at flow control, the second at framing — a genuinely
        useful distinction when judging a marginal link.
        """
        image = make_image()
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        start = SCRATCH_ADDR + 12 * BLOCK_SIZE
        fake_firmware.memory[start : start + BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.bad_blocks == (12,)
        assert result.erased_blocks == (12,)
        assert "read as erased" in result.describe()

    async def test_finds_several_blocks_across_chunks(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        for block in (3, 4, 5, 33, 68):
            fake_firmware.memory[SCRATCH_ADDR + block * BLOCK_SIZE] ^= 0xFF
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.bad_blocks == (3, 4, 5, 33, 68)
        assert result.runs == ((3, 5), (33, 33), (68, 68))

    async def test_maps_bad_blocks_to_absolute_sectors(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Repair needs the sector number the erase command takes."""
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 33 * BLOCK_SIZE] ^= 0xFF
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR)
        # Block 33 sits in the second sector of the region.
        assert result.bad_sectors == ((SCRATCH_ADDR + SECTOR_SIZE) // SECTOR_SIZE,)

    async def test_counts_an_unanswered_block_as_bad(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unverified block cannot be called good, but it's tracked separately."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)

        real = session.device_checksum
        calls = {"n": 0}

        async def flaky(addr: int, length: int, timeout: float = 15.0) -> int | None:
            calls["n"] += 1
            # Fail the first coarse chunk and every fine read inside it.
            if length == BLOCK_SIZE or calls["n"] == 1:
                return None
            return await real(addr, length, timeout)

        monkeypatch.setattr(session, "device_checksum", flaky)
        result = await session.audit(image, SCRATCH_ADDR)
        assert result.unanswered_blocks
        assert set(result.unanswered_blocks) <= set(result.bad_blocks)
        assert "unanswered" in result.describe()


class TestTagCorrection:
    """The trap: a committed slot never matches the file's raw block-0 checksum."""

    async def test_committed_slot_reports_a_false_mismatch_without_the_tag(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        wire = build_container(b"section one" * 3000)
        fake_firmware.commit(SCRATCH_ADDR, wire, 0xFADE0021)
        session = await opened(fake_firmware)
        uncorrected = await session.audit(wire, SCRATCH_ADDR)
        assert uncorrected.bad_blocks == (0,)  # byte-perfect flash, spurious failure

    async def test_stamped_tag_clears_it(self, fake_firmware: FakeFirmwareTransport) -> None:
        wire = build_container(b"section one" * 3000)
        fake_firmware.commit(SCRATCH_ADDR, wire, 0xFADE0021)
        session = await opened(fake_firmware)
        result = await session.audit(wire, SCRATCH_ADDR, stamped_tag=0xFADE0021)
        assert result.ok

    async def test_the_correction_is_scoped_to_block_zero(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """It must not mask real damage elsewhere in the region."""
        wire = build_container(b"section one" * 3000)
        fake_firmware.commit(SCRATCH_ADDR, wire, 0xFADE0021)
        fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF
        session = await opened(fake_firmware)
        result = await session.audit(wire, SCRATCH_ADDR, stamped_tag=0xFADE0021)
        assert result.bad_blocks == (5,)

    async def test_a_wrong_tag_still_reports_block_zero(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        wire = build_container(b"section one" * 3000)
        fake_firmware.commit(SCRATCH_ADDR, wire, 0xFADE0021)
        session = await opened(fake_firmware)
        result = await session.audit(wire, SCRATCH_ADDR, stamped_tag=0xFADE0099)
        assert result.bad_blocks == (0,)


class TestSkipBlockZero:
    async def test_ignores_a_deliberately_unwritten_header(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The header-last case: block 0 is erased on purpose, mid-write."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
        session = await opened(fake_firmware)

        assert (await session.audit(image, SCRATCH_ADDR)).bad_blocks == (0,)
        skipped = await session.audit(image, SCRATCH_ADDR, skip_block0=True)
        assert skipped.ok
        assert skipped.skipped_block0
        assert "block 0 skipped" in skipped.describe()


class TestAuditValidation:
    async def test_rejects_a_zero_chunk(self, fake_firmware: FakeFirmwareTransport) -> None:
        session = await opened(fake_firmware)
        with pytest.raises(LumagenFirmwareError, match="at least 1"):
            await session.audit(make_image(4), SCRATCH_ADDR, chunk_blocks=0)

    async def test_chunk_of_one_checks_every_block(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = make_image(8)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        result = await session.audit(image, SCRATCH_ADDR, chunk_blocks=1)
        assert result.ok
        assert result.chunks_checked == 8

    async def test_handles_a_partial_tail_block(self, fake_firmware: FakeFirmwareTransport) -> None:
        """A real release ends section 0 in an 18-byte block."""
        image = make_image(2) + b"\x5a" * 18
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        assert (await session.audit(image, SCRATCH_ADDR)).ok

        fake_firmware.memory[SCRATCH_ADDR + 2 * BLOCK_SIZE + 3] ^= 0xFF
        assert (await session.audit(image, SCRATCH_ADDR)).bad_blocks == (2,)

    def test_default_chunk_is_exactly_one_sector(self) -> None:
        assert AUDIT_CHUNK_BLOCKS * BLOCK_SIZE == SECTOR_SIZE


class TestRepair:
    async def test_rewrites_only_the_affected_sector(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The point of sector-granular repair: don't re-roll the whole transfer."""
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 40 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        result = await session.repair(image, SCRATCH_ADDR)

        assert result.ok
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image
        # Exactly one sector erased, and only its blocks rewritten.
        assert len(fake_firmware.erases) == 1
        assert fake_firmware.erases[0][1] == 1  # a single sector
        written = {addr for addr, _ in fake_firmware.payload_writes}
        assert len(written) == BLOCKS_PER_SECTOR

    async def test_rewrites_the_whole_sector_not_just_bad_blocks(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Erasing a sector destroys the good blocks sharing it.

        NOR programming only clears bits, so there is no way to patch one block in
        place — every block in the erased sector has to go back down.
        """
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 33 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        await session.repair(image, SCRATCH_ADDR)

        # Blocks 32..63 share the sector with block 33 and were all rewritten.
        expected = {SCRATCH_ADDR + b * BLOCK_SIZE for b in range(32, 64)}
        assert {addr for addr, _ in fake_firmware.payload_writes} == expected
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image

    async def test_repairs_several_sectors(self, fake_firmware: FakeFirmwareTransport) -> None:
        image = make_image(70)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        for block in (2, 40, 65):
            fake_firmware.memory[SCRATCH_ADDR + block * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        result = await session.repair(image, SCRATCH_ADDR)
        assert result.ok
        assert len(fake_firmware.erases) == 3
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image

    async def test_is_a_no_op_when_nothing_is_wrong(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        session = await opened(fake_firmware)
        result = await session.repair(image, SCRATCH_ADDR)
        assert result.ok
        assert fake_firmware.erases == []
        assert fake_firmware.payload_writes == []

    async def test_withholds_block_zero_under_header_last(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A repair must not accidentally commit a slot it was fixing."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
        fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        result = await session.repair(image, SCRATCH_ADDR, skip_block0=True)

        assert result.ok
        # Block 5's sector was erased and rewritten, but block 0 stayed erased.
        assert fake_firmware.region(SCRATCH_ADDR, 4) == b"\xff\xff\xff\xff"
        assert SCRATCH_ADDR not in {addr for addr, _ in fake_firmware.payload_writes}

    async def test_reports_failure_when_it_cannot_converge(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A link still dropping bytes needs a lower rate, not another attempt."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        # Simulate a link that corrupts one block on every rewrite.
        real_write = session.write_raw

        async def lossy(data: bytes) -> None:
            await real_write(data)
            fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        monkeypatch.setattr(session, "write_raw", lossy)
        result = await session.repair(image, SCRATCH_ADDR, passes=2)
        assert not result.ok
        assert 5 in result.bad_blocks

    async def test_extra_passes_are_attempted(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure that outlives its pass should be fixed by the next one.

        The corruption is injected on the *last* write of pass 1, so nothing in
        that pass rewrites it — which is what forces a second pass. Corrupting
        earlier wouldn't: the block's own rewrite later in the same sector would
        repair it, and the pass would converge immediately (as an earlier version
        of this test discovered).
        """
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        real_write = session.write_raw
        writes = {"n": 0}

        async def flaky(data: bytes) -> None:
            await real_write(data)
            writes["n"] += 1
            if writes["n"] == BLOCKS_PER_SECTOR:  # last block of pass 1's sector
                fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        monkeypatch.setattr(session, "write_raw", flaky)
        result = await session.repair(image, SCRATCH_ADDR, passes=3)
        assert result.ok
        assert len(fake_firmware.erases) == 2  # one per pass
        assert fake_firmware.region(SCRATCH_ADDR, len(image)) == image

    async def test_a_single_pass_does_not_retry(
        self, fake_firmware: FakeFirmwareTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """passes=1 (the default) means one erase-rewrite-audit round, no more."""
        image = make_image(40)
        fake_firmware.memory[SCRATCH_ADDR : SCRATCH_ADDR + len(image)] = image
        fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        session = await opened(fake_firmware)
        real_write = session.write_raw
        writes = {"n": 0}

        async def flaky(data: bytes) -> None:
            await real_write(data)
            writes["n"] += 1
            if writes["n"] == BLOCKS_PER_SECTOR:
                fake_firmware.memory[SCRATCH_ADDR + 5 * BLOCK_SIZE] ^= 0xFF

        monkeypatch.setattr(session, "write_raw", flaky)
        result = await session.repair(image, SCRATCH_ADDR)
        assert not result.ok
        assert len(fake_firmware.erases) == 1


class TestAuditResultShape:
    def test_runs_collapse_contiguous_blocks(self) -> None:
        result = AuditResult(base=0, total_blocks=100, bad_blocks=(1, 2, 3, 9, 20, 21))
        assert result.runs == ((1, 3), (9, 9), (20, 21))

    def test_empty_result_is_ok(self) -> None:
        result = AuditResult(base=0, total_blocks=10)
        assert result.ok
        assert result.runs == ()
        assert result.bad_sectors == ()

    def test_sectors_are_deduplicated(self) -> None:
        result = AuditResult(base=0, total_blocks=64, bad_blocks=(0, 1, 2, 33))
        assert result.bad_sectors == (0, 1)

    def test_checksum_helper_matches_additive_sum(self) -> None:
        """Guards the correction against drifting from the container helper."""
        image = build_container(b"x" * 8192)
        plain = FirmwareSession._expected_sum(image, 0, len(image), None)
        assert plain == additive_checksum(image)
