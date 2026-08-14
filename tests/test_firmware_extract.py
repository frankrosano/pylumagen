"""Tests for vendor-EXE parsing.

Run against a synthetic PE built in ``conftest``, because the real updaters are
Lumagen, Inc.'s copyrighted binaries and cannot live in a public repo. The
extractor is separately cross-checked against five real releases outside the test
suite, where it produced byte-identical output to the hardware-proven reference
implementation. Those five are the ones that happened to be on hand, not the
whole catalogue — a spot check rather than proof the extractor generalises.
"""

from __future__ import annotations

import pytest

from aiolumagen.exceptions import LumagenFirmwareImageError
from aiolumagen.firmware.extract import (
    SWDATA_LEN_FALLBACK,
    FirmwareImage,
    extract_images,
    extract_resources,
    find_swdata_descriptor,
    parse_release_name,
    pe_image_base,
    pe_sections,
)
from aiolumagen.firmware.protocol import BOOT_SECTOR_LEN, FirmwareRevision
from tests.conftest import IMAGE_BASE, SWDATA_VA, build_container, build_updater_exe


class TestPeParsing:
    def test_reads_the_section_table(self) -> None:
        sections = pe_sections(build_updater_exe())
        assert [s.name for s in sections] == [".text", ".rsrc", ".data"]
        assert pe_image_base(build_updater_exe()) == IMAGE_BASE

    def test_rejects_a_non_pe(self) -> None:
        with pytest.raises(LumagenFirmwareImageError, match="not a Windows executable"):
            extract_images(b"this is not an executable")

    def test_rejects_an_mz_stub_with_no_pe_header(self) -> None:
        blob = bytearray(b"\x00" * 0x100)
        blob[0:2] = b"MZ"
        with pytest.raises(LumagenFirmwareImageError, match="no PE header"):
            extract_images(bytes(blob))


class TestResourceWalk:
    def test_extracts_every_rcdata_resource(self) -> None:
        payloads = {
            131: build_container(b"one" * 100),
            132: build_container(b"two" * 100),
            134: build_container(b"four" * 100),
        }
        found = extract_resources(build_updater_exe(resources=payloads))
        assert found == payloads

    def test_ignores_resource_ids_it_does_not_recognise(self) -> None:
        exe = build_updater_exe(
            resources={131: build_container(b"real" * 64), 999: b"not firmware"}
        )
        bundle = extract_images(exe)
        assert set(bundle.images) == {"section0", "section1"}


class TestSwdataDescriptor:
    def test_recovers_address_and_length_from_code(self) -> None:
        swdata = b"\x5a" * 0x30000
        found = find_swdata_descriptor(build_updater_exe(swdata=swdata))
        assert found == (SWDATA_VA, len(swdata))

    def test_walks_past_a_decoy_match(self) -> None:
        """The needle occurs incidentally in a 5 MB binary.

        A scan that accepted its first hit, or gave up on a non-match, would take
        the decoy's out-of-range pointer instead of the real descriptor.
        """
        swdata = b"\x77" * 0x28000
        found = find_swdata_descriptor(build_updater_exe(swdata=swdata, decoy=True))
        assert found == (SWDATA_VA, len(swdata))

    def test_returns_none_when_absent(self) -> None:
        assert find_swdata_descriptor(build_updater_exe(with_descriptor=False)) is None

    def test_length_comes_from_code_not_a_constant(self) -> None:
        """The fallback length is wrong for at least one real release.

        This is the reason the code scan exists at all, so assert the recovered
        length actually tracks the image rather than the hardcoded value.
        """
        swdata = b"\x11" * (BOOT_SECTOR_LEN + 0x1234)
        bundle = extract_images(build_updater_exe(swdata=swdata))
        section0 = bundle.section0
        assert section0 is not None
        assert section0.size == len(swdata) != SWDATA_LEN_FALLBACK
        assert section0.descriptor_recovered


class TestWireForm:
    def test_section0_is_sent_without_its_boot_sector(self) -> None:
        """swdata images flash from 0x0, so its first sector IS the bootloader.

        Sending the whole thing to 0x20000 would shift every byte of firmware up
        by one sector and leave an unbootable unit — the most destructive mistake
        available in this subsystem.
        """
        swdata = bytes(range(256)) * (BOOT_SECTOR_LEN // 256) + b"\xab" * 0x1000
        bundle = extract_images(build_updater_exe(swdata=swdata))
        section0 = bundle.section0
        assert section0 is not None
        assert section0.payload == swdata
        assert section0.wire_bytes == swdata[BOOT_SECTOR_LEN:]
        assert len(section0.wire_bytes) == len(swdata) - BOOT_SECTOR_LEN

    def test_short_section0_is_not_stripped(self) -> None:
        """Guards the strip against underflowing into a negative-length slice."""
        image = FirmwareImage(name="section0", payload=b"\x01" * 128, source="test")
        assert image.wire_bytes == b"\x01" * 128

    def test_container_images_keep_their_header(self) -> None:
        """The 16-byte header is flashed; it is not EXE packaging to be stripped.

        The device reads it back at boot to elect a slot, so dropping it would
        produce a slot that never wins an election.
        """
        payload = b"section-one" * 200
        bundle = extract_images(build_updater_exe(resources={131: build_container(payload)}))
        section1 = bundle.section1
        assert section1 is not None
        assert section1.payload == payload
        assert section1.wire_bytes == build_container(payload)
        assert len(section1.wire_bytes) == len(payload) + 16


class TestBundle:
    def test_groups_chip_images_separately(self) -> None:
        exe = build_updater_exe(
            resources={
                131: build_container(b"s1" * 64),
                132: build_container(b"rx" * 64),
                133: build_container(b"tx" * 64),
                134: build_container(b"ntx" * 64),
            }
        )
        bundle = extract_images(exe)
        assert set(bundle.chip_images) == {"hdmi_rx", "hdmi_tx", "hdmi_ntx"}
        assert bundle.section1 is not None
        assert bundle.section0 is not None

    def test_corrupt_resource_fails_extraction(self) -> None:
        broken = bytearray(build_container(b"payload" * 64))
        broken[20] ^= 0xFF  # flip a payload byte, invalidating the checksum
        with pytest.raises(LumagenFirmwareImageError, match="section1"):
            extract_images(build_updater_exe(resources={131: bytes(broken)}))

    def test_release_is_parsed_from_the_filename(self) -> None:
        bundle = extract_images(build_updater_exe(), source_name="radiance_pro030326.exe")
        assert bundle.release == FirmwareRevision(year=2026, month=3, day=3)
        assert bundle.source_name == "radiance_pro030326.exe"

    def test_release_is_none_without_a_filename(self) -> None:
        assert extract_images(build_updater_exe()).release is None


class TestParseReleaseName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("radiance_pro030326.exe", FirmwareRevision(2026, 3, 3)),
            ("radiance_pro112325.exe", FirmwareRevision(2025, 11, 23)),
            ("092025", FirmwareRevision(2025, 9, 20)),
        ],
    )
    def test_parses_known_shapes(self, name: str, expected: FirmwareRevision) -> None:
        assert parse_release_name(name) == expected

    @pytest.mark.parametrize("name", ["updater.exe", "radiance_pro.exe", "999999", "12345"])
    def test_returns_none_for_anything_else(self, name: str) -> None:
        assert parse_release_name(name) is None
