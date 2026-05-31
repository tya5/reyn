"""Tier 2: ``compaction_grant`` is opt-in per worker (B55 retro fix).

Pinned invariants:

- ``WorkerSpec.compaction_grant`` defaults to ``False`` when the batch
  yaml entry omits the field (= backwards compat with batch_b4*.yaml
  and pre-B55 configs).
- When the batch yaml sets ``compaction_grant: true`` on a worker, the
  loader returns ``WorkerSpec.compaction_grant == True``.
- ``setup_worktree`` injects the ``chat.compaction`` block into the
  worker's ``reyn.local.yaml`` **only** when
  ``worker.compaction_grant`` is True.
- The ``permissions`` + ``sandbox`` env grant block (= B52 retro) is
  unconditional and remains injected regardless of the compaction flag.

Motivation: B55 W1-S4 word_stats_demo R verdict was caused by the
``chat.compaction`` config lowering (PR #880) being injected into every
worker's ``reyn.local.yaml``. Compaction_check fired mid-flow on a
short-skill scenario that didn't need it, partially compacted the
post-spawn context, and the final reply LLM ended up re-spawning the
skill instead of synthesising from the artifact. The grant is needed
only by the worker hosting ``chat_compactor_auto_trigger``, hence
opt-in.

testing.ja.md compliance:
- No mocks. Real ``load_batch_config`` against tmp_path yamls and
  real ``setup_worktree`` writing to a tmp directory.
- No private-state assertions; pins ``WorkerSpec.compaction_grant``
  and the on-disk ``reyn.local.yaml`` content.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_batch_config import WorkerSpec, load_batch_config  # noqa: E402
from dogfood_batch_dispatch import setup_worktree  # noqa: E402


def _write_scenario_yaml(path: Path) -> None:
    path.write_text(
        "metadata:\n  name: test\nscenarios:\n"
        "  - id: scen_0\n    input: hello\n    expected: world\n",
        encoding="utf-8",
    )


def _write_batch_yaml(
    path: Path,
    scenario_yaml_path: str,
    *,
    compaction_grant: bool | None,
) -> None:
    """Write a minimal batch yaml; optionally include compaction_grant."""
    grant_field = (
        f"    compaction_grant: {'true' if compaction_grant else 'false'}\n"
        if compaction_grant is not None
        else ""
    )
    path.write_text(
        f"""batch:
  name: TEST
  date: "2026-05-27"
  head: deadbeef
workers:
  - name: W2
    scenario_set: test_set
    scenario_set_path: {scenario_yaml_path}
    port: 8001
    worktree: /tmp/test-wt
    agent_prefix: test-w2-s
{grant_field}past_batches: []
journal_dir: /tmp/test-journal
""",
        encoding="utf-8",
    )


# ── Loader contract ──────────────────────────────────────────────────────


def test_omitted_compaction_grant_defaults_to_false(tmp_path):
    """Tier 2: batch yaml may omit ``compaction_grant`` entirely; the
    loader defaults to False so legacy batch configs preserve the
    pre-B55-retro behaviour of *not* injecting the compaction block."""
    scen = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen)
    batch = tmp_path / "batch.yaml"
    _write_batch_yaml(batch, str(scen), compaction_grant=None)

    cfg = load_batch_config(batch)
    assert cfg.workers[0].compaction_grant is False


def test_explicit_compaction_grant_true_is_respected(tmp_path):
    """Tier 2: ``compaction_grant: true`` propagates to ``WorkerSpec``."""
    scen = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen)
    batch = tmp_path / "batch.yaml"
    _write_batch_yaml(batch, str(scen), compaction_grant=True)

    cfg = load_batch_config(batch)
    assert cfg.workers[0].compaction_grant is True


def test_explicit_compaction_grant_false_is_respected(tmp_path):
    """Tier 2: ``compaction_grant: false`` is the same as omitting it
    (= both produce the default off-by-default behaviour)."""
    scen = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen)
    batch = tmp_path / "batch.yaml"
    _write_batch_yaml(batch, str(scen), compaction_grant=False)

    cfg = load_batch_config(batch)
    assert cfg.workers[0].compaction_grant is False


# ── setup_worktree behaviour ────────────────────────────────────────────


def _mk_repo_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a minimal repo + pre-existing worktree dir.

    ``setup_worktree`` only invokes ``git worktree add`` when the dir
    doesn't already exist, so pre-creating the dir bypasses the git
    call in tests (= no real git operations needed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "reyn.local.yaml").write_text(
        "models:\n  strong:   openai/gemini-2.5-flash\n",
        encoding="utf-8",
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    return repo, wt


def _mk_worker(wt: Path, *, compaction_grant: bool) -> WorkerSpec:
    return WorkerSpec(
        name="W2",
        scenario_set="x",
        scenario_set_path="x",
        port=1,
        n_scenarios=1,
        worktree=str(wt),
        agent_prefix="x",
        compaction_grant=compaction_grant,
    )


def test_setup_worktree_injects_compaction_note_when_granted(tmp_path):
    """Tier 2: when ``compaction_grant=True``, the worker's ``reyn.local.yaml``
    carries the #1128 compaction note (config-forcing is now a no-op).

    #1128 PR-a: the grant used to inject ``trigger_total_tokens: 2000`` +
    head/tail=1 to force the removed background auto-fire path. Those config
    keys no longer exist; the grant now injects only an explanatory note and
    scenarios drive compaction explicitly via ``/compact``."""
    repo, wt = _mk_repo_and_worktree(tmp_path)
    setup_worktree(_mk_worker(wt, compaction_grant=True), "HEAD", repo)

    yaml_text = (wt / "reyn.local.yaml").read_text()
    assert "config-forcing removed" in yaml_text
    assert "trigger_total_tokens" not in yaml_text  # removed key never injected


def test_setup_worktree_omits_compaction_note_without_grant(tmp_path):
    """Tier 2: when ``compaction_grant=False`` (= default), the worker's
    ``reyn.local.yaml`` does NOT contain the #1128 compaction note."""
    repo, wt = _mk_repo_and_worktree(tmp_path)
    setup_worktree(_mk_worker(wt, compaction_grant=False), "HEAD", repo)

    yaml_text = (wt / "reyn.local.yaml").read_text()
    assert "config-forcing removed" not in yaml_text


def test_setup_worktree_b52_grants_unconditional(tmp_path):
    """Tier 2: the B52 retro grants (``permissions: web.fetch`` and
    ``sandbox: backend: noop``) inject regardless of compaction grant
    because they're per-worker env requirements unrelated to compaction."""
    repo, wt = _mk_repo_and_worktree(tmp_path)
    setup_worktree(_mk_worker(wt, compaction_grant=False), "HEAD", repo)

    yaml_text = (wt / "reyn.local.yaml").read_text()
    assert "web.fetch: allow" in yaml_text
    assert "backend: noop" in yaml_text
