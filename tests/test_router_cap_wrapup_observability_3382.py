"""Tier 2: #3382 — degrading to the canned router-cap reply is observable.

The per-turn router cap's user-facing close (``session.py`` #1496 site C)
tries an LLM force-close wrap-up and falls back to a canned i18n message
when it cannot produce one. That fallback used to be reached through a
bare ``except Exception: pass``, so three distinct situations — the
wrap-up call failed, no LLM is configured/reachable, the LLM returned no
text — collapsed into one indistinguishable outcome with no record at all.

These tests pin the surviving contract: whenever the canned reply is
emitted, a WARNING names *which* of those happened; when the wrap-up
succeeds, nothing is logged. Both legs use the designed ``_llm_caller``
Tier-2 seam, so neither depends on the environment having credentials.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from reyn.config import LoopConfig, SafetyConfig
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.errors import RouterCapExceeded
from reyn.runtime.session import _ROUTER_RETRY_EXHAUSTED_MSG, Session
from tests._support.agent_session import make_session
from tests._support.router_loop import RaisingLLM, ScriptedLLM, text_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _make_session(tmp_path: Path) -> Session:
    safety = SafetyConfig(loop=LoopConfig(max_router_calls_per_turn=3))
    session = make_session(
        agent_name="test_cap_agent",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )
    return session


def _drain(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name == "reyn.runtime.session"
    ]


def _emit(session: Session, caller) -> None:
    asyncio.run(session._emit_router_cap_exhausted_user(
        RouterCapExceeded(count=3, cap=3, last_reason="loop_reason"),
        chain_id="chain-obs",
        _llm_caller=caller,
    ))


def test_wrap_up_call_failure_names_the_provider_error(
    tmp_path, monkeypatch, caplog
):
    """Tier 2: when the wrap-up call raises, the warning names it as a
    call failure AND carries the provider's own exception type + message,
    which is what distinguishes "no LLM configured/reachable" from any
    other wrap-up failure. Without that, a misconfigured deployment looks
    identical to a healthy one that simply hit its cap.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")

    _emit(session, RaisingLLM(RuntimeError("LLM Provider NOT provided")))

    (warning,) = _warnings(caplog)
    assert "wrap_up_failed" in warning, warning
    assert "RuntimeError" in warning, warning
    assert "LLM Provider NOT provided" in warning, warning
    assert "chain-obs" in warning, warning

    # ... and the user still gets the canned reply (the degrade is
    # logged, not turned into an error).
    agent_msgs = [m for m in _drain(session) if m.kind == "agent"]
    assert agent_msgs[0].text == _ROUTER_RETRY_EXHAUSTED_MSG["en"]


def test_wrap_up_empty_is_named_distinctly_from_a_call_failure(
    tmp_path, monkeypatch, caplog
):
    """Tier 2: an LLM that answers with no text is a different situation
    from an LLM that could not be called, and the warning must say so —
    the two used to be the same silent `pass`.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")

    _emit(session, ScriptedLLM([LLMToolCallResult(
        content=None, tool_calls=[], finish_reason="stop", usage=_USAGE,
    )]))

    (warning,) = _warnings(caplog)
    assert "wrap_up_empty" in warning, warning
    assert "wrap_up_failed" not in warning, (
        f"an empty answer must not be reported as a call failure: {warning}"
    )


def test_successful_wrap_up_logs_no_degrade_warning(
    tmp_path, monkeypatch, caplog
):
    """Tier 2: non-vacuity — the warning is tied to the canned fallback,
    not emitted on every cap-exhausted turn. A warning that always fires
    carries no information about whether the reply was degraded.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")

    _emit(session, ScriptedLLM([text_result("wrapped up cleanly")]))

    assert _warnings(caplog) == []
    agent_msgs = [m for m in _drain(session) if m.kind == "agent"]
    assert agent_msgs[0].text == "wrapped up cleanly"


def test_handle_user_message_reaches_the_degrade_warning(
    tmp_path, monkeypatch, caplog
):
    """Tier 2: production wiring — the warning is reachable from the real
    entry point (``_handle_user_message`` hitting the cap), not only from
    a direct call to the emit helper. The mechanism being correct and
    production reaching it are separate claims; this is the second one.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        RaisingLLM(RuntimeError("boom")),
    )
    monkeypatch.setattr(Session, "_reset_router_turn_counter", lambda self: None)
    session.router_invocations_this_turn = 3
    session._router_last_reason = "out_of_scope"

    asyncio.run(session._handle_inbox_text("hello", chain_id="chain-wired"))

    (warning,) = _warnings(caplog)
    assert "wrap_up_failed" in warning, warning
    assert "chain-wired" in warning, warning
