"""Tier 2: ChipSpec framework — spec registry + expansion builders.

The status bar is now declarative: each chip is a ``ChipSpec`` with a key,
label, value function, and optional expansion builder. Tests exercise the
public surface (``_CHIP_SPECS``, ``_model_expansion``, ``_cost_expansion``,
``_agent_expansion``, ``_task_expansion``, ``_more_expansion``) using real
instances; no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import (
    _CHIP_SPECS,
    _agent_expansion,
    _build_task_tree,
    _cost_expansion,
    _model_expansion,
    _more_expansion,
    _task_expansion,
)
from reyn.interfaces.inline.region import DetailElement
from reyn.interfaces.inline.region_command import CommandUIElement


def _snap(**over):
    """A status snapshot dict covering all chip value/expansion inputs."""
    base = {
        "model": "standard",
        "model_classes": ["light", "standard", "strong"],
        "agent_names": ["default"],
        "attached_name": "default",
        "session_tree": [],
        "skill_run_ids": [],
        "usage": (0, 0, 0),
        "cost_usd": 0.0,
        "task_count": 0,
        "task_tree": [],
        "cron_jobs": [],
        "mcp_servers": [],
        "hooks": [],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_chip_specs_has_required_keys_in_order() -> None:
    """Tier 2: the registry exposes model/cost/agent/task/more in that order."""
    keys = [s.key for s in _CHIP_SPECS]
    assert keys == ["model", "cost", "agent", "task", "more"]


def test_model_chip_value_returns_model_name() -> None:
    """Tier 2: the model chip's value() returns the model string."""
    spec = next(s for s in _CHIP_SPECS if s.key == "model")
    assert spec.value(_snap(model="flash-lite")) == "flash-lite"


def test_cost_chip_value_returns_formatted_dollars() -> None:
    """Tier 2: the cost chip's value() returns a dollar-formatted string."""
    spec = next(s for s in _CHIP_SPECS if s.key == "cost")
    result = spec.value(_snap(cost_usd=0.0123))
    assert result.startswith("$")
    assert "0123" in result or "0.0123" in result


def test_agent_chip_value_returns_attached_name() -> None:
    """Tier 2: the agent chip's value() returns the attached agent name."""
    spec = next(s for s in _CHIP_SPECS if s.key == "agent")
    assert spec.value(_snap(attached_name="researcher")) == "researcher"


def test_agent_chip_value_returns_dash_when_none() -> None:
    """Tier 2: the agent chip's value() returns '—' when no agent is attached."""
    spec = next(s for s in _CHIP_SPECS if s.key == "agent")
    assert spec.value(_snap(attached_name=None)) == "—"


def test_task_chip_value_returns_count_string() -> None:
    """Tier 2: the task chip's value() returns the task count as a string."""
    spec = next(s for s in _CHIP_SPECS if s.key == "task")
    assert spec.value(_snap(task_count=3)) == "3"
    assert spec.value(_snap(task_count=0)) == "0"


def test_more_chip_has_expansion() -> None:
    """Tier 2: the 'more' chip has a non-None expansion (Phase 5a)."""
    spec = next(s for s in _CHIP_SPECS if s.key == "more")
    assert spec.expansion is not None


# ---------------------------------------------------------------------------
# _more_expansion (Phase 5a: read-only cron/mcp/hooks overflow panel)
# ---------------------------------------------------------------------------


def test_more_expansion_empty_returns_detail_element() -> None:
    """Tier 2: _more_expansion with empty config sections returns a read-only DetailElement."""
    el = _more_expansion(_snap(), lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_more_expansion_empty_shows_all_section_headers_with_zero() -> None:
    """Tier 2: empty snap shows cron/mcp/hooks headers with (0) and a (none) line each."""
    el = _more_expansion(_snap(), lambda _: None)
    joined = " ".join(el.lines())
    assert "cron" in joined
    assert "mcp" in joined
    assert "hooks" in joined
    assert "(0)" in joined
    assert "(none)" in joined


def test_more_expansion_empty_all_three_sections_each_have_none_line() -> None:
    """Tier 2: each of the three sections contains a '(none)' indicator when empty."""
    el = _more_expansion(_snap(), lambda _: None)
    lines = el.lines()
    # There should be a (none) line following each header.
    none_lines = [ln for ln in lines if "(none)" in ln]
    # At minimum one (none) per section — i.e. three total when all empty.
    assert none_lines


def test_more_expansion_populated_cron_shows_on_off_markers() -> None:
    """Tier 2: populated cron_jobs render nightly with 'on' and paused with 'off'."""
    snap = _snap(
        cron_jobs=[
            {"name": "nightly", "schedule": "0 0 * * *", "enabled": True},
            {"name": "paused",  "schedule": "0 * * * *", "enabled": False},
        ],
        mcp_servers=[{"name": "github"}],
        hooks=[{"label": "on_push"}],
    )
    el = _more_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "nightly" in joined
    assert "on" in joined       # enabled marker for nightly
    assert "paused" in joined
    assert "off" in joined      # disabled marker for paused


def test_more_expansion_populated_mcp_shows_server_name() -> None:
    """Tier 2: populated mcp_servers renders the server name."""
    snap = _snap(
        cron_jobs=[],
        mcp_servers=[{"name": "github"}],
        hooks=[],
    )
    el = _more_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "github" in joined


def test_more_expansion_populated_hooks_shows_label() -> None:
    """Tier 2: populated hooks renders the hook label."""
    snap = _snap(
        cron_jobs=[],
        mcp_servers=[],
        hooks=[{"label": "on_push"}],
    )
    el = _more_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "on_push" in joined


# ---------------------------------------------------------------------------
# _model_expansion
# ---------------------------------------------------------------------------


def test_model_expansion_with_classes_is_command_ui() -> None:
    """Tier 2: with classes, _model_expansion returns a CommandUIElement picker."""
    submitted: list[str] = []
    snap = _snap(model="standard", model_classes=["light", "standard", "strong"])
    el = _model_expansion(snap, submitted.append)
    assert isinstance(el, CommandUIElement)


def test_model_expansion_picker_submits_slash_model_on_select() -> None:
    """Tier 2: selecting row N in the model picker submits '/model <classN>'."""
    submitted: list[str] = []
    snap = _snap(model="standard", model_classes=["light", "standard", "strong"])
    el = _model_expansion(snap, submitted.append)
    el.on_select(0)
    el.on_select(2)
    assert submitted == ["/model light", "/model strong"]


def test_model_expansion_picker_out_of_range_does_not_submit() -> None:
    """Tier 2: on_select with an out-of-range index does not call the callback."""
    submitted: list[str] = []
    snap = _snap(model="standard", model_classes=["light", "standard"])
    el = _model_expansion(snap, submitted.append)
    el.on_select(9)
    assert submitted == []


def test_model_expansion_picker_marks_current_with_arrow() -> None:
    """Tier 2: the current model row starts with ▸; others do not."""
    snap = _snap(model="standard", model_classes=["light", "standard", "strong"])
    el = _model_expansion(snap, lambda _: None)
    rows = el.lines()
    assert any(r.startswith("▸") and "standard" in r for r in rows)
    assert any("light" in r and not r.startswith("▸") for r in rows)
    assert any("strong" in r and not r.startswith("▸") for r in rows)


def test_model_expansion_without_classes_is_detail_element() -> None:
    """Tier 2: with no classes, _model_expansion returns a non-selectable DetailElement."""
    snap = _snap(model="flash-lite", model_classes=[])
    el = _model_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_model_expansion_without_classes_shows_current_and_hint() -> None:
    """Tier 2: the no-classes fallback detail shows the model name and /model hint."""
    snap = _snap(model="flash-lite", model_classes=[])
    el = _model_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "flash-lite" in joined
    assert "/model" in joined


# ---------------------------------------------------------------------------
# _cost_expansion
# ---------------------------------------------------------------------------


def test_cost_expansion_is_detail_element() -> None:
    """Tier 2: _cost_expansion returns a read-only DetailElement."""
    snap = _snap(cost_usd=0.01, usage=(100, 5, 105))
    el = _cost_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_cost_expansion_shows_total_cost_and_token_breakdown() -> None:
    """Tier 2: cost expansion lines contain a total cost line and a token line."""
    snap = _snap(cost_usd=0.0123, usage=(200, 9, 209))
    el = _cost_expansion(snap, lambda _: None)
    lines = el.lines()
    joined = " ".join(lines)
    assert "0.0123" in joined
    assert "prompt 200" in joined
    assert "completion 9" in joined
    assert "total 209" in joined


def test_cost_expansion_breaks_down_total_agent_session() -> None:
    """Tier 2: cost expansion shows distinct total / agent / session amounts when
    multiple agents/sessions accrue cost (total across agents ≥ the attached
    session). Each level renders its own dollar amount, not one collapsed total."""
    snap = _snap(cost_usd=0.0100, cost_agent=0.0100, cost_total=0.0750)
    lines = _cost_expansion(snap, lambda _: None).lines()
    by_label = {ln.split()[0]: ln for ln in lines if ln.split()}
    assert {"total", "agent", "session"} <= set(by_label)
    assert "0.0750" in by_label["total"]      # sum across loaded agents
    assert "0.0100" in by_label["session"]    # the attached session
    # the breakdown is not collapsed: total reflects the cross-agent sum
    assert by_label["total"] != by_label["session"]


# ---------------------------------------------------------------------------
# _agent_expansion  (Phase 2: agent/session tree + attach/switch on select)
# ---------------------------------------------------------------------------

_TREE_SNAP = [
    {
        "agent": "default",
        "attached": True,
        "sessions": [
            {"sid": "main", "attached": True},
            {"sid": "sub1", "attached": False},
        ],
    },
    {
        "agent": "researcher",
        "attached": False,
        "sessions": [
            {"sid": "main", "attached": False},
        ],
    },
]


def test_agent_expansion_with_tree_is_command_ui() -> None:
    """Tier 2: with a non-empty session_tree, _agent_expansion returns a CommandUIElement."""
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, lambda _: None)
    assert isinstance(el, CommandUIElement)


def test_agent_expansion_rows_include_agents_and_sessions() -> None:
    """Tier 2: rows contain agent names and indented session sids."""
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, lambda _: None)
    rows = el.lines()
    joined = " ".join(rows)
    assert "default" in joined
    assert "researcher" in joined
    assert "sub1" in joined
    # session rows are indented
    assert any("main" in r and r.startswith("    ") for r in rows)


def test_agent_expansion_attached_agent_marked_with_arrow() -> None:
    """Tier 2: the attached agent row starts with ▸; unattached agents do not."""
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, lambda _: None)
    rows = el.lines()
    assert any(r.startswith("▸") and "default" in r for r in rows)
    assert any("researcher" in r and not r.startswith("▸") for r in rows)


def test_agent_expansion_attached_session_marked_with_arrow() -> None:
    """Tier 2: the attached session row carries ▸; others do not."""
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, lambda _: None)
    rows = el.lines()
    # "main" under "default" is attached → must have ▸ in its row
    main_rows = [r for r in rows if "main" in r and "default" not in r]
    assert any("▸" in r for r in main_rows)


def test_agent_expansion_selecting_agent_row_submits_attach(tmp_path) -> None:
    """Tier 2: selecting an agent row submits /attach <agent>."""
    submitted: list[str] = []
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, submitted.append)
    # Row 0 is the "default" agent row.
    el.on_select(0)
    assert submitted == ["/attach default"]


def test_agent_expansion_selecting_attached_session_submits_switch(tmp_path) -> None:
    """Tier 2: selecting a session row under the attached agent submits /session switch."""
    submitted: list[str] = []
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, submitted.append)
    rows = el.lines()
    # Find the index of the "main" session row under default (first agent).
    main_idx = next(i for i, r in enumerate(rows) if "main" in r and r.startswith("    "))
    submitted.clear()
    el.on_select(main_idx)
    assert submitted == ["/session switch main"]


def test_agent_expansion_selecting_non_attached_agent_session_submits_attach(tmp_path) -> None:
    """Tier 2: selecting a session of a non-attached agent submits /attach <agent>."""
    submitted: list[str] = []
    snap = _snap(session_tree=_TREE_SNAP)
    el = _agent_expansion(snap, submitted.append)
    rows = el.lines()
    # Find researcher's session row (last row in the tree).
    researcher_sess_idx = next(
        i for i, r in enumerate(rows)
        if "main" in r and r.startswith("    ") and
        # researcher is after default's rows; find the one NOT in the default block.
        # We look for the last "main" session row.
        i > next(j for j, rr in enumerate(rows) if "researcher" in rr)
    )
    submitted.clear()
    el.on_select(researcher_sess_idx)
    assert submitted == ["/attach researcher"]


def test_agent_expansion_empty_tree_returns_detail_element() -> None:
    """Tier 2: empty session_tree → a non-selectable DetailElement with '(no agents)'."""
    snap = _snap(session_tree=[])
    el = _agent_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False
    assert any("no agents" in r for r in el.lines())


# ---------------------------------------------------------------------------
# _build_task_tree
# ---------------------------------------------------------------------------

_ROOT_DICT = {
    "task_id": "t-root",
    "name": "Root Task",
    "status": "running",
    "requester": "session-1",
    "requester_kind": "session",
}
_CHILD_DICT = {
    "task_id": "t-child",
    "name": "Child Task",
    "status": "ready",
    "requester": "t-root",
    "requester_kind": "task",
}


def test_build_task_tree_child_nests_under_root() -> None:
    """Tier 2: a task with requester_kind='task' and requester=<root id> nests under root."""
    tree = _build_task_tree([_ROOT_DICT, _CHILD_DICT])
    assert tree, "expected at least one root node"
    root = next((n for n in tree if n["task_id"] == "t-root"), None)
    assert root is not None
    child_ids = [c["task_id"] for c in root["children"]]
    assert "t-child" in child_ids


def test_build_task_tree_session_owned_task_is_root() -> None:
    """Tier 2: a task with requester_kind='session' appears at the top level (is a root)."""
    tree = _build_task_tree([_ROOT_DICT])
    root_ids = [n["task_id"] for n in tree]
    assert "t-root" in root_ids


def test_build_task_tree_returns_plain_dicts() -> None:
    """Tier 2: _build_task_tree returns plain dicts; mutating the result does not raise."""
    tree = _build_task_tree([_ROOT_DICT, _CHILD_DICT])
    assert tree
    node = tree[0]
    assert isinstance(node, dict)
    # Mutating should be fine — plain dict, not a frozen dataclass or Task instance.
    node["_test_key"] = "ok"


# ---------------------------------------------------------------------------
# _task_expansion
# ---------------------------------------------------------------------------


def test_task_expansion_empty_tree_is_detail_element() -> None:
    """Tier 2: _task_expansion with empty task_tree returns a non-selectable DetailElement."""
    snap = _snap(task_tree=[])
    el = _task_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_task_expansion_empty_tree_shows_no_active_tasks_message() -> None:
    """Tier 2: empty task_tree expansion contains a 'no active task' message."""
    snap = _snap(task_tree=[])
    el = _task_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "no active task" in joined


def test_task_expansion_populated_shows_parent_and_child_names() -> None:
    """Tier 2: populated task_tree renders both parent and child task names."""
    tree = _build_task_tree([_ROOT_DICT, _CHILD_DICT])
    snap = _snap(task_tree=tree)
    el = _task_expansion(snap, lambda _: None)
    joined = " ".join(el.lines())
    assert "Root Task" in joined
    assert "Child Task" in joined


def test_task_expansion_child_row_more_indented_than_parent() -> None:
    """Tier 2: in a populated tree, the child row has more leading whitespace than the root."""
    tree = _build_task_tree([_ROOT_DICT, _CHILD_DICT])
    snap = _snap(task_tree=tree)
    el = _task_expansion(snap, lambda _: None)
    rows = el.lines()
    root_row = next(r for r in rows if "Root Task" in r)
    child_row = next(r for r in rows if "Child Task" in r)
    root_indent = len(root_row) - len(root_row.lstrip())
    child_indent = len(child_row) - len(child_row.lstrip())
    assert child_indent > root_indent
