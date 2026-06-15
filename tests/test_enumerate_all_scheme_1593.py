"""Tier 2: EnumerateAllScheme conformance — the flat-native-JSON scheme (#1593 PR-2).

enumerate-all is the first SELF-CONTAINED ToolUseScheme: its presentation is its
own (flat catalog enumeration vs universal's wrappers), while interpret / execute /
format_feedback delegate to the shared router SchemeOps. These pin the 4-method
contract + the presentation composition (base_tools + catalog_entries flat + the
prior-shape SP params), without a running router.

Uses a hand-written ``_FakeOps`` (a protocol-conforming Fake with explicit
return values — NOT a MagicMock; per testing.ja.md "use real instances or a
Fake"), so the test asserts the SCHEME's delegation/composition, not the router's
substrate (which has its own tests).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tools.scheme import (
    Execute,
    ExecutionResult,
    Presentation,
    SchemeOps,
    ToolUseScheme,
)
from reyn.tools.schemes.enumerate_all import EnumerateAllScheme


class _FakeOps:
    """Protocol-conforming Fake SchemeOps with deterministic returns (not a mock)."""

    def __init__(self) -> None:
        self.dispatched: list[dict] | None = None

    def present(self, available, layer_ctx) -> Presentation:  # universal-only; unused here
        return Presentation(llm_tools_payload=[{"function": {"name": "WRAPPER"}}])

    def base_tools(self, available, layer_ctx) -> list[dict]:
        return [{"function": {"name": "file__read"}}]

    async def catalog_entries(self) -> list[dict]:
        return [{"function": {"name": "git__commit"}}, {"function": {"name": "web__fetch"}}]

    def resolve(self, llm_response, tool_catalog: dict) -> list[dict]:
        return [{"tc": llm_response, "name": "git__commit", "args": {}}]

    async def dispatch(self, actions: list[dict]) -> list[dict]:
        self.dispatched = actions
        return [{"name": a["name"], "ok": True} for a in actions]

    def feedback(self, result) -> list[dict]:
        # #1608: ops.feedback now receives the enriched ExecutionResult; build the
        # representative messages from its tool_results (delegation unchanged).
        return [{"role": "tool", "content": tr["name"]} for tr in result.tool_results]


def test_enumerate_all_conforms_to_protocol() -> None:
    """Tier 2: EnumerateAllScheme satisfies the ToolUseScheme protocol + names itself."""
    s = EnumerateAllScheme()
    assert isinstance(s, ToolUseScheme)
    assert s.name == "enumerate-all"


@pytest.mark.asyncio
async def test_build_presentation_is_base_plus_catalog_flat() -> None:
    """Tier 2: presentation = base_tools + catalog_entries, flat (no universal
    wrappers / no discovery). The scheme composes the router building blocks
    (catalog_entries awaited — #1593 PR-2 async seam)."""
    s = EnumerateAllScheme()
    pres = await s.build_presentation(
        {"skills_for_tools": [], "hot_list_aliases": []}, {"search_visible": False}, _FakeOps(),
    )
    names = [t["function"]["name"] for t in pres.llm_tools_payload]
    assert names == ["file__read", "git__commit", "web__fetch"]   # base then catalog, flat
    assert "WRAPPER" not in names                                   # NOT via ops.present


@pytest.mark.asyncio
async def test_build_presentation_tool_use_sp_disable_wrappers() -> None:
    """Tier 2: #1627 Stage 4 — enumerate-all's tool_use_sp slot-map encodes the
    no-wrapper, search-visible SP (sp_params removed from build_presentation).

    The slot-map must contain slot_pre_environment (the Capabilities block) with
    NO ## Action categories (universal_wrappers_enabled=False) and WITH the
    search_actions chain (search_visible=True from layer_ctx).
    """
    s = EnumerateAllScheme()
    pres = await s.build_presentation(
        {"skills_for_tools": [], "hot_list_aliases": []},
        {"search_visible": True},
        _FakeOps(),
    )
    # sp_params removed — check the slot-map instead
    assert isinstance(pres.tool_use_sp, dict), "tool_use_sp must be a dict slot-map"
    slots = pres.tool_use_sp
    # Wrappers off → no ## Action categories in slot_post_environment
    assert "slot_post_environment" not in slots or "## Action categories" not in slots.get("slot_post_environment", "")
    # search_visible=True → search_actions in the chain
    assert "search_actions" in slots.get("slot_pre_environment", "")


def test_interpret_resolves_to_execute() -> None:
    """Tier 2: interpret delegates to ops.resolve → Execute carrying resolved
    effective-name actions (qualified names route through the shared resolution)."""
    s = EnumerateAllScheme()
    interp = s.interpret("llm-resp", tool_catalog={}, ops=_FakeOps())
    assert isinstance(interp, Execute)
    assert interp.actions[0]["name"] == "git__commit"


@pytest.mark.asyncio
async def test_execute_dispatches_via_ops() -> None:
    """Tier 2: execute runs the resolved actions through ops.dispatch (the OS
    permission/dispatch substrate, P5) and returns an ExecutionResult."""
    s = EnumerateAllScheme()
    ops = _FakeOps()
    interp = Execute(actions=[{"name": "git__commit", "args": {}}])
    res = await s.execute(interp, None, ops)
    assert isinstance(res, ExecutionResult)
    assert ops.dispatched == interp.actions               # dispatched the resolved actions
    assert res.tool_results[0] == {"name": "git__commit", "ok": True}


def test_format_feedback_delegates_to_ops() -> None:
    """Tier 2: format_feedback delegates to ops.feedback (the shared JSON
    tool_result formatting — enumerate-all reuses universal's base)."""
    s = EnumerateAllScheme()
    msgs = s.format_feedback(ExecutionResult(tool_results=[{"name": "git__commit"}]), _FakeOps())
    assert msgs == [{"role": "tool", "content": "git__commit"}]


def test_per_layer_config_nondefault_selects_enumerate_all() -> None:
    """Tier 2: a NON-default ``tool_use: {chat: enumerate-all}`` resolves the chat
    layer to EnumerateAllScheme, while the default stays universal-category
    (byte-identical) and a chat-only override leaves step/phase untouched.

    Pins the config→selection seam at its public surfaces: the per-layer config
    dataclass (``ToolUseConfig``), the scheme resolver (``_resolve_tool_use_scheme``),
    and the scheme's public ``.name`` — NOT a running router or private state. The
    config value is what each frontend threads (chat_tool_use_scheme) through the
    factory → ChatSession → RouterLoopDriver → RouterLoop(scheme_name=)."""
    from reyn.chat.router_loop import _resolve_tool_use_scheme
    from reyn.config import _build_tool_use_config

    # Default (None / missing tool_use:) → all layers universal-category.
    default_cfg = _build_tool_use_config(None)
    assert default_cfg.chat == "universal-category"
    assert _resolve_tool_use_scheme(default_cfg.chat).name == "universal-category"

    # NON-default chat layer → enumerate-all; sibling layers keep the default.
    cfg = _build_tool_use_config({"chat": "enumerate-all"})
    assert cfg.chat == "enumerate-all"
    assert cfg.step == "universal-category"    # chat-only override is per-layer
    assert cfg.phase == "universal-category"
    assert _resolve_tool_use_scheme(cfg.chat).name == "enumerate-all"
