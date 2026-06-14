"""Tier 2: tool-use scheme abstraction (#1593 PR-1).

PR-1 moves universal-category behind the ``ToolUseScheme`` protocol with **zero
behaviour change** — the byte-identical proof is the *existing* tool-use / LLMReplay
suites passing unchanged (incl. the exclude regressions
``test_chat_exclude_tools_187`` / ``test_exclude_execution_block_1406`` /
``test_run_once_187``). These tests pin the new abstraction surface itself: the
registry, the protocol conformance, the per-layer config, the delegation seam, and
the Execute-only invariant of universal-category. Real types, no mocks (a recording
Fake ``SchemeOps`` exercises the delegation).
"""
from __future__ import annotations

import pytest

from reyn.config import ToolUseConfig, _build_tool_use_config
from reyn.tools.scheme import (
    DEFAULT_SCHEME_NAME,
    ExecContext,
    Execute,
    ExecutionResult,
    Presentation,
    ToolUseScheme,
    get_scheme,
    register_scheme,
    registered_scheme_names,
)
from reyn.tools.schemes.universal_category import UniversalCategoryScheme


class _RecordingOps:
    """A recording Fake ``SchemeOps`` — lets us exercise the delegating scheme
    without a full router. Real callables, no mock framework."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def present(self, available, layer_ctx) -> Presentation:
        self.calls.append("present")
        return Presentation(llm_tools_payload=[{"t": 1}], sp_params={"x": True})

    def resolve(self, llm_response, tool_catalog: dict) -> list[dict]:
        self.calls.append("resolve")
        return [{"tc": tc, "name": tc["name"], "args": {}} for tc in llm_response]

    async def dispatch(self, actions: list[dict]) -> list[dict]:
        self.calls.append("dispatch")
        return [{"status": "ok", "for": a["name"]} for a in actions]

    def feedback(self, tool_results: list[dict]) -> list[dict]:
        self.calls.append("feedback")
        return tool_results


# ── registry ────────────────────────────────────────────────────────────────


def test_registry_register_get_resolve() -> None:
    """Tier 2: register a scheme by name, look it up, and the default name resolves."""
    register_scheme(UniversalCategoryScheme())
    assert DEFAULT_SCHEME_NAME == "universal-category"
    s = get_scheme(DEFAULT_SCHEME_NAME)
    assert s is not None and s.name == "universal-category"
    assert DEFAULT_SCHEME_NAME in registered_scheme_names()
    assert get_scheme("no-such-scheme") is None


def test_universal_conforms_to_protocol() -> None:
    """Tier 2: UniversalCategoryScheme satisfies the ToolUseScheme protocol."""
    assert isinstance(UniversalCategoryScheme(), ToolUseScheme)


# ── delegation seam + Execute-only invariant ─────────────────────────────────


@pytest.mark.asyncio
async def test_universal_build_presentation_delegates() -> None:
    """Tier 2: build_presentation delegates to ops.present (the router's logic).
    Async seam (#1593 PR-2) but universal's body stays a sync delegation — the
    awaited result equals the unchanged ops.present output (byte-identical)."""
    ops = _RecordingOps()
    pres = await UniversalCategoryScheme().build_presentation({}, {}, ops)
    assert "present" in ops.calls
    assert pres.llm_tools_payload == [{"t": 1}] and pres.sp_params == {"x": True}


def test_universal_interpret_emits_execute_only() -> None:
    """Tier 2: universal-category always yields Execute (never RePresent/CodeBlock),
    carrying the ops-resolved actions — the OS exclude-gates these pre-dispatch."""
    ops = _RecordingOps()
    interp = UniversalCategoryScheme().interpret(
        [{"name": "a"}, {"name": "b"}], tool_catalog={}, ops=ops,
    )
    assert isinstance(interp, Execute)
    assert [x["name"] for x in interp.actions] == ["a", "b"]
    assert "resolve" in ops.calls


@pytest.mark.asyncio
async def test_universal_execute_and_feedback_round_trip() -> None:
    """Tier 2: execute delegates dispatch, format_feedback delegates feedback."""
    ops = _RecordingOps()
    scheme = UniversalCategoryScheme()
    res = await scheme.execute(Execute(actions=[{"name": "a"}]), ExecContext(), ops)
    assert res.tool_results == [{"status": "ok", "for": "a"}]
    fb = scheme.format_feedback(ExecutionResult(tool_results=res.tool_results), ops)
    assert fb == res.tool_results
    assert ops.calls.count("dispatch") == 1 and ops.calls.count("feedback") == 1


# ── per-layer config ─────────────────────────────────────────────────────────


def test_tool_use_config_per_layer_and_defaults() -> None:
    """Tier 2: per-layer scheme selection parses (NON-default) + defaults to
    universal-category; a non-string value is a loud error."""
    assert _build_tool_use_config(None) == ToolUseConfig()
    assert ToolUseConfig().chat == "universal-category"
    cfg = _build_tool_use_config({"chat": "enumerate-all"})
    assert cfg.chat == "enumerate-all"
    assert cfg.step == "universal-category" and cfg.phase == "universal-category"
    with pytest.raises(ValueError):
        _build_tool_use_config({"chat": 123})
