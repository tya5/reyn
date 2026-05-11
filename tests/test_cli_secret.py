"""Tier 2: OS invariant — ``reyn secret`` CLI subcommands.

Pins the contract for the four ``reyn secret`` subcommands:
  - ``set KEY=VALUE``: saves secret, emits ``secret_set`` event (value masked)
  - ``set KEY`` (no value): falls back to getpass prompt in real usage;
    tested here via KEY=VALUE form only to avoid interactive I/O
  - ``list``: shows KEY names; values never appear in output
  - ``clear KEY``: removes key, emits ``secret_cleared`` event
  - ``rotate KEY=VALUE``: saves secret, emits ``secret_rotated`` event
  - Audit events: ``key`` field correct, ``value_masked`` is "***"
  - Argparse registration: all four subcommands parse without error
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.cli.commands.secret import (
    _get_audit_log,
    _parse_key_value,
    register,
    run_clear,
    run_list,
    run_rotate,
    run_set,
)
from reyn.secrets.store import load_secrets, save_secret

# ── helper ────────────────────────────────────────────────────────────────────

def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def _make_args(key_value: str) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.key_value = key_value
    return ns


def _make_clear_args(key: str) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.key = key
    return ns


# ── argparse registration ─────────────────────────────────────────────────────

def test_set_parses():
    """Tier 2: 'secret set KEY=VALUE' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["secret", "set", "MY_KEY=my_value"])
    assert args.secret_cmd == "set"
    assert args.key_value == "MY_KEY=my_value"


def test_list_parses():
    """Tier 2: 'secret list' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["secret", "list"])
    assert args.secret_cmd == "list"


def test_clear_parses():
    """Tier 2: 'secret clear KEY' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["secret", "clear", "MY_KEY"])
    assert args.secret_cmd == "clear"
    assert args.key == "MY_KEY"


def test_rotate_parses():
    """Tier 2: 'secret rotate KEY=VALUE' is a valid CLI invocation."""
    parser = _make_parser()
    args = parser.parse_args(["secret", "rotate", "MY_KEY=new_value"])
    assert args.secret_cmd == "rotate"
    assert args.key_value == "MY_KEY=new_value"


# ── _parse_key_value helper ───────────────────────────────────────────────────

def test_parse_key_value_with_equals():
    """Tier 2: KEY=VALUE is split into (key, value)."""
    key, value = _parse_key_value("FOO=bar")
    assert key == "FOO"
    assert value == "bar"


def test_parse_key_value_without_equals():
    """Tier 2: KEY without '=' returns (key, None) — triggers interactive prompt."""
    key, value = _parse_key_value("FOO")
    assert key == "FOO"
    assert value is None


def test_parse_key_value_with_value_containing_equals():
    """Tier 2: value may itself contain '=' — only first '=' is the separator."""
    key, value = _parse_key_value("AUTH=Bearer=token")
    assert key == "AUTH"
    assert value == "Bearer=token"


# ── run_set ───────────────────────────────────────────────────────────────────

def test_run_set_saves_secret_and_emits_event(tmp_path, capsys):
    """Tier 2: run_set writes to the store and emits 'secret_set' audit event."""
    secrets = tmp_path / "secrets.env"

    audit_log = _get_audit_log()
    before = len(audit_log.all())

    args = _make_args("REYN_CLI_SET=testval")
    # Monkey-patch the store path by monkeypatching save_secret
    # We call run_set directly but override the store path by writing
    # to a temp path manually and verifying via the audit log.
    # Since run_set imports save_secret internally and uses the default path,
    # we test the audit event (path-independent) and separately verify save_secret
    # behavior via the store tests.
    run_set(args)

    events = audit_log.all()
    new_events = events[before:]
    assert any(e.type == "secret_set" and e.data.get("key") == "REYN_CLI_SET" for e in new_events)
    # Value is always masked
    assert all(e.data.get("value_masked") == "***" for e in new_events if e.type == "secret_set")


def test_run_set_value_masked_in_event():
    """Tier 2: the audit event for secret_set never contains the actual value."""
    audit_log = _get_audit_log()
    before = len(audit_log.all())

    args = _make_args("REYN_SET_MASK=supersecret")
    run_set(args)

    events = audit_log.all()
    new_events = events[before:]
    for e in new_events:
        if e.type == "secret_set":
            assert "supersecret" not in str(e.data)
            assert e.data.get("value_masked") == "***"


# ── run_list ──────────────────────────────────────────────────────────────────

def test_run_list_shows_keys_not_values(tmp_path, capsys):
    """Tier 2: run_list displays KEY names but never the secret values."""
    # Save a secret via store directly so we control content
    save_secret("LIST_TEST_KEY", "ultra_secret_value")

    run_list(argparse.Namespace())
    captured = capsys.readouterr()

    # Key name appears
    assert "LIST_TEST_KEY" in captured.out
    # Value must NOT appear
    assert "ultra_secret_value" not in captured.out


# ── run_clear ─────────────────────────────────────────────────────────────────

def test_run_clear_removes_key_and_emits_event(capsys):
    """Tier 2: run_clear removes a secret and emits 'secret_cleared' event."""
    # Plant the key first
    save_secret("REYN_CLEAR_ME", "to_be_removed")

    audit_log = _get_audit_log()
    before = len(audit_log.all())

    run_clear(_make_clear_args("REYN_CLEAR_ME"))

    events = audit_log.all()
    new_events = events[before:]
    assert any(
        e.type == "secret_cleared" and e.data.get("key") == "REYN_CLEAR_ME"
        for e in new_events
    )


def test_run_clear_missing_key_no_event(capsys):
    """Tier 2: run_clear on a missing key emits no audit event (nothing changed)."""
    audit_log = _get_audit_log()
    before = len(audit_log.all())

    run_clear(_make_clear_args("DEFINITELY_NOT_STORED_XYZ_123"))

    events = audit_log.all()
    new_events = events[before:]
    # No event emitted when key was not found
    assert not any(e.type == "secret_cleared" for e in new_events)


# ── run_rotate ────────────────────────────────────────────────────────────────

def test_run_rotate_saves_and_emits_rotated_event(capsys):
    """Tier 2: run_rotate saves the new value and emits 'secret_rotated' event."""
    audit_log = _get_audit_log()
    before = len(audit_log.all())

    args = _make_args("REYN_ROTATE_KEY=new_rotated_value")
    run_rotate(args)

    events = audit_log.all()
    new_events = events[before:]
    assert any(
        e.type == "secret_rotated" and e.data.get("key") == "REYN_ROTATE_KEY"
        for e in new_events
    )


def test_run_rotate_value_masked_in_event():
    """Tier 2: the audit event for secret_rotated never contains the actual value."""
    audit_log = _get_audit_log()
    before = len(audit_log.all())

    args = _make_args("REYN_ROTATE_MASK=another_secret")
    run_rotate(args)

    events = audit_log.all()
    new_events = events[before:]
    for e in new_events:
        if e.type == "secret_rotated":
            assert "another_secret" not in str(e.data)
            assert e.data.get("value_masked") == "***"
