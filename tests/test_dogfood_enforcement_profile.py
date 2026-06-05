"""Tier 2: ``enforcement_profile`` opt-in per worker (#1335 strict-permission profile).

Pinned invariants:

- ``WorkerSpec.enforcement_profile`` defaults to False when the batch yaml
  omits the field (backwards compat); ``enforcement_profile: true`` propagates.
- With enforcement_profile=True, ``setup_worktree`` prepares the worker's
  reyn.local.yaml WITHOUT the routing blanket grants — no ``file.write: allow``
  (stripped from the source), no ``sandbox.backend: noop``, no ``web.fetch:
  allow`` — so the REAL permission resolver + Seatbelt/Landlock gates fire for
  permission/sandbox-enforcement scenarios (carve-out, broad-read, …).
- With enforcement_profile=False (default), the routing grants ARE injected
  (the existing routing/functional env), unchanged.

No mocks: real ``load_batch_config`` against tmp yamls + real ``setup_worktree``
writing to a tmp dir (pre-created worktree bypasses ``git worktree add``).
"""
from __future__ import annotations

import sys
from pathlib import Path

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


def _write_batch_yaml(path: Path, scenario_yaml_path: str, *, enforcement: bool | None) -> None:
    field = (
        f"    enforcement_profile: {'true' if enforcement else 'false'}\n"
        if enforcement is not None
        else ""
    )
    path.write_text(
        f"""batch:
  name: TEST
  date: "2026-06-05"
  head: deadbeef
workers:
  - name: W1
    scenario_set: test_set
    scenario_set_path: {scenario_yaml_path}
    port: 8001
    worktree: /tmp/test-wt
    agent_prefix: test-w1-s
{field}past_batches: []
journal_dir: /tmp/test-journal
""",
        encoding="utf-8",
    )


def _mk_repo_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Source reyn.local.yaml carries the swe_bench blanket file.write:allow (#1051).
    (repo / "reyn.local.yaml").write_text(
        "models:\n  strong:   openai/gemini-2.5-flash\n"
        "permissions:\n  file.write: allow\n",
        encoding="utf-8",
    )
    wt = tmp_path / "wt"
    wt.mkdir()  # pre-create → setup_worktree skips git worktree add
    return repo, wt


def _mk_worker(wt: Path, *, enforcement_profile: bool) -> WorkerSpec:
    return WorkerSpec(
        name="W1",
        scenario_set="x",
        scenario_set_path="x",
        port=1,
        n_scenarios=1,
        worktree=str(wt),
        agent_prefix="x",
        enforcement_profile=enforcement_profile,
    )


# ── loader contract ──────────────────────────────────────────────────────


def test_omitted_enforcement_profile_defaults_false(tmp_path):
    """Tier 2: omitting enforcement_profile → False (legacy batch compat)."""
    scen = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen)
    batch = tmp_path / "batch.yaml"
    _write_batch_yaml(batch, str(scen), enforcement=None)
    cfg = load_batch_config(batch)
    assert cfg.workers[0].enforcement_profile is False


def test_explicit_enforcement_profile_true_respected(tmp_path):
    """Tier 2: enforcement_profile: true propagates to WorkerSpec."""
    scen = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen)
    batch = tmp_path / "batch.yaml"
    _write_batch_yaml(batch, str(scen), enforcement=True)
    cfg = load_batch_config(batch)
    assert cfg.workers[0].enforcement_profile is True


# ── setup_worktree behaviour ────────────────────────────────────────────


def test_enforcement_profile_strips_blanket_grants(tmp_path):
    """Tier 2: enforcement_profile=True → no file.write:allow / noop / web.fetch:allow
    so the REAL permission + Seatbelt/Landlock gates fire."""
    repo, wt = _mk_repo_and_worktree(tmp_path)
    setup_worktree(_mk_worker(wt, enforcement_profile=True), "HEAD", repo)
    text = (wt / "reyn.local.yaml").read_text()
    assert "file.write: allow" not in text  # stripped from source
    assert "backend: noop" not in text  # routing grant NOT injected
    assert "web.fetch: allow" not in text  # routing grant NOT injected
    assert "enforcement profile" in text  # explanatory marker present
    # strong-tier flash-lite force still applies.
    assert "strong:   openai/gemini-2.5-flash-lite" in text


def test_default_profile_keeps_routing_grants(tmp_path):
    """Tier 2: enforcement_profile=False (default) → routing grants injected
    (existing routing/functional env, unchanged)."""
    repo, wt = _mk_repo_and_worktree(tmp_path)
    setup_worktree(_mk_worker(wt, enforcement_profile=False), "HEAD", repo)
    text = (wt / "reyn.local.yaml").read_text()
    assert "web.fetch: allow" in text
    assert "backend: noop" in text
    assert "file.write: allow" in text  # source blanket kept in routing profile
