"""Tier 2: opt-in per-step (act-turn) workspace capture (#1560 PR-1, capture side).

`time_travel.act_turn_capture: true` registers a generic post-append observer on
the WAL; on each `step_completed` it captures a `write-tree` snapshot into the
op-content-log keyed by the step seq (= `CommittedStep.seq`). The mechanism's
three load-bearing properties are pinned here:

- **P7**: the `StateLog` observer is workspace-agnostic — it passes only
  `(kind, seq, fields)`; all workspace knowledge lives in the registry callback.
- **Never-block**: a failing observer can never fail/corrupt the WAL append
  (the entry is durable pre-observer; failures are swallowed).
- **Gating**: capture only when `act_turn_capture` is on (else the callback is
  never even registered — zero per-append cost) AND `workspace_store` is present
  (the Tier-1 #1584 gate) AND `kind == step_completed`.

Restore (read the log → `read-tree`) is PR-2. Real StateLog + AgentRegistry +
shadow git; no mocks.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.config import TimeTravelConfig, _build_time_travel_config
from reyn.events.state_log import StateLog

# ── config ─────────────────────────────────────────────────────────────────


def test_config_act_turn_capture_parse() -> None:
    """Tier 2: act_turn_capture parses the NON-default (True) + defaults False;
    non-bool is a loud error. workspace_capture default unaffected."""
    assert _build_time_travel_config({"act_turn_capture": True}).act_turn_capture is True
    assert _build_time_travel_config(None).act_turn_capture is False
    assert _build_time_travel_config({}).act_turn_capture is False
    assert TimeTravelConfig().act_turn_capture is False
    # both keys independently parsed
    cfg = _build_time_travel_config({"workspace_capture": False, "act_turn_capture": True})
    assert cfg.workspace_capture is False and cfg.act_turn_capture is True
    with pytest.raises(ValueError):
        _build_time_travel_config({"act_turn_capture": "yes"})


# ── StateLog generic observer (P7 + never-block) ───────────────────────────


@pytest.mark.asyncio
async def test_statelog_observer_receives_wal_vocab_only(tmp_path) -> None:
    """Tier 2: P7 — a registered observer is called with only `(kind, seq, fields)`,
    pure WAL vocabulary, no workspace coupling in StateLog."""
    log = StateLog(tmp_path / "wal.jsonl")
    seen: list = []

    async def cb(kind, seq, fields):
        seen.append((kind, seq, fields))

    log.register_post_append(cb)
    seq = await log.append("inbox_consume", target="a", msg_id="m1")
    assert seen == [("inbox_consume", seq, {"target": "a", "msg_id": "m1"})]


@pytest.mark.asyncio
async def test_statelog_observer_failure_never_blocks_append(tmp_path) -> None:
    """Tier 2: never-block — a raising observer cannot fail or corrupt the append;
    the returned seq is correct and the entry is durably readable."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def boom(kind, seq, fields):
        raise RuntimeError("capture exploded")

    log.register_post_append(boom)
    seq = await log.append("inbox_consume", target="a", msg_id="m1")
    assert seq == 1                                          # append succeeded
    persisted = [e["seq"] for e in log.iter_from(1)]
    assert seq in persisted                                 # entry persisted intact


@pytest.mark.asyncio
async def test_statelog_no_observer_is_default(tmp_path) -> None:
    """Tier 2: with no observer registered the append path is unchanged."""
    log = StateLog(tmp_path / "wal.jsonl")
    s1 = await log.append("inbox_consume", target="a", msg_id="m1")
    s2 = await log.append("inbox_consume", target="a", msg_id="m2")
    assert (s1, s2) == (1, 2)
    assert len(list(log.iter_from(1))) == 2


# ── registry capture gating ────────────────────────────────────────────────


def _make_registry(tmp_path: Path, *, act_turn_capture: bool, workspace_capture: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile):  # not exercised by these capture tests
        raise AssertionError("factory must not be called")

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
        workspace_capture=workspace_capture, act_turn_capture=act_turn_capture,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


@pytest.mark.asyncio
async def test_act_turn_off_no_capture(tmp_path) -> None:
    """Tier 2: default off → no op-content-log, and a `step_completed` append
    produces no capture. (The WAL observer is not even registered when off — a
    zero-per-append-cost optimization, visible in the registry constructor; here
    we pin the observable contract: off ⇒ no capture path.)"""
    reg = _make_registry(tmp_path, act_turn_capture=False)
    assert reg.op_content_log is None
    await reg.state_log.append("step_completed", run_id="r1", op_kind="file_write")
    assert reg.op_content_log is None   # still no capture log


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git required for write-tree")
async def test_act_turn_on_captures_tree_per_step(tmp_path) -> None:
    """Tier 2: act_turn_capture on + workspace present → a `step_completed` append
    records a `(op_seq, tree_sha)` op-content-log entry keyed by that seq."""
    reg = _make_registry(tmp_path, act_turn_capture=True)
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")

    seq = await reg.state_log.append("step_completed", run_id="r1", op_kind="file_write")

    captured = {e["op_seq"]: e["tree_sha"] for e in reg.op_content_log.entries()}
    assert seq in captured                                 # the step was captured
    assert captured[seq]                                   # keyed to a real tree object


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git required for write-tree")
async def test_act_turn_on_but_workspace_off_is_noop(tmp_path) -> None:
    """Tier 2: Tier-1 gate — act_turn_capture on but workspace_capture off →
    workspace_store is None → no capture (one switch governs both)."""
    reg = _make_registry(tmp_path, act_turn_capture=True, workspace_capture=False)
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")

    await reg.state_log.append("step_completed", run_id="r1", op_kind="file_write")
    assert reg.op_content_log.entries() == []             # no capture (workspace off)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git required for write-tree")
async def test_non_step_completed_is_not_captured(tmp_path) -> None:
    """Tier 2: only `step_completed` triggers capture — other WAL kinds don't."""
    reg = _make_registry(tmp_path, act_turn_capture=True)
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")

    await reg.state_log.append("step_started", run_id="r1", op_kind="file_write")
    await reg.state_log.append("inbox_consume", target="alpha", msg_id="m1")
    assert reg.op_content_log.entries() == []
