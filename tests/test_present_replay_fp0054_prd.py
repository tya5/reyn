"""Present replay/rewind re-render (FP-0054 PR-D, §8).

Presentation is a cache; the ``presented`` event is the truth. On replay a
``presented`` event re-renders best-effort from the still-durable ``data_ref``; a
gone / inline ref shows an expiry placeholder pointing at the audit event — never a
crash, never a stale render. Real EventLog + Workspace + a real on-disk ref — no
collaborator mocks; assertions are on content presence + the placeholder fact, never
exact render layout.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle
from reyn.core.present import load_ref_from_disk, replay_presentation
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _ctx(tmp_path: Path) -> tuple[OpContext, EventLog]:
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="present_replay_test",
    )
    return ctx, events


def _presented_event(events: EventLog) -> dict:
    evs = [e for e in events.all() if e.type == "presented"]
    assert evs, "present emitted no presented event"
    return evs[-1].data


def test_presented_event_replays_to_best_effort_rerender(tmp_path, monkeypatch):
    """Tier 2: a real presented event replays to a best-effort re-render — the ref's
    content reaches the replay surface (a presentation, not a placeholder)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "people.json").write_text(
        json.dumps([{"name": "alice", "role": "author"},
                    {"name": "bob", "role": "editor"}])
    )
    ctx, events = _ctx(tmp_path)
    op = PresentIROp(
        kind="present", data_ref="people.json",
        blueprint={
            "component": "table", "rows": {"$bind": ""},
            "columns": [{"header": "Name", "path": "/name"}],
        },
    )
    asyncio.run(handle(op, ctx))

    # Replay the emitted event through the replay path (default on-disk loader).
    replayed = replay_presentation(_presented_event(events), load_ref=load_ref_from_disk)

    assert replayed.is_placeholder is False
    rendered = "\n".join(replayed.lines)
    # The content the ref carried reaches the surface again (content presence, not
    # exact layout).
    assert "alice" in rendered
    assert "bob" in rendered


def test_gone_ref_replays_to_expiry_placeholder(tmp_path, monkeypatch):
    """Tier 2: a presented event whose data_ref is gone replays to an expiry
    placeholder pointing at the audit event — not a crash, not a stale render."""
    monkeypatch.chdir(tmp_path)
    ref = tmp_path / "gone.json"
    ref.write_text(json.dumps({"a": 1}))
    ctx, events = _ctx(tmp_path)
    op = PresentIROp(
        kind="present", data_ref="gone.json",
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    asyncio.run(handle(op, ctx))
    event_data = _presented_event(events)

    # The ref is GC'd / deleted after the presentation — the cache is gone.
    ref.unlink()

    replayed = replay_presentation(event_data, load_ref=load_ref_from_disk)

    assert replayed.is_placeholder is True
    text = "\n".join(replayed.lines)
    # Points at the durable audit event, and names the missing ref.
    assert "gone.json" in text
    assert "presented" in text  # references the audit event
    # Does NOT stale-render the old value.
    assert replayed.lines and all("\"a\": 1" not in ln for ln in replayed.lines)


def test_inline_data_presentation_replays_to_placeholder(tmp_path):
    """Tier 2: an inline-data presentation (bytes never persisted, only the audit
    marker) replays to a placeholder pointing at the audit event — the event carries
    no content bytes to re-render."""
    ctx, events = _ctx(tmp_path)
    op = PresentIROp(
        kind="present", data_inline={"a": 1},
        blueprint={"component": "text", "text": {"$bind": "/a"}},
    )
    asyncio.run(handle(op, ctx))

    replayed = replay_presentation(_presented_event(events))

    assert replayed.is_placeholder is True
    assert any("inline" in ln for ln in replayed.lines)


def test_replay_never_raises_on_a_missing_ref():
    """Tier 2: replay is best-effort by construction — a data_ref that cannot load
    yields a placeholder, never an exception (a bad event must not crash a replay)."""
    replayed = replay_presentation(
        {"data_ref": "/does/not/exist.json", "view": "t", "rows": 0},
    )
    assert replayed.is_placeholder is True
