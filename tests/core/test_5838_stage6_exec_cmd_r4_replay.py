"""Tier 2: #5838 段6's own R4/replay CONFIRMATION (Con 5 from the issue's
own Cons table) -- does NOT implement anything; confirms that a `tool:`
step dispatching `exec` with `cmd` (non-deterministic shell expansion) is
NOT re-executed on pipeline resume, the same exactly-once contract every
other top-level step already gets from `PipelineExecutor.resume`/`_run_
from` (`core/pipeline/executor.py`).

Why this matters specifically for `cmd` (the issue's own framing,
architect/lead-coder thread on #5838): `cmd`-mode shell expansion
($PATH-dependent resolution, glob, etc.) is not guaranteed to reproduce the
SAME result across two separate real executions, so if resume re-ran a
completed `cmd` step instead of replaying its recorded result, a rewind/
resume could silently substitute a DIFFERENT real outcome for the one
actually recorded in `.reyn/events` -- the exact audit-trail-diverges-
from-reality shape charter lens 7 forbids.

Mechanism, read from the code (not re-derived here): `PipelineExecutor.
resume()`'s own docstring states "Replays every step already in snapshot[
"completed_step_results"] (no re-execution — exactly-once)". The TOP-LEVEL
loop (`_run_from`, `core/pipeline/executor.py`) starts at `start_index =
snapshot["step_index"]` -- a value written ONLY after a step's OWN
post-execution `record_pipeline_state` call completes -- so a step whose
completion was already durably recorded is never re-entered; nothing
re-reads `completed_step_results` to decide per-step whether to re-run
(unlike the NESTED `_run_scope`, used by `call`/`fold`, which does check
`key in completed_step_results` per sub-step -- a top-level step instead
relies on `start_index` alone). Either way the effect for a `tool:` step
is the same: a step < `start_index` is never dispatched again.

This file pins that CONFIRMATION with a real `tool_dispatch` callable
(a plain async function, not a mock) that RAISES if called -- so "the
exec step was not re-run" is a witnessed fact (an assertion failure would
fire), not an absence-of-evidence inference."""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep


@pytest.mark.asyncio
async def test_resume_does_not_redispatch_an_already_completed_exec_cmd_step(tmp_path: Path):
    """Tier 2: a pipeline whose step 0 (`tool: exec` with `cmd`) already
    completed and was durably recorded (`step_index=1` in the snapshot)
    resumes WITHOUT re-invoking `tool_dispatch` for that step -- the
    recorded result is reused as both `pipe_data` and the `named_stores`
    entry, byte-identical to what was recorded, never a fresh dispatch."""
    log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    pipeline = Pipeline(steps=[ToolStep(name="exec", args={"cmd": "echo hi"}, output="x")])

    dispatch_calls: list[tuple[str, dict]] = []

    async def _dispatch(name: str, args: dict):
        dispatch_calls.append((name, args))
        raise AssertionError(
            "tool_dispatch must not be called for a step already present "
            "in completed_step_results -- this would re-run the shell "
            "command, which may expand differently the second time"
        )

    prior_result = {
        "kind": "sandboxed_exec", "status": "ok",
        "stdout": "hi\n", "stderr": "", "returncode": 0,
    }
    snapshot = {
        "step_index": 1,
        "named_stores": {"x": prior_result},
        "pipe_data": prior_result,
        "completed_step_results": {"0": prior_result},
    }

    result = await PipelineExecutor().resume(
        "run-5838-r4", pipeline=pipeline, tool_dispatch=_dispatch,
        state_log=log, snapshot=snapshot,
    )

    assert dispatch_calls == [], "the exec/cmd step must not be re-dispatched on resume"
    assert result.pipe_data == prior_result, (
        "resume must return the RECORDED result unchanged, not a freshly "
        "computed one"
    )
    assert result.named_stores["x"] == prior_result
