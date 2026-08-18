"""Lumagen Radiance Pro command constants and helpers.

The Lumagen speaks short ASCII commands over 9600 8N1. Most commands are a
single character with no terminator; a handful are documented to need a
carriage return. The constants in this module mirror the command tables in
``lumagen-research/Tip0011_RS232CommandInterface_111023.pdf``.

**This module is the single source of truth for Lumagen wire commands.**
Consumers — including ``ha-lumagen``'s button/select/switch/remote tables —
must reference these members rather than re-declaring the literals. The
table is hostile to hand-transcription: most commands are a single opaque
character, and **eight pairs differ only by letter case while meaning
unrelated things**:

===========  ==============  ==============================
Lower        Upper           Nothing in common
===========  ==============  ==============================
``v`` down   ``V``           auto-aspect off
``w`` 16:9   ``W``           2.35
``s`` OSD    ``S``           save
``g`` OSD on ``G``           2.40
``n`` 4:3    ``N``           non-linear stretch
``l`` lbox   ``L``           output zone select
``a`` mem A  ``A``           1.90
``c`` mem C  ``C``           2.00
===========  ==============  ==============================

A mistyped literal is a valid command for a *different* operation and
nothing will reject it — pressing "memory A" would silently change the
aspect ratio to 1.90. A mistyped enum member is an ``AttributeError`` at
import. That asymmetry is the whole reason this module exists.

Every enum here is a :class:`~enum.StrEnum`, so members are ``str``
instances and can be handed straight to
:meth:`~aiolumagen.client.LumagenClient.send_command` without ``.value``.
"""

from __future__ import annotations

import textwrap
from enum import StrEnum

from aiolumagen.exceptions import LumagenCommandError
from aiolumagen.state import HdrGammaMode, SharpnessSensitivity


class Input(StrEnum):
    """Direct input-select commands (1-8 buttons).

    For inputs 9-19 on models that support them, do **not** hand-build the
    command — the encoding changes above 9 (see :func:`input_command`). Use
    :meth:`~aiolumagen.client.LumagenClient.set_input`, which routes through
    that encoder.
    """

    INPUT_1 = "i1"
    INPUT_2 = "i2"
    INPUT_3 = "i3"
    INPUT_4 = "i4"
    INPUT_5 = "i5"
    INPUT_6 = "i6"
    INPUT_7 = "i7"
    INPUT_8 = "i8"
    PREVIOUS = "P"


class Aspect(StrEnum):
    """Aspect-ratio commands, in ascending ratio order.

    The wider ratios are Radiance Pro only, and three of them are two-byte
    sequences prefixed with ``+``. That prefix is a modifier the device
    applies to the following key, so ``+j`` is *not* "1.85 then something" —
    it selects 2.10 outright. Send it as one unit.

    Also documented but not modelled here, since each needs its own UI
    affordance and nothing has asked for them yet: the no-zoom variants
    (``[`` 4:3, ``]`` letterbox, ``/`` 1.85, ``K`` 2.35) and the pillarbox
    set (``+n`` 4:3, ``+l`` 1.375, ``+w`` 1.66).
    """

    RATIO_4_3 = "n"
    LETTERBOX = "l"
    RATIO_16_9 = "w"
    RATIO_16_9_NZ = "*"
    RATIO_1_85 = "j"
    RATIO_1_90 = "A"
    RATIO_2_00 = "C"
    RATIO_2_10 = "+j"
    RATIO_2_20 = "E"
    RATIO_2_35 = "W"
    RATIO_2_40 = "G"
    RATIO_2_55 = "+W"
    RATIO_2_76 = "+N"
    AUTO_ENABLE = "~"
    AUTO_DISABLE = "V"

    NLS = "N"
    """Non-linear stretch.

    A modifier, not a standalone mode: Tip0011 says to send the source aspect
    first and then this. The device also publishes whether NLS is currently
    engaged, via :attr:`~aiolumagen.state.LumagenState.nls_active`.
    """


class Memory(StrEnum):
    """Memory-slot select commands."""

    A = "a"
    B = "b"
    C = "c"
    D = "d"


class Navigation(StrEnum):
    """OSD navigation commands."""

    MENU = "M"
    EXIT = "X"
    OK = "k"
    MENU_OFF = "!"
    UP = "^"
    DOWN = "v"
    LEFT = "<"
    RIGHT = ">"

    HELP = "U"
    """Show on-screen help for the currently highlighted menu item.

    Only meaningful with the menu open — there is no highlighted item
    otherwise.
    """

    ALT = "#"
    """Modifier that selects a key's alternate function.

    Prefixes the key it modifies, e.g. ALT + 2.35 selects 2.40 input aspect.
    Most of those alternates already have a direct command in :class:`Aspect`,
    so reach for this only for a function that has none.

    Note the device's *delimiter mode* remaps this to ``:``; this library
    never enables delimiter mode, so ``#`` is correct here.
    """


class Power(StrEnum):
    """Power commands."""

    ON = "%"
    STANDBY = "$"


class Misc(StrEnum):
    """Miscellaneous single-shot commands."""

    SAVE = "S"
    """Begin a save, as the remote's SAVE key does — needs ``OK`` to confirm.

    Two keystrokes and it leaves a confirmation prompt on screen if the second
    never arrives. For unattended use prefer
    :func:`save_config_command`, which commits in one shot.
    """

    HDR_SETUP = "Y"
    TEST_PATTERN = "H"
    OSD_ON = "g"
    OSD_OFF = "s"

    ZONE = "L"
    """Output zone select."""


class Query(StrEnum):
    """Query commands. All expect a ``!``-prefixed response.

    Every member here has a sending method on
    :class:`~aiolumagen.client.LumagenClient`. Don't add one speculatively —
    a ``Query`` nobody sends means a parser branch nobody can reach.
    ``ZQS00`` (alive) and ``ZQI01`` (input video format) were removed for
    exactly that reason: byte-level liveness in
    ``LumagenClient._on_bytes_received`` supersedes the former, and nothing
    ever consumed the latter.
    """

    DEVICE_INFO = "ZQS01"
    POWER = "ZQS02"
    INPUT_INFO = "ZQI00"
    # No ZQI24 (Full v4) query: v5 is the supported floor. The !I21-!I24
    # *parsers* stay, because a device whose reporting menu is still set to
    # Full v4 pushes !I24 unsolicited.
    FULL_STATUS_V5 = "ZQI25"
    SHARPNESS = "ZQI30"
    DISPLAY_REC2020 = "ZQI50"
    SOURCE_HDR_STATUS = "ZQI52"
    GAME_MODE = "ZQI53"
    AUTO_ASPECT = "ZQI54"
    # The only query that reports output width outright. Everything else forces
    # width to be inferred from height x aspect, which is wrong for a scaled
    # output — see LumagenState.output_width.
    OUTPUT_MODE = "ZQO01"


# Echo mode — sent once at startup to reduce command echoing and enable
# the Lumagen's "Full v4"/"Full v5" unsolicited status reports (the report
# format is set separately via the device's menu; this just turns on the
# echo-off mode that keeps responses clean). See the Lumagen RS-232 doc.
ECHO_OFF_WITH_STATUS = "ZE2"


def input_command(n: int) -> str:
    """Return the command string to select input ``n`` (1-19).

    Inputs 1-9 are ``i`` followed by the digit. **Inputs 10-19 are not
    ``i10``-``i19``** — Tip0011's command table spells this out twice:

        ``INPUT`` / ``i`` — Choose input (i.e. ``i2`` for input 2 and
        ``i+2`` for input 12)

        ``10+`` / ``+`` — Add 10 to the next digit entered for input
        selection

    So ``+`` is a prefix modifier consumed by the *next* digit, and input 12
    is ``i+2``. This function previously emitted ``f"i{n}"`` across the whole
    1-19 range, which for n>=10 sent ``i1`` (select input 1) followed by a
    stray digit the device treats as menu input. It was silently wrong — a
    valid command sequence for the wrong operation, so nothing rejected it.
    """
    if not 1 <= n <= 19:
        raise LumagenCommandError(f"input must be 1-19, got {n}")
    if n <= 9:
        return f"i{n}"
    return f"i+{n - 10}"


def sharpness_command(
    *,
    enabled: bool,
    level: int,
    sensitivity: SharpnessSensitivity = SharpnessSensitivity.NORMAL,
) -> str:
    """Build a ``ZY521ELS`` sharpness command. Requires CR terminator on send.

    :param enabled: ``True`` for ``Y`` (sharpening on), ``False`` for ``N``.
    :param level: Sharpening intensity, 0-7 (7 = strongest).
    :param sensitivity: :class:`~aiolumagen.state.SharpnessSensitivity`. A bare
        ``"H"``/``"N"`` string is still accepted and coerced, so callers that
        carry the wire letter around keep working.

    Per the Lumagen RS-232 doc (Tip0011, ``ZY521ELS<CR>``), this sets both
    horizontal and vertical sharpness to the same level. For independent
    H/V control use ``ZY522`` (not yet wrapped — call via ``send_command``).
    """
    if not 0 <= level <= 7:
        raise LumagenCommandError(f"sharpness level must be 0-7, got {level}")
    try:
        sens = SharpnessSensitivity(sensitivity)
    except ValueError:
        raise LumagenCommandError(
            f"sharpness sensitivity must be 'H' or 'N', got {sensitivity!r}"
        ) from None
    e = "Y" if enabled else "N"
    return f"ZY521{e}{level}{sens.value}"


def game_mode_command(enabled: bool) -> str:
    """Build a ``ZY551X`` game-mode command. Requires CR terminator on send."""
    return "ZY5511" if enabled else "ZY5510"


def fan_speed_command(speed: int) -> str:
    """Build a ``ZY552X`` minimum-fan-speed command. Speed 1-10.

    ``speed`` is the value the Lumagen shows in its own menu; the wire
    digit is one lower. Confirmed empirically: sending ``ZY5524`` makes
    the device report a minimum fan speed of 5, and ``ZY5523`` reports 4.
    So the documented ``X=0-9`` range (from
    ``lumagen-research/FIRMWARE_REVERSE_ENGINEERING_FINDINGS.md`` — this
    command isn't in the Tip0011 PDF at all) is a 0-based index into a
    1-based display. Callers work in the device's units and this encoder
    owns the conversion, so there's only one notion of "fan speed" in the
    library.

    Requires CR terminator on send.
    """
    if not 1 <= speed <= 10:
        raise LumagenCommandError(f"fan speed must be 1-10, got {speed}")
    return f"ZY552{speed - 1}"


def subtitle_shift_command(level: int) -> str:
    """Build a ``ZY553X`` subtitle-shift command. Level 0/1/2.

    Requires CR terminator on send. Reverse-engineered from the firmware;
    the bundled Tip0011 PDF doesn't document this command explicitly.
    """
    if level not in (0, 1, 2):
        raise LumagenCommandError(f"subtitle shift must be 0, 1, or 2; got {level}")
    return f"ZY553{level}"


def reset_auto_aspect_command() -> str:
    """``ZY550`` — reset and reinitiate automatic aspect detection."""
    return "ZY550"


def hdr_intensity_mapping_command(*, display_max_nits: int, gamma_mode: HdrGammaMode) -> str:
    """Build a ``ZY417XXXXXG`` HDR intensity-mapping command.

    Requires a CR terminator on send. Per Tip0011:

    * ``XXXXX`` — display peak luminance in nits, zero-padded to 5 digits.
      ``00000`` disables HDR mapping; otherwise ``00050``-``10000`` sets
      the display's max level (target nits the Lumagen tone-maps toward).
    * ``G`` — gamma mode: ``A`` (auto, recommended), ``H`` (force HDR),
      ``S`` (force SDR).

    The command applies to the **currently selected output CMS**. The
    Lumagen menu reaches the same setting at
    *Output → CMS → CMSx → HDR Mapping*. There's no documented query
    that returns the current value, so callers wanting to track state
    have to remember what they last sent (the integration handles this
    optimistically).

    :param display_max_nits: 0 (disable mapping), or 50-10000 (active).
    :param gamma_mode: :class:`~aiolumagen.state.HdrGammaMode`. A bare
        ``"A"``/``"H"``/``"S"`` string is still accepted and coerced.
    """
    if display_max_nits != 0 and not 50 <= display_max_nits <= 10000:
        raise LumagenCommandError(
            f"display_max_nits must be 0 (disable) or 50-10000, got {display_max_nits}"
        )
    try:
        gamma = HdrGammaMode(gamma_mode)
    except ValueError:
        raise LumagenCommandError(
            f"gamma_mode must be 'A', 'H', or 'S'; got {gamma_mode!r}"
        ) from None
    return f"ZY417{display_max_nits:05d}{gamma.value}"


# ----------------------------------------------------------------------
# On-screen display messages (ZT / ZB / ZC)
# ----------------------------------------------------------------------

OSD_LINE_LENGTH = 30
"""Characters per OSD row, per Tip0011's ``ZTMxxxx`` entry."""

OSD_LINE_COUNT = 2
"""Rows the OSD message area provides."""

OSD_MIN_CHAR = 0x20
OSD_MAX_CHAR = 0x7A
"""Inclusive bounds of the renderable OSD character range (space through ``z``).

Worth noting what falls *outside* the top of this range: ``{`` (0x7B) is the
device's alternative message terminator, and ``|`` ``}`` ``~`` follow it. A
stray ``{`` in message text would end the message early and leave the rest to
be interpreted as commands, which is the main reason
:func:`sanitize_osd_text` exists rather than trusting callers.
"""

OSD_PERSIST_DURATION = 9
"""Duration value that leaves the message up until explicitly cleared."""

OSD_TRUNCATION_PLACEHOLDER = "..."
"""Marks text dropped because it exceeded two rows.

Three periods rather than a single ellipsis character: ``…`` is 0x2026, far
outside the renderable range, so it would be stripped and the truncation would
pass unnoticed.
"""


def sanitize_osd_text(text: str) -> str:
    """Drop every character the OSD can't render.

    Silent removal is deliberate here, and is the opposite of the choice made
    for input labels (:func:`input_label_command` raises instead). The
    difference is provenance: OSD text is typically generated — a notification
    body, a template render, a media title — so a stray character should cost
    that character, not the whole message. A label is something a person typed
    once and expects to see back verbatim, so silently altering it would be
    worse than refusing it.
    """
    return "".join(char for char in text if OSD_MIN_CHAR <= ord(char) <= OSD_MAX_CHAR)


def _osd_row(text: str, *, center: bool, pad: bool) -> str:
    """Fit one already-sanitized string into a single OSD row."""
    row = text[:OSD_LINE_LENGTH]
    if center:
        # str.center pads to exactly the field width, so it implies pad.
        return row.center(OSD_LINE_LENGTH)
    if pad:
        return row.ljust(OSD_LINE_LENGTH)
    return row


def osd_message_command(
    *,
    text: str | None = None,
    line1: str | None = None,
    line2: str | None = None,
    duration: int = 3,
    center: bool = False,
) -> str:
    """Build a ``ZTMxxxx`` on-screen message. Requires a CR terminator on send.

    Pass either ``text`` (word-wrapped across both rows) or ``line1`` /
    ``line2`` (placed verbatim), not both. Wrapping uses the stdlib, including
    its long-word splitting, and marks dropped content with
    :data:`OSD_TRUNCATION_PLACEHOLDER` so a message that didn't fit doesn't
    look like one that simply ended.

    :param text: Message to wrap across up to :data:`OSD_LINE_COUNT` rows.
    :param line1: Explicit first row.
    :param line2: Explicit second row.
    :param duration: ``0``-``9``. :data:`OSD_PERSIST_DURATION` leaves the
        message up until :func:`osd_clear_command`; the lower values are
        device-defined steps that Tip0011 doesn't quantify.
    :param center: Centre each row within the 30-character field.

    The row-one padding is load-bearing: the device fills the field
    left-to-right with no row delimiter, so row two only starts where it
    should if row one occupies exactly 30 characters. A single-row message
    isn't padded, since there's nothing after it to position.

    :raises LumagenCommandError: for a duration outside 0-9, for passing both
        ``text`` and explicit rows, or when nothing renderable is left after
        sanitisation (use :func:`osd_clear_command` to clear the OSD — sending
        an empty message is not how that's done).
    """
    if not 0 <= duration <= 9:
        raise LumagenCommandError(f"OSD duration must be 0-9, got {duration}")
    explicit = line1 is not None or line2 is not None
    if text is not None and explicit:
        raise LumagenCommandError(
            "pass either text= (wrapped) or line1=/line2= (verbatim), not both"
        )

    if explicit:
        rows = [sanitize_osd_text(line1 or ""), sanitize_osd_text(line2 or "")]
        # Trailing empty row carries no information; drop it so a one-row
        # message isn't padded out to 60 characters for nothing.
        if not rows[1].strip():
            rows = rows[:1]
    else:
        clean = sanitize_osd_text(text or "")
        rows = textwrap.wrap(
            clean,
            width=OSD_LINE_LENGTH,
            max_lines=OSD_LINE_COUNT,
            placeholder=OSD_TRUNCATION_PLACEHOLDER,
        )

    if not any(row.strip() for row in rows):
        raise LumagenCommandError(
            "OSD message is empty after removing unrenderable characters; "
            "use osd_clear_command() to clear the display"
        )

    last = len(rows) - 1
    payload = "".join(
        _osd_row(row, center=center, pad=index != last) for index, row in enumerate(rows)
    )
    return f"ZT{duration}{payload}"


def osd_clear_command() -> str:
    """``ZC`` — clear any on-screen message. No CR terminator."""
    return "ZC"


def osd_block_char_command(char: str) -> str:
    """``ZB<X>`` — render ``char`` as a solid block in OSD messages. No CR.

    This is how the device draws a bar: nominate a character, then repeat it in
    a message. Picking one that won't appear in real text (``#``, ``~``'s
    neighbours are out of range, so something like ``#`` or ``@``) keeps the
    two uses from colliding.

    The setting is global and sticky, which is the sharp edge — every later
    message renders that character as a block too. Nominating a space is legal
    and turns all whitespace into bars; it isn't rejected here because the
    device permits it, but it's almost never what's wanted.

    :raises LumagenCommandError: unless ``char`` is exactly one renderable
        character.
    """
    if len(char) != 1:
        raise LumagenCommandError(
            f"OSD block character must be exactly one character, got {char!r}"
        )
    if not OSD_MIN_CHAR <= ord(char) <= OSD_MAX_CHAR:
        raise LumagenCommandError(
            f"OSD block character must be in the renderable range "
            f"{OSD_MIN_CHAR:#04x}-{OSD_MAX_CHAR:#04x}, got {char!r}"
        )
    return f"ZB{char}"


# ----------------------------------------------------------------------
# Input labels, hotplug, and configuration
# ----------------------------------------------------------------------

INPUT_LABEL_MAX_LENGTH = 10
"""Longest input label the device stores, per Tip0011's ``ZY524`` entry."""

_INPUT_LABEL_MEMORIES = ("A", "B", "C", "D")
_INPUT_LABEL_ALL_MEMORIES = "0"


def input_label_command(input_number: int, label: str, *, memory: str = "A") -> str:
    """Build a ``ZY524XYlabel`` input-label write. Requires a CR terminator.

    Labels are per (input, memory), so the same physical source can read
    differently under each memory bank. Passing ``memory="ALL"`` writes the
    label to A through D at once, which is what you want unless the banks are
    deliberately configured to show different names.

    :param input_number: Logical input, 1-8. Narrower than the 1-19 range
        :func:`input_command` accepts because labelling is only defined for the
        first eight.
    :param label: Up to :data:`INPUT_LABEL_MAX_LENGTH` renderable characters.
        Spaces are fine and preserved — Tip0011's own example is ``Roku 2A``.
        An empty label is passed through; the doc doesn't say whether that
        clears the label or is ignored, so treat the result as unverified.
    :param memory: ``"A"``-``"D"``, or ``"ALL"`` for every bank.

    :raises LumagenCommandError: on an out-of-range input or memory, a label
        over the length limit, or a label containing characters the OSD can't
        render. Unlike OSD message text, an illegal label is refused rather
        than silently stripped — see :func:`sanitize_osd_text` for why.
    """
    if not 1 <= input_number <= 8:
        raise LumagenCommandError(f"input-label input must be 1-8, got {input_number}")
    bank = memory.upper()
    if bank in ("ALL", _INPUT_LABEL_ALL_MEMORIES):
        selector = _INPUT_LABEL_ALL_MEMORIES
    elif bank in _INPUT_LABEL_MEMORIES:
        selector = bank
    else:
        raise LumagenCommandError(f"input-label memory must be 'A'-'D' or 'ALL', got {memory!r}")
    if len(label) > INPUT_LABEL_MAX_LENGTH:
        raise LumagenCommandError(
            f"input label must be {INPUT_LABEL_MAX_LENGTH} characters or fewer, "
            f"got {len(label)} ({label!r})"
        )
    illegal = sorted({c for c in label if not OSD_MIN_CHAR <= ord(c) <= OSD_MAX_CHAR})
    if illegal:
        raise LumagenCommandError(
            f"input label contains characters the Lumagen cannot display: {''.join(illegal)!r}"
        )
    return f"ZY524{selector}{input_number - 1}{label}"


def input_restart_command(input_number: int | str = "all") -> str:
    """Build a ``ZY520X`` HDMI hotplug toggle. Requires a CR terminator.

    Pulses hotplug detect so the source re-reads EDID and renegotiates. This is
    the documented remedy for a source stuck at the wrong resolution, missing
    audio format, or a blank picture after a chain change — the equivalent of
    reseating the cable without reaching behind the rack.

    :param input_number: Logical input 1-8, or ``"all"`` for every HDMI input.

    **Radiance Pro encoding.** Inputs 1-8 map to ``0``-``7`` and "all" is
    ``A``. Tip0011 gives a different mapping for earlier Radiance models, where
    only ``0``-``5`` are inputs and ``7`` means all — so on a non-Pro unit,
    asking for input 8 here would restart every input instead. This library
    targets the Pro.

    :raises LumagenCommandError: on an out-of-range input or an unrecognised
        string.
    """
    if isinstance(input_number, str):
        if input_number.strip().upper() not in ("ALL", "A"):
            raise LumagenCommandError(f"input restart takes 1-8 or 'all', got {input_number!r}")
        return "ZY520A"
    if not 1 <= input_number <= 8:
        raise LumagenCommandError(f"input restart takes 1-8 or 'all', got {input_number}")
    return f"ZY520{input_number - 1}"


def show_aspect_command() -> str:
    """``ZY811`` — pop the current input and aspect onto the OSD. Requires CR.

    Reverse-engineered from the firmware's command table rather than taken from
    Tip0011, which doesn't list it. Harmless if unsupported: an unrecognised
    ``ZY`` command is ignored, so the failure mode is "no overlay appears".
    """
    return "ZY811"


def save_config_command() -> str:
    """``ZY6SAVECONFIG`` — commit the running configuration to flash. Requires CR.

    Single-shot, unlike :attr:`Misc.SAVE`, which mirrors the remote's SAVE key
    and needs an ``OK`` to confirm. Prefer this for anything unattended: a
    two-keystroke save that loses its second keystroke leaves a confirmation
    prompt on screen.

    Tip0011 attaches one precondition — exit any on-screen test pattern first,
    or the save may capture the pattern as configuration.
    """
    return "ZY6SAVECONFIG"
