"""Command-builder unit tests.

Wire encodings only — no transport, no client. Where a builder's output is
non-obvious the test cites the line in
``lumagen-research/Tip0011_RS232CommandInterface_111023.pdf`` it comes from.
"""

from __future__ import annotations

import pytest

from aiolumagen.commands import input_command
from aiolumagen.exceptions import LumagenCommandError


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, "i1"),
        (2, "i2"),
        (9, "i9"),
    ],
)
def test_input_command_single_digit_inputs(number: int, expected: str) -> None:
    """Inputs 1-9 are ``i`` plus the digit."""
    assert input_command(number) == expected


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (10, "i+0"),
        (12, "i+2"),
        (19, "i+9"),
    ],
)
def test_input_command_uses_plus_prefix_above_nine(number: int, expected: str) -> None:
    """Inputs 10-19 use the ``+`` modifier, NOT a two-digit number.

    Regression. Tip0011's ASCII command list states the encoding twice:

        ``INPUT`` / ``i`` — Choose input (i.e. ``i2`` for input 2 and ``i+2``
        for input 12)

        ``10+`` / ``+`` — Add 10 to the next digit entered for input selection

    This function used to return ``f"i{n}"`` for the whole 1-19 range, so
    input 12 went out as ``i12`` — which the device reads as "select input 1"
    followed by a stray digit. Both halves are valid commands, so nothing
    errored; the wrong input was simply selected. ``i+2`` is the doc's own
    example, which is why it's pinned here explicitly.
    """
    assert input_command(number) == expected


@pytest.mark.parametrize("number", [0, -1, 20, 100])
def test_input_command_rejects_out_of_range(number: int) -> None:
    assert not 1 <= number <= 19
    with pytest.raises(LumagenCommandError, match="1-19"):
        input_command(number)


def test_input_command_boundary_switches_encoding_at_ten() -> None:
    """9 -> 10 is where the encoding changes; pin both sides together."""
    assert input_command(9) == "i9"
    assert input_command(10) == "i+0"
