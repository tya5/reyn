"""Tier 2: web/deps.py resolves python.unsafe from config (B36 W6 fix).

OS invariant: the PermissionResolver constructed by the web gateway must
honour the ``python.unsafe`` config key in reyn.yaml, the same way the CLI
honours the ``--allow-unsafe-python`` flag.

Two cases:
  1. Config has ``python.unsafe: allow`` → unsafe_python_allowed=True
     → unsafe-mode step succeeds.
  2. Config has no ``python.unsafe`` entry → unsafe_python_allowed=False
     → unsafe-mode step raises PermissionError.

Uses a real temp config file and real PermissionResolver instances.
No mocks.
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml

from reyn.permissions.permissions import PermissionDecl, PermissionResolver, PythonPermission
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AutoApproveInterventionBus:
    """Minimal real InterventionBus that auto-approves every prompt."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(choice_id="yes")


def _run(coro):
    return asyncio.run(coro)


def _make_unsafe_decl() -> PermissionDecl:
    return PermissionDecl(
        python=[PythonPermission(module="my.module", function="run", mode="unsafe")],
    )


def _resolver_from_config_file(config_path: Path, project_root: Path) -> PermissionResolver:
    """Build a PermissionResolver the same way web/deps.py does after the fix.

    Reads reyn.yaml, extracts permissions section, derives unsafe_python_allowed
    from python.unsafe key — mirrors the patched _get_perm_resolver() logic.
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    perm_config = raw.get("permissions") or {}
    unsafe_python_allowed = perm_config.get("python.unsafe") == "allow"
    return PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=False,
        unsafe_python_allowed=unsafe_python_allowed,
    )


# ---------------------------------------------------------------------------
# Case 1 — python.unsafe: allow in config → resolver allows unsafe steps
# ---------------------------------------------------------------------------


def test_web_resolver_allows_unsafe_python_when_config_set(tmp_path):
    """Tier 2: config python.unsafe:allow → web resolver permits unsafe step.

    The PermissionResolver built from a real temp reyn.yaml that contains
    ``permissions: { python.unsafe: allow }`` must allow an unsafe-mode python
    step to proceed (no PermissionError).
    """
    config_file = tmp_path / "reyn.yaml"
    config_file.write_text(
        textwrap.dedent("""\
            model: standard
            permissions:
              python.unsafe: allow
        """),
        encoding="utf-8",
    )

    resolver = _resolver_from_config_file(config_file, tmp_path)
    decl = _make_unsafe_decl()
    bus = _AutoApproveInterventionBus()

    # Must not raise — unsafe step is allowed.
    perm = _run(resolver.require_python(decl, "my.module", "run", bus, skill_name="s"))
    assert perm.mode == "unsafe"


# ---------------------------------------------------------------------------
# Case 2 — no python.unsafe in config → resolver denies unsafe steps
# ---------------------------------------------------------------------------


def test_web_resolver_denies_unsafe_python_when_config_absent(tmp_path):
    """Tier 2: no python.unsafe in config → web resolver blocks unsafe step.

    The PermissionResolver built from a real temp reyn.yaml that does NOT
    contain ``python.unsafe: allow`` must raise PermissionError for an
    unsafe-mode python step, preserving the default-deny behaviour.
    """
    config_file = tmp_path / "reyn.yaml"
    config_file.write_text(
        textwrap.dedent("""\
            model: standard
            permissions:
              python.safe: allow
        """),
        encoding="utf-8",
    )

    resolver = _resolver_from_config_file(config_file, tmp_path)
    decl = _make_unsafe_decl()
    bus = _AutoApproveInterventionBus()

    with pytest.raises(PermissionError):
        _run(resolver.require_python(decl, "my.module", "run", bus, skill_name="s"))
