"""Dogfood scenario runner (FP-0036 Component B).

Drives the chat router (= same path as ``reyn chat``) with a scenario's
``input`` / ``prompts``, captures the reply text, the emitted P6 events,
and the workspace artifacts. Returns a RunResult per scenario for the
verifier (F3) to score.

Storage layout under ``.reyn/dogfood/runs/<run_id>/``:
  scenarios/<scenario_id>/output.json    # reply + verifier verdicts
  scenarios/<scenario_id>/events.jsonl   # captured event tail
  scenarios/<scenario_id>/artifacts/     # workspace snapshot
  summary.json                            # 4-band aggregate + Brier

Injection seam
--------------
``run_scenario_set`` accepts an optional ``runner_fn`` parameter that
overrides the default live-LLM path. The signature is::

    async def runner_fn(scenario: Scenario) -> ScenarioRunResult

This seam is the primary test path — the framework is fully testable
without a live LLM. ``--replay`` mode (F5) also populates this seam.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from reyn.dev.dogfood.scenarios import Scenario, ScenarioSet


# ---------------------------------------------------------------------------
# Fresh-mode state annotation
# ---------------------------------------------------------------------------

#: Default state mode — fresh means every scenario starts from
#: DEFAULT_HOT_LIST_SEED with no carry-over action_usage.jsonl / wal.jsonl /
#: history.jsonl / reyn/local/ state. Override via env var
#: REYN_DOGFOOD_STATE_MODE for deliberate non-fresh comparison runs.
_DEFAULT_STATE_MODE = "fresh"


def _resolve_state_mode() -> str:
    """Return the current state mode.

    Reads ``REYN_DOGFOOD_STATE_MODE`` env var; falls back to ``"fresh"``.
    The field is written into every per-scenario output.json so that
    cross-batch V comparisons can verify both runs share the same mode.
    """
    return os.environ.get("REYN_DOGFOOD_STATE_MODE", _DEFAULT_STATE_MODE)


# ---------------------------------------------------------------------------
# Outcome ordering
# ---------------------------------------------------------------------------

#: Ordered from worst (index 0) to best (index 3).
OUTCOME_ORDER: list[str] = ["blocked", "refuted", "inconclusive", "verified"]


def _outcome_rank(outcome: str) -> int:
    """Return the rank of *outcome* (higher = better). Unknown outcomes → -1."""
    try:
        return OUTCOME_ORDER.index(outcome)
    except ValueError:
        return -1


def _worst_outcome(*outcomes: str) -> str:
    """Return the worst (lowest-ranked) outcome among *outcomes*."""
    ranked = [(outcome, _outcome_rank(outcome)) for outcome in outcomes]
    return min(ranked, key=lambda x: x[1])[0]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScenarioRunResult:
    """Per-scenario execution result including verifier verdicts."""

    scenario_id: str
    reply_text: str
    events: list[dict]
    artifacts: list[dict]
    reply_outcome: str = "inconclusive"    # from verifiers.reply
    events_outcome: str = "inconclusive"   # from verifiers.events
    artifacts_outcome: str = "inconclusive"  # from verifiers.artifacts
    overall_outcome: str = "inconclusive"  # worst-case of the three
    detail: dict = field(default_factory=dict)
    #: Explicit hot-list state annotation — "fresh" means the scenario ran
    #: from DEFAULT_HOT_LIST_SEED with no carry-over state. Set to
    #: "non-fresh" (or a custom label) when a deliberate warm-state run is
    #: needed for comparison. See §6.7 of dogfood-discipline.md.
    state_mode: str = field(default_factory=_resolve_state_mode)

    def __post_init__(self) -> None:
        # Recompute overall_outcome from the three verifier outcomes
        self.overall_outcome = _worst_outcome(
            self.reply_outcome,
            self.events_outcome,
            self.artifacts_outcome,
        )


@dataclass
class RunResult:
    """Aggregate result for a full scenario set run."""

    run_id: str
    set_name: str
    started_at: datetime
    completed_at: datetime | None
    scenario_results: list[ScenarioRunResult] = field(default_factory=list)

    def aggregate(self) -> dict:
        """Compute 4-band aggregate counts + Brier score (if predictions exist).

        Returns a dict with keys:
          verified, inconclusive, refuted, blocked  — scenario counts
          total                                      — total scenario count
          verified_rate                              — verified / total (0.0 if total == 0)
          brier_score                                — float if any scenario has
                                                       outcome_prediction; None otherwise
        """
        counts: dict[str, int] = {
            "verified": 0,
            "inconclusive": 0,
            "refuted": 0,
            "blocked": 0,
        }
        brier_sum = 0.0
        brier_n = 0

        for sr in self.scenario_results:
            outcome = sr.overall_outcome
            if outcome in counts:
                counts[outcome] += 1
            else:
                counts["inconclusive"] += 1

            # Brier score: if the scenario carries an outcome_prediction dict,
            # compute the quadratic score against the actual outcome.
            prediction: dict | None = sr.detail.get("outcome_prediction")
            if prediction:
                for band in OUTCOME_ORDER:
                    predicted_prob = float(prediction.get(band, 0.0))
                    actual = 1.0 if outcome == band else 0.0
                    brier_sum += (predicted_prob - actual) ** 2
                brier_n += 1

        total = sum(counts.values())
        verified_rate = counts["verified"] / total if total > 0 else 0.0

        result: dict = {
            **counts,
            "total": total,
            "verified_rate": verified_rate,
        }
        if brier_n > 0:
            # Average the per-scenario, per-band sum over n scenarios.
            # Each scenario contributes len(OUTCOME_ORDER) squared errors, so
            # divide by (brier_n * len(OUTCOME_ORDER)) to normalise to [0, 2].
            result["brier_score"] = brier_sum / (brier_n * len(OUTCOME_ORDER))
        else:
            result["brier_score"] = None

        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=str))
            fh.write("\n")


def _scenario_storage(run_dir: Path, scenario_id: str) -> Path:
    return _ensure_dir(run_dir / "scenarios" / scenario_id)


def _persist_scenario_result(
    run_dir: Path, result: ScenarioRunResult
) -> None:
    """Write per-scenario output.json and events.jsonl to run storage."""
    sdir = _scenario_storage(run_dir, result.scenario_id)

    output_data = {
        "scenario_id": result.scenario_id,
        "reply_text": result.reply_text,
        "reply_outcome": result.reply_outcome,
        "events_outcome": result.events_outcome,
        "artifacts_outcome": result.artifacts_outcome,
        "overall_outcome": result.overall_outcome,
        "state_mode": result.state_mode,
        "detail": result.detail,
    }
    _write_json(sdir / "output.json", output_data)
    _write_jsonl(sdir / "events.jsonl", result.events)

    # Persist artifact snapshots as JSON files under artifacts/
    artifact_dir = _ensure_dir(sdir / "artifacts")
    for i, artifact in enumerate(result.artifacts):
        art_name = artifact.get("id") or artifact.get("type") or f"artifact_{i}"
        _write_json(artifact_dir / f"{art_name}.json", artifact)


def _build_summary(run_result: RunResult) -> dict:
    agg = run_result.aggregate()
    return {
        "run_id": run_result.run_id,
        "set_name": run_result.set_name,
        "started_at": run_result.started_at.isoformat(),
        "completed_at": (
            run_result.completed_at.isoformat()
            if run_result.completed_at is not None
            else None
        ),
        **agg,
    }


# ---------------------------------------------------------------------------
# Default runner_fn — stubbed for MVP; replaced by live-LLM path in CLI
# ---------------------------------------------------------------------------

async def _default_runner_fn(scenario: "Scenario") -> ScenarioRunResult:  # pragma: no cover
    """Placeholder live-LLM runner.

    In production the CLI injects a proper runner_fn that drives the chat
    router.  This stub returns an inconclusive result so the framework
    functions correctly in offline / unit-test contexts without a live LLM.
    """
    return ScenarioRunResult(
        scenario_id=scenario.id,
        reply_text="(live runner not configured)",
        events=[],
        artifacts=[],
        reply_outcome="inconclusive",
        events_outcome="inconclusive",
        artifacts_outcome="inconclusive",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

RunnerFn = Callable[["Scenario"], Awaitable[ScenarioRunResult]]


async def run_scenario_set(
    scenario_set: "ScenarioSet",
    *,
    run_id: str | None = None,
    storage_dir: Path | None = None,
    agent_name: str = "default",
    n: int = 1,
    replay_fixture_dir: Path | None = None,
    runner_fn: RunnerFn | None = None,
    with_interpretation: bool = False,
    interpretation_model: str | None = None,
) -> RunResult:
    """Run every scenario in *scenario_set*, repeat N times for stability,
    write results under *storage_dir*.

    Parameters
    ----------
    scenario_set:
        The loaded ``ScenarioSet`` (from F1's ``load_scenario_set``).
    run_id:
        Unique identifier for this run. Auto-generated (UUID4) if omitted.
    storage_dir:
        Root directory for run output. Defaults to
        ``.reyn/dogfood/runs/<run_id>`` relative to cwd.
    agent_name:
        Chat-router agent name. Passed to the live-LLM runner_fn.
    n:
        Number of times to repeat the full scenario set (for stability bands).
        Scenario results across repetitions are aggregated (worst-case outcome
        wins — a single ``refuted`` in N runs marks the scenario refuted).
    replay_fixture_dir:
        When set, the framework loads ``reyn.dev.dogfood.replay`` and uses recorded
        fixtures instead of live LLM calls. Takes precedence over runner_fn.
    runner_fn:
        Injectable async callable ``(Scenario) -> ScenarioRunResult``. If
        omitted, defaults to ``_default_runner_fn`` (returns inconclusive).
        The CLI populates this with the real headless chat-router path.
    with_interpretation:
        When True, after the verifier finishes, invoke
        ``reyn.dev.dogfood.interpretation.generate_interpretation`` for every
        scenario and store the resulting 3-line summary in
        ``result.detail["interpretation"]``. Adds one cheap LLM call per
        scenario (~$0.0005 at flash-lite tier).
    interpretation_model:
        Override the LiteLLM model id used for interpretation. Defaults to
        ``reyn.dev.dogfood.interpretation.DEFAULT_MODEL``.

    Returns
    -------
    RunResult
        The aggregate run result (also written to storage).
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    if storage_dir is None:
        storage_dir = Path.cwd() / ".reyn" / "dogfood" / "runs" / run_id

    _ensure_dir(storage_dir)

    # Replay mode: delegate runner_fn to the F5 replay module
    if replay_fixture_dir is not None:
        try:
            from reyn.dev.dogfood.replay import replay_run  # type: ignore[import]

            async def _replay_runner(scenario: "Scenario") -> ScenarioRunResult:
                return await replay_run(scenario, fixture_dir=replay_fixture_dir)

            effective_runner_fn: RunnerFn = _replay_runner
        except ImportError:
            raise ImportError(
                "reyn.dev.dogfood.replay is not yet available. "
                "Install the full FP-0036 package to use --replay mode."
            )
    else:
        effective_runner_fn = runner_fn or _default_runner_fn

    started_at = datetime.now(timezone.utc)
    # Collect per-scenario results across N repetitions; worst-case wins
    per_scenario_results: dict[str, list[ScenarioRunResult]] = {}

    # Import verifier triad once — all three live in the same package so a
    # single ImportError means the package is not installed.
    try:
        from reyn.dev.dogfood.verifiers import (
            verify_artifacts,
            verify_events,
            verify_reply,
        )
        _verifiers_available = True
    except ImportError:
        _verifiers_available = False

    for _rep in range(max(1, n)):
        for scenario in scenario_set.scenarios:
            result = await effective_runner_fn(scenario)

            # ── Verifier triad ──────────────────────────────────────────────
            # Invoke verify_reply / verify_events / verify_artifacts and
            # write outcomes + detail back onto the result so the 4-band
            # aggregate reflects real verdicts rather than the "inconclusive"
            # default that runner_fn returns.
            #
            # Guard: only fire when the scenario declares at least one
            # expected_* block.  Scenarios with no assertions are used in
            # legacy / exploratory runs where the runner_fn's outcomes are
            # authoritative (e.g. blocked from a failed live run).  Invoking
            # the verifier for such scenarios would replace meaningful
            # outcomes with "blocked" (= no assertion declared), which is
            # misleading.
            _has_expected = (
                scenario.expected_reply is not None
                or scenario.expected_events is not None
                or scenario.expected_artifacts is not None
            )
            if _verifiers_available and _has_expected:
                # reply verifier is async (may invoke LLM judge)
                reply_result = await verify_reply(
                    scenario.expected_reply,
                    result.reply_text,
                )
                # events and artifacts verifiers are synchronous
                events_result = verify_events(
                    scenario.expected_events,
                    result.events,
                )
                artifacts_result = verify_artifacts(
                    scenario.expected_artifacts,
                    result.artifacts,
                )

                result.reply_outcome = reply_result.outcome
                result.events_outcome = events_result.outcome
                result.artifacts_outcome = artifacts_result.outcome
                result.detail.update({
                    "reply": reply_result.detail,
                    "events": events_result.detail,
                    "artifacts": artifacts_result.detail,
                })
                # Recompute overall_outcome from the updated sub-outcomes.
                result.overall_outcome = _worst_outcome(
                    result.reply_outcome,
                    result.events_outcome,
                    result.artifacts_outcome,
                )

            # Attach outcome_prediction from the scenario definition if
            # the runner_fn didn't already populate it.
            if (
                "outcome_prediction" not in result.detail
                and hasattr(scenario, "outcome_prediction")
                and scenario.outcome_prediction is not None
            ):
                pred = scenario.outcome_prediction
                result.detail["outcome_prediction"] = {
                    "verified": pred.verified,
                    "inconclusive": pred.inconclusive,
                    "refuted": pred.refuted,
                    "blocked": pred.blocked,
                }
            per_scenario_results.setdefault(scenario.id, []).append(result)

    completed_at = datetime.now(timezone.utc)

    # Merge: worst-case outcome across repetitions
    merged: list[ScenarioRunResult] = []
    for scenario_id, results in per_scenario_results.items():
        if len(results) == 1:
            merged.append(results[0])
        else:
            worst = min(results, key=lambda r: _outcome_rank(r.overall_outcome))
            merged.append(worst)

    # Optional interpretation pass — one cheap LLM call per scenario summarising
    # whether the run matched expectations. Failures are isolated to the
    # auxiliary detail field and never abort the run.
    if with_interpretation:
        from reyn.dev.dogfood.interpretation import (
            DEFAULT_MODEL,
            generate_interpretation,
        )

        model = interpretation_model or DEFAULT_MODEL
        scenario_index = {s.id: s for s in scenario_set.scenarios}
        for result in merged:
            scenario = scenario_index.get(result.scenario_id)
            if scenario is None:
                continue
            summary = await generate_interpretation(
                scenario, result, model=model
            )
            result.detail["interpretation"] = summary

    # Persist per-scenario results
    for result in merged:
        _persist_scenario_result(storage_dir, result)

    run_result = RunResult(
        run_id=run_id,
        set_name=scenario_set.name,
        started_at=started_at,
        completed_at=completed_at,
        scenario_results=merged,
    )

    summary = _build_summary(run_result)
    _write_json(storage_dir / "summary.json", summary)

    return run_result


def load_run_result_from_storage(run_dir: Path) -> RunResult:
    """Reconstruct a RunResult from persisted storage (for ``reyn dogfood report``).

    Reads ``summary.json`` and all ``scenarios/*/output.json`` files.
    """
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"No summary.json found in {run_dir}")

    summary = json.loads(summary_path.read_text())

    run_id = summary["run_id"]
    set_name = summary["set_name"]
    started_at = datetime.fromisoformat(summary["started_at"])
    completed_at_raw = summary.get("completed_at")
    completed_at = (
        datetime.fromisoformat(completed_at_raw) if completed_at_raw else None
    )

    scenario_results: list[ScenarioRunResult] = []
    scenarios_dir = run_dir / "scenarios"
    if scenarios_dir.exists():
        for output_path in sorted(scenarios_dir.glob("*/output.json")):
            data = json.loads(output_path.read_text())

            # Load events
            events_path = output_path.parent / "events.jsonl"
            events: list[dict] = []
            if events_path.exists():
                for line in events_path.read_text().splitlines():
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))

            sr = ScenarioRunResult(
                scenario_id=data["scenario_id"],
                reply_text=data.get("reply_text", ""),
                events=events,
                artifacts=[],
                reply_outcome=data.get("reply_outcome", "inconclusive"),
                events_outcome=data.get("events_outcome", "inconclusive"),
                artifacts_outcome=data.get("artifacts_outcome", "inconclusive"),
                overall_outcome=data.get("overall_outcome", "inconclusive"),
                state_mode=data.get("state_mode", _DEFAULT_STATE_MODE),
                detail=data.get("detail", {}),
            )
            scenario_results.append(sr)

    return RunResult(
        run_id=run_id,
        set_name=set_name,
        started_at=started_at,
        completed_at=completed_at,
        scenario_results=scenario_results,
    )
