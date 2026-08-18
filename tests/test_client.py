"""LumagenClient integration tests against a FakeTransport."""

from __future__ import annotations

import asyncio

import pytest

from aiolumagen.client import LumagenClient
from aiolumagen.exceptions import (
    LumagenCommandError,
    LumagenConnectionError,
    LumagenError,
)
from aiolumagen.state import LumagenState
from tests.conftest import FakeTransport


@pytest.fixture
async def client(fake_transport: FakeTransport):
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    yield c
    await c.stop()


async def test_start_connects_and_sends_startup_sequence(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    # Startup sequence: ZE2, then ZQS01, ZQS02, ZQI00, ZQI25.
    sent = b"".join(fake_transport.sent)
    assert b"ZE2" in sent
    assert b"ZQS01" in sent
    assert b"ZQS02" in sent
    assert b"ZQI00" in sent
    assert b"ZQI25" in sent


async def test_startup_queries_non_pushed_secondary_state(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """Startup must pull the state the !I25 push doesn't carry.

    Regression: sharpness (ZQI30), game mode (ZQI53), auto aspect (ZQI54),
    display Rec.2020 (ZQI50) and source HDR (ZQI52) were never queried, so
    their entities sat at "unknown" forever and set_sharpness couldn't
    preserve enabled/level.

    ZQO01 joined the list later for the same class of reason: the !O01 parser
    existed but nothing ever sent the query, so the only authoritative output
    width never arrived and output_width fell back to an aspect-derived value
    that is wrong on a scaled output.
    """
    await client.start()
    sent = b"".join(fake_transport.sent)
    for query in (b"ZQI30", b"ZQI53", b"ZQI54", b"ZQI50", b"ZQI52", b"ZQO01"):
        assert query in sent, f"{query!r} not issued at startup: {fake_transport.sent}"


async def test_status_poll_queries_secondary_state_when_powered_on(
    fake_transport: FakeTransport,
) -> None:
    """Powered-on status polls refresh the non-pushed fields too."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=0.05,
        stale_timeout=10.0,
    )
    await c.start()
    fake_transport.feed(b"!S02,1\r\n")  # power on -> status branch active
    fake_transport.sent.clear()
    await asyncio.sleep(0.12)  # a couple of status intervals
    await c.stop()
    sent = b"".join(fake_transport.sent)
    assert b"ZQI25" in sent  # primary status still polled
    assert b"ZQI30" in sent  # sharpness refreshed alongside it


async def test_inbound_bytes_update_state(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.feed(b"!S02,1\r\n!S01,RadiancePro,030225,1018,000000\r\n")
    # The protocol is sync; state is updated inline with feed_bytes.
    assert client.state.power_on is True
    assert client.state.model == "RadiancePro"


async def test_subscribe_receives_updates(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    received: list[tuple[LumagenState, tuple[str, ...]]] = []
    client.subscribe(lambda state, codes: received.append((state, codes)))
    await client.start()
    fake_transport.feed(b"!S02,1\r\n")
    assert received
    assert received[-1][0].power_on is True


async def test_unsubscribe_stops_updates(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    received: list[LumagenState] = []
    unsubscribe = client.subscribe(lambda state, _codes: received.append(state))
    await client.start()
    # Clear any startup-handshake updates (e.g. auto-fed !S01)
    received.clear()
    fake_transport.feed(b"!S02,1\r\n")
    unsubscribe()
    fake_transport.feed(b"!S02,0\r\n")
    # Only the first update was seen by the listener.
    assert [s.power_on for s in received] == [True]
    # But the client's own state did update.
    assert client.state.power_on is False


async def test_commands_send_expected_bytes(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    await client.power_on()
    await client.standby()
    await client.set_input(3)
    assert fake_transport.sent == [b"%", b"$", b"i3"]


async def test_set_input_above_nine_uses_the_plus_encoding(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """set_input must route through the encoder, not format i{n} itself.

    Tip0011's own example is "i+2 for input 12". Previously this wrote b"i12",
    which selects input 1 and then feeds a stray digit — silently the wrong
    input, with no error anywhere. See test_commands.py for the encoder tests.
    """
    await client.start()
    fake_transport.sent.clear()
    await client.set_input(10)
    await client.set_input(12)
    await client.set_input(19)
    assert fake_transport.sent == [b"i+0", b"i+2", b"i+9"]


async def test_send_command_before_start_raises(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    with pytest.raises(LumagenError):
        await c.send_command("%")


async def test_poll_loop_fires_power_query(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=0.05,
        status_poll_interval=None,
    )
    await c.start()
    fake_transport.sent.clear()
    await asyncio.sleep(0.12)  # two poll intervals
    await c.stop()
    assert any(b"ZQS02" in chunk for chunk in fake_transport.sent)


async def test_poll_loop_skips_status_when_powered_off(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=0.05,
    )
    await c.start()
    # power_on is None (unknown) → ZQI25 is gated off
    fake_transport.sent.clear()
    await asyncio.sleep(0.12)
    await c.stop()
    assert all(b"ZQI25" not in chunk for chunk in fake_transport.sent)


async def test_liveness_tracks_bytes_not_state_changes(
    fake_transport: FakeTransport,
) -> None:
    """Steady-state polls (bytes arrive but state doesn't change) keep the
    client available. Regression for the bug where liveness was tied to
    state-change callbacks, which the parser suppresses when the Lumagen's
    values are unchanged — leading to false-positive stale detection on
    an idle but responsive device.

    Uses a long stale_timeout so the test isn't sensitive to the
    startup-sequence's internal asyncio.sleep delays.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
        stale_timeout=10.0,
    )
    await c.start()
    # The FakeTransport auto-feeds !S01 on the startup ZQS01, so the
    # client is available after start().
    assert c.available is True
    first_response_time = c._last_response_time
    assert first_response_time is not None

    # Feed an identical !S01 — same model/firmware as before. The parser
    # will NOT fire a state-change callback because nothing changed, but
    # the byte stream should still refresh liveness.
    fake_transport.feed(b"!S01,FakeModel,000000,0000,000000\r\n")
    assert c._last_response_time is not None
    assert c._last_response_time > first_response_time
    assert c.available is True

    await c.stop()


async def test_available_goes_false_on_true_silence(
    fake_transport: FakeTransport,
) -> None:
    """When no bytes arrive at all for stale_timeout, available flips to False.

    Sets stale_timeout slightly longer than the startup sequence (~1.2s of
    asyncio.sleep) so it's reached *after* startup completes.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
        stale_timeout=2.0,
    )
    await c.start()
    assert c.available is True

    # No feed, just wait past the stale_timeout.
    await asyncio.sleep(2.5)
    assert c.available is False

    await c.stop()


async def test_init_rejects_stale_timeout_below_poll_interval(
    fake_transport: FakeTransport,
) -> None:
    """The constructor enforces stale_timeout > longest poll interval.

    Regression for the 0.1.0 bug: stale_timeout=45s with 60s polls fired
    "no response in 45s" warnings every poll cycle even though responses
    were arriving. Root cause: the poll loop checks staleness immediately
    after sending each query, before the device's response can arrive —
    so a too-tight timeout makes elapsed-since-last-response always
    exceed it. Enforced at construction time so this can't regress.
    """
    with pytest.raises(ValueError, match="stale_timeout"):
        LumagenClient(
            fake_transport,
            power_poll_interval=60.0,
            status_poll_interval=60.0,
            stale_timeout=45.0,  # the regressing default from 0.1.0
        )


async def test_init_allows_stale_timeout_above_poll_interval(
    fake_transport: FakeTransport,
) -> None:
    """Sanity check: a sensible config is accepted."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=60.0,
        status_poll_interval=60.0,
        stale_timeout=90.0,
    )
    assert c is not None


async def test_init_allows_any_stale_timeout_when_polling_disabled(
    fake_transport: FakeTransport,
) -> None:
    """Without polling, the invariant doesn't apply (no cycle to outrun)."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
        stale_timeout=2.0,
    )
    assert c is not None


async def test_send_command_schedules_refresh_after_control_command(
    fake_transport: FakeTransport,
) -> None:
    """A non-query command should trigger follow-up status queries when
    REFRESH_TICKS is non-empty.

    With Full v5 enabled REFRESH_TICKS defaults to ``()`` (the device
    pushes everything in real time), but the mechanism is still
    available for older firmwares — this test pins down its
    behavior with an explicit override.
    """
    c = LumagenClient(
        fake_transport,
        # Long poll interval so the regular loop doesn't muddy the test.
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    # Tighten the refresh tick so the test runs fast.
    c.REFRESH_TICKS = (0.1,)
    await c.start()
    fake_transport.sent.clear()

    # Send a control command (power on).
    await c.power_on()

    # Wait past the tick.
    await asyncio.sleep(0.2)
    await c.stop()

    # We should see ZQS02 + ZQI25 fired by the refresh tick.
    zqs02_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQS02")
    zqi25_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQI25")
    assert zqs02_count == 1, (
        f"Expected 1 ZQS02 from refresh tick, got {zqs02_count}: {fake_transport.sent}"
    )
    assert zqi25_count == 1, (
        f"Expected 1 ZQI25 from refresh tick, got {zqi25_count}: {fake_transport.sent}"
    )


async def test_send_command_does_not_refresh_for_query_commands(
    fake_transport: FakeTransport,
) -> None:
    """Query commands (Z-prefixed) must not trigger their own refresh — that
    would recurse into an unbounded query storm."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    c.REFRESH_TICKS = (0.05, 0.15)
    await c.start()
    fake_transport.sent.clear()

    # Sending a query directly should NOT schedule additional follow-ups.
    await c.query_power()

    await asyncio.sleep(0.25)
    await c.stop()

    # Only the explicit query should have been sent — no refresh ticks fired.
    # (We allow exactly 1 ZQS02 from the explicit call.)
    zqs02_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQS02")
    assert zqs02_count == 1, (
        f"Query command should not auto-refresh, got {zqs02_count} ZQS02s: {fake_transport.sent}"
    )


async def test_refresh_coalesces_overlapping_calls(
    fake_transport: FakeTransport,
) -> None:
    """Bursts of commands should result in one refresh window, not N stacked."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    c.REFRESH_TICKS = (0.1, 0.3)
    await c.start()
    fake_transport.sent.clear()

    # Three commands in quick succession (e.g. user rapidly pressing buttons).
    await c.power_on()
    await c.standby()
    await c.send_command("k")  # arbitrary OSD command

    await asyncio.sleep(0.4)  # past second tick
    await c.stop()

    # Without coalescing we'd see 3 schedules x 2 ticks = 6 of each.
    # With coalescing we see exactly the most recent schedule's ticks: 2 each.
    zqs02_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQS02")
    zqi25_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQI25")
    assert zqs02_count == 2, (
        f"Expected coalesced refresh = 2 ZQS02s, got {zqs02_count}: {fake_transport.sent}"
    )
    assert zqi25_count == 2, (
        f"Expected coalesced refresh = 2 ZQI25s, got {zqi25_count}: {fake_transport.sent}"
    )


async def test_send_command_refresh_kwarg_can_disable(
    fake_transport: FakeTransport,
) -> None:
    """Callers that want fire-and-forget can opt out via refresh=False."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    c.REFRESH_TICKS = (0.05, 0.15)
    await c.start()
    fake_transport.sent.clear()

    await c.send_command("%", refresh=False)

    await asyncio.sleep(0.25)
    await c.stop()

    zqs02_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQS02")
    zqi25_count = sum(1 for chunk in fake_transport.sent if chunk == b"ZQI25")
    assert zqs02_count == 0, f"refresh=False should suppress ticks, got {zqs02_count}"
    assert zqi25_count == 0, f"refresh=False should suppress ticks, got {zqi25_count}"


async def test_default_refresh_ticks_is_empty_no_post_command_polling(
    fake_transport: FakeTransport,
) -> None:
    """The shipped default (Full v5 happy path) issues no follow-up polls.

    Regression for the v5 cleanup: REFRESH_TICKS used to be (5.0,) to
    catch power transitions Full v4 couldn't push. With Full v5 the
    device pushes everything via !I25, so no extra polls are needed —
    REFRESH_TICKS defaults to () and request_refresh is a no-op.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    # Don't override REFRESH_TICKS — we want the production default.
    assert c.REFRESH_TICKS == ()
    await c.start()
    fake_transport.sent.clear()

    await c.power_on()
    await c.standby()
    await c.send_command("k")

    # Wait long enough that any vestigial 5s tick would have fired.
    await asyncio.sleep(0.2)
    await c.stop()

    # Only the three control commands themselves should appear; no extra
    # ZQS02 / ZQI25 from a refresh task.
    assert fake_transport.sent == [b"%", b"$", b"k"]


# ---------- Phase 1 setter tests (sharpness / game mode / fan / subtitle / auto aspect) ----------


async def test_set_sharpness_writes_command_with_cr_then_queries(
    fake_transport: FakeTransport,
) -> None:
    """set_sharpness writes ZY521ELS<CR> followed by a ZQI30 refresh."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_sharpness(enabled=True, level=4, sensitivity="N")

    await c.stop()
    assert fake_transport.sent == [b"ZY521Y4N\r", b"ZQI30"]


async def test_set_sharpness_validates_inputs(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    with pytest.raises(ValueError, match="0-7"):
        await c.set_sharpness(enabled=True, level=99)
    with pytest.raises(ValueError, match="'H' or 'N'"):
        await c.set_sharpness(enabled=True, level=2, sensitivity="X")

    # Neither bad call should have written anything.
    assert fake_transport.sent == []
    await c.stop()


async def test_set_game_mode_writes_with_cr_then_queries(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_game_mode(True)
    await c.set_game_mode(False)

    await c.stop()
    assert fake_transport.sent == [b"ZY5511\r", b"ZQI53", b"ZY5510\r", b"ZQI53"]


async def test_set_fan_speed_writes_with_cr(
    fake_transport: FakeTransport,
) -> None:
    """Fan speed has no query at all, so we only write.

    The wire digit is one below the requested speed — the device's menu is
    1-based over a 0-based wire value, so asking for 3 must send ``ZY5522``
    (sending ``ZY5523`` would land the device on 4).
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_fan_speed(3)

    await c.stop()
    assert fake_transport.sent == [b"ZY5522\r"]


async def test_set_fan_speed_spans_the_full_wire_range(
    fake_transport: FakeTransport,
) -> None:
    """Speed 1 and 10 must map to the wire ends, 0 and 9."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_fan_speed(1)
    await c.set_fan_speed(10)

    await c.stop()
    assert fake_transport.sent == [b"ZY5520\r", b"ZY5529\r"]


async def test_set_fan_speed_validates_range(
    fake_transport: FakeTransport,
) -> None:
    """0 and 11 are outside the device's 1-10 menu range."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    with pytest.raises(ValueError, match="1-10"):
        await c.set_fan_speed(11)
    with pytest.raises(ValueError, match="1-10"):
        await c.set_fan_speed(0)
    await c.stop()


async def test_set_subtitle_shift_writes_with_cr(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_subtitle_shift(2)

    await c.stop()
    assert fake_transport.sent == [b"ZY5532\r"]


async def test_set_subtitle_shift_validates(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    with pytest.raises(ValueError, match="0, 1, or 2"):
        await c.set_subtitle_shift(3)
    await c.stop()


async def test_reset_auto_aspect_writes_with_cr_then_queries(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.reset_auto_aspect()

    await c.stop()
    assert fake_transport.sent == [b"ZY550\r", b"ZQI54"]


async def test_query_methods_dispatch_correct_strings(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.query_sharpness()
    await c.query_game_mode()
    await c.query_auto_aspect()

    await c.stop()
    assert fake_transport.sent == [b"ZQI30", b"ZQI53", b"ZQI54"]


# ---------- Phase 2 HDR setter / query tests ----------


async def test_query_display_rec2020_dispatches_correct_string(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.query_display_rec2020()
    await c.query_source_hdr_status()

    await c.stop()
    assert fake_transport.sent == [b"ZQI50", b"ZQI52"]


async def test_set_hdr_intensity_mapping_writes_command_with_cr(
    fake_transport: FakeTransport,
) -> None:
    """ZY417 takes 5-digit nits + gamma mode, terminated with CR."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_hdr_intensity_mapping(display_max_nits=1000, gamma_mode="A")

    await c.stop()
    # 1000 nits zero-padded to 5 digits + auto gamma + CR.
    assert fake_transport.sent == [b"ZY41701000A\r"]


async def test_set_hdr_intensity_mapping_disable_uses_zero_nits(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    await c.set_hdr_intensity_mapping(display_max_nits=0, gamma_mode="A")

    await c.stop()
    assert fake_transport.sent == [b"ZY41700000A\r"]


async def test_set_hdr_intensity_mapping_validates_inputs(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    await c.start()
    fake_transport.sent.clear()

    with pytest.raises(ValueError, match="50-10000"):
        await c.set_hdr_intensity_mapping(display_max_nits=49, gamma_mode="A")
    with pytest.raises(ValueError, match="50-10000"):
        await c.set_hdr_intensity_mapping(display_max_nits=20000, gamma_mode="A")
    with pytest.raises(ValueError, match="'A', 'H', or 'S'"):
        await c.set_hdr_intensity_mapping(display_max_nits=1000, gamma_mode="X")

    assert fake_transport.sent == []
    await c.stop()


# ---------- Input label discovery (query_input_labels) ----------


async def test_query_input_labels_sends_serial_queries(
    fake_transport: FakeTransport,
) -> None:
    """query_input_labels issues ZQS1<mem><0-7> for inputs 1-8, in order."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()
    fake_transport.sent.clear()

    # Nothing answers, so every input burns its (tiny) deadline. The point is
    # that all 8 still go out — a timeout skips one label, not the sweep.
    await c.query_input_labels(memory="A", timeout=0.01)

    await c.stop()
    assert fake_transport.sent == [
        b"ZQS1A0",
        b"ZQS1A1",
        b"ZQS1A2",
        b"ZQS1A3",
        b"ZQS1A4",
        b"ZQS1A5",
        b"ZQS1A6",
        b"ZQS1A7",
    ]


async def test_query_input_labels_validates_memory(
    fake_transport: FakeTransport,
) -> None:
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()
    fake_transport.sent.clear()
    with pytest.raises(ValueError, match="'A'-'D'"):
        await c.query_input_labels(memory="Z")
    assert fake_transport.sent == []
    await c.stop()


async def test_query_input_labels_populates_state_end_to_end(
    fake_transport: FakeTransport,
) -> None:
    """Prime + response feed fills state.input_labels; unanswered inputs stay unset.

    Wraps the transport write so each ZQS1A<y> is answered synchronously with
    a canned label, exercising the full serialized prime->send->correlate loop.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()
    fake_transport.sent.clear()

    original_write = fake_transport.write
    # y (input-1) -> label. Inputs 4-8 (y=3..7) deliberately unanswered.
    answers = {0: "Apple TV", 1: "Roku", 2: "Shield"}

    async def _write_and_answer(data: bytes) -> None:
        await original_write(data)
        if data.startswith(b"ZQS1A") and len(data) == 6:
            name = answers.get(int(chr(data[5])))
            if name is not None:
                fake_transport.feed(f"!S1A,{name}\r\n".encode())

    fake_transport.write = _write_and_answer  # type: ignore[method-assign]

    await c.query_input_labels(memory="A", timeout=0.02)
    await c.stop()

    assert c.state.input_labels == {1: "Apple TV", 2: "Roku", 3: "Shield"}


async def test_label_query_timeout_clears_primer_so_a_late_reply_is_dropped(
    fake_transport: FakeTransport,
) -> None:
    """An unanswered label query must not leave the parser primed.

    This is the misattribution the old fixed-sleep loop couldn't prevent: it
    primed input N, slept, and moved on to N+1 with the primer still set, so a
    reply that arrived a moment too late was filed under the wrong input. Now
    a timeout clears the primer, which downgrades "too slow" from a wrong
    label to a missing one.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()

    # Nothing answers any of the 8 queries.
    await c.query_input_labels(memory="A", timeout=0.01)
    assert c.state.input_labels == {}

    # A reply straggling in after the sweep has no owner and must be dropped
    # rather than attributed to input 8 (the last one primed).
    fake_transport.feed(b"!S1A,Late Reply\r\n")
    await c.stop()
    assert c.state.input_labels == {}


# ---------- Request/response correlation ----------


async def test_query_and_wait_returns_response_payload(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """The awaited payload is the text after the code.

    The fake answers ZQS01 synchronously inside write(), which also pins down
    that the waiter is registered *before* the send — otherwise this reply
    would land before anyone was listening and the call would time out.
    """
    await client.start()
    payload = await client.query_and_wait("ZQS01")
    assert payload == "FakeModel,000000,0000,000000"


async def test_query_and_wait_times_out_when_nothing_answers(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """A missing reply raises the builtin TimeoutError, not a library type.

    aiolumagen deliberately has no timeout exception of its own, so consumers
    already catching TimeoutError around asyncio.timeout need no changes.
    """
    await client.start()
    with pytest.raises(TimeoutError):
        await client.query_and_wait("ZQS02", timeout=0.05)


async def test_query_and_wait_returns_empty_payload_for_unsupported_query(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """An empty payload is a real answer: "replied, but doesn't support this".

    The Lumagen echoes any syntactically valid ZQ code with nothing after the
    comma, so callers must be able to tell that apart from silence. Silence is
    a TimeoutError; this is "".
    """
    await client.start()
    original_write = fake_transport.write

    async def _write_and_answer(data: bytes) -> None:
        await original_write(data)
        if data == b"ZQI99":
            fake_transport.feed(b"!I99,\r\n")

    fake_transport.write = _write_and_answer  # type: ignore[method-assign]
    assert await client.query_and_wait("ZQI99", timeout=0.5) == ""


async def test_query_and_wait_rejects_uninferable_command_without_expect(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """ZQS1A0 is answered with !S1A, so its code can't be inferred."""
    await client.start()
    fake_transport.sent.clear()
    with pytest.raises(LumagenCommandError, match="cannot infer"):
        await client.query_and_wait("ZQS1A0")
    # Nothing should have been written for a command we refused to track.
    assert fake_transport.sent == []


async def test_wait_for_response_resolves_on_unsolicited_push(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """wait_for_response works without sending anything — for pushed reports."""
    await client.start()

    async def _push_later() -> None:
        await asyncio.sleep(0.02)
        fake_transport.feed(b"!S02,1\r\n")

    push_task = asyncio.create_task(_push_later())
    assert await client.wait_for_response("!S02", timeout=1.0) == "1"
    assert client.state.power_on is True
    await push_task


async def test_response_observer_fires_even_when_state_is_unchanged(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """Correlation must not inherit the state-change dedupe.

    The protocol suppresses update callbacks when a response changes nothing,
    so a waiter keyed on state would hang on the second identical poll. The
    observer hook fires per response line instead.
    """
    await client.start()
    fake_transport.feed(b"!S02,1\r\n")  # establish power on
    # Second identical response: no state change, so no state listener fires.
    original_write = fake_transport.write

    async def _write_and_answer(data: bytes) -> None:
        await original_write(data)
        if data == b"ZQS02":
            fake_transport.feed(b"!S02,1\r\n")

    fake_transport.write = _write_and_answer  # type: ignore[method-assign]
    assert await client.query_and_wait("ZQS02", timeout=0.5) == "1"


async def test_stop_fails_pending_waiters_with_connection_error(
    fake_transport: FakeTransport,
) -> None:
    """A waiter outliving the client gets a catchable error, not CancelledError."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()

    waiter = asyncio.create_task(c.wait_for_response("S02", timeout=5.0))
    await asyncio.sleep(0)  # let the task register its waiter
    await c.stop()

    with pytest.raises(LumagenConnectionError):
        await waiter


async def test_query_device_info_returns_the_payload(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """The one query in the family that awaits its answer.

    Callers use it as a connection check ("did a Lumagen actually reply on this
    port?"), which is a question a fire-and-forget send can't answer. It also
    keeps the ZQS01 literal inside the library, so a consumer doing the same
    check doesn't have to hardcode a wire code.
    """
    await client.start()
    fake_transport.sent.clear()
    payload = await client.query_device_info()
    assert payload == "FakeModel,000000,0000,000000"
    assert fake_transport.sent == [b"ZQS01"]
    # Parsed fields still land on state as usual.
    assert client.state.model == "FakeModel"


async def test_query_device_info_times_out_on_a_silent_device(
    fake_transport: FakeTransport,
) -> None:
    """A port with nothing on the other end raises rather than hanging."""
    c = LumagenClient(
        fake_transport,
        power_poll_interval=None,
        status_poll_interval=None,
    )
    await c.start()

    # Stop the fake answering, simulating a port with no Lumagen behind it.
    async def _write_without_answering(data: bytes) -> None:
        fake_transport.sent.append(data)

    fake_transport.write = _write_without_answering  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await c.query_device_info(timeout=0.05)
    await c.stop()


# ---------- OSD messaging / labels / hotplug / config ----------


async def test_show_message_writes_with_cr(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """ZT needs the CR terminator; ZB and ZC do not.

    Getting that wrong is invisible in testing against a fake and obvious on
    hardware (the message never appears), so it's pinned per command.
    """
    await client.start()
    fake_transport.sent.clear()
    await client.show_message("Volume 42")
    assert fake_transport.sent == [b"ZT3Volume 42\r"]


async def test_clear_message_and_block_char_send_without_cr(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    await client.set_osd_block_char("#")
    await client.clear_message()
    assert fake_transport.sent == [b"ZB#", b"ZC"]


async def test_show_message_explicit_rows_pad_row_one(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    await client.show_message(line1="Input 3", line2="Apple TV", duration=9)
    assert fake_transport.sent == [b"ZT9Input 3                       Apple TV\r"]


async def test_show_message_validation_happens_before_any_write(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """A rejected message must not put a partial command on the wire."""
    await client.start()
    fake_transport.sent.clear()
    with pytest.raises(LumagenCommandError):
        await client.show_message("hello", duration=42)
    with pytest.raises(LumagenCommandError):
        await client.show_message("~~~")  # sanitises to nothing
    assert fake_transport.sent == []


async def test_show_message_does_not_trigger_a_status_refresh(
    fake_transport: FakeTransport,
) -> None:
    """An OSD message changes no device state worth re-polling.

    It is also not Z-prefixed... except it is, so the send_command guard
    already covers it. Pinned anyway: if that guard ever keys off something
    other than the Z prefix, a message must not start a poll storm.
    """
    c = LumagenClient(
        fake_transport,
        power_poll_interval=10.0,
        status_poll_interval=10.0,
        stale_timeout=30.0,
    )
    c.REFRESH_TICKS = (0.05,)
    await c.start()
    fake_transport.sent.clear()
    await c.show_message("hi")
    await asyncio.sleep(0.15)
    await c.stop()
    assert fake_transport.sent == [b"ZT3hi\r"]


async def test_set_input_label_writes_then_reads_back(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """The read-back is the point: the device is the authority on what it kept.

    Without it, state.input_labels would report what was requested rather than
    what was stored, which diverges the moment the device rejects or trims a
    label.
    """
    await client.start()
    fake_transport.sent.clear()

    original_write = fake_transport.write

    async def _write_and_answer(data: bytes) -> None:
        await original_write(data)
        if data == b"ZQS1A1":
            fake_transport.feed(b"!S1A,Roku 2A\r\n")

    fake_transport.write = _write_and_answer  # type: ignore[method-assign]

    await client.set_input_label(2, "Roku 2A")
    assert fake_transport.sent == [b"ZY524A1Roku 2A\r", b"ZQS1A1"]
    assert client.state.input_labels == {2: "Roku 2A"}


async def test_set_input_label_all_memories_reads_back_bank_a(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    """All four banks were written to the same value, so bank A confirms them."""
    await client.start()
    fake_transport.sent.clear()
    await client.set_input_label(
        1,
        "Shield",
        memory="ALL",
    )
    assert fake_transport.sent[0] == b"ZY52400Shield\r"
    assert fake_transport.sent[1] == b"ZQS1A0"


async def test_set_input_label_rejects_bad_input_before_writing(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    with pytest.raises(LumagenCommandError, match="1-8"):
        await client.set_input_label(9, "Nope")
    with pytest.raises(LumagenCommandError, match="characters or fewer"):
        await client.set_input_label(1, "x" * 11)
    assert fake_transport.sent == []


async def test_restart_input_writes_with_cr(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    await client.restart_input(3)
    await client.restart_input("all")
    await client.restart_input()
    assert fake_transport.sent == [b"ZY5202\r", b"ZY520A\r", b"ZY520A\r"]


async def test_show_aspect_and_save_config_write_with_cr(
    fake_transport: FakeTransport, client: LumagenClient
) -> None:
    await client.start()
    fake_transport.sent.clear()
    await client.show_aspect()
    await client.save_config()
    assert fake_transport.sent == [b"ZY811\r", b"ZY6SAVECONFIG\r"]
