"""Tier 2: /rewind slash command — time-travel dispatch (ADR-0038 1f, D8, #3987).

Two forms:
- ``/rewind``     → publishes a command-UI request (``{"kind": "rewind",
  "points": [...], "branches": [...]}``) the front-end renders as a picker,
  plus a ``__rewind_list__`` text fallback. #3987 ② widened the request:
  ``points`` now includes abandoned-branch checkpoints
  (``include_abandoned=True``) and ``branches`` carries ``list_branches()``'s
  own tree, converted from ``Branch`` dataclasses to plain dicts at this one
  seam (the payload crosses a transport boundary).
- ``/rewind <N>`` → calls the unified ``AgentRegistry.checkout(N)`` (D8: undo
  for a live-branch seq, fork-switch for a dead-branch one) and surfaces the
  result (scriptable + TUI-free).

Real ``AgentRegistry`` + ``StateLog`` throughout, including for the bare
``/rewind`` path (#3987's own review point: a stub without ``list_branches``
silently diverges from the real registry the moment ITS shape changes,
which is exactly what broke here once) — a light session stub only captures
the outbox, never stands in for the registry.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.slash import slash_ctx


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _record_gen(reg: AgentRegistry, name: str, seq: int) -> None:
    """Persist a generation for ``name`` cut at boundary ``seq`` — same
    convention ``test_registry_list_rewind_points_1f.py`` uses, reproduced
    rather than imported across test files."""
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = seq
    reg._store_for(name).record(snap)


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session.outbox_msgs)


class _CapturingSession:
    """Minimal session: captures outbox messages, holds an optional registry."""

    def __init__(self, registry=None) -> None:
        self.agent_name = "test"
        # #5769 stage 3 ④: ``rewind_cmd`` now reads this PUBLIC identity (the
        # same shape ``Session.session_id`` exposes) to build its default
        # session-local ``checkout`` scope.
        self.session_id = "main"
        self._registry = registry
        self.outbox_msgs: list = []
        self._pending_command_ui = None

    async def _put_outbox(self, msg) -> None:
        self.outbox_msgs.append(msg)

    @property
    def pending_command_ui(self):
        return self._pending_command_ui

    def set_pending_command_ui(self, payload) -> None:
        self._pending_command_ui = payload


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


def _handler():
    cmd = REGISTRY.get("rewind")
    assert cmd is not None
    return cmd


def test_rewind_is_registered() -> None:
    """Tier 2: /rewind is in the registry with the seq usage hint."""
    cmd = _handler()
    assert "seq" in cmd.usage.lower()


@pytest.mark.asyncio
async def test_bare_rewind_opens_picker_via_command_ui_and_text_fallback(tmp_path) -> None:
    """Tier 2: bare /rewind publishes a command-UI request (the inline region
    selector) AND a __rewind_list__ text fallback (the --cui path).

    #3987 ②: the request now ALSO carries ``branches`` — a real
    ``AgentRegistry`` here, not a stub, is what caught the review point this
    test used to miss (a stub without ``list_branches`` silently diverges the
    moment the real seam changes). ``points`` are sent in ASCENDING seq order
    (the tree builder does its own ordering); the ``--cui`` fallback still
    reverses for display.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("step_completed", run_id="r1", step="s")
    for s in (s1, s2):
        _record_gen(reg, "alpha", s)

    session = _CapturingSession(registry=reg)
    await _handler().handler(_ctx(session), "")

    payload = session.pending_command_ui
    assert payload["kind"] == "rewind"
    assert [p["seq"] for p in payload["points"]] == [s1, s2], (
        "ascending — the tree builder, not this handler, decides display order"
    )
    assert payload["branches"] == [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": s2,
         "parent_branch_id": None, "is_active": True},
    ]
    assert [m.kind for m in session.outbox_msgs] == ["__rewind_list__"]
    assert "seq 42" not in session.outbox_msgs[0].text  # sanity: no stale literal
    assert f"seq {s2}" in session.outbox_msgs[0].text, (
        "the --cui fallback must list the newest checkpoint, and its own "
        "reverse must have put it first"
    )


@pytest.mark.asyncio
async def test_bare_rewind_with_no_checkpoints_replies(tmp_path) -> None:
    """Tier 2: bare /rewind with no rewind points → a clear message, no picker.

    A real, freshly-constructed ``AgentRegistry`` over an empty WAL — no
    generations recorded — is genuinely empty here (measured:
    ``list_rewind_points`` and ``list_branches`` both return ``[]`` with
    nothing appended), so this needs no stub at all.
    """
    reg = _make_registry(tmp_path)
    session = _CapturingSession(registry=reg)
    await _handler().handler(_ctx(session), "")
    assert session.pending_command_ui is None
    assert any("no earlier checkpoints" in m.text for m in session.outbox_msgs)


@pytest.mark.asyncio
async def test_bare_rewind_payload_feeds_the_real_tree_builder(tmp_path) -> None:
    """Tier 2: #3987 ② composite witness (lead-coder review point) — a real
    ``AgentRegistry`` with an ACTUAL fork, through the real slash handler,
    into the real ``build_branch_tree_rows``. Neither end is a hand-written
    dict: this is the one test that would catch a shape mismatch between what
    the handler sends and what the tree builder expects, which a test that
    only checks one side or the other cannot.
    """
    from reyn.core.events.snapshot_generations import rewind as _rewind_record
    from reyn.interfaces.common.branch_tree import (
        ROW_CHECKPOINT,
        ROW_HEADER,
        build_branch_tree_rows,
    )

    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")
    s3 = await log.append("inbox_consume", target="alpha", msg_id="m3")
    for s in (s1, s2, s3):
        _record_gen(reg, "alpha", s)
    # A genuine fork: rewind to s1, stranding s2/s3 on an abandoned branch.
    await _rewind_record(log, target_n=s1)
    s4 = await log.append("inbox_consume", target="alpha", msg_id="m4")
    _record_gen(reg, "alpha", s4)

    session = _CapturingSession(registry=reg)
    await _handler().handler(_ctx(session), "")
    payload = session.pending_command_ui
    assert payload is not None, "a fork must still produce a non-empty picker"
    assert any(not b["is_active"] for b in payload["branches"]), (
        "this scenario must actually produce an abandoned branch, or the "
        "rest of this test is vacuous"
    )

    rows = build_branch_tree_rows(payload["branches"], payload["points"])
    headers = [r for r in rows if r["row"] == ROW_HEADER]
    checkpoints = [r for r in rows if r["row"] == ROW_CHECKPOINT]
    assert {h["branch_id"] for h in headers} == {b["branch_id"] for b in payload["branches"]}, (
        f"every real branch must get its own header row: {rows!r}"
    )
    assert any(not h["is_active"] for h in headers)
    assert {c["seq"] for c in checkpoints} == {s1, s2, s3, s4}, (
        f"every real checkpoint must round-trip through the payload into a "
        f"row: {rows!r}"
    )


@pytest.mark.asyncio
async def test_rewind_with_seq_invokes_checkout(tmp_path) -> None:
    """Tier 2: /rewind <N> calls AgentRegistry.checkout(N) and reports success.

    The slash uses the SAME unified checkout the picker dispatches (D8) — no
    sibling-gap. Drives a real registry: WAL seq 1 + 2 appended, checkout to
    seq 1. The reply names the target seq; the WAL grows a reset-record.
    """
    reg = _make_registry(tmp_path)
    log = reg.state_log
    await log.append("inbox_put", target="alpha", msg_id="a", msg_kind="user", payload={})
    await log.append("inbox_put", target="alpha", msg_id="b", msg_kind="user", payload={})
    head_before = log.current_seq

    session = _CapturingSession(registry=reg)
    await _handler().handler(_ctx(session), "1")

    # A reset-record was appended (checkout ran).
    assert log.current_seq > head_before
    # The reply names the target seq.
    texts = [getattr(m, "text", "") for m in session.outbox_msgs]
    assert any("seq 1" in t and "checked out" in t for t in texts)


@pytest.mark.asyncio
async def test_rewind_non_integer_arg_errors() -> None:
    """Tier 2: /rewind <non-int> surfaces a decision-enabling error, no crash."""
    session = _CapturingSession()
    await _handler().handler(_ctx(session), "abc")
    assert [m.kind for m in session.outbox_msgs] == ["error"]
    assert "abc" in session.outbox_msgs[0].text


@pytest.mark.asyncio
async def test_rewind_seq_without_registry_errors() -> None:
    """Tier 2: /rewind <N> with no registry attached → error (not a crash)."""
    session = _CapturingSession(registry=None)
    await _handler().handler(_ctx(session), "5")
    assert [m.kind for m in session.outbox_msgs] == ["error"]


@pytest.mark.asyncio
async def test_rewind_abandoned_target_checks_out_fork_switch(tmp_path) -> None:
    """Tier 2: /rewind <N> into an abandoned branch now SUCCEEDS (fork-switch).

    Contract reversal from the rewind_to era: rewind_to rejected an abandoned
    target (RewindIntoAbandonedError); the unified checkout (D8) has no
    active-target guard, so checking out a dead-branch seq revives that lineage
    — a fork-switch, not an error. Pins the new behaviour decisively.
    """
    from reyn.core.events.snapshot_generations import rewind as _rewind_record
    reg = _make_registry(tmp_path)
    log = reg.state_log
    await log.append("inbox_put", target="alpha", msg_id="a", msg_kind="user", payload={})
    await log.append("inbox_put", target="alpha", msg_id="b", msg_kind="user", payload={})
    await _rewind_record(log, target_n=1)  # abandons seq 2 (dead branch)
    head_before = log.current_seq

    session = _CapturingSession(registry=reg)
    await _handler().handler(_ctx(session), "2")  # checkout the dead-branch seq

    # No error — the dead-branch checkout succeeded (fork-switch).
    assert "error" not in [m.kind for m in session.outbox_msgs]
    assert log.current_seq > head_before  # a reset-record reviving seq 2's lineage
    texts = [getattr(m, "text", "") for m in session.outbox_msgs]
    assert any("checked out" in t for t in texts)
