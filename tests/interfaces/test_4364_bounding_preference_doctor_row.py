"""Tier 2: #4364 (lead-coder assignment, issue-comment candidate ②) —
``reyn doctor`` gains a declared-vs-composed row for every #4206 leaf with a
live agent-layer override receptacle (``BOUNDING_KEYS``/``PREFERENCE_KEYS``).

No mocks — drives the real ``run`` against real ``.reyn/agents/<name>/
profile.yaml`` files under ``tmp_path``, matching this command family's own
established shape (``test_4364_storage_cap_doctor_row.py``).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.interfaces.cli.commands.doctor import run
from reyn.runtime.profile import AgentProfile
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _header_and_disclosure(out: str) -> None:
    """#5658-precedent falsify witness: this section's own header + the
    session-layer-invisibility disclosure must appear in EVERY run —
    removing the whole block (or the disclosure line inside it) flips
    this assertion red, not just a narrower per-scenario one."""
    assert "Agent-layer overrides" in out
    assert "declared (project) vs. COMPOSED" in out
    assert (
        "session-layer overrides are never visible to doctor" in out
    ), "the D-2 (no live session) disclosure must be printed unconditionally"


def test_no_agents_reports_nothing_to_compose_without_fabricating(tmp_path: Path, capsys):
    """Tier 2: deny-side — no ``.reyn/agents/`` at all. Must say so plainly,
    never invent a mismatch."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    assert "no .reyn/agents/<name>/ found" in out
    assert "declared=" not in out, "no agent exists to compose against — no declared/composed line may appear"


def test_agent_with_no_override_produces_no_mismatch_line(tmp_path: Path, capsys):
    """Tier 2: accept-side (no diff) — an agent with an EMPTY bounding/
    preferences dict (the common case) narrows nothing; the row must say
    so positively (✓), not stay silent (silence reads identically to "not
    checked", the same D-3 concern every other doctor row addresses)."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    assert "declared=" not in out
    assert "no agent narrows/overrides" in out


def test_bounding_narrowing_reports_the_real_declared_and_composed_values(
    tmp_path: Path, capsys,
):
    """Tier 2: accept-side — the owner's own motivating incident
    (``llm.model``, #4206 ②). An agent's ``bounding: {model: light}``
    against a ``standard`` project default must produce a line naming
    BOTH real values — via the SAME ``compose_model_ceiling`` a real
    agent spawn uses, not a hand-computed comparison."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)  # llm.model: standard
    AgentProfile(name="alice", role="", bounding={"model": "light"}).save(
        tmp_path / ".reyn" / "agents" / "alice",
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    mismatch_line = next(line for line in out.splitlines() if "llm.model" in line and "declared=" in line)
    assert "[alice]" in mismatch_line
    assert "declared='standard'" in mismatch_line
    assert "composed='light'" in mismatch_line


def test_bounding_same_as_declared_is_not_a_mismatch(tmp_path: Path, capsys):
    """Tier 2: falsify pair for the previous test — an agent that narrows
    to the SAME value the project already has composes identically, so
    NO line is printed (this is the discriminator that proves the check
    reads the real composed value, not merely "an override key is
    present")."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)  # llm.model: standard
    AgentProfile(name="alice", role="", bounding={"model": "standard"}).save(
        tmp_path / ".reyn" / "agents" / "alice",
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    assert "declared=" not in out
    assert "no agent narrows/overrides" in out


def test_preference_override_reports_declared_and_composed(tmp_path: Path, capsys):
    """Tier 2: accept-side — axis ③ (``PREFERENCE_KEYS``), free-override,
    via the SAME ``resolve_preference`` a real turn uses."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + "output_language: english\n",
    )
    AgentProfile(
        name="alice", role="", preferences={"output_language": "japanese"},
    ).save(tmp_path / ".reyn" / "agents" / "alice")

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    mismatch_line = next(
        line for line in out.splitlines() if "output_language" in line and "declared=" in line
    )
    assert "[alice]" in mismatch_line
    assert "declared='english'" in mismatch_line
    assert "composed='japanese'" in mismatch_line


def test_invalid_profile_is_reported_by_name_not_silently_skipped(tmp_path: Path, capsys):
    """Tier 2: an agent's profile.yaml with a bounding key #4206 doesn't
    recognize fails the SAME ``AgentProfile.load`` validation a real
    session-spawn would hit — doctor must name the failing agent rather
    than silently omitting it (D-1: report the real failure, never
    swallow it) or crashing the whole command for every other agent."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    bad_dir = tmp_path / ".reyn" / "agents" / "bob"
    bad_dir.mkdir(parents=True)
    (bad_dir / "profile.yaml").write_text(
        "name: bob\nbounding:\n  not_a_real_key: standard\n", encoding="utf-8",
    )
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    _header_and_disclosure(out)
    assert "bob" in out
    assert "failed to load" in out
    # alice's own (no-op) comparison still ran — one bad profile must not
    # abort the whole command.
    assert "no agent narrows/overrides" in out


def test_measurable_leaf_keys_widened_include_every_bounding_and_preference_key():
    """Tier 2: #4364 D-3's own auditable-list discipline — widening
    coverage is a diff to ``_MEASURABLE_LEAF_KEYS``, never a silent count
    change (this module's own established rule, ``test_4364_pr3a_doctor_
    cli.py``'s sibling assertion). Every key BOUNDING_KEYS/PREFERENCE_KEYS
    resolve to must now be measurable."""
    from reyn.config.config_schema import walk_config_schema
    from reyn.config_axis import Axis
    from reyn.interfaces.cli.commands.doctor import _MEASURABLE_LEAF_KEYS

    override_leaf_keys = {
        n.key for n in walk_config_schema()
        if n.axis in (Axis.BOUNDING, Axis.PREFERENCE) and n.override_enabled
    }
    assert override_leaf_keys, "no BOUNDING/PREFERENCE override-enabled leaves found — test fixture stale"
    for key in override_leaf_keys:
        assert key in _MEASURABLE_LEAF_KEYS, (
            f"{key!r} has a live override receptacle but is not in "
            f"_MEASURABLE_LEAF_KEYS"
        )
