"""Lumagen response parser.

Direct port of the C++ ``lumagen_parser`` that used to live in
``esphome-lumagen/components/``. The logic is deliberately mechanical: the
Lumagen protocol is stable and its field layouts are documented, so there's
no value in cleverness here — just a line buffer, a ``!``-scan, a CSV split,
and a small dispatch table.

Key invariants (from the old C++ comments, confirmed against captures):

* The Lumagen may echo the query command on the same line as the response,
  e.g. ``ZQS01!S01,RadiancePro,...``. Always locate the response by scanning
  for ``!``, not by assuming position 0.
* Lines terminate with ``\\r\\n``, but some firmware revisions send only
  ``\\n``. Strip whichever we see.
* Some lines arrive without a final newline before the next line's prefix
  (rare; happens when the Lumagen coalesces unsolicited reports). We don't
  try to handle that — we trust the boundary is a newline.
* Handled response codes: S00, S01, S02, S1A-S1D, I00, I21, I22, I23, I24,
  I25, I30, I50, I52, I53, I54, O01. Unknown codes are logged at DEBUG and
  ignored. ``I01`` (input video format) was handled at one point but is no
  longer: nothing sends ``ZQI01``, so the branch was unreachable and its
  raw payload had no consumer. An unsolicited ``!I01`` now surfaces as an
  unhandled-code DEBUG line, which is more useful than silently stashing a
  string nobody reads.
* **A response prefix does not imply the query is supported.** The device
  answers *any* syntactically valid ``ZQ`` code by echoing the matching
  code with an empty payload — ``ZQI99`` returns ``!I99,`` exactly like
  ``ZQI55`` does. So when probing for undocumented queries, the only
  positive signal is a *non-empty* payload; the presence of a ``!Ixx,``
  line proves nothing. Verified on a Radiance Pro 4242 across both the
  ``ZQI`` and ``ZQS`` namespaces.

Full v5 (``ZQI25`` / ``!I25``) is the recommended unsolicited-reporting
mode for current Lumagen firmware. It extends the v4 layout with trailing
fields:

* index 23 — active input memory letter (``A``/``B``/``C``/``D``)
* index 24 — power state (``0``/``1``)
* index 25 — subtitle shift status (``0`` off, ``1`` 3%, ``2`` 6%)
* index 26 — auto aspect status (``0`` off, ``1`` disabled, ``2`` on)

Indices 23 and 24 were previously only obtainable via separate ``ZQI00`` and
``ZQS02`` queries; v5 pushes them on every state change so power transitions
and memory swaps reach listeners in real time without a follow-up poll.

**On the evidence for 25 and 26.** ``ZQI25`` is absent from
``Tip0011_RS232CommandInterface_111023.pdf`` entirely — Full v5 postdates that
revision, so every index above 22 is undocumented. All four are now confirmed
on hardware (Radiance Pro 4242, firmware 030326):

* 23 and 24 by the captures in ``tests/test_protocol.py``.
* 25 by driving ``ZY5530``/``ZY5531``/``ZY5532`` and reading the field back —
  it tracked ``0``/``1``/``2`` exactly, in both directions.
* 26 by switching auto aspect three different ways — the serial ``V`` command,
  the OSD menu, and subtitle-shift inhibition — each agreeing with ``ZQI54``.

Both reads stay length-guarded, because a firmware that stops the payload
earlier is still supported: the recorded ``030225`` capture ends at index 24, so
there the fields stay ``None`` and nothing changes. Tip0011's own advice for
this layout — "allow for future comma delimited fields being added at the end of
the response" — is why a newer firmware having grown two more fields was the
likeliest explanation, and it held.

Index 26 now also feeds the boolean
:attr:`~aiolumagen.state.LumagenState.auto_aspect`, which is why ``ZQI54`` is no
longer polled unconditionally — see
:meth:`~aiolumagen.client.LumagenClient._query_secondary_status`. The device
pushes ``!I25`` on every auto-aspect change (verified with no query
outstanding), so the push is both sufficient and faster. ``ZQI54`` remains as
the fallback for firmware without index 26.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace

from aiolumagen.formatting import (
    decode_output_mask,
    decode_vertical_rate,
    derive_horizontal_resolution,
)
from aiolumagen.state import (
    AutoAspectStatus,
    Colorspace,
    HdrStatus,
    InputStatus,
    LumagenState,
    SharpnessSensitivity,
    SourceMode,
    SubtitleShift,
)

_LOGGER = logging.getLogger(__name__)

MAX_RX_BUFFER = 4096
"""Maximum in-flight line length before we give up and discard.

The C++ implementation used 512 for the tight ESP32 memory budget; 4096 is
fine in Python and gives us plenty of headroom for pathological bursts.
"""


# I24 field indices. Documented in the old C++ header and in
# Tip0011_RS232CommandInterface. The comment keys map to Lumagen's
# single-letter field names.
_I24_INPUT_STATUS = 0  # M
_I24_SOURCE_VRATE = 1  # RRR
_I24_SOURCE_RESOLUTION = 2  # VVVV
_I24_SOURCE_3D_MODE = 3  # D: 0,1,2,4,8
_I24_INPUT_CONFIG = 4  # X
_I24_SOURCE_ASPECT = 5  # AAA
_I24_CONTENT_ASPECT = 6  # SSS
_I24_NLS_ACTIVE = 7  # Y: 'N'=NLS, '-'=normal
_I24_OUTPUT_3D_MODE = 8  # T: 0,1,2,4,8
_I24_OUTPUT_ENABLED = 9  # WWWW: 16-bit hex mask, b0 = output 1
_I24_OUTPUT_CMS = 10  # C: 0-7
_I24_OUTPUT_STYLE = 11  # B: 0-7
_I24_OUTPUT_VRATE = 12  # PPP
_I24_OUTPUT_RESOLUTION = 13  # QQQQ
_I24_OUTPUT_ASPECT = 14  # ZZZ
_I24_COLORSPACE = 15  # E: 0=601, 1=709, 2=2020, 3=2100
_I24_HDR_FLAG = 16  # F: 0=SDR, 1=HDR
_I24_SOURCE_MODE = 17  # G: i, p, -, n
_I24_OUTPUT_MODE = 18  # H: 'I' or 'P' (uppercase on the wire)
_I24_VIRTUAL_INPUT = 19  # II: 1-19
_I24_PHYSICAL_INPUT = 20  # KK: 1-19
_I24_DETECTED_SOURCE_ASPECT = 21  # JJJ
_I24_DETECTED_CONTENT_ASPECT = 22  # LLL

# I25-only fields appended to the v4 layout (see module docstring).
_I25_INPUT_MEMORY = 23  # A/B/C/D
_I25_POWER_STATE = 24  # 0=off, 1=on
# Indices 25/26 are empirical and NOT in Tip0011 — see the module docstring's
# Full v5 section. Both are read behind a length guard, so a firmware that
# stops at power (like the 030225 capture in tests/test_protocol.py) simply
# leaves them unset.
_I25_SUBTITLE_SHIFT = 25  # 0=off, 1=3%, 2=6%
_I25_AUTO_ASPECT = 26  # 0=off, 1=disabled, 2=on

_INPUT_STATUS_MAP = {
    "0": InputStatus.NO_SOURCE,
    "1": InputStatus.ACTIVE,
    "2": InputStatus.TEST_PATTERN,
}

_COLORSPACE_MAP = {
    "0": Colorspace.REC_601,
    "1": Colorspace.REC_709,
    "2": Colorspace.REC_2020,
    "3": Colorspace.REC_2100,
}

_SUBTITLE_SHIFT_MAP = {
    "0": SubtitleShift.OFF,
    "1": SubtitleShift.PERCENT_3,
    "2": SubtitleShift.PERCENT_6,
}

_AUTO_ASPECT_MAP = {
    "0": AutoAspectStatus.OFF,
    "1": AutoAspectStatus.DISABLED,
    "2": AutoAspectStatus.ON,
}


def _as_int(value: str, label: str) -> int | None:
    """Parse a small integer status field, or ``None`` if it's unreadable.

    Callers assign only on a non-``None`` result, so a malformed field leaves
    the previously-observed value in place rather than blanking it — a garbled
    line shouldn't erase good state.
    """
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        _LOGGER.debug("Could not parse %s field %r", label, value)
        return None


# Type alias for the per-update callback handed to LumagenProtocol.
StateUpdateCallback = Callable[[LumagenState, tuple[str, ...]], None]
"""Callback shape: ``(new_state, codes_touched) -> None``.

``codes_touched`` lists the 3-char response codes that contributed to this
update — e.g. ``("I24",)`` for a full-status report, ``("S02",)`` for a
power-only change.
"""

ResponseObserver = Callable[[str, str], None]
"""Callback shape: ``(code, payload) -> None``.

Fired once for **every** response line the parser reads, which makes it
strictly broader than :data:`StateUpdateCallback` in two ways that matter for
request/response correlation:

* It fires even when the response changed nothing, where the state-update
  callback is suppressed by the no-op dedupe.
* It fires for codes the parser has no handler for, so a caller can await an
  undocumented response.

``payload`` is the text after the code (and its optional comma), exactly as
the per-code handlers see it — empty string for a payload-less response. Since
the Lumagen answers any syntactically valid ``ZQ`` code with an empty payload,
an observer being called proves the line arrived, *not* that the query is
supported; check for a non-empty payload for that.

Observers run synchronously from the transport's data callback, after the new
state has been committed, so reading :attr:`LumagenProtocol.state` from inside
one sees the response's effect.
"""


class LumagenProtocol:
    """Streaming parser for Lumagen responses.

    Feed bytes in with :meth:`feed_bytes`. Complete lines trigger a state
    update and the supplied callback is invoked with the new state and the
    set of codes that were touched. The protocol is fully synchronous — it
    does no I/O — so it's easy to test in isolation.
    """

    def __init__(self, on_update: StateUpdateCallback) -> None:
        self._on_update = on_update
        self._state = LumagenState()
        self._rx = bytearray()
        self._response_observers: list[ResponseObserver] = []
        # Correlation context for input-label queries. The Lumagen's label
        # response (!S1x,<label>) reports only the memory letter, never the
        # input number, so the client primes this with the input it's about
        # to ask for; the next !S1x response is attributed to it. Only valid
        # while a serialized query is in flight — labels are query-only, so
        # no unsolicited !S1x can race it.
        self._pending_label_input: int | None = None

    @property
    def state(self) -> LumagenState:
        """Current accumulated state snapshot."""
        return self._state

    @property
    def pending_label_input(self) -> int | None:
        """Input number a subsequent ``!S1x`` label response will be assigned to.

        ``None`` once the pending response has been consumed.

        Read-only observability, not a coordination channel. The client used to
        poll this to tell whether a label query had been answered; it now
        awaits the response itself (see
        :meth:`~aiolumagen.client.LumagenClient.query_and_wait`), so the only
        remaining consumers are tests asserting the primer is set and cleared
        at the right moments — which is worth keeping, because a stuck primer
        is how a label gets filed under the wrong input.
        """
        return self._pending_label_input

    def expect_input_label(self, input_number: int | None) -> None:
        """Prime the parser to attribute the next ``!S1x`` response to ``input_number``.

        Called by the client immediately before it sends a ``ZQS1XY`` label
        query. Pass ``None`` to clear the primer — the client does this when a
        label query times out, so a late reply can't be misattributed to
        whichever input is asked about next. See :attr:`pending_label_input`.
        """
        self._pending_label_input = input_number

    def add_response_observer(self, observer: ResponseObserver) -> Callable[[], None]:
        """Register a per-response callback; returns an unregister callable.

        See :data:`ResponseObserver` for the contract. This exists so
        :class:`~aiolumagen.client.LumagenClient` can correlate a query with
        its reply without the protocol layer growing any notion of a waiter —
        it stays synchronous and I/O-free, and the async bookkeeping lives in
        the client where it belongs.
        """
        self._response_observers.append(observer)

        def _unregister() -> None:
            with suppress(ValueError):
                self._response_observers.remove(observer)

        return _unregister

    def reset(self) -> None:
        """Discard any partial line and pending label context. Call on reconnect.

        Registered response observers survive: the client wires its correlation
        observer once at construction and expects it to outlive a reconnect.
        """
        self._rx.clear()
        self._pending_label_input = None

    def feed_bytes(self, data: bytes | bytearray) -> None:
        """Append inbound bytes and emit an update for each complete line.

        A ``line`` ends at the first ``\\n`` we see. Trailing ``\\r`` is
        stripped. Empty lines are ignored. If the buffer exceeds
        :data:`MAX_RX_BUFFER` without a newline, we discard and warn — this
        is recovery from a missing terminator, not normal operation.
        """
        if not data:
            return
        self._rx.extend(data)
        while True:
            nl = self._rx.find(b"\n")
            if nl < 0:
                if len(self._rx) > MAX_RX_BUFFER:
                    _LOGGER.warning(
                        "RX buffer exceeded %d bytes without a newline; discarding", MAX_RX_BUFFER
                    )
                    self._rx.clear()
                return
            raw = bytes(self._rx[:nl])
            del self._rx[: nl + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            try:
                line = raw.decode("ascii", errors="replace")
            except Exception:  # pragma: no cover — decode("replace") can't raise
                continue
            self._process_line(line)

    # ------------------------------------------------------------------
    # Line / response handling
    # ------------------------------------------------------------------

    def _process_line(self, line: str) -> None:
        _LOGGER.debug("RX: %s", line)
        bang = line.find("!")
        if bang < 0:
            # No response marker — probably an echo of our own command.
            return
        tail = line[bang + 1 :]
        if len(tail) < 3:
            return
        code = tail[:3]
        # Most responses delimit the payload with a comma (``!S02,1``), but
        # not all do — a response that packs its payload straight onto the
        # code (``!I30Y4N``) would otherwise be read as having no payload at
        # all and silently dropped. Falling back to the remainder is strictly
        # better than discarding it: every handler validates its own payload,
        # and for genuinely payload-less responses (``!S00``) the slice is
        # empty anyway.
        data = tail[4:] if len(tail) > 3 and tail[3] == "," else tail[3:]
        self._handle_response(code, data)

    def _handle_response(self, code: str, data: str) -> None:
        # Build a shallow copy of the current state, mutate fields on it,
        # and swap it in at the end. This avoids ``replace(**kwargs)`` with
        # a loosely-typed dict (mypy rightly rejects that) while still
        # firing exactly one callback per inbound line.
        pending = replace(self._state)
        touched: tuple[str, ...] = (code,)
        # Whether the response contributed to state. A False here means we
        # skip the state commit but STILL notify response observers below:
        # correlation cares that the line arrived, not that it changed
        # anything.
        applied = True

        if code == "S00":
            pending.alive = True
        elif code == "S01":
            self._handle_s01(data, pending)
        elif code == "S02":
            pending.power_on = data[:1] == "1"
        elif code in ("S1A", "S1B", "S1C", "S1D"):
            # Input label response. ``data`` is the whole label (commas and
            # spaces preserved). The response carries only the memory letter,
            # so we rely on the client-primed pending input to know which
            # logical input it belongs to.
            if self._pending_label_input is None:
                _LOGGER.debug("Input label response %r with no pending input; ignoring", data)
                applied = False
            else:
                n = self._pending_label_input
                self._pending_label_input = None
                # Build a fresh dict so the previous state's map is untouched
                # — the equality diff below depends on old and new not
                # aliasing.
                pending.input_labels = {**self._state.input_labels, n: data}
        elif code == "I00":
            self._handle_i00(data, pending)
        elif code == "I24":
            pending.full_status_raw = data
            self._apply_i24(data, pending)
        elif code == "I25":
            pending.full_status_raw = data
            self._apply_i25(data, pending)
        elif code in ("I21", "I22", "I23"):
            pending.full_status_raw = data
            self._apply_i2x(data, pending)
        elif code == "I30":
            self._handle_i30(data, pending)
        elif code == "I50":
            pending.display_supports_rec2020 = data[:1] == "Y"
        elif code == "I52":
            self._handle_i52(data, pending)
        elif code == "I53":
            pending.game_mode = data[:1] == "1"
        elif code == "I54":
            pending.auto_aspect = data[:1] == "1"
        elif code == "O01":
            self._handle_o01(data, pending)
        else:
            _LOGGER.debug("Unhandled Lumagen code !%s,%s", code, data)
            applied = False

        if applied:
            pending.last_update_codes = touched
            # Compare ignoring last_update_codes so that "no payload change"
            # polls don't wake up listeners.
            changed = not self._state_matches_ignoring_codes(pending)
            self._state = pending
            if changed:
                self._on_update(self._state, touched)

        # Observers run last, and unconditionally, so a client awaiting this
        # code resumes only after state and state-listeners have settled.
        self._notify_response(code, data)

    def _notify_response(self, code: str, data: str) -> None:
        """Fire every registered response observer for one response line."""
        for observer in list(self._response_observers):
            try:
                observer(code, data)
            except Exception:
                _LOGGER.exception("Response observer raised; dropping it")
                with suppress(ValueError):
                    self._response_observers.remove(observer)

    def _state_matches_ignoring_codes(self, pending: LumagenState) -> bool:
        """Return True if ``pending`` equals current state but for the codes tuple."""
        cur = self._state
        if cur is pending:
            return True
        current_codes = cur.last_update_codes
        pending_codes = pending.last_update_codes
        cur.last_update_codes = pending_codes
        try:
            return cur == pending
        finally:
            cur.last_update_codes = current_codes

    # ------------------------------------------------------------------
    # Per-code handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_s01(data: str, state: LumagenState) -> None:
        """!S01 = ``<model name>,<software rev>,<model number>,<serial>``.

        Four fields, per Tip0011's ``ZQS01`` entry (its own example is
        ``!S01,RadianceXD,102308,1009,745``, noting XD's model number as
        1009 and XE's as 1010). A Radiance Pro answers e.g.
        ``!S01,RadiancePro,030225,1018,000000`` — software revision is an
        ``MMDDYY`` date code, and the serial may legitimately read as all
        zeros.

        This previously split with ``maxsplit=2`` against a docstring that
        claimed three fields, which silently glued the model number and
        serial into one discarded string. Fields beyond the fourth (if a
        future firmware adds any) are ignored here but remain visible in
        :attr:`~aiolumagen.state.LumagenState.device_info_raw`.
        """
        state.device_info_raw = data
        parts = data.split(",")
        if len(parts) >= 1:
            state.model = parts[0]
        if len(parts) >= 2:
            state.firmware = parts[1]
        if len(parts) >= 3:
            state.model_number = parts[2]
        if len(parts) >= 4:
            state.serial = parts[3]

    @staticmethod
    def _handle_i00(data: str, state: LumagenState) -> None:
        """!I00 = ``<input>,<memory>,<config>``."""
        state.input_info_raw = data
        parts = data.split(",", 2)
        if len(parts) >= 1:
            state.current_input = parts[0]
        if len(parts) >= 2:
            state.input_memory = parts[1]

    @staticmethod
    def _handle_i30(data: str, state: LumagenState) -> None:
        """!I30 = sharpness setting, format ``<E><L><S>`` matching ZY521ELS.

        E = ``Y``/``N`` (enabled), L = ``0``-``7`` (level), S = ``H``/``N``
        (sensitivity). The Lumagen doc (Tip0011, ``ZQI30``) describes the
        response only as "Returns values corresponding to the YZ521ELS
        command" — no published example payload, so this implementation
        is best-effort. Always stash the raw payload so a diagnostic dump
        can show the wire bytes if the structured fields look wrong.
        """
        state.sharpness_raw = data
        # Tolerate every plausible framing of the same three values. Tip0011
        # documents the response only as "returns values corresponding to
        # the ZY521ELS command" with no example payload, and the observed
        # shape varies:
        #   !I30Y4N     - packed straight onto the code, no delimiter
        #   !I30,Y4N    - comma-delimited, values packed
        #   !I30,Y,4,N  - one comma-separated field per value
        # Compacting out commas and whitespace reduces all three to "Y4N",
        # so the positional reads below work regardless of framing.
        compact = data.replace(",", "").replace(" ", "").strip()
        if len(compact) >= 1 and compact[0] in ("Y", "N"):
            state.sharpness_enabled = compact[0] == "Y"
        if len(compact) >= 2 and compact[1].isdigit():
            level = int(compact[1])
            if 0 <= level <= 7:
                state.sharpness_level = level
        if len(compact) >= 3 and compact[2] in ("H", "N"):
            state.sharpness_sensitivity = SharpnessSensitivity(compact[2])
        if state.sharpness_enabled is None and state.sharpness_level is None:
            # Nothing recognisable — surface the raw bytes so an unexpected
            # firmware framing is diagnosable instead of silently unknown.
            _LOGGER.debug("Could not parse !I30 sharpness payload %r", data)

    @staticmethod
    def _handle_i52(data: str, state: LumagenState) -> None:
        """!I52 = source HDR status: ``V,Min,Max,Cll``.

        Per Tip0011:

        * ``V`` = 0 (source not HDR) or 1 (source is HDR)
        * ``Min`` = source mastering display minimum luminance, decimal
          (e.g. ``.0050``).
        * ``Max`` = source mastering display max luminance, integer nits.
        * ``Cll`` = MaxCLL, integer nits.

        For SDR sources the device fills Min/Max/Cll with zero
        placeholders (``0,.0000,0,0``). We only populate the structured
        fields when V=1; SDR keeps them at None so an entity reads
        "unknown" rather than a misleading "0 nits".
        """
        parts = data.split(",")
        if not parts:
            return
        is_hdr = parts[0] == "1"
        # Note: !I52 is parallel to !I24's HDR flag; trust !I24 as primary
        # but also reflect this query's view in is_hdr/hdr_status so a
        # standalone ZQI52 still updates the sensors.
        state.is_hdr = is_hdr
        state.hdr_status = HdrStatus.HDR if is_hdr else HdrStatus.SDR
        if not is_hdr:
            # Clear any stale mastering metadata when the source flips to SDR.
            state.hdr_source_min_luminance = None
            state.hdr_source_max_luminance = None
            state.hdr_source_max_cll = None
            return

        # HDR source — pull mastering metadata.
        if len(parts) >= 2:
            try:
                state.hdr_source_min_luminance = float(parts[1])
            except TypeError, ValueError:
                _LOGGER.debug("Could not parse !I52 Min field %r", parts[1])
        if len(parts) >= 3:
            try:
                state.hdr_source_max_luminance = int(parts[2])
            except TypeError, ValueError:
                _LOGGER.debug("Could not parse !I52 Max field %r", parts[2])
        if len(parts) >= 4:
            try:
                state.hdr_source_max_cll = int(parts[3])
            except TypeError, ValueError:
                _LOGGER.debug("Could not parse !I52 Cll field %r", parts[3])

    @staticmethod
    def _apply_i24(data: str, state: LumagenState) -> None:
        """!I24 = Full v4 status, 23 comma-separated fields."""
        fields = data.split(",")
        LumagenProtocol._apply_i2x_common(fields, state)

        # I24-only fields (colorspace, HDR, source mode, virtual input)
        if len(fields) > _I24_COLORSPACE:
            cs = _COLORSPACE_MAP.get(fields[_I24_COLORSPACE])
            if cs is not None:
                state.colorspace = cs
        if len(fields) > _I24_HDR_FLAG:
            is_hdr = fields[_I24_HDR_FLAG] == "1"
            state.is_hdr = is_hdr
            state.hdr_status = HdrStatus.HDR if is_hdr else HdrStatus.SDR
        if len(fields) > _I24_SOURCE_MODE:
            raw = fields[_I24_SOURCE_MODE]
            try:
                state.source_mode = SourceMode(raw)
            except ValueError:
                _LOGGER.debug("Unknown source mode %r", raw)
        if len(fields) > _I24_VIRTUAL_INPUT:
            state.current_input = fields[_I24_VIRTUAL_INPUT]

        LumagenProtocol._apply_i24_extended(fields, state)
        LumagenProtocol._apply_derived(state)

    @staticmethod
    def _apply_i24_extended(fields: list[str], state: LumagenState) -> None:
        """Documented ``!I24`` fields that used to be parsed and discarded.

        Restricted to the I24/I25 path on purpose. I22 and I23 share these
        positions per Tip0011, but I21's documented signature
        (``!I21,M,RRR,VVVV,D,X,AAA,SSS,Y,C,B,PPP,QQQQ,ZZZ``) omits the ``T``
        and ``WWWW`` fields that its own field list immediately below it
        *does* define — so for I21 the doc contradicts itself about whether
        everything from index 8 on is shifted by two. Rather than guess, the
        shorter formats stay on :meth:`_apply_i2x_common`'s minimal set.

        Every read is length-guarded and every conversion failure is
        swallowed to a debug line: a truncated or malformed status line must
        degrade to "field unknown", never abort the rest of the parse.
        """
        if len(fields) > _I24_SOURCE_3D_MODE:
            state.source_3d_mode = fields[_I24_SOURCE_3D_MODE]
        if len(fields) > _I24_INPUT_CONFIG:
            state.input_config = fields[_I24_INPUT_CONFIG]
        if len(fields) > _I24_NLS_ACTIVE:
            # 'N' = NLS engaged, '-' = normal. Anything else is unexpected;
            # leave the prior value rather than inventing a boolean.
            nls = fields[_I24_NLS_ACTIVE]
            if nls in ("N", "-"):
                state.nls_active = nls == "N"
        if len(fields) > _I24_OUTPUT_3D_MODE:
            state.output_3d_mode = fields[_I24_OUTPUT_3D_MODE]
        if len(fields) > _I24_OUTPUT_ENABLED:
            raw_mask = fields[_I24_OUTPUT_ENABLED].strip()
            try:
                mask = int(raw_mask, 16)
            except ValueError:
                _LOGGER.debug("Could not parse output mask %r", raw_mask)
            else:
                state.output_enabled_mask = mask
                state.active_outputs = decode_output_mask(mask)
        if len(fields) > _I24_OUTPUT_CMS:
            cms = _as_int(fields[_I24_OUTPUT_CMS], "output CMS")
            if cms is not None:
                state.output_cms = cms
        if len(fields) > _I24_OUTPUT_STYLE:
            style = _as_int(fields[_I24_OUTPUT_STYLE], "output style")
            if style is not None:
                state.output_style = style
        if len(fields) > _I24_OUTPUT_ASPECT:
            state.output_aspect = fields[_I24_OUTPUT_ASPECT]
        if len(fields) > _I24_OUTPUT_MODE:
            # The device sends output mode uppercase ('I'/'P') where source
            # mode is lowercase. Lowercasing lets both share SourceMode
            # instead of introducing a second enum for the same concept.
            raw_mode = fields[_I24_OUTPUT_MODE].lower()
            try:
                state.output_scan_mode = SourceMode(raw_mode)
            except ValueError:
                _LOGGER.debug("Unknown output scan mode %r", raw_mode)
        if len(fields) > _I24_PHYSICAL_INPUT:
            state.physical_input = fields[_I24_PHYSICAL_INPUT]
        if len(fields) > _I24_DETECTED_SOURCE_ASPECT:
            state.detected_source_aspect = fields[_I24_DETECTED_SOURCE_ASPECT]
        if len(fields) > _I24_DETECTED_CONTENT_ASPECT:
            state.detected_content_aspect = fields[_I24_DETECTED_CONTENT_ASPECT]

    @staticmethod
    def _handle_o01(data: str, state: LumagenState) -> None:
        """!O01 = output mode. The only source of true output width.

        Tip0011 (``ZQO01``) documents the payload as ``vertical rate * 100,
        horizontal res, vertical res, interlaced, 3D mode``, with the example
        ``!O01,5994,1920,1080,0,0``.

        Field 1 matters because it is the **only** place the Lumagen states its
        output width outright. Everywhere else width has to be inferred from
        height and an aspect code, and for the output that inference is
        unsound — see
        :attr:`~aiolumagen.state.LumagenState.output_width` for why an
        anamorphic setup turns 4096 into 5119.

        Deliberately does not touch the rate or 3D fields. ``output_vrate`` /
        ``output_refresh_hz`` already come off the ``!I25`` push in a different
        encoding (``059`` vs ``5994``), and two writers for one value invites
        exactly the precedence bug this method exists to fix. The raw payload is
        always stashed so a diagnostic dump can show the wire bytes.
        """
        state.output_mode_raw = data
        fields = data.split(",")

        def numeric(index: int) -> int | None:
            if len(fields) <= index:
                return None
            try:
                value = int(fields[index].strip())
            except ValueError:
                return None
            # 0 is the device's no-signal placeholder, not a real geometry.
            return value if value > 0 else None

        state.output_width_reported = numeric(1)
        state.output_height_reported = numeric(2)
        LumagenProtocol._apply_derived(state)

    @staticmethod
    def _apply_derived(state: LumagenState) -> None:
        """Decode the raw code fields into usable numbers.

        Pure function of fields already parsed off the same line — no extra
        query, no I/O. Runs at the end of every status-line path so the
        derived values can never disagree with the raw ones they came from.
        See :mod:`aiolumagen.formatting` for the decoders.
        """
        if state.source_vrate is not None:
            state.source_refresh_hz = decode_vertical_rate(state.source_vrate)
        if state.output_vrate is not None:
            state.output_refresh_hz = decode_vertical_rate(state.output_vrate)
        if state.source_resolution is not None and state.source_aspect is not None:
            state.source_width = derive_horizontal_resolution(
                state.source_resolution, state.source_aspect
            )
        # Output width: prefer what the device reported over what we can infer.
        # The derivation multiplies height by the *output aspect*, which is only
        # the raster aspect when the output is unscaled. Feeding an anamorphic
        # lens breaks that assumption — a 4096x2160 raster reports aspect 2.37,
        # and 2160 x 2.37 = 5119. !O01 carries the real width, so once it has
        # been seen it wins; the derivation stays as the fallback for a device
        # that has not answered ZQO01 yet.
        if state.output_width_reported is not None:
            state.output_width = state.output_width_reported
        elif state.output_resolution is not None and state.output_aspect is not None:
            state.output_width = derive_horizontal_resolution(
                state.output_resolution, state.output_aspect
            )

    @staticmethod
    def _apply_i25(data: str, state: LumagenState) -> None:
        """!I25 = Full v5 status, v4 layout + 2 trailing fields.

        Reuses :meth:`_apply_i24` for the 23 v4-shared fields, then pulls
        the v5 additions: active input memory letter (idx 23) and power
        state (idx 24). With v5 enabled on the device, power transitions
        and memory swaps reach listeners as part of the unsolicited push
        stream, eliminating the need for a follow-up ZQS02/ZQI00 poll
        after every control command.
        """
        LumagenProtocol._apply_i24(data, state)

        fields = data.split(",")
        if len(fields) > _I25_INPUT_MEMORY:
            mem = fields[_I25_INPUT_MEMORY]
            if mem:  # Treat empty string the same as "not reported"
                state.input_memory = mem
        if len(fields) > _I25_POWER_STATE:
            state.power_on = fields[_I25_POWER_STATE] == "1"
        # Indices 25/26 are empirical (see the module docstring). A firmware
        # that stops at power leaves both unset rather than guessing.
        if len(fields) > _I25_SUBTITLE_SHIFT:
            shift = _SUBTITLE_SHIFT_MAP.get(fields[_I25_SUBTITLE_SHIFT])
            if shift is not None:
                state.subtitle_shift = shift
        if len(fields) > _I25_AUTO_ASPECT:
            auto = _AUTO_ASPECT_MAP.get(fields[_I25_AUTO_ASPECT])
            if auto is not None:
                state.auto_aspect_status = auto
                # Also feed the boolean. This index was previously kept away
                # from it in case the mapping was wrong; it has since been
                # confirmed on hardware (4242 / firmware 030326) against three
                # independent ways of switching auto aspect off — the serial
                # 'V' command, the OSD menu, and subtitle-shift inhibition —
                # all reporting DISABLED here and 0 from ZQI54.
                #
                # The gain is latency, not information: this rides the push, so
                # a change lands immediately instead of waiting for the next
                # ZQI54 poll. The device pushes !I25 on every auto-aspect
                # change, verified by listening with no query outstanding.
                state.auto_aspect = auto is AutoAspectStatus.ON

    @staticmethod
    def _apply_i2x(data: str, state: LumagenState) -> None:
        """!I21/I22/I23 = older unsolicited formats (leading fields only)."""
        LumagenProtocol._apply_i2x_common(data.split(","), state)
        LumagenProtocol._apply_derived(state)

    @staticmethod
    def _apply_i2x_common(fields: list[str], state: LumagenState) -> None:
        """Fields shared by I21/I22/I23/I24 - input status through output res."""
        if len(fields) > _I24_INPUT_STATUS:
            status = _INPUT_STATUS_MAP.get(fields[_I24_INPUT_STATUS])
            if status is not None:
                state.input_status = status
        if len(fields) > _I24_SOURCE_VRATE:
            state.source_vrate = fields[_I24_SOURCE_VRATE]
        if len(fields) > _I24_SOURCE_RESOLUTION:
            state.source_resolution = fields[_I24_SOURCE_RESOLUTION]
        if len(fields) > _I24_SOURCE_ASPECT:
            state.source_aspect = fields[_I24_SOURCE_ASPECT]
        if len(fields) > _I24_CONTENT_ASPECT:
            state.content_aspect = fields[_I24_CONTENT_ASPECT]
        if len(fields) > _I24_OUTPUT_VRATE:
            state.output_vrate = fields[_I24_OUTPUT_VRATE]
        if len(fields) > _I24_OUTPUT_RESOLUTION:
            state.output_resolution = fields[_I24_OUTPUT_RESOLUTION]
