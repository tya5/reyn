"""Tier 2: OS invariant — secrets.env startup loader.

Pins the following invariants for ``reyn.security.secrets.loader.load_secrets_to_environ``:

  - File absent: gracefully returns without error
  - File present: values are injected into os.environ
  - No override: pre-existing env vars are NOT overwritten
  - Parse errors: bad lines emit UserWarning and are skipped; loader continues
  - chmod 600 enforce: world-readable file triggers warning and auto-fix
  - Comments and blank lines are ignored
  - Quoted values have quotes stripped
"""
from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path

import pytest

from reyn.security.secrets.loader import load_secrets_to_environ

# ── helpers ──────────────────────────────────────────────────────────────────

def _write_secrets(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


# ── tests ────────────────────────────────────────────────────────────────────

def test_missing_file_is_graceful(tmp_path):
    """Tier 2: absent secrets.env is silently skipped — no error, no crash."""
    missing = tmp_path / "no_such_file.env"
    # Should not raise
    load_secrets_to_environ(path=missing)


def test_values_injected_into_environ(tmp_path, monkeypatch):
    """Tier 2: KEY=value pairs in secrets.env are added to os.environ."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "REYN_TEST_FOO=hello\nREYN_TEST_BAR=world\n")

    # Ensure keys are not already set
    monkeypatch.delenv("REYN_TEST_FOO", raising=False)
    monkeypatch.delenv("REYN_TEST_BAR", raising=False)

    load_secrets_to_environ(path=secrets)

    assert os.environ["REYN_TEST_FOO"] == "hello"
    assert os.environ["REYN_TEST_BAR"] == "world"


def test_existing_env_not_overridden(tmp_path, monkeypatch):
    """Tier 2: load_secrets_to_environ MUST NOT override pre-existing env vars."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "REYN_TEST_EXISTING=from_file\n")

    monkeypatch.setenv("REYN_TEST_EXISTING", "from_shell")

    load_secrets_to_environ(path=secrets)

    # Shell value wins
    assert os.environ["REYN_TEST_EXISTING"] == "from_shell"


def test_comments_and_blanks_ignored(tmp_path, monkeypatch):
    """Tier 2: comment lines (#) and blank lines do not produce key entries."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "# This is a comment\n\nREYN_TEST_REAL=yes\n")

    monkeypatch.delenv("REYN_TEST_REAL", raising=False)

    load_secrets_to_environ(path=secrets)

    assert os.environ.get("REYN_TEST_REAL") == "yes"


def test_parse_error_skipped_with_warning(tmp_path, monkeypatch):
    """Tier 2: lines without '=' emit a UserWarning and are skipped; parsing continues."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "NOT_A_VALID_LINE\nREYN_TEST_GOOD=ok\n")

    monkeypatch.delenv("REYN_TEST_GOOD", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_secrets_to_environ(path=secrets)

    # At least one warning was emitted for the bad line
    assert any("no '='" in str(w.message) or "skipping" in str(w.message) for w in caught)
    # Good line still loaded
    assert os.environ.get("REYN_TEST_GOOD") == "ok"


def test_chmod_warning_on_world_readable(tmp_path, monkeypatch):
    """Tier 2: world-readable secrets.env emits a warning and is auto-chmod'd to 600."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "REYN_TEST_WR=value\n")
    # Make world-readable
    secrets.chmod(0o644)

    monkeypatch.delenv("REYN_TEST_WR", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_secrets_to_environ(path=secrets)

    # A warning about permissions was emitted
    assert any("600" in str(w.message) or "readable" in str(w.message) for w in caught)
    # File was auto-fixed to 600
    mode = secrets.stat().st_mode & 0o777
    assert mode == 0o600


def test_quoted_values_stripped(tmp_path, monkeypatch):
    """Tier 2: double-quoted and single-quoted values have their quotes removed."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(
        secrets,
        'REYN_TEST_DQ="double quoted"\nREYN_TEST_SQ=\'single quoted\'\n',
    )

    monkeypatch.delenv("REYN_TEST_DQ", raising=False)
    monkeypatch.delenv("REYN_TEST_SQ", raising=False)

    load_secrets_to_environ(path=secrets)

    assert os.environ["REYN_TEST_DQ"] == "double quoted"
    assert os.environ["REYN_TEST_SQ"] == "single quoted"


def test_multiple_calls_idempotent(tmp_path, monkeypatch):
    """Tier 2: calling load_secrets_to_environ twice does not change values set by the first call."""
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, "REYN_TEST_IDEM=first\n")

    monkeypatch.delenv("REYN_TEST_IDEM", raising=False)

    load_secrets_to_environ(path=secrets)
    assert os.environ["REYN_TEST_IDEM"] == "first"

    # Overwrite file with different value
    _write_secrets(secrets, "REYN_TEST_IDEM=second\n")
    load_secrets_to_environ(path=secrets)

    # Still "first" — env is not overridden once set
    assert os.environ["REYN_TEST_IDEM"] == "first"
