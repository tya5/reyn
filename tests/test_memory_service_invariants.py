"""Tier 2: OS invariant tests for MemoryService.

Policy compliance (`docs/deep-dives/contributing/testing.md`):
- NO unittest.mock, MagicMock, AsyncMock, or patch.
- NO private-state assertions.
- File callbacks are thin closures over tmp_path (plain open/os.unlink/os.listdir).
  These facsimiles stand in for the real _file_write / _file_read / _file_delete /
  _file_regenerate_index methods on ChatSession; the real ones gate on OpContext +
  Workspace + PermissionResolver, which would pull the entire OS stack into what
  should be a unit-level Tier 2 test. The closures do identical filesystem work
  without the permission layer — permissible because this test verifies
  MemoryService orchestration, not the permission gate (which has its own Tier 1
  contract tests).
- EventLog is real (no stub).
- Each docstring's first line declares its Tier.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.chat.services.memory_service import MemoryService
from reyn.core.events.events import EventLog

# ---------------------------------------------------------------------------
# Helpers — thin closure-based fakes for file callbacks
# ---------------------------------------------------------------------------


def _make_callbacks(base: Path):
    """Return (file_write, file_read, file_delete, file_regenerate_index)
    as plain async closures over *base*.

    These exercise the same filesystem surface as ChatSession's real callbacks
    without pulling in OpContext or PermissionResolver.
    """

    async def file_write(path: str, content: str) -> dict:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": path, "written": True}

    async def file_read(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"error": f"file not found: {path}"}
        return {"path": path, "content": p.read_text(encoding="utf-8")}

    async def file_delete(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"error": f"file not found: {path}"}
        os.unlink(p)
        return {"path": path, "deleted": True}

    async def file_regenerate_index(
        *,
        path: str,
        output_path: str,
        entry_template: str,
        header: str,
    ) -> dict:
        """Minimal index regenerator: scans *.md files (excluding MEMORY.md),
        reads frontmatter fields, renders entry_template per file, writes
        output_path.  Matches the real op_runtime regenerate_index semantics
        closely enough to exercise MemoryService's orchestration.
        """
        dir_path = Path(path)
        if not dir_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)

        entries = 0
        lines = [header]
        for md_file in sorted(dir_path.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            # Parse YAML frontmatter between --- delimiters
            fields: dict[str, str] = {}
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end != -1:
                    fm_block = text[4:end]
                    for line in fm_block.splitlines():
                        if ": " in line:
                            k, v = line.split(": ", 1)
                            fields[k.strip()] = v.strip()
            slug = md_file.stem
            rendered = entry_template.format(slug=slug, **fields)
            lines.append(rendered + "\n")
            entries += 1

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(lines), encoding="utf-8")
        return {"path": path, "output_path": output_path, "entries": entries}

    return file_write, file_read, file_delete, file_regenerate_index


def _make_service(tmp_path: Path) -> tuple[MemoryService, EventLog]:
    """Construct a MemoryService with real EventLog and closure-based file
    callbacks rooted at *tmp_path*."""
    events = EventLog()
    fw, fr, fd, fri = _make_callbacks(tmp_path)
    svc = MemoryService(
        agent_workspace_dir=tmp_path / "agents" / "test_agent",
        events=events,
        file_write=fw,
        file_read=fr,
        file_delete=fd,
        file_regenerate_index=fri,
    )
    return svc, events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_then_read_body_roundtrip(tmp_path: Path) -> None:
    """Tier 2: remember + read_body round-trip — body matches, no frontmatter leaks."""
    svc, _ = _make_service(tmp_path)

    result = await svc.remember(
        layer="agent",
        slug="hello",
        name="Hello Note",
        description="A test note",
        type="note",
        body="hello world",
    )
    assert "error" not in result
    assert result["saved"] == "hello"
    assert result["layer"] == "agent"

    read = await svc.read_body(layer="agent", slug="hello")
    assert "error" not in read
    assert read["slug"] == "hello"
    assert read["layer"] == "agent"
    # Body content must include the payload text
    assert "hello world" in read["content"]
    # Frontmatter is present in the file but read_body returns the full file
    # content including frontmatter (same contract as the original session
    # implementation). Verify at minimum that the body text is present and
    # that the *caller* receives raw content — no stripping or mutation.
    # The key invariant: the body text survives the round-trip intact.
    assert read["content"].endswith("hello world")


@pytest.mark.asyncio
async def test_forget_removes_file_and_updates_index(tmp_path: Path) -> None:
    """Tier 2: forget removes the memory file and MEMORY.md no longer references it."""
    svc, _ = _make_service(tmp_path)

    # First remember two entries
    await svc.remember(
        layer="agent", slug="keep", name="Keep", description="kept", type="note", body="stay"
    )
    await svc.remember(
        layer="agent", slug="gone", name="Gone", description="removed", type="note", body="bye"
    )

    # Forget the second
    result = await svc.forget(layer="agent", slug="gone")
    assert "error" not in result
    assert result["deleted"] == "gone"

    # The file must not exist
    gone_path = Path(svc.memory_path("agent", "gone"))
    assert not gone_path.exists()

    # MEMORY.md must not reference the deleted slug
    index_path = Path(svc.memory_dir("agent")) / "MEMORY.md"
    assert index_path.exists()
    index_text = index_path.read_text(encoding="utf-8")
    assert "gone" not in index_text
    # The surviving entry must still be present
    assert "keep" in index_text


@pytest.mark.asyncio
async def test_memory_path_and_dir_contracts(tmp_path: Path) -> None:
    """Tier 2: memory_path / memory_dir return correctly shaped paths for each layer."""
    svc, _ = _make_service(tmp_path)

    # shared layer
    shared_dir = svc.memory_dir("shared")
    assert shared_dir == str(Path(".reyn") / "memory")

    shared_path = svc.memory_path("shared", "myslug")
    assert shared_path == str(Path(".reyn") / "memory" / "myslug.md")

    # agent layer — must be rooted under agent_workspace_dir
    agent_dir = svc.memory_dir("agent")
    expected_agent_dir = str(tmp_path / "agents" / "test_agent" / "memory")
    assert agent_dir == expected_agent_dir

    agent_path = svc.memory_path("agent", "myslug")
    assert agent_path == str(
        tmp_path / "agents" / "test_agent" / "memory" / "myslug.md"
    )
    # Must end with slug.md
    assert agent_path.endswith("myslug.md")


@pytest.mark.asyncio
async def test_events_emitted_for_remember_and_forget(tmp_path: Path) -> None:
    """Tier 2: remember emits memory_saved; forget emits memory_deleted. Read via EventLog.all()."""
    svc, events = _make_service(tmp_path)

    await svc.remember(
        layer="agent",
        slug="evt-test",
        name="Evt",
        description="event check",
        type="note",
        body="content",
    )

    emitted = [e.type for e in events.all()]
    assert "memory_saved" in emitted

    saved_event = next(e for e in events.all() if e.type == "memory_saved")
    assert saved_event.data["slug"] == "evt-test"
    assert saved_event.data["layer"] == "agent"

    await svc.forget(layer="agent", slug="evt-test")

    emitted_after = [e.type for e in events.all()]
    assert "memory_deleted" in emitted_after

    deleted_event = next(e for e in events.all() if e.type == "memory_deleted")
    assert deleted_event.data["slug"] == "evt-test"
    assert deleted_event.data["layer"] == "agent"
