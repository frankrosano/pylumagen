"""Tests for updater-mode command formatting and reply parsing.

Several assertions here carry literal byte strings taken from recorded vendor USB
captures. Those are the strongest tests in the subsystem: they check our generated
commands against what the official updater actually put on the wire.
"""

from __future__ import annotations

import pytest

from aiolumagen.firmware.protocol import (
    BLOCK_SIZE,
    SCRATCH_SECTOR,
    SECTION1_SLOTS,
    DeviceIdentity,
    FirmwareRevision,
    blocks_for,
    decode_hex_reply,
    erase_run,
    erase_sector,
    other_slot,
    pad_to_even_end,
    parse_checksum,
    parse_identity,
    parse_power_state,
    sectors_for,
    set_address,
    set_baud,
    set_length,
    set_source_address,
)


class TestCommandFormatting:
    def test_registers_are_six_digit_uppercase_hex(self) -> None:
        assert set_address(0x020000) == "A020000"
        assert set_source_address(0xB00000) == "aB00000"
        assert set_length(0x06FBCA) == "L06FBCA"

    def test_baud_is_decimal_unlike_everything_else(self) -> None:
        assert set_baud(230400) == "B230400"
        assert set_baud(9600) == "B009600"

    @pytest.mark.parametrize(
        ("sector", "expected"),
        [
            (0x58, "S7958a7"),  # scratch, from a recorded session
            (0x08, "S7908f7"),  # section-1 slot A, from a recorded session
            (0x60, "S7960 9f".replace(" ", "")),
            (0x05, "S7905fa"),
        ],
    )
    def test_erase_run_matches_the_recorded_wire_bytes(self, sector: int, expected: str) -> None:
        """Case is significant and asymmetric: sector uppercase, complement lowercase."""
        assert erase_run(sector) == expected

    def test_erase_sector_uses_the_same_encoding(self) -> None:
        assert erase_sector(0x05) == "S3705fa"

    def test_scratch_sector_is_derived_not_hardcoded(self) -> None:
        assert SCRATCH_SECTOR == 0x58
        assert erase_run(SCRATCH_SECTOR) == "S7958a7"


class TestPadToEvenEnd:
    def test_pads_an_odd_end_with_one_null(self) -> None:
        """From capture: 286,737-byte chip containers went out as 286,738."""
        data = b"\x01" * 286_737
        padded = pad_to_even_end(0x7C0000, data)
        assert len(padded) == 286_738
        assert padded[-1] == 0x00
        assert padded[:-1] == data

    def test_leaves_an_even_end_alone(self) -> None:
        data = b"\x01" * 457_674  # 030225 section 0, an even length
        assert pad_to_even_end(0xB00000, data) is data

    def test_parity_is_of_the_end_address_not_the_length(self) -> None:
        assert len(pad_to_even_end(0x1001, b"\x00" * 4)) == 4 + 1
        assert len(pad_to_even_end(0x1000, b"\x00" * 4)) == 4

    def test_padding_does_not_change_the_additive_checksum(self) -> None:
        """A 0x00 pad byte adds nothing, so a padded region still verifies."""
        data = b"\x7f" * 101
        assert sum(pad_to_even_end(0x1000, data)) == sum(data)


class TestGeometry:
    def test_sectors_round_up(self) -> None:
        assert sectors_for(0) == 0
        assert sectors_for(1) == 1
        assert sectors_for(0x20000) == 1
        assert sectors_for(0x20001) == 2
        assert sectors_for(3_158_962) == 25  # section 1, matching L000019 in capture

    def test_blocks_round_up(self) -> None:
        # Real section-0 wire sizes, and the section-1 size, from the captures.
        assert blocks_for(457_674) == 112  # 030225: 111 full blocks + a partial
        assert blocks_for(458_422) == 112  # 112325/120325
        assert blocks_for(3_158_962) == 772  # section 1, per the recorded session
        assert blocks_for(BLOCK_SIZE) == 1
        assert blocks_for(BLOCK_SIZE + 1) == 2

    def test_handles_a_tiny_tail_block(self) -> None:
        """030326's section 0 ends in an 18-byte block.

        A degenerate tail is exactly where an off-by-one in the block loop or the
        header-last reordering would hide, so it gets its own case.
        """
        assert blocks_for(458_770) == 113
        assert 458_770 - 112 * BLOCK_SIZE == 18


class TestSlots:
    def test_slot_codes_map_to_the_documented_layout(self) -> None:
        assert SECTION1_SLOTS["00"].first_sector == 0x08
        assert SECTION1_SLOTS["00"].address == 0x100000
        assert SECTION1_SLOTS["99"].first_sector == 0x60
        assert SECTION1_SLOTS["99"].address == 0xC00000

    def test_other_slot_is_the_running_one(self) -> None:
        assert other_slot("00") == SECTION1_SLOTS["99"]
        assert other_slot("99") == SECTION1_SLOTS["00"]

    def test_unknown_code_yields_no_slot(self) -> None:
        """An unrecognised Z35 reply means the layout is unknown, not "the other one".

        Returning a plausible address here is how you erase the wrong half of flash.
        """
        assert other_slot("zz") is None
        assert other_slot("") is None


class TestFirmwareRevision:
    def test_parses_mmddyy(self) -> None:
        assert FirmwareRevision.parse("030326") == FirmwareRevision(2026, 3, 3)
        assert FirmwareRevision.parse("112325") == FirmwareRevision(2025, 11, 23)

    @pytest.mark.parametrize("text", ["", "12345", "1234567", "abcdef", "133025", "030025"])
    def test_rejects_non_revisions(self, text: str) -> None:
        assert FirmwareRevision.parse(text) is None

    def test_ordering_is_chronological_not_numeric(self) -> None:
        """The trap this class exists to prevent.

        030225 (2 March 2025) is numerically SMALLER than 101524 (15 October 2024)
        but chronologically later. A naive integer comparison inverts the update
        decision for eight months of every year.
        """
        older = FirmwareRevision.parse("101524")
        newer = FirmwareRevision.parse("030225")
        assert older is not None and newer is not None
        assert older < newer
        assert int("101524") > int("030225")  # the wrong answer, for contrast

    def test_sorts_a_real_release_sequence(self) -> None:
        releases = ["030326", "092025", "112325", "030225", "120325"]
        parsed = [FirmwareRevision.parse(r) for r in releases]
        assert all(p is not None for p in parsed)
        assert [p.mmddyy for p in sorted(parsed)] == [  # type: ignore[union-attr]
            "030225",
            "092025",
            "112325",
            "120325",
            "030326",
        ]

    def test_from_identify_reads_hex_digits_as_decimal(self) -> None:
        """The I reply's revision field is hex-encoded but reads as decimal."""
        assert FirmwareRevision.from_identify(0x030326) == FirmwareRevision(2026, 3, 3)

    def test_mmddyy_round_trips(self) -> None:
        revision = FirmwareRevision.parse("120325")
        assert revision is not None
        assert revision.mmddyy == "120325"
        assert str(revision) == "2025-12-03"


class TestParseIdentity:
    def test_parses_the_documented_shape(self) -> None:
        identity = parse_identity("030326.16.04D2")
        assert identity == DeviceIdentity(
            revision_raw=0x030326,
            device_id=0x16,
            serial=0x04D2,
            revision=FirmwareRevision(2026, 3, 3),
        )
        assert identity is not None
        assert identity.model == "RadiancePro"
        assert identity.is_radiance_pro

    def test_tolerates_a_command_echo(self) -> None:
        """The device may echo the command as a prefix, so position isn't trusted."""
        assert parse_identity("I 030326.16.04D2\r\n") == parse_identity("030326.16.04D2")

    def test_recognises_a_non_pro_model(self) -> None:
        identity = parse_identity("101524.13.0001")
        assert identity is not None
        assert identity.model == "RadianceMini"
        assert not identity.is_radiance_pro

    @pytest.mark.parametrize("text", ["", "garbage", "030326.16", "..."])
    def test_returns_none_when_unparseable(self, text: str) -> None:
        assert parse_identity(text) is None


class TestParseChecksum:
    def test_parses_a_recorded_reply(self) -> None:
        assert parse_checksum("CS=025a4128") == 0x025A4128

    def test_tolerates_an_echoed_prefix(self) -> None:
        assert parse_checksum("LC CS=0004d452") == 0x0004D452

    def test_takes_exactly_eight_digits(self) -> None:
        """The reply has no terminator, so a following byte must not be absorbed."""
        assert parse_checksum("CS=0265a30dOk") == 0x0265A30D

    @pytest.mark.parametrize("text", ["", "no checksum here", "CS=zzzzzzzz"])
    def test_no_answer_is_none_not_zero(self, text: str) -> None:
        """None means 'no answer' — usually 'still busy' — not 'checksum is 0'.

        Collapsing the two would report a verification mismatch on a device that
        simply hadn't replied yet.
        """
        assert parse_checksum(text) is None


class TestDecodeHexReply:
    def test_decodes_two_characters_per_byte(self) -> None:
        assert decode_hex_reply(b"bebebaba") == b"\xbe\xbe\xba\xba"

    def test_tolerates_surrounding_whitespace(self) -> None:
        assert decode_hex_reply(b"  00ff10  ") == b"\x00\xff\x10"

    def test_returns_none_for_non_hex(self) -> None:
        assert decode_hex_reply(b"not hex at all") is None


class TestParsePowerState:
    def test_reads_on_and_standby(self) -> None:
        assert parse_power_state("!S02,1\r\n") is True
        assert parse_power_state("!S02,0\r\n") is False

    def test_tolerates_an_echo(self) -> None:
        assert parse_power_state("ZQS02!S02,1") is True

    @pytest.mark.parametrize("text", ["", "!S01,x", "!S02,"])
    def test_no_answer_is_none_not_standby(self, text: str) -> None:
        """None means the main firmware didn't answer — already in updater mode.

        Distinct from standby, and the preflight gate treats them differently.
        """
        assert parse_power_state(text) is None
