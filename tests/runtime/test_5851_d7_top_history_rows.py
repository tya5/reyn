"""Tier 2: #5851 D7 — ``ProcessMemoryForensics.top_history_rows`` is now
actually filled in (was ``None`` while #5896 was still landing; 09-10
ruling: "genuinely buildable now, walk not needed").

Real ``Session``s throughout (``make_session``), real ``ProcessMemoryGuard``,
history rows appended directly (``session.history.append(...)`` — the same
"setup, not the code path under test" precedent
``test_1128_step3_token_budget_headtail.py``/``test_4387_history_resident_
eviction.py`` already use for this exact list). Witnessed through
``run_cache_release_and_forensics``'s own return value AND the
``process_memory_forensics`` audit-event's payload — never through
``_top_history_rows`` called as if it were the public surface on its own,
and never through a session's own ``_media_store`` directly (this repo's own
testing policy, and the same convention
``test_5939_pr5_instance_cache_ladder_wiring.py``'s module docstring states).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import (
    CONTENT_BYTES_META_KEY,
    CONTENT_REF_META_KEY,
    SPILLED_META_KEY,
    ChatMessage,
)
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.runtime.process_memory_release import (
    _TOP_HISTORY_ROWS_LIMIT,
    run_cache_release_and_forensics,
)
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


def _session(tmp_path: Path, name: str) -> "object":
    return make_session(
        agent_name=name,
        state_log=StateLog(tmp_path / f"wal-{name}.jsonl"),
        snapshot_path=tmp_path / f"snap-{name}.json",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / f"ws-{name}",
    )


def _guard() -> ProcessMemoryGuard:
    return ProcessMemoryGuard(reader=lambda: 0, cap_bytes=None, enforce=False, metric="phys_footprint")


async def _run(session) -> "object":
    """Drives the real public entry point with the SAME ``EventLog`` the
    session's own production ladder call uses (``s._audit_events`` — an
    accepted direct reach in this exact test family, see
    ``test_5939_pr5_instance_cache_ladder_wiring.py``'s own ``s._audit_
    events.emit(...)`` calls; unlike ``_media_store``, this attribute
    IS the module's own testing-policy-sanctioned seam here)."""
    return await run_cache_release_and_forensics(_guard(), session._audit_events, sessions=[session])


@pytest.mark.asyncio
async def test_the_biggest_resident_row_sorts_first(tmp_path: Path) -> None:
    """Tier 2: the core ordering claim — a real, much-larger message's
    own ``resident_bytes()`` outweighs several small ones, and
    ``top_history_rows`` reflects that, not append order."""
    s = _session(tmp_path, "d7-order")
    s.history.append(ChatMessage(role="user", content="small a"))
    s.history.append(ChatMessage(role="user", content="small b"))
    s.history.append(ChatMessage(role="assistant", content="X" * 5000, name="big-one"))
    s.history.append(ChatMessage(role="user", content="small c"))

    forensics = await _run(s)

    assert forensics.top_history_rows, "arrange: the population must be non-empty"
    assert forensics.top_history_rows[0]["tool"] == "big-one", (
        f"the largest resident row was not sorted first: {forensics.top_history_rows!r}"
    )
    # Every OTHER row genuinely weighs less than the winner — proves the
    # sort is real, not a lucky single-element order.
    winner_bytes = forensics.top_history_rows[0]["bytes"]
    assert all(r["bytes"] <= winner_bytes for r in forensics.top_history_rows[1:])


@pytest.mark.asyncio
async def test_the_schema_matches_architects_own_sketch(tmp_path: Path) -> None:
    """Tier 2: each row is exactly ``{tool, bytes, ts}`` (#5939's own
    schema sketch) — ``tool`` degrades to ``None`` for a non-tool row
    rather than fabricating a name."""
    s = _session(tmp_path, "d7-schema")
    s.history.append(ChatMessage(role="user", content="plain row", ts="2026-09-10T00:00:00Z"))

    forensics = await _run(s)

    (row,) = forensics.top_history_rows
    assert set(row) == {"tool", "bytes", "ts"}, row
    assert row["tool"] is None, "a non-tool row must not fabricate a tool name"
    assert row["ts"] == "2026-09-10T00:00:00Z"
    assert isinstance(row["bytes"], int) and row["bytes"] > 0


@pytest.mark.asyncio
async def test_a_content_ref_row_counts_its_real_body_not_just_its_shell(
    tmp_path: Path,
) -> None:
    """Tier 2: #5973's own correction, reused here — an un-spilled
    content-ref row's weight is ``resident_bytes()`` (a small shell,
    post-#5896) PLUS its stamped ``CONTENT_BYTES_META_KEY`` body size,
    never the shell alone. The stamped-meta path needs no real
    ``MediaStore`` I/O (``ChatMessage.body_bytes`` reads the stamped int
    directly — see that method's own docstring), so ``media_store``
    stays whatever the session's own default already is.

    Strip-falsify (verified by hand, file-internal Edit only, reverted):
    replacing ``_top_history_rows``'s weight formula with
    ``m.resident_bytes()`` alone turns this red (the big row's bytes
    drops far below the stamped body size)."""
    s = _session(tmp_path, "d7-content-ref")
    small_row = ChatMessage(role="user", content="tiny")
    ref_row = ChatMessage(
        role="tool", content="", name="read_file",
        meta={CONTENT_REF_META_KEY: "some/ref", CONTENT_BYTES_META_KEY: 9_000_000},
    )
    s.history.append(small_row)
    s.history.append(ref_row)

    forensics = await _run(s)

    (winner,) = [r for r in forensics.top_history_rows if r["tool"] == "read_file"]
    assert winner["bytes"] >= 9_000_000, (
        f"the content-ref row's own stamped body size did not reach "
        f"top_history_rows -- only its small resident shell did: {winner!r}"
    )


@pytest.mark.asyncio
async def test_a_spilled_content_ref_row_does_not_double_count_its_body(
    tmp_path: Path,
) -> None:
    """Tier 2: deny side -- a row already ``SPILLED_META_KEY``-marked has
    had its body moved OUT of process memory (the entire point of
    spilling); its weight must stay its own small resident shell, never
    adding the (no-longer-resident) body size back in."""
    s = _session(tmp_path, "d7-spilled")
    spilled_row = ChatMessage(
        role="tool", content="", name="spilled-tool",
        meta={
            CONTENT_REF_META_KEY: "some/ref",
            CONTENT_BYTES_META_KEY: 9_000_000,
            SPILLED_META_KEY: True,
        },
    )
    s.history.append(spilled_row)

    forensics = await _run(s)

    (row,) = forensics.top_history_rows
    assert row["bytes"] < 9_000_000, (
        f"a spilled row's body was counted as if it were still resident: {row!r}"
    )


@pytest.mark.asyncio
async def test_the_limit_caps_the_returned_rows(tmp_path: Path) -> None:
    """Tier 2: more resident rows than the limit -- only the top N come
    back, never the full population."""
    s = _session(tmp_path, "d7-limit")
    for i in range(_TOP_HISTORY_ROWS_LIMIT + 3):
        s.history.append(ChatMessage(role="user", content=f"row {i}" * (i + 1)))

    forensics = await _run(s)

    assert len(forensics.top_history_rows) == _TOP_HISTORY_ROWS_LIMIT


@pytest.mark.asyncio
async def test_rows_from_multiple_sessions_are_merged_before_ranking(
    tmp_path: Path,
) -> None:
    """Tier 2: the population spans every session in ``sessions``, not
    just the first -- a big row on the SECOND session still wins."""
    a = _session(tmp_path, "d7-multi-a")
    b = _session(tmp_path, "d7-multi-b")
    a.history.append(ChatMessage(role="user", content="small"))
    b.history.append(ChatMessage(role="assistant", content="Y" * 5000, name="second-session-big"))

    forensics = await run_cache_release_and_forensics(_guard(), a._audit_events, sessions=[a, b])

    assert forensics.top_history_rows[0]["tool"] == "second-session-big"


@pytest.mark.asyncio
async def test_no_sessions_yields_an_empty_list_not_an_error(tmp_path: Path) -> None:
    """Tier 2: non-vacuity's deny side -- ``sessions=None`` (every
    pre-PR-5 caller) is a normal input, not an error, and reads back as
    ``[]`` -- never ``None`` (the field's own OLD type), so a consumer
    can always iterate it without a null check."""
    from reyn.core.events.events import EventLog

    forensics = await run_cache_release_and_forensics(_guard(), EventLog(), sessions=None)
    assert forensics.top_history_rows == []


@pytest.mark.asyncio
async def test_top_history_rows_reaches_the_audit_event(tmp_path: Path) -> None:
    """Tier 2: the machine-readable face actually carries the field --
    populating ``ProcessMemoryForensics`` alone would be dead data if the
    audit-event (the face other processes / a post-mortem reader
    actually consume) never forwarded it."""
    s = _session(tmp_path, "d7-event")
    s.history.append(ChatMessage(role="assistant", content="Z" * 5000, name="event-row"))
    events = collect_events(s)

    await _run(s)
    await settle(s)

    (forensics_event,) = [e for e in events if e.type == "process_memory_forensics"]
    rows = forensics_event.data["top_history_rows"]
    assert rows and rows[0]["tool"] == "event-row", rows
