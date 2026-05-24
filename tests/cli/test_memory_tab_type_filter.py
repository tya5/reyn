"""Tier 2: Memory tab ``t`` cycles per-type filter (Wave-11 A#1).

Wave-11 A#1. The memory tab can grow to 20+ entries (MEMORY.md
alone has 20+ feedback rows in active sessions). j/k walks the
flat list one row at a time; reaching a specific TYPE requires
many keypresses. This adds a per-type cycle filter on ``t``.

Cycle: ``None`` → ``user`` → ``feedback`` → ``project`` →
``reference`` → ``None``. Banner surfaces the active filter +
``[t] to cycle`` hint.

Coexistence with events tab's ``t`` (= tail cycle): events tab
binding is gated to ``panel_type == "events"`` via
``App.check_action``, so when the user is on the memory tab the
``t`` key falls through to ``RightPanel.on_key`` for the type
cycle.

Pinned:
  - ``cycle_memory_type_filter`` advances through the cycle in
    order, wraps to ``None`` after ``reference``
  - cycling resets the memory cursor to 0 (= list shape changed)
  - ``render_memory(type_filter="feedback")`` keeps only FEEDBACK
    entries + adds the banner
  - ``t`` key on memory tab invokes the cycle
  - Keys tab routes the description through PANEL_EXPLICIT
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_fake_entries():
    """Build minimal MemoryEntry-shaped objects for the renderer test."""
    class _FakeEntry:
        def __init__(self, name: str, kind: str, description: str = ""):
            self.name = name
            self.type = kind
            self.description = description
            self.body = ""

    return [
        _FakeEntry("alpha_user", "user", "user pref"),
        _FakeEntry("beta_feedback", "feedback", "feedback note"),
        _FakeEntry("gamma_project", "project", "project fact"),
        _FakeEntry("delta_reference", "reference", "ref"),
    ]


@pytest.mark.asyncio
async def test_cycle_memory_type_filter_walks_order() -> None:
    """Tier 2: cycle advances None → user → feedback → project → reference → None."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        assert panel._memory_type_filter is None
        assert panel.cycle_memory_type_filter() == "user"
        assert panel.cycle_memory_type_filter() == "feedback"
        assert panel.cycle_memory_type_filter() == "project"
        assert panel.cycle_memory_type_filter() == "reference"
        assert panel.cycle_memory_type_filter() is None


@pytest.mark.asyncio
async def test_cycle_resets_memory_cursor() -> None:
    """Tier 2: cycling clamps cursor to 0 (= list shape changed)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._memory_cursor = 7
        panel.cycle_memory_type_filter()
        assert panel._memory_cursor == 0


def test_render_memory_with_type_filter_keeps_only_target(tmp_path: Path) -> None:
    """Tier 2: ``render_memory(type_filter='feedback')`` drops other types."""
    from reyn.chat.tui.widgets.right_panel.memory_tab import render_memory

    # Build a real memory dir with 4 entries — one per type.
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    for name, kind in [
        ("alpha_user", "user"),
        ("beta_feedback", "feedback"),
        ("gamma_project", "project"),
        ("delta_reference", "reference"),
    ]:
        # memory.py reads ``type`` at the frontmatter root (= flat),
        # not nested under ``metadata`` despite the prod-format MEMORY.md
        # nesting convention. The flat form is what the parser actually
        # honours.
        (mem_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\ntype: {kind}\n---\n\n"
            f"body for {name}\n"
        )

    # No filter — all 4 entries visible.
    _rendered, all_entries, _ys = render_memory(tmp_path, cursor=0)
    names = {e.name for e in all_entries}
    assert {"alpha_user", "beta_feedback", "gamma_project", "delta_reference"} <= names

    # Filter to "feedback" → only that type appears.
    rendered_f, entries_f, _ys2 = render_memory(
        tmp_path, cursor=0, type_filter="feedback",
    )
    names_f = {e.name for e in entries_f}
    assert "beta_feedback" in names_f
    assert "alpha_user" not in names_f
    assert "gamma_project" not in names_f
    assert "delta_reference" not in names_f
    # Banner surfaces the filter.
    assert "FEEDBACK" in rendered_f
    assert "[t]" in rendered_f


def test_render_memory_without_filter_omits_banner(tmp_path: Path) -> None:
    """Tier 2: cold-default (no filter) does NOT prepend the banner."""
    from reyn.chat.tui.widgets.right_panel.memory_tab import render_memory

    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "x.md").write_text(
        "---\nname: x\ndescription: d\ntype: user\n---\n\nbody\n"
    )
    rendered, _entries, _ys = render_memory(tmp_path, cursor=0)
    # Banner is gated on type_filter being set.
    assert "⌕ filter:" not in rendered


def test_render_memory_unknown_filter_is_ignored(tmp_path: Path) -> None:
    """Tier 2: an unrecognised ``type_filter`` value falls through to all-types."""
    from reyn.chat.tui.widgets.right_panel.memory_tab import render_memory

    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "x.md").write_text(
        "---\nname: x\ndescription: d\ntype: user\n---\n\nbody\n"
    )
    rendered, entries, _ys = render_memory(
        tmp_path, cursor=0, type_filter="garbage-typo",
    )
    # Unknown filter → no banner + full entry list.
    assert "⌕ filter:" not in rendered
    (only_entry,) = entries


@pytest.mark.asyncio
async def test_t_key_on_memory_tab_invokes_cycle() -> None:
    """Tier 2: pressing ``t`` while memory tab focused advances the filter."""
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel.set_panel_type("memory")
        await pilot.pause()
        assert panel._memory_type_filter is None
        key_event = textual_events.Key(key="t", character="t")
        panel.on_key(key_event)
        await pilot.pause()
        assert panel._memory_type_filter == "user"


def test_keys_tab_t_first_occurrence_is_events() -> None:
    """Tier 2: ``t`` already appears in Keys tab under EVENTS (gated).

    The memory-tab ``t`` (= type-filter cycle) is documented via the
    flash status (``memory filter: USER`` etc.) since the Keys tab
    de-duplicates by key. Documenting it in the Keys tab would need
    a same-key duplicate row — not worth the dedup-logic complexity
    for a single-tab convenience binding. Pin that the events
    binding (the prior owner of ``t``) still renders.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    markup, _ = render_keys(app)
    # ``t`` from app.BINDINGS (events tab) still renders.
    assert "Tail events" in markup
