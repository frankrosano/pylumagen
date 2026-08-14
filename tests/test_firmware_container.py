"""Tests for the 0xBABABEBE firmware container."""

from __future__ import annotations

import struct

import pytest

from aiolumagen.exceptions import LumagenFirmwareImageError
from aiolumagen.firmware.container import (
    HEADER_LEN,
    MAGIC,
    TAG_UNWRITTEN,
    ContainerHeader,
    additive_checksum,
    expected_stored_checksum,
    find_containers,
    parse_container,
)
from tests.conftest import build_container


def test_additive_checksum_is_a_plain_byte_sum() -> None:
    assert additive_checksum(b"") == 0
    assert additive_checksum(b"\x01\x02\x03") == 6
    # Truncation to 32 bits, not an error.
    assert additive_checksum(b"\xff" * 0x2000000) == (0xFF * 0x2000000) & 0xFFFFFFFF


def test_parse_container_round_trips() -> None:
    payload = bytes(range(256)) * 4
    container = parse_container(build_container(payload))
    assert container.payload == payload
    assert container.header.magic == MAGIC
    assert container.header.size == len(payload)
    assert container.checksum_ok
    assert container.raw == build_container(payload)


def test_parse_container_rejects_bad_magic() -> None:
    blob = bytearray(build_container(b"x" * 32))
    struct.pack_into("<I", blob, 0, 0xDEADBEEF)
    with pytest.raises(LumagenFirmwareImageError, match="bad container magic"):
        parse_container(bytes(blob))


def test_parse_container_rejects_corrupt_payload() -> None:
    """A flipped payload byte must be caught before anything reaches flash."""
    blob = bytearray(build_container(b"a" * 64))
    blob[HEADER_LEN] = blob[HEADER_LEN] ^ 0xFF
    with pytest.raises(LumagenFirmwareImageError, match="checksum mismatch"):
        parse_container(bytes(blob))


def test_parse_container_rejects_a_device_read() -> None:
    """A stamped tag means these bytes came off a device, not out of an updater.

    Worth distinguishing: a shipped image and a committed slot differ only in
    those four bytes, and treating one as the other silently breaks the
    tag-correction arithmetic that the section-1 skip decision depends on.
    """
    blob = build_container(b"payload" * 8, tag=0xFADE0003)
    with pytest.raises(LumagenFirmwareImageError, match="container tag is"):
        parse_container(blob)


def test_parse_container_rejects_truncation() -> None:
    full = build_container(b"z" * 512)
    with pytest.raises(LumagenFirmwareImageError, match="only"):
        parse_container(full[: HEADER_LEN + 100])
    with pytest.raises(LumagenFirmwareImageError, match="too short"):
        parse_container(b"\xbe\xbe")


class TestContainerHeader:
    def test_unpack_needs_a_full_header(self) -> None:
        assert ContainerHeader.unpack(b"\x00" * 15) is None

    def test_shipped_header_is_not_committed(self) -> None:
        header = ContainerHeader.unpack(build_container(b"q" * 16))
        assert header is not None
        assert header.has_magic
        assert header.tag == TAG_UNWRITTEN
        assert not header.committed
        assert header.generation is None

    def test_erased_slot_reads_as_uncommitted(self) -> None:
        """An all-0xFF region must never look like a slot that can win a boot."""
        header = ContainerHeader.unpack(b"\xff" * HEADER_LEN)
        assert header is not None
        assert not header.has_magic
        assert not header.committed
        assert header.generation is None

    def test_committed_header_exposes_its_generation(self) -> None:
        header = ContainerHeader.unpack(build_container(b"q" * 16, tag=0xFADE001C))
        assert header is not None
        assert header.committed
        assert header.generation == 0x1C

    def test_unrecognised_tag_shape_has_no_generation(self) -> None:
        """Unknown must stay unknown rather than collapsing to zero.

        Ordering a real generation against a fabricated 0 is how you conclude the
        *running* slot is the older one and overwrite live firmware, so an
        unrecognised tag has to be unorderable.
        """
        header = ContainerHeader.unpack(build_container(b"q" * 16, tag=0x12340005))
        assert header is not None
        assert header.generation is None


class TestExpectedStoredChecksum:
    def test_corrects_for_the_device_stamped_tag(self) -> None:
        """Reproduces a real committed slot, from a recorded hardware session.

        The device overwrites the four bytes at +4 on commit, so a byte-perfect
        slot can never sum to the image's own checksum. These numbers are from the
        capture: raw - 1020 (four shipped 0xFF) + 500 (a stamped 0xFADE001C).
        """
        wire = build_container(b"section one payload" * 512)
        raw = additive_checksum(wire)
        corrected = expected_stored_checksum(wire, 0xFADE001C)
        assert corrected == raw - (0xFF * 4) + sum((0xFADE001C).to_bytes(4, "little"))
        assert corrected == raw - 520

    def test_differs_from_the_raw_sum(self) -> None:
        """The whole point: comparing against the raw sum always reports a mismatch.

        If these two were equal, every "does the device already hold this image?"
        test would answer no, and every update would needlessly rewrite a live
        A/B slot.
        """
        wire = build_container(b"payload" * 256)
        assert expected_stored_checksum(wire, 0xFADE0001) != additive_checksum(wire)

    def test_matches_a_faithfully_stamped_slot(self) -> None:
        """End to end: stamp a slot the way the device does, then predict its sum."""
        wire = bytearray(build_container(b"abc" * 1000))
        tag = 0xFADE0042
        wire[4:8] = tag.to_bytes(4, "little")
        assert additive_checksum(bytes(wire)) == expected_stored_checksum(
            build_container(b"abc" * 1000), tag
        )


class TestFindContainers:
    def test_finds_containers_at_any_offset(self) -> None:
        first = build_container(b"one" * 64)
        second = build_container(b"two" * 128)
        blob = b"\x00" * 37 + first + b"\xcc" * 11 + second
        found = find_containers(blob)
        assert [offset for offset, _ in found] == [37, 37 + len(first) + 11]
        assert [c.payload for _, c in found] == [b"one" * 64, b"two" * 128]

    def test_ignores_magic_that_does_not_validate(self) -> None:
        """A bare 4-byte magic is not evidence; the checksum is what qualifies it."""
        decoy = struct.pack("<IIII", MAGIC, TAG_UNWRITTEN, 0x9999, 4) + b"\x00\x00\x00\x00"
        assert find_containers(decoy) == []
