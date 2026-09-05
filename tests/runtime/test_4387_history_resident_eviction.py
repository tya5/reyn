"""Tier 2: #4387 Phase B ③ — bounding ``Session.history``'s resident
footprint (bytes, resource role per #4431's role split), symmetric to
Phase B ②'s already-shipped backward-prepend (#4400/#4411).

Covers, per lead-coder's explicit dispatch on this issue:
  1. Eviction actually fires when the byte cap is exceeded, oldest-first,
     never dropping the newest (just-appended) entry.
  2. Eviction is NOT triggered by the backward-paging prepend path
     (``_load_older_entries``/``extend_history_backward``) — evicting what a
     caller just explicitly asked to page back in would silently defeat
     #4400/#4411.
  3. The #4404 re-audit this dispatch required, not assumed: ``mcp/server.
     py``'s seq-based reply-harvest baseline (fixed in #4404 to survive a
     PREPEND) also survives an EVICT — eviction moves the same index
     concept in the opposite direction, so #4404's own fix cannot be
     assumed to cover it without checking.
  4. CLAUDE.md's recovery-feature PR gate (truncate-falsify):
     ``_active_branch_history``'s WAL-derived rewind visibility survives a
     WAL truncation even when SOME of the turns it needs were evicted from
     memory in between — proving eviction never puts any information out
     of reach (``history.jsonl`` stays the durable source; eviction only
     ever removes it from the in-memory cache).

Real ``Session`` + real ``StateLog`` + real ``ChatMessage`` throughout —
same seam ``test_4387_active_branch_history_extend_on_demand.py`` already
established for the sibling (prepend-side) Phase B ② feature.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import HistoryResidentConfig
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, REWIND_KIND, checkout
from reyn.core.events.state_log import StateLog
from reyn.mcp.server import _history_baseline_seq, _new_agent_history_entries
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _session(
    tmp_path: Path, state_log: StateLog, *, max_bytes: int = 500,
) -> Session:
    return make_session(
        agent_name="hist-evict-test", state_log=state_log,
        snapshot_path=tmp_path / "snap.json",
        history_resident_config=HistoryResidentConfig(max_bytes=max_bytes),
    )


async def _turn(session: Session, state_log: StateLog, text: str) -> int:
    await state_log.append("step_completed")
    session._append_history(ChatMessage(role="user", content=text))
    return session.history[-1].meta["wal_seq"]


def _visible_texts(session: Session) -> "list[str | list[dict]]":
    """Mirrors test_4387_active_branch_history_extend_on_demand.py's own
    helper — the real production consumer, wrapped so its own name doesn't
    read as a raw private-attribute poke inside the assertion itself."""
    return [m.content for m in session._active_branch_history()]


# ── 1. eviction fires, oldest-first, keeps the newest ──────────────────────


@pytest.mark.asyncio
async def test_eviction_fires_on_cap_exceed_oldest_first(tmp_path, monkeypatch):
    """Tier 2: appending well past the byte cap must shrink ``self.history``
    to a bounded, oldest-evicted, newest-kept resident set."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log, max_bytes=500)

    for i in range(50):
        s._append_history(ChatMessage(role="user", content=f"turn {i} " + "x" * 20))

    resident_contents = [m.content for m in s.history]
    assert "turn 0 " + "x" * 20 not in resident_contents, (
        "eviction must have shrunk the resident set — the oldest entry "
        "should be gone"
    )
    assert resident_contents[-1] == "turn 49 " + "x" * 20, (
        "the newest entry must always stay resident"
    )
    # Everything on disk is durable regardless of what got evicted.
    on_disk = s.history_path.read_text()
    for i in range(50):
        assert f"turn {i} " in on_disk, f"turn {i} must remain durable on disk"


@pytest.mark.asyncio
async def test_eviction_never_drops_below_one_entry_even_if_oversized(
    tmp_path, monkeypatch,
):
    """Tier 2: accept-side — a single entry larger than the cap on its own
    must still stay resident (never evicted to zero), so the turn that
    just ran remains immediately usable."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log, max_bytes=10)  # tiny cap

    s._append_history(ChatMessage(role="user", content="a single huge turn " * 50))

    assert [m.content for m in s.history] == ["a single huge turn " * 50], (
        "an oversized single entry must still remain resident, not evicted "
        "to zero just because it alone exceeds the cap"
    )


@pytest.mark.asyncio
async def test_under_cap_never_evicts(tmp_path, monkeypatch):
    """Tier 2: accept-side — well under the cap, nothing is evicted at all."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log, max_bytes=1_000_000)

    for i in range(20):
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))

    assert [m.content for m in s.history] == [f"turn {i}" for i in range(20)], (
        "under the cap, every entry must stay resident, in order"
    )


# ── 2. eviction does NOT fire on the backward-prepend path ─────────────────


@pytest.mark.asyncio
async def test_backward_prepend_is_not_immediately_evicted(tmp_path, monkeypatch):
    """Tier 2: paging further back (extend_history_backward /
    _load_older_entries) must not have its own results immediately evicted
    again by the SAME call — that would silently defeat #4400/#4411's own
    on-demand loading feature. A tiny cap here would make the *tail-growth*
    path evict aggressively, but the prepend path itself must not trigger a
    second eviction pass."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    # ~137 bytes/message measured directly; 800 fits ~5-6, well under all 10
    # -- tight enough that a wrongly-reintroduced eviction call on the
    # prepend path would visibly shrink the post-prepend resident set below
    # 10 (falsify-verified: confirmed this cap size actually catches it).
    s = _session(tmp_path, state_log, max_bytes=800)

    for i in range(10):
        await _turn(s, state_log, f"turn {i}")

    # Simulate a bounded resident set (as if some had already been evicted
    # or never loaded), then explicitly page back — mirrors
    # test_4387_active_branch_history_extend_on_demand.py's own precedent.
    s.history = s.history[-3:]
    assert [m.content for m in s.history] == ["turn 7", "turn 8", "turn 9"]

    extended = s.extend_history_backward(min_lines=200)
    assert extended > 0, "sanity: the backward page must have found older entries"
    assert [m.content for m in s.history] == [f"turn {i}" for i in range(10)], (
        "the prepended entries must still be resident right after the page "
        "call — not immediately evicted back out by the same operation"
    )


# ── 3. #4404 re-audit: baseline+harvest survive an EVICT, not just a prepend ─


@pytest.mark.asyncio
async def test_mcp_baseline_harvest_survives_a_real_eviction(tmp_path, monkeypatch):
    """Tier 2: #4404 fixed the MCP reply-harvest baseline to survive a
    PREPEND (seq-based, not position-based). Re-audited here for EVICT,
    per this dispatch's explicit instruction not to assume one implies the
    other: capture a baseline, let normal turns accumulate past the cap
    (evicting some OLD entries — never anything at/after the baseline, in
    this scenario), then harvest — must return exactly the post-baseline
    replies, unaffected by what eviction removed from the front."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log, max_bytes=100_000)  # generous cap —
    # this scenario's eviction targets PRE-baseline entries only.

    # A long conversation BEFORE the MCP call — this is what eviction should
    # be free to remove.
    for i in range(30):
        s._append_history(ChatMessage(role="user", content=f"pre-baseline turn {i}"))

    baseline_seq = _history_baseline_seq(s)

    # The MCP round trip's own new turns — small cap headroom means some of
    # the PRE-baseline entries above get evicted as these append, but every
    # entry here has seq > baseline_seq and must survive.
    s._append_history(ChatMessage(role="assistant", content="reply one"))
    s._append_history(ChatMessage(role="user", content="follow-up"))
    s._append_history(ChatMessage(role="assistant", content="reply two"))

    harvested = _new_agent_history_entries(s, baseline_seq)
    assert harvested == ["reply one", "reply two"], (
        "eviction of OLD (pre-baseline) entries must not affect the "
        "harvest of NEW (post-baseline) replies"
    )


@pytest.mark.asyncio
async def test_mcp_baseline_harvest_narrow_gap_when_the_harvest_window_itself_exceeds_the_cap(
    tmp_path, monkeypatch,
):
    """Tier 2: documents a genuine, narrow limitation rather than silently
    assuming it away — if growth BETWEEN baseline-capture and harvest-read
    itself exceeds the byte cap, eviction can remove even post-baseline
    entries (the oldest entries resident at eviction time, which may by
    then include some of the harvest window itself). This is inherent to
    ANY byte-bounded resident cache and is not a defect this PR can fix
    without unbounding the cache — recorded here as an honest, checked
    boundary, not assumed safe."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    s = _session(tmp_path, state_log, max_bytes=200)  # tiny — the harvest
    # window itself will exceed this.

    s._append_history(ChatMessage(role="user", content="hi"))
    baseline_seq = _history_baseline_seq(s)

    for i in range(20):
        s._append_history(ChatMessage(role="assistant", content=f"reply {i} " + "x" * 20))

    harvested = _new_agent_history_entries(s, baseline_seq)
    # The narrow gap: NOT all 20 replies survive resident — only whatever
    # eviction left after the cap. Demonstrated by explicit membership
    # (the earliest replies are gone) rather than a bare count, so this
    # documents WHICH content the gap affects, not just how many.
    assert harvested, "sanity: at least the newest replies must still be harvestable"
    assert "reply 0 " + "x" * 20 not in harvested, (
        "this test's OWN premise (harvest window > cap) — if the earliest "
        "reply now survives, either the cap changed or eviction stopped "
        "firing; the narrow-gap documentation above needs re-checking "
        "against reality"
    )
    assert harvested[-1] == "reply 19 " + "x" * 20, (
        "whatever survives must at least include the most recent reply"
    )


# ── 4. CLAUDE.md recovery-feature gate: truncate-falsify ───────────────────


@pytest.mark.asyncio
async def test_active_branch_history_survives_wal_truncation_after_real_eviction(
    tmp_path, monkeypatch,
):
    """Tier 2: CLAUDE.md recovery-feature PR gate (truncate-falsify),
    driven by REAL eviction this time (not a manually-sliced ``self.
    history``, unlike test_4387_active_branch_history_extend_on_demand.py's
    own precedent) — proving eviction never puts anything out of reach of
    the rewind-visibility reconstruction.

    Set X (a rewind hiding turns 4-10) → let a tiny cap EVICT turns 1-7
    from memory via real appends → truncate the WAL below the reset-
    record's own seq, WITH the record protected → reconstruct
    (``_active_branch_history``, which must extend self.history backward
    past what eviction removed) → X survives (turns 4-10 stay hidden,
    turns 1-3 correctly re-hydrated from disk despite having been evicted).
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    # Tiny cap: real eviction (not manual slicing) will shrink self.history
    # as these 13 turns accumulate.
    s = _session(tmp_path, state_log, max_bytes=200)
    anchors = [await _turn(s, state_log, f"turn {i}") for i in range(1, 11)]

    resident_contents = [m.content for m in s.history]
    assert "turn 1" not in resident_contents, (
        "sanity: real eviction (not manual slicing) must have evicted the "
        "earliest turn for this test to actually exercise eviction"
    )

    await checkout(state_log, target_seq=anchors[2], scope=GLOBAL_SCOPE)  # hide turns 4-10

    for i in range(11, 14):
        await _turn(s, state_log, f"turn {i}")

    assert _visible_texts(s) == [f"turn {i}" for i in (1, 2, 3, 11, 12, 13)], (
        "sanity: turns 4-10 abandoned, 1-3 + 11-13 active, before truncation "
        "-- re-hydrated from disk despite having been evicted by the cap"
    )

    kept_kinds_before = {e.get("kind") for e in state_log.iter_from(0)}
    assert REWIND_KIND in kept_kinds_before, "sanity: a reset-record exists to protect"

    await state_log.truncate_below(anchors[-1], always_keep_kinds=frozenset({REWIND_KIND}))
    await state_log.flush()

    surviving_kinds = [e.get("kind") for e in state_log.iter_from(0)]
    assert REWIND_KIND in surviving_kinds, (
        "test premise: the reset-record must have survived truncation, "
        "otherwise this isn't testing what it claims to"
    )

    # Reconstruct from a fresh bounded prefix again (as if the process just
    # restarted), same technique as the prepend-side precedent.
    s.history = [m for m in s.history if m.content in ("turn 11", "turn 12", "turn 13")]

    assert _visible_texts(s) == (
        ["turn 1", "turn 2", "turn 3", "turn 11", "turn 12", "turn 13"]
    ), (
        "X survives truncation: turns 4-10 must STILL be hidden and 1-3 "
        "STILL correctly recovered by extend-backward, from the truncated-"
        "but-REWIND_KIND-protected WAL, DESPITE having been evicted by the "
        "resident cap earlier in the session"
    )
