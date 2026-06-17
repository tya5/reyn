"""Tier 2: ``dogfood_batch_dispatch.render_worker_prompt`` emits an
absolute deliverable path against the supplied repo root.

Pinned invariants:

- The "Deliverable" section of the generated prompt contains an
  **absolute filesystem path** (= starts with ``/``) for the
  ``results-worker-N.json`` write target, not the relative
  ``config.journal_dir/...`` form.
- The prompt explicitly states the MAIN repo CWD so the sub-agent
  knows which root the absolute path corresponds to.
- The absolute path is constructed by joining ``repo_root`` with
  ``config.journal_dir`` and ``workers/results-worker-<n>.json``.
- When ``repo_root`` is omitted, the function falls back to
  ``Path.cwd()`` — backwards-compat for callers that pre-date this
  fix.

Motivation: B45 retrospective recorded the "worker output path
inconsistency" carry-over: W2/W3/W7 wrote to the worktree-relative
``docs/...`` path inside ``/tmp/reyn-worktrees/b{n}-{w}/...`` rather
than the main repo's ``docs/...``. The aggregator then couldn't find
the files and a manual ``cp`` step was required. B47 worked around it
by hand-coding "from MAIN repo CWD" into each SkillRuntime() dispatch prompt;
this fix lifts that into the script-generated template.

testing.ja.md compliance:
- No mocks. Real ``render_worker_prompt`` called against a tmp_path
  config.
- Tier 2 contract pin: the prompt's deliverable-path shape.
- No private-state assertions; only the rendered string.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def _make_config(journal_dir: str) -> BatchConfig:
    return BatchConfig(
        batch=BatchMeta(
            name="BTEST",
            date="2026-05-22",
            head="deadbeef",
            env_vars={"REYN_EMPTY_STOP_RETRY": "1"},
            user_params={"hot_list_n": 10},
            hard_caps={"tool_uses": 50, "wall_clock_min": 15},
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
        journal_dir=journal_dir,
    )


def test_deliverable_path_is_absolute_against_repo_root(tmp_path):
    """Tier 2: rendered prompt emits the deliverable path as an absolute
    filesystem path joined from the supplied repo_root + journal_dir +
    worker filename (B45-carry-over fix)."""
    config = _make_config("docs/journal/test-batch")
    repo_root = tmp_path / "fake-repo"
    repo_root.mkdir()

    prompt = render_worker_prompt(
        config, config.workers[0], repo_root=repo_root,
    )
    expected_path = str(
        repo_root.resolve()
        / "docs/journal/test-batch/workers/results-worker-1.json"
    )
    assert expected_path in prompt, (
        f"prompt should contain absolute deliverable path "
        f"{expected_path!r}. Got prompt excerpt:\n"
        f"{prompt[prompt.find('## Deliverable'):prompt.find('## Hard caps')]}"
    )


def test_prompt_states_main_repo_cwd_explicitly(tmp_path):
    """Tier 2: the prompt must say "MAIN repo CWD is <path>" so the
    sub-agent knows which root the absolute path corresponds to (=
    avoids ambiguity for sub-agents running from a worktree)."""
    config = _make_config("docs/journal/test")
    repo_root = tmp_path / "fake-repo"
    repo_root.mkdir()

    prompt = render_worker_prompt(
        config, config.workers[0], repo_root=repo_root,
    )
    assert "MAIN repo CWD" in prompt, (
        f"prompt should explicitly state the MAIN repo CWD. "
        f"Got Deliverable section:\n"
        f"{prompt[prompt.find('## Deliverable'):prompt.find('## Hard caps')]}"
    )
    assert str(repo_root.resolve()) in prompt


def test_deliverable_path_is_no_longer_relative(tmp_path):
    """Tier 2: regression guard — the prompt MUST NOT emit the
    bare relative form `{config.journal_dir}/workers/results-worker-N.json`
    (= the pre-fix shape that caused workers to write to worktree-
    relative paths)."""
    config = _make_config("docs/journal/test")
    repo_root = tmp_path / "fake-repo"
    repo_root.mkdir()

    prompt = render_worker_prompt(
        config, config.workers[0], repo_root=repo_root,
    )
    # The exact relative form (= journal_dir/workers/...) should NOT
    # appear as a backtick-wrapped Write target. We check by looking
    # for the bare relative path inside a `Write` instruction.
    bare_rel = "docs/journal/test/workers/results-worker-1.json"
    # The bare relative path may appear *inside* the absolute path
    # (because the absolute is the relative joined to repo_root). What
    # we forbid is the bare relative wrapped as a backticked Write
    # target by itself (= no preceding path component).
    bad_form = f"`{bare_rel}`"
    assert bad_form not in prompt, (
        f"prompt must not contain the bare relative deliverable form "
        f"{bad_form!r}. Got prompt:\n{prompt[:500]}"
    )


def test_default_repo_root_is_cwd_when_omitted():
    """Tier 2: when ``repo_root`` is omitted, the function falls back
    to ``Path.cwd()`` — backwards-compat for callers pre-fix. We
    verify the cwd appears in the prompt; the exact absolute path is
    not pinned because it depends on test invocation cwd."""
    config = _make_config("docs/journal/test")
    prompt = render_worker_prompt(config, config.workers[0])
    cwd = str(Path.cwd().resolve())
    assert cwd in prompt, (
        f"prompt should contain the current cwd {cwd!r} when "
        f"repo_root is omitted (= backwards-compat)"
    )


def test_smoke_against_b47_yaml_does_not_emit_relative_paths():
    """Tier 2: end-to-end smoke against a real committed batch yaml
    (= dogfood/batch_b47.yaml) — the resulting prompt for W1 must
    contain an absolute path, not the bare relative shape. This pins
    the fix against the actual production yaml shape, not just the
    synthetic test config."""
    real_yaml = (
        Path(__file__).resolve().parent.parent
        / "dogfood" / "batch_b47.yaml"
    )
    if not real_yaml.is_file():
        pytest.skip("dogfood/batch_b47.yaml not present in checkout")
    from dogfood_batch_config import load_batch_config

    config = load_batch_config(real_yaml)
    prompt = render_worker_prompt(
        config, config.workers[0], repo_root=Path.cwd(),
    )
    assert "/Users" in prompt or "/home" in prompt or "/tmp" in prompt, (
        "prompt should contain an absolute filesystem path"
    )
    # Bare relative form must not appear as the Write target.
    relative_only = (
        f"`{config.journal_dir}/workers/results-worker-1.json`"
    )
    assert relative_only not in prompt
