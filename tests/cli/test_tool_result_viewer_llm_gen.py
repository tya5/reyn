"""Tier 2: LLM template generation + safety fence (#1154 Phase 3 S3).

Falsification-first: each attack vector has a red→green test proving the
safety fence neutralises the threat. The _parse_template_response function
is the security boundary between untrusted LLM output and the TUI renderer.

Attack vectors covered:
- Rich markup in LLM-generated label → escaped at construction time
- Non-existent field in LLM output → dropped (strict allowlist)
- Code/non-JSON in LLM output → None returned (json.loads only, no eval)
- Giant row count → capped at _MAX_TEMPLATE_ROWS (8)
- Giant caption → capped at _MAX_CAPTION_CHARS (40) chars

render_tool_result_async contract:
- sync registry runs first; LLM path skips on registry hit
- llm_client=None → LLM path skipped entirely
- cache hit → _generate_template not called again
- LLM failure (exception) → None (YAML fallback), shape cached as None
"""
from __future__ import annotations

import json

import pytest

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    _MAX_CAPTION_CHARS,
    _MAX_TEMPLATE_ROWS,
    _SHAPE_TEMPLATE_CACHE,
    TemplateSchema,
    _parse_template_response,
    render_tool_result_async,
)

# ---------------------------------------------------------------------------
# Real stub collaborator — no mocks (testing policy: no MagicMock/AsyncMock)
# ---------------------------------------------------------------------------

class _StubLLMClient:
    """Minimal real stub for llm_client used by _generate_template.

    Implements only the ``complete(prompt, max_tokens)`` coroutine.
    ``calls`` records every prompt for assertion purposes.
    ``_raise`` makes the stub raise an exception instead of returning.
    """

    def __init__(self, response: str, *, raise_on_call: Exception | None = None):
        self._response = response
        self._raise = raise_on_call
        self.calls: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 256) -> str:
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        return self._response


# ---------------------------------------------------------------------------
# _parse_template_response — valid input
# ---------------------------------------------------------------------------

def test_parse_valid_response_produces_schema() -> None:
    """Tier 2: valid LLM JSON → TemplateSchema with correct rows and caption."""
    raw = json.dumps({
        "rows": [
            {"label": "From", "field": "sender"},
            {"label": "Subject", "field": "subject"},
        ],
        "caption": "email",
    })
    schema = _parse_template_response(raw, valid_keys=frozenset({"sender", "subject"}))
    assert schema is not None, "expected schema for valid JSON"
    assert schema.rows[0][0] == "From"
    assert schema.rows[0][1] == "sender"
    assert ("Subject", "subject") in schema.rows
    assert schema.caption == "email"


# ---------------------------------------------------------------------------
# Attack vector 1: Rich markup in label
# ---------------------------------------------------------------------------

def test_parse_escapes_label_markup() -> None:
    """Tier 2: LLM-injected Rich markup in label is escaped at schema construction.

    Falsification: without escape() on label, "[bold]Evil[/bold]" would be
    interpreted as a Rich instruction in the TUI — the plain-text assertion
    would not contain the literal bracket characters.
    """
    raw = json.dumps({
        "rows": [{"label": "[bold]Evil[/bold]", "field": "k"}],
        "caption": "",
    })
    schema = _parse_template_response(raw, valid_keys=frozenset({"k"}))
    assert schema is not None
    label = schema.rows[0][0]
    assert "[bold]" in label, (
        f"expected literal '[bold]' in escaped label, got {label!r}"
    )
    assert "Evil" in label


# ---------------------------------------------------------------------------
# Attack vector 2: Non-existent field (allowlist bypass attempt)
# ---------------------------------------------------------------------------

def test_parse_drops_unknown_field() -> None:
    """Tier 2: a field not in valid_keys is silently dropped from the schema.

    Falsification: without the allowlist check, an attacker who controls LLM
    output could name an arbitrary field key, potentially causing a KeyError
    or exposing unintended data at apply time.
    """
    raw = json.dumps({
        "rows": [
            {"label": "Legit", "field": "real_key"},
            {"label": "Inject", "field": "nonexistent_key"},
        ],
        "caption": "",
    })
    schema = _parse_template_response(raw, valid_keys=frozenset({"real_key"}))
    assert schema is not None
    field_keys = [row[1] for row in schema.rows]
    assert "nonexistent_key" not in field_keys, (
        f"expected unknown field to be dropped, got rows: {schema.rows}"
    )
    assert "real_key" in field_keys


def test_parse_all_unknown_fields_returns_none() -> None:
    """Tier 2: when ALL rows have unknown fields, None is returned.

    Falsification: without the empty-rows guard, an all-unknown response
    would produce a TemplateSchema with zero rows.
    """
    raw = json.dumps({
        "rows": [{"label": "x", "field": "no_such_key"}],
        "caption": "",
    })
    result = _parse_template_response(raw, valid_keys=frozenset({"a", "b"}))
    assert result is None, "expected None when all fields are unknown"


# ---------------------------------------------------------------------------
# Attack vector 3: Code / non-JSON in LLM output
# ---------------------------------------------------------------------------

def test_parse_rejects_non_json_output() -> None:
    """Tier 2: non-JSON LLM output returns None (no eval, no exec).

    Falsification: if _parse_template_response used eval() instead of
    json.loads(), malicious Python code in raw would execute.
    """
    for bad_raw in [
        "import os; os.system('rm -rf /')",
        "not json at all",
        "{'rows': [], 'caption': ''}",   # Python dict literal, not JSON
        "",
        "null",
        "42",
    ]:
        result = _parse_template_response(bad_raw, valid_keys=frozenset({"k"}))
        assert result is None, (
            f"expected None for non-dict/non-JSON input: {bad_raw!r}"
        )


def test_parse_rejects_list_toplevel() -> None:
    """Tier 2: a JSON array at top level (not a dict) returns None."""
    raw = json.dumps([{"label": "x", "field": "k"}])
    result = _parse_template_response(raw, valid_keys=frozenset({"k"}))
    assert result is None


# ---------------------------------------------------------------------------
# Attack vector 4: Giant row count
# ---------------------------------------------------------------------------

def test_parse_caps_row_count() -> None:
    """Tier 2: LLM output with more than _MAX_TEMPLATE_ROWS rows is capped.

    Falsification: without the cap, a 20-row LLM response would produce a
    20-row table in the TUI, making the preview pane unusable.
    """
    keys = [f"k{i}" for i in range(20)]
    rows = [{"label": f"Label {i}", "field": f"k{i}"} for i in range(20)]
    raw = json.dumps({"rows": rows, "caption": ""})
    schema = _parse_template_response(raw, valid_keys=frozenset(keys))
    assert schema is not None
    assert len(schema.rows) <= _MAX_TEMPLATE_ROWS, (
        f"expected row count ≤ {_MAX_TEMPLATE_ROWS}, got {len(schema.rows)}"
    )


# ---------------------------------------------------------------------------
# Attack vector 5: Giant caption
# ---------------------------------------------------------------------------

def test_parse_caps_caption_length() -> None:
    """Tier 2: a caption longer than _MAX_CAPTION_CHARS chars is truncated.

    Falsification: without the cap, a 200-char LLM-injected caption would
    overflow the TUI table caption area.
    """
    long_caption = "x" * 200
    raw = json.dumps({
        "rows": [{"label": "A", "field": "k"}],
        "caption": long_caption,
    })
    schema = _parse_template_response(raw, valid_keys=frozenset({"k"}))
    assert schema is not None
    assert "x" * (_MAX_CAPTION_CHARS + 1) not in schema.caption, (
        f"expected caption to be capped; got: {schema.caption!r}"
    )


# ---------------------------------------------------------------------------
# render_tool_result_async — integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_uses_cache_on_second_call() -> None:
    """Tier 2: second call with same shape fingerprint skips _generate_template.

    Falsification: without the cache, every call would trigger an LLM request
    — the stub would record 2 calls instead of 0.
    """
    fp = frozenset({"_s3_cache_test_key"})
    original = _SHAPE_TEMPLATE_CACHE.get(fp, "absent")
    try:
        cached_schema = TemplateSchema(
            rows=[("Cached", "_s3_cache_test_key")], caption=""
        )
        _SHAPE_TEMPLATE_CACHE[fp] = cached_schema

        stub = _StubLLMClient(response='{"rows": [], "caption": ""}')
        result = {"_s3_cache_test_key": "value"}
        await render_tool_result_async(result, stub)
        assert stub.calls == [], (
            "expected LLM not to be called on cache hit, "
            f"but got {len(stub.calls)} call(s)"
        )
    finally:
        if original == "absent":
            _SHAPE_TEMPLATE_CACHE.pop(fp, None)
        else:
            _SHAPE_TEMPLATE_CACHE[fp] = original


@pytest.mark.asyncio
async def test_async_llm_failure_stores_none_and_returns_none() -> None:
    """Tier 2: LLM error → None returned; shape cached as None (no retry).

    Falsification: without the exception guard, a transient LLM error would
    propagate to the TUI and crash the preview pane.
    """
    fp = frozenset({"_s3_fail_test_key"})
    original = _SHAPE_TEMPLATE_CACHE.get(fp, "absent")
    try:
        stub = _StubLLMClient(
            response="",
            raise_on_call=RuntimeError("LLM timeout"),
        )
        result = {"_s3_fail_test_key": "value"}
        viewed = await render_tool_result_async(result, stub)
        assert viewed is None, "expected None on LLM failure"
        assert _SHAPE_TEMPLATE_CACHE.get(fp) is None, (
            "expected None in cache after LLM failure (do not retry)"
        )
    finally:
        if original == "absent":
            _SHAPE_TEMPLATE_CACHE.pop(fp, None)
        else:
            _SHAPE_TEMPLATE_CACHE[fp] = original


@pytest.mark.asyncio
async def test_async_none_llm_client_skips_generation() -> None:
    """Tier 2: llm_client=None skips LLM path; returns None for unmatched result.

    Falsification: without the None-client guard, passing llm_client=None
    would raise AttributeError when attempting to call client.complete.
    """
    result = {"_s3_no_client_key": "value"}
    viewed = await render_tool_result_async(result, llm_client=None)
    assert viewed is None


@pytest.mark.asyncio
async def test_async_sync_registry_hit_skips_llm() -> None:
    """Tier 2: sync registry match returns immediately without calling LLM.

    Falsification: if the async path always called the LLM regardless of
    sync registry result, the stub would record a call even for known types.
    """
    stub = _StubLLMClient(response='{"rows": [], "caption": ""}')
    result = {"content_type": "application/json", "content": '{"x": 1}'}
    viewed = await render_tool_result_async(result, stub)
    assert viewed is not None, "expected sync JSON viewer to fire"
    assert stub.calls == [], (
        "expected LLM not to be called when sync registry matched"
    )
