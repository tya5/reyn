"""Unit tests for LLMReplay cache-key computation (PR35 Wave 1 Task E).

Covers:
- Tools/tool_choice in key → different tools produce different keys.
- Same tools/tool_choice → same key (idempotent).
- Backward compat: no tools → legacy key format, stable across runs.
- tool_choice alone (same tools, different tool_choice) → distinct keys.

No real LLM is called; all tests exercise key() directly.
"""
from __future__ import annotations

from reyn.dev.testing.replay import LLMReplay

# ── helpers ────────────────────────────────────────────────────────────────────

_MESSAGES = [{"role": "user", "content": "Hello"}]
_MODEL = "openai/gemini-2.5-flash-lite"

_TOOLS_A = [
    {
        "type": "function",
        "function": {"name": "invoke_skill", "description": "Run a skill", "parameters": {}},
    }
]
_TOOLS_B = [
    {
        "type": "function",
        "function": {"name": "list_skills", "description": "List skills", "parameters": {}},
    }
]


# ── tests ──────────────────────────────────────────────────────────────────────


def test_cache_key_differs_when_tools_differ():
    """Tier 3a: Same model + messages + different tools must produce distinct keys."""
    key_a = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_A)
    key_b = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_B)
    assert key_a != key_b, (
        "Cache collision: different tools= arrays produced identical SHA-256 keys"
    )


def test_cache_key_same_when_tools_identical():
    """Tier 3a: Same model + messages + same tools → identical key (idempotent)."""
    key_1 = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_A)
    key_2 = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_A)
    assert key_1 == key_2, (
        "Cache key is not stable: identical tools= arrays produced different keys"
    )


def test_cache_key_handles_no_tools():
    """Tier 3a: Messages without tools= use the legacy key format — stable across runs.

    Backward-compat (Option A): when tools is None/[] and tool_choice is
    None/'', the key must equal the pre-PR35 format
    sha256(model_bytes + messages_json_bytes) — the original byte-concatenation
    used before PR35, without a pipe separator.
    """
    import hashlib
    import json

    messages_json = json.dumps(_MESSAGES, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256()
    h.update(_MODEL.encode())
    h.update(messages_json.encode())
    legacy_key = h.hexdigest()

    # None (absent) — no tools at all.
    assert LLMReplay.key(_MODEL, _MESSAGES) == legacy_key
    # Empty list — semantically identical to "no tools".
    assert LLMReplay.key(_MODEL, _MESSAGES, tools=[]) == legacy_key
    # None tool_choice alongside None tools.
    assert LLMReplay.key(_MODEL, _MESSAGES, tools=None, tool_choice=None) == legacy_key
    # Empty string tool_choice alongside empty tools.
    assert LLMReplay.key(_MODEL, _MESSAGES, tools=[], tool_choice="") == legacy_key


def test_cache_key_tool_choice_affectskey():
    """Tier 3a: Same messages + same tools but different tool_choice → distinct keys."""
    key_auto = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_A, tool_choice="auto")
    key_required = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_A, tool_choice="required")
    assert key_auto != key_required, (
        "Cache collision: 'auto' vs 'required' tool_choice produced identical keys"
    )
