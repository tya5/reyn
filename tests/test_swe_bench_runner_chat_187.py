"""Tier 2: #187 — the general-agent (reyn chat) SWE runner path withholds test_patch.

#187 solves SWE with the general agent (`reyn chat` / RouterLoop) instead of the
swe_bench skill. The held-out evaluation `test_patch` must NEVER reach the agent
(test-leakage = the cheat); the harness scores it externally from the dataset.

The in-container agent loop itself needs docker (= sandbox_2's faithful dogfood,
not a unit). What IS unit-testable here:
  (a) the SWE task prompt withholds test_patch and carries the issue + base_commit;
  (b) the prompt is minimal / de-prescribed (no rigid "reproduce → verify" steps —
      re-introducing a procedure would be the phase-graph over-specialization the
      pivot removed);
  (c) the runner exposes `--agent-mode {skill,chat}` (default skill = the old
      path stays, non-destructive) and `chat` requires `--env-backend=docker`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import swe_bench_runner as r  # noqa: E402

_INSTANCE = {
    "instance_id": "astropy__astropy-13453",
    "repo": "astropy/astropy",
    "base_commit": "abc123def456",
    "problem_statement": "BUG: Table.write(format='html') drops the formats kwarg.",
    "hints_text": "Look at astropy/io/ascii/html.py near the write method.",
    "test_patch": (
        "diff --git a/astropy/io/ascii/tests/test_html.py ...\n"
        "+def test_formats_kwarg_applied(): SECRET_EVAL_ASSERTION"
    ),
}


def test_swe_task_prompt_withholds_test_patch() -> None:
    """Tier 2: the agent's SWE task prompt does NOT contain the held-out test_patch."""
    prompt = r.build_swe_task_prompt(_INSTANCE)
    assert "SECRET_EVAL_ASSERTION" not in prompt, "the held-out test_patch must not leak into the agent prompt"
    assert "test_patch" not in prompt, "do not mention test_patch (held out; the harness scores it)"


def test_swe_task_prompt_carries_issue_and_base_commit() -> None:
    """Tier 2: the prompt carries the legitimate task inputs (issue + base_commit)."""
    prompt = r.build_swe_task_prompt(_INSTANCE)
    assert "Table.write(format='html') drops the formats kwarg" in prompt
    assert "abc123def456" in prompt  # base_commit
    assert "astropy/astropy" in prompt  # repo


def test_swe_task_prompt_is_minimal_no_rigid_procedure() -> None:
    """Tier 2: the prompt is de-prescribed — no rigid 'Step 1 / reproduce / verify'
    procedure (that would re-introduce the phase-graph over-specialization the pivot
    removed). The agent decides how; the prompt just states the task + that it has tools."""
    lowered = r.build_swe_task_prompt(_INSTANCE).lower()
    # No numbered procedure / prescribed verify method.
    assert "step 1" not in lowered
    assert "reproduce the" not in lowered  # not prescribing a reproduction procedure
    # It does hand the agent agency over the 'how'.
    assert "your judgment" in lowered


def test_runner_exposes_agent_mode_flag_default_skill() -> None:
    """Tier 2: --agent-mode {skill,chat}, default 'skill' (old path stays, non-destructive)."""
    p = r.build_parser()
    assert p.parse_args(["--input", "/dev/null"]).agent_mode == "skill"
    assert p.parse_args(["--input", "/dev/null", "--agent-mode", "chat"]).agent_mode == "chat"


def test_agent_mode_chat_requires_docker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tier 2: --agent-mode=chat without --env-backend=docker exits 1 (the general
    agent needs the per-instance container)."""
    import json

    inst_file = tmp_path / "inst.json"
    inst_file.write_text(json.dumps(_INSTANCE), encoding="utf-8")
    rc = r.main(["--input", str(inst_file), "--agent-mode", "chat"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --env-backend=docker" in err
