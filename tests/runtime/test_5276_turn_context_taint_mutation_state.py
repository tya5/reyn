"""Tier 2: #5276 — moving the ``turn`` rung's untrusted-taint detection from
the READ side (a full ``metas_have_untrusted`` re-scan on every call to
``Session._ephemeral_contextual_for_turn``, owner-measured py-spy: ~60/sec
from the status panel, live gate and Tool tab combined) to the MUTATION
side (``Session._untrusted_taint_active``, maintained incrementally by
``Session._append_history`` and re-derived in full only at the rare events
that can retroactively change it — a compaction watermark advance, or a
wholesale ``self.history`` replacement that bypasses ``_append_history``
entirely: ``load_history`` / ``restore_state``).

This file does NOT re-litigate the engage/lift transition-audit-event
properties #5282 already pins (``test_5282_ephemeral_narrowing_audit_event.py``,
whose own two mutating helpers were updated by this same PR to drive the new
mutation-hook via ``_append_history`` rather than a bare ``self.history``
append/reassignment). It pins the properties #5276 itself adds: an ordinary
append never mistakenly flips the taint on (deny-side pin), the transitions
this file exercises are not vacuous (they actually change the public denial
set), and — the architect's own explicit acceptance criterion for this
design — the incrementally-maintained state matches a fresh from-scratch
re-derivation after the two paths that replace history wholesale.

Driven entirely through the PUBLIC read surface
(``Session.capability_visibility_state()`` — mirrors ``test_3380_tool_tab_
ephemeral_narrowing.py`` and ``test_5282_ephemeral_narrowing_audit_event.py``'s
own harnesses), never the private ``_untrusted_taint_active`` field directly,
per this repo's "a test must not depend on private state" testing policy.

Real ``Session`` throughout — no mock/stand-in for the collaborator under
test.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on

# Same witness tool test_3380/test_5282 use — denied by the built-in
# ``_untrusted`` profile once the turn-context narrowing engages.
_UNTRUSTED_DENIED_TOOL = "run_prompt"


def _denied_by_turn_context(s: Session) -> "set[str]":
    state = s.capability_visibility_state()
    return {i["name"] for i in state["denied_by_turn_context"] if i["kind"] == "tool"}


def test_ordinary_appends_never_flip_the_taint_on(tmp_path: Path) -> None:
    """Tier 2: deny-side pin — the O(1) append-time check must discriminate
    on the entry's OWN meta, not fire on "any append happened" or "any
    role/kind of entry". A run of ordinary user/assistant/tool entries none
    of which carry the ``external_source`` marker must never engage
    narrowing, however many of them land."""
    s = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    for i in range(10):
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))
        s._append_history(ChatMessage(role="assistant", content=f"reply {i}"))
        s._append_history(ChatMessage(
            role="tool", content=f"result {i}", tool_call_id=f"tc{i}", name="read_file",
        ))
    assert _denied_by_turn_context(s) == set(), (
        "10 rounds of untainted appends must never engage the turn-context "
        "narrowing — the O(1) check is keyed to the untrusted marker, not "
        "to append activity itself"
    )

    # Control arm: the SAME session, the same append path, now DOES carry
    # the marker — proves the assertion above was not vacuously true
    # because narrowing can never engage in this session at all.
    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(s), (
        "control: this session's narrowing IS reachable — the untainted run "
        "above genuinely tested something, not a session where narrowing "
        "can never engage at all"
    )


def test_the_engage_and_lift_transitions_are_not_vacuous(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — the deny-set genuinely changes shape across the
    engage/lift transitions this design's mutation hook drives, rather than
    the public read staying constant (e.g. always empty, or always full)
    regardless of the taint state."""
    s = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    before = _denied_by_turn_context(s)

    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    tainted_seq = s.history[-1].seq
    engaged = _denied_by_turn_context(s)

    s._append_history(ChatMessage(
        role="summary", content="summarised",
        meta={
            "structured": {}, "covers_from_seq": 1,
            "covers_through_seq": tainted_seq,
        },
    ))
    lifted = _denied_by_turn_context(s)

    assert before == set(), "sanity: nothing denied before any taint"
    assert engaged != before and _UNTRUSTED_DENIED_TOOL in engaged, (
        "engaging the taint must genuinely add denials, not leave the "
        "public read unchanged"
    )
    assert lifted == before, (
        "a real compaction watermark advance past the tainted entry must "
        "restore the exact pre-taint deny-set, not merely 'some other "
        "non-empty state'"
    )


def _reconstruct_from_disk(tmp_path: Path) -> Session:
    """A cold Session over the SAME on-disk history/workspace another
    Session instance already wrote — the real crash-recovery shape
    ``load_history`` exists for. Never shares the writer's in-memory
    ``self.history`` or ``self._untrusted_taint_active``."""
    s = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    s.load_history()
    return s


def test_cold_start_reload_reconstructs_the_taint_state_fast_path(
    tmp_path: Path,
) -> None:
    """Tier 2: ``load_history``'s FAST path (a real ``covers_through_seq``
    summary exists, so the bounded tail read is safe) populates
    ``self.history`` via a bare ``.append`` that bypasses
    ``_append_history``'s own incremental hook entirely — this is the one
    path #5276's design has to explicitly re-derive fresh at the end of, or
    a restarted session would silently start un-narrowed despite genuinely
    tainted content in its reloaded active window."""
    from reyn.runtime.session import _HISTORY_HYDRATE_MIN_LINES

    writer = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    writer._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    # A summary so the fast path's own peek finds a real seq, plus enough
    # post-summary turns to clear the hydrate floor — the exact shape
    # test_4387_bounded_history_hydration.py's own fast-path test uses.
    summary_seq = writer.history[-1].seq
    writer._append_history(ChatMessage(
        role="summary", content="unrelated fold",
        meta={"structured": {}, "covers_through_seq": summary_seq},
    ))
    for i in range(_HISTORY_HYDRATE_MIN_LINES + 5):
        writer._append_history(ChatMessage(role="user", content=f"turn {i}"))
    # Taint the reloaded ACTIVE window itself (after the watermark), so the
    # reload's own re-derivation has something genuine to find.
    writer._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> again", meta={"external_source": True})
    )

    reloaded = _reconstruct_from_disk(tmp_path)

    assert reloaded.history[0].role == "summary", (
        "sanity: this must exercise the FAST path, not the fallback"
    )
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(reloaded), (
        "a cold reload via the fast path must reconstruct the taint state "
        "from the reloaded active window, not start un-narrowed just "
        "because _append_history's own incremental hook never ran on it"
    )


def test_cold_start_reload_reconstructs_the_taint_state_fallback_path(
    tmp_path: Path,
) -> None:
    """Tier 2: the counterpart for ``load_history``'s FALLBACK path (full
    forward read + full scan, taken when the last on-disk entry has no
    real ``seq`` — legacy/untrusted-peek-failure shape)."""
    writer = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    writer._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    # Force the fallback: hand-append a trailing line with seq==0, the same
    # technique test_4387_bounded_history_hydration.py's own fallback test
    # uses to defeat the fast-path's last-line peek.
    import json
    with writer.history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "legacy tail", "seq": 0}) + "\n")

    reloaded = _reconstruct_from_disk(tmp_path)

    assert reloaded.history[-1].content == "legacy tail", (
        "sanity: this must exercise the FALLBACK path, not the fast one"
    )
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(reloaded), (
        "a cold reload via the fallback path must also reconstruct the "
        "taint state, not just the fast path"
    )


def test_restore_state_after_a_cold_reload_matches_fresh_rederivation(
    tmp_path: Path,
) -> None:
    """Tier 2: architect's own explicit acceptance criterion for this
    design — after a full recovery cycle (cold reload from disk, then
    ``restore_state`` from a real recovered snapshot, the actual
    crash-recovery sequence), the taint state must still agree with what a
    from-scratch derivation would say. ``restore_state`` does not itself
    touch ``self.history`` today, so this also guards the OTHER half: that
    ``load_history``'s own re-derivation (pinned above) is not silently
    undone or left stale by whatever ``restore_state`` does afterwards."""
    from reyn.core.events.agent_snapshot import AgentSnapshot

    writer = make_session(
        agent_name="alpha", workspace_base_dir=tmp_path, safety=narrowing_on(),
    )
    writer._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(writer), (
        "sanity: the writer session itself is tainted before recovery"
    )

    reloaded = _reconstruct_from_disk(tmp_path)
    before_restore = _denied_by_turn_context(reloaded)

    reloaded.restore_state(AgentSnapshot.empty("alpha"))

    assert _denied_by_turn_context(reloaded) == before_restore, (
        "restore_state must not change the taint-derived deny-set from "
        "what the cold reload already established -- the incremental "
        "state must still agree with a fresh re-derivation after a full "
        "recovery cycle, not just after the reload alone"
    )
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(reloaded), (
        "sanity: still genuinely tainted after restore_state, not "
        "vacuously equal because both sides are empty"
    )
