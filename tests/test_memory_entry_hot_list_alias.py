"""Tier 2 tests for memory_entry hot-list alias surfacing (2026-05-17 N4).

When a user saves a memory entry via ``memory_operation__remember_shared``,
the file lands at ``<cwd>/.reyn/memory/<slug>.md``. In a fresh subsequent
session, the LLM needs to be able to read that entry without first running
``list_actions(category=['memory_entry'])`` — the weak default model rarely
takes that discovery step proactively (= empirical observation, e2e-coder
N4-d probe).

This is accomplished by:

1. ``_enumerate_shared_memory_entries(host)`` — scans the shared-layer
   directory and returns ``{memory_entry__<slug>: {description}}`` from
   each entry's frontmatter.
2. The router's hot-list-seed builder extends the static seed with these
   discovered names so the aliases appear in ``tools=`` at session start.
3. ``_resource_alias_metadata`` renders a human description from the
   entry's frontmatter when the alias is exposed.
4. ``_read_memory_body_args`` in ``universal_dispatch`` was updated to
   emit ``{layer: "shared", slug}`` instead of ``{name}``, matching the
   target ``read_memory_body`` parameter shape (= the dispatch shape
   mismatch documented in the prior memory-entry deferred entry).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.router_loop import (
    _enumerate_shared_memory_entries,
    _resource_alias_metadata,
)
from reyn.tools.universal_dispatch import _read_memory_body_args


class _FakeHost:
    def __init__(self, mem_dir: Path | None) -> None:
        self._mem_dir = mem_dir

    def memory_dir(self, layer: str) -> str:
        if layer == "shared" and self._mem_dir is not None:
            return str(self._mem_dir)
        return ""


# ── _enumerate_shared_memory_entries ────────────────────────────────────────


def test_enumerate_returns_entries_keyed_by_qualified_name(tmp_path):
    """Tier 2: each .md file under shared/ produces a memory_entry__<slug> key."""
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "user_project_phoenix.md").write_text(
        "---\nname: Project Phoenix\ndescription: User is working on Phoenix.\ntype: project\n---\n\nbody",
        encoding="utf-8",
    )
    (mem_dir / "user_language_python.md").write_text(
        "---\nname: Python\ndescription: User writes Python 3.12.\ntype: project\n---\n\nbody",
        encoding="utf-8",
    )
    host = _FakeHost(mem_dir)

    result = _enumerate_shared_memory_entries(host)

    assert set(result.keys()) == {
        "memory_entry__user_project_phoenix",
        "memory_entry__user_language_python",
    }


def test_enumerate_extracts_description_from_frontmatter(tmp_path):
    """Tier 2: frontmatter's ``description`` field surfaces as the alias description."""
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "user_project_phoenix.md").write_text(
        "---\nname: Project Phoenix\ndescription: User is working on Phoenix with Python 3.12.\ntype: project\n---\n\nbody",
        encoding="utf-8",
    )
    host = _FakeHost(mem_dir)

    result = _enumerate_shared_memory_entries(host)

    assert result["memory_entry__user_project_phoenix"]["description"] == (
        "User is working on Phoenix with Python 3.12."
    )


def test_enumerate_skips_memory_index(tmp_path):
    """Tier 2: ``MEMORY.md`` is the index, not an entry — must not be aliased."""
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
    (mem_dir / "entry.md").write_text(
        "---\nname: x\ndescription: y\ntype: project\n---\n\nbody",
        encoding="utf-8",
    )
    host = _FakeHost(mem_dir)

    result = _enumerate_shared_memory_entries(host)

    assert "memory_entry__MEMORY" not in result
    assert "memory_entry__entry" in result


def test_enumerate_returns_empty_when_memory_dir_absent(tmp_path):
    """Tier 2: no shared memory dir → empty dict, no exception (cold-start case)."""
    host = _FakeHost(tmp_path / "no_such_dir")

    result = _enumerate_shared_memory_entries(host)

    assert result == {}


def test_enumerate_returns_empty_when_host_lacks_memory_dir():
    """Tier 2: a host without ``memory_dir`` (= legacy / mock) returns empty, no crash."""

    class _HostNoMemoryDir:
        pass

    result = _enumerate_shared_memory_entries(_HostNoMemoryDir())

    assert result == {}


def test_enumerate_handles_missing_frontmatter_gracefully(tmp_path):
    """Tier 2: an entry without frontmatter still yields a qualified name, with a generic description."""
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "plain.md").write_text("just text, no frontmatter", encoding="utf-8")
    host = _FakeHost(mem_dir)

    result = _enumerate_shared_memory_entries(host)

    assert "memory_entry__plain" in result
    # Falls back to the placeholder generic description.
    assert "plain" in result["memory_entry__plain"]["description"]


# ── _resource_alias_metadata: memory_entry case ─────────────────────────────


def test_resource_alias_memory_entry_renders_description():
    """Tier 2: memory_entry alias surfaces with the user-supplied description."""
    skill_meta = {
        "memory_entry__user_project_phoenix": {
            "description": "User is working on Phoenix with Python 3.12.",
        },
    }

    result = _resource_alias_metadata(
        "memory_entry__user_project_phoenix",
        skill_metadata_lookup=skill_meta,
        mcp_tool_lookup=None,
    )

    assert result is not None
    description, params = result
    assert "user_project_phoenix" in description
    assert "User is working on Phoenix with Python 3.12." in description


def test_resource_alias_memory_entry_has_empty_params():
    """Tier 2: memory_entry alias takes no args — the slug is encoded in the alias name."""
    result = _resource_alias_metadata(
        "memory_entry__foo",
        skill_metadata_lookup={"memory_entry__foo": {"description": "any"}},
        mcp_tool_lookup=None,
    )

    assert result is not None
    _, params = result
    assert params == {"type": "object", "properties": {}, "required": []}


def test_resource_alias_memory_entry_without_meta_still_resolves():
    """Tier 2: a memory_entry alias must surface even when no metadata is plumbed,
    so the LLM still discovers the action; description falls back to a placeholder."""
    result = _resource_alias_metadata(
        "memory_entry__orphan",
        skill_metadata_lookup=None,
        mcp_tool_lookup=None,
    )

    assert result is not None
    description, params = result
    assert "orphan" in description
    assert params["properties"] == {}


# ── _read_memory_body_args transform ────────────────────────────────────────


def test_read_memory_body_args_sends_canonical_layer_slug_pair():
    """Tier 2: transform output must match ``read_memory_body`` parameters
    (= ``{layer, slug}``). Previously sent ``{name}``, causing dispatch failure when
    a memory_entry alias was invoked (= the 'pre-existing dispatch shape mismatch'
    deferred in FP-0034 D2-full).
    """
    args = _read_memory_body_args("user_project_phoenix", {})

    assert args == {"layer": "shared", "slug": "user_project_phoenix"}


def test_read_memory_body_args_ignores_caller_args():
    """Tier 2: extra caller-supplied args are not forwarded — the alias is curried,
    the qualified name supplies the only routing info needed."""
    args = _read_memory_body_args("some_slug", {"unrelated": "value"})

    assert "unrelated" not in args
    assert args == {"layer": "shared", "slug": "some_slug"}
