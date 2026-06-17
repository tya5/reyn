"""Tier 2: #1654 — the chat router resolves to the FULL ModelSpec so per-model
kwargs (reasoning_effort, temperature, …) reach litellm.

The gap this guards: the chat router used host.resolve_model(name) → the bare
``.model`` STRING, dropping ModelSpec.kwargs → reasoning_effort (#1650/#1652) and
every model kwarg were inert on the chat-router path. The #1650/#1652 headless
tests missed it by passing a ModelSpec DIRECTLY to call_llm_tools / stubbing
litellm; this test goes through the host's RESOLVE path (resolve_model_spec)
so the drop can't silently regress. Non-default values (#1646 lesson).

Real RouterHostAdapter + a resolver carrying kwargs + a real async litellm stub
(no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.chat.services import MemoryService, RouterHostAdapter
from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver, ModelSpec

_EFFORT = "high"     # non-default reasoning_effort
_TEMP = 0.37         # non-default temperature


async def _noop(*a, **k):
    return {}


def _mk_host_with_kwargs():
    events = EventLog(subscribers=[])
    workspace = Path(".reyn") / "agents" / "t"
    resolver = ModelResolver({
        # the chat router's tier carries per-model kwargs in reyn.yaml dict form
        "light": {"model": "gemini/gemini-2.5-flash-lite",
                  "reasoning_effort": _EFFORT, "temperature": _TEMP},
    })
    return RouterHostAdapter(
        agent_name="t", agent_role="r", output_language="en",
        allowed_skills=None, allowed_mcp=None, permission_resolver=None,
        mcp_servers=None, project_context="", events=events, resolver=resolver,
        memory=MemoryService(agent_workspace_dir=workspace, events=events,
            file_write=_noop, file_read=_noop, file_delete=_noop, file_regenerate_index=_noop),
        journal=None, agent_registry=None, skill_enumerate_fn=lambda exclude: [],
        agent_workspace_dir=workspace, plan_registry_getter=lambda: None,
        file_read=_noop, file_write=_noop, file_delete=_noop, file_list_directory=_noop,
        file_regenerate_index=_noop, mcp_list_servers=_noop, mcp_list_tools=_noop,
        mcp_call_tool=_noop, run_skill_awaitable=_noop, spawn_skill=_noop, send_to_agent=_noop,
        put_outbox=_noop, append_history=lambda m: None, spawn_plan_task=_noop,
        delegation_tracker=lambda: [], agent_replies_tracker=lambda: [],
        turn_budget_engine=None, environment_backend=None,
    )


def test_resolve_model_spec_preserves_kwargs_resolve_model_drops_them():
    """Tier 2: #1654 — resolve_model_spec returns the FULL spec (kwargs intact);
    resolve_model returns the bare string (the drop that caused the bug). This is
    the distinction the chat router relies on."""
    host = _mk_host_with_kwargs()
    spec = host.resolve_model_spec("light")
    assert isinstance(spec, ModelSpec)
    assert spec.kwargs.get("reasoning_effort") == _EFFORT
    assert spec.kwargs.get("temperature") == _TEMP
    # resolve_model (the old path) drops kwargs — bare string only:
    assert host.resolve_model("light") == "gemini/gemini-2.5-flash-lite"


def test_dict_form_reasoning_effort_accepted_for_openai_summary():
    """Tier 2: #1654 — the OpenAI summary opt-in dict form
    {"effort": <level>, "summary": "detailed"} is accepted (the level is
    validated; the summary rides through to litellm's GPT-5 transformation)."""
    spec = ModelSpec(
        model="openai/gpt-5",
        kwargs={"reasoning_effort": {"effort": "low", "summary": "detailed"}},
    )
    assert spec.kwargs["reasoning_effort"] == {"effort": "low", "summary": "detailed"}


def test_dict_form_invalid_effort_level_rejected():
    """Tier 2: #1654 — an invalid effort level inside the dict is rejected at
    load (same fail-fast as the string form)."""
    import pytest
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        ModelSpec(model="openai/gpt-5", kwargs={"reasoning_effort": {"effort": "bogus", "summary": "detailed"}})


def _fake_resp():
    msg = type("_M", (), {"content": "ok", "tool_calls": None, "reasoning_content": None})()
    ch = type("_C", (), {"message": msg, "finish_reason": "stop"})()
    us = type("_U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
    return type("_R", (), {"choices": [ch], "usage": us})()


class _Capture:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        return _fake_resp()


def test_resolved_spec_kwargs_reach_litellm_via_call_llm_tools(monkeypatch):
    """Tier 2: #1654 — a spec from host.resolve_model_spec, passed to
    call_llm_tools (the chat-router path), delivers its kwargs to the litellm
    call — NOT dropped. Goes through the resolve path (the regression guard)."""
    import litellm

    from reyn.llm.llm import call_llm_tools

    host = _mk_host_with_kwargs()
    spec = host.resolve_model_spec("light")  # the FIXED resolve path
    stub = _Capture()
    monkeypatch.setattr(litellm, "acompletion", stub)
    asyncio.run(call_llm_tools(
        model=spec, messages=[{"role": "user", "content": "hi"}], tools=[], max_retries=0,
    ))
    assert stub.calls, "litellm.acompletion never reached"
    kw = stub.calls[0]
    assert kw.get("reasoning_effort") == _EFFORT, f"reasoning_effort dropped: {sorted(kw)}"
    assert kw.get("temperature") == _TEMP, f"temperature dropped: {sorted(kw)}"
