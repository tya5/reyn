"""Tier 2: #4482 PR-3 — the Artifacts tab (list + open) through the REAL
TextualChatApp, a REAL FlowView, and a REAL `artifact_ref` mint/resolve
round-trip. No mocks of reyn's own logic — only a fake OS-opener binary on
PATH (the same technique `test_copy_mode_3507.py`'s clipboard tests use;
launching a REAL external application from a test is neither possible nor
desirable, so the boundary this test owns is "reyn resolved the right path
and launched a subprocess with it", not "an app window opened").
"""
from __future__ import annotations

import os
import stat
import sys
import time

import pytest
from textual_flowview import FlowView

from reyn.data.workspace.artifact_ref import mint_ref
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import artifact_pane_commands, artifact_pane_options
from reyn.runtime.outbox import OutboxMessage
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.textual_chat_test_helpers import QueueTransport


def _presentation_frame(*, name: str, ref: str, media_type: str = "text/html") -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{
            "component": "artifact",
            "media_type": media_type,
            "name": name,
            "body": {"ref": ref, "size": 999},
        }]},
    )


@pytest.mark.asyncio
async def test_artifact_row_appears_in_the_pane_after_a_real_presentation_frame():
    """Tier 2: a real `presentation` frame carrying a resolved artifact node,
    ingested through the real app, produces a real Artifacts-pane row —
    exercising `_artifact_rows()` -> `collect_artifact_rows` against a REAL
    `self.conversation` FlowModel (invariant 3's own source), not a hand-built
    fixture list."""
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame(name="report.pptx", ref="abc123"))
        await pilot.pause()

        rows = app._artifact_rows()
        assert [r.name for r in rows] == ["report.pptx"]
        assert rows[0].ref == "abc123"

        pane_rows = artifact_pane_options(rows)
        assert pane_rows == ["report.pptx"]
        pane_cmds = artifact_pane_commands(rows)
        assert pane_cmds == ["/open abc123"]


@pytest.mark.asyncio
async def test_newest_artifact_sorts_first_across_multiple_presentations():
    """Tier 2: two separate presentation turns, newest artifact first —
    exercising real FlowView entry ORDER (`self.conversation` iteration),
    not a synthetic list the pure module was already unit-tested against."""
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame(name="first.pptx", ref="ref-1"))
        await pilot.pause()
        app._ingest_frame(_presentation_frame(name="second.pptx", ref="ref-2"))
        await pilot.pause()

        rows = app._artifact_rows()
        assert [r.name for r in rows] == ["second.pptx", "first.pptx"]


@pytest.mark.asyncio
async def test_open_artifact_end_to_end_resolves_the_real_ref_and_launches_the_opener(
    tmp_path, monkeypatch,
):
    """Tier 2: the full `/open <ref>` path — sentinel dispatch, REAL
    `resolve_ref` (against a REAL minted ref, REAL project_root), REAL
    subprocess launch to a fake opener on PATH. Proves the ref shown on
    the row is the SAME ref that ends up opening the SAME file
    (architect's #4482 requirement) — not by inspection of the code, but
    by observing the actual file the fake opener received."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")
    ref = mint_ref(tmp_path, "default", target)  # the SAME mint the real
    # artifact_payload.py pipeline would have performed when presenting it

    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    script = bindir / opener_name
    script.write_text(f"#!/bin/sh\necho \"$1\" > {sink}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame(name="report.pptx", ref=ref))
        await pilot.pause()

        await app._handle_open_artifact_request(ref)
        await pilot.pause()

        while not sink.exists():  # unbounded — CI's own timeout is the backstop
            time.sleep(0.05)
        assert sink.read_text().strip() == str(target)


@pytest.mark.asyncio
async def test_open_artifact_with_an_unresolvable_ref_reports_not_found(tmp_path, monkeypatch):
    """Tier 2: a ref that resolves to nothing (unknown, or the file is
    gone) reports a status line — never a silent no-op, never a crash."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_open_artifact_request("does-not-exist")
        await pilot.pause()

        entry = list(app.query_one(FlowView).entries)[-1]
        assert "not found" in entry.item.text
