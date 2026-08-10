"""Tier 2: PR-resume-ux U3 — chat CLI flags --no-restore / --reset.

Two new flags exposed on ``reyn chat``:
  --no-restore      Skip restore_all this run (state stays on disk for next).
  --reset           Wipe in-flight agent state (snapshots + WAL) before
                    starting; events/ is preserved (P6 audit truth).

Implementation is split between argparse (flag definition) and a helper
``_reset_project_state`` that does the actual file deletion. Tests cover
the helper directly + argparse integration.

#3213 item 3: ``--no-restore`` used to skip ONLY the WAL-derived agent-state
restore (``restore_all()``) while the persisted chat transcript was still
loaded via ``Session.load_history()`` on a separate path gated by a ``fresh``
flag that only ``run-once`` set. The tests below drive the REAL production
``chat.run(args)`` entry point end to end (the exact code changed by this
fix), not a hand-built call to ``load_history()``, so they exercise the
wiring, not just the mechanism.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.chat import _reset_project_state, register

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


def test_grant_file_write_flag_removed():
    """Tier 2: #3924 deletion-witness — --grant-file-write is no longer a
    registered 'reyn chat' flag (owner ruling: per-invocation permission
    flags don't scope well in a multi-agent system; measured zero real call
    sites outside its own tests, #3924). Formerly
    test_chat_grant_file_write_187.py::test_chat_parser_exposes_grant_file_write_flag
    (that file's remaining tests — the resolver-level "allow config permits
    a write"/"empty config denies a write" claims — are covered generically
    elsewhere, independent of any chat flag: see
    test_require_file_jit_ask_1505.py::test_file_write_bus_none_denies_outside_zone)."""
    parser = _make_parser_with_chat()
    with pytest.raises(SystemExit):
        parser.parse_args(["chat", "--grant-file-write"])


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
        "events": project_root / ".reyn" / "events" / "agents" / "alpha" / "chat" / "log.jsonl",
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("seed\n", encoding="utf-8")
    return paths


def test_reset_deletes_wal_and_snapshots(tmp_path):
    """Tier 2: --reset removes WAL + per-agent snapshot."""
    paths = _seed_project_state(tmp_path)
    _reset_project_state(tmp_path, confirm=False)

    assert not paths["wal"].exists(), "WAL must be deleted by --reset"
    assert not paths["agent_snap"].exists(), "agent snapshot must be deleted"


def test_reset_preserves_events_dir(tmp_path):
    """Tier 2: ``.reyn/events/`` is P6 audit truth — --reset must NOT touch it."""
    paths = _seed_project_state(tmp_path)
    _reset_project_state(tmp_path, confirm=False)

    assert paths["events"].exists(), (
        "events/ is audit log (P6) — --reset must preserve it"
    )
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


# ---------------------------------------------------------------------------
# #3213 item 3 — --no-restore must also skip loading the chat transcript
# ---------------------------------------------------------------------------


def _run_chat_once(tmp_path, monkeypatch, *, no_restore: bool):
    """Drive the REAL `reyn chat` entry point (`chat.run(args)`) through the
    fast one-shot drive, with only the network-facing LLM call
    (`send_to_agent_impl`) substituted for a same-signature stand-in — the
    exact substitution `test_reyn_run_once_cli_reaches_registry_shutdown`
    (test_teardown_completeness_2783.py) already uses for this same reason.
    Returns the ``Session.load_history`` call count observed via a
    call-through spy (real behavior still runs; only the count is watched —
    not a private-state read).
    """
    from reyn.interfaces.cli.commands.chat import register as chat_register
    from reyn.runtime.session import Session

    monkeypatch.chdir(tmp_path)
    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    chat_register(sub)
    argv = ["chat"]
    if no_restore:
        argv.append("--no-restore")
    args = top.parse_args(argv)
    args.once = True  # drive the fast one-shot branch, same as `run-once`

    # Signature mirrors send_to_agent_impl, ``inbox_kind`` included (#3595 step
    # 1b): `reyn run-once` passes it explicitly, and a double that swallows the
    # kwarg would stop witnessing that the operator path still claims "user".
    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None,
                          inbox_kind="user") -> dict:
        assert inbox_kind == "user", (
            "`reyn run-once` is the operator's own line at a first-party CLI; it "
            f"must not ride a non-operator inbox kind (got {inbox_kind!r})"
        )
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    orig_load_history = Session.load_history
    calls = {"n": 0}

    def _spy_load_history(self) -> None:
        calls["n"] += 1
        return orig_load_history(self)

    monkeypatch.setattr(Session, "load_history", _spy_load_history)

    from reyn.interfaces.cli.commands import chat as chat_mod
    chat_mod.run(args)

    return calls["n"]


def _seed_transcript(tmp_path: Path) -> Path:
    history_path = tmp_path / ".reyn" / "agents" / "default" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    seed = (
        '{"role": "user", "content": [{"type": "text", "text": "prior attempt"}], '
        '"seq": 1}\n'
    )
    history_path.write_text(seed, encoding="utf-8")
    return history_path


def test_no_restore_skips_transcript_load(tmp_path, monkeypatch):
    """Tier 2: `reyn chat --no-restore` must skip loading the persisted chat
    transcript, not just the WAL-derived agent-state restore (#3213 item 3).
    Drives the real CLI path; `Session.load_history` must NOT be called."""
    _seed_transcript(tmp_path)

    n_calls = _run_chat_once(tmp_path, monkeypatch, no_restore=True)

    assert n_calls == 0, (
        "--no-restore must skip Session.load_history() — the chat transcript "
        "load path — not just registry.restore_all() (#3213 item 3)"
    )


def test_default_run_still_loads_transcript(tmp_path, monkeypatch):
    """Tier 2: without --no-restore, the transcript load is unaffected — the
    skip is per-run, not sticky. Same real CLI path as the --no-restore test
    above, flag simply omitted."""
    _seed_transcript(tmp_path)

    n_calls = _run_chat_once(tmp_path, monkeypatch, no_restore=False)

    assert n_calls == 1, (
        "a normal run (no --no-restore) must still call Session.load_history() "
        "exactly once"
    )


def test_no_restore_does_not_delete_or_truncate_transcript(tmp_path, monkeypatch):
    """Tier 2: the ★ property that must survive — `--no-restore` SKIPS loading,
    it does NOT delete anything. This is what keeps `--no-restore`
    non-destructive per ADR-0010 (distinct from the destructive `--reset`).
    Asserts the transcript file is untouched on disk after a --no-restore run,
    and that a SUBSEQUENT run without the flag loads it normally (proving the
    skip was per-run, not a deletion)."""
    history_path = _seed_transcript(tmp_path)
    original_content = history_path.read_text(encoding="utf-8")

    _run_chat_once(tmp_path, monkeypatch, no_restore=True)

    assert history_path.exists(), (
        "--no-restore must not delete the chat transcript file"
    )
    assert history_path.read_text(encoding="utf-8") == original_content, (
        "--no-restore must not truncate/modify the chat transcript file"
    )

    # A subsequent run WITHOUT the flag loads it normally (non-sticky skip).
    n_calls_next_run = _run_chat_once(tmp_path, monkeypatch, no_restore=False)
    assert n_calls_next_run == 1, (
        "a following run without --no-restore must load the still-intact "
        "transcript normally"
    )
