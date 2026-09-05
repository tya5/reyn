"""Tier 2: #3380 — the Tool tab reflects the EPHEMERAL per-turn narrowing, and says
that it is the ephemeral one.

#3379 made advertisement and enforcement read one effective source, so "visible but
not callable" is gone. What survived is the diagnostic half: the tab showed only the
ENVELOPE contextual (``resolved_profile_for``), while the narrowing that actually
denied the owner's ``exec`` is the ephemeral ``_untrusted`` profile, composed by
``Session._effective_contextual_for_turn``. An operator asking the tab "what can this
agent do right now" got an answer to "what is configured in general" — a surface that
looks authoritative and answers a different question.

★ **This is not a second notion of "what is narrowed".** The tab reads
``Session._ephemeral_contextual_for_turn()`` — the exact term
``_effective_contextual_for_turn`` composes for the live gate — so the tab and the
gate cannot drift. Computing a parallel one is the defect class #3378 closed.

★ **Freshness is by derivation, not by promise.** The taint is re-read from the live
``history`` on every call (``metas_have_untrusted``), never latched at turn start, and
the open drawer pane is rebuilt from a fresh snapshot on every frame (#3338). So the
row is as-of-now and self-clears when the untrusted entry compacts out — pinned by
``test_turn_context_denial_self_clears_when_the_taint_leaves_the_context``. That is
why it does not need (and must not carry) an "as of turn N" caveat.
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
from tests._support.untrusted_narrowing import narrowing_on

# In the census this host composes: denied by the built-in ``_untrusted`` profile
# (a re-delegation surface), and present in the un-narrowed catalog — the control
# arm below asserts that second half rather than assuming it.
#
# The BARE spelling, though the profile's ``_FLOORED_QUALIFIED`` declares the
# qualified ``delegate_to_agent``: #3428 stopped advertising the qualified
# spelling of an operation the base tools already name, so the qualified one is no
# longer in the census this reads. The deny still covers both forms — the profile
# derives every invocable spelling from ``unwrapped_tool_name`` (#2111) — which is
# what ``test_both_spellings_of_a_floored_tool_stay_denied`` below keeps true, so
# this constant naming one of them is a choice of witness and not a narrowing of
# the claim.
_UNTRUSTED_DENIED_TOOL = "run_prompt"


def _session(tmp_path: Path) -> Session:
    # #3501: the ephemeral narrowing is opt-in; a test whose subject is how it
    # renders has to turn it on.
    #
    # #3615: a REAL registry back-reference (even with nothing bound — no topology
    # profile for "alpha") is required for ``capability_visibility_state``'s ENVELOPE
    # axis to be genuinely DETERMINED rather than merely defaulted. Before #3615, a
    # registry-less session's un-narrowed tools read as "authorized" by the same
    # allow-everything default the fix closes off (``ContextualLayer(None)`` is (top))
    # — this file's own control arm asserted that "authorized" without ever having
    # resolved an envelope. This module's subject is the TURN-CONTEXT axis, which is
    # independent of envelope resolution (composes only ``ephemeral_contextual``), so
    # giving it a real, resolvable (if unbound) envelope isolates that from the defect
    # #3615 fixes, rather than accidentally exercising it.
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / "snap.json",
            safety=narrowing_on(),
            registry=holder.get("reg"),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg.get_or_load("alpha")


def _mark_untrusted(s: Session) -> None:
    """The #1862 marker the real producer stamps on an external peer answer —
    the same ``external_source`` meta ``metas_have_untrusted`` scans for.

    #5276: goes through ``_append_history`` — the real mutation chokepoint
    that maintains ``Session._untrusted_taint_active`` incrementally — not a
    bare ``s.history.append``, which the incremental hook never observes."""
    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )


def _tools(state: dict, key: str) -> "set[str]":
    return {i["name"] for i in state[key] if i["kind"] == "tool"}


def test_ephemeral_narrowing_reaches_the_tool_tab_as_its_own_reason(tmp_path) -> None:
    """Tier 2: while untrusted content is live, a tool the ephemeral ``_untrusted``
    profile denies leaves ``authorized`` and appears under ``denied_by_turn_context``.

    The control arm (same session, before the taint) asserts the tool IS authorized,
    so its later absence is caused by the narrowing rather than by its never having
    been in this host's census.
    """
    s = _session(tmp_path)

    before = s.capability_visibility_state()
    assert _UNTRUSTED_DENIED_TOOL in _tools(before, "authorized"), (
        "control arm: the target is not in the un-narrowed census — the gate below "
        "would pass vacuously"
    )
    assert not _tools(before, "denied_by_turn_context"), (
        "an untainted context must report no per-turn narrowing"
    )

    _mark_untrusted(s)
    after = s.capability_visibility_state()

    assert _UNTRUSTED_DENIED_TOOL in _tools(after, "denied_by_turn_context"), (
        "the ephemeral narrowing that denies the tool at the live gate is still "
        "invisible to the Tool tab — the operator can only discover it by calling"
    )
    assert _UNTRUSTED_DENIED_TOOL not in _tools(after, "authorized"), (
        "a tool the live gate rejects this turn must not be offered as available"
    )
    assert _UNTRUSTED_DENIED_TOOL not in _tools(after, "denied_by_envelope"), (
        "a transient per-turn denial reported as an envelope denial states a lasting "
        "fact about a condition that clears itself"
    )
    # The narrowing is targeted, not a wipe: a read tool the profile allows stays put.
    assert "read_file" in _tools(after, "authorized")


def test_turn_context_denial_self_clears_when_the_taint_leaves_the_context(
    tmp_path,
) -> None:
    """Tier 2: the row is derived from the LIVE context at read time, so it disappears
    once the untrusted entry compacts out — the freshness property that makes showing a
    per-turn value in a status pane honest (the #3338 lesson: if you show it, guarantee
    its liveness)."""
    s = _session(tmp_path)
    _mark_untrusted(s)
    assert _UNTRUSTED_DENIED_TOOL in _tools(
        s.capability_visibility_state(), "denied_by_turn_context"
    )

    # #5276: the untrusted entry compacting out of the active context — a
    # real compaction watermark advance via _append_history (same shape
    # test_narrowing_self_clears_when_a_real_compaction_covers_the_taint
    # below already uses), not a raw self.history reassignment the
    # incremental taint hook never observes.
    tainted_seq = next(
        m.seq for m in s.history if (m.meta or {}).get("external_source")
    )
    s._append_history(
        ChatMessage(
            role="summary", content="summarised",
            meta={
                "structured": {}, "covers_from_seq": 1,
                "covers_through_seq": tainted_seq,
            },
        )
    )

    cleared = s.capability_visibility_state()
    assert not _tools(cleared, "denied_by_turn_context"), (
        "the tab still reports a per-turn denial whose cause is gone — a stale value "
        "presented as current"
    )
    assert _UNTRUSTED_DENIED_TOOL in _tools(cleared, "authorized")


def test_narrowing_self_clears_when_a_real_compaction_covers_the_taint(
    tmp_path,
) -> None:
    """Tier 2: #4387 Phase A — ``_ephemeral_contextual_for_turn`` bounds its
    ``metas_have_untrusted`` scan to ``seq > self._compaction_watermark()``
    instead of scanning all of ``self.history`` forever. Proven against a
    REAL compaction watermark (a ``role="summary"`` entry appended via
    ``_append_history`` with ``covers_through_seq`` at the tainted entry's
    own seq — the exact shape ``CompactionController``'s
    ``make_summary_message`` produces), not by physically removing the
    entry from ``self.history`` the way
    ``test_turn_context_denial_self_clears_when_the_taint_leaves_the_context``
    above does. The tainted raw entry stays PRESENT in ``self.history``
    throughout this test; only the watermark moves past its seq — so a
    pre-fix scan (unbounded) would still see it and stay narrowed forever,
    while the bounded scan correctly treats it as compacted out.
    """
    s = _session(tmp_path)
    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )
    tainted_seq = s.history[-1].seq
    assert tainted_seq > 0, "a real _append_history call must assign a coordinate"
    assert _UNTRUSTED_DENIED_TOOL in _tools(
        s.capability_visibility_state(), "denied_by_turn_context"
    ), "control arm: the taint must engage narrowing before any compaction"

    s._append_history(
        ChatMessage(
            role="summary", content="summarised",
            meta={
                "structured": {}, "covers_from_seq": 1,
                "covers_through_seq": tainted_seq,
            },
        )
    )

    cleared = s.capability_visibility_state()
    assert not _tools(cleared, "denied_by_turn_context"), (
        "a compaction watermark covering the tainted entry's seq must clear the "
        "narrowing even though the raw entry is still physically present in "
        "self.history"
    )
    assert _UNTRUSTED_DENIED_TOOL in _tools(cleared, "authorized")
    assert any((m.meta or {}).get("external_source") for m in s.history), (
        "sanity: the tainted entry is still physically present — the clearing "
        "above is due to watermark bounding, not removal"
    )


def test_every_floored_tool_stays_denied_at_the_live_gate(tmp_path) -> None:
    """Tier 2: the ephemeral floor rejects EVERY name it floors at the live gate,
    not just the one the constant above happens to witness.

    #3429 changed what "every" ranges over. This test used to enumerate the
    witness tool's two SPELLINGS (``all_invocable_forms``), because a floor
    written in one spelling left the other reachable and the census-chosen
    witness must not become the only spelling anything checks. There is one
    spelling now, so the analogous over-narrow-witness risk is the TOOL: the
    arm enumerates the whole floor rather than re-checking the constant."""
    from reyn.security.permissions.capability_profile import _BUILTIN_UNTRUSTED_DENY
    from reyn.security.permissions.effective import tool_contextually_denied

    s = _session(tmp_path)
    _mark_untrusted(s)
    effective = s._effective_contextual_for_turn()

    assert _UNTRUSTED_DENIED_TOOL in _BUILTIN_UNTRUSTED_DENY, (
        "the witness constant is not in the floor, so this arm would not be "
        "checking the thing the rest of the file is about"
    )
    for name in sorted(_BUILTIN_UNTRUSTED_DENY):
        assert tool_contextually_denied(effective, name), (
            f"{name} reaches the live gate while untrusted content is in context"
        )


def test_the_tab_reads_the_same_ephemeral_term_the_live_gate_composes(tmp_path) -> None:
    """Tier 2: the tab's per-turn denial set is not a parallel computation — every tool
    it reports is one the composed per-turn contextual actually rejects at
    ``tool_contextually_denied``, the single seam every dispatch path calls."""
    from reyn.security.permissions.effective import tool_contextually_denied

    s = _session(tmp_path)
    _mark_untrusted(s)
    effective = s._effective_contextual_for_turn()

    reported = _tools(s.capability_visibility_state(), "denied_by_turn_context")
    assert reported, "nothing was reported — the agreement below would be vacuous"
    for name in reported:
        assert tool_contextually_denied(effective, name), (
            f"{name} is shown as denied this turn but the live gate would allow it"
        )


def _bind_topology_profile(root: Path, *, member: str, body: str) -> None:
    """A real topology binding + the capability_profile it names — the #1827 envelope
    narrowing source, resolved by ``AgentRegistry.resolved_profile_for``."""
    td = root / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / "t.yaml").write_text(
        f"name: t\nkind: network\nmembers: [{member}, peer]\n"
        f"profiles:\n  {member}: narrowed\n",
        encoding="utf-8",
    )
    pd = root / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "narrowed.yaml").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_an_envelope_denial_keeps_its_durable_reason_while_tainted(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: when BOTH narrowings deny a tool, the tab reports the envelope one.

    That is the actionable answer: the envelope denial outlives the taint, so telling
    the operator "this clears when the untrusted content compacts out" would be false.
    Driven from a real ``AgentRegistry`` + real ``Session`` with a real topology
    binding, so the two reasons come from genuinely different sources.
    """
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry

    monkeypatch.chdir(tmp_path)
    envelope_denied = "run_prompt"
    _bind_topology_profile(
        tmp_path, member="alice", body=f"name: narrowed\ntool_deny: [{envelope_denied}]\n",
    )

    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name,
            state_log=state_log,
            registry=holder.get("reg"),
            chat_tool_use_scheme="enumerate-all",
            safety=narrowing_on(),
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    )
    session = reg.get_session("alice", sid)
    _mark_untrusted(session)

    state = session.capability_visibility_state()
    assert envelope_denied in _tools(state, "denied_by_envelope"), (
        "a durable envelope denial was re-attributed to the transient per-turn axis"
    )
    assert envelope_denied not in _tools(state, "denied_by_turn_context")
    # ``spawn_agent`` is in the enumerate-all census this arm composes, and the
    # topology profile above denies neither it nor anything but ``run_prompt``
    # (``envelope_denied``) — so ``spawn_agent`` is ephemeral-only here.
    assert "spawn_agent" in _tools(state, "denied_by_turn_context"), (
        "control: a tool denied ONLY by the ephemeral profile still lands on the "
        "per-turn axis, so the split above is a split and not a collapse"
    )


def test_the_status_seam_carries_which_narrowing_denied_the_row(tmp_path) -> None:
    """Tier 2: the reason survives the read model. The renderer cannot distinguish the
    two axes if the projection flattens them into a single ``denied`` flag."""
    from reyn.interfaces.repl.status import _session_visibility_items

    s = _session(tmp_path)
    _mark_untrusted(s)

    items = _session_visibility_items(s)
    assert items is not None
    row = next(i for i in items if i["name"] == _UNTRUSTED_DENIED_TOOL)
    assert row["denied"] is True
    assert row["denied_reason"] == "turn_context"
    assert row["on"] is False, "a non-flippable row must not read as merely toggled off"


def _vis_snapshot(items) -> dict:
    return {"visibility_items": items, "mcp_servers": [], "skills": []}


def test_the_tab_says_the_denial_is_the_per_turn_one_not_the_profile() -> None:
    """Tier 2: the two non-flippable reasons render distinguishably, and the per-turn
    row names its condition rather than a bare "denied".

    Without this the operator reads "denied by capability profile" and goes looking for
    a profile to edit that does not deny it — the same wrong-question failure #3380 is
    about, one level down.
    """
    from reyn.interfaces.inline.textual_chat.chrome import _visibility_pane_entries

    entries = _visibility_pane_entries(
        _vis_snapshot([
            {"kind": "tool", "name": "off_tool", "on": False, "denied": False,
             "denied_reason": None},
            {"kind": "tool", "name": "env_tool", "on": False, "denied": True,
             "denied_reason": "envelope"},
            {"kind": "tool", "name": "turn_tool", "on": False, "denied": True,
             "denied_reason": "turn_context"},
        ]),
        "tool", None,
    )
    rows = {
        name: (row, slash)
        for row, slash in entries
        for name in ("off_tool", "env_tool", "turn_tool")
        if name in row
    }
    off_row, off_slash = rows["off_tool"]
    env_row, env_slash = rows["env_tool"]
    turn_row, turn_slash = rows["turn_tool"]

    assert off_slash, "a /visibility-off row stays user-flippable"
    assert not env_slash and not turn_slash, (
        "neither non-flippable reason may offer a /visibility toggle that cannot work"
    )
    # Distinguishability with the name stripped: no two of the three states may
    # render identically (a shared rendering sends the operator after the wrong fix).
    off_shape = off_row.replace("off_tool", "")
    env_shape = env_row.replace("env_tool", "")
    turn_shape = turn_row.replace("turn_tool", "")
    assert env_shape != off_shape, "an envelope denial reads as a flippable /visibility off"
    assert turn_shape != off_shape, "a per-turn denial reads as a flippable /visibility off"
    assert turn_shape != env_shape, (
        "the per-turn denial reads as a durable capability-profile denial — the "
        "operator would look for a profile to edit that does not deny it"
    )
    assert "untrusted" in turn_row, (
        "the per-turn row does not name the condition that caused it, so the operator "
        "cannot tell what would lift it"
    )
