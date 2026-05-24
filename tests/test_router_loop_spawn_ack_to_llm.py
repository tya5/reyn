"""Tier 2: RouterLoop spawn-ack-to-LLM env-gated path (NF-W7-B43-2 fix).

Pinned invariants:

- When ``REYN_SPAWN_ACK_TO_LLM=1`` is set AND a tool_call returned a
  spawn-ack (= ``status="spawned"`` from invoke_skill / invoke_action),
  the loop:
    1. emits ``invoke_skill_spawn_ack_exit`` event (= P6 audit, same
       as the default path);
    2. annotates the spawn-ack tool_result with the
       ``_SPAWN_ACK_TOOL_DIRECTIVE`` via ``_post_text``;
    3. continues the loop (= does NOT early-exit, does NOT push an
       OS-synthetic spawn-ack to outbox).
- When the env var is unset, the existing default path runs: OS
  pushes the deterministic ``_SPAWN_ACK_MSG`` to outbox and exits
  the loop. No directive injection on tool_result.
- ``_SPAWN_ACK_TOOL_DIRECTIVE`` is a non-empty string mentioning
  "skill" (= context) and "do not include" (= H3 hallucination
  defense), distinct from the chat-router empty-stop retry directive
  in shape and intent.

References:
- NF-W7-B43-2 root cause: B43 W7-S7 leak attractor (= 10/10
  deterministic in trace-patch-replay; LLM echoed
  ``_SPAWN_ACK_MSG`` literal from history's assistant slot).
- N=10 directive variant ablation (2026-05-20): Variant D
  positive-framing directive: 7/10 ACK, 3/10 EMPTY, 0/10
  HALLUCINATE. Bare status + retry-only re-introduces H3 race
  (= 5/10 HALLUCINATE in N=10 Variant F).
- Anthropic ``handling-stop-reasons`` docs + PR #265 / PR #287's
  ``REYN_EMPTY_STOP_RETRY=1`` covers the residual 3/10 EMPTY rate
  for the spawn-ack-to-LLM opt-in.

testing.ja.md compliance:
- Uses the ``llm_caller=`` injection seam (= PR #265 revision) +
  ``pytest.monkeypatch`` for env var setup. No ``unittest.mock.patch``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.chat.router_loop import (
    _SPAWN_ACK_MSG,
    _SPAWN_ACK_TOOL_DIRECTIVE,
    RouterLoop,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests.test_router_loop import (
    FakeRouterHost,
    _ScriptedLLM,
    text_result,
    tool_result,
)

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


# ---------------------------------------------------------------------------
# Spy host that records tool_result mutations (= _post_text injection)
# ---------------------------------------------------------------------------


class _MessageCapturingScripted:
    """Captures the messages list per LLM call so the test can inspect
    what the LLM saw on its second turn (= post-spawn-ack continuation)."""

    def __init__(self, script: list[LLMToolCallResult]):
        self._script = list(script)
        self.call_count = 0
        self.messages_per_call: list[list[dict]] = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        msgs = kwargs.get("messages") or []
        self.messages_per_call.append([dict(m) for m in msgs])
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def _make_spawn_ack_host() -> FakeRouterHost:
    """Build a host whose invoke_skill returns a spawn-ack on call."""
    host = FakeRouterHost(skills=[{"name": "some_skill", "category": "general"}])

    async def fake_spawn(**kwargs):
        return {
            "status": "spawned",
            "run_id": "20260520T000000Z_some_skill_aaaa",
            "chain_id": kwargs.get("chain_id", ""),
            "skill": kwargs.get("skill", ""),
            "note": "Running in the background.",
        }

    host.run_skill_awaitable = fake_spawn
    return host


def _make_loop_with_llm_caller(host: FakeRouterHost, llm_caller) -> RouterLoop:
    return RouterLoop(
        host=host, chain_id="chain-spawn-ack-test",
        max_iterations=5, llm_caller=llm_caller,
    )


# ---------------------------------------------------------------------------
# Directive constant shape
# ---------------------------------------------------------------------------


def test_spawn_ack_tool_directive_is_non_empty_string():
    """Tier 2: directive must be a non-empty string."""
    assert isinstance(_SPAWN_ACK_TOOL_DIRECTIVE, str)
    assert _SPAWN_ACK_TOOL_DIRECTIVE.strip()


def test_spawn_ack_tool_directive_signals_spawn_context_and_h3_defense():
    """Tier 2: directive content must signal both the spawn context
    (= "skill" / "spawned") AND the H3 hallucination defense
    (= "do not include" + "skill output"). NF-W7-B43-2: N=10 variant
    ablation showed positive framing + skill-output exclusion is what
    flips ACK rate from 0-10% to 70% while keeping HALLUCINATE at 0%.
    """
    directive = _SPAWN_ACK_TOOL_DIRECTIVE.lower()
    assert "skill" in directive
    assert "spawn" in directive or "running" in directive or "background" in directive
    assert "do not include" in directive or "do not predict" in directive
    assert "skill output" in directive or "the result" in directive


def test_spawn_ack_tool_directive_distinct_from_outbox_message():
    """Tier 2: directive is for LLM consumption in tool_result; it
    differs from ``_SPAWN_ACK_MSG`` (= the user-visible deterministic
    text used in the default outbox-push path). The two constants
    must NOT alias to the same string — they live in different
    channels with different audiences.
    """
    assert _SPAWN_ACK_TOOL_DIRECTIVE != _SPAWN_ACK_MSG["en"]
    assert _SPAWN_ACK_TOOL_DIRECTIVE != _SPAWN_ACK_MSG["ja"]


# ---------------------------------------------------------------------------
# Env-gated behaviour: opt-in path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_ack_to_llm_env_on_continues_loop_with_directive(monkeypatch):
    """Tier 2: when ``REYN_SPAWN_ACK_TO_LLM=1``, the router does NOT
    push ``_SPAWN_ACK_MSG`` to outbox + does NOT early-exit. NF-W7-B43-2:
    the spawn-ack tool_result is annotated with the directive and the
    loop continues to the next LLM call.

    Pins:
      - LLM called exactly TWICE (= round 1 spawn-ack + round 2
        LLM-composed reply); default path would be 1 call.
      - Outbox final message is the LLM-composed text (= "ack reply"
        from round 2), NOT the OS-synthetic ``_SPAWN_ACK_MSG``.
      - The ``meta.source`` on the outbox message is NOT "spawn_ack"
        (= it's a regular LLM agent reply now).
      - The round-2 LLM call's tool message includes
        ``_SPAWN_ACK_TOOL_DIRECTIVE`` text in its content (= injected
        via ``_post_text`` after the JSON body).
    """
    monkeypatch.setenv("REYN_SPAWN_ACK_TO_LLM", "1")
    host = _make_spawn_ack_host()
    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
        text_result("Skill started; awaiting result."),
    ]
    spy = _MessageCapturingScripted(rounds)
    loop = _make_loop_with_llm_caller(host, spy)

    await loop.run("run skill", [])

    assert spy.call_count == 2, (
        f"Expected 2 LLM calls in opt-in path (round 1 spawn-ack + "
        f"round 2 LLM-composed reply); got {spy.call_count}"
    )
    # Round-2 messages must contain the directive (appended via
    # ``_post_text`` outside the tool result's JSON body).
    round2_msgs = spy.messages_per_call[1]
    tool_msgs = [m for m in round2_msgs if m.get("role") == "tool"]
    assert tool_msgs, "round 2 must carry the spawn-ack tool_result"
    tool_content = tool_msgs[0].get("content", "")
    assert _SPAWN_ACK_TOOL_DIRECTIVE in tool_content, (
        "tool_result content must include the directive via _post_text"
    )
    # Outbox: only the LLM-composed reply, no OS-synthetic spawn-ack.
    (out,) = host.outbox
    assert out["text"] == "Skill started; awaiting result."
    assert out["meta"].get("source") != "spawn_ack"


@pytest.mark.asyncio
async def test_spawn_ack_to_llm_env_off_keeps_default_outbox_path(monkeypatch):
    """Tier 2: regression guard — without ``REYN_SPAWN_ACK_TO_LLM=1``,
    the default behaviour runs — OS pushes ``_SPAWN_ACK_MSG`` to outbox
    + early-exits. The LLM is called only once (= round 1), no
    directive injection happens.
    """
    monkeypatch.delenv("REYN_SPAWN_ACK_TO_LLM", raising=False)
    host = _make_spawn_ack_host()
    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
    ]
    scripted = _ScriptedLLM(rounds)
    loop = _make_loop_with_llm_caller(host, scripted)

    await loop.run("run skill", [])

    assert scripted.call_count == 1, (
        f"Default path: exactly 1 LLM call (early-exit on spawn-ack); "
        f"got {scripted.call_count}"
    )
    # Outbox holds the OS-synthetic spawn-ack with meta.source="spawn_ack".
    # B49 W1-S6 follow-up: the spawn-ack text now carries a structured
    # ``[task_spawned] kind=skill ...`` header so the LLM can correlate
    # with the later ``[task_completed]`` injection. The user-friendly
    # trailer (= _SPAWN_ACK_MSG) is preserved as the second paragraph.
    (out,) = host.outbox
    assert out["meta"].get("source") == "spawn_ack"
    assert "[task_spawned] kind=skill" in out["text"], (
        f"spawn-ack must carry the structured header for correlation; "
        f"got {out['text']!r}"
    )
    assert _SPAWN_ACK_MSG["en"] in out["text"], (
        f"spawn-ack must preserve the user-friendly trailer; got "
        f"{out['text']!r}"
    )


@pytest.mark.asyncio
async def test_invoke_skill_spawn_ack_exit_event_fires_in_both_paths(monkeypatch):
    """Tier 2: P6 audit guard — ``invoke_skill_spawn_ack_exit`` event
    must fire regardless of env var state. Audit trail is invariant
    across the path switch.
    """
    # Path 1: opt-in
    monkeypatch.setenv("REYN_SPAWN_ACK_TO_LLM", "1")
    host1 = _make_spawn_ack_host()
    rounds1 = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
        text_result("ack"),
    ]
    loop1 = _make_loop_with_llm_caller(host1, _ScriptedLLM(rounds1))
    await loop1.run("run skill", [])
    events1 = [e for e in host1._events.emitted if e["type"] == "invoke_skill_spawn_ack_exit"]
    (only1,) = events1  # opt-in path must emit the audit event

    # Path 2: default
    monkeypatch.delenv("REYN_SPAWN_ACK_TO_LLM", raising=False)
    host2 = _make_spawn_ack_host()
    rounds2 = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
    ]
    loop2 = _make_loop_with_llm_caller(host2, _ScriptedLLM(rounds2))
    await loop2.run("run skill", [])
    events2 = [e for e in host2._events.emitted if e["type"] == "invoke_skill_spawn_ack_exit"]
    (only2,) = events2  # default path must also emit the audit event


@pytest.mark.asyncio
async def test_spawn_ack_directive_not_injected_in_default_path(monkeypatch):
    """Tier 2: regression guard — default path does NOT inject the
    directive into tool_result content. Without env opt-in, the
    spawn-ack tool message stays in the original shape that
    downstream OS / history persistence has always expected.
    """
    monkeypatch.delenv("REYN_SPAWN_ACK_TO_LLM", raising=False)
    host = _make_spawn_ack_host()
    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
    ]
    scripted = _ScriptedLLM(rounds)
    loop = _make_loop_with_llm_caller(host, scripted)
    await loop.run("run skill", [])

    # Default path's only outbox message is OS-synthetic — directive
    # text doesn't show up anywhere user-visible.
    assert all(_SPAWN_ACK_TOOL_DIRECTIVE not in m["text"] for m in host.outbox)
