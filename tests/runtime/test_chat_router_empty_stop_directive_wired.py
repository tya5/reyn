"""Tier 2: Session wires the empty-stop retry into the chat router's
RouterLoop — the SHARED uniform ``"resume"`` directive is always threaded;
the actual retry SWITCH is config-driven (#4677, owner default ``False``
since 2026-08-14 — was hardcoded ``True``/always-on, #187).

Pinned invariants:

- ``Session._handle_user_message`` constructs ``RouterLoop`` with
  ``empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE`` (the single shared
  "resume" directive — NOT a chat-specific string) UNCONDITIONALLY — the
  directive is inert unless ``empty_stop_retry_auto`` is also True, so
  threading it costs nothing and a config-driven re-enable needs no other
  wiring change.
- ``empty_stop_retry_auto`` reflects ``chat.empty_stop_retry`` — ``False``
  by default (a session built with no override), ``True`` when the
  operator/test explicitly turns it on. Pinned via a real
  ``CapturingRouterLoop`` subclass injected through ``pytest.monkeypatch``
  (= module-attribute setup, not fake-collaborator mocking per
  testing.ja.md).

History (#187 owner decision, 2026-06-07): the previous chat-specific directive
(B43-NF-W6-1: "Now write your reply to the user … Do not call another tool.")
was RETIRED. It was unevidenced per-site differentiation, and its anti-invoke
framing ("do not call another tool") was itself suspect — chat models also
tool-call. All sites (chat / plan-step / agent op-loop) now use the single
content-neutral ``EMPTY_STOP_RETRY_DIRECTIVE`` = "resume". The cross-site
uniform-DIRECTIVE-wiring invariant is pinned in
``test_empty_stop_retry_uniform_187``; this file pins the chat site's
directive wiring AND the config-driven auto-flag, behaviourally (driving
the real Session construction).

#4677 (owner, 2026-08-14, "resume 注入をデフォルト off にしてくれないかな"):
after an incident where 30 empty-response detections in one ``reyn-self``
run each cost a second LLM call, the retry's auto-fire switch moved from
hardcoded ``True`` to ``chat.empty_stop_retry`` (default ``False``). This
does NOT mean empty responses stopped happening — their root cause is
still unmeasured (#3698's anyio cancel-scope is a candidate) — only that
a detected empty response no longer automatically costs a second LLM
call unless the operator opts back in (the retry still helps recovery in
some environments, per architect's own Trace-patch-replay measurement:
0/10 → 10/10 narration recovery on a specific case — see #4677).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime import router_loop as rl
from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


class _ScriptedLLM:
    """Real-fake callable matching the ``call_llm_tools`` signature."""

    def __init__(self, script: list[LLMToolCallResult]):
        self._script = list(script)
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def _make_session(tmp_path: Path, *, empty_stop_retry: bool = False) -> Session:
    return make_session(agent_name="test_agent_b44", empty_stop_retry=empty_stop_retry)


# ---------------------------------------------------------------------------
# Wiring pin — Session constructs RouterLoop with the shared directive
# ---------------------------------------------------------------------------


class _CapturingRouterLoop(rl.RouterLoop):
    """Real subclass that records the kwargs every RouterLoop construction
    receives. Used in place of the production RouterLoop via
    ``monkeypatch.setattr`` so the test observes Session's construction
    call without mocking the type contract."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any):
        _CapturingRouterLoop.last_kwargs = dict(kwargs)
        super().__init__(**kwargs)


async def _run_and_capture(monkeypatch, tmp_path: Path, *, empty_stop_retry: bool) -> dict:
    monkeypatch.chdir(tmp_path)
    # session._handle_user_message does ``from reyn.runtime.router_loop import
    # RouterLoop`` inside the function, so the module-level patch is observed.
    monkeypatch.setattr(rl, "RouterLoop", _CapturingRouterLoop)
    scripted = _ScriptedLLM([_text_result("ok")])
    monkeypatch.setattr(rl, "call_llm_tools", scripted)

    session = _make_session(tmp_path, empty_stop_retry=empty_stop_retry)

    await session._handle_inbox_text("hello", chain_id="chain-test-b44")
    return _CapturingRouterLoop.last_kwargs


@pytest.mark.asyncio
async def test_chat_session_always_passes_the_shared_resume_directive(monkeypatch, tmp_path):
    """Tier 2c: the shared ``EMPTY_STOP_RETRY_DIRECTIVE`` is threaded
    regardless of whether the auto-retry switch is on — the directive is
    inert unless ``empty_stop_retry_auto`` is also True (#4677), so it
    costs nothing to always pass it and lets a config-driven re-enable
    skip any other wiring change."""
    captured = await _run_and_capture(monkeypatch, tmp_path, empty_stop_retry=False)
    assert captured.get("empty_stop_retry_directive") == EMPTY_STOP_RETRY_DIRECTIVE, (
        "Session must pass the shared EMPTY_STOP_RETRY_DIRECTIVE, not an "
        "inlined or chat-specific string. Got: "
        + repr(captured.get("empty_stop_retry_directive"))
    )


@pytest.mark.asyncio
async def test_chat_session_defaults_empty_stop_retry_auto_to_false(monkeypatch, tmp_path):
    """Tier 2c: #4677 — a session built with no override (the operator's
    real default) passes ``empty_stop_retry_auto=False``. Before this PR
    Session always passed ``True`` regardless of any config; this is the
    owner-directed default flip (2026-08-14)."""
    captured = await _run_and_capture(monkeypatch, tmp_path, empty_stop_retry=False)
    assert captured.get("empty_stop_retry_auto") is False, (
        "Session's default (no chat.empty_stop_retry override) must be "
        "False (#4677, owner default since 2026-08-14). Got kwargs: "
        + str(sorted(captured))
    )


@pytest.mark.asyncio
async def test_chat_session_honours_an_explicit_empty_stop_retry_opt_in(monkeypatch, tmp_path):
    """Tier 2c: #4677's own required condition ② — an operator (or an
    environment relying on the retry's measured narration-recovery
    benefit, e.g. a weak model) MUST be able to turn it back on via
    config. Losing this path would be a real regression for whoever
    depends on it, not just a config-plumbing nicety."""
    captured = await _run_and_capture(monkeypatch, tmp_path, empty_stop_retry=True)
    assert captured.get("empty_stop_retry_auto") is True, (
        "chat.empty_stop_retry=True must still reach RouterLoop as "
        "empty_stop_retry_auto=True — the config knob must actually turn "
        "the retry back on. Got kwargs: " + str(sorted(captured))
    )
