"""Tier 2: --allow-unsafe-python CLI flag on reyn chat.

Pins that the flag is registered, defaults to False, and the legacy
--allow-untrusted-python alias still parses (FP-0014 compat).
The behavioral gate (unsafe step raises without the flag) is enforced
at the PermissionResolver level; these tests cover CLI wiring only.
"""
from __future__ import annotations

import argparse

from reyn.interfaces.cli.commands.chat import register


def _make_parser_with_chat() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def test_allow_unsafe_python_flag_parses() -> None:
    """Tier 2: --allow-unsafe-python is a valid CLI flag on reyn chat."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--allow-unsafe-python"])
    assert args.allow_unsafe_python is True


def test_allow_unsafe_python_flag_default_false() -> None:
    """Tier 2: backward compat — default chat invocation has the flag off."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat"])
    assert args.allow_unsafe_python is False


def test_legacy_allow_untrusted_python_alias_still_parses() -> None:
    """Tier 2: --allow-untrusted-python remains an alias post-FP-0014.

    The legacy flag is kept as an argparse alias targeting the same dest
    so existing scripts / invocations continue to work during the
    Track A → B transition.
    """
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--allow-untrusted-python"])
    assert args.allow_unsafe_python is True
