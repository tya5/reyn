"""Tier 2: --allow-unsafe-python flag on reyn chat (B6 infra fix).

Invariant: the PermissionResolver constructed by `reyn chat` must reflect the
--allow-unsafe-python CLI flag, as observed through the public
``require_python`` API:
  - Without the flag → mode='unsafe' python step raises PermissionError.
  - With the flag    → mode='unsafe' python step is allowed (given
                        matching config approval or non-interactive resolver).

This mirrors the behaviour already present on ``reyn run`` and closes the gap
that made skill_improver's copy_to_work preprocessor permanently fail in
chat mode.

FP-0014: pure → safe, trusted → unsafe; --allow-untrusted-python →
--allow-unsafe-python (legacy flag accepted as an argparse alias).
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from reyn.cli.commands.chat import register
from reyn.permissions.permissions import PermissionDecl, PermissionResolver, PythonPermission
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_parser_with_chat() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


class _AutoApproveInterventionBus:
    """Minimal real InterventionBus that auto-approves every request.

    Used so require_python can reach the approval step and succeed (or fail
    on the unsafe-python guard) without blocking on interactive I/O.
    """

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        return InterventionAnswer(choice_id="yes")


def _run(coro):
    return asyncio.run(coro)


def _make_unsafe_decl() -> PermissionDecl:
    """PermissionDecl with a single unsafe-mode python step."""
    return PermissionDecl(
        python=[PythonPermission(module="my.module", function="run", mode="unsafe")],
    )


def _make_resolver(tmp_path: Path, *, unsafe_python_allowed: bool) -> PermissionResolver:
    """Build a config-approving, non-interactive PermissionResolver."""
    return PermissionResolver(
        # pre-approve the unsafe step so the guard (not the approval) is
        # the only gate being tested.
        config_permissions={"python.unsafe": "allow"},
        project_root=tmp_path,
        interactive=False,
        unsafe_python_allowed=unsafe_python_allowed,
    )


# ── argparse: flag definition ──────────────────────────────────────────────────


def test_allow_unsafe_python_flag_parses():
    """Tier 2: --allow-unsafe-python is a valid CLI flag on reyn chat."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--allow-unsafe-python"])
    assert args.allow_unsafe_python is True


def test_allow_unsafe_python_flag_default_false():
    """Tier 2: backward compat — default chat invocation has the flag off."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat"])
    assert args.allow_unsafe_python is False


def test_legacy_allow_untrusted_python_alias_still_parses():
    """Tier 2: --allow-untrusted-python remains an alias post-FP-0014.

    The legacy flag is kept as an argparse alias targeting the same dest
    so existing scripts / invocations continue to work during the
    Track A → B transition.
    """
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--allow-untrusted-python"])
    assert args.allow_unsafe_python is True


# ── Behavioral: PermissionResolver wiring ─────────────────────────────────────


def test_unsafe_python_blocked_without_flag(tmp_path):
    """Tier 2: without --allow-unsafe-python, an unsafe-mode step raises.

    Observed via require_python public API: the resolver must raise
    PermissionError mentioning the missing flag, not silently pass.
    """
    resolver = _make_resolver(tmp_path, unsafe_python_allowed=False)
    decl = _make_unsafe_decl()
    bus = _AutoApproveInterventionBus()

    with pytest.raises(PermissionError, match="--allow-unsafe-python"):
        _run(resolver.require_python(decl, "my.module", "run", bus, skill_name="s"))


def test_unsafe_python_allowed_with_flag(tmp_path):
    """Tier 2: with --allow-unsafe-python, an unsafe-mode step succeeds.

    Observed via require_python returning a PythonPermission (no exception).
    """
    resolver = _make_resolver(tmp_path, unsafe_python_allowed=True)
    decl = _make_unsafe_decl()
    bus = _AutoApproveInterventionBus()

    perm = _run(resolver.require_python(decl, "my.module", "run", bus, skill_name="s"))
    assert perm.mode == "unsafe"
