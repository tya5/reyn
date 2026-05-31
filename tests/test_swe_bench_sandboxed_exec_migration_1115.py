"""Tier 2: FP-0008 #1115 Stage 2 — swe_bench fully migrated off the shell op.

After A-ii (#1126, verify/report) + this PR (setup/explore/apply), every exec
phase in the swe_bench skill uses `sandboxed_exec` (routing through the run's
EnvironmentBackend) instead of the deprecated `shell` op, and the skill no
longer declares `permissions.shell`. These pins guard against a regression that
re-introduces `shell` and against the P8 violation (Control-IR JSON in a body)
that setup.md previously carried.

Real skill loaded from disk (no mocks); docstrings open "Tier 2:".
"""
from __future__ import annotations

from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.sandbox import SandboxPolicy

_SKILL_ROOT = (
    Path(__file__).parent.parent / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)
_PHASES = _SKILL_ROOT / "phases"

# Phases that run commands (exec). plan is pure file+grep (no exec).
_EXEC_PHASES = ("setup", "explore", "apply", "verify", "report")


def _skill():
    return load_dsl_skill(_SKILL_ROOT / "skill.md")


def test_no_phase_allows_shell_op() -> None:
    """Tier 2: no swe_bench phase lists the deprecated `shell` op in allowed_ops."""
    skill = _skill()
    for name, phase in skill.phases.items():
        assert "shell" not in phase.allowed_ops, (
            f"Phase '{name}' still lists 'shell' in allowed_ops: {phase.allowed_ops}. "
            f"#1115 Stage 2 migrated all exec to sandboxed_exec."
        )


def test_exec_phases_use_sandboxed_exec_with_policy() -> None:
    """Tier 2: every exec phase uses sandboxed_exec and declares a valid default_sandbox_policy."""
    skill = _skill()
    for name in _EXEC_PHASES:
        phase = skill.phases[name]
        assert "sandboxed_exec" in phase.allowed_ops, (
            f"Phase '{name}' must allow sandboxed_exec. Got: {phase.allowed_ops}"
        )
        policy = phase.default_sandbox_policy
        assert isinstance(policy, dict) and policy, (
            f"Phase '{name}' must declare a non-empty default_sandbox_policy reaching "
            f"the Phase object. Got: {policy!r}"
        )
        SandboxPolicy(**policy)  # raises on an unknown/typo'd key


def test_skill_no_longer_declares_shell_permission() -> None:
    """Tier 2: skill.permissions.shell is removed (no phase emits kind: shell)."""
    skill = _skill()
    assert skill.permissions.shell is False, (
        "swe_bench.permissions.shell must be removed after the sandboxed_exec "
        "migration — no phase emits the shell op."
    )


def test_no_phase_body_embeds_control_ir_shell_json() -> None:
    """Tier 2: no phase body embeds a Control-IR shell JSON literal (P8 cleanup).

    setup.md previously hard-coded `{"kind": "shell", "cmd": ...}` in its body —
    a P8 violation (Control IR format is OS-injected, not described in the body).
    """
    for md in _PHASES.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert '"kind": "shell"' not in text and "kind: shell" not in text, (
            f"{md.name} embeds a Control-IR shell literal — P8 violation. "
            f"Describe WHAT to run; the OS injects the op format."
        )
