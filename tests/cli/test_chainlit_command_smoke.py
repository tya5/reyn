"""Tier 1: `reyn chainlit` CLI argparse + missing-extra fallback.

Two contracts pinned:
1. ``register(sub)`` adds the ``chainlit`` subcommand with
   ``--host`` / ``--port`` / ``--watch`` / ``--headless`` flags.
2. When ``chainlit`` is not installed, ``run()`` prints a hint to
   stderr and exits with status 1 (= same UX as ``reyn web``).

The actual chainlit subprocess invocation is exercised manually
(= Test plan item). These tests cover the argparse + fallback paths
that ship in every environment.
"""
from __future__ import annotations

import argparse
import builtins
import sys

import pytest

from reyn.interfaces.cli.commands.chainlit import register, run


def _make_parser_with_chainlit() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def test_chainlit_subcommand_default_flags():
    """Tier 1: bare ``chainlit`` invocation lands all default values."""
    parser = _make_parser_with_chainlit()
    args = parser.parse_args(["chainlit"])
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.watch is False
    assert args.headless is False


def test_chainlit_subcommand_overrides_parse():
    """Tier 1: --host / --port / --watch / --headless all accepted."""
    parser = _make_parser_with_chainlit()
    args = parser.parse_args(
        ["chainlit", "--host", "0.0.0.0", "--port", "9000",
         "--watch", "--headless"]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.watch is True
    assert args.headless is True


def test_chainlit_missing_dependency_hint(monkeypatch, capsys):
    """Tier 1: import error path prints install hint and exits non-zero."""
    real_import = builtins.__import__

    def _fail_chainlit(name, *args, **kwargs):
        if name == "chainlit":
            raise ImportError("No module named 'chainlit'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_chainlit)

    ns = argparse.Namespace(
        host="127.0.0.1", port=8000, watch=False, headless=False,
    )
    with pytest.raises(SystemExit) as ei:
        run(ns)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "chainlit" in err.lower()
    assert "pip install" in err


def test_chainlit_subcommand_registered_in_build_parser():
    """Tier 1: ``reyn chainlit`` is discoverable via the top-level parser
    (= chainlit module is wired into ``commands.__init__.ALL``)."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["chainlit", "--port", "9999"])
    assert args.command == "chainlit"
    assert args.port == 9999
