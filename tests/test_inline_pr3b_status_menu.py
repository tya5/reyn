"""Tier 2: inline status-menu pure builders — chips + read-only dropdown lines.

The navigable menu / focus handling is interactive (verified live, e2e); here we
pin the pure data→display mapping that feeds the status row and each chip's
read-only dropdown. Plain-value inputs keep these decoupled from the Session.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import dropdown_lines, status_chips


def test_status_chips_has_five_labelled_values() -> None:
    """Tier 2: the status row exposes model/agents/skills/cost/ctx with values."""
    chips = status_chips("flash-lite", 2, 1, 0.0123, 12800)
    labels = [lbl for lbl, _ in chips]
    assert labels == ["model", "agents", "skills", "cost", "ctx"]
    by = dict(chips)
    assert by["model"] == "flash-lite"
    assert by["agents"] == "2"
    assert by["skills"] == "1"
    assert by["cost"] == "$0.0123"


def test_ctx_abbreviates_thousands_but_not_small_counts() -> None:
    """Tier 2: ctx shows 'Nk' at >=1000 tokens, the raw count below."""
    assert dict(status_chips("m", 0, 0, 0.0, 12800))["ctx"] == "12k"
    assert dict(status_chips("m", 0, 0, 0.0, 500))["ctx"] == "500"


def test_agents_dropdown_marks_attached() -> None:
    """Tier 2: the agents panel marks the attached agent with ▸."""
    lines = dropdown_lines(
        "agents", model="m", agent_names=["default", "researcher"],
        attached_name="default", skill_run_ids=[], usage=(0, 0, 0), cost_usd=0.0,
    )
    assert any(ln.startswith("▸") and "default" in ln for ln in lines)
    assert any("researcher" in ln and not ln.startswith("▸") for ln in lines)


def test_skills_dropdown_lists_runs_or_empty_message() -> None:
    """Tier 2: skills panel lists run ids, or a clear empty message."""
    lines = dropdown_lines(
        "skills", model="m", agent_names=[], attached_name=None,
        skill_run_ids=["run_abcd", "run_ef01"], usage=(0, 0, 0), cost_usd=0.0,
    )
    assert any("run_abcd" in ln for ln in lines)
    empty = dropdown_lines(
        "skills", model="m", agent_names=[], attached_name=None,
        skill_run_ids=[], usage=(0, 0, 0), cost_usd=0.0,
    )
    assert any("no running skills" in ln for ln in empty)


def test_cost_dropdown_shows_token_breakdown() -> None:
    """Tier 2: cost/ctx panel shows the prompt/completion/total breakdown."""
    lines = dropdown_lines(
        "cost", model="m", agent_names=[], attached_name=None,
        skill_run_ids=[], usage=(100, 5, 105), cost_usd=0.01,
    )
    joined = " ".join(lines)
    assert "prompt 100" in joined
    assert "completion 5" in joined
    assert "total 105" in joined


def test_model_dropdown_shows_current_and_change_hint() -> None:
    """Tier 2: model panel shows the current class + how to change it."""
    lines = dropdown_lines(
        "model", model="flash-lite", agent_names=[], attached_name=None,
        skill_run_ids=[], usage=(0, 0, 0), cost_usd=0.0,
    )
    joined = " ".join(lines)
    assert "flash-lite" in joined
    assert "/model" in joined
