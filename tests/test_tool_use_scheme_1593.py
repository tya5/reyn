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

from types import SimpleNamespace

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
        tcs = getattr(llm_response, "tool_calls", None) or []
        return [{"tc": tc, "name": tc["name"], "args": {}} for tc in tcs]

    async def dispatch(self, actions: list[dict]) -> list[dict]:
        self.calls.append("dispatch")
        return [{"status": "ok", "for": a["name"]} for a in actions]

    def feedback(self, result) -> list[dict]:
        # #1608: ops.feedback now receives the enriched ExecutionResult and returns
        # appendable MESSAGES (the relocated assistant+tool-message build). The Fake
        # records the delegated result + returns a representative message sequence.
        self.calls.append("feedback")
        self.last_feedback_result = result
        return [
            {"role": "assistant", "content": result.assistant_content,
             "tool_calls": result.tool_calls},
            *(
                {"role": "tool", "tool_call_id": tc.get("id"), "content": str(r)}
                for tc, r in zip(result.tool_calls, result.tool_results)
            ),
        ]


# ── registry ────────────────────────────────────────────────────────────────


def test_registry_register_get_resolve() -> None:
    """Tier 2: register a scheme by name, look it up, and the default name resolves."""
    register_scheme(UniversalCategoryScheme())
    assert DEFAULT_SCHEME_NAME == "enumerate-all"
    s = get_scheme(DEFAULT_SCHEME_NAME)
    assert s is not None and s.name == "enumerate-all"  # #1657
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


def test_universal_interpret_execute_with_tool_calls() -> None:
    """Tier 2: with tool calls, universal yields Execute carrying the ops-resolved
    actions — the OS exclude-gates these pre-dispatch. (#1593 loop-unify: the
    no-tool-call → PlainText case is pinned in test_scheme_interpretation_match_1593.)"""
    ops = _RecordingOps()
    resp = SimpleNamespace(content="", tool_calls=[{"name": "a"}, {"name": "b"}])
    interp = UniversalCategoryScheme().interpret(resp, tool_catalog={}, ops=ops)
    assert isinstance(interp, Execute)
    assert [x["name"] for x in interp.actions] == ["a", "b"]
    assert "resolve" in ops.calls


@pytest.mark.asyncio
async def test_universal_execute_and_feedback_round_trip() -> None:
    """Tier 2: execute delegates dispatch; format_feedback delegates to ops.feedback
    with the ENRICHED result and returns appendable MESSAGES (#1608 unified contract,
    not the former tool_results passthrough)."""
    ops = _RecordingOps()
    scheme = UniversalCategoryScheme()
    res = await scheme.execute(Execute(actions=[{"name": "a"}]), ExecContext(), ops)
    assert res.tool_results == [{"status": "ok", "for": "a"}]
    enriched = ExecutionResult(
        tool_results=res.tool_results, tool_calls=[{"id": "c1"}], assistant_content="hi",
    )
    fb = scheme.format_feedback(enriched, ops)
    # the full enriched result is delegated (not just tool_results) ...
    assert ops.last_feedback_result is enriched
    # ... and the return is appendable messages: assistant turn + one tool message.
    assert fb[0]["role"] == "assistant" and fb[0]["tool_calls"] == [{"id": "c1"}]
    assert fb[1]["role"] == "tool" and fb[1]["tool_call_id"] == "c1"
    assert ops.calls.count("dispatch") == 1 and ops.calls.count("feedback") == 1


# ── per-layer config ─────────────────────────────────────────────────────────


def test_tool_use_config_per_layer_and_defaults() -> None:
    """Tier 2: per-layer scheme selection parses (NON-default) + defaults to
    universal-category; a non-string value is a loud error."""
    assert _build_tool_use_config(None) == ToolUseConfig()
    assert ToolUseConfig().chat == "enumerate-all"
    cfg = _build_tool_use_config({"chat": "enumerate-all"})
    assert cfg.chat == "enumerate-all"
    assert cfg.step == "universal-category" and cfg.phase == "universal-category"
    with pytest.raises(ValueError):
        _build_tool_use_config({"chat": 123})
