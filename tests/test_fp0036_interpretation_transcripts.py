"""Tier 1/2: per-scenario interpretation + transcripts publish path (FP-0036).

Covers:
- ``reyn.dogfood.interpretation.build_prompt`` payload shape (Tier 1).
- ``reyn.dogfood.interpretation.generate_interpretation`` failure-mode
  fallback when litellm import fails (Tier 1).
- ``reyn.dogfood.publish.build_transcripts_section`` rendering against
  on-disk run storage (Tier 2 — exercises file IO + outcome ordering).
- ``publish_run(..., with_transcripts=True)`` appends the section to the
  rendered body (Tier 2).

Policy:
- No unittest.mock; storage is real tmp_path scenarios/<id>/output.json
  files; litellm fallback path is exercised via an ImportError-injecting
  stub installed into ``sys.modules`` for the duration of one test.
- Public surface only — no asserts on private attrs.
- No mock transport needed for the transcripts path; publish_run runs
  with ``dry_run=True``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from reyn.dogfood.interpretation import (
    DEFAULT_MODEL,
    build_prompt,
    generate_interpretation,
)
from reyn.dogfood.publish import (
    _DEFAULT_TEMPLATE_PATH,
    _GITHUB_DISCUSSION_BODY_LIMIT,
    DEFAULT_CATEGORY_SLUG,
    DEFAULT_REPO,
    PublishConfig,
    build_transcripts_section,
    publish_run,
)
from reyn.dogfood.runner import ScenarioRunResult
from reyn.dogfood.scenarios import (
    ExpectedReply,
    Scenario,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scenario(
    *,
    sid: str = "smoke_q1",
    input_text: str = "あなたは何ができる?",
    rubric: list[str] | None = None,
) -> Scenario:
    return Scenario(
        id=sid,
        input=input_text,
        expected_reply=ExpectedReply(
            kind="judge",
            rubric=rubric or ["mentions reyn", "self-introduction"],
        ),
    )


def _make_result(
    *,
    sid: str = "smoke_q1",
    reply: str = "私は Reyn agent です。 capability list を出せます。",
    overall: str = "verified",
    interpretation: str | None = None,
) -> ScenarioRunResult:
    detail: dict = {}
    if interpretation is not None:
        detail["interpretation"] = interpretation
    # ScenarioRunResult.__post_init__ recomputes overall as worst-of(reply,
    # events, artifacts); align all three so the desired overall sticks.
    return ScenarioRunResult(
        scenario_id=sid,
        reply_text=reply,
        events=[{"type": "skill_started"}, {"type": "skill_finished"}],
        artifacts=[],
        reply_outcome=overall,
        events_outcome=overall,
        artifacts_outcome=overall,
        overall_outcome=overall,
        detail=detail,
    )


def _write_scenario_storage(
    run_dir: Path,
    result: ScenarioRunResult,
) -> None:
    sdir = run_dir / "scenarios" / result.scenario_id
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "output.json").write_text(
        json.dumps({
            "scenario_id": result.scenario_id,
            "reply_text": result.reply_text,
            "reply_outcome": result.reply_outcome,
            "events_outcome": result.events_outcome,
            "artifacts_outcome": result.artifacts_outcome,
            "overall_outcome": result.overall_outcome,
            "detail": result.detail,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(ev) + "\n")


def _write_scenario_set_yaml(path: Path, *, scenario_inputs: dict[str, str]) -> None:
    lines: list[str] = [
        "type: dogfood_scenario_set",
        "name: smoke_set",
        "scenarios:",
    ]
    for sid, inp in scenario_inputs.items():
        lines.append(f"  - id: {sid}")
        lines.append(f"    input: {json.dumps(inp, ensure_ascii=False)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# interpretation.build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_contains_input_reply_and_expected() -> None:
    """Tier 1: build_prompt returns 2 messages and embeds scenario surfaces."""
    scenario = _make_scenario()
    result = _make_result()

    messages = build_prompt(scenario, result)

    assert messages, "build_prompt must return a non-empty message list"
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"

    user_text = messages[-1]["content"]
    assert "あなたは何ができる?" in user_text
    assert "私は Reyn agent" in user_text
    assert "reply.judge_rubric" in user_text
    assert "mentions reyn" in user_text
    assert "Event types observed" in user_text
    assert "skill_started" in user_text


def test_build_prompt_truncates_long_reply() -> None:
    """Tier 1: reply over 1500 chars is truncated with a marker."""
    scenario = _make_scenario()
    huge = "x" * 5000
    result = _make_result(reply=huge)

    messages = build_prompt(scenario, result)
    user_text = messages[-1]["content"]

    assert "...(truncated)" in user_text


# ---------------------------------------------------------------------------
# interpretation.generate_interpretation fallback
# ---------------------------------------------------------------------------

def test_generate_interpretation_returns_fallback_when_litellm_missing(monkeypatch) -> None:
    """Tier 1: missing litellm produces the documented unavailable fallback string."""
    # Inject a sentinel that raises ImportError when something tries to
    # `import litellm` — works because importlib consults sys.modules first.
    class _Boom:
        def __getattr__(self, name):
            raise ImportError("simulated missing litellm")

    monkeypatch.setitem(sys.modules, "litellm", _Boom())

    scenario = _make_scenario()
    result = _make_result()

    out = asyncio.run(generate_interpretation(scenario, result))

    assert out.startswith("(interpretation unavailable")


def test_default_model_is_flash_lite() -> None:
    """Tier 1: shipped default model id is the documented flash-lite tier."""
    assert "flash-lite" in DEFAULT_MODEL


# ---------------------------------------------------------------------------
# publish.build_transcripts_section
# ---------------------------------------------------------------------------

def test_build_transcripts_empty_when_no_scenarios_dir(tmp_path) -> None:
    """Tier 2: empty string when run dir has no scenarios/ subdir."""
    out = build_transcripts_section(tmp_path)
    assert out == ""


def test_build_transcripts_renders_all_records(tmp_path) -> None:
    """Tier 2: each scenario produces a fenced ``<details>`` block with markers."""
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()

    _write_scenario_storage(run_dir, _make_result(sid="alpha", overall="verified"))
    _write_scenario_storage(run_dir, _make_result(sid="beta", overall="refuted"))
    _write_scenario_storage(run_dir, _make_result(sid="gamma", overall="inconclusive"))

    set_yaml = tmp_path / "set.yaml"
    _write_scenario_set_yaml(set_yaml, scenario_inputs={
        "alpha": "first prompt",
        "beta": "second prompt",
        "gamma": "third prompt",
    })

    section = build_transcripts_section(run_dir, scenario_set_path=set_yaml)

    # Section header
    assert "## Scenarios" in section
    # Three folding blocks, one per id, with stable id order (sorted glob)
    assert section.count("<details>") == 3
    assert section.count("</details>") == 3
    # Outcome markers
    assert "✓ <code>alpha</code> — verified" in section
    assert "✗ <code>beta</code> — refuted" in section
    assert "? <code>gamma</code> — inconclusive" in section
    # Inputs are surfaced from the scenario set YAML
    assert "first prompt" in section
    assert "second prompt" in section
    assert "third prompt" in section
    # Verifier verdict rows
    assert "| reply | verified |" in section


def test_build_transcripts_includes_interpretation_when_present(tmp_path) -> None:
    """Tier 2: detail.interpretation lines render as a > blockquote."""
    run_dir = tmp_path / "run-y"
    run_dir.mkdir()

    interp = (
        "matched. reply mentioned reyn and capabilities.\n"
        "primary surface: reply judge rubric all hit.\n"
        "events emitted: skill_started → skill_finished, no must_not violations."
    )
    _write_scenario_storage(
        run_dir, _make_result(sid="alpha", interpretation=interp)
    )

    section = build_transcripts_section(run_dir)

    assert "**Interpretation**" in section
    assert "> matched. reply mentioned reyn" in section
    assert "> primary surface: reply judge rubric" in section


def test_build_transcripts_truncates_long_reply(tmp_path) -> None:
    """Tier 2: replies exceeding the 800-char default get a truncation marker."""
    run_dir = tmp_path / "run-z"
    run_dir.mkdir()

    huge = "y" * 5000
    _write_scenario_storage(run_dir, _make_result(sid="alpha", reply=huge))

    section = build_transcripts_section(run_dir)

    assert "...(truncated)" in section


# ---------------------------------------------------------------------------
# publish_run integration — dry-run path
# ---------------------------------------------------------------------------

def test_publish_run_dry_run_appends_transcripts(tmp_path) -> None:
    """Tier 2: dry_run + with_transcripts appends the scenarios section to body."""
    run_dir = tmp_path / "run-pub"
    run_dir.mkdir()

    summary = {
        "run_id": "run-pub",
        "set_name": "smoke_set",
        "batch_id": 99,
        "topic": "transcripts smoke",
        "started_at": "2026-05-17T10:00:00+00:00",
        "completed_at": "2026-05-17T10:05:00+00:00",
        "verified": 1, "inconclusive": 0, "refuted": 0, "blocked": 0,
        "total": 1, "verified_rate": 1.0, "brier_score": None,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    _write_scenario_storage(run_dir, _make_result(sid="alpha"))

    set_yaml = tmp_path / "set.yaml"
    _write_scenario_set_yaml(set_yaml, scenario_inputs={"alpha": "first prompt"})

    config = PublishConfig(
        repo=DEFAULT_REPO,
        category_slug=DEFAULT_CATEGORY_SLUG,
        template_path=_DEFAULT_TEMPLATE_PATH,
        token="dummy",
    )

    result_without = publish_run(
        "run-pub", config=config, storage_dir=run_dir, dry_run=True,
    )
    result_with = publish_run(
        "run-pub", config=config, storage_dir=run_dir, dry_run=True,
        with_transcripts=True, scenario_set_path=set_yaml,
    )

    assert "## Scenarios" not in result_without["body"]
    assert "## Scenarios" in result_with["body"]
    assert "<code>alpha</code>" in result_with["body"]
    assert "first prompt" in result_with["body"]
    # Body remains under the GitHub limit for a tiny run
    assert len(result_with["body"]) < _GITHUB_DISCUSSION_BODY_LIMIT


# ---------------------------------------------------------------------------
# CLI argparse — new flags
# ---------------------------------------------------------------------------

def test_cli_run_argparse_has_with_interpretation_flag() -> None:
    """Tier 1: 'reyn dogfood run' exposes --with-interpretation + model override."""
    import argparse

    from reyn.cli.commands.dogfood import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)

    args = parser.parse_args([
        "dogfood", "run", "some.yaml",
        "--with-interpretation",
        "--interpretation-model", "openai/gpt-4o-mini",
    ])
    assert args.with_interpretation is True
    assert args.interpretation_model == "openai/gpt-4o-mini"


def test_cli_publish_argparse_has_with_transcripts_flag() -> None:
    """Tier 1: 'reyn dogfood publish' exposes --with-transcripts + --scenario-set."""
    import argparse

    from reyn.cli.commands.dogfood import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)

    args = parser.parse_args([
        "dogfood", "publish", "run-abc",
        "--with-transcripts",
        "--scenario-set", "dogfood/scenarios/chat_router_smoke.yaml",
    ])
    assert args.with_transcripts is True
    assert args.scenario_set == "dogfood/scenarios/chat_router_smoke.yaml"
