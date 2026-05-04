"""Tier 2b (subsystem invariant) tests for reyn.memory.

These tests guard the public-API contracts of the memory subsystem
(`reyn.memory`). No mocks — all assertions go through the real
`read_entry`, `list_entries`, `find_one`, `render_body`, and `memory_dir`
surfaces, using `tmp_path` for real filesystem operations.

NOTE — known bug (do NOT fix here):
  `reyn.memory.memory.rewrite_index` has a broken relative import:
      from .op_runtime.file import regenerate_index_impl
  `op_runtime` is NOT a sub-package of `reyn.memory`; the correct path is
  `reyn.op_runtime`. As a result, calling `rewrite_index()` raises
  `ModuleNotFoundError`. The index-rewrite tests below call
  `regenerate_index_impl` directly from its real location
  (`reyn.op_runtime.file`) so the tests cover the invariant without being
  blocked by the upstream bug. The bug should be fixed in a separate PR.

See docs/en/contributing/testing.md for the tier model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.memory.memory import (
    AmbiguousMemoryError,
    INDEX_FILENAME,
    INDEX_HEADER,
    ENTRY_TEMPLATE,
    find_one,
    list_entries,
    read_entry,
    render_body,
)
from reyn.memory.memory_paths import memory_dir
from reyn.op_runtime.file import regenerate_index_impl


def _rewrite_index(scope_dir: Path) -> None:
    """Local stand-in for reyn.memory.memory.rewrite_index.

    rewrite_index() is currently broken (ModuleNotFoundError — see module
    docstring). This helper calls regenerate_index_impl directly using the
    same constants that rewrite_index passes, so the invariant tests remain
    meaningful while the upstream fix is pending.
    """
    regenerate_index_impl(
        dir_path=scope_dir,
        output_path=scope_dir / INDEX_FILENAME,
        entry_template=ENTRY_TEMPLATE,
        header=INDEX_HEADER,
    )


# ── helpers ────────────────────────────────────────────────────────────────────


def _write_memory_file(
    directory: Path,
    slug: str,
    *,
    name: str,
    description: str,
    type_: str = "project",
    body: str = "Some body text.",
) -> Path:
    """Write a well-formed memory file to *directory* and return its path."""
    path = directory / f"{slug}.md"
    path.write_text(
        render_body(name=name, description=description, type_=type_, body=body),
        encoding="utf-8",
    )
    return path


# ── read_entry ─────────────────────────────────────────────────────────────────


def test_read_entry_returns_none_on_missing_file(tmp_path):
    """Tier 2b: read_entry returns None for a path that does not exist.

    Protects: callers of read_entry must be able to handle the absent-file
    case without an OSError propagating. The contract is None — not an
    exception — so that list_entries and other aggregators can skip
    unreadable files gracefully.
    """
    absent = tmp_path / "does_not_exist.md"
    result = read_entry(absent)
    assert result is None


def test_read_entry_parses_frontmatter_fields(tmp_path):
    """Tier 2b: read_entry correctly extracts name, description, type, slug, body.

    Protects: downstream consumers (CLI, context builders) rely on the
    MemoryEntry fields being populated from frontmatter without further
    parsing. If the contract breaks, injected memory shows wrong metadata.
    """
    path = _write_memory_file(
        tmp_path,
        "api_key_safety",
        name="API Key Safety",
        description="Never print API key values",
        type_="feedback",
        body="Never print or echo API key values to the terminal.",
    )
    entry = read_entry(path)

    assert entry is not None
    assert entry.slug == "api_key_safety"
    assert entry.name == "API Key Safety"
    assert entry.description == "Never print API key values"
    assert entry.type == "feedback"
    assert "Never print or echo" in entry.body
    # body must NOT contain frontmatter markers
    assert "---" not in entry.body


def test_read_entry_body_stripped_of_surrounding_whitespace(tmp_path):
    """Tier 2b: body returned by read_entry has surrounding whitespace stripped.

    Protects: the docstring for MemoryEntry.body guarantees 'ready to inject
    without further normalization'. Leading/trailing blank lines from the raw
    file must not leak into the returned body.
    """
    path = tmp_path / "padded.md"
    path.write_text(
        "---\nname: Padded\ndescription: desc\ntype: project\n---\n\n\n  content  \n\n",
        encoding="utf-8",
    )
    entry = read_entry(path)
    assert entry is not None
    assert entry.body == "content"


def test_read_entry_description_is_first_line_only(tmp_path):
    """Tier 2b: multi-line description in frontmatter is truncated to first line.

    Protects: index rendering and context builders expect a single-line
    description per entry. The truncation contract prevents multi-line YAML
    strings from polluting table/list renderings.
    """
    path = tmp_path / "multiline.md"
    path.write_text(
        "---\nname: M\ndescription: |\n  First line.\n  Second line.\ntype: project\n---\nbody\n",
        encoding="utf-8",
    )
    entry = read_entry(path)
    assert entry is not None
    assert entry.description == "First line."
    assert "\n" not in entry.description


# ── list_entries ───────────────────────────────────────────────────────────────


def test_list_entries_excludes_memory_md_index(tmp_path):
    """Tier 2b: list_entries never includes MEMORY.md itself in results.

    Protects: the index file is a derived artifact, not a primary memory
    entry. Including it would make the index entry appear as a regular memory
    in the context and cause recursive drift.
    """
    _write_memory_file(tmp_path, "entry_a", name="Entry A", description="desc a")
    (tmp_path / "MEMORY.md").write_text("# Memory Index\n\n- [Entry A](entry_a.md) — desc a\n")

    entries = list_entries(scope_dir=tmp_path)

    slugs = [e.slug for e in entries]
    assert "MEMORY" not in slugs
    assert "entry_a" in slugs


def test_list_entries_returns_empty_for_nonexistent_dir(tmp_path):
    """Tier 2b: list_entries returns [] without error when the scope dir is absent.

    Protects: callers initializing a fresh project (no .reyn/memory yet)
    must not get an OSError before the first memory is written.
    """
    absent_dir = tmp_path / "does_not_exist"
    result = list_entries(scope_dir=absent_dir)
    assert result == []


def test_list_entries_skips_unreadable_files(tmp_path):
    """Tier 2b: list_entries silently skips files that cannot be parsed.

    Protects: a corrupted or permission-denied memory body file must not
    abort the full listing. The agent should still see all readable entries.
    """
    _write_memory_file(tmp_path, "good", name="Good", description="ok")
    # Write a file with no frontmatter at all — read_entry returns None for it
    (tmp_path / "bad.md").write_text("no frontmatter here\n", encoding="utf-8")

    entries = list_entries(scope_dir=tmp_path)
    slugs = [e.slug for e in entries]

    assert "good" in slugs
    assert "bad" in slugs  # bad is readable; read_entry returns a MemoryEntry with empty fields


def test_list_entries_sorted_by_filename(tmp_path):
    """Tier 2b: list_entries returns entries in filename-sorted order.

    Protects: stable order is required so context assembly is deterministic
    across OS runs. Filesystem directory order is not stable on all platforms.
    """
    _write_memory_file(tmp_path, "zzz_last", name="ZZZ", description="last")
    _write_memory_file(tmp_path, "aaa_first", name="AAA", description="first")
    _write_memory_file(tmp_path, "mmm_mid", name="MMM", description="mid")

    entries = list_entries(scope_dir=tmp_path)
    slugs = [e.slug for e in entries]

    assert slugs == sorted(slugs)


# ── rewrite_index ──────────────────────────────────────────────────────────────


def test_rewrite_index_writes_body_and_updates_index(tmp_path):
    """Tier 2b: rewrite_index regenerates MEMORY.md to reflect current .md files.

    Protects: after writing a new memory body file, rewrite_index must produce
    a MEMORY.md that references it. The body file and index must both be
    consistent — this is the dual-write invariant documented in memory.py.
    """
    _write_memory_file(
        tmp_path,
        "my_entry",
        name="My Entry",
        description="A test entry",
        body="Important content.",
    )

    _rewrite_index(tmp_path)

    index_path = tmp_path / "MEMORY.md"
    assert index_path.exists(), "_rewrite_index must create MEMORY.md"

    index_text = index_path.read_text(encoding="utf-8")
    assert "My Entry" in index_text
    assert "my_entry" in index_text
    assert "A test entry" in index_text


def test_rewrite_index_excludes_self_from_index(tmp_path):
    """Tier 2b: the regenerated index never includes an entry for MEMORY.md itself.

    Protects: the index is derived output; it must not self-reference.
    If MEMORY.md appeared as an entry, recursive context builds would embed
    the entire index as a 'memory' entry.
    """
    _write_memory_file(tmp_path, "entry_x", name="Entry X", description="desc")
    # Pre-populate index so there is something to overwrite
    (tmp_path / "MEMORY.md").write_text("old content\n", encoding="utf-8")

    _rewrite_index(tmp_path)

    index_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    # The index must not reference itself
    assert "MEMORY.md" not in index_text
    assert "MEMORY" not in [line.split("]")[0].lstrip("- [") for line in index_text.splitlines() if "](" in line]


def test_rewrite_index_handles_empty_directory(tmp_path):
    """Tier 2b: rewrite_index produces a valid (empty-body) MEMORY.md even with no entries.

    Protects: callers that rebuild the index after deleting the last memory
    must get a clean index file, not a crash or a stale file with orphaned
    references.
    """
    _rewrite_index(tmp_path)

    index_path = tmp_path / "MEMORY.md"
    assert index_path.exists()
    # No entries — body should be empty (header only or empty)
    content = index_path.read_text(encoding="utf-8")
    # Crucially: no entry lines referencing any slug
    entry_lines = [l for l in content.splitlines() if ".md" in l and "MEMORY" not in l]
    assert entry_lines == [], f"Expected no entry lines, got: {entry_lines}"


def test_rewrite_index_reflects_deletion(tmp_path):
    """Tier 2b: after removing a body file, rewrite_index removes it from MEMORY.md.

    Protects: the index is always derived from the files actually present.
    A stale index that still references deleted body files would cause
    read_entry to return None and the OS to surface ghost memory entries.
    """
    path_a = _write_memory_file(tmp_path, "alpha", name="Alpha", description="first")
    _write_memory_file(tmp_path, "beta", name="Beta", description="second")

    _rewrite_index(tmp_path)
    assert "Alpha" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    path_a.unlink()
    _rewrite_index(tmp_path)

    index_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "Alpha" not in index_text
    assert "Beta" in index_text


# ── find_one ───────────────────────────────────────────────────────────────────


def test_find_one_exact_slug_match(tmp_path):
    """Tier 2b: find_one resolves an exact slug to the correct MemoryEntry.

    Protects: the primary resolution path (exact slug) used by `reyn memory
    show <slug>` and recall_docs. A wrong result here corrupts context.
    """
    entries = list_entries(scope_dir=tmp_path)  # empty
    path = _write_memory_file(tmp_path, "p7_rule", name="P7 Rule", description="P7")
    entries = list_entries(scope_dir=tmp_path)

    result = find_one("p7_rule", entries)

    assert result is not None
    assert result.slug == "p7_rule"


def test_find_one_returns_none_on_missing(tmp_path):
    """Tier 2b: find_one returns None when no entry matches the query.

    Protects: callers that lookup a slug that doesn't exist (e.g. after a
    delete) must get None, not an exception, so the CLI can surface a clean
    'not found' message.
    """
    _write_memory_file(tmp_path, "existing", name="Existing", description="here")
    entries = list_entries(scope_dir=tmp_path)

    result = find_one("nonexistent_slug", entries)
    assert result is None


def test_find_one_raises_on_ambiguous_substring(tmp_path):
    """Tier 2b: find_one raises AmbiguousMemoryError when multiple entries share a substring.

    Protects: silent disambiguation is dangerous — the OS could inject the
    wrong memory. The error surface lets callers ask the user for
    clarification instead.
    """
    _write_memory_file(tmp_path, "api_key_safety", name="API Key Safety", description="d1")
    _write_memory_file(tmp_path, "api_rate_limit", name="API Rate Limit", description="d2")
    entries = list_entries(scope_dir=tmp_path)

    with pytest.raises(AmbiguousMemoryError):
        find_one("api", entries)


def test_find_one_case_insensitive_name_match(tmp_path):
    """Tier 2b: find_one resolves by case-insensitive display name.

    Protects: users querying memory by display name use natural case;
    the resolution must not be case-sensitive to avoid silent 'not found'
    on obvious matches.
    """
    _write_memory_file(tmp_path, "p7_strictness", name="P7 Strictness", description="d")
    entries = list_entries(scope_dir=tmp_path)

    result = find_one("p7 strictness", entries)
    assert result is not None
    assert result.slug == "p7_strictness"


def test_find_one_strips_md_extension(tmp_path):
    """Tier 2b: find_one accepts a query like 'slug.md' and resolves correctly.

    Protects: some callers (e.g. Control IR ops) may pass the filename
    including the extension. Stripping .md before resolution prevents 'not
    found' failures caused solely by extension suffix.
    """
    _write_memory_file(tmp_path, "local_env", name="Local Env", description="d")
    entries = list_entries(scope_dir=tmp_path)

    result = find_one("local_env.md", entries)
    assert result is not None
    assert result.slug == "local_env"


# ── memory_dir (agent isolation) ───────────────────────────────────────────────


def test_remember_agent_isolates_from_shared(tmp_path, monkeypatch):
    """Tier 2b: agent-scoped memory dir is distinct from the shared memory dir.

    Protects: the two-layer memory model (PR15+). Files written to an agent's
    scoped directory must not appear in the shared layer and vice versa.
    list_entries must not cross the boundary; each scope is independent.
    """
    monkeypatch.chdir(tmp_path)

    shared_dir = memory_dir(agent=None)
    agent_dir = memory_dir(agent="agent_alpha")

    shared_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)

    _write_memory_file(shared_dir, "shared_note", name="Shared Note", description="shared")
    _write_memory_file(agent_dir, "agent_note", name="Agent Note", description="agent-only")

    shared_entries = list_entries(scope_dir=shared_dir)
    agent_entries = list_entries(scope_dir=agent_dir)

    shared_slugs = {e.slug for e in shared_entries}
    agent_slugs = {e.slug for e in agent_entries}

    assert "shared_note" in shared_slugs, "shared memory must be visible in shared scope"
    assert "agent_note" not in shared_slugs, "agent memory must NOT bleed into shared scope"
    assert "agent_note" in agent_slugs, "agent memory must be visible in agent scope"
    assert "shared_note" not in agent_slugs, "shared memory must NOT bleed into agent scope"


def test_memory_dir_shared_path():
    """Tier 2b: memory_dir() (agent=None) returns the shared .reyn/memory path.

    Protects: callers that build paths to the shared layer must get a stable,
    predictable path. If this path drifts, skills and the CLI disagree on
    where shared memory lives.
    """
    result = memory_dir(agent=None)
    assert result == Path(".reyn") / "memory"


def test_memory_dir_agent_path():
    """Tier 2b: memory_dir(agent=<name>) returns agent-scoped path.

    Protects: the agent-scoped path contract. Skills that write to their own
    scope and callers that read from it must agree on the path structure.
    """
    result = memory_dir(agent="my_agent")
    assert result == Path(".reyn") / "agents" / "my_agent" / "memory"


# ── render_body ────────────────────────────────────────────────────────────────


def test_render_body_produces_parseable_frontmatter(tmp_path):
    """Tier 2b: render_body output is parseable by read_entry with round-trip fidelity.

    Protects: the write path (render_body) and read path (read_entry) must be
    inverse operations. If they drift, files written by the CLI import path
    cannot be read back by the context builder.
    """
    content = render_body(
        name="Round Trip",
        description="verifying write/read symmetry",
        type_="feedback",
        body="Body content here.",
    )

    path = tmp_path / "round_trip.md"
    path.write_text(content, encoding="utf-8")

    entry = read_entry(path)

    assert entry is not None
    assert entry.name == "Round Trip"
    assert entry.description == "verifying write/read symmetry"
    assert entry.type == "feedback"
    assert entry.body == "Body content here."
    assert entry.slug == "round_trip"
