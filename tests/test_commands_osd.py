"""Command-builder tests for OSD messaging, labels, hotplug and config.

Wire encodings only. Where an encoding is non-obvious the test cites the
``lumagen-research/Tip0011_RS232CommandInterface_111023.pdf`` entry it comes from,
because most of these are opaque single characters or fixed-width fields where
an off-by-one is invisible in the output.
"""

from __future__ import annotations

import pytest

from aiolumagen import (
    INPUT_LABEL_MAX_LENGTH,
    OSD_LINE_COUNT,
    OSD_LINE_LENGTH,
    Aspect,
    Memory,
    Misc,
    Navigation,
)
from aiolumagen.commands import (
    input_label_command,
    input_restart_command,
    osd_block_char_command,
    osd_clear_command,
    osd_message_command,
    sanitize_osd_text,
    save_config_command,
    show_aspect_command,
)
from aiolumagen.exceptions import LumagenCommandError


def _payload(command: str) -> str:
    """Strip the ``ZT<duration>`` prefix, leaving the row field."""
    assert command.startswith("ZT")
    return command[3:]


# ---------- OSD: character range ----------


def test_sanitize_keeps_the_full_renderable_range() -> None:
    """Space through 'z' inclusive, per Tip0011's ZT entry."""
    renderable = "".join(chr(c) for c in range(0x20, 0x7B))
    assert sanitize_osd_text(renderable) == renderable


@pytest.mark.parametrize("char", ["{", "|", "}", "~", "\n", "\t", "\x00", "é", "…"])
def test_sanitize_drops_unrenderable_characters(char: str) -> None:
    assert sanitize_osd_text(f"a{char}b") == "ab"


def test_sanitize_drops_the_alternate_terminator() -> None:
    """'{' is the device's other end-of-message marker, not a printable char.

    Letting one through would end the message early and leave the remainder to
    be read as commands — the one stripped character with consequences beyond
    a cosmetic gap.
    """
    assert "{" not in sanitize_osd_text("Vol {50}")
    assert sanitize_osd_text("Vol {50}") == "Vol 50"


# ---------- OSD: row layout ----------


def test_message_wraps_across_two_rows_and_pads_the_first() -> None:
    """Row one must occupy exactly 30 chars or row two starts in the wrong place.

    The device fills the field left-to-right with no row delimiter, so the
    padding is what positions the second row.
    """
    command = osd_message_command(text="Now playing: The Matrix Reloaded", duration=5)
    assert command.startswith("ZT5")
    payload = _payload(command)
    assert payload[:OSD_LINE_LENGTH] == "Now playing: The Matrix".ljust(OSD_LINE_LENGTH)
    assert payload[OSD_LINE_LENGTH:] == "Reloaded"


def test_single_row_message_is_not_padded() -> None:
    """Nothing follows it, so padding would only waste wire time."""
    assert osd_message_command(text="Volume 42") == "ZT3Volume 42"


def test_explicit_rows_are_placed_verbatim() -> None:
    command = osd_message_command(line1="Input 3", line2="Apple TV")
    payload = _payload(command)
    assert payload == "Input 3".ljust(OSD_LINE_LENGTH) + "Apple TV"


def test_explicit_second_row_alone_still_occupies_row_two() -> None:
    """line2 without line1 must leave row one blank, not promote the text."""
    payload = _payload(osd_message_command(line1="", line2="bottom"))
    assert payload == " " * OSD_LINE_LENGTH + "bottom"


def test_blank_second_row_collapses_to_a_single_row() -> None:
    assert osd_message_command(line1="top", line2="") == "ZT3top"
    assert osd_message_command(line1="top", line2="   ") == "ZT3top"


def test_centering_pads_both_sides() -> None:
    payload = _payload(osd_message_command(text="Hi", center=True))
    assert payload == "Hi".center(OSD_LINE_LENGTH)
    assert len(payload) == OSD_LINE_LENGTH


def test_overlong_row_is_truncated_not_wrapped_when_explicit() -> None:
    """Explicit rows are verbatim: they clip at the field width, never reflow."""
    payload = _payload(osd_message_command(line1="x" * 50, line2="y"))
    assert payload == "x" * OSD_LINE_LENGTH + "y"


def test_truncation_is_marked_with_renderable_characters() -> None:
    """Text past two rows is dropped, and says so.

    The marker is three periods rather than a single ellipsis because U+2026 is
    outside the renderable range — it would be stripped and the truncation
    would pass unnoticed.
    """
    payload = _payload(osd_message_command(text=" ".join(["word"] * 40)))
    assert payload.endswith("...")
    assert len(payload) <= OSD_LINE_LENGTH * OSD_LINE_COUNT


# ---------- OSD: validation ----------


@pytest.mark.parametrize("duration", [0, 1, 5, 9])
def test_message_accepts_the_documented_duration_range(duration: int) -> None:
    assert osd_message_command(text="x", duration=duration).startswith(f"ZT{duration}")


@pytest.mark.parametrize("duration", [-1, 10, 99])
def test_message_rejects_durations_outside_0_9(duration: int) -> None:
    with pytest.raises(LumagenCommandError, match="0-9"):
        osd_message_command(text="x", duration=duration)


def test_message_rejects_mixing_wrapped_and_explicit_rows() -> None:
    """Ambiguous intent — the two modes would fight over the same field."""
    with pytest.raises(LumagenCommandError, match="not both"):
        osd_message_command(text="wrapped", line1="explicit")


@pytest.mark.parametrize("text", ["", "   ", "{}|~", "\n\t"])
def test_message_rejects_content_that_sanitises_to_nothing(text: str) -> None:
    """Sending an empty message is not how the OSD gets cleared."""
    with pytest.raises(LumagenCommandError, match="osd_clear_command"):
        osd_message_command(text=text)


# ---------- OSD: clear and block character ----------


def test_clear_command() -> None:
    assert osd_clear_command() == "ZC"


def test_block_char_command() -> None:
    assert osd_block_char_command("#") == "ZB#"


def test_block_char_allows_space_even_though_it_is_a_bad_idea() -> None:
    """The device permits it, so the library doesn't override that judgement.

    It turns every space in every later message into a block, which is why the
    docstring warns rather than the code refusing.
    """
    assert osd_block_char_command(" ") == "ZB "


@pytest.mark.parametrize("char", ["", "##", "abc"])
def test_block_char_requires_exactly_one_character(char: str) -> None:
    with pytest.raises(LumagenCommandError, match="exactly one"):
        osd_block_char_command(char)


@pytest.mark.parametrize("char", ["{", "~", "\n", "é"])
def test_block_char_must_be_renderable(char: str) -> None:
    with pytest.raises(LumagenCommandError, match="renderable range"):
        osd_block_char_command(char)


# ---------- Input labels (ZY524) ----------


def test_input_label_doc_example() -> None:
    """Tip0011: "ZY524A1Roku 2A" sets input 2, memory A, to "Roku 2A"."""
    assert input_label_command(2, "Roku 2A") == "ZY524A1Roku 2A"


def test_input_label_y_field_is_input_minus_one() -> None:
    assert input_label_command(1, "First") == "ZY524A0First"
    assert input_label_command(8, "Last") == "ZY524A7Last"


@pytest.mark.parametrize("memory", ["B", "b", "C", "D"])
def test_input_label_honours_the_memory_bank(memory: str) -> None:
    assert input_label_command(3, "X", memory=memory).startswith(f"ZY524{memory.upper()}2")


def test_input_label_all_memories_uses_selector_zero() -> None:
    """X='0' writes banks A through D in one command."""
    assert input_label_command(1, "Apple TV", memory="ALL") == "ZY52400Apple TV"
    assert input_label_command(1, "Apple TV", memory="all") == "ZY52400Apple TV"


def test_input_label_accepts_the_maximum_length() -> None:
    label = "x" * INPUT_LABEL_MAX_LENGTH
    assert input_label_command(1, label).endswith(label)


def test_input_label_rejects_an_overlong_label() -> None:
    """Refused rather than truncated: a silently shortened label is worse than
    an error, because the user would have to notice it on the device."""
    with pytest.raises(LumagenCommandError, match="characters or fewer"):
        input_label_command(1, "x" * (INPUT_LABEL_MAX_LENGTH + 1))


def test_input_label_rejects_unrenderable_characters() -> None:
    """Unlike OSD text, a label is refused rather than silently stripped.

    A label is typed once and expected back verbatim, so quietly altering it
    would be the worse failure.
    """
    with pytest.raises(LumagenCommandError, match="cannot display"):
        input_label_command(1, "Caf\u00e9")


@pytest.mark.parametrize("number", [0, 9, 19, -1])
def test_input_label_rejects_inputs_outside_1_8(number: int) -> None:
    """Narrower than input *selection*, which goes to 19 — labels only cover 8."""
    with pytest.raises(LumagenCommandError, match="1-8"):
        input_label_command(number, "X")


def test_input_label_rejects_an_unknown_memory() -> None:
    with pytest.raises(LumagenCommandError, match="'A'-'D' or 'ALL'"):
        input_label_command(1, "X", memory="Z")


# ---------- HDMI hotplug (ZY520) ----------


def test_input_restart_maps_inputs_to_zero_based_wire_values() -> None:
    """Radiance Pro: X=0-7 for inputs 1-8, 'A' for all."""
    assert input_restart_command(1) == "ZY5200"
    assert input_restart_command(8) == "ZY5207"


@pytest.mark.parametrize("value", ["all", "ALL", "a", "A", " all "])
def test_input_restart_all_inputs(value: str) -> None:
    assert input_restart_command(value) == "ZY520A"


def test_input_restart_defaults_to_all() -> None:
    assert input_restart_command() == "ZY520A"


@pytest.mark.parametrize("number", [0, 9, -1])
def test_input_restart_rejects_out_of_range_inputs(number: int) -> None:
    with pytest.raises(LumagenCommandError, match="1-8 or 'all'"):
        input_restart_command(number)


def test_input_restart_rejects_unknown_strings() -> None:
    with pytest.raises(LumagenCommandError, match="1-8 or 'all'"):
        input_restart_command("everything")


# ---------- Show aspect / save config ----------


def test_show_aspect_command() -> None:
    assert show_aspect_command() == "ZY811"


def test_save_config_command_is_the_single_shot_form() -> None:
    """Distinct from Misc.SAVE, which is the remote key and needs an OK."""
    assert save_config_command() == "ZY6SAVECONFIG"
    assert Misc.SAVE == "S"


# ---------- New enum members ----------


@pytest.mark.parametrize(
    ("member", "wire"),
    [
        (Aspect.RATIO_1_90, "A"),
        (Aspect.RATIO_2_00, "C"),
        (Aspect.RATIO_2_10, "+j"),
        (Aspect.RATIO_2_20, "E"),
        (Aspect.RATIO_2_35, "W"),
        (Aspect.RATIO_2_40, "G"),
        (Aspect.RATIO_2_55, "+W"),
        (Aspect.RATIO_2_76, "+N"),
        (Aspect.NLS, "N"),
        (Navigation.HELP, "U"),
        (Navigation.ALT, "#"),
        (Misc.ZONE, "L"),
    ],
)
def test_new_members_carry_the_documented_wire_value(member: str, wire: str) -> None:
    assert member == wire


def test_case_differing_pairs_stay_distinct() -> None:
    """The module's central hazard, now that the additions doubled the count.

    Each pair below differs only by case and means something unrelated, so a
    transcription slip is a valid command for the wrong operation. Enum members
    are what make that slip an AttributeError instead.
    """
    pairs = (
        (Navigation.DOWN, Aspect.AUTO_DISABLE),  # v / V
        (Aspect.RATIO_16_9, Aspect.RATIO_2_35),  # w / W
        (Misc.OSD_OFF, Misc.SAVE),  # s / S
        (Misc.OSD_ON, Aspect.RATIO_2_40),  # g / G
        (Aspect.RATIO_4_3, Aspect.NLS),  # n / N
        (Aspect.LETTERBOX, Misc.ZONE),  # l / L
        (Memory.A, Aspect.RATIO_1_90),  # a / A
        (Memory.C, Aspect.RATIO_2_00),  # c / C
    )
    for lower, upper in pairs:
        assert str(lower) != str(upper)
        assert str(lower).lower() == str(upper).lower()
