"""Tier 2: OS invariant — fresh-mode reset infrastructure (B39).

Verifies:
1. dogfood_fresh_reset.sh wipes the correct state files from a seeded tmp
   workspace and leaves non-targeted files untouched.
2. ScenarioRunResult carries state_mode="fresh" by default, and the field is
   written into the serialised output.json.
3. REYN_DOGFOOD_STATE_MODE env var overrides the default state_mode.
4. load_run_result_from_storage round-trips state_mode correctly.

No mocks. Script tested via real subprocess invocation.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_fresh_reset.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_workspace(root: Path) -> dict[str, Path]:
    """Create a workspace with all fresh-mode state files present.

    Returns a dict of label → Path for each seeded file/dir so tests can
    assert presence/absence independently.
    """
    files: dict[str, Path] = {}

    state_dir = root / ".reyn" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Files that MUST be wiped by the reset script
    for name in ("action_usage.jsonl", "wal.jsonl", "history.jsonl"):
        p = state_dir / name
        p.write_text('{"dummy": true}\n', encoding="utf-8")
        files[name] = p

    # Directory that MUST be wiped
    plans_dir = state_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "decomposition.json").write_text("{}", encoding="utf-8")
    files["plans/"] = plans_dir

    reyn_local = root / "reyn" / "local"
    reyn_local.mkdir(parents=True, exist_ok=True)
    (reyn_local / "skill.md").write_text("# dummy skill\n", encoding="utf-8")
    files["reyn/local/"] = reyn_local

    # File that MUST NOT be wiped (events log)
    events_dir = root / ".reyn" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_file = events_dir / "session.jsonl"
    events_file.write_text('{"type":"workflow_started"}\n', encoding="utf-8")
    files["events/session.jsonl"] = events_file

    return files


def _run_reset(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(workspace)],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Tests — Part B: reset script
# ---------------------------------------------------------------------------

class TestDogfoodFreshResetScript:
    def test_wipes_action_usage(self, tmp_path: Path) -> None:
        """Tier 2: reset script removes action_usage.jsonl from seeded workspace."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not files["action_usage.jsonl"].exists()

    def test_wipes_wal(self, tmp_path: Path) -> None:
        """Tier 2: reset script removes wal.jsonl from seeded workspace."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not files["wal.jsonl"].exists()

    def test_wipes_history(self, tmp_path: Path) -> None:
        """Tier 2: reset script removes history.jsonl from seeded workspace."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not files["history.jsonl"].exists()

    def test_wipes_plans_dir(self, tmp_path: Path) -> None:
        """Tier 2: reset script removes .reyn/state/plans/ from seeded workspace."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not files["plans/"].exists()

    def test_wipes_reyn_local(self, tmp_path: Path) -> None:
        """Tier 2: reset script removes reyn/local/ from seeded workspace."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not files["reyn/local/"].exists()

    def test_does_not_wipe_events(self, tmp_path: Path) -> None:
        """Tier 2: reset script leaves .reyn/events/ intact (live-server constraint)."""
        files = _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert files["events/session.jsonl"].exists()

    def test_idempotent_on_already_clean_workspace(self, tmp_path: Path) -> None:
        """Tier 2: reset script exits 0 when all state files are already absent."""
        # No seeding — workspace is already clean
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "done" in result.stdout

    def test_echoes_what_was_wiped(self, tmp_path: Path) -> None:
        """Tier 2: reset script logs each removed file/dir to stdout."""
        _seed_workspace(tmp_path)
        result = _run_reset(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "action_usage.jsonl" in result.stdout
        assert "wal.jsonl" in result.stdout
        assert "history.jsonl" in result.stdout

    def test_defaults_to_cwd_when_no_arg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 2: reset script uses cwd when called without an explicit path arg."""
        _seed_workspace(tmp_path)
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / ".reyn" / "state" / "action_usage.jsonl").exists()


# ---------------------------------------------------------------------------
# Tests — Part C: state_mode field in runner
# ---------------------------------------------------------------------------

class TestStateModeField:
    def test_scenario_run_result_default_state_mode(self) -> None:
        """Tier 2: ScenarioRunResult carries state_mode='fresh' by default."""
        from reyn.dev.dogfood.runner import ScenarioRunResult

        result = ScenarioRunResult(
            scenario_id="s1",
            reply_text="hi",
            events=[],
            artifacts=[],
        )
        assert result.state_mode == "fresh"

    def test_state_mode_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 2: REYN_DOGFOOD_STATE_MODE env var overrides default state_mode."""
        monkeypatch.setenv("REYN_DOGFOOD_STATE_MODE", "non-fresh")

        # Re-import _resolve_state_mode so env var takes effect
        import importlib

        import reyn.dev.dogfood.runner as runner_mod
        importlib.reload(runner_mod)

        result = runner_mod.ScenarioRunResult(
            scenario_id="s1",
            reply_text="hi",
            events=[],
            artifacts=[],
        )
        assert result.state_mode == "non-fresh"

        # Clean up: reload with original env
        monkeypatch.delenv("REYN_DOGFOOD_STATE_MODE", raising=False)
        importlib.reload(runner_mod)

    def test_state_mode_written_to_output_json(self, tmp_path: Path) -> None:
        """Tier 2: state_mode field appears in persisted output.json."""
        from reyn.dev.dogfood.runner import ScenarioRunResult, _persist_scenario_result

        result = ScenarioRunResult(
            scenario_id="s-fresh-1",
            reply_text="ok",
            events=[],
            artifacts=[],
        )
        _persist_scenario_result(tmp_path, result)

        output_path = tmp_path / "scenarios" / "s-fresh-1" / "output.json"
        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data["state_mode"] == "fresh"

    def test_state_mode_round_trips_via_storage(self, tmp_path: Path) -> None:
        """Tier 2: load_run_result_from_storage preserves state_mode from output.json."""
        from datetime import datetime, timezone

        from reyn.dev.dogfood.runner import (
            RunResult,
            ScenarioRunResult,
            _build_summary,
            _persist_scenario_result,
            _write_json,
            load_run_result_from_storage,
        )

        run_id = "test-run-fresh"
        run_dir = tmp_path / run_id

        result = ScenarioRunResult(
            scenario_id="s-rt-1",
            reply_text="hello",
            events=[],
            artifacts=[],
            state_mode="fresh",
        )
        _persist_scenario_result(run_dir, result)

        run_result = RunResult(
            run_id=run_id,
            set_name="test-set",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            scenario_results=[result],
        )
        summary = _build_summary(run_result)
        _write_json(run_dir / "summary.json", summary)

        loaded = load_run_result_from_storage(run_dir)
        (only,) = loaded.scenario_results
        assert only.state_mode == "fresh"

    def test_state_mode_default_on_legacy_output_json(self, tmp_path: Path) -> None:
        """Tier 2: loading a legacy output.json without state_mode defaults to 'fresh'."""
        from datetime import datetime, timezone

        from reyn.dev.dogfood.runner import (
            RunResult,
            ScenarioRunResult,
            _build_summary,
            _write_json,
            load_run_result_from_storage,
        )

        run_id = "legacy-run"
        run_dir = tmp_path / run_id

        # Write output.json without the state_mode field (simulates a pre-B39 file)
        scenario_dir = run_dir / "scenarios" / "s-legacy"
        scenario_dir.mkdir(parents=True, exist_ok=True)
        legacy_data = {
            "scenario_id": "s-legacy",
            "reply_text": "old reply",
            "reply_outcome": "inconclusive",
            "events_outcome": "inconclusive",
            "artifacts_outcome": "inconclusive",
            "overall_outcome": "inconclusive",
            "detail": {},
            # NOTE: no state_mode key
        }
        (scenario_dir / "output.json").write_text(
            json.dumps(legacy_data), encoding="utf-8"
        )

        dummy_result = ScenarioRunResult(
            scenario_id="s-legacy",
            reply_text="old reply",
            events=[],
            artifacts=[],
        )
        run_result = RunResult(
            run_id=run_id,
            set_name="legacy-set",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            scenario_results=[dummy_result],
        )
        _write_json(run_dir / "summary.json", _build_summary(run_result))

        loaded = load_run_result_from_storage(run_dir)
        assert loaded.scenario_results[0].state_mode == "fresh"

    def test_run_scenario_set_persists_state_mode(self, tmp_path: Path) -> None:
        """Tier 2: run_scenario_set end-to-end writes state_mode into output.json."""
        from reyn.dev.dogfood.runner import ScenarioRunResult, run_scenario_set

        # Minimal mock scenario set
        class _FakeScenario:
            id = "s-e2e-fresh"
            expected_reply = None
            expected_events = None
            expected_artifacts = None
            outcome_prediction = None

        class _FakeScenarioSet:
            name = "e2e-test-set"
            scenarios = [_FakeScenario()]

        async def _stubbed_runner(scenario) -> ScenarioRunResult:
            return ScenarioRunResult(
                scenario_id=scenario.id,
                reply_text="stub reply",
                events=[],
                artifacts=[],
            )

        asyncio.run(
            run_scenario_set(
                _FakeScenarioSet(),
                storage_dir=tmp_path,
                runner_fn=_stubbed_runner,
            )
        )

        output_path = tmp_path / "scenarios" / "s-e2e-fresh" / "output.json"
        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data["state_mode"] == "fresh"
