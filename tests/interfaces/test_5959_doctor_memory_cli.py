"""Tier 2: #5959 (owner-hit) — ``reyn doctor-memory`` CLI wiring + the
audit-event exit surface. Real logic lives in
:mod:`reyn.runtime.memory_breakdown` (its own test file,
``tests/runtime/test_5959_memory_breakdown.py``) — this file is
reachability (the #4478 "declared, implemented, tested, invoked by
nobody" shape) + a real end-to-end CLI run against a real ``.reyn/``
tree, no mocks.
"""
from __future__ import annotations

import argparse
import json
from argparse import Namespace
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor_memory import register, run

# ── Reachability (the #4478 shape: declared, implemented, but never wired) ──


def test_doctor_memory_is_registered_on_the_reyn_parser():
    """Tier 2: 'reyn doctor-memory' is wired into a real subparser, not
    just importable in isolation."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    register(sub)

    args = parser.parse_args(["doctor-memory"])
    assert args.func is run
    assert args.top_n == 20
    assert args.no_audit_event is False


def test_doctor_memory_is_registered_via_the_real_command_registry():
    """Tier 2: the ALL list in commands/__init__.py — not just this
    module's own register() — actually includes doctor_memory. Mirrors
    test_4364_pr3a_doctor_cli.py's own identically-shaped test for
    'reyn doctor' (#4478's own review named this exact gap: importable
    but unreachable through argparse)."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert "doctor-memory" in subparsers_action.choices


# ── end to end, real .reyn/ tree, no mocks ───────────────────────────────


def test_run_prints_both_discovery_tables_and_the_bounding_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """Tier 2: a real invocation, against a real (empty but present)
    .reyn/ directory, prints both ⓐ tables, the ⓑ census, the dead-
    declaration filter, and the shallowness disclosure -- and does not
    raise, the whole point of a report-only tool."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()

    run(Namespace(top_n=5, no_audit_event=False))

    out = capsys.readouterr().out
    assert "sys.getsizeof is shallow" in out
    assert "gc.get_objects() never tracks" in out
    assert "ⓐ discovery" in out
    assert "ⓑ completeness" in out
    assert "dead-declaration candidates" in out
    assert "③ tracemalloc" in out


def test_no_audit_event_flag_skips_the_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: --no-audit-event genuinely skips the P6 emit -- verified by
    the absence of any events/direct/cli file the emit would otherwise
    create (real filesystem effect, not a mocked call-count)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()

    run(Namespace(top_n=1, no_audit_event=True))

    direct_cli_dir = tmp_path / ".reyn" / "events" / "direct" / "cli"
    assert not direct_cli_dir.exists() or not any(direct_cli_dir.rglob("*.jsonl")), (
        "--no-audit-event must produce zero audit-event files"
    )


def test_the_audit_event_actually_lands_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the default (no --no-audit-event) path really emits
    process_memory_breakdown to a real .reyn/events/direct/cli file --
    the 'same breakdown reaches the audit trail even with nobody
    watching' acceptance criterion, verified against the real seam
    (emit_cli_event -> emit_direct_event), not a stand-in sink."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()

    run(Namespace(top_n=2, no_audit_event=False))

    direct_cli_dir = tmp_path / ".reyn" / "events" / "direct" / "cli"
    files = list(direct_cli_dir.rglob("*.jsonl"))
    assert files, "the default run must emit at least one audit-event file"
    lines = [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matching = [e for e in lines if e.get("type") == "process_memory_breakdown"]
    assert matching, "a process_memory_breakdown event must be on disk"
    assert "type_breakdown" in matching[0]["data"]
    assert "bounding_census" in matching[0]["data"]
