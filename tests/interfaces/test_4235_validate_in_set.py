"""Tier 2: #4235 — ``reyn config validate`` widened to also report unknown/
renamed config keys in the hot-reload IN-set (``.reyn/{mcp,cron,hooks,
skills,pipelines,presentations}.yaml``), not just the policy tier
(reyn.yaml / reyn.local.yaml / ~/.reyn/config.yaml).

Companion to ``test_config_validate_migrate_command_4174.py`` (#4174 T0's
own policy-tier coverage, unchanged by this PR — its own accept/reject
tests still pass verbatim). Design (lead-coder + docs-maintainer ruling):
the two tiers are reported as SEPARATE labeled sections, never merged
into one dict — a policy-tier fix means "edit reyn.yaml and restart"; an
IN-set fix means "edit .reyn/*.yaml, applies next turn automatically" —
merging would lose exactly that "which one, and how" information.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr("reyn.config._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("reyn.config.loader._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_an_unknown_in_set_key_is_reported_in_its_own_labeled_section(
    project, capsys,
):
    """Tier 2: an unrecognized top-level key in an IN-set file (here,
    .reyn/config/hooks.yaml — any of the 6 IN-set files merges into the
    same top-level dict) is reported under the IN-SET section, separately
    from the policy-tier section, with IN-set-specific remedy text (no
    restart, no 'reyn config migrate' mention)."""
    _write_yaml(project / "reyn.yaml", "model: standard\n")
    _write_yaml(
        project / ".reyn" / "config" / "hooks.yaml",
        "totally_bogus_in_set_key: 1\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hot-reload IN-set" in out
    assert "totally_bogus_in_set_key" in out
    assert "next turn automatically" in out
    # The policy tier is clean — its OWN section must not appear at all.
    assert "Policy tier" not in out


def test_an_unknown_policy_tier_key_does_not_leak_into_the_in_set_section(
    project, capsys,
):
    """Tier 2: accept-side for the IN-set half — a policy-tier-only
    problem (reyn.yaml) produces ONLY the policy-tier section; a clean
    IN-set must not spuriously grow an empty or spurious second section."""
    _write_yaml(project / "reyn.yaml", "totally_made_up_top_level_key: 1\n")
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Policy tier" in out
    assert "totally_made_up_top_level_key" in out
    assert "Hot-reload IN-set" not in out


def test_both_tiers_unknown_are_reported_as_two_separate_sections(project, capsys):
    """Tier 2: both tiers dirty at once — two clearly separated sections,
    each naming ONLY its own tier's key (never merged into one list,
    which would lose which tier — and therefore which remedy — a finding
    belongs to)."""
    _write_yaml(project / "reyn.yaml", "totally_made_up_top_level_key: 1\n")
    _write_yaml(
        project / ".reyn" / "config" / "cron.yaml",
        "totally_bogus_in_set_key: 1\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Policy tier" in out
    assert "Hot-reload IN-set" in out
    policy_idx = out.index("Policy tier")
    in_set_idx = out.index("Hot-reload IN-set")
    # The IN-set key must not appear inside the policy-tier section (i.e.
    # before the IN-set heading) and vice versa.
    assert "totally_made_up_top_level_key" in out[policy_idx:in_set_idx]
    assert "totally_bogus_in_set_key" not in out[policy_idx:in_set_idx]
    assert "totally_bogus_in_set_key" in out[in_set_idx:]


def test_a_well_formed_in_set_produces_no_findings(project, capsys):
    """Tier 2: accept-side — real, valid IN-set files touching several
    registries never trip the new section (a false positive here would
    teach operators to ignore the report, the same #4174 T0 concern)."""
    _write_yaml(project / "reyn.yaml", "model: standard\n")
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  servers: {}\n",
    )
    _write_yaml(
        project / ".reyn" / "config" / "hooks.yaml",
        "hooks: []\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    # #4231 (C)'s disabled-by-dependency check landed on main after this
    # test was first written and shares the same "all clean" branch — the
    # message is now the unified 3-way string, not the 2-tier-only one.
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out
