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
    _session_hook_items,
    _session_visibility_items,
    _task_expansion,
    _task_rows,
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
        "cost_agent": 0.0,
        "agent_tokens": 0,
        "task_count": 0,
        "task_tree": [],
        "cron_jobs": [],
        "mcp_servers": [],
        "hooks": [],
        "visibility_items": [],
        "hook_items": [],
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


def test_chips_carry_per_item_value_colours() -> None:
    """Tier 2: each chip has a value_color (the value renders bold in that colour),
    and they vary per item so the eye separates the chips (owner: bold + per-item
    colour). Not asserting exact hex — just that they're set and differ."""
    by = {s.key: s.value_color for s in _CHIP_SPECS}
    assert all(by.values())                  # every chip has a colour
    assert by["model"] != by["agent"]        # per-item variation, not uniform
    assert by["cost"] != by["task"]


def test_model_chip_value_returns_model_name() -> None:
    """Tier 2: the model chip's value() returns the model string."""
    spec = next(s for s in _CHIP_SPECS if s.key == "model")
    assert spec.value(_snap(model="flash-lite")) == "flash-lite"


def test_cost_chip_value_returns_formatted_dollars() -> None:
    """Tier 2: cost chip reads the durable per-agent cost (cost_agent), not the
    per-session cost_usd that resets on restart."""
    spec = next(s for s in _CHIP_SPECS if s.key == "cost")
    result = spec.value(_snap(cost_agent=0.0123))
    assert result.startswith("$")
    assert "0123" in result or "0.0123" in result


def test_cost_chip_reads_agent_cost_not_session_cost() -> None:
    """Tier 2: cost chip shows cost_agent (durable, restart-surviving) even when
    cost_usd (per-session) differs — fixes the cost-reset-on-restart bug."""
    spec = next(s for s in _CHIP_SPECS if s.key == "cost")
    snap = _snap(cost_usd=0.0, cost_agent=0.0500)
    assert "$0.0500" in spec.value(snap)


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
# _more_expansion (Phase 5a → #2285: visibility/hook toggles + read-only cron)
# ---------------------------------------------------------------------------

def _all_lines(elements: list) -> list[str]:
    """Flatten lines from a list of RegionElements (DetailElement or CommandUIElement)."""
    return [ln for el in elements for ln in el.lines()]


def test_more_expansion_returns_element_list() -> None:
    """Tier 2: _more_expansion always returns a list of RegionElements (not a single element)."""
    result = _more_expansion(_snap(), lambda _: None)
    assert isinstance(result, list)
    assert len(result) >= 1


def test_more_expansion_fallback_all_detail_elements_when_no_session_data() -> None:
    """Tier 2: without visibility_items/hook_items the panel is all-read-only DetailElements."""
    result = _more_expansion(_snap(), lambda _: None)
    assert all(isinstance(el, DetailElement) for el in result)


def test_more_expansion_empty_shows_all_section_headers() -> None:
    """Tier 2: empty snap shows mcp/hooks/cron headers in the panel."""
    joined = " ".join(_all_lines(_more_expansion(_snap(), lambda _: None)))
    assert "mcp" in joined
    assert "hooks" in joined
    assert "cron" in joined
    assert "(none)" in joined


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
    joined = " ".join(_all_lines(_more_expansion(snap, lambda _: None)))
    assert "nightly" in joined
    assert "on" in joined
    assert "paused" in joined
    assert "off" in joined


def test_more_expansion_populated_mcp_shows_server_name() -> None:
    """Tier 2: populated mcp_servers renders the server name in fallback mode."""
    snap = _snap(mcp_servers=[{"name": "github"}])
    joined = " ".join(_all_lines(_more_expansion(snap, lambda _: None)))
    assert "github" in joined


def test_more_expansion_populated_hooks_shows_label() -> None:
    """Tier 2: populated hooks renders the hook label in fallback mode."""
    snap = _snap(hooks=[{"label": "on_push"}])
    joined = " ".join(_all_lines(_more_expansion(snap, lambda _: None)))
    assert "on_push" in joined


# ---------------------------------------------------------------------------
# _more_expansion toggle mode (#2285: visibility_items / hook_items present)
# ---------------------------------------------------------------------------


def test_more_expansion_visibility_items_yield_command_ui_rows() -> None:
    """Tier 2: when visibility_items are present, _more_expansion returns CommandUIElement
    rows (selectable) with [on]/[off] markers and /visibility dispatch commands."""
    dispatched: list[str] = []
    snap = _snap(
        visibility_items=[
            {"kind": "tool", "name": "bash", "on": True},
            {"kind": "tool", "name": "sandboxed_exec", "on": False},
        ],
    )
    result = _more_expansion(snap, dispatched.append)

    # At least one CommandUIElement for the tool items.
    cmd_els = [el for el in result if isinstance(el, CommandUIElement)]
    assert cmd_els, "expected at least one selectable CommandUIElement for tool items"

    all_ln = _all_lines(result)
    assert any("[on] bash" in ln for ln in all_ln)
    assert any("[off] sandboxed_exec" in ln for ln in all_ln)


def test_more_expansion_toggle_on_item_dispatches_off_command() -> None:
    """Tier 2: selecting an [on] tool row dispatches '/visibility off tool <name>'."""
    dispatched: list[str] = []
    snap = _snap(
        visibility_items=[{"kind": "tool", "name": "bash", "on": True}],
    )
    result = _more_expansion(snap, dispatched.append)
    cmd_el = next(el for el in result if isinstance(el, CommandUIElement))
    cmd_el.on_select(0)
    assert dispatched == ["/visibility off tool bash"]


def test_more_expansion_toggle_off_item_dispatches_on_command() -> None:
    """Tier 2: selecting an [off] mcp row dispatches '/visibility on mcp <name>'."""
    dispatched: list[str] = []
    snap = _snap(
        visibility_items=[{"kind": "mcp", "name": "brave", "on": False}],
    )
    result = _more_expansion(snap, dispatched.append)
    cmd_el = next(el for el in result if isinstance(el, CommandUIElement))
    cmd_el.on_select(0)
    assert dispatched == ["/visibility on mcp brave"]


def test_more_expansion_section_headers_are_non_selectable() -> None:
    """Tier 2: section header elements are DetailElement (non-selectable) even when
    item rows are CommandUIElement — the Region cursor skips them."""
    snap = _snap(
        visibility_items=[{"kind": "tool", "name": "bash", "on": True}],
    )
    result = _more_expansion(snap, lambda _: None)
    # All DetailElements must have selectable=False.
    for el in result:
        if isinstance(el, DetailElement):
            assert el.selectable is False


def test_more_expansion_hook_items_show_toggle_rows() -> None:
    """Tier 2: hook_items produce [on]/[off] rows; selecting dispatches /hook command."""
    dispatched: list[str] = []
    snap = _snap(
        hook_items=[
            {"name": "format", "scope": "runtime", "on": True},
            {"name": "lint",   "scope": "per-agent", "on": False},
        ],
    )
    result = _more_expansion(snap, dispatched.append)

    all_ln = _all_lines(result)
    assert any("[on] format" in ln for ln in all_ln)
    assert any("[off] lint" in ln for ln in all_ln)

    # on→off
    cmd_el = next(el for el in result if isinstance(el, CommandUIElement))
    cmd_el.on_select(0)
    assert "/hook off format" in dispatched


def test_more_expansion_cron_section_always_read_only() -> None:
    """Tier 2: the cron section is always a DetailElement (deferred in #2285) even
    when visibility_items are present."""
    snap = _snap(
        visibility_items=[{"kind": "tool", "name": "bash", "on": True}],
        cron_jobs=[{"name": "nightly", "schedule": "0 0 * * *", "enabled": True}],
    )
    result = _more_expansion(snap, lambda _: None)
    # Verify cron content appears in the lines.
    joined = " ".join(_all_lines(result))
    assert "nightly" in joined
    # All elements including the cron one must be either Detail or Command; the cron
    # section itself (containing "cron") must be a DetailElement.
    cron_els = [el for el in result if isinstance(el, DetailElement)
                and any("cron" in ln for ln in el.lines())]
    assert cron_els, "cron section must be a read-only DetailElement"


# ---------------------------------------------------------------------------
# _session_visibility_items / _session_hook_items graceful fallback
# ---------------------------------------------------------------------------


def test_session_visibility_items_returns_empty_when_method_absent() -> None:
    """Tier 2: _session_visibility_items returns [] when session has no capability_visibility_state."""
    assert _session_visibility_items(object()) == []


def test_session_hook_items_returns_empty_when_method_absent() -> None:
    """Tier 2: _session_hook_items returns [] when session has no hook_state."""
    assert _session_hook_items(object()) == []


def test_session_visibility_items_reads_state_and_computes_on_flag() -> None:
    """Tier 2: _session_visibility_items calls capability_visibility_state() and maps
    hidden_by_session to on=False, authorized-but-not-hidden to on=True."""
    class _FakeSession:
        def capability_visibility_state(self):
            return {
                "authorized": [
                    {"kind": "tool", "name": "bash"},
                    {"kind": "mcp",  "name": "brave"},
                ],
                "hidden_by_session": [{"kind": "mcp", "name": "brave"}],
            }

    items = _session_visibility_items(_FakeSession())
    by_name = {it["name"]: it for it in items}
    assert by_name["bash"]["on"] is True
    assert by_name["brave"]["on"] is False


def test_session_hook_items_reads_state() -> None:
    """Tier 2: _session_hook_items calls hook_state() and returns name/scope/on dicts."""
    class _FakeSession:
        def hook_state(self):
            return [
                {"name": "format", "scope": "runtime",   "enabled": True},
                {"name": "lint",   "scope": "per-agent", "enabled": False},
            ]

    items = _session_hook_items(_FakeSession())
    by_name = {it["name"]: it for it in items}
    assert by_name["format"]["on"] is True
    assert by_name["lint"]["on"] is False
    assert by_name["format"]["scope"] == "runtime"


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
    snap = _snap(cost_usd=0.0123, usage=(200, 9, 209), agent_tokens=209)
    el = _cost_expansion(snap, lambda _: None)
    lines = el.lines()
    joined = " ".join(lines)
    assert "0.0123" in joined
    assert "prompt 200" in joined
    assert "completion 9" in joined
    assert "total 209" in joined


def test_cost_expansion_tokens_line_uses_agent_tokens_when_present() -> None:
    """Tier 2: when agent_tokens is in the snapshot (durable per-agent total from
    registry.agent_tokens), the tokens line shows it instead of the session total —
    so the count survives restart."""
    snap = _snap(usage=(200, 9, 209), agent_tokens=1500)
    lines = _cost_expansion(snap, lambda _: None).lines()
    joined = " ".join(lines)
    assert "total 1500" in joined
    assert "total 209" not in joined


def test_cost_expansion_tokens_fallback_to_session_total_when_absent() -> None:
    """Tier 2: when agent_tokens is absent (e2e backend not yet landed), the tokens
    line falls back to the session total so nothing breaks pre-landing."""
    snap = _snap(usage=(200, 9, 209))
    snap.pop("agent_tokens", None)
    lines = _cost_expansion(snap, lambda _: None).lines()
    assert "total 209" in " ".join(lines)


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


# _task_rows live-reading contract
# ---------------------------------------------------------------------------


def test_task_rows_dict_replacement_is_visible_through_lambda() -> None:
    """Tier 2: live-reading DetailElement over task_cache re-reads the dict on every lines() call.

    _menu_open creates DetailElement(lambda: _task_rows(tc.get("tree") or [], 0) …)
    so that the open task dropdown reflects _task_poll's updates. The key contract:
    replacing tc["tree"] (as _task_poll does — it assigns a new list, not in-place)
    is immediately visible in the next lines() call, unlike a snapshot-time pre-compute.
    """
    tc: dict = {"tree": [{"task_id": "a", "name": "Task A", "status": "running", "children": []}]}
    el = DetailElement(lambda: _task_rows(tc.get("tree") or [], 0) or ["(no active tasks)"])
    assert any("Task A" in ln for ln in el.lines())

    # Simulate _task_poll replacing the tree with a new list (not in-place mutation):
    tc["tree"] = [{"task_id": "b", "name": "Task B", "status": "done", "children": []}]
    assert any("Task B" in ln for ln in el.lines())
    assert not any("Task A" in ln for ln in el.lines())


def test_task_rows_empty_tree_replacement_shows_fallback() -> None:
    """Tier 2: when _task_poll clears the tree, the live element shows the empty-state fallback."""
    tc: dict = {"tree": [{"task_id": "a", "name": "Task A", "status": "running", "children": []}]}
    el = DetailElement(lambda: _task_rows(tc.get("tree") or [], 0) or ["(no active tasks)"])
    assert any("Task A" in ln for ln in el.lines())

    tc["tree"] = []
    assert el.lines() == ["(no active tasks)"]
