"""Tier 2: OS invariant — chat router i18n for fallback messages (F8 + F11).

F8: when the per-turn router retry budget is exhausted, the user-facing fallback
    message must be in the configured output_language, not hardcoded English.

F11: the system prompt built by router_system_prompt must include an explicit
    language instruction matching the configured output_language, so LLM-generated
    clarifying questions and direct replies land in the right language.

Policy: no MagicMock / AsyncMock on collaborators. Real ChatSession and
build_system_prompt instances. RouterLoop.run() is patched only where strictly
necessary to avoid network calls (Tier 3 LLM-replay tests are separate).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.session import (
    ChatSession,
    _ROUTER_RETRY_EXHAUSTED_MSG,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    tmp_path: Path,
    *,
    cap: int = 3,
    output_language: str | None = "ja",
) -> ChatSession:
    """Minimal ChatSession with a real BudgetTracker and configurable language.

    output_language=None tests the unset-user behavior (= no language
    directive in router prompt; fallback messages use English).
    """
    cost = CostConfig(router_invocations_per_turn=cap)
    bt = BudgetTracker(cost)
    return ChatSession(
        agent_name="test_agent",
        output_language=output_language,
        budget_tracker=bt,
    )


def _drain_outbox(session: ChatSession) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _run(coro):
    return asyncio.run(coro)


_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


# ---------------------------------------------------------------------------
# F8 tests — retry-exhausted fallback message language
# ---------------------------------------------------------------------------

def test_retry_exhausted_fallback_is_japanese_when_output_language_ja(
    tmp_path, monkeypatch
):
    """Tier 2: when output_language=ja and the router cap is exhausted,
    the user-facing agent outbox message contains Japanese (F8).

    The fallback text must come from _ROUTER_RETRY_EXHAUSTED_MSG["ja"],
    not from the hardcoded English string.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3, output_language="ja")

    # Pre-spend the budget and suppress the reset so the very first
    # _run_router_loop attempt inside _handle_user_message is rejected.
    monkeypatch.setattr(ChatSession, "_reset_router_turn_counter", lambda self: None)
    session._router_invocations_this_turn = 3
    session._router_last_reason = "out_of_scope"

    _run(session._handle_user_message("こんにちは", chain_id="chain-ja"))

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert agent_msgs, "Expected at least one agent outbox message"

    fallback_text = agent_msgs[0].text
    # Must contain Japanese-specific marker from _ROUTER_RETRY_EXHAUSTED_MSG["ja"]
    assert "router 予算" in fallback_text, (
        f"Expected Japanese fallback but got: {fallback_text!r}"
    )
    assert "I couldn't find a way" not in fallback_text, (
        f"Hardcoded English found in ja fallback: {fallback_text!r}"
    )


def test_retry_exhausted_fallback_is_english_when_output_language_en(
    tmp_path, monkeypatch
):
    """Tier 2: when output_language=en and the router cap is exhausted,
    the fallback message contains English (F8 — default language path).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3, output_language="en")

    monkeypatch.setattr(ChatSession, "_reset_router_turn_counter", lambda self: None)
    session._router_invocations_this_turn = 3
    session._router_last_reason = "test_reason"

    _run(session._handle_user_message("hello", chain_id="chain-en"))

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert agent_msgs, "Expected at least one agent outbox message"

    fallback_text = agent_msgs[0].text
    assert "I couldn't find a way" in fallback_text, (
        f"Expected English fallback but got: {fallback_text!r}"
    )


def test_retry_exhausted_fallback_defaults_to_english_for_unsupported_language(
    tmp_path, monkeypatch
):
    """Tier 2: when output_language is an unsupported code (e.g. "fr"),
    the fallback message falls back to English without raising (F8).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3, output_language="fr")

    monkeypatch.setattr(ChatSession, "_reset_router_turn_counter", lambda self: None)
    session._router_invocations_this_turn = 3
    session._router_last_reason = ""

    # Must not raise.
    _run(session._handle_user_message("bonjour", chain_id="chain-fr"))

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert agent_msgs, "Expected at least one agent outbox message"

    fallback_text = agent_msgs[0].text
    # Falls back to English (the safe default for unknown languages).
    assert "I couldn't find a way" in fallback_text, (
        f"Expected English fallback for unsupported lang but got: {fallback_text!r}"
    )


# ---------------------------------------------------------------------------
# F11 tests — system prompt language instruction
# ---------------------------------------------------------------------------

def test_system_prompt_contains_explicit_ja_instruction():
    """Tier 2: when output_language=ja, build_system_prompt includes an
    explicit instruction to reply in Japanese (F11).

    The instruction must be stronger than the generic 'match the user's
    language' line — it must name the language code 'ja' explicitly so
    LLM-generated clarifying questions land in Japanese.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        output_language="ja",
    )
    assert "language: ja" in prompt, (
        f"Expected explicit 'language: ja' instruction in system prompt.\n"
        f"Prompt excerpt (Behaviour section):\n"
        + "\n".join(l for l in prompt.splitlines() if "Behaviour" in l or "language" in l.lower())
    )


def test_system_prompt_contains_explicit_en_instruction():
    """Tier 2: when output_language=en, build_system_prompt includes 'language: en'
    in the Behaviour section (F11 — symmetric check for English).
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        output_language="en",
    )
    assert "language: en" in prompt, (
        f"Expected 'language: en' in system prompt but got:\n"
        + "\n".join(l for l in prompt.splitlines() if "Behaviour" in l or "language" in l.lower())
    )


def test_system_prompt_omits_language_directive_when_output_language_is_none():
    """Tier 2: when output_language is None (= user did not configure),
    build_system_prompt does NOT emit any 'Always reply in language: <code>'
    directive. The LLM picks the reply language based on user input
    naturally instead of being forced into a Reyn default. (Q2 follow-up.)
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        output_language=None,
    )
    # No "Always reply in language" line at all.
    assert "Always reply in language" not in prompt, (
        f"Expected no language directive when output_language=None, "
        f"but got:\n"
        + "\n".join(l for l in prompt.splitlines() if "language" in l.lower())
    )
    # Behaviour section still exists (just without the language line).
    assert "## Behaviour" in prompt


def test_system_prompt_default_output_language_is_none():
    """Tier 2: build_system_prompt defaults to None when output_language is
    omitted — same as explicit None (= no language directive). Prior behavior
    forced 'language: en' as a hardcoded default; that masked the unset-user
    case, so we now treat omitted == None.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "not_found", "content": ""},
        # output_language intentionally omitted → defaults to None
    )
    assert "Always reply in language" not in prompt


def test_router_loop_passes_output_language_to_system_prompt(tmp_path, monkeypatch):
    """Tier 2: RouterLoop.run() passes the host's output_language to
    build_system_prompt so the LLM receives the correct language instruction
    (F11 — integration path: ChatSession → RouterLoop → build_system_prompt).

    We stub call_llm_tools to avoid network I/O and capture the system prompt
    that RouterLoop would send.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3, output_language="ja")
    session.is_attached = True

    captured_prompts: list[str] = []

    async def fake_llm_tools(*, model, messages, tools, tool_choice,
                              skill_name, budget, budget_agent):
        # Capture the system message (first in messages list).
        for msg in messages:
            if msg.get("role") == "system":
                captured_prompts.append(msg["content"])
        return _text_result("テスト応答")

    async def run():
        with patch("reyn.chat.router_loop.call_llm_tools", side_effect=fake_llm_tools):
            await session._handle_user_message("こんにちは", chain_id="chain-prompt")

    _run(run())

    assert captured_prompts, "No system prompt was captured — LLM was never called"
    system_prompt = captured_prompts[0]
    assert "language: ja" in system_prompt, (
        f"Router did not pass output_language=ja to system prompt.\n"
        f"Prompt lines with 'language':\n"
        + "\n".join(l for l in system_prompt.splitlines() if "language" in l.lower())
    )


# ---------------------------------------------------------------------------
# Q2: Optional[str] propagation — unset = no directive, no regional default
# ---------------------------------------------------------------------------


def test_config_output_language_default_is_none(tmp_path, monkeypatch):
    """Tier 2: when no config file sets output_language, ReynConfig loads
    with output_language=None (= unset). Reyn explicitly does not silently
    default to a regional language; the chat router treats None as "no
    directive, LLM picks based on user input".
    """
    from reyn.config import load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))  # avoid reading ~/.reyn

    cfg = load_config(cwd=tmp_path)
    assert cfg.output_language is None


def test_config_output_language_explicit_value_preserved(tmp_path, monkeypatch):
    """Tier 2: when reyn.yaml sets output_language=ja, ReynConfig
    preserves the explicit value (= no normalization to None for set
    values; only the unset case yields None).
    """
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text("output_language: ja\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = load_config(cwd=tmp_path)
    assert cfg.output_language == "ja"


def test_config_output_language_empty_string_treated_as_unset(
    tmp_path, monkeypatch
):
    """Tier 2: yaml `output_language: ""` (= explicit empty) is treated as
    unset (= None). Allows users to opt out of any language pinning even
    when a project-level reyn.yaml sets a value, by overriding to "" in
    reyn.local.yaml.
    """
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text("output_language: ja\n", encoding="utf-8")
    (tmp_path / "reyn.local.yaml").write_text(
        'output_language: ""\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = load_config(cwd=tmp_path)
    assert cfg.output_language is None


def test_retry_exhausted_fallback_is_english_when_output_language_is_none(
    tmp_path, monkeypatch
):
    """Tier 2: when output_language is None (= user did not configure),
    the router retry-exhausted fallback message is English (= the safe
    global default for an internal error string when the user has not
    expressed a language preference). Regional languages like ja are
    NOT silently chosen as fallback.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path, cap=3, output_language=None)
    session.is_attached = True

    delivered: list[dict] = []

    async def fake_put_outbox(msg):
        delivered.append({
            "kind": msg.kind,
            "text": msg.text,
            "meta": msg.meta,
        })

    session._put_outbox = fake_put_outbox  # type: ignore[assignment]

    from reyn.chat.session import RouterCapExceeded

    async def run():
        await session._emit_router_cap_exhausted_user(
            RouterCapExceeded(count=3, cap=3, last_reason="loop"),
            chain_id="chain-test",
        )

    _run(run())

    # The agent reply should be the English fallback, not the Japanese one.
    agent_msgs = [m for m in delivered if m["kind"] == "agent"]
    assert agent_msgs, f"no agent reply emitted; got: {delivered}"
    assert agent_msgs[0]["text"] == _ROUTER_RETRY_EXHAUSTED_MSG["en"], (
        f"Expected English fallback when output_language=None; got: "
        f"{agent_msgs[0]['text']!r}"
    )
    # Specifically NOT the ja message.
    assert agent_msgs[0]["text"] != _ROUTER_RETRY_EXHAUSTED_MSG["ja"]
