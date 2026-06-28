"""Tier 2: ChipSpec framework — spec registry + expansion builders.

The status bar is now declarative: each chip is a ``ChipSpec`` with a key,
label, value function, and optional expansion builder. Tests exercise the
public surface (``_CHIP_SPECS``, ``_model_expansion``, ``_cost_expansion``,
``_agent_expansion``, ``_task_expansion``) using real instances; no mocks.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import (
    _CHIP_SPECS,
    _agent_expansion,
    _cost_expansion,
    _model_expansion,
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
        "skill_run_ids": [],
        "usage": (0, 0, 0),
        "cost_usd": 0.0,
        "task_count": 0,
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


def test_more_chip_has_no_expansion() -> None:
    """Tier 2: the 'more' chip has no expansion (Phase 5 placeholder)."""
    spec = next(s for s in _CHIP_SPECS if s.key == "more")
    assert spec.expansion is None


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


# ---------------------------------------------------------------------------
# _agent_expansion
# ---------------------------------------------------------------------------


def test_agent_expansion_is_detail_element() -> None:
    """Tier 2: _agent_expansion returns a read-only DetailElement."""
    snap = _snap(agent_names=["default"], attached_name="default")
    el = _agent_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_agent_expansion_marks_attached_agent() -> None:
    """Tier 2: the attached agent row starts with ▸; others do not."""
    snap = _snap(agent_names=["default", "researcher"], attached_name="default")
    el = _agent_expansion(snap, lambda _: None)
    rows = el.lines()
    assert any(r.startswith("▸") and "default" in r for r in rows)
    assert any("researcher" in r and not r.startswith("▸") for r in rows)


def test_agent_expansion_empty_shows_none_message() -> None:
    """Tier 2: with no agents, the expansion shows a '(none)' line."""
    snap = _snap(agent_names=[], attached_name=None)
    el = _agent_expansion(snap, lambda _: None)
    rows = el.lines()
    assert any("none" in r for r in rows)


# ---------------------------------------------------------------------------
# _task_expansion
# ---------------------------------------------------------------------------


def test_task_expansion_is_detail_element() -> None:
    """Tier 2: _task_expansion returns a read-only DetailElement."""
    snap = _snap(task_count=2)
    el = _task_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False


def test_task_expansion_shows_count_and_pluralises() -> None:
    """Tier 2: task expansion line includes the count; plural for N≠1."""
    snap_1 = _snap(task_count=1)
    el_1 = _task_expansion(snap_1, lambda _: None)
    assert "1 active task" in " ".join(el_1.lines())
    # plural
    snap_3 = _snap(task_count=3)
    el_3 = _task_expansion(snap_3, lambda _: None)
    assert "tasks" in " ".join(el_3.lines())
