"""Tier 2: PR-resume-ux U3 — chat CLI flags --no-restore / --reset.

Two new flags exposed on ``reyn chat``:
  --no-restore      Skip restore_all this run (state stays on disk for next).
  --reset           Wipe in-flight skill state (snapshots + WAL) before
                    starting; events/ is preserved (P6 audit truth).

Implementation is split between argparse (flag definition) and a helper
``_reset_project_state`` that does the actual file deletion. Tests cover
the helper directly + argparse integration.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.cli.commands.chat import register, _reset_project_state


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _make_parser_with_chat() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser


def test_no_restore_flag_parses():
    """Tier 2: --no-restore is a valid CLI flag and stored on namespace."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--no-restore"])
    assert args.no_restore is True


def test_reset_flag_parses():
    """Tier 2: --reset is a valid CLI flag."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat", "--reset"])
    assert args.reset is True


def test_default_flags_off():
    """Tier 2: backward compat — default chat invocation has both flags off."""
    parser = _make_parser_with_chat()
    args = parser.parse_args(["chat"])
    assert args.no_restore is False
    assert args.reset is False


# ---------------------------------------------------------------------------
# _reset_project_state helper
# ---------------------------------------------------------------------------


def _seed_project_state(project_root: Path) -> dict:
    """Seed a project with all the file types --reset should affect."""
    paths = {
        "wal": project_root / ".reyn" / "state" / "wal.jsonl",
        "agent_snap": project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json",
        "skill_snap": project_root / ".reyn" / "agents" / "alpha" / "state" / "skills" / "run_x.snapshot.json",
        "events": project_root / ".reyn" / "events" / "agents" / "alpha" / "chat" / "log.jsonl",
        "events_skill": project_root / ".reyn" / "events" / "agents" / "alpha" / "skill_runs" / "run_x.jsonl",
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("seed\n", encoding="utf-8")
    return paths


def test_reset_deletes_wal_and_snapshots(tmp_path):
    """Tier 2: --reset removes WAL + per-agent snapshot + per-skill snapshots."""
    paths = _seed_project_state(tmp_path)
    _reset_project_state(tmp_path, confirm=False)

    assert not paths["wal"].exists(), "WAL must be deleted by --reset"
    assert not paths["agent_snap"].exists(), "agent snapshot must be deleted"
    assert not paths["skill_snap"].exists(), "per-skill snapshot must be deleted"


def test_reset_preserves_events_dir(tmp_path):
    """Tier 2: ``.reyn/events/`` is P6 audit truth — --reset must NOT touch it."""
    paths = _seed_project_state(tmp_path)
    _reset_project_state(tmp_path, confirm=False)

    assert paths["events"].exists(), (
        "events/ is audit log (P6) — --reset must preserve it"
    )
    assert paths["events_skill"].exists()
    # Read content unchanged
    assert paths["events"].read_text() == "seed\n"


def test_reset_idempotent_on_clean_state(tmp_path):
    """Tier 2: --reset on already-clean state is a no-op (no error)."""
    # No state seeded — should not raise
    _reset_project_state(tmp_path, confirm=False)


def test_reset_with_confirm_true_prompts(tmp_path, monkeypatch):
    """Tier 2: with confirm=True, the helper reads a confirmation answer.

    The user typing 'no' (or anything that's not 'yes') aborts the reset.
    """
    paths = _seed_project_state(tmp_path)

    # Simulate user typing 'no'
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    aborted = _reset_project_state(tmp_path, confirm=True)
    assert aborted is False, "user 'no' must abort reset"
    assert paths["wal"].exists(), "abort must preserve state"

    # Simulate user typing 'yes'
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    confirmed = _reset_project_state(tmp_path, confirm=True)
    assert confirmed is True
    assert not paths["wal"].exists(), "confirmed reset must delete state"
