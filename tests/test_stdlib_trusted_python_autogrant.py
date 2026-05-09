"""Tier 2: stdlib skill trusted-python auto-grant invariant.

OS invariant: stdlib skills (bundled under src/reyn/stdlib/skills/) are
shipped by the Reyn team and therefore trusted by construction.  The CLI must
auto-allow their trusted Python preprocessor steps without requiring the user
to pass --allow-untrusted-python.  User-provided skills (reyn/local/,
reyn/project/) must still require the flag — the safety gate must not regress.

Three sub-invariants:
  - is_stdlib_skill() returns True for a path inside stdlib_root()
  - is_stdlib_skill() returns False for a path outside stdlib_root()
  - is_stdlib_skill() returns False for a reyn/project/ path

Four behavioral invariants (via PermissionResolver — the same resolver that
``reyn mcp install`` and ``reyn run`` construct):
  - stdlib skill dir → trusted_python_allowed=True → trusted step succeeds
    (config-approved path)
  - non-stdlib skill dir → trusted_python_allowed=False (default) → trusted
    step raises PermissionError (safety regression guard)
  - --non-interactive + trusted_python_allowed=True → silent allow (no prompt)
  - --non-interactive + trusted_python_allowed=False → silent deny (safety)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver, PythonPermission
from reyn.skill.skill_paths import is_stdlib_skill, stdlib_root
from reyn.user_intervention import InterventionAnswer, UserIntervention


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AutoApproveInterventionBus:
    """Minimal real InterventionBus that auto-approves every request."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(choice_id="yes")


def _run(coro):
    return asyncio.run(coro)


def _make_trusted_decl() -> PermissionDecl:
    return PermissionDecl(
        python=[PythonPermission(module="./step.py", function="run", mode="trusted")],
    )


def _make_resolver(*, trusted_python_allowed: bool, tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"python.trusted": "allow"},
        project_root=tmp_path,
        interactive=False,
        trusted_python_allowed=trusted_python_allowed,
    )


# ---------------------------------------------------------------------------
# is_stdlib_skill() — path detection
# ---------------------------------------------------------------------------


def test_is_stdlib_skill_true_for_stdlib_path():
    """Tier 2: is_stdlib_skill returns True for a path inside stdlib_root()."""
    stdlib_skill_dir = stdlib_root() / "skills" / "mcp_install"
    assert is_stdlib_skill(stdlib_skill_dir) is True


def test_is_stdlib_skill_false_for_user_path(tmp_path):
    """Tier 2: is_stdlib_skill returns False for a user-provided skill path.

    Safety regression guard: paths outside stdlib/ must NOT be auto-trusted.
    """
    user_skill_dir = tmp_path / "reyn" / "local" / "my_custom_skill"
    user_skill_dir.mkdir(parents=True)
    assert is_stdlib_skill(user_skill_dir) is False


def test_is_stdlib_skill_false_for_project_path(tmp_path):
    """Tier 2: is_stdlib_skill returns False for a reyn/project/ skill path."""
    project_skill_dir = tmp_path / "reyn" / "project" / "my_skill"
    project_skill_dir.mkdir(parents=True)
    assert is_stdlib_skill(project_skill_dir) is False


# ---------------------------------------------------------------------------
# Behavioral: stdlib auto-trust via PermissionResolver
# ---------------------------------------------------------------------------


def test_stdlib_skill_trusted_python_allowed(tmp_path):
    """Tier 2: a stdlib skill's trusted python step succeeds without the CLI flag.

    This is the core invariant: ``reyn mcp install`` (and ``reyn run`` on a
    stdlib skill) constructs the resolver with trusted_python_allowed derived
    from is_stdlib_skill().  For a stdlib skill the result must be True, so
    require_python must succeed.
    """
    stdlib_skill_dir = stdlib_root() / "skills" / "mcp_install"
    auto_trust = is_stdlib_skill(stdlib_skill_dir)
    # Invariant: the stdlib detection must yield True so the resolver is built
    # with trusted_python_allowed=True.
    assert auto_trust is True, (
        "is_stdlib_skill returned False for a known stdlib skill — "
        "the auto-trust logic in run_install / run will build a resolver "
        "with trusted_python_allowed=False and block the skill."
    )

    resolver = _make_resolver(trusted_python_allowed=auto_trust, tmp_path=tmp_path)
    decl = _make_trusted_decl()
    bus = _AutoApproveInterventionBus()

    # Must not raise — stdlib trusted python steps are allowed without the flag.
    perm = _run(resolver.require_python(decl, "./step.py", "run", bus, skill_name="mcp_install"))
    assert perm.mode == "trusted"


def test_user_skill_trusted_python_still_requires_flag(tmp_path):
    """Tier 2: a user-provided skill's trusted python step still requires the flag.

    Safety regression guard: the stdlib auto-trust must not weaken the gate for
    skills outside stdlib/.  A user skill resolved via reyn/local/ must continue
    to produce trusted_python_allowed=False (i.e. require the explicit flag).
    """
    user_skill_dir = tmp_path / "reyn" / "local" / "my_skill"
    user_skill_dir.mkdir(parents=True)
    auto_trust = is_stdlib_skill(user_skill_dir)
    # Invariant: non-stdlib → auto_trust must be False.
    assert auto_trust is False, (
        "is_stdlib_skill returned True for a non-stdlib user skill path — "
        "the auto-trust logic would grant trusted python to user-supplied code."
    )

    resolver = _make_resolver(trusted_python_allowed=auto_trust, tmp_path=tmp_path)
    decl = _make_trusted_decl()
    bus = _AutoApproveInterventionBus()

    # Must raise — user skill trusted python steps require the explicit flag.
    with pytest.raises(PermissionError, match="--allow-untrusted-python"):
        _run(resolver.require_python(decl, "./step.py", "run", bus, skill_name="my_skill"))


# ---------------------------------------------------------------------------
# Non-interactive mode: silent allow / silent deny
# ---------------------------------------------------------------------------


def _make_resolver_no_config(*, trusted_python_allowed: bool, tmp_path: Path) -> PermissionResolver:
    """Resolver without any config-level python.trusted grant.

    This exercises the real non-interactive gate (no config bypass),
    mirroring what ``reyn mcp install --non-interactive`` actually constructs.
    """
    return PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
        trusted_python_allowed=trusted_python_allowed,
    )


class _DenyAllInterventionBus:
    """Minimal bus that errors if called — proves no prompt was issued."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        raise AssertionError(
            f"InterventionBus.request was called in non-interactive mode: {iv}"
        )


def test_non_interactive_stdlib_trusted_python_silent_allow(tmp_path):
    """Tier 2: --non-interactive + trusted_python_allowed=True → silent allow.

    OS invariant: stdlib skills running under --non-interactive must not block
    on a prompt.  require_python must succeed without firing InterventionBus.
    """
    resolver = _make_resolver_no_config(trusted_python_allowed=True, tmp_path=tmp_path)
    decl = _make_trusted_decl()
    bus = _DenyAllInterventionBus()

    # Must not raise and must not call the bus.
    perm = _run(resolver.require_python(decl, "./step.py", "run", bus, skill_name="mcp_install"))
    assert perm.mode == "trusted"


def test_non_interactive_user_skill_trusted_python_silent_deny(tmp_path):
    """Tier 2: --non-interactive + trusted_python_allowed=False → silent deny.

    Safety regression guard: user-supplied skills must still be blocked in
    non-interactive mode.  No prompt fires — the flag-absent check raises
    PermissionError before any bus interaction.
    """
    resolver = _make_resolver_no_config(trusted_python_allowed=False, tmp_path=tmp_path)
    decl = _make_trusted_decl()
    bus = _DenyAllInterventionBus()

    # Must raise with the --allow-untrusted-python message (not "denied by user").
    with pytest.raises(PermissionError, match="--allow-untrusted-python"):
        _run(resolver.require_python(decl, "./step.py", "run", bus, skill_name="my_skill"))
