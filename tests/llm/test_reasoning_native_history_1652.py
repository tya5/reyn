"""Tier 2: #1652/② — reasoning capture-as-bundle + canonical modify_params.

Complements the rewritten capture/integration tests (which pin bundle capture +
wire re-attach + bound). Here: the multi-field bundle (reasoning_content +
thinking_blocks), the omit-when-empty None, the bundle helpers (legacy str
absorb + native attach), and that the recorded_acompletion chokepoint enables
litellm's canonical ``modify_params``.

No mocks: real bundle extractor over a plain namespace msg, real helpers, and
a real async fake for litellm.acompletion.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from reyn.llm.llm import _extract_reasoning_bundle, recorded_acompletion
from reyn.runtime.reasoning_continuity import (
    as_reasoning_bundle,
    attach_reasoning,
    reasoning_text,
)


def test_extract_bundle_includes_reasoning_content_and_thinking_blocks() -> None:
    """Tier 2: a msg with both fields → a bundle carrying both (dict-ified)."""
    msg = SimpleNamespace(
        reasoning_content="2+2=4",
        thinking_blocks=[{"type": "thinking", "thinking": "add them"}],
        provider_specific_fields=None,
    )
    assert _extract_reasoning_bundle(msg) == {
        "reasoning_content": "2+2=4",
        "thinking_blocks": [{"type": "thinking", "thinking": "add them"}],
    }


def test_extract_bundle_empty_is_none() -> None:
    """Tier 2: no reasoning fields → None (omit-when-empty / byte-identical)."""
    msg = SimpleNamespace(reasoning_content=None, thinking_blocks=None, provider_specific_fields=None)
    assert _extract_reasoning_bundle(msg) is None
    assert _extract_reasoning_bundle(SimpleNamespace(reasoning_content="")) is None


def test_bundle_helpers_absorb_legacy_str_and_extract_text() -> None:
    """Tier 2: legacy str entries normalize to a bundle; text extraction works."""
    assert as_reasoning_bundle("old-text") == {"reasoning_content": "old-text"}
    assert as_reasoning_bundle({"reasoning_content": "x"}) == {"reasoning_content": "x"}
    assert as_reasoning_bundle(None) is None
    assert as_reasoning_bundle("") is None
    assert reasoning_text("legacy") == "legacy"
    assert reasoning_text({"reasoning_content": "b"}) == "b"
    assert reasoning_text({"thinking_blocks": [1]}) == ""  # no text part


def test_attach_reasoning_carries_all_fields_and_noops_when_empty() -> None:
    """Tier 2: attach copies the bundle fields onto the wire dict; empty = no-op."""
    msg: dict = {"role": "assistant", "content": "a"}
    attach_reasoning(msg, {"reasoning_content": "r", "thinking_blocks": [{"t": 1}]})
    assert msg["reasoning_content"] == "r"
    assert msg["thinking_blocks"] == [{"t": 1}]
    # legacy str
    msg2: dict = {"role": "assistant", "content": "a"}
    attach_reasoning(msg2, "legacy")
    assert msg2["reasoning_content"] == "legacy"
    # empty → byte-identical (no keys added)
    msg3: dict = {"role": "assistant", "content": "a"}
    attach_reasoning(msg3, None)
    assert msg3 == {"role": "assistant", "content": "a"}


@pytest.mark.asyncio
async def test_recorded_acompletion_enables_modify_params(monkeypatch) -> None:
    """Tier 2: the chokepoint sets litellm.modify_params=True (canonical litellm
    mechanism for reasoning continuity across tool turns — not a Reyn hack)."""
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "modify_params", False, raising=False)

    async def _resp(**_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[],
        )

    monkeypatch.setattr(litellm, "acompletion", _resp)
    await recorded_acompletion(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        purpose="main",
        recorder=None,
    )
    assert litellm.modify_params is True
