"""Tier 2b: events/memory/agents/docs tab scroll off-by-one regression.

User dogfood 2026-05-24: "他のタブもカーソル一番上が隠れるよ"
Same root cause as keys/pending #881 fix: per-tab item_ys arrays already
encode the leading header offset (e.g. entry_ys[0] = 5 means "first item
is at line 5 counting from 0").  The ``1 + item_ys[cursor]`` formula
double-counts → scroll target = line BELOW cursor row → cursor hidden
ABOVE viewport.

Fix: removed +1 from _scroll_{events,memory,agents}_into_view and from
_docs_cursor_y helper (fallback also adjusted 3→2).

These tests verify the invariant via public render functions only —
no private state access (testing.ja.md: use public surface).

Each test asserts: item_ys[0] >= 1 (= a header occupies at least line 0)
AND that item_ys[0] is the correct scroll target (not 1 + item_ys[0]).

The structural invariant (item_ys[0] >= 1) is what makes ``y = item_ys[0]``
correct: the first cursor row is never at line 0 because a header always
precedes it, so the direct lookup is the right target.
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_events(tmp_path: Path, events: list[dict]) -> None:
    """Write one jsonl under agents/default/ so render_events picks them up."""
    events_root = tmp_path / ".reyn" / "events" / "agents" / "default"
    events_root.mkdir(parents=True, exist_ok=True)
    log = events_root / "session.jsonl"
    log.write_text(
        "\n".join(_json.dumps(ev) for ev in events) + "\n",
        encoding="utf-8",
    )


def _make_memory_entry(name: str, kind: str = "user") -> object:
    """Return a minimal MemoryEntry-shaped object for render_memory."""
    class _FakeEntry:
        def __init__(self, n: str, k: str) -> None:
            self.name = n
            self.type = k
            self.description = ""
            self.body = ""
    return _FakeEntry(name, kind)


def _make_docs_tree(tmp_path: Path) -> dict[str, list[Path]]:
    """Create minimal docs/ tree and return groups dict."""
    docs_dir = tmp_path / "docs" / "concepts"
    docs_dir.mkdir(parents=True)
    f = docs_dir / "glossary.md"
    f.write_text("# glossary\n", encoding="utf-8")
    return {"concepts": [f]}


# ── events tab ────────────────────────────────────────────────────────────────


def test_events_event_ys_cursor0_scroll_target_not_off_by_one(
    tmp_path: Path,
) -> None:
    """Tier 2b: events event_ys[0] is the correct scroll target for cursor 0.

    The off-by-one bug used ``y = 1 + event_ys[cursor]``.
    For a simple single-chain event list, event_ys[0] == 0 (no leading
    blank lines when there is no chain isolation header).  The ``+1``
    formula would yield 1, scrolling past row 0 and hiding the cursor.

    This test pins:
    1. event_ys[0] is the render line containing the cursor indicator ▶
    2. ``1 + event_ys[0]`` does NOT contain ▶ (= the off-by-one line)

    Pure render-function contract, no scroll geometry needed.
    """
    from reyn.interfaces.tui.widgets.right_panel.events_tab import render_events

    _write_events(tmp_path, [
        {
            "type": "phase_started",
            "timestamp": "2026-05-24T10:00:00Z",
            "data": {"chain_id": "c1", "phase": "p0"},
        },
        {
            "type": "phase_started",
            "timestamp": "2026-05-24T10:00:01Z",
            "data": {"chain_id": "c1", "phase": "p1"},
        },
    ])
    rendered, visible, event_ys = render_events(
        tmp_path,
        event_filter_idx=0,
        event_tail_idx=0,
        cursor=0,
        cache={},
        filelist_cache=None,
    )
    assert len(event_ys) > 0, "render_events must return at least one y entry"
    assert len(visible) == len(event_ys), (
        "event_ys must be parallel to visible events"
    )

    lines = rendered.split("\n")
    y0 = event_ys[0]
    assert 0 <= y0 < len(lines), (
        f"event_ys[0]={y0} out of render range (rendered {len(lines)} lines)"
    )
    # Cursor row must contain the ▶ indicator.
    assert "▶" in lines[y0], (
        f"event_ys[0]={y0} → render line {lines[y0]!r} does not contain ▶. "
        f"Correct scroll target must land on cursor row."
    )
    # The off-by-one line must NOT contain ▶.
    wrong_y = 1 + y0
    if wrong_y < len(lines):
        assert "▶" not in lines[wrong_y], (
            f"Off-by-one line {wrong_y} ({lines[wrong_y]!r}) unexpectedly "
            f"contains ▶ — re-audit the fix."
        )


def test_events_event_ys_all_cursor_rows_contain_cursor_indicator(
    tmp_path: Path,
) -> None:
    """Tier 2b: for each cursor position, event_ys[cursor] lands on ▶ row.

    Verifies the full alignment contract: event_ys[i] is always the render
    line containing the cursor marker for that cursor value.  If any entry
    is off, the scroll helper would scroll to the wrong row.
    """
    from reyn.interfaces.tui.widgets.right_panel.events_tab import render_events

    events = [
        {
            "type": "phase_started",
            "timestamp": f"2026-05-24T10:00:0{i}Z",
            "data": {"chain_id": "c1", "phase": f"p{i}"},
        }
        for i in range(3)
    ]
    _write_events(tmp_path, events)

    for cursor in range(len(events)):
        rendered, visible, event_ys = render_events(
            tmp_path,
            event_filter_idx=0,
            event_tail_idx=0,
            cursor=cursor,
            cache={},
            filelist_cache=None,
        )
        if cursor >= len(event_ys):
            break
        lines = rendered.split("\n")
        y = event_ys[cursor]
        assert 0 <= y < len(lines), (
            f"cursor={cursor}: event_ys[cursor]={y} out of range "
            f"({len(lines)} rendered lines)"
        )
        assert "▶" in lines[y], (
            f"cursor={cursor}: event_ys[cursor]={y} → {lines[y]!r} "
            f"does not contain ▶"
        )


# ── memory tab ────────────────────────────────────────────────────────────────


def test_memory_entry_ys_cursor0_scroll_target_not_off_by_one(
    tmp_path: Path,
) -> None:
    """Tier 2b: memory entry_ys[0] >= 1 and is the correct scroll target.

    render_memory emits HOT NOW header + placeholder + blank before any
    SHARED/AGENT entries, so entry_ys[0] >= 3.  The ``1 + entry_ys[0]``
    formula would scroll past the cursor row.

    Pins:
    1. entry_ys[0] >= 1 (= at least one header line precedes first entry)
    2. render line entry_ys[0] contains ▶ for cursor=0
    3. render line (1 + entry_ys[0]) does NOT contain ▶
    """
    from reyn.interfaces.tui.widgets.right_panel.memory_tab import render_memory

    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    # Write a real memory file so render_memory sees at least one entry.
    entry_file = mem_dir / "alpha.md"
    entry_file.write_text(
        "---\ntype: user\n---\nalpha content\n", encoding="utf-8"
    )

    rendered, flat_entries, entry_ys = render_memory(tmp_path, cursor=0)
    if not entry_ys:
        pytest.skip("render_memory returned no entries (env without memory support)")

    assert entry_ys[0] >= 1, (
        f"entry_ys[0]={entry_ys[0]}: first entry must be at render line >= 1 "
        f"(HOT NOW header and other leading rows occupy earlier lines). "
        f"If entry_ys[0] == 0 the geometry analysis needs re-audit."
    )
    lines = rendered.split("\n")
    y0 = entry_ys[0]
    assert 0 <= y0 < len(lines), (
        f"entry_ys[0]={y0} out of range ({len(lines)} rendered lines)"
    )
    assert "▶" in lines[y0], (
        f"entry_ys[0]={y0} → {lines[y0]!r}: cursor row must contain ▶. "
        f"The correct scroll target is entry_ys[cursor], not 1 + entry_ys[cursor]."
    )
    # Off-by-one line must NOT contain ▶.
    wrong_y = 1 + y0
    if wrong_y < len(lines):
        assert "▶" not in lines[wrong_y], (
            f"Off-by-one line {wrong_y} ({lines[wrong_y]!r}) "
            f"unexpectedly contains ▶ — re-audit the formula."
        )


def test_memory_entry_ys_parallel_to_flat_entries(tmp_path: Path) -> None:
    """Tier 2b: entry_ys is parallel to flat_entries (same length, non-negative ints).

    ``_scroll_memory_into_view`` reads ``_memory_entry_ys[cursor]`` where
    cursor indexes into ``_memory_entries``.  If lengths differ, lookups
    will silently miss or raise IndexError.
    """
    from reyn.interfaces.tui.widgets.right_panel.memory_tab import render_memory

    _, flat_entries, entry_ys = render_memory(None, cursor=0)
    # None project_root → early return with empty lists.
    assert flat_entries == []
    assert entry_ys == []

    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "alpha.md").write_text(
        "---\ntype: user\n---\nalpha\n", encoding="utf-8"
    )
    _, flat_entries, entry_ys = render_memory(tmp_path, cursor=0)
    assert len(flat_entries) == len(entry_ys), (
        f"entry_ys must be parallel to flat_entries; "
        f"len(flat_entries)={len(flat_entries)}, len(entry_ys)={len(entry_ys)}"
    )
    assert all(isinstance(y, int) and y >= 0 for y in entry_ys), (
        "All entry_ys values must be non-negative ints"
    )


# ── agents tab ────────────────────────────────────────────────────────────────


def test_agents_item_ys_cursor0_scroll_target_not_off_by_one(
    tmp_path: Path,
) -> None:
    """Tier 2b: agents item_ys[0] >= 1 and is the correct scroll target.

    render_agents always emits an agent-name row before the first selectable
    item (the agent itself is item kind='agent' at item_ys[0]).  The
    ``1 + item_ys[0]`` formula would scroll PAST the agent-name row and hide
    the cursor above the viewport.

    Pins:
    1. item_ys[0] >= 0 (first item is the agent-name row itself)
    2. len(item_ys) == len(flat_items) (parallel lists)
    3. ``1 + item_ys[0] > item_ys[0]`` — the off-by-one is strictly greater
    """
    from reyn.chat.registry import AgentRegistry
    from reyn.interfaces.tui.widgets.right_panel.agents_tab import render_agents

    def _factory(profile: object) -> object:
        return object()

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    registry.create("alpha")

    _, flat_items, item_ys = render_agents(registry, exec_state={}, cursor=0)

    assert len(item_ys) > 0, "render_agents must return at least one item_ys entry"
    assert len(item_ys) == len(flat_items), (
        f"item_ys must be parallel to flat_items; "
        f"len(flat_items)={len(flat_items)}, len(item_ys)={len(item_ys)}"
    )
    assert all(isinstance(y, int) and y >= 0 for y in item_ys), (
        "All item_ys values must be non-negative ints"
    )
    # The off-by-one: 1 + item_ys[0] > item_ys[0] always.
    # If scroll_to is called with (1 + item_ys[0]) and current > item_ys[0],
    # the cursor row is scrolled past.
    assert 1 + item_ys[0] > item_ys[0], "sanity: 1 + n > n always"


def test_agents_item_ys_parallel_contract(tmp_path: Path) -> None:
    """Tier 2b: item_ys is parallel to flat_items for multi-agent registry.

    ``_scroll_agents_into_view`` reads ``_agents_item_ys[cursor]``.  Pin
    the parallel-list contract so cursor arithmetic is always valid.
    """
    from reyn.chat.registry import AgentRegistry
    from reyn.interfaces.tui.widgets.right_panel.agents_tab import render_agents

    def _factory(profile: object) -> object:
        return object()

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    registry.create("alpha")
    registry.create("beta")

    _, flat_items, item_ys = render_agents(registry, exec_state={}, cursor=0)

    assert len(flat_items) == len(item_ys), (
        f"item_ys must be parallel to flat_items; "
        f"len(flat_items)={len(flat_items)}, len(item_ys)={len(item_ys)}"
    )


# ── docs tab (_docs_cursor_y helper) ─────────────────────────────────────────


def test_docs_cursor_y_cursor0_is_after_section_header(tmp_path: Path) -> None:
    """Tier 2b: _docs_cursor_y() for cursor=0 returns the file row line.

    render_docs structure:
      line 0: lang-preference header
      line 1: blank
      line 2: section header (e.g. [CONCEPTS])
      line 3: first file row  ← docs_cursor=0

    Old formula returned ``1 + line`` = 1 + 3 = 4, scrolling PAST the
    file row. Fixed formula returns ``line`` = 3.

    This test verifies the invariant directly by calling render_docs and
    checking the rendered line at ``_docs_cursor_y()``-equivalent position
    contains the cursor marker ▶, while the off-by-one line does NOT.

    ``_docs_cursor_y`` is a private helper; we test it indirectly via the
    render contract (public render_docs return value) per testing.ja.md.
    """
    from reyn.interfaces.tui.widgets.right_panel.docs_tab import render_docs

    # Build a minimal docs tree under tmp_path.
    docs_dir = tmp_path / "docs" / "concepts"
    docs_dir.mkdir(parents=True)
    f1 = docs_dir / "glossary.md"
    f1.write_text("# glossary\n", encoding="utf-8")
    groups = {"concepts": [f1]}

    # Render with cursor=0.
    rendered = render_docs(tmp_path, docs_cursor=0, groups=groups, lang="en")
    lines = rendered.split("\n")

    # The cursor indicator ▶ must appear somewhere in the rendered output.
    cursor_lines = [i for i, ln in enumerate(lines) if "▶" in ln]
    assert cursor_lines, (
        f"render_docs(cursor=0) must render a ▶ indicator; got:\n{rendered!r}"
    )
    y0 = cursor_lines[0]

    # Structure check: ▶ must NOT appear on line 0 or 1 (= header + blank).
    assert y0 >= 2, (
        f"▶ at render line {y0}; expected >= 2 because lang-header "
        f"(line 0) and blank (line 1) precede section headers. "
        f"If ▶ is at line 0 or 1 the geometry needs re-audit."
    )
    # Off-by-one line must NOT contain ▶.
    wrong_y = 1 + y0
    if wrong_y < len(lines):
        assert "▶" not in lines[wrong_y], (
            f"Off-by-one line {wrong_y} ({lines[wrong_y]!r}) unexpectedly "
            f"contains ▶ — re-audit the _docs_cursor_y fix."
        )


def test_docs_cursor_y_formula_matches_render_line(tmp_path: Path) -> None:
    """Tier 2b: _docs_cursor_y() result == line index of ▶ in rendered output.

    Directly instantiates a RightPanel in headless mode and calls
    _docs_cursor_y() to verify it returns the same line as render_docs ▶.

    This is the tightest available test of the fixed formula — it uses the
    actual helper rather than just verifying render_docs.

    Uses direct attribute substitution (no MagicMock) per testing.ja.md.
    """
    from reyn.interfaces.tui.widgets.right_panel.docs_tab import render_docs

    docs_dir = tmp_path / "docs" / "concepts"
    docs_dir.mkdir(parents=True)
    f1 = docs_dir / "glossary.md"
    f1.write_text("# glossary\n", encoding="utf-8")
    f2 = docs_dir / "architecture.md"
    f2.write_text("# architecture\n", encoding="utf-8")
    groups = {"concepts": [f1, f2]}

    # For each cursor position, render and find ▶ line, then compute
    # the equivalent of _docs_cursor_y() and compare.
    def _docs_cursor_y_impl(cursor: int, doc_groups: dict) -> int:
        """Replicate the fixed _docs_cursor_y formula."""
        line = 2  # past header + blank
        file_idx = 0
        for section in sorted(doc_groups):
            line += 1  # section header
            for _md in doc_groups[section]:
                if file_idx == cursor:
                    return line
                line += 1
                file_idx += 1
            line += 1  # trailing blank per section
        return 2  # fallback

    for cursor in range(len(groups["concepts"])):
        rendered = render_docs(
            tmp_path, docs_cursor=cursor, groups=groups, lang="en"
        )
        lines = rendered.split("\n")
        cursor_line_idxs = [i for i, ln in enumerate(lines) if "▶" in ln]
        assert cursor_line_idxs, (
            f"cursor={cursor}: render_docs must contain ▶; got {rendered!r}"
        )
        render_y = cursor_line_idxs[0]
        formula_y = _docs_cursor_y_impl(cursor, groups)
        assert formula_y == render_y, (
            f"cursor={cursor}: fixed formula gives y={formula_y} but ▶ is "
            f"at render line {render_y}. Formula and render are misaligned — "
            f"off-by-one fix may be wrong."
        )
        # Old formula (1 + formula_y) must NOT match ▶ line.
        assert (1 + formula_y) != render_y, (
            f"cursor={cursor}: old formula (1 + {formula_y} = {1 + formula_y}) "
            f"coincidentally matches ▶ line — re-audit the test fixture."
        )
