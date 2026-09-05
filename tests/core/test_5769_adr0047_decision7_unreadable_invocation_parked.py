"""Tier 2: ADR-0047 acceptance, decision 7 -- "An item whose owner cannot
be named is skipped observably, not answered globally."

`AgentRegistry._rewake_pipeline_runs` already `logger.warning`s and
`continue`s when a run dir's own `invocation.json` is unreadable/absent
(`load_invocation` returns `None`) -- the mechanism is on `main`. What
was missing (ADR-0047's own remaining blocker, docs/deep-dives/decisions/
0047-session-scoped-rewind.md#decision-7): no test exercised that branch
at all, so replacing the `continue` with a GLOBAL fallback (e.g.
resurrecting the run under a synthetic/default identity instead of
naming its true, unreadable owner) would go green.

This file closes it: a run dir whose invocation.json is unreadable (or
genuinely absent) is left PARKED (never re-woken, no session created for
it, no attempts bumped, no result written) AND the skip is observable
(the existing `logger.warning`, captured via `caplog`) -- both real,
driven witnesses, not an assertion over an empty collection (the run dir
DOES exist on disk; the scan DOES visit it; it is the ONE run dir in the
whole state root, so a false-negative "list is empty because nothing
ran" cannot pass this by accident).

Real `AgentRegistry`/`StateLog` throughout -- no mocks."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import (
    pipeline_run_dir,
    read_resume_attempts,
)
from reyn.runtime.registry import AgentRegistry


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry -- no session factory is ever expected to run in
    this file (a parked run creates no session), so a factory that fails
    loudly if invoked doubles as part of the witness."""

    def _no_factory(profile):
        raise AssertionError(
            f"session factory must not be called for a parked run "
            f"(profile={profile.name!r})"
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory, state_log=state_log)
    return reg


def _result_json(run_dir: Path) -> "dict | None":
    from reyn.core.pipeline.work_order import read_result
    return read_result(run_dir)


@pytest.mark.asyncio
async def test_absent_invocation_json_is_parked_and_the_skip_is_observable(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: decision 7, the ABSENT half -- a run dir that exists (e.g. a
    crash before invocation.json's own atomic write ever landed) but has
    no invocation.json at all. Left parked; the skip is logged, naming
    the run dir."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)

    baseline_names = reg.list_names()  # AgentRegistry auto-creates its own
    # default agent at construction, unrelated to the pipeline scan below --
    # compare AFTER against this baseline, not against emptiness.

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-no-invocation")
    run_dir.mkdir(parents=True)  # the run dir exists; invocation.json does not

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.registry"):
        rewoken = await reg._rewake_pipeline_runs()

    assert rewoken == [], "an unreadable-owner run must never be re-woken"
    assert _result_json(run_dir) is None, "a parked run must not reach terminal"
    assert read_resume_attempts(run_dir) == 0, "a parked run must not bump the resume counter"
    assert reg.list_names() == baseline_names, (
        "no session/agent identity may be invented to resurrect a run whose "
        f"true owner (driver_agent/driver_sid) could not be read -- "
        f"before={baseline_names!r} after={reg.list_names()!r}"
    )
    assert any(
        "run-no-invocation" in r.message and "no readable invocation.json" in r.message
        for r in caplog.records
    ), f"the skip must be observable, naming the run dir -- got {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_corrupt_invocation_json_is_parked_and_the_skip_is_observable(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: decision 7, the CORRUPT half -- invocation.json exists but
    is not valid JSON (a crash mid-write, or ``json.loads``/
    ``PipelineWorkOrder(**data)`` raising ``ValueError``/``TypeError`` --
    ``load_invocation``'s own documented ``except`` clause). Same parked
    + observable outcome as the absent case -- corrupt and absent are
    both "the owner cannot be named", not two different severities."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)

    baseline_names = reg.list_names()

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-corrupt-invocation")
    run_dir.mkdir(parents=True)
    (run_dir / "invocation.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.registry"):
        rewoken = await reg._rewake_pipeline_runs()

    assert rewoken == []
    assert _result_json(run_dir) is None
    assert read_resume_attempts(run_dir) == 0
    assert reg.list_names() == baseline_names
    assert any(
        "run-corrupt-invocation" in r.message and "no readable invocation.json" in r.message
        for r in caplog.records
    ), f"got {[r.message for r in caplog.records]}"
