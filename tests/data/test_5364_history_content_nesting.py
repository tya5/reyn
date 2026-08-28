"""Tier 2: #5364 — the new ``.reyn/memory/history-content/<session_id>/``
tool-result store must never be visible to any of the FOUR non-recursive
``memory/`` scanners (#5364's own "★★4 つとも非再帰 glob("*.md") + file
ごとに全文read_text" census, tui-coder — as of 2026-08-28):

  1. ``reyn.data.memory.memory.list_entries``
  2. ``reyn.data.index.knowledge_ingest._iter_memory_entries``
  3. ``reyn.tools.memory._regenerate_index`` (exercised via
     ``reyn.data.memory.memory.rewrite_index``, its own public wrapper —
     see #4901's own resolved-import fix, which is the SAME seam a
     ``remember_shared``/``remember_agent``/``forget_memory`` tool call
     drives in production)
  4. ``reyn.core.op_runtime.file.regenerate_index_impl``

Each is driven directly (not mocked) against ONE shared fixture directory
that places both a genuine memory entry directly under ``memory/`` AND a
``.md`` file nested under ``memory/history-content/<something>/`` — the
architect correction on #5364 (issuecomment-5446911378-class finding): a
test covering only 1 of the 4 scanners leaves the other 3 unguarded if
someone later ``rglob``s just one of them. Depth is what matters here, not
the ``history-content`` name specifically (#5364 §2 test②'s own caveat) —
so the nested probe uses a name unrelated to this store, and would catch
ANY future subtree gaining depth under ``memory/``, not just this one.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.index.knowledge_ingest import _iter_memory_entries
from reyn.data.memory.memory import list_entries, rewrite_index
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext

_PRESENT_SLUG = "present-entry"
_PRESENT_BODY = (
    "---\nname: present-entry\ndescription: a real, directly-placed memory entry\n"
    "---\n\nbody text here.\n"
)
_NESTED_SUBTREE = "some-nested-subtree"  # deliberately NOT "history-content"


def _make_project(tmp_path: Path) -> Path:
    """Places one genuine memory entry directly under memory/ (PRESENT
    side — a scan that silently stops finding real entries would still go
    green on the absence-only check alone, #5364 §2 test②'s own warning)
    AND one .md file nested one level deep (the invariant under test)."""
    memory_dir = tmp_path / ".reyn" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / f"{_PRESENT_SLUG}.md").write_text(_PRESENT_BODY, encoding="utf-8")

    nested_dir = memory_dir / _NESTED_SUBTREE / "some-session-id"
    nested_dir.mkdir(parents=True)
    (nested_dir / "leaked.md").write_text(
        "---\nname: leaked\ndescription: must never be seen by a memory/ scanner\n"
        "---\n\nif this appears in a scanner's output, nesting is not honored.\n",
        encoding="utf-8",
    )
    return memory_dir


def _tool_ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    # memory.py's fallback path writes an ABSOLUTE state_dir path —
    # Workspace's own _resolve_write requires an explicit permission
    # grant for absolute writes, so Workspace needs its OWN
    # permission_resolver (not just ToolContext's), anchored at tmp_path
    # (same pattern as test_fp0066_p3a_knowledge_ingest.py::_make_ctx).
    perm = PermissionResolver({}, project_root=tmp_path)
    return ToolContext(
        events=events,
        permission_resolver=perm,
        workspace=Workspace(events=events, base_dir=tmp_path, permission_resolver=perm),
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def test_list_entries_present_and_scanner_1_of_4(tmp_path: Path) -> None:
    """Tier 2: scanner ① (reyn.data.memory.memory.list_entries) sees the
    real entry (present side) and does NOT see the nested leaked one."""
    memory_dir = _make_project(tmp_path)

    entries = list_entries(scope_dir=memory_dir)

    names = {e.path.stem for e in entries}
    assert _PRESENT_SLUG in names, "the real, directly-placed entry must still be found"
    assert "leaked" not in names, "a nested .md must not be picked up (non-recursive glob)"


def test_iter_memory_entries_present_and_scanner_2_of_4(tmp_path: Path) -> None:
    """Tier 2: scanner ② (knowledge_ingest._iter_memory_entries) sees the
    real entry and does NOT see the nested leaked one."""
    _make_project(tmp_path)

    entries = _iter_memory_entries(tmp_path)

    slugs = {slug for _, slug, _ in entries}
    assert _PRESENT_SLUG in slugs, "the real, directly-placed entry must still be found"
    assert "leaked" not in slugs, "a nested .md must not be picked up (non-recursive glob)"


def test_regenerate_index_present_and_scanner_3_of_4(tmp_path: Path) -> None:
    """Tier 2: scanner ③ (tools.memory._regenerate_index, driven through
    its real production caller — remember_shared, which rebuilds the
    index after every mutation) sees the real entry and does NOT see the
    nested leaked one, in the rendered MEMORY.md body."""
    _make_project(tmp_path)
    ctx = _tool_ctx(tmp_path)

    result = asyncio.run(
        invoke_tool(
            get_default_registry(), "remember_shared",
            {
                "slug": "another-entry", "name": "another-entry",
                "description": "triggers a real index rebuild",
                "type": "reference", "body": "x",
            },
            ctx,
        ),
    )
    assert "error" not in result, f"remember_shared failed: {result!r}"
    assert result.get("saved") == "another-entry"

    index_text = (tmp_path / ".reyn" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert _PRESENT_SLUG in index_text, "the real, directly-placed entry must still be indexed"
    assert "leaked" not in index_text, (
        "a nested .md must not be picked up (non-recursive glob) — "
        f"got:\n{index_text}"
    )


def test_regenerate_index_impl_present_and_scanner_4_of_4(tmp_path: Path) -> None:
    """Tier 2: scanner ④ (op_runtime.file.regenerate_index_impl, driven
    through rewrite_index — its real production wrapper, #4901) sees the
    real entry and does NOT see the nested leaked one."""
    memory_dir = _make_project(tmp_path)

    rewrite_index(memory_dir)

    index_text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert _PRESENT_SLUG in index_text, "the real, directly-placed entry must still be indexed"
    assert "leaked" not in index_text, (
        "a nested .md must not be picked up (non-recursive glob) — "
        f"got:\n{index_text}"
    )


def test_the_actual_5364_store_lands_under_the_nested_nonleaking_shape(
    tmp_path: Path,
) -> None:
    """Tier 2: #5364 §2 test③ — the REAL store (not a hand-placed probe)
    never writes a .md directly under memory/, driven through the real
    write API."""
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig

    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="test-agent",
        session_id="a-real-session",
    )
    block = store.save_tool_result("some tool output", mime_type="text/plain")

    memory_dir = tmp_path / ".reyn" / "memory"
    written = tmp_path / block["path"]
    assert written.is_file(), "sanity: the store must actually have written the file"
    # #5369 TESTS-READ (architect): the invariant under test is DEPTH, not
    # a name. `direct_md_files == []` was vacuous — mime_type="text/plain"
    # never produces a .md file at all, so a regression to a FLAT (non-
    # nested) layout would still pass it. Checking the written file's own
    # parent is not memory_dir itself catches that regression regardless
    # of what the nested directory happens to be named (config-independent
    # — a future rename of MediaStoreConfig.history_content_dir's default
    # does not need this test to change).
    assert written.parent != memory_dir, (
        f"save_tool_result must never place a file directly under memory/ "
        f"(flat, unnested) — got {written!r} whose parent is memory_dir itself"
    )
    # lead-coder (#5369 BLOCKING, taken same-PR per house rule 6): the assert
    # above only rules out "directly under memory/" — a regression that
    # writes back to the pre-#5364 `.reyn/tool-results/` (audit tier, a
    # sibling of memory/, not a descendant) would otherwise pass it too.
    # This checks the OTHER half: the write must land somewhere IN the
    # memory/ tree, not merely "not directly under" some other tree.
    assert memory_dir in written.parents, (
        f"save_tool_result must write inside memory/ — got {written!r}, "
        f"which is not under {memory_dir!r} at all (e.g. a regression that "
        f"writes back to `.reyn/tool-results/` would otherwise pass)"
    )
