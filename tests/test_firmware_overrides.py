"""Tests for the planner overrides: ``force=`` and ``only=``.

These exist for deliberate re-flashes, recovery, and on-device testing, so the
important properties are (a) they actually override the *comparison*, and (b)
they conspicuously do **not** override the correctness gates. A `force` that
relaxed a length check would flash a guessed-length image.
"""

from __future__ import annotations

import pytest

from aiolumagen.exceptions import LumagenFirmwareError, LumagenFirmwareImageError
from aiolumagen.firmware import WRITABLE_SECTIONS
from aiolumagen.firmware.extract import (
    SECTION0,
    SECTION1,
    FirmwareBundle,
    FirmwareImage,
    extract_images,
)
from aiolumagen.firmware.plan import DeviceStatus, SectionAction, plan_update
from aiolumagen.firmware.protocol import (
    ADDR_LIVE,
    BOOT_SECTOR_LEN,
    SCRATCH_ADDR,
    SECTION1_SLOTS,
    parse_identity,
)
from aiolumagen.firmware.session import FirmwareSession
from tests.conftest import FakeFirmwareTransport, build_updater_exe
from tests.test_firmware_plan import make_bundle, status_holding

pytestmark = pytest.mark.usefixtures("no_delays")


def verdict(plan: object, name: str) -> SectionAction:
    return next(s.action for s in plan.sections if s.name == name)  # type: ignore[attr-defined]


class TestForce:
    def test_writes_section1_the_device_already_has(self) -> None:
        """The whole point: the up-to-date comparison is bypassed."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        status = status_holding(section1)

        assert verdict(plan_update(bundle, status), SECTION1) is SectionAction.SKIP
        forced = plan_update(bundle, status, force=True)
        assert verdict(forced, SECTION1) is SectionAction.WRITE
        assert forced.writes_section1
        assert "forced" in next(s.reason for s in forced.sections if s.name == SECTION1)

    def test_is_recorded_on_the_plan(self) -> None:
        """A forced plan is not the library's opinion about what's needed, and a UI
        has to be able to tell the difference."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        plan = plan_update(bundle, status_holding(section1), force=True)
        assert plan.forced
        assert plan.overridden
        assert plan.only is None
        assert any("force=True" in w for w in plan.warnings)
        assert "OVERRIDDEN" in plan.describe()

    def test_unforced_plan_is_not_marked_overridden(self) -> None:
        plan = plan_update(make_bundle())
        assert not plan.forced
        assert not plan.overridden
        assert "OVERRIDDEN" not in plan.describe()

    def test_suppresses_the_unverified_warning(self) -> None:
        """ "Couldn't confirm you need this" is noise when you said write it anyway."""
        plan = plan_update(make_bundle(), force=True)
        assert not any("without confirming" in w for w in plan.warnings)

    def test_does_not_override_the_descriptor_gate(self) -> None:
        """force makes an update unconditional, never unchecked.

        The fallback section-0 length varies between releases, so writing it would
        flash a truncated or over-long image. Forcing past that is exactly what
        must not be possible.
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
            plan_update(bundle, force=True)

    def test_does_not_override_the_device_model_gate(self) -> None:
        status = DeviceStatus(identity=parse_identity("101524.13.0001"))
        with pytest.raises(LumagenFirmwareImageError, match="not a"):
            plan_update(make_bundle(), status, force=True)

    def test_does_not_override_a_missing_image(self) -> None:
        with pytest.raises(LumagenFirmwareImageError, match="no section0 image"):
            plan_update(FirmwareBundle(images={}), force=True)


class TestOnly:
    def test_section0_alone(self) -> None:
        bundle = make_bundle()
        plan = plan_update(bundle, only=[SECTION0])
        assert [s.name for s in plan.to_write] == [SECTION0]
        assert verdict(plan, SECTION1) is SectionAction.SKIP
        assert "not selected" in next(s.reason for s in plan.sections if s.name == SECTION1)
        assert not plan.writes_section1

    def test_section1_alone(self) -> None:
        bundle = make_bundle()
        plan = plan_update(bundle, only=[SECTION1])
        assert [s.name for s in plan.to_write] == [SECTION1]
        assert verdict(plan, SECTION0) is SectionAction.SKIP

    def test_both_is_the_same_as_the_default_selection(self) -> None:
        bundle = make_bundle()
        both = plan_update(bundle, only=[SECTION0, SECTION1])
        default = plan_update(bundle)
        assert [s.name for s in both.to_write] == [s.name for s in default.to_write]

    def test_section1_alone_still_compares_unless_forced(self) -> None:
        """`only` scopes the plan; it doesn't imply force."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        plan = plan_update(bundle, status_holding(section1), only=[SECTION1])
        assert verdict(plan, SECTION1) is SectionAction.SKIP
        assert plan.is_empty

    def test_combines_with_force(self) -> None:
        """The combination the on-device tests need: rewrite exactly one section."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        plan = plan_update(bundle, status_holding(section1), only=[SECTION1], force=True)
        assert [s.name for s in plan.to_write] == [SECTION1]
        assert plan.forced and plan.only == (SECTION1,)

    def test_is_recorded_on_the_plan(self) -> None:
        plan = plan_update(make_bundle(), only=[SECTION0])
        assert plan.only == (SECTION0,)
        assert plan.overridden
        assert not plan.forced
        assert any("restricted to" in w for w in plan.warnings)
        assert "only=section0" in plan.describe()

    def test_deduplicates_and_keeps_order(self) -> None:
        plan = plan_update(make_bundle(), only=[SECTION1, SECTION0, SECTION1])
        assert plan.only == (SECTION1, SECTION0)

    def test_section0_problems_do_not_block_a_section1_only_plan(self) -> None:
        """Scoping past a section the caller isn't touching must actually work.

        A recovery flash of section 1 shouldn't be refused because section 0's
        descriptor couldn't be recovered — that gate protects a write we're not
        being asked to do.
        """
        bundle = FirmwareBundle(
            images={
                SECTION0: FirmwareImage(
                    name=SECTION0,
                    payload=b"\x00" * (BOOT_SECTOR_LEN + 0x1000),
                    source="fallback constants",
                    descriptor_recovered=False,
                ),
                SECTION1: FirmwareImage(
                    name=SECTION1,
                    payload=b"payload" * 512,
                    source="test",
                    container=None,
                ),
            }
        )
        plan = plan_update(bundle, only=[SECTION1])
        assert [s.name for s in plan.to_write] == [SECTION1]

    def test_rejects_an_empty_selection(self) -> None:
        """Selecting nothing must not look like "already up to date"."""
        with pytest.raises(LumagenFirmwareImageError, match="selects no sections"):
            plan_update(make_bundle(), only=[])

    def test_rejects_an_unknown_name(self) -> None:
        with pytest.raises(LumagenFirmwareImageError, match="unknown section"):
            plan_update(make_bundle(), only=["section2"])

    @pytest.mark.parametrize("chip", ["hdmi_rx", "hdmi_tx", "hdmi_ntx"])
    def test_rejects_chip_images_with_an_explanation(self, chip: str) -> None:
        """Chips are a deliberate scope decision, so say so rather than 'unknown'."""
        with pytest.raises(LumagenFirmwareImageError, match="no write path"):
            plan_update(make_bundle(), only=[chip])

    def test_rejects_section1_when_the_updater_has_none(self) -> None:
        bundle = extract_images(build_updater_exe(resources={}))
        with pytest.raises(LumagenFirmwareImageError, match="no section-1 image"):
            plan_update(bundle, only=[SECTION1])

    def test_writable_sections_is_the_advertised_set(self) -> None:
        assert WRITABLE_SECTIONS == (SECTION0, SECTION1)


class TestSessionPlumbing:
    """The flags have to survive the trip through run_update to the device."""

    async def test_force_rewrites_a_matching_slot(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        live = SECTION1_SLOTS["99"]
        target = SECTION1_SLOTS["00"]
        # Device already holds the image, so an unforced run would skip section 1.
        fake_firmware.commit(live.address, section1.wire_bytes, 0xFADE0011)

        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle, force=True)

        assert SECTION1 in result.written
        assert fake_firmware.region(target.address, len(section1.wire_bytes)) == (
            section1.wire_bytes
        )

    async def test_unforced_run_skips_the_matching_slot(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The control for the test above."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle)
        assert result.written == (SECTION0,)

    async def test_only_section0_leaves_section1_untouched(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        bundle = make_bundle()
        section0 = bundle.section0
        assert section0 is not None
        target = SECTION1_SLOTS["00"]

        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle, only=[SECTION0])

        assert result.written == (SECTION0,)
        # The section-1 slot was never erased or written.
        assert fake_firmware.region(target.address, 16) == b"\xff" * 16
        assert all(sector != target.first_sector for sector, _ in fake_firmware.erases)

    async def test_only_section1_does_not_promote(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Section 1 has no promotion step, so nothing should reach live section 0."""
        bundle = make_bundle()
        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle, only=[SECTION1])

        assert result.written == (SECTION1,)
        assert not result.promoted
        assert fake_firmware.copies == []
        assert fake_firmware.region(SCRATCH_ADDR, 16) == b"\xff" * 16

    async def test_only_section0_with_no_promote_stages_only(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """The recommended first on-device write test: full path, nothing at stake."""
        bundle = make_bundle()
        section0 = bundle.section0
        assert section0 is not None
        wire = section0.wire_bytes

        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle, only=[SECTION0], promote=False)

        assert result.written == (SECTION0,)
        assert not result.promoted
        assert fake_firmware.region(SCRATCH_ADDR, len(wire)) == wire
        assert fake_firmware.region(ADDR_LIVE, 16) == b"\xff" * 16
        assert fake_firmware.copies == []

    async def test_dry_run_honours_force_in_the_reported_plan(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """A forced dry run must show the forced plan, or it previews the wrong thing."""
        bundle = make_bundle()
        section1 = bundle.section1
        assert section1 is not None
        fake_firmware.commit(SECTION1_SLOTS["99"].address, section1.wire_bytes, 0xFADE0011)

        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        result = await session.run_update(bundle, dry_run=True, force=True)

        assert result.plan.forced
        assert result.plan.writes_section1
        assert result.written == ()
        assert fake_firmware.erases == []

    async def test_rejects_a_prebuilt_plan_combined_with_force(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        """Silently ignoring a flag on a destructive operation is not acceptable."""
        bundle = make_bundle()
        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        with pytest.raises(LumagenFirmwareError, match="not both"):
            await session.run_update(bundle, plan=plan_update(bundle), force=True)

    async def test_rejects_a_prebuilt_plan_combined_with_only(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        bundle = make_bundle()
        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        with pytest.raises(LumagenFirmwareError, match="not both"):
            await session.run_update(bundle, plan=plan_update(bundle), only=[SECTION0])

    async def test_an_unwritable_selection_fails_before_any_erase(
        self, fake_firmware: FakeFirmwareTransport
    ) -> None:
        bundle = make_bundle()
        session = FirmwareSession(transport=fake_firmware)  # type: ignore[arg-type]
        await session.connect()
        with pytest.raises(LumagenFirmwareImageError, match="no write path"):
            await session.run_update(bundle, only=["hdmi_rx"])
        assert fake_firmware.erases == []
        assert fake_firmware.payload_writes == []


class TestBundleWithoutSection1:
    def test_default_plan_omits_it_without_complaint(self) -> None:
        """Not every updater need carry a section 1; only an explicit request errors."""
        bundle = extract_images(build_updater_exe(resources={}))
        plan = plan_update(bundle)
        assert [s.name for s in plan.to_write] == [SECTION0]
        assert not any(s.name == SECTION1 for s in plan.sections)

    def test_force_does_not_invent_one(self) -> None:
        bundle = extract_images(build_updater_exe(resources={}))
        assert [s.name for s in plan_update(bundle, force=True).to_write] == [SECTION0]
