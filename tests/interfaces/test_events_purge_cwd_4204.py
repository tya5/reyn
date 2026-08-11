"""Tier 2: #4204 bucket A — `reyn events purge` anchors on the project root,
not raw cwd.

`run_purge` previously built `root` from a bare relative `Path(".reyn") /
"events"` — equivalent to anchoring on raw process cwd, since a relative
`Path` resolves against it. Architect's own assessment (#4204): this was
fail-safe in DIRECTION (a subdirectory launch resolves to a phantom,
usually-absent `.reyn/events/` rather than some OTHER real project's tree,
so it never purges the wrong tree) but not in EFFECT — a destructive
`--before` purge launched from a subdirectory silently did nothing (exit
0, "No events directory" message) while the real project's event files sat
untouched, which is its own operator-trust hazard: the operator believes
the purge ran.

No mocks — real on-disk `.jsonl` files, the real `run_purge` CLI dispatch.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.interfaces.cli.commands.events import run_purge


def _write_event_file(events_dir: Path, date_str: str) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    p = events_dir / f"{date_str}.jsonl"
    p.write_text('{"type": "session_started"}\n', encoding="utf-8")
    return p


def test_purge_deletes_old_files_at_the_project_root(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: control — a purge run AT the project root deletes files
    older than --before (establishes the baseline this file's other test
    falsifies against)."""
    events_dir = tmp_path / ".reyn" / "events"
    old_file = _write_event_file(events_dir, "2026-01-01")
    new_file = _write_event_file(events_dir, "2026-06-01")
    monkeypatch.chdir(tmp_path)

    run_purge(argparse.Namespace(before="2026-03-01", agent=None, dry_run=False))

    assert not old_file.exists()
    assert new_file.exists()


def test_purge_from_a_subdirectory_still_reaches_the_project_events(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: #4204 — `reyn events purge` launched from a subdirectory of
    the project must still purge the PROJECT's `.reyn/events/`, not
    silently no-op against a phantom directory under the subdirectory.

    Falsify-worthy shape: without the fix, this test's `run_purge` call
    prints "No events directory at <phantom path>" and the real
    `old_file` survives untouched — the exact silent-no-op architect
    named."""
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    events_dir = tmp_path / ".reyn" / "events"
    old_file = _write_event_file(events_dir, "2026-01-01")
    new_file = _write_event_file(events_dir, "2026-06-01")

    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    run_purge(argparse.Namespace(before="2026-03-01", agent=None, dry_run=False))

    out = capsys.readouterr().out
    assert "No events directory" not in out, (
        "purge silently no-op'd against a phantom subdirectory-anchored path"
    )
    assert not old_file.exists()
    assert new_file.exists()
    assert not (subdir / ".reyn").exists()
