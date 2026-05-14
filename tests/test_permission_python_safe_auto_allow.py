"""Tier 2: OS invariant — mode:safe stdlib auto-allow parity with mode:unsafe.

Verifies that PermissionResolver.require_python for mode:safe auto-approves in
non-interactive context when unsafe_python_allowed=True (i.e. `reyn run`
stdlib path), mirroring the existing mode:unsafe auto-allow behaviour.

See: permissions.py require_python safe-mode fix (2026-05-15).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver, PythonPermission
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

# ── Helpers ───────────────────────────────────────────────────────────────────


class _AutoDenyBus:
    """Real InterventionBus that auto-denies every prompt (non-interactive guard)."""

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id="no")


def _make_decl(module: str = "./aggregate.py", function: str = "collect", mode: str = "safe") -> PermissionDecl:
    return PermissionDecl(
        python=[PythonPermission(module=module, function=function, mode=mode)],
    )


def _run(coro):
    return asyncio.run(coro)


# ── Test cases ────────────────────────────────────────────────────────────────


def test_safe_python_auto_allowed_in_stdlib_non_interactive(tmp_path: Path) -> None:
    """Tier 2: safe-mode step auto-approves when unsafe_python_allowed=True, non-interactive.

    This is the stdlib `reyn run` path. mode:safe should be at least as permissive
    as mode:unsafe in the same context — it is the more restricted capability, so
    there is no reason to be *more* restrictive than unsafe in auto-allow scope.
    """
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
        unsafe_python_allowed=True,
    )
    decl = _make_decl(mode="safe")
    bus = _AutoDenyBus()

    # Must NOT raise — safe step auto-approved on the stdlib non-interactive path.
    result = _run(resolver.require_python(decl, "./aggregate.py", "collect", bus, skill_name="ops_report"))
    assert result is not None
    assert result.mode == "safe"
    # No prompt was issued (non-interactive auto-allow must not touch the bus).
    assert bus.requests == []


def test_safe_python_denied_in_non_stdlib_non_interactive(tmp_path: Path) -> None:
    """Tier 2: safe-mode step is denied when unsafe_python_allowed=False, non-interactive.

    Without the stdlib flag, non-interactive context has no implicit approval
    and the bus auto-denies, so require_python must raise PermissionError.
    """
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
        unsafe_python_allowed=False,
    )
    decl = _make_decl(mode="safe")
    bus = _AutoDenyBus()

    with pytest.raises(PermissionError, match="denied by user"):
        _run(resolver.require_python(decl, "./aggregate.py", "collect", bus, skill_name="ops_report"))


def test_safe_python_explicit_deny_overrides_auto_allow(tmp_path: Path) -> None:
    """Tier 2: explicit session deny wins over auto-allow (setdefault must not clobber it).

    Verifies that the fix uses setdefault — so if _session already has False
    for the key (an explicit deny), the auto-allow path does not overwrite it.
    """
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
        unsafe_python_allowed=True,
    )
    # Manually pre-seed an explicit deny in the session dict (public interface
    # via _session is an implementation detail, but this is a whitebox OS
    # invariant test — the session dict state IS the observable contract here).
    key = "ops_report/python.safe/./aggregate.py:collect"
    resolver._session[key] = False

    decl = _make_decl(mode="safe")
    bus = _AutoDenyBus()

    # The explicit deny must survive — auto-allow must not clobber it.
    with pytest.raises(PermissionError, match="denied by user"):
        _run(resolver.require_python(decl, "./aggregate.py", "collect", bus, skill_name="ops_report"))


def test_safe_python_still_requires_approval_when_interactive(tmp_path: Path) -> None:
    """Tier 2: interactive context does NOT auto-allow safe steps via the new path.

    The new auto-allow is gated on non-interactive only. In interactive mode,
    even with unsafe_python_allowed=True, require_python must route through
    _approve → _prompt, NOT the pre-seed shortcut.

    Observable: the bus receives a prompt request (the bus auto-denies, so
    require_python raises). If the new path incorrectly auto-approved in
    interactive mode, the bus would receive zero requests and no error would
    be raised — that would be the failure signal.
    """
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=True,
        unsafe_python_allowed=True,
    )
    decl = _make_decl(mode="safe")
    bus = _AutoDenyBus()

    # In interactive mode the auto-deny bus causes a denial, so PermissionError is raised.
    with pytest.raises(PermissionError, match="denied by user"):
        _run(resolver.require_python(decl, "./aggregate.py", "collect", bus, skill_name="ops_report"))

    # The key assertion: the bus was consulted (proves routing went through _prompt,
    # not the non-interactive auto-allow shortcut).
    assert len(bus.requests) == 1
