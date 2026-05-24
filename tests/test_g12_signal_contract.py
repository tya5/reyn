"""Tier 2: G12 (answered)-signal contract — issue #156 fix invariant.

Pins the contract-correct placement of the G12 neutral signal: instead of
appending a `{"role": "user", "content": "(answered)"}` message (= role
contract violation that triggers weak-model canned-reply attractor at
100% rate in polluted-history post-tool turns), the signal is embedded
INSIDE the trailing role=tool message content.

No mocks. Tests the pure helper `_apply_g12_signal` directly with real
inputs.

Regression source: 2026-05-18 issue #156, measurement comment
https://github.com/tya5/reyn/issues/156#issuecomment-4472779626.
"""
from __future__ import annotations

import json

import pytest

from reyn.llm.llm import _apply_g12_signal

# ── 1. No-op gate (= signal must NOT fire on non-post-tool turns) ─────────


def test_non_post_tool_turn_no_op() -> None:
    """Tier 2: when last message is not role=tool, no signal is injected.

    The signal applies only on post-tool turns. If this gate breaks, user
    turns (role=user) would get the signal embedded — which is the
    contract violation issue #156 fixed by relocating to role=tool.
    """
    # role=user case
    user_msgs = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "hello"},
    ]
    assert _apply_g12_signal(user_msgs) == user_msgs

    # role=assistant case (= mid-tool-call shape)
    asst_msgs = [
        {"role": "user", "content": "do X"},
        {"role": "assistant", "tool_calls": [{"id": "abc"}]},
    ]
    assert _apply_g12_signal(asst_msgs) == asst_msgs


# ── 2. JSON-shaped tool content (= dominant production path) ────────────────


def test_json_tool_content_gets_top_level_signal_field() -> None:
    """Tier 2: when tool content is JSON-shaped, the signal is injected as
    a top-level field after the opening brace.

    Parses to a valid JSON object. Original fields are preserved.
    """
    msgs = [
        {"role": "user", "content": "summarize foo"},
        {"role": "assistant", "tool_calls": [{"id": "abc"}]},
        {"role": "tool", "content": '{"status": "ok", "data": {"path": "foo.md"}}'},
    ]
    result = _apply_g12_signal(msgs)

    # Identity + structural assertions
    assert result is not msgs, "must return a new list, not mutate in-place"
    (_, _, _) = result
    assert result[0] is msgs[0] and result[1] is msgs[1], (
        "non-trailing messages must be the same references (= no copies)"
    )

    # The trailing tool message has the signal injected, parses as valid JSON
    new_tool = result[-1]
    assert new_tool["role"] == "tool"
    parsed = json.loads(new_tool["content"])
    assert parsed["_g12_signal"].startswith("(answered)")
    assert "task complete" in parsed["_g12_signal"]
    assert parsed["status"] == "ok"
    assert parsed["data"] == {"path": "foo.md"}


def test_json_tool_content_no_role_user_appended() -> None:
    """Tier 2: V1-INNER must NOT append a role=user "(answered)" message.

    This is the core contract-violation regression guard. The prior shape
    appended such a message and triggered weak-model canned-reply
    attractor (issue #156, 10/10 reproduction).
    """
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "tool_calls": [{"id": "abc"}]},
        {"role": "tool", "content": '{"a": 1}'},
    ]
    result = _apply_g12_signal(msgs)
    # No new user message exists with content "(answered)"
    user_answered = [
        m for m in result
        if m.get("role") == "user" and m.get("content") == "(answered)"
    ]
    assert user_answered == [], (
        f"Found {len(user_answered)} role=user '(answered)' message(s). "
        "V1-INNER must embed the signal INSIDE the role=tool message, "
        "not append a fake user message. This is the issue #156 regression "
        "guard."
    )


def test_json_tool_content_does_not_mutate_input() -> None:
    """Tier 2: the input messages list and its trailing message dict are
    not mutated. Caller-side references remain valid.
    """
    original_tool_content = '{"a": 1, "b": 2}'
    msgs = [
        {"role": "tool", "content": original_tool_content},
    ]
    _apply_g12_signal(msgs)
    assert msgs[-1]["content"] == original_tool_content, (
        "must not mutate the original message dict's content field"
    )


# ── 3. Non-JSON tool content (= defensive fallback) ─────────────────────────


def test_plain_text_tool_content_gets_prefix() -> None:
    """Tier 2: when tool content is non-JSON, the signal is prefixed
    so it precedes the substantive content.
    """
    msgs = [
        {"role": "tool", "content": "plain text result"},
    ]
    result = _apply_g12_signal(msgs)
    new_content = result[-1]["content"]
    assert new_content.startswith("(answered)")
    assert new_content.endswith("plain text result")


# ── 4. Non-string content (= future content-parts API) ──────────────────────


def test_non_string_content_returns_unchanged() -> None:
    """Tier 2: non-string tool content (= list of content parts or None)
    is a no-op rather than risk corrupting the structured data. This
    keeps the helper future-safe for Anthropic-style content parts.
    """
    msgs = [{"role": "tool", "content": [{"type": "text", "text": "result"}]}]
    assert _apply_g12_signal(msgs) == msgs


# ── 5. Empty JSON object — must produce parse-valid output ─────────────────


def test_empty_json_object_produces_valid_json() -> None:
    """Tier 2: empty `{}` tool content must NOT yield invalid JSON
    (= trailing comma). Empty object shapes get the signal as the
    sole field, no separator comma.
    """
    msgs = [{"role": "tool", "content": "{}"}]
    result = _apply_g12_signal(msgs)
    new_content = result[-1]["content"]
    # Must parse cleanly
    parsed = json.loads(new_content)
    assert parsed["_g12_signal"].startswith("(answered)")
    # No other fields (= the only key is the signal)
    assert list(parsed.keys()) == ["_g12_signal"]


def test_empty_json_object_with_whitespace_produces_valid_json() -> None:
    """Tier 2: `{ }` with internal whitespace still produces valid JSON.

    Edge case — defensive against tool dispatchers that pretty-print
    empty objects.
    """
    msgs = [{"role": "tool", "content": "{ }"}]
    result = _apply_g12_signal(msgs)
    json.loads(result[-1]["content"])  # raises if invalid


# ── 6. Env var disable (= operator opt-out) ─────────────────────────────────


def test_env_var_off_returns_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: REYN_G12_SIGNAL=off disables the workaround entirely.

    Operator opt-out for diagnostic / A/B comparison. When set, the
    helper is a no-op even on post-tool turns.
    """
    monkeypatch.setenv("REYN_G12_SIGNAL", "off")
    msgs = [{"role": "tool", "content": '{"a": 1}'}]
    result = _apply_g12_signal(msgs)
    assert result is msgs, "REYN_G12_SIGNAL=off must short-circuit (no copy)"
    assert msgs[-1]["content"] == '{"a": 1}', "content must be unmodified"


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "False", "no", "NO"])
def test_env_var_off_case_and_alias_variants(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Tier 2: case-insensitive disable values + common aliases all work."""
    monkeypatch.setenv("REYN_G12_SIGNAL", value)
    msgs = [{"role": "tool", "content": '{"a": 1}'}]
    assert _apply_g12_signal(msgs) is msgs


@pytest.mark.parametrize("value", ["", "on", "1", "true", "garbage"])
def test_env_var_non_disable_values_leave_workaround_active(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Tier 2: any non-disable value (= unset, "on", garbage) leaves the
    workaround active. Default state is on; disable is opt-in.
    """
    monkeypatch.setenv("REYN_G12_SIGNAL", value)
    msgs = [{"role": "tool", "content": '{"a": 1}'}]
    result = _apply_g12_signal(msgs)
    assert result is not msgs, f"value={value!r} should leave workaround active"
    parsed = json.loads(result[-1]["content"])
    assert "_g12_signal" in parsed


# ── 7. Multi-turn realistic shape ───────────────────────────────────────────


def test_realistic_polluted_history_post_tool_shape() -> None:
    """Tier 2: realistic snowball + post-tool shape from the issue #156
    tui-coder baseline. Only the trailing role=tool gets modified.
    Prior assistant messages that mention `(answered)` are left intact
    (= the helper does not scrub history; that is a separate concern).
    """
    msgs = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "previous turn"},
        {"role": "assistant", "content": "It looks like you've pasted (answered) again..."},  # snowball
        {"role": "user", "content": "summarize readme.md"},
        {"role": "assistant", "tool_calls": [{"id": "xyz"}]},
        {"role": "tool", "content": '{"status": "ok", "data": {"content": "# Reyn..."}}'},
    ]
    result = _apply_g12_signal(msgs)

    # Trailing tool message got the signal field
    parsed = json.loads(result[-1]["content"])
    assert parsed["_g12_signal"].startswith("(answered)")
    assert "task complete" in parsed["_g12_signal"]
    assert parsed["status"] == "ok"

    # Prior snowball-style assistant message is left untouched (= helper
    # has a single responsibility: place the signal, do not scrub history).
    assert "(answered)" in result[2]["content"]
    assert result[2] is msgs[2], "non-trailing messages must be same references"

    # No role=user "(answered)" message exists
    user_answered = [m for m in result if m.get("role") == "user" and m.get("content") == "(answered)"]
    assert user_answered == []
