"""Tier 2: #6014 follow-up gate — scripts/check_ci_role_declared.py.

Every top-level ``scripts/*.py`` must declare its own ``CI: gate|report|
manual`` role, and the declared role's own obligation (wired into a
workflow, and/or a non-empty reason naming a reader/runner) must actually
hold. See that script's own module docstring for the full design and the
issue #6014 ruling behind the three-value split (``report`` is not folded
into ``gate`` — each carries a different obligation).

Real files on a real ``tmp_path`` (mirrors ``test_5131_tui_widget_
boundary.py``'s own no-mocks placement/rationale) — no mocks. Drives both
``find_violations`` directly (the detector) and ``main()`` (the CLI
wiring), per that same file's own precedent that a green detector result
is not evidence the CLI path ever reaches it.
"""
from __future__ import annotations

from pathlib import Path

import scripts.check_ci_role_declared as check_ci_role_declared
from scripts.check_ci_role_declared import find_violations, main


def _write_script(scripts_dir: Path, name: str, docstring_body: str) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / name).write_text(f'"""{docstring_body}"""\n', encoding="utf-8")


def _write_workflow(workflows_dir: Path, name: str, content: str) -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(content, encoding="utf-8")


def test_a_script_with_no_ci_line_is_a_violation(tmp_path: Path) -> None:
    """Tier 2: the core state #6014 exists to close — a script that never
    said anything about its own CI role at all (the pre-migration state of
    all 92 real scripts, and the exact shape #5265's orphan script was
    in)."""
    scripts_dir = tmp_path / "scripts"
    _write_script(scripts_dir, "some_check.py", "Just a description, no CI line.")

    violations = find_violations(scripts_dir, tmp_path / "workflows")

    assert violations != []
    assert any("no `CI: gate|report|manual` line" in v for v in violations)


def test_two_ci_lines_are_ambiguous_not_silently_resolved(tmp_path: Path) -> None:
    """Tier 2: falsification contrast — a script cannot declare two roles;
    this must be its own violation kind, not "first one wins" or "last
    one wins" silently."""
    scripts_dir = tmp_path / "scripts"
    _write_script(
        scripts_dir, "confused.py",
        "CI: gate\nCI: manual -- someone, sometimes\n",
    )

    violations = find_violations(scripts_dir, tmp_path / "workflows")

    assert violations != []
    assert any("ambiguous" in v for v in violations)


def test_declared_gate_wired_into_a_workflow_is_clean(tmp_path: Path) -> None:
    """Tier 2: the satisfied case for `gate` — its own filename appears in
    some workflow file's text."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "wired_gate.py", "CI: gate\n")
    _write_workflow(workflows_dir, "some.yml", "run: python scripts/wired_gate.py\n")

    assert find_violations(scripts_dir, workflows_dir) == []


def test_declared_gate_not_wired_is_a_violation(tmp_path: Path) -> None:
    """Tier 2: the exact failure #6014's own census found by hand three
    times over — a script that SAYS it is a gate but appears in no
    workflow at all."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "orphan_gate.py", "CI: gate\n")
    _write_workflow(workflows_dir, "unrelated.yml", "run: python scripts/other.py\n")

    violations = find_violations(scripts_dir, workflows_dir)

    assert violations != []
    assert any("never actually wired" in v for v in violations)


def test_declared_report_wired_with_a_reader_is_clean(tmp_path: Path) -> None:
    """Tier 2: `report`'s satisfied case — wired AND names a reader."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(
        scripts_dir, "audit_tool.py",
        "CI: report -- read by lead-coder during periodic sweeps\n",
    )
    _write_workflow(workflows_dir, "some.yml", "run: python scripts/audit_tool.py\n")

    assert find_violations(scripts_dir, workflows_dir) == []


def test_declared_report_with_no_reader_is_a_violation_even_if_wired(
    tmp_path: Path,
) -> None:
    """Tier 2: the exact false-accept #6014's design flagged — collapsing
    `report` into `gate`'s single check (wired-only) would let a `report`
    with nobody actually reading it pass silently. This is issue #6014's
    ruling #1's own discriminator: `report` needs a SEPARATE obligation,
    not just `gate`'s check reused."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "unread_report.py", "CI: report\n")
    _write_workflow(workflows_dir, "some.yml", "run: python scripts/unread_report.py\n")

    violations = find_violations(scripts_dir, workflows_dir)

    assert violations != []
    assert any("no reason naming who reads it" in v for v in violations)


def test_declared_report_not_wired_is_a_violation(tmp_path: Path) -> None:
    """Tier 2: `report` still needs the wiring half of its obligation —
    a reason alone does not excuse an absent workflow reference."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "orphan_report.py", "CI: report -- someone\n")
    _write_workflow(workflows_dir, "unrelated.yml", "run: python scripts/other.py\n")

    violations = find_violations(scripts_dir, workflows_dir)

    assert violations != []
    assert any("does not appear in any" in v for v in violations)


def test_declared_manual_with_a_runner_and_act_is_clean(tmp_path: Path) -> None:
    """Tier 2: `manual`'s satisfied case — no wiring required at all, only
    a non-empty reason naming who runs it and by what act."""
    scripts_dir = tmp_path / "scripts"
    _write_script(
        scripts_dir, "human_tool.py",
        "CI: manual -- run by backlog-watcher during an ad hoc triage sweep\n",
    )

    assert find_violations(scripts_dir, tmp_path / "workflows") == []


def test_declared_manual_with_no_reason_is_state_4(tmp_path: Path) -> None:
    """Tier 2: the exact #5265-shaped trap #6014's design names explicitly
    — a `manual` declaration with nobody named as the runner is "someone's
    habit" wearing `manual`'s clothes, not a real declaration. This is the
    discriminator's own falsifiability: writing `CI: manual` alone must
    NOT silently pass."""
    scripts_dir = tmp_path / "scripts"
    _write_script(scripts_dir, "habit_only.py", "CI: manual\n")

    violations = find_violations(scripts_dir, tmp_path / "workflows")

    assert violations != []
    assert any("no reason naming who runs it" in v for v in violations)


def test_a_script_spawned_by_a_directly_wired_sibling_script_counts_as_wired(
    tmp_path: Path,
) -> None:
    """Tier 2: the real edge case #6014's own census found —
    ``wheel_parity_probe.py``/``wheel_plugin_install_probe.py`` are never
    named in any workflow file themselves; they are spawned by
    ``wheel_reachability_smoke.py``, which IS directly workflow-wired.
    One level of transitive closure (see ``_wiring_haystack``'s own
    docstring) must resolve this without naming either script here."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "spawned_probe.py", "CI: gate\n")
    _write_script(
        scripts_dir, "parent_smoke.py",
        "CI: gate\n\nSubprocess target: scripts/spawned_probe.py\n",
    )
    _write_workflow(workflows_dir, "some.yml", "run: python scripts/parent_smoke.py\n")

    assert find_violations(scripts_dir, workflows_dir) == []


def test_a_script_reached_only_via_an_unwired_siblings_docstring_is_still_a_violation(
    tmp_path: Path,
) -> None:
    """Tier 2: falsification contrast — the transitive closure is ONE
    level, gated on the sibling ITSELF being directly wired; a mention
    inside a script that is NOT directly wired must not launder an
    unrelated script into "wired" for free."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "spawned_probe.py", "CI: gate\n")
    _write_script(
        scripts_dir, "unwired_sibling.py",
        "CI: manual -- run by hand\n\nSee also scripts/spawned_probe.py.\n",
    )
    _write_workflow(workflows_dir, "unrelated.yml", "run: python scripts/other.py\n")

    violations = find_violations(scripts_dir, workflows_dir)

    assert violations != []
    assert any("spawned_probe.py" in v for v in violations)


def test_underscore_prefixed_files_are_excluded_from_the_population(
    tmp_path: Path,
) -> None:
    """Tier 2: a helper module (the existing `_markers.py`-shaped
    convention) carries no CI role to declare at all — it must not even
    enter the population, so a bare undeclared `_helper.py` is not a
    violation."""
    scripts_dir = tmp_path / "scripts"
    _write_script(scripts_dir, "_helper.py", "No CI line, and none needed.")

    assert find_violations(scripts_dir, tmp_path / "workflows") == []


def test_main_exits_nonzero_when_the_real_scripts_dir_has_a_violation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the blocking gap ``test_5131_tui_widget_boundary.py``'s own
    precedent names directly — a green ``find_violations`` result proves
    nothing about whether ``main()``'s own CLI path reaches it. Monkeypatch
    the MODULE-LEVEL ``_SCRIPTS_DIR``/``_WORKFLOWS_DIR`` (fresh name
    lookups inside ``main()``, not bound defaults) and drive ``main()``
    itself."""
    scripts_dir = tmp_path / "scripts"
    _write_script(scripts_dir, "undeclared.py", "No CI line here either.")
    monkeypatch.setattr(check_ci_role_declared, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(
        check_ci_role_declared, "_WORKFLOWS_DIR", tmp_path / "workflows",
    )

    assert main() == 1


def test_main_exits_zero_on_a_fully_compliant_fixture_population(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: falsification contrast for the test above — a fixture
    population with every script satisfying its own declared role still
    passes through ``main()``'s full CLI path to exit 0."""
    scripts_dir = tmp_path / "scripts"
    workflows_dir = tmp_path / "workflows"
    _write_script(scripts_dir, "clean_gate.py", "CI: gate\n")
    _write_workflow(workflows_dir, "some.yml", "run: python scripts/clean_gate.py\n")
    monkeypatch.setattr(check_ci_role_declared, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(check_ci_role_declared, "_WORKFLOWS_DIR", workflows_dir)

    assert main() == 0


def test_main_refuses_to_read_an_empty_population_as_clean(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: vacuity guard (#4846-class) — a scan that finds ZERO
    scripts is a scanner failure (a moved directory, a broken glob), not a
    clean population; must not read as green."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    monkeypatch.setattr(check_ci_role_declared, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(
        check_ci_role_declared, "_WORKFLOWS_DIR", tmp_path / "workflows",
    )

    assert main() == 1
