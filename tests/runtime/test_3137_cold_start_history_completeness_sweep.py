"""Tier 2: #3137 — cold-start ``self.history`` completeness regression guard.

#3137 (deferred out of #2946 Item 3) asked for a consumer sweep of every
reader that assumes ``Session.history`` is COMPLETE (= reflects the whole
``history.jsonl`` transcript) on a cold start, on the theory that a lazy /
partial load path might leave ``build_history`` / ``_active_branch_history``
/ ``restore_all`` consumers looking at a truncated view.

**Sweep result (AST + manual trace, not regex): no defect found.** Every
production session-construction factory (``registry_bootstrap.py``,
``interfaces/cli/commands/chat.py``, ``interfaces/cli/commands/mcp.py``,
``interfaces/cli/commands/dogfood.py``, ``interfaces/web/deps.py``) calls
``s.load_history()`` SYNCHRONOUSLY, immediately after constructing the
``Session`` and BEFORE returning it to the caller. ``AgentRegistry._construct_
session`` (used by both ``get_or_load`` and ``spawn_session``) always
dispatches through this one factory, so EVERY session — including ones
``restore_all()`` constructs for a previously-unloaded agent during
crash-recovery — gets a fully-populated ``self.history`` before any consumer
(``build_history``, ``_active_branch_history``, ``_latest_summary``,
``_uncompacted_tool_call_records``, the MCP ``send_to_agent_impl`` baseline
read, the TUI restore projection, ``ChatReadModel.conversation_history``)
can observe it — there is no async gap between construction and
``load_history()`` where a partial-history race could open.

**#4387 Phase B ① UPDATE**: ``load_history()`` no longer always reads
``history.jsonl`` in full — on a file whose last entry carries a real
assigned ``seq`` (every append does, post-#3704), it reads backward and
stops once it has collected AT LEAST ``_HISTORY_HYDRATE_MIN_LINES`` (200)
lines AND has seen the latest ``role="summary"`` entry (or BOF). So
"complete" for a cold start now means "everything since the latest
compaction, plus a 200-line minimum floor" — NOT "the entire file" once a
session exceeds that floor with no compaction having run. This test's own
fixture (5 turns, no summary) stays well under the floor, so the bounded
read reaches BOF anyway and this test's completeness assertion still holds
byte-for-byte — it is not testing the general "whole file" claim the
sentence above used to make, only "small session ⟹ still fully loaded",
which remains true. A future test asserting completeness on a fixture
LARGER than the floor would need to also account for the watermark/floor
bound, not assume unconditional full-file completeness. #2946 items 1/2/4
(topology-lazy / restore_all-bucketing / StateLog-tail-read) still touch
WAL-derived AgentSnapshot state, a SEPARATE substrate from the
``history.jsonl``-derived chat log (see
``interfaces/inline/textual_chat/restore.py``'s docstring: the chat log is
"derived-at-read... NOT WAL-event-reconstructed state") — unaffected by
this update.

The two intentional exceptions — ``reyn chat --fresh`` / ``--no-restore`` —
deliberately SKIP the ``load_history()`` call so the session starts with a
genuinely EMPTY (not partial) transcript; that is documented, opted-in
behavior, not the completeness-assumption hazard #3137 was scoped against.

**Recovery-feature PR gate**: N/A. This PR adds no reconstruction / WAL-
event-derived state — it is a regression test locking in an EXISTING
invariant (every factory call-site already calls ``load_history()`` pre-
return), not new recovery functionality. ``history.jsonl`` is also not
WAL-derived (see the restore.py docstring cited above), so the truncate-
falsify gate's premise (WAL-event-derived state that must survive WAL
truncation) does not apply to it either.

This test is the regression guard for that finding: it exercises the exact
scenario #3137 worried about — a previously-unloaded agent whose session is
constructed by ``restore_all()`` during crash-recovery (not by an explicit
``get_or_load`` the test drives directly) — and asserts the reconstructed
session's ``self.history`` already contains the FULL pre-existing transcript,
with no further ``load_history()`` call needed. Real ``AgentRegistry`` +
``StateLog`` + ``Session`` throughout (no mocks); the test's session factory
mirrors the production shape (constructs, then calls ``load_history()``,
exactly like every real factory above) so a future factory that DROPS the
call — the actual regression this guards against — goes RED here.

**★ Scope of what the strip-falsify above actually witnesses.** The strip
removed ``load_history()`` from THIS TEST's OWN factory closure, not from any
of the five real production factories enumerated above. That proves "IF the
factory loads, a ``restore_all()``-constructed session sees a complete
history" — a real, load-bearing property, and what makes this test
non-vacuous. It does NOT prove "the five real factories actually call
``load_history()`` today" — deleting the ``s.load_history()`` call at
``interfaces/web/deps.py:359`` (or any of the other four) would leave this
test green, because this test never imports or exercises those call sites.
That second claim is established by manual/AST inspection in this PR's
enumeration, not gated by any test — a real, named gap, not a silent one.
"The mechanism is correct" and "production reaches the mechanism" are
separate claims; this test gates only the first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

AGENT = "alpha"


def _make_registry(tmp_path: Path, wal: Path) -> AgentRegistry:
    """Mirrors the production factory shape: construct, THEN load_history()
    before the session is handed back — the exact invariant #3137 asked
    whether every real factory upholds (it does; see the module docstring)."""
    state_log = StateLog(wal)

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        s.load_history()
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


def _iv_dict(iv_id: str) -> dict:
    return {
        "kind": "ask_user", "prompt": f"Q-{iv_id}?", "detail": "",
        "choices": [], "suggestions": [], "run_id": "r1",
        "actor": "demo", "id": iv_id,
    }


@pytest.mark.asyncio
async def test_restore_all_constructed_session_has_complete_history(tmp_path, monkeypatch):
    """Tier 2: a session ``restore_all()`` constructs for a previously-unloaded
    agent (crash-recovery path, not a caller-driven ``get_or_load``) must have
    its FULL pre-existing ``history.jsonl`` transcript in ``self.history`` —
    not partial, not empty — the moment ``restore_all()`` returns."""
    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / ".reyn" / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    AgentProfile.new(AGENT, role="").save(agent_dir)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── run 1: write a real multi-turn transcript + one outstanding
    # intervention (so restore_all's non-empty-snapshot gate actually
    # constructs a session for this agent on the next "restart"). ──
    reg1 = _make_registry(tmp_path, wal)
    s1 = reg1.get_or_load(AGENT)
    turns = [
        ChatMessage(role="user", content="turn one"),
        ChatMessage(role="assistant", content="reply one"),
        ChatMessage(role="user", content="turn two"),
        ChatMessage(role="assistant", content="reply two"),
        ChatMessage(role="user", content="turn three"),
    ]
    for msg in turns:
        s1._append_history(msg)
    await s1.journal.record_intervention_dispatched(
        intervention_id="iv1", iv_dict=_iv_dict("iv1"),
    )
    await s1.journal.flush()
    state_log1 = reg1.state_log
    assert state_log1 is not None
    await state_log1.aclose()

    # sanity: the transcript is durable on disk before "restart".
    on_disk = [ln for ln in s1.history_path.read_text().splitlines() if ln.strip()]
    assert len(on_disk) == len(turns)

    # ── "restart": a FRESH registry (nothing loaded yet) whose ONLY entry
    # point into this agent is restore_all() — never a direct get_or_load
    # the test drives. This is the actual cold-start shape #3137 asked about. ──
    reg2 = _make_registry(tmp_path, wal)
    await reg2.restore_all()

    s2 = reg2.get_session(AGENT)
    assert s2 is not None, (
        "restore_all() must have constructed the session (non-empty "
        "outstanding_interventions from run 1)"
    )
    assert s2 is not s1, "sanity: this is a genuinely fresh in-memory session"

    # ★ the completeness assertion: self.history already has EVERY turn from
    # the pre-existing transcript, not a partial prefix/suffix and not empty —
    # observed at the earliest possible point (restore_all already returned;
    # no consumer has driven a further load_history() call).
    assert [m.content for m in s2.history] == [m.content for m in turns], (
        "restore_all()-constructed session must see the FULL history.jsonl "
        "transcript, not a partial/empty one"
    )

    await reg2.state_log.aclose()
