"""Tier 2: dogfood batch worker prompt template embeds verdict-scoping guidance.

Pinned invariant:

- The generated worker prompt contains explicit "verdict scoping" guidance
  telling sub-agents NOT to add unwritten requirements based on the
  ``covers:`` field, scenario ``id``, or scenario set name.
- The rubric / events / artifacts lists in the yaml are the only verdict
  criteria.

Motivation: B48 W1-S1/S6/S7 were verdicted I by sub-agents who treated
``covers: stdlib-skills/direct-llm`` as a requirement that the
``direct_llm`` skill must have been invoked. The scenario yaml had no
``artifacts:`` requirement — the rubric was fully met — so the correct
verdict should have been V. 3-scenario over-strict misclassification cost
~3V across the W1 worker. This regression guard pins the guidance string
in the prompt template so future contributors do not inadvertently drop it.

testing.ja.md compliance:
- No mocks. Real ``render_worker_prompt`` against a tmp_path config.
- Tier 2 contract pin on the prompt template wording.
- No private-state assertions.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_batch_config import (  # noqa: E402
    BatchConfig,
    BatchMeta,
    PastBatch,
    WorkerSpec,
)
from dogfood_batch_dispatch import render_worker_prompt  # noqa: E402


def _make_config() -> BatchConfig:
    return BatchConfig(
        batch=BatchMeta(
            name="BTEST",
            date="2026-05-22",
            head="deadbeef",
            env_vars={},
            user_params={},
            hard_caps={},
        ),
        workers=(
            WorkerSpec(
                name="W1",
                scenario_set="test_set.yaml",
                scenario_set_path="dogfood/scenarios/test_set.yaml",
                port=8201,
                n_scenarios=3,
                worktree="/tmp/test-wt-1",
                agent_prefix="test-w1-s",
            ),
        ),
        past_batches=(PastBatch(name="BPREV", aggregate_path="prev.json"),),
        journal_dir="docs/journal/test",
    )


def test_prompt_contains_verdict_scoping_guidance():
    """Tier 2 (B48 W1 fix): the prompt MUST explicitly state that
    ``covers:`` and scenario metadata are NOT verdict criteria."""
    cfg = _make_config()
    prompt = render_worker_prompt(cfg, cfg.workers[0])

    # Specific phrase identifying the verdict-scoping section
    assert "verdict scoping" in prompt.lower(), (
        "prompt must include 'verdict scoping' section. Got Verdict-rules "
        "section excerpt:\n"
        + prompt[prompt.find('## Verdict rules'):prompt.find('## Verdict rules') + 600]
    )

    # The guidance must name `covers:` explicitly as metadata-not-criterion
    assert "covers" in prompt, (
        "prompt must mention `covers:` field as metadata-not-criterion"
    )

    # The guidance must call out the rubric / events / artifacts as the
    # canonical verdict sources
    for canonical in ("rubric", "must_emit", "must_not_emit", "artifacts"):
        assert canonical in prompt, (
            f"prompt should reference {canonical!r} as a canonical verdict source"
        )


def test_prompt_cites_b48_observation():
    """Tier 2: regression guard — the guidance references B48 retrospective
    as the source observation. If a future contributor removes the
    historical reference without preserving the rule, this fails."""
    cfg = _make_config()
    prompt = render_worker_prompt(cfg, cfg.workers[0])
    # The wording mentions B48 (or "B48 retrospective" / "B48 W1")
    assert "B48" in prompt, (
        "prompt should reference the B48 observation that triggered "
        "this guidance"
    )


def test_prompt_verdict_rules_still_intact():
    """Tier 2: regression — the original V/I/R/B definitions are still
    present after the guidance addition (= no accidental displacement)."""
    cfg = _make_config()
    prompt = render_worker_prompt(cfg, cfg.workers[0])
    for line in (
        "**V (verified)**:",
        "**I (inconclusive)**:",
        "**R (refuted)**:",
        "**B (blocked)**:",
    ):
        assert line in prompt, (
            f"prompt should still contain {line!r} verdict-rule line; the "
            f"B48 guidance addition must not displace the existing rules"
        )
