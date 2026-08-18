"""Typed state model for a Lumagen Radiance Pro.

The state dataclass is designed to plug into Home Assistant's
``DataUpdateCoordinator`` with ``always_update=False``: two equal
``LumagenState`` instances compare equal, so unchanged polls won't trigger
entity state writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Colorspace(StrEnum):
    """Output colorspace reported in the `!I24` E field."""

    REC_601 = "Rec.601"
    REC_709 = "Rec.709"
    REC_2020 = "Rec.2020"
    REC_2100 = "Rec.2100"


class HdrStatus(StrEnum):
    """Source dynamic range reported in the `!I24` F field."""

    SDR = "SDR"
    HDR = "HDR"


class InputStatus(StrEnum):
    """Input signal status reported in the `!I24` M field (index 0)."""

    NO_SOURCE = "No Source"
    ACTIVE = "Active"
    TEST_PATTERN = "Test Pattern"


class SourceMode(StrEnum):
    """Source scan mode reported in the `!I24`/`!I25` G field (index 17).

    The Lumagen historically used ``-`` to mean "no input detected"; the
    Full v5 firmware introduced ``n`` as a distinct value for the same
    condition (observed empirically — the older ``-`` may still appear
    during transient states). Both are exposed as the same enum member
    ``NO_INPUT`` so consumers can compare by identity without caring which
    wire byte the firmware sent.
    """

    INTERLACED = "i"
    PROGRESSIVE = "p"
    NO_INPUT = "-"
    NO_INPUT_V5 = "n"


class SharpnessSensitivity(StrEnum):
    """Sharpness sensitivity reported in the ``!I30`` / ``ZY521ELS`` S field.

    ``H`` — high (more aggressive edge detection)
    ``N`` — normal
    """

    HIGH = "H"
    NORMAL = "N"


class SubtitleShift(StrEnum):
    """Subtitle-shift amount, from the trailing ``!I25`` field at payload 25.

    Mirrors the three levels the ``ZY553X`` setter writes. Values follow the
    :class:`Colorspace` / :class:`InputStatus` precedent — a meaningful token
    mapped from the numeric wire code (``0``/``1``/``2``), not the code itself.

    **Empirically mapped, not documented.** ``ZQI25`` appears nowhere in
    ``Tip0011_RS232CommandInterface_111023.pdf``, and this repo's own recorded
    capture from firmware ``030225`` stops at payload 24 (power) — so on that
    firmware this field is simply absent and stays ``None``. The index is a
    hypothesis rather than a fact: verify against hardware before relying on
    it, and see the Full v5 section of :mod:`aiolumagen.protocol` for the
    evidence trail.
    """

    OFF = "Off"
    PERCENT_3 = "3%"
    PERCENT_6 = "6%"


class AutoAspectStatus(StrEnum):
    """Auto-aspect state, from the trailing ``!I25`` field at payload 26.

    Richer than the boolean :attr:`LumagenState.auto_aspect` (which comes from
    ``ZQI54``): the device distinguishes "off" from "disabled", where the
    latter means auto-aspect is configured but currently inhibited.

    Carries the same empirical caveat as :class:`SubtitleShift` — see that
    docstring. Because this field is unverified it deliberately does **not**
    feed :attr:`LumagenState.auto_aspect`, which stays sourced from the
    documented ``ZQI54`` query.
    """

    OFF = "Off"
    DISABLED = "Disabled"
    ON = "On"


class HdrGammaMode(StrEnum):
    """Gamma-mode flag in ``ZY417XXXXXG`` HDR intensity-mapping commands.

    Per Tip0011: the trailing ``G`` selects which gamma curve feeds the
    3D LUT during HDR-to-SDR mapping.

    * ``A`` — auto (Lumagen picks based on source metadata, recommended)
    * ``H`` — force HDR gamma
    * ``S`` — force SDR gamma

    There's no documented query for the active mode, so this enum only
    serves the write side — the integration tracks the last-set value
    optimistically.
    """

    AUTO = "A"
    HDR = "H"
    SDR = "S"


@dataclass(slots=True)
class LumagenState:
    """Snapshot of a Lumagen's reported state.

    All fields are optional — the Lumagen sends partial updates, and the
    protocol layer merges them into this single object over time. Fields
    remain ``None`` until the corresponding response has been seen at least
    once.

    Comparison (``__eq__``) is field-wise, suitable for
    ``DataUpdateCoordinator(always_update=False)``.
    """

    # --- Power / heartbeat ---
    power_on: bool | None = None
    alive: bool = False  # True after first !S00 seen

    # --- Device info (from !S01) ---
    model: str | None = None
    """Model *name*, e.g. ``RadiancePro``. Identical across the product line."""

    firmware: str | None = None
    """Software revision as an ``MMDDYY`` date code, e.g. ``030225``."""

    model_number: str | None = None
    """Manufacturer's model *number*, e.g. ``1018``.

    Unlike :attr:`model` this distinguishes hardware within the line —
    Tip0011 gives Radiance XD as 1009 and XE as 1010. There's no published
    number-to-name table beyond those, so treat it as an opaque identifier.
    """

    serial: str | None = None
    """Serial number as reported by ``!S01``, verbatim.

    **Not reliable as a unique identifier.** Some units report all zeros
    (``000000``); the value is passed through unchanged rather than
    normalised to ``None`` so consumers can tell "device said zeros" from
    "not yet observed". Check for a non-zero value before using it as
    identity.
    """

    device_info_raw: str | None = None
    """Whole ``!S01`` payload — kept so an unexpected field layout stays
    diagnosable even though the fields above are parsed out of it."""

    # --- Input info (from !I00) ---
    current_input: str | None = None
    input_memory: str | None = None
    input_info_raw: str | None = None

    # --- Input labels (from !S1x, populated by ZQS1XY queries) ---
    input_labels: dict[int, str] = field(default_factory=dict)
    """Logical input number (1-8) -> configured label (e.g. ``{2: "Apple TV"}``).

    Empty until :meth:`~aiolumagen.client.LumagenClient.query_input_labels`
    runs. The Lumagen's label response (``!S1x,<label>``) carries only the
    *memory* letter, not the input number, so the protocol layer correlates
    each response to the input the client last asked about (see
    ``LumagenProtocol.expect_input_label``). Labels are per (input, memory);
    this map holds one memory's worth (memory A by default).
    """

    # --- Full status (from !I24 / !I21-I23) ---
    input_status: InputStatus | None = None
    source_vrate: str | None = None
    source_resolution: str | None = None
    source_aspect: str | None = None
    content_aspect: str | None = None
    output_vrate: str | None = None
    output_resolution: str | None = None
    colorspace: Colorspace | None = None
    is_hdr: bool | None = None
    hdr_status: HdrStatus | None = None
    source_mode: SourceMode | None = None
    full_status_raw: str | None = None

    # --- Full status, extended source fields (from !I24 / !I25 only) ---
    # These are documented in Tip0011's ZQI24 layout but were previously
    # parsed and thrown away. Populated only on the !I24/!I25 path: the
    # doc's !I21 signature contradicts its own field list about whether
    # T/WWWW are present, so the shorter formats are left on the minimal
    # shared field set rather than guessing.
    input_config: str | None = None
    """``X`` — active input config number for the current input resolution."""

    source_3d_mode: str | None = None
    """``D`` — source 3D mode code (0, 1, 2, 4, 8). ``0`` is 2D."""

    nls_active: bool | None = None
    """``Y`` — Non-Linear Stretch active (``N`` = on, ``-`` = normal)."""

    physical_input: str | None = None
    """``KK`` — physical input backing the current virtual input (1-19).

    :attr:`current_input` is the *virtual* input the user selected; this is
    the HDMI port actually feeding it. They differ whenever input configs
    remap ports.
    """

    detected_source_aspect: str | None = None
    """``JJJ`` — raster aspect the device *detected*, vs :attr:`source_aspect`
    which is the one currently *applied*. The pair is what makes auto-aspect
    behaviour explainable."""

    detected_content_aspect: str | None = None
    """``LLL`` — detected content aspect, counterpart to
    :attr:`content_aspect`."""

    # --- Full status, extended output fields (from !I24 / !I25 only) ---
    output_aspect: str | None = None
    """``ZZZ`` — output raster aspect code (e.g. ``178``)."""

    output_scan_mode: SourceMode | None = None
    """``H`` — output scan mode. Shares :class:`SourceMode` with
    :attr:`source_mode`; the device sends this field uppercase (``I``/``P``)
    and the parser lowercases before coercion. Distinct from
    :attr:`output_mode_raw`, which is the whole ``!O01`` payload."""

    output_3d_mode: str | None = None
    """``T`` — output 3D mode code (0, 1, 2, 4, 8)."""

    output_enabled_mask: int | None = None
    """``WWWW`` — 16-bit output-enable bitmask, parsed from hex. Bit 0 is
    output 1. See :attr:`active_outputs` for the decoded form."""

    active_outputs: tuple[int, ...] | None = None
    """1-based output numbers currently enabled, decoded from
    :attr:`output_enabled_mask`. An empty tuple means all outputs off, which
    is a real state — distinct from ``None`` ("not observed")."""

    output_cms: int | None = None
    """``C`` — active output CMS (0-7)."""

    output_style: int | None = None
    """``B`` — active output style (0-7)."""

    # --- Derived values (decoded from the raw code fields above) ---
    # Computed by the parser from fields already on the wire — no extra
    # queries. See :mod:`aiolumagen.formatting` for the decoders.
    source_refresh_hz: float | None = None
    """:attr:`source_vrate` decoded to Hz (``059`` -> ``59.94``)."""

    output_refresh_hz: float | None = None
    """:attr:`output_vrate` decoded to Hz."""

    source_width: int | None = None
    """Source display width derived from :attr:`source_resolution` and
    :attr:`source_aspect`. The Lumagen never reports width directly."""

    output_width: int | None = None
    """Output display width — best available value.

    Sourced from :attr:`output_width_reported` when ``!O01`` has been seen,
    otherwise derived from :attr:`output_resolution` and :attr:`output_aspect`.

    Prefer the reported value because the derivation is only valid when the
    aspect field describes the *raster*. For the source it does (index 5 is
    documented as source raster aspect), but the output aspect is the aspect
    of the image being produced, which need not match the raster: a 4096x2160
    output feeding an anamorphic lens reports an output aspect of 2.37, and
    ``2160 x 2.37`` gives 5119 rather than 4096. That is not an imprecision to
    be snapped away — it is the wrong input."""
    output_width_reported: int | None = None
    """Output width as the device itself reports it, from ``!O01`` field 1.

    Authoritative, unlike the aspect-derived fallback. ``None`` until ``!O01``
    has been seen — which requires :meth:`~aiolumagen.client.LumagenClient
    .query_output_mode` to have run, since the field never rides the ``!I25``
    push stream."""
    output_height_reported: int | None = None
    """Output vertical resolution from ``!O01`` field 2.

    Kept for cross-checking against :attr:`output_resolution`, which carries
    the same number via the status push."""

    # --- Full v5 trailing fields, empirically mapped ---
    subtitle_shift: SubtitleShift | None = None
    """Payload index 25. See :class:`SubtitleShift` for the caveat — absent on
    firmware ``030225`` and unverified anywhere."""

    auto_aspect_status: AutoAspectStatus | None = None
    """Payload index 26. See :class:`AutoAspectStatus`. Deliberately does not
    feed :attr:`auto_aspect`, which stays sourced from ``ZQI54``."""

    # --- Output mode (from !O01) ---
    output_mode_raw: str | None = None

    # --- Sharpness (from !I30 / ZY521ELS) ---
    sharpness_enabled: bool | None = None
    sharpness_level: int | None = None  # 0-7
    sharpness_sensitivity: SharpnessSensitivity | None = None
    sharpness_raw: str | None = None
    """Raw payload of the most recent !I30 — kept for diagnostic dumps."""

    # --- Game mode (from !I53 / ZY551) ---
    game_mode: bool | None = None

    # --- Auto aspect (from !I54) ---
    auto_aspect: bool | None = None

    # --- HDR — source mastering metadata (from !I52) ---
    # Populated only when V=1 (source is actually HDR). For SDR sources
    # the device reports placeholder zeros; we leave fields as None
    # rather than expose those as "0 nits" which would be misleading.
    hdr_source_min_luminance: float | None = None
    """Source mastering display minimum luminance in nits (e.g. 0.0050)."""

    hdr_source_max_luminance: int | None = None
    """Source mastering display max luminance in nits (e.g. 1000)."""

    hdr_source_max_cll: int | None = None
    """Source MaxCLL in nits (max content light level for the HDR title)."""

    # --- HDR — display capability (from !I50) ---
    display_supports_rec2020: bool | None = None
    """Whether the connected display reports Rec.2020 EDID support."""

    # --- Bookkeeping ---
    last_update_codes: tuple[str, ...] = field(default_factory=tuple)
    """Codes touched by the most recent protocol update (for debugging)."""
