"""Tests for the decision about which sections get flashed.

The consequential logic in the subsystem: getting section 1 wrong either rewrites
a live A/B slot for no reason (slow, and it puts a working slot at risk) or skips
an update the device actually needs.
"""

from __future__ import annotations

import pytest

from aiolumagen.exceptions import LumagenFirmwareImageError
from aiolumagen.firmware.container import ContainerHeader, expected_stored_checksum
from aiolumagen.firmware.extract import (
    SECTION0,
    SECTION1,
    FirmwareBundle,
    FirmwareImage,
    extract_images,
)
from aiolumagen.firmware.plan import (
    DeviceStatus,
    SectionAction,
    plan_update,
)
from aiolumagen.firmware.protocol import BOOT_SECTOR_LEN, FirmwareRevision, parse_identity
from tests.conftest import build_container, build_updater_exe

SECTION1_PAYLOAD = b"section-one-payload" * 512


def make_bundle(*, source_name: str | None = None) -> FirmwareBundle:
    swdata = bytes(range(256)) * (BOOT_SECTOR_LEN // 256) + b"\xc3" * 0x2000
    exe = build_updater_exe(swdata=swdata, resources={131: build_container(SECTION1_PAYLOAD)})
    return extract_images(exe, source_name=source_name)


def committed_header(wire: bytes, tag: int) -> ContainerHeader:
    """The header a device would report for a slot holding `wire`, stamped `tag`."""
    stamped = bytearray(wire[:16])
    stamped[4:8] = tag.to_bytes(4, "little")
    header = ContainerHeader.unpack(bytes(stamped))
    assert header is not None
    return header


def status_holding(image: FirmwareImage, tag: int = 0xFADE0007) -> DeviceStatus:
    """A device whose live slot already holds `image`, stamped as committed."""
    wire = image.wire_bytes
    return DeviceStatus(
        identity=parse_identity("120325.16.0001"),
        section1_target_code="00",
        section1_live_header=committed_header(wire, tag),
        section1_live_checksum=expected_stored_checksum(wire, tag),
    )


class TestSection0:
    def test_is_always_written(self) -> None:
        """It changes every release and stages to scratch, so deciding costs more
        than it saves."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        plan = plan_update(bundle, status_holding(section1))
        names = [s.name for s in plan.to_write]
        assert names == [SECTION0]
        assert plan.sections[0].action is SectionAction.WRITE

    def test_missing_section0_is_refused(self) -> None:
        with pytest.raises(LumagenFirmwareImageError, match="no section0 image"):
            plan_update(FirmwareBundle(images={}))

    def test_unrecovered_descriptor_is_refused(self) -> None:
        """A fallback length may not match this release.

        The length varies between releases and the hardcoded value is already known
        wrong for one of them, so writing it would flash a truncated or over-long
        image. Refuse rather than warn.
        """
        bundle = FirmwareBundle(
            images={
                SECTION0: FirmwareImage(
                    name=SECTION0,
                    payload=b"\x00" * (BOOT_SECTOR_LEN + 0x1000),
                    source="fallback constants",
                    descriptor_recovered=False,
                )
            }
        )
        with pytest.raises(LumagenFirmwareImageError, match="swdata descriptor"):
            plan_update(bundle)

    def test_no_section0_when_the_fallback_is_unmapped(self) -> None:
        """An updater whose descriptor can't be found may yield no section 0 at all.

        That has to surface as a refusal rather than an empty plan that looks like
        "already up to date".
        """
        bundle = extract_images(build_updater_exe(with_descriptor=False))
        assert bundle.section0 is None
        with pytest.raises(LumagenFirmwareImageError, match="no section0 image"):
            plan_update(bundle)


class TestSection1:
    def test_skipped_when_the_live_slot_already_matches(self) -> None:
        """The tag-corrected comparison in action: no needless live-slot rewrite."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        plan = plan_update(bundle, status_holding(section1))
        entry = next(s for s in plan.sections if s.name == SECTION1)
        assert entry.action is SectionAction.SKIP
        assert "already matches" in entry.reason
        assert not plan.writes_section1

    def test_written_when_the_checksum_differs(self) -> None:
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        status = status_holding(section1)
        wrong = DeviceStatus(
            identity=status.identity,
            section1_target_code=status.section1_target_code,
            section1_live_header=status.section1_live_header,
            section1_live_checksum=(status.section1_live_checksum or 0) ^ 0xFF,
        )
        plan = plan_update(bundle, wrong)
        entry = next(s for s in plan.sections if s.name == SECTION1)
        assert entry.action is SectionAction.WRITE
        assert plan.writes_section1

    def test_written_when_the_live_slot_is_a_different_size(self) -> None:
        """A size disagreement settles it without needing a checksum at all."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        status = DeviceStatus(
            section1_live_header=ContainerHeader(
                magic=0xBABABEBE, tag=0xFADE0002, checksum=0, size=1234
            ),
            section1_live_checksum=0,
        )
        entry = next(s for s in plan_update(bundle, status).sections if s.name == SECTION1)
        assert entry.action is SectionAction.WRITE
        assert "bytes" in entry.reason

    def test_written_when_the_device_cannot_be_read(self) -> None:
        """Unknown errs towards writing, with a warning saying so.

        Skipping a needed section 1 would leave section 0 and section 1 from
        different releases — a pairing no vendor session produces.
        """
        plan = plan_update(make_bundle(), DeviceStatus())
        entry = next(s for s in plan.sections if s.name == SECTION1)
        assert entry.action is SectionAction.WRITE
        assert any("without confirming" in w for w in plan.warnings)

    def test_raw_checksum_comparison_would_have_been_wrong(self) -> None:
        """Guards the trap directly.

        A device reporting the image's *raw* sum is not byte-perfect — the tag at
        +4 is always stamped — so this must be planned as a write. If the
        correction were dropped, this case would flip to SKIP and the genuinely
        matching case above would flip to WRITE.
        """
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        wire = section1.wire_bytes
        status = DeviceStatus(
            section1_live_header=committed_header(wire, 0xFADE0007),
            section1_live_checksum=sum(wire) & 0xFFFFFFFF,
        )
        entry = next(s for s in plan_update(bundle, status).sections if s.name == SECTION1)
        assert entry.action is SectionAction.WRITE


class TestChipImages:
    def test_are_always_skipped(self) -> None:
        exe = build_updater_exe(
            resources={
                131: build_container(SECTION1_PAYLOAD),
                132: build_container(b"rx" * 64),
                133: build_container(b"tx" * 64),
                134: build_container(b"ntx" * 64),
            }
        )
        plan = plan_update(extract_images(exe), DeviceStatus())
        chips = [s for s in plan.sections if s.name.startswith("hdmi_")]
        assert len(chips) == 3
        assert all(s.action is SectionAction.SKIP for s in chips)
        assert all("out of scope" in s.reason for s in chips)
        assert not any(s.name.startswith("hdmi_") for s in plan.to_write)


class TestDeviceGuards:
    def test_refuses_a_non_radiance_pro(self) -> None:
        status = DeviceStatus(identity=parse_identity("101524.13.0001"))
        with pytest.raises(LumagenFirmwareImageError, match="not a"):
            plan_update(make_bundle(), status)

    def test_warns_on_a_downgrade(self) -> None:
        bundle = make_bundle(source_name="radiance_pro030225.exe")
        status = DeviceStatus(identity=parse_identity("120325.16.0001"))
        plan = plan_update(bundle, status)
        assert any("OLDER than" in w for w in plan.warnings)

    def test_no_downgrade_warning_on_an_upgrade(self) -> None:
        bundle = make_bundle(source_name="radiance_pro030326.exe")
        status = DeviceStatus(identity=parse_identity("120325.16.0001"))
        plan = plan_update(bundle, status)
        assert not any("OLDER than" in w for w in plan.warnings)

    def test_downgrade_check_is_chronological(self) -> None:
        """The MMDDYY trap, reached through the planner rather than in isolation.

        A device on 101524 (Oct 2024) offered 030225 (Mar 2025) is an UPGRADE. A
        naive integer comparison sees 30225 < 101524 and cries downgrade.
        """
        bundle = make_bundle(source_name="radiance_pro030225.exe")
        status = DeviceStatus(identity=parse_identity("101524.16.0001"))
        plan = plan_update(bundle, status)
        assert plan.release == FirmwareRevision(2025, 3, 2)
        assert not any("OLDER than" in w for w in plan.warnings)


class TestPlanReporting:
    def test_totals_only_count_what_will_be_written(self) -> None:
        bundle = make_bundle()
        section0, section1 = bundle.section0, bundle.section1
        assert section0 is not None and section1 is not None
        plan = plan_update(bundle, status_holding(section1))
        assert plan.total_bytes == len(section0.wire_bytes)
        assert not plan.is_empty

    def test_estimate_scales_with_the_work(self) -> None:
        """The user-visible payoff: a section-0-only update is much shorter."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        short = plan_update(bundle, status_holding(section1)).estimated_seconds(230400)
        full = plan_update(bundle, DeviceStatus()).estimated_seconds(230400)
        assert 0 < short < full

    def test_estimate_is_safe_at_zero_baud(self) -> None:
        assert plan_update(make_bundle(), DeviceStatus()).estimated_seconds(0) == 0.0

    def test_describe_mentions_every_section(self) -> None:
        bundle = make_bundle(source_name="radiance_pro030326.exe")
        section1 = bundle.section1
        assert section1 is not None
        text = plan_update(bundle, status_holding(section1)).describe()
        assert "WRITE section0" in text
        assert "skip  section1" in text
        assert "2026-03-03" in text
