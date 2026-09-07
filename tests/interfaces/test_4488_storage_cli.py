"""Tier 2: #4478/#4476 Phase 1 — ``reyn storage stats`` CLI contract.

Gives ``MediaStore.storage_stats`` and ``aggregate_history_stats`` an actual
caller (lead-coder's #4478 review condition ①: a measurement surface with
no reader is a mechanism nobody uses, the shape flagged repeatedly this
session; #4476 lands on this SAME surface per the same review). Command
renamed ``media`` → ``storage`` on lead-coder's #4488 review, once
``history.jsonl`` reporting made the original name mismatch what it covers
— this is that rename's follow-through test file. No mocks — drives the
real ``run_stats`` against real on-disk state under ``tmp_path``.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.interfaces.cli.commands.storage import register, run_stats


def test_stats_reports_zero_on_a_fresh_project(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: no .reyn/media or .reyn/memory/history-content yet — reports
    zeros, does not error or create the directories as a side effect."""
    monkeypatch.chdir(tmp_path)
    run_stats(Namespace(project_root="."))
    out = capsys.readouterr().out
    assert "media/" in out
    assert "memory/history-content/" in out
    assert not (tmp_path / ".reyn" / "media").exists()
    assert not (tmp_path / ".reyn" / "memory" / "history-content").exists()


def test_stats_reflects_real_writes(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: files written through the real MediaStore API show up in the
    CLI's printed counts/totals (#5364: tool-result writes now land under
    memory/history-content/)."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    store.save_media(b"x" * 30, mime_type="image/png", chain_id="c", tool="t", seq=1)
    store.save_tool_result("hello", chain_id="c", tool="t", seq=1)

    monkeypatch.chdir(tmp_path)
    run_stats(Namespace(project_root="."))
    out = capsys.readouterr().out

    media_line = next(line for line in out.splitlines() if line.startswith("media/"))
    tr_line = next(
        line for line in out.splitlines() if line.startswith("memory/history-content/")
    )
    assert "1" in media_line and "30" in media_line
    assert "1" in tr_line and str(len("hello")) in tr_line


def test_stats_honors_an_explicit_project_root(tmp_path: Path, capsys):
    """Tier 2: --project-root overrides cwd — the CLI does not implicitly
    assume the current directory is the project."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    store.save_media(b"y" * 7, mime_type="image/png", chain_id="c", tool="t", seq=1)

    run_stats(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out
    media_line = next(line for line in out.splitlines() if line.startswith("media/"))
    assert "1" in media_line and "7" in media_line


def test_stats_reflects_a_real_history_jsonl(tmp_path: Path, monkeypatch, capsys):
    """Tier 2: #4476 — a history.jsonl under .reyn/agents/<name>/ shows up
    in the same command's output, not a separate one."""
    hist = tmp_path / ".reyn" / "agents" / "alice" / "history.jsonl"
    hist.parent.mkdir(parents=True)
    hist.write_text('{"seq": 1}\n{"seq": 2}\n{"seq": 3}\n', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    run_stats(Namespace(project_root="."))
    out = capsys.readouterr().out

    hist_line = next(line for line in out.splitlines() if line.startswith("history.jsonl"))
    assert "1" in hist_line  # 1 file found
    assert "3" in hist_line  # 3 turns
    assert str(hist.stat().st_size) in hist_line


def test_storage_stats_is_registered_on_the_reyn_parser():
    """Tier 2: (reachability) 'reyn storage stats' is wired into the real
    top-level parser, not just importable in isolation — this is the exact
    shape ('declared, implemented, tested, invoked by nobody') #4478's
    review flagged; asserts the subcommand is actually reachable through
    argparse, the real entry point an operator uses."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    register(sub)

    args = parser.parse_args(["storage", "stats", "--project-root", "/tmp"])
    assert args.func is run_stats


# ── #5896 stage ③: `reyn storage migrate-bodies` ────────────────────────


def test_migrate_bodies_is_registered_on_the_reyn_parser():
    """Tier 2: (reachability) 'reyn storage migrate-bodies' is wired into
    the real top-level parser — same shape as the sibling reachability
    test above."""
    import argparse

    from reyn.interfaces.cli.commands.storage import run_migrate_bodies

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    register(sub)

    args = parser.parse_args(["storage", "migrate-bodies", "--project-root", "/tmp"])
    assert args.func is run_migrate_bodies
    assert args.agent is None
    assert args.min_bytes is None


def test_migrate_bodies_migrates_a_large_row_and_reports_it(
    tmp_path: Path, capsys,
):
    """Tier 2: driving the real CLI entry point (not the lower-level
    ``migrate_inline_history_bodies`` directly, which the sibling
    ``test_5896_stage3_migrate_bodies.py`` already covers in depth) —
    proves the CLI wiring itself (MediaStore construction per agent,
    ``--min-bytes`` threading, the printed report) actually works
    end-to-end against a real on-disk history.jsonl."""
    import json

    from reyn.interfaces.cli.commands.storage import run_migrate_bodies
    from reyn.runtime.chat_message import CONTENT_REF_META_KEY

    hist_dir = tmp_path / ".reyn" / "agents" / "alice"
    hist_dir.mkdir(parents=True)
    hist_path = hist_dir / "history.jsonl"
    big = "b" * 2000
    row = {
        "role": "tool", "content": big, "ts": "", "seq": 1, "meta": {},
        "tool_calls": None, "tool_call_id": "c1", "name": "t",
        "spillability": "last_resort", "disclosure": None,
    }
    hist_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    run_migrate_bodies(Namespace(project_root=str(tmp_path), agent=None, min_bytes=1000))
    out = capsys.readouterr().out

    assert "alice: migrated 1 row" in out
    assert "total: 1 row(s) migrated" in out
    migrated = json.loads(hist_path.read_text(encoding="utf-8").splitlines()[0])
    assert migrated["content"] == ""
    ref = migrated["meta"][CONTENT_REF_META_KEY]
    assert (tmp_path / ref).read_text(encoding="utf-8") == big


def test_migrate_bodies_honors_the_agent_filter(tmp_path: Path):
    """Tier 2: --agent scopes the migration to one agent's history.jsonl,
    leaving every other agent's file untouched — an operator migrating
    one known-huge agent must not be forced to also touch every other
    agent's history."""
    import json

    from reyn.interfaces.cli.commands.storage import run_migrate_bodies

    big = "c" * 2000
    for agent in ("alice", "bob"):
        d = tmp_path / ".reyn" / "agents" / agent
        d.mkdir(parents=True)
        row = {
            "role": "tool", "content": big, "ts": "", "seq": 1, "meta": {},
            "tool_calls": None, "tool_call_id": "c1", "name": "t",
            "spillability": "last_resort", "disclosure": None,
        }
        (d / "history.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    run_migrate_bodies(
        Namespace(project_root=str(tmp_path), agent="alice", min_bytes=1000),
    )

    alice_line = json.loads(
        (tmp_path / ".reyn" / "agents" / "alice" / "history.jsonl").read_text().splitlines()[0],
    )
    bob_line = json.loads(
        (tmp_path / ".reyn" / "agents" / "bob" / "history.jsonl").read_text().splitlines()[0],
    )
    assert alice_line["content"] == "", "the targeted agent must be migrated"
    assert bob_line["content"] == big, "an un-targeted agent must be left untouched"


def test_migrate_bodies_reports_start_and_end_footprint_bracketing_the_work(
    tmp_path: Path, capsys,
):
    """Tier 1: architect ruling, follow-up to PR #5947 — the command
    reports THIS process's own footprint at start AND end (2 points,
    #5851's own reader), and does so BRACKETING the actual migration
    work — start before any row is touched, end after every agent has
    been processed but before the final total summary. Ordering
    (``<``), not a line count, is the claim: a duplicate or missing
    footprint line would break this chain regardless of how many total
    lines happen to appear.

    Real reader, no mock (same platform-variance idiom
    ``test_5851a_process_memory_observe.py`` already established): on a
    platform this repo declares supported (darwin/linux), both lines
    carry the real metric name; on any other platform both say "not
    measurable"."""
    import json

    from reyn.interfaces.cli.commands.storage import run_migrate_bodies
    from reyn.runtime.process_memory import process_memory_metric_name

    d = tmp_path / ".reyn" / "agents" / "alice"
    d.mkdir(parents=True)
    row = {
        "role": "tool", "content": "x" * 2000, "ts": "", "seq": 1, "meta": {},
        "tool_calls": None, "tool_call_id": "c1", "name": "t",
        "spillability": "last_resort", "disclosure": None,
    }
    (d / "history.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    run_migrate_bodies(
        Namespace(project_root=str(tmp_path), agent=None, min_bytes=1000),
    )
    lines = capsys.readouterr().out.splitlines()

    start_idx = next(i for i, ln in enumerate(lines) if ln.startswith("footprint (start):"))
    migrated_idx = next(i for i, ln in enumerate(lines) if ln.startswith("alice: migrated"))
    end_idx = next(i for i, ln in enumerate(lines) if ln.startswith("footprint (end):"))
    total_idx = next(i for i, ln in enumerate(lines) if ln.startswith("total:"))
    assert start_idx < migrated_idx < end_idx < total_idx, (
        f"footprint must bracket the migration work, in this order; got {lines!r}"
    )

    metric = process_memory_metric_name()
    if metric is None:
        assert "not measurable on this platform" in lines[start_idx]
        assert "not measurable on this platform" in lines[end_idx]
    else:
        assert metric in lines[start_idx] and metric in lines[end_idx]


def test_migrate_bodies_reports_footprint_even_with_nothing_to_migrate(
    tmp_path: Path, capsys,
):
    """Tier 1: accept-side — the "no .reyn/agents/ directory" early-
    return path still reports both footprint points, not just the
    common (work-to-do) path. A tool meant to pair cost against effect
    must report ITS OWN cost even on a run that changed nothing. The
    start line must be the FIRST line printed and the end line the
    LAST — position, not a count, is the claim."""
    from reyn.interfaces.cli.commands.storage import run_migrate_bodies

    run_migrate_bodies(Namespace(project_root=str(tmp_path), agent=None, min_bytes=None))
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("footprint (start):"), (
        f"the start footprint must be the very first line printed; got {lines!r}"
    )
    assert lines[-1].startswith("footprint (end):"), (
        f"the end footprint must be the very last line printed on this no-op "
        f"path; got {lines!r}"
    )
