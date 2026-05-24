"""Tier 2: ``load_batch_config`` derives ``n_scenarios`` from the actual
scenario yaml, with caller-declared overrides validated.

Pinned invariants:

- When the batch yaml omits ``n_scenarios``, the loader fills it from
  the on-disk scenario yaml's actual count.
- When the batch yaml declares a ``n_scenarios`` that matches the
  on-disk count, the loader silently accepts it (= backwards compat
  with existing batch_b4*.yaml configs).
- When the batch yaml declares a ``n_scenarios`` that **mismatches**
  the on-disk count, the loader:
  - emits a ``UserWarning`` explaining the drift
  - **uses the on-disk count** as the authoritative value
- When the scenario yaml is missing / unreadable / malformed, the
  loader falls back to the caller-declared value (= best-effort, no
  hard crash on partial filesystem state).

Motivation: B45/B46/B47 W6 entries had ``n_scenarios: 7`` hardcoded
against ``plan_mode.yaml`` which has 3 scenarios. The mismatch caused
sub-agents to dispatch 7 agents but only 3 scenarios existed → 4
``Blocked`` verdicts per batch. The B47 retrospective documented this
as a carry-over; this fix closes it at the loader layer so future
batch yamls cannot drift without a visible warning.

testing.ja.md compliance:
- No mocks. Real ``load_batch_config`` against tmp_path yamls.
- No private-state assertions; pins the public ``WorkerSpec.n_scenarios``.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_batch_config import load_batch_config  # noqa: E402


def _write_scenario_yaml(path: Path, n: int) -> None:
    """Write a minimal scenario yaml with ``n`` scenario entries."""
    scenarios = "\n".join(
        f"  - id: scen_{i}\n    input: hello\n    expected: world"
        for i in range(n)
    )
    path.write_text(
        "metadata:\n  name: test\nscenarios:\n" + scenarios,
        encoding="utf-8",
    )


def _write_batch_yaml(
    path: Path,
    scenario_yaml_path: str,
    *,
    n_scenarios_declared: int | None = None,
) -> None:
    """Write a minimal batch yaml referencing the given scenario yaml.

    When ``n_scenarios_declared`` is None, the worker block omits the
    field entirely (= exercising the auto-derive path)."""
    n_field = (
        f"    n_scenarios: {n_scenarios_declared}\n"
        if n_scenarios_declared is not None
        else ""
    )
    path.write_text(
        f"""batch:
  name: TEST
  date: "2026-05-21"
  head: deadbeef
workers:
  - name: W1
    scenario_set: test_set
    scenario_set_path: {scenario_yaml_path}
    port: 8001
{n_field}    worktree: /tmp/test-wt
    agent_prefix: test-w1-s
past_batches: []
journal_dir: /tmp/test-journal
""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Auto-derive: omitted n_scenarios is filled from the scenario yaml
# ---------------------------------------------------------------------------


def test_omitted_n_scenarios_derived_from_scenario_yaml(tmp_path):
    """Tier 2b: batch yaml may omit ``n_scenarios`` entirely;
    the loader fills it from the on-disk scenario yaml count."""
    scen_path = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen_path, n=5)
    batch_path = tmp_path / "batch.yaml"
    _write_batch_yaml(batch_path, str(scen_path), n_scenarios_declared=None)

    cfg = load_batch_config(batch_path)
    assert cfg.workers[0].n_scenarios == 5, (
        f"omitted n_scenarios should be derived as 5; got "
        f"{cfg.workers[0].n_scenarios}"
    )


# ---------------------------------------------------------------------------
# Validation: declared n_scenarios is overridden when it mismatches
# ---------------------------------------------------------------------------


def test_declared_n_scenarios_matching_actual_silent(tmp_path):
    """Tier 2: backwards-compat — declaring ``n_scenarios`` that matches
    the actual count loads silently (= no warning)."""
    scen_path = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen_path, n=3)
    batch_path = tmp_path / "batch.yaml"
    _write_batch_yaml(batch_path, str(scen_path), n_scenarios_declared=3)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_batch_config(batch_path)
        mismatch_warnings = [
            w for w in caught
            if "n_scenarios" in str(w.message) and "does not match" in str(w.message)
        ]
        assert not mismatch_warnings, (
            f"matching declared n_scenarios should be silent; got "
            f"{[str(w.message) for w in mismatch_warnings]}"
        )
    assert cfg.workers[0].n_scenarios == 3


def test_declared_n_scenarios_mismatched_warns_and_overrides(tmp_path):
    """Tier 2b: the W6 drift case — declared ``n_scenarios: 7`` against a
    3-scenario yaml emits a warning and uses the actual count (3) as
    authoritative."""
    scen_path = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen_path, n=3)
    batch_path = tmp_path / "batch.yaml"
    _write_batch_yaml(batch_path, str(scen_path), n_scenarios_declared=7)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_batch_config(batch_path)
        mismatch_warnings = [
            w for w in caught
            if "n_scenarios" in str(w.message) and "does not match" in str(w.message)
        ]
        assert mismatch_warnings, (
            f"mismatched declared n_scenarios should emit at least one "
            f"warning; got none. All warnings: "
            f"{[str(w.message) for w in caught]}"
        )
        assert "7" in str(mismatch_warnings[0].message)
        assert "3" in str(mismatch_warnings[0].message)

    # Authoritative value is the on-disk actual count, not the declared one.
    assert cfg.workers[0].n_scenarios == 3, (
        f"mismatched n_scenarios should be overridden to 3 (actual); "
        f"got {cfg.workers[0].n_scenarios}"
    )


# ---------------------------------------------------------------------------
# Fallback: scenario yaml unreadable → use declared
# ---------------------------------------------------------------------------


def test_missing_scenario_yaml_falls_back_to_declared(tmp_path):
    """Tier 2: if the scenario yaml file doesn't exist, the loader
    falls back to the caller-declared value rather than crashing.
    This keeps best-effort behavior for batch yamls that point at
    not-yet-created scenario files (= dispatch-time prep)."""
    batch_path = tmp_path / "batch.yaml"
    _write_batch_yaml(
        batch_path, "/nonexistent/scenarios.yaml", n_scenarios_declared=4,
    )
    cfg = load_batch_config(batch_path)
    assert cfg.workers[0].n_scenarios == 4


def test_malformed_scenario_yaml_falls_back_to_declared(tmp_path):
    """Tier 2: a malformed scenario yaml (= not a mapping, or no
    ``scenarios`` list) also falls back. Catches partial-filesystem
    state without hard-crashing the batch load."""
    scen_path = tmp_path / "scenarios.yaml"
    scen_path.write_text("just a string, not a dict\n", encoding="utf-8")
    batch_path = tmp_path / "batch.yaml"
    _write_batch_yaml(batch_path, str(scen_path), n_scenarios_declared=2)

    cfg = load_batch_config(batch_path)
    assert cfg.workers[0].n_scenarios == 2


# ---------------------------------------------------------------------------
# Required-field regression guard
# ---------------------------------------------------------------------------


def test_required_fields_still_enforced(tmp_path):
    """Tier 2: dropping ``n_scenarios`` is now allowed (= auto-derived),
    but the other required worker fields (``name``, ``port``,
    ``worktree``, etc.) still raise ValueError on omission."""
    scen_path = tmp_path / "scenarios.yaml"
    _write_scenario_yaml(scen_path, n=2)
    batch_path = tmp_path / "batch.yaml"
    # Omit `port` — should raise
    batch_path.write_text(
        f"""batch:
  name: TEST
  date: "2026-05-21"
  head: deadbeef
workers:
  - name: W1
    scenario_set: test_set
    scenario_set_path: {scen_path}
    worktree: /tmp/test-wt
    agent_prefix: test-w1-s
past_batches: []
journal_dir: /tmp/test-journal
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="port"):
        load_batch_config(batch_path)
