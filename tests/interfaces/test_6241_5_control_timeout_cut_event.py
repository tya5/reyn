"""Tier 2: #6241 ⑤ — a BOUNDED control POST (``payload["type"]`` in
``BOUNDED_PAYLOAD_TYPES``) that is actually CUT by
``remote_client._CONTROL_TIMEOUT_S`` must leave a durable, bounded witness
(a ``control_post_bounded_timeout_cut`` audit-event) — the one
misclassification #6083's human trace and #6244's AST-derived prologue
population cannot see: both can only confirm a ``ptype`` does not AWAIT an
unbounded operation, never that the bounded timeout is enough headroom for
the one it does await. That gap is invisible to any static check and shows
up only at runtime, as a real ``httpx.ReadTimeout``.

Real collaborators throughout (CLAUDE.md — no MagicMock/AsyncMock/patch):
``post_control`` is exercised against a REAL ``asyncio.start_server``
listener that accepts and never answers (the SAME idiom
``test_5894_control_timeout_and_coalesce.py`` already established for this
exact module), with ``timeout_s`` injected as the subject (testing.md — a
test writes no duration; the DECISION/threshold is an input, never a
``sleep`` the assertion waits out) rather than any wait the test sits out.
The audit-event side reads back real ``emit_cli_event`` writes from a real
``.reyn/events`` directory (mirrors ``test_5732_pump_swallow_visibility.py``
/ ``test_6230_stage2_unknown_kind_degrade.py``'s own end-to-end idiom).

Strip-falsify performed (observed, then reverted via in-file Edit — no
``git checkout``/``stash``/``restore`` used, CLAUDE.md): replacing the
``_record_control_timeout_cut(ptype, read_timeout)`` call inside
``post_control``'s ``except`` clause with ``pass`` turned
``test_a_cut_bounded_control_post_emits_the_event_once`` RED — not a fast
assertion failure but an unbounded HANG: ``_wait_for_events_of_kind``
polls with no ceiling (testing.md's own rule) for an event that, with the
call stripped, never lands, so the test never returns on its own and is
only ever ended by an external kill switch (CI's ``--timeout=120``; a
local ``timeout 20 pytest ...`` observed exit code ``124``). Reverted with
the same Edit tool, back to green (6 passed).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from reyn.interfaces.repl import remote_client
from reyn.interfaces.repl.remote_client import ControlTimeoutCutStats, post_control

_T = 0.05  # the injected control timeout — the subject, not a wait


async def _silent_server(hold: asyncio.Event):
    """A real listener that reads the request and then holds the connection
    open, writing nothing, until ``hold`` is set (teardown) — identical
    idiom to ``test_5894_control_timeout_and_coalesce.py``'s own helper,
    duplicated rather than imported (that module is not a shared support
    module; neither is this one)."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)
            await hold.wait()
        finally:
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


def _url(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}/agui/chat/alpha"


def _read_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    found: "list[dict]" = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


async def _wait_for_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    """Poll for at least one matching event, unboundedly (testing.md — no
    ceiling, no ``sleep(N)`` the assertion depends on). Needed because
    ``EventStore.write`` (``core/events/event_store.py``) writes off-loop
    via a background ``DurabilityWorker`` whenever an event loop is
    running — the exact situation every ``async def test_...`` here runs
    under — so the write can genuinely still be in flight the instant
    ``post_control`` returns. ``asyncio.sleep(0)`` is a cooperative yield,
    not a wait duration; CI's own ``--timeout=120`` is the kill switch."""
    while True:
        found = _read_events_of_kind(events_dir, kind)
        if found:
            return found
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# ControlTimeoutCutStats — unit level, mirrors PumpSwallowStats's own tests
# ---------------------------------------------------------------------------


def test_record_returns_true_only_on_the_first_occurrence_of_a_ptype() -> None:
    """Tier 2: the dedup-gate witness — the SAME ``ptype`` returns True
    once, then False on every further occurrence."""
    stats = ControlTimeoutCutStats()
    first = stats.record("heartbeat")
    second = stats.record("heartbeat")
    third = stats.record("heartbeat")
    assert (first, second, third) == (True, False, False)


def test_counts_grows_every_occurrence_even_while_dedup_gates_the_event() -> None:
    """Tier 2: the public per-ptype count is COMPLETE (every occurrence),
    independent of the event-emission gate ``record()``'s return value
    drives."""
    stats = ControlTimeoutCutStats()
    for _ in range(5):
        stats.record("heartbeat")
    assert stats.counts["heartbeat"] == 5


def test_a_different_ptype_is_a_separate_first_occurrence() -> None:
    """Tier 2: falsification pair — dedup is keyed per-ptype, not global;
    a different ``ptype`` gets its own first-occurrence True and its own
    independent count."""
    stats = ControlTimeoutCutStats()
    assert stats.record("heartbeat") is True
    assert stats.record("user_message") is True
    assert stats.counts == {"heartbeat": 1, "user_message": 1}


# ---------------------------------------------------------------------------
# post_control against a real silent server — real emit + real read-back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cut_bounded_control_post_emits_the_event_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the defect this PR closes, end to end — a real BOUNDED
    control POST (``payload["type"] == "heartbeat"``, a member of
    ``BOUNDED_PAYLOAD_TYPES``) against a server that never answers raises
    a real ``httpx.ReadTimeout`` after the injected ``timeout_s``, and
    that raise leaves a durable ``control_post_bounded_timeout_cut``
    audit-event naming the payload type and the timeout that was in
    force — never the exception's own message or traceback (CLAUDE.md /
    the ``pump_exception_swallowed`` precedent this mirrors)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    # Process-lifetime module state (mirrors PumpSwallowStats's own
    # per-App-instance scope one level up) — swapped for a fresh instance
    # so this test's own first-occurrence assertion cannot be polluted by
    # an earlier test's "heartbeat" cut in the SAME pytest process.
    monkeypatch.setattr(remote_client, "_CONTROL_TIMEOUT_CUT_STATS", ControlTimeoutCutStats())

    hold = asyncio.Event()
    server = await _silent_server(hold)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            result = await post_control(
                client, _url(server), params={}, payload={"type": "heartbeat"},
                timeout_s=_T,
            )
            assert not result and result.kind == "not_delivered"

        events = await _wait_for_events_of_kind(
            reyn_dir / "events", "control_post_bounded_timeout_cut",
        )
        [event] = events  # exactly one — unpack raises otherwise (strip-falsify target)
        assert event["data"]["payload_type"] == "heartbeat"
        assert event["data"]["timeout_seconds"] == _T
        assert "message" not in event["data"]
        assert "traceback" not in event["data"]
    finally:
        hold.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_second_cut_of_the_same_ptype_emits_no_second_event_but_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the bounded-by-key shape ``pump_exception_swallowed``
    established — the SAME ``ptype`` cut twice durably records exactly
    ONE audit-event (a persistently slow network must not flood
    ``.reyn/events``), while ``ControlTimeoutCutStats.counts`` keeps the
    complete tally readable from a test or a debugger — not from any
    operator-facing surface (none reads ``counts`` today; the audit-event
    itself, in ``.reyn/events``, is the operator-facing side)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    stats = ControlTimeoutCutStats()
    monkeypatch.setattr(remote_client, "_CONTROL_TIMEOUT_CUT_STATS", stats)

    hold = asyncio.Event()
    server = await _silent_server(hold)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            for _ in range(2):
                result = await post_control(
                    client, _url(server), params={}, payload={"type": "heartbeat"},
                    timeout_s=_T,
                )
                assert not result and result.kind == "not_delivered"

        events = await _wait_for_events_of_kind(
            reyn_dir / "events", "control_post_bounded_timeout_cut",
        )
        [event] = events  # STILL exactly one, not two
        assert stats.counts["heartbeat"] == 2
    finally:
        hold.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_control_post_that_succeeds_emits_no_timeout_cut_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification pair — a control POST that never raises
    ``httpx.ReadTimeout`` at all (a real listener that answers instead of
    holding) must not emit the event; the discriminating control that
    proves the emit above is caused by the cut, not by every control POST."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(remote_client, "_CONTROL_TIMEOUT_CUT_STATS", ControlTimeoutCutStats())

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)
            body = b'{"status": "ok"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            result = await post_control(
                client, _url(server), params={}, payload={"type": "heartbeat"},
                timeout_s=_T,
            )
            assert result and result.kind == "delivered"
        # A cooperative yield lets any background DurabilityWorker task
        # run a step — not a wait the assertion depends on; nothing is
        # expected to ever appear, so there is no condition to poll for.
        await asyncio.sleep(0)
        events = _read_events_of_kind(reyn_dir / "events", "control_post_bounded_timeout_cut")
        assert events == []
    finally:
        server.close()
        await server.wait_closed()
