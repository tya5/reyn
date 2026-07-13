"""Tier 2: ChipSpec framework — spec registry + expansion builders.

The status bar is now declarative: each chip is a ``ChipSpec`` with a key,
label, value function, and optional expansion builder. Tests exercise the
public surface (``_CHIP_SPECS``, ``_model_expansion``, ``_cost_expansion``,
``_agent_expansion``, ``_task_expansion``) using real instances; no mocks.

The "…" ("more") chip is a 2-level redesign: it has no ``expansion`` of its
own — Enter on it shows a level-1 sub-bar (``_MORE_SUB_CHIP_SPECS``: tool /
mcp / skill / pipe / hook / cron), and Enter on a sub-chip shows THAT
category's own dropdown (level 2), reusing the same menu_region/dropdown +
height-cap windowing every other chip uses. Each category has its own
expansion function (``_tool_category_expansion`` etc.) tested below in
isolation — the core regression this redesign guards against is categories
bleeding into each other (the old ``_more_expansion`` concatenated everything
into one flat list).
"""
from __future__ import annotations

from reyn.interfaces.inline.app import (
    _CHIP_SPECS,
    _MENU_REGION_MAX_HEIGHT,
    _MORE_SUB_CHIP_SPECS,
    _agent_expansion,
    _build_task_tree,
    _cost_expansion,
    _cost_scope_state,
    _cron_category_expansion,
    _extract_skills,
    _hook_category_expansion,
    _mcp_category_expansion,
    _model_expansion,
    _pipe_category_expansion,
    _session_hook_items,
    _session_pipelines,
    _session_visibility_items,
    _skill_category_expansion,
    _task_expansion,
    _task_rows,
    _tool_category_expansion,
    _visibility_items_by_kind,
)
from reyn.interfaces.inline.region import DetailElement
from reyn.interfaces.inline.region_command import CommandUIElement
from reyn.llm.pricing import CostBreakdown


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
        "ctx_used": 0,
        "ctx_window": 0,
        "ctx_source": "litellm catalog: standard",
        "session_cached_tokens": 0,
        "ctx_recent_usage": (0, 0),
        "ctx_compaction_status_fn": lambda: {"effective_trigger": 0, "free_window": 0},
        "task_count": 0,
        "task_tree": [],
        "cron_jobs": [],
        "mcp_servers": [],
        "hooks": [],
        "skills": [],
        "pipelines": [],
        "visibility_items": [],
        "hook_items": [],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_chip_specs_has_required_keys_in_order() -> None:
    """Tier 2: the registry exposes model/agent/task/cost/ctx/more in that order
    (owner: cost moved between task and ctx)."""
    keys = [s.key for s in _CHIP_SPECS]
    assert keys == ["model", "agent", "task", "cost", "ctx", "more"]


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


def test_ctx_chip_value_returns_usage_percent() -> None:
    """Tier 2: the ctx chip's value() returns used/window as a rounded percent."""
    spec = next(s for s in _CHIP_SPECS if s.key == "ctx")
    assert spec.value(_snap(ctx_used=50_000, ctx_window=200_000)) == "25%"


def test_ctx_chip_value_shows_dash_when_window_unknown() -> None:
    """Tier 2: a zero/missing window (no attached session budget yet) shows '—'
    rather than raising a ZeroDivisionError."""
    spec = next(s for s in _CHIP_SPECS if s.key == "ctx")
    assert spec.value(_snap(ctx_used=0, ctx_window=0)) == "—"


def test_ctx_chip_value_shows_dash_when_no_call_completed_yet() -> None:
    """Tier 2: used<=0 (no LLM call has completed this session — last_call_usage
    starts at TokenUsage() zero) shows '—', not a misleading "0%" — a real
    completed call's prompt_tokens is never actually 0 (system prompt alone is
    nonzero), so 0 unambiguously means "not measured yet"."""
    spec = next(s for s in _CHIP_SPECS if s.key == "ctx")
    assert spec.value(_snap(ctx_used=0, ctx_window=200_000)) == "—"


def test_ctx_expansion_is_detail_element() -> None:
    """Tier 2: the ctx chip's dropdown is read-only (no picker — nothing to select)."""
    from reyn.interfaces.inline.app import _ctx_expansion
    el = _ctx_expansion(_snap(ctx_used=50_000, ctx_window=200_000), lambda _: None)
    assert isinstance(el, DetailElement)


def test_ctx_expansion_shows_prompt_window_free_and_source() -> None:
    """Tier 2: the dropdown breaks the percent down into raw prompt/window/free
    token counts plus WHERE the window size came from (owner: 情報源もわかるように —
    litellm catalog vs reyn's own fallback default, since there is no
    user-configurable override of a model's context window in reyn today).
    ctx_used is the LAST ACTUAL REQUEST's prompt_tokens (owner: 「ユーザは
    prompt tokensの方を気にするのでは」→ confirmed) against the model's REAL
    context window — "how close to the hard limit", not a compaction-internal
    estimate (that lives in the separate "compaction" line — see
    test_ctx_expansion_shows_compaction_estimate_separately)."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap(
        ctx_used=50_000, ctx_window=200_000,
        ctx_source="litellm catalog: claude-opus-4-8",
    )
    lines = _ctx_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)
    assert "prompt" in joined and "50,000 tokens" in joined
    assert "200,000 tokens" in joined
    assert "litellm catalog: claude-opus-4-8" in joined
    assert "150,000 tokens" in joined  # free = window - prompt
    assert "25%" in joined


def test_ctx_expansion_shows_compaction_estimate_separately() -> None:
    """Tier 2: owner asked to keep the compaction subsystem's own lightweight
    history estimate visible too, but NOT collapsed into the same number as
    the real prompt/window figures above (that ambiguity was the original bug
    — 「今の見せ方だと意味がわからない」). It gets its own labeled line with its
    own (smaller, already-adjusted) denominator, the compaction trigger
    threshold — never the model's real window.

    The compaction figures come from a LAZY status_fn (not eager snapshot
    fields) — Session.context_window_status() is expensive (json.dumps +
    token-estimate of the full history) and must only run while this dropdown
    is open, not on every _snapshot() render-frame call (perf regression
    caught in review: mirrors the exact WAL-off-loop lesson from #1765 — don't
    reintroduce a per-frame/per-loop-tick cost)."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap(
        ctx_used=50_000, ctx_window=200_000,
        ctx_compaction_status_fn=lambda: {"effective_trigger": 68_000, "free_window": 67_296},
    )
    lines = _ctx_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)
    assert "compaction" in joined
    assert "704 / 68,000 tokens est." in joined
    assert "1% to trigger" in joined  # round(100 * 704 / 68000) == 1


def test_ctx_expansion_compaction_status_fn_called_lazily() -> None:
    """Tier 2: the compaction status_fn must NOT be invoked until lines() is
    actually called — proves the deferral is real, not just a renamed eager
    read. Falsification: if _ctx_expansion or its outer closure called
    status_fn eagerly, `calls` would already be 1 before lines() runs."""
    from reyn.interfaces.inline.app import _ctx_expansion
    calls = []
    def _status_fn():
        calls.append(1)
        return {"effective_trigger": 100, "free_window": 50}
    snap = _snap(ctx_compaction_status_fn=_status_fn)
    el = _ctx_expansion(snap, lambda _: None)
    assert calls == [], "status_fn must not be called before lines() is invoked"
    el.lines()
    assert calls == [1], "status_fn must be called exactly once when lines() runs"


def test_ctx_expansion_compaction_estimate_zero_trigger_shows_zero_percent() -> None:
    """Tier 2: a zero/unknown compaction trigger must not divide by zero."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap(ctx_compaction_status_fn=lambda: {"effective_trigger": 0, "free_window": 0})
    joined = "\n".join(_ctx_expansion(snap, lambda _: None).lines())
    assert "0 / 0 tokens est.  (0% to trigger)" in joined


def test_ctx_expansion_missing_compaction_status_fn_defaults_to_zero() -> None:
    """Tier 2: a snap without ctx_compaction_status_fn (e.g. an older caller,
    or a test that doesn't care about this line) must not crash."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap()
    snap.pop("ctx_compaction_status_fn", None)
    joined = "\n".join(_ctx_expansion(snap, lambda _: None).lines())
    assert "0 / 0 tokens est.  (0% to trigger)" in joined


def test_ctx_expansion_shows_current_turn_cache_hit_only() -> None:
    """Tier 2: ctx is the CURRENT-state chip (owner: 「ctxはカレントの状態と考える」) —
    it shows only the most recent LLM call's cache-hit rate. Cumulative
    cache-hit lives in the cost chip instead (see
    test_cost_expansion_shows_cumulative_cache_hit_rate), so ctx must NOT
    duplicate a session-cumulative figure."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap(
        usage=(80_000, 500, 80_500), session_cached_tokens=60_000,  # session cumulative — unused here
        ctx_recent_usage=(10_000, 2_000),                           # last call: 20%
    )
    lines = _ctx_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)
    assert "20% hit (2,000 / 10,000 prompt tokens)" in joined
    assert "75% hit" not in joined  # the session-cumulative figure must NOT appear


def test_ctx_expansion_cache_hit_zero_when_no_prompt_tokens() -> None:
    """Tier 2: no prompt tokens yet (fresh session, no call completed) must not
    divide by zero."""
    from reyn.interfaces.inline.app import _ctx_expansion
    snap = _snap(usage=(0, 0, 0), session_cached_tokens=0, ctx_recent_usage=(0, 0))
    joined = "\n".join(_ctx_expansion(snap, lambda _: None).lines())
    assert "0% hit (0 / 0 prompt tokens)" in joined


def test_more_chip_has_no_expansion() -> None:
    """Tier 2: the 'more' chip has NO expansion (2-level redesign) — Enter on
    it opens the level-1 sub-bar (_MORE_SUB_CHIP_SPECS) via app.py's
    _is_more()/_menu_open special-case, not a menu_region dropdown directly."""
    spec = next(s for s in _CHIP_SPECS if s.key == "more")
    assert spec.expansion is None


def _all_lines(el) -> list[str]:
    """Flatten lines from a RegionElement (or a list of them)."""
    if isinstance(el, list):
        return [ln for e in el for ln in e.lines()]
    return el.lines()


# ---------------------------------------------------------------------------
# _MORE_SUB_CHIP_SPECS registry
# ---------------------------------------------------------------------------


def test_more_sub_chip_specs_has_required_keys_in_order() -> None:
    """Tier 2: the sub-bar exposes tool/mcp/skill/pipe/hook/cron in that order."""
    keys = [s.key for s in _MORE_SUB_CHIP_SPECS]
    assert keys == ["tool", "mcp", "skill", "pipe", "hook", "cron"]


def test_more_sub_chip_specs_all_have_an_expansion() -> None:
    """Tier 2: every sub-bar category (unlike "more" itself) has a real expansion —
    each Enter on a sub-chip must show something, never a dead dropdown."""
    assert all(s.expansion is not None for s in _MORE_SUB_CHIP_SPECS)


# ---------------------------------------------------------------------------
# Category isolation — the core regression this redesign guards against: the
# old _more_expansion concatenated tool/mcp/skill/hook/cron into ONE flat
# list; each category expansion below must show ONLY its own kind.
# ---------------------------------------------------------------------------


def test_tool_category_shows_only_tool_items_not_mcp_or_skill() -> None:
    """Tier 2: _tool_category_expansion ignores mcp/skill visibility_items even
    when all three kinds are present in the same snapshot."""
    snap = _snap(
        visibility_items=[
            {"kind": "tool", "name": "bash", "on": True},
            {"kind": "mcp", "name": "github", "on": True},
            {"kind": "skill", "name": "pdf_editing", "on": True},
        ],
    )
    el = _tool_category_expansion(snap, lambda _: None)
    lines = _all_lines(el)
    assert any("bash" in ln for ln in lines)
    assert not any("github" in ln for ln in lines)
    assert not any("pdf_editing" in ln for ln in lines)


def test_mcp_category_shows_only_mcp_items_not_tool_or_skill() -> None:
    """Tier 2: _mcp_category_expansion ignores tool/skill visibility_items."""
    snap = _snap(
        visibility_items=[
            {"kind": "tool", "name": "bash", "on": True},
            {"kind": "mcp", "name": "github", "on": True},
            {"kind": "skill", "name": "pdf_editing", "on": True},
        ],
    )
    el = _mcp_category_expansion(snap, lambda _: None)
    lines = _all_lines(el)
    assert any("github" in ln for ln in lines)
    assert not any("bash" in ln for ln in lines)
    assert not any("pdf_editing" in ln for ln in lines)


def test_skill_category_shows_only_skill_items_not_tool_or_mcp() -> None:
    """Tier 2: _skill_category_expansion ignores tool/mcp visibility_items."""
    snap = _snap(
        visibility_items=[
            {"kind": "tool", "name": "bash", "on": True},
            {"kind": "mcp", "name": "github", "on": True},
            {"kind": "skill", "name": "pdf_editing", "on": True},
        ],
    )
    el = _skill_category_expansion(snap, lambda _: None)
    lines = _all_lines(el)
    assert any("pdf_editing" in ln for ln in lines)
    assert not any("bash" in ln for ln in lines)
    assert not any("github" in ln for ln in lines)


# ---------------------------------------------------------------------------
# _tool_category_expansion
# ---------------------------------------------------------------------------


def test_tool_category_empty_shows_none() -> None:
    """Tier 2: no tool visibility_items and no fallback source → "(none)"."""
    el = _tool_category_expansion(_snap(), lambda _: None)
    assert isinstance(el, DetailElement)
    assert _all_lines(el) == ["(none)"]


def test_tool_category_toggle_rows_and_dispatch() -> None:
    """Tier 2: tool visibility_items yield [on]/[off] CommandUIElement rows;
    selecting dispatches '/visibility off|on tool <name>'."""
    dispatched: list[str] = []
    snap = _snap(
        visibility_items=[
            {"kind": "tool", "name": "bash", "on": True},
            {"kind": "tool", "name": "sandboxed_exec", "on": False},
        ],
    )
    el = _tool_category_expansion(snap, dispatched.append)
    assert isinstance(el, CommandUIElement)
    lines = el.lines()
    assert any("[on] bash" in ln for ln in lines)
    assert any("[off] sandboxed_exec" in ln for ln in lines)
    el.on_select(0)
    el.on_select(1)
    assert dispatched == ["/visibility off tool bash", "/visibility on tool sandboxed_exec"]


# ---------------------------------------------------------------------------
# _mcp_category_expansion (has a config-based fallback, unlike tool)
# ---------------------------------------------------------------------------


def test_mcp_category_falls_back_to_config_servers_when_no_visibility_items() -> None:
    """Tier 2: without mcp visibility_items, falls back to the read-only
    mcp_servers config listing (name-only, no toggle state)."""
    snap = _snap(mcp_servers=[{"name": "github"}])
    el = _mcp_category_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert any("github" in ln for ln in _all_lines(el))


def test_mcp_category_toggle_rows_and_dispatch() -> None:
    """Tier 2: mcp visibility_items yield toggle rows; selecting dispatches
    '/visibility on mcp <name>' for an [off] row."""
    dispatched: list[str] = []
    snap = _snap(visibility_items=[{"kind": "mcp", "name": "brave", "on": False}])
    el = _mcp_category_expansion(snap, dispatched.append)
    assert isinstance(el, CommandUIElement)
    el.on_select(0)
    assert dispatched == ["/visibility on mcp brave"]


# ---------------------------------------------------------------------------
# _skill_category_expansion (has a config-based fallback, mirrors mcp)
# ---------------------------------------------------------------------------


def test_skill_category_falls_back_to_config_skills_when_no_visibility_items() -> None:
    """Tier 2: without skill visibility_items, falls back to the read-only
    skills config listing (name-only, no toggle state)."""
    snap = _snap(skills=[{"name": "pdf_editing"}])
    el = _skill_category_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert any("pdf_editing" in ln for ln in _all_lines(el))


def test_skill_category_toggle_rows_and_dispatch() -> None:
    """Tier 2: skill visibility_items yield toggle rows; selecting dispatches
    '/visibility off skill <name>' for an [on] row."""
    dispatched: list[str] = []
    snap = _snap(visibility_items=[{"kind": "skill", "name": "pdf_editing", "on": True}])
    el = _skill_category_expansion(snap, dispatched.append)
    assert isinstance(el, CommandUIElement)
    el.on_select(0)
    assert dispatched == ["/visibility off skill pdf_editing"]


# ---------------------------------------------------------------------------
# _hook_category_expansion
# ---------------------------------------------------------------------------


def test_hook_category_falls_back_to_config_hooks_when_no_hook_items() -> None:
    """Tier 2: without hook_items, falls back to the read-only hooks config listing."""
    snap = _snap(hooks=[{"label": "on_push"}])
    el = _hook_category_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert any("on_push" in ln for ln in _all_lines(el))


def test_hook_category_toggle_rows_and_dispatch() -> None:
    """Tier 2: hook_items yield [on]/[off] rows; selecting dispatches '/hook off|on <name>'."""
    dispatched: list[str] = []
    snap = _snap(
        hook_items=[
            {"name": "format", "scope": "runtime", "on": True},
            {"name": "lint", "scope": "per-agent", "on": False},
        ],
    )
    el = _hook_category_expansion(snap, dispatched.append)
    assert isinstance(el, CommandUIElement)
    lines = el.lines()
    assert any("[on] format" in ln for ln in lines)
    assert any("[off] lint" in ln for ln in lines)
    el.on_select(0)
    assert dispatched == ["/hook off format"]


# ---------------------------------------------------------------------------
# _pipe_category_expansion (always read-only — no toggle mechanism, in scope)
# ---------------------------------------------------------------------------


def test_pipe_category_empty_shows_none() -> None:
    """Tier 2: no registered pipelines → "(none)"."""
    el = _pipe_category_expansion(_snap(), lambda _: None)
    assert isinstance(el, DetailElement)
    assert _all_lines(el) == ["(none)"]


def test_pipe_category_shows_name_and_description_read_only() -> None:
    """Tier 2: registered pipelines show name + description; always DetailElement
    (never CommandUIElement — on/off toggling for pipelines is explicitly out
    of scope for this slice)."""
    snap = _snap(pipelines=[{"name": "hello", "description": "Minimal greeting pipeline"}])
    el = _pipe_category_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False
    joined = " ".join(_all_lines(el))
    assert "hello" in joined
    assert "Minimal greeting pipeline" in joined


# ---------------------------------------------------------------------------
# _cron_category_expansion (always read-only — no toggle mechanism, in scope)
# ---------------------------------------------------------------------------


def test_cron_category_empty_shows_none() -> None:
    """Tier 2: no cron jobs → "(none)"."""
    el = _cron_category_expansion(_snap(), lambda _: None)
    assert isinstance(el, DetailElement)
    assert _all_lines(el) == ["(none)"]


def test_cron_category_shows_on_off_markers_read_only() -> None:
    """Tier 2: cron jobs render nightly with 'on' and paused with 'off'; always
    DetailElement (cron on/off toggling is explicitly out of scope for this slice)."""
    snap = _snap(
        cron_jobs=[
            {"name": "nightly", "schedule": "0 0 * * *", "enabled": True},
            {"name": "paused", "schedule": "0 * * * *", "enabled": False},
        ],
    )
    el = _cron_category_expansion(snap, lambda _: None)
    assert isinstance(el, DetailElement)
    assert el.selectable is False
    joined = " ".join(_all_lines(el))
    assert "[on] nightly" in joined
    assert "[off] paused" in joined


# ---------------------------------------------------------------------------
# _extract_skills / _session_pipelines graceful fallback
# ---------------------------------------------------------------------------


def test_extract_skills_returns_empty_on_missing_config() -> None:
    """Tier 2: _extract_skills returns [] when config has no skills/entries section."""
    class _NoSkills:
        pass
    assert _extract_skills(_NoSkills()) == []


def test_extract_skills_reads_entries_dict() -> None:
    """Tier 2: _extract_skills reads config.skills.entries (a name→spec dict)."""
    class _WithSkills:
        skills = {"entries": {"pdf_editing": {"path": "skills/pdf.md"}}}
    result = _extract_skills(_WithSkills())
    assert result == [{"name": "pdf_editing"}]


def test_session_pipelines_returns_empty_when_attribute_absent() -> None:
    """Tier 2: _session_pipelines returns [] when session has no pipeline_registry
    (defensive — Session always constructs one in practice)."""
    assert _session_pipelines(object()) == []


def test_session_pipelines_reads_registry_entries() -> None:
    """Tier 2: _session_pipelines calls pipeline_registry.entries() and returns
    name/description dicts."""
    class _FakeRegistry:
        def entries(self):
            return (("hello", "Minimal greeting pipeline"),)

    class _FakeSession:
        pipeline_registry = _FakeRegistry()

    result = _session_pipelines(_FakeSession())
    assert result == [{"name": "hello", "description": "Minimal greeting pipeline"}]


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


def test_cost_expansion_breaks_down_ses_agt_prj() -> None:
    """Tier 2: cost expansion's Total row shows distinct Session / Agent /
    Project amounts when multiple agents/sessions accrue cost (Project total
    across agents >= the attached session). Each scope column renders its own
    dollar amount, not one collapsed total (owner-approved 5-row x 3-scope
    cost-panel design, #cost-panel-breakdown)."""
    snap = _snap(cost_usd=0.0100, cost_agent=0.0100, cost_total=0.0750)
    lines = _cost_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)
    header = next(ln for ln in lines if ln.startswith("COST"))
    total_row = next(ln for ln in lines if ln.startswith("Total"))
    # the 3 scope headers are present, in Session/Agent/Project order
    assert ["Ses", "Agt", "Prj"] == header.split()[-3:]
    assert "0.0750" in total_row     # Project: sum across loaded agents
    assert "0.0100" in total_row     # Session (and Agent, same value here)
    # the breakdown is not collapsed: distinct Project vs Session figures
    # both appear in the Total row (not just one repeated number).
    assert total_row.count("0.0100") >= 1 and "0.0750" in total_row
    assert "Input" in joined and "Output" in joined and "Saved" in joined


def test_cost_expansion_shows_cumulative_cache_hit_rate() -> None:
    """Tier 2: owner explicitly asked to confirm the SESSION-cumulative cache-hit
    figure lives in the cost chip (not ctx, which is current-call-only) —
    session_cached_tokens is a subset of usage[0]=prompt_tokens (#1772 semantics)."""
    snap = _snap(usage=(80_000, 500, 80_500), session_cached_tokens=60_000)
    lines = _cost_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)
    assert "75% hit (60,000 / 80,000 prompt tokens, cumulative)" in joined


def test_cost_expansion_input_output_saved_rows_render_expected_values() -> None:
    """Tier 2: Input/Output/Saved/Saved% rows render the session-scope
    CostBreakdown's derived values — Input = prompt+cache_read+cache_creation
    cost, Output = completion_cost, Saved = cache_savings, Saved% = Saved /
    (Input + Saved) (the no-cache-baseline denominator, not Saved / Total)."""
    breakdown = CostBreakdown(
        prompt_cost=0.0010,
        cache_read_cost=0.0002,
        cache_creation_cost=0.0004,
        completion_cost=0.0089,
        cache_savings=0.0072,
        prompt_tokens=1000,
        cached_tokens=800,
    )
    session_total = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost + breakdown.completion_cost
    snap = _snap(
        cost_usd=session_total, cost_agent=0.0, cost_total=0.0,
        cost_breakdown_session=breakdown,
    )
    lines = _cost_expansion(snap, lambda _: None).lines()
    input_row = next(ln for ln in lines if ln.startswith("Input"))
    output_row = next(ln for ln in lines if ln.startswith("Output"))
    saved_row = next(ln for ln in lines if ln.startswith("Saved") and not ln.startswith("Saved%"))
    pct_row = next(ln for ln in lines if ln.startswith("Saved%"))

    expected_input = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    expected_pct = round(100 * breakdown.cache_savings / (expected_input + breakdown.cache_savings))

    assert f"${expected_input:.4f}" in input_row
    assert f"${breakdown.completion_cost:.4f}" in output_row
    assert f"${breakdown.cache_savings:.4f}" in saved_row
    assert f"{expected_pct}%" in pct_row


def test_cost_expansion_saved_pct_denominator_is_input_plus_saved_not_total() -> None:
    """Tier 2: FALSIFY the wrong denominator. Saved% must be computed as
    Saved / (Input + Saved), NOT Saved / Total. Total includes Output, which
    is unrelated to the cache discount — using it as the denominator would
    silently understate the shown percentage."""
    breakdown = CostBreakdown(
        prompt_cost=0.0010,
        cache_read_cost=0.0002,
        completion_cost=0.0500,  # large Output relative to Input — makes the
        cache_savings=0.0072,    # two candidate denominators visibly diverge
        prompt_tokens=1000,
        cached_tokens=800,
    )
    input_cost = breakdown.prompt_cost + breakdown.cache_read_cost
    total = input_cost + breakdown.completion_cost
    correct_pct = round(100 * breakdown.cache_savings / (input_cost + breakdown.cache_savings))
    wrong_pct = round(100 * breakdown.cache_savings / total)
    assert correct_pct != wrong_pct, "scenario must make the two denominators diverge"

    snap = _snap(
        cost_usd=total, cost_agent=0.0, cost_total=0.0,
        cost_breakdown_session=breakdown,
    )
    lines = _cost_expansion(snap, lambda _: None).lines()
    pct_row = next(ln for ln in lines if ln.startswith("Saved%"))
    assert f"{correct_pct}%" in pct_row
    assert f"{wrong_pct}%" not in pct_row


def test_cost_expansion_shows_approximate_marker_when_breakdown_diverges_from_total() -> None:
    """Tier 2: >200k tiered-pricing guard — when the accumulated breakdown's
    Input+Output sum materially diverges from the authoritative (litellm-
    accurate) Total, the panel marks the affected scope's rows with "~"
    (approximate) instead of showing exact numbers that visibly don't add up,
    and appends a footnote. Below-threshold scenarios (the other tests in this
    file) must NOT show the marker."""
    breakdown = CostBreakdown(
        prompt_cost=1.0,
        completion_cost=1.0,
        cache_savings=0.1,
        prompt_tokens=1000,
        cached_tokens=100,
    )
    # authoritative Total materially disagrees with breakdown.total_cost (2.0)
    # — simulates >200k tiered pricing having kicked in for this scope. Agent /
    # Project scopes zeroed so only the Session column drives this assertion.
    diverging_total = 3.0
    snap = _snap(
        cost_usd=diverging_total, cost_agent=0.0, cost_total=0.0,
        cost_breakdown_session=breakdown,
    )
    lines = _cost_expansion(snap, lambda _: None).lines()
    input_row = next(ln for ln in lines if ln.startswith("Input"))
    assert "~" in input_row
    assert any("approx" in ln for ln in lines)
    # a genuine divergence is NOT reported as "unavailable" (distinct causes).
    assert not any("unavailable" in ln for ln in lines)
    # Total row itself stays exact — it's the authoritative figure, never
    # marked approximate.
    total_row = next(ln for ln in lines if ln.startswith("Total"))
    assert "~" not in total_row
    assert f"${diverging_total:.4f}" in total_row


def test_cost_expansion_no_approximate_marker_when_breakdown_reconciles() -> None:
    """Tier 2: the normal (below-200k) case shows no approximate marker — the
    guard must not false-positive on ordinary floating-point-exact scenarios."""
    breakdown = CostBreakdown(
        prompt_cost=0.001, completion_cost=0.002, cache_savings=0.0005,
        prompt_tokens=100, cached_tokens=10,
    )
    reconciled_total = breakdown.total_cost
    snap = _snap(
        cost_usd=reconciled_total, cost_agent=0.0, cost_total=0.0,
        cost_breakdown_session=breakdown,
    )
    lines = _cost_expansion(snap, lambda _: None).lines()
    assert not any("~" in ln for ln in lines)
    assert not any("approx" in ln for ln in lines)


def test_cost_expansion_restart_state_does_not_false_fire_tiered_marker() -> None:
    """Tier 2: FALSIFY the >200k-marker false-fire after restart. The durable
    per-agent Total survives restart (ledger-hydrated), but the in-memory
    CostBreakdown resets to 0 (NOT ledger-persisted). A scope with an empty
    breakdown (component-sum 0) but a non-zero authoritative Total is
    "breakdown UNAVAILABLE", NOT ">200k tiered pricing" — the panel must NOT
    show the "~"/tiered footnote (the misattribution the architect caught), and
    must instead show the Total exact + a distinct "unavailable" note with the
    component cells blanked to "—".

    Without the state guard (i.e. if divergence alone drove the marker), this
    scenario's 0-vs-nonzero gap would fire the tiered footnote — this test
    fails against that pre-fix behavior. Verified load-bearing: neutralizing
    the ``unavail`` branch (``if False``) makes BOTH the direct-state assertion
    AND the rendering assertions below go RED (the state becomes ``approx`` and
    the "~"/tiered footnote false-fires).
    """
    from reyn.llm.pricing import CostBreakdown

    # Direct-state contract: empty breakdown + non-zero authoritative Total
    # classifies as "unavail" (NOT "approx"). This is the load-bearing branch
    # under strip — asserting the returned state directly (not just the
    # rendered rows) makes the guard impossible to satisfy without the branch.
    empty_state = _cost_scope_state(CostBreakdown(), authoritative_total=5.0)
    assert empty_state[-1] == "unavail", (
        f"empty breakdown + Total>0 must be 'unavail', got {empty_state[-1]!r}"
    )
    # a scope WITH divergent-but-present components is the DISTINCT 'approx'
    # case — pins the two causes apart so 'unavail' can't absorb tiering.
    present_diverging = _cost_scope_state(
        CostBreakdown(prompt_cost=1.0, completion_cost=1.0), authoritative_total=3.0,
    )
    assert present_diverging[-1] == "approx"

    # Agent scope: durable Total rebuilt from ledger, breakdown reset to empty.
    empty = CostBreakdown()
    snap = _snap(
        cost_usd=0.0,            # session (this process) genuinely 0 → ok/empty
        cost_agent=5.0,          # durable per-agent Total survived restart
        cost_total=5.0,          # project = that one agent
        cost_breakdown_session=empty,
        cost_breakdown_agent=empty,
        cost_breakdown_project=empty,
    )
    lines = _cost_expansion(snap, lambda _: None).lines()
    joined = "\n".join(lines)

    # The false-fire is suppressed: no tiered-pricing marker/footnote.
    assert not any("~" in ln for ln in lines)
    assert "approx" not in joined
    assert "tiered" not in joined
    # Instead, the distinct "unavailable" note appears, and the Total stays exact.
    assert "unavailable" in joined
    total_row = next(ln for ln in lines if ln.startswith("Total"))
    assert "$5.0000" in total_row
    # component cells blanked to the em-dash placeholder, not fake $0.0000.
    input_row = next(ln for ln in lines if ln.startswith("Input"))
    assert "—" in input_row


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


# ---------------------------------------------------------------------------
# Category dropdown overflow (the "…" chip's original "Window too small" bug,
# #2633) — still a live concern PER CATEGORY after this 2-level redesign: the
# tool category alone can list one row per registered tool (76 observed live).
# ---------------------------------------------------------------------------


def test_tool_category_many_tools_exceeds_menu_region_cap() -> None:
    """Tier 2: a real session's per-tool visibility list (one row per registered
    tool, dozens in production — 76 observed live) produces MORE lines than
    _MENU_REGION_MAX_HEIGHT for the tool category ALONE. #2633 fixed
    app.py's dropdown_height() (was unbounded Dimension.exact(len(menu_region.
    lines()))) with menu_region.set_max_visible() + windowed rendering — this
    2-level redesign reuses that SAME menu_region/dropdown for each sub-bar
    category, so the cap must still hold per-category (this pins the
    content-volume precondition; the actual height-cap fix lives in
    un-exported closures inside run_inline_input and isn't unit-testable
    directly — see test_inline_region_framework.py for the underlying
    Region.set_max_visible mechanics)."""
    many_tools = [
        {"kind": "tool", "name": f"tool_{i}", "on": True} for i in range(76)
    ]
    snap = _snap(visibility_items=many_tools)
    el = _tool_category_expansion(snap, lambda _: None)
    assert len(el.lines()) > _MENU_REGION_MAX_HEIGHT
