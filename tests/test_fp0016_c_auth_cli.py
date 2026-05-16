"""Tier 2: FP-0016 Component C — reyn auth CLI surface.

Smoke tests for argparse registration:
- subcommand discovery
- help text presence
- --help for each subcommand
- run_list with empty store prints "(no OAuth tokens stored)"
- run_revoke on unknown key exits 1
- run_login unknown provider exits 1
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reyn.cli import build_parser

# ── helpers ────────────────────────────────────────────────────────────────────


def _parse_help(parser: argparse.ArgumentParser, *args: str) -> str:
    """Capture --help output by catching SystemExit and reading stdout."""
    import contextlib
    import io

    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc_info:
        with contextlib.redirect_stdout(buf):
            parser.parse_args(list(args))
    assert exc_info.value.code == 0
    return buf.getvalue()


# ── 1. Subcommand registration ─────────────────────────────────────────────────


def test_auth_subcommand_registered() -> None:
    """Tier 2: build_parser() returns parser with 'auth' subcommand visible in help."""
    parser = build_parser()
    help_text = parser.format_help()
    assert "auth" in help_text


def test_auth_login_help_shows_provider_arg() -> None:
    """Tier 2: reyn auth login --help output contains PROVIDER metavar."""
    parser = build_parser()
    help_text = _parse_help(parser, "auth", "login", "--help")
    assert "PROVIDER" in help_text


def test_auth_list_help() -> None:
    """Tier 2: reyn auth list --help exits 0 with no required args shown."""
    parser = build_parser()
    # Should not raise (i.e., --help exits 0 cleanly)
    help_text = _parse_help(parser, "auth", "list", "--help")
    assert "list" in help_text


def test_auth_revoke_help() -> None:
    """Tier 2: reyn auth revoke --help exits 0 and shows KEY metavar."""
    parser = build_parser()
    help_text = _parse_help(parser, "auth", "revoke", "--help")
    assert "KEY" in help_text


# ── 2. run_list — empty store path ────────────────────────────────────────────


def test_auth_list_empty_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list with empty store prints '(no OAuth tokens stored)'."""
    from reyn.cli.commands.auth import run_list

    # Point the OAuth token store to a non-existent file in tmp_path
    store = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(store))

    args = argparse.Namespace()
    run_list(args)

    captured = capsys.readouterr()
    assert "(no OAuth tokens stored)" in captured.err


# ── 3. run_revoke — unknown key exits 1 ───────────────────────────────────────


def test_auth_revoke_unknown_key_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: run_revoke on unknown key → SystemExit(1)."""
    from reyn.cli.commands.auth import run_revoke

    store = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(store))

    args = argparse.Namespace(key="nonexistent_key_xyz")
    with pytest.raises(SystemExit) as exc_info:
        run_revoke(args)
    assert exc_info.value.code == 1


# ── 4. run_login — unknown provider exits 1 ───────────────────────────────────


def test_auth_login_unknown_provider_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: run_login with unconfigured provider → SystemExit(1) with error message.

    load_config is a lazy import inside run_login, so we patch it at the
    reyn.config module level (= the target of the 'from reyn.config import
    load_config' call site inside run_login).
    """
    import reyn.config as _cfg_mod
    from reyn.cli.commands.auth import run_login
    from reyn.config import AuthConfig, ReynConfig

    # Patch load_config at the reyn.config module level.
    # run_login does `from reyn.config import load_config` on each call, so
    # patching the attribute on the module ensures the call inside run_login
    # sees our stub.

    empty_cfg = ReynConfig(auth=AuthConfig(providers={}))
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: empty_cfg)

    args = argparse.Namespace(provider="unknown_provider", save_as=None)
    with pytest.raises(SystemExit) as exc_info:
        run_login(args)
    assert exc_info.value.code == 1
