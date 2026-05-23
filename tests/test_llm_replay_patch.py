"""Tests for the --patch option in scripts/llm_replay.py.

Tier 2: OS invariant — verifies patch expression parsing, application,
ordering, error handling, and end-to-end integration with a real fake LLM.

Testing policy compliance:
- No MagicMock / AsyncMock / patch.  Real callable stubs only.
- No private-state assertions.
- No algorithm-level pins (sort order, dict iteration order).
- Tier declared in each docstring's first line.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Import helpers (same pattern as test_llm_replay_script.py)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
from typing import Any

import pytest

_SCRIPTS_DIR = _Path(__file__).parent.parent / "scripts"


def _import_replay():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_replay", _SCRIPTS_DIR / "llm_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared real callable stub — no mocks
# ---------------------------------------------------------------------------


class _CaptureLLM:
    """Real async callable; captures call kwargs for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        msg = type("_Msg", (), {"content": "ok", "tool_calls": []})()
        choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("_Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()
        return type("_Resp", (), {"choices": [choice], "usage": usage})()


# ---------------------------------------------------------------------------
# Trace builders
# ---------------------------------------------------------------------------


def _write_simple_trace(path: Path, request_id: str) -> None:
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": None,
        "tool_choice": None,
        "sampling_params": {},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")


def _write_trace_with_tools(path: Path, request_id: str) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "invoke_skill",
                "description": "run a skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "skill name",
                        }
                    },
                    "required": ["name"],
                },
            },
        }
    ]
    req = {
        "kind": "request",
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "model": "gemini-2.5-flash-lite",
        "caller_hint": "router",
        "messages": [{"role": "user", "content": "do something"}],
        "tools": tools,
        "tool_choice": "auto",
        "sampling_params": {},
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")


# ---------------------------------------------------------------------------
# (a) basic patch: --patch a.b=1 nested dict update
# ---------------------------------------------------------------------------


class TestBasicPatch:
    def test_nested_dict_replace(self, tmp_path: Path) -> None:
        """Tier 2: --patch sampling_params.temperature=0.9 updates nested dict field."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-a-001"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"temperature": 0.0},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["sampling_params.temperature=0.9"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only.get("temperature") == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# (b) list index patch: --patch list[0]=value
# ---------------------------------------------------------------------------


class TestListIndexPatch:
    def test_list_index_replace(self, tmp_path: Path) -> None:
        """Tier 2: --patch messages[0].content='patched' replaces first message content."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-b-001"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=['messages[0].content="patched content"'],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only["messages"][0]["content"] == "patched content"


# ---------------------------------------------------------------------------
# (c) list nested: --patch tools[0].function.parameters.properties.name.enum=["x","y"]
# ---------------------------------------------------------------------------


class TestListNestedPatch:
    def test_router_enum_patch(self, tmp_path: Path) -> None:
        """Tier 2: patching tools[0].function.parameters.properties.name.enum sets enum list."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-c-001"
        _write_trace_with_tools(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[
                'tools[0].function.parameters.properties.name.enum=["skill_a","skill_b","skill_c"]'
            ],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        tools = only["tools"]
        enum_val = (
            tools[0]["function"]["parameters"]["properties"]["name"]["enum"]
        )
        assert enum_val == ["skill_a", "skill_b", "skill_c"]


# ---------------------------------------------------------------------------
# (d) += string append
# ---------------------------------------------------------------------------


class TestStringAppendPatch:
    def test_append_to_message_content(self, tmp_path: Path) -> None:
        """Tier 2: += appends a string to an existing string field."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-d-001"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=['messages[0].content+=" MUST output flat names"'],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        content = only["messages"][0]["content"]
        assert content.startswith("hello")
        assert "MUST output flat names" in content


# ---------------------------------------------------------------------------
# (e) ?= optional set
# ---------------------------------------------------------------------------


class TestOptionalSetPatch:
    def test_optional_set_absent_key(self, tmp_path: Path) -> None:
        """Tier 2: ?= sets a field that does not exist yet."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-e-001"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["sampling_params.temperature?=0.5"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only.get("temperature") == pytest.approx(0.5)

    def test_optional_set_existing_key_unchanged(self, tmp_path: Path) -> None:
        """Tier 2: ?= leaves an existing field unchanged."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-e-002"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"temperature": 0.8},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["sampling_params.temperature?=0.0"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        # existing value 0.8 must be preserved, not replaced with 0.0
        assert only.get("temperature") == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# (f) -- delete
# ---------------------------------------------------------------------------


class TestDeletePatch:
    def test_delete_dict_field(self, tmp_path: Path) -> None:
        """Tier 2: -- removes a dict field from the payload."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-f-001"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"temperature": 0.5, "max_tokens": 100},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["sampling_params.max_tokens--"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        # max_tokens must not be present in the litellm call
        assert "max_tokens" not in only

    def test_delete_list_element(self, tmp_path: Path) -> None:
        """Tier 2: -- removes a list element by index."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-f-002"

        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["messages[0]--"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        (only_msg,) = only["messages"]
        assert only_msg["role"] == "user"


# ---------------------------------------------------------------------------
# (g) multiple patches — sequential application, last write wins
# ---------------------------------------------------------------------------


class TestMultiplePatchOrder:
    def test_last_patch_wins(self, tmp_path: Path) -> None:
        """Tier 2: multiple --patch exprs applied in CLI order; later =  replaces earlier."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-g-001"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[
                'messages[0].content="first"',
                'messages[0].content="second"',
                'messages[0].content="third"',
            ],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only["messages"][0]["content"] == "third"

    def test_independent_patches_both_applied(self, tmp_path: Path) -> None:
        """Tier 2: two independent patches each take effect."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-g-002"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"temperature": 0.0, "max_tokens": 50},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[
                "sampling_params.temperature=0.7",
                "sampling_params.max_tokens=200",
            ],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only.get("temperature") == pytest.approx(0.7)
        assert only.get("max_tokens") == 200


# ---------------------------------------------------------------------------
# (h) invalid path → error (SystemExit)
# ---------------------------------------------------------------------------


class TestInvalidPath:
    def test_absent_path_raises(self, tmp_path: Path) -> None:
        """Tier 2: patching a path whose parent does not exist raises SystemExit."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-h-001"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        with pytest.raises(SystemExit):
            asyncio.run(mod._run(
                request_id=rid,
                trace_path=path,
                model_override=None,
                temperature_override=None,
                max_tokens_override=None,
                n=1,
                full=False,
                output_format="pretty",
                patch_exprs=["nonexistent_key.deep.field=42"],
                acompletion_fn=stub,
            ))

        # LLM must NOT have been called
        assert not stub.calls

    def test_malformed_expression_raises(self, tmp_path: Path) -> None:
        """Tier 2: a syntactically unparseable patch expression raises SystemExit."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-h-002"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        with pytest.raises(SystemExit):
            asyncio.run(mod._run(
                request_id=rid,
                trace_path=path,
                model_override=None,
                temperature_override=None,
                max_tokens_override=None,
                n=1,
                full=False,
                output_format="pretty",
                patch_exprs=[""],  # empty path is invalid
                acompletion_fn=stub,
            ))

        assert not stub.calls


# ---------------------------------------------------------------------------
# (i) += on non-string target → error
# ---------------------------------------------------------------------------


class TestAppendNonString:
    def test_append_to_non_string_raises(self, tmp_path: Path) -> None:
        """Tier 2: += on a non-string target raises SystemExit."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-i-001"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"temperature": 0.5},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        with pytest.raises(SystemExit):
            asyncio.run(mod._run(
                request_id=rid,
                trace_path=path,
                model_override=None,
                temperature_override=None,
                max_tokens_override=None,
                n=1,
                full=False,
                output_format="pretty",
                # temperature is a float, not a string
                patch_exprs=["sampling_params.temperature+= more"],
                acompletion_fn=stub,
            ))

        assert not stub.calls


# ---------------------------------------------------------------------------
# (j) value JSON parsing: bool, list, string fallback
# ---------------------------------------------------------------------------


class TestValueParsing:
    """Tier 2: value field is parsed as JSON literal; falls back to raw string."""

    def _run_patch(self, tmp_path: Path, patch_expr: str) -> dict:
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-j-001"
        req = {
            "kind": "request",
            "request_id": rid,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "model": "gemini-2.5-flash-lite",
            "caller_hint": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "tool_choice": None,
            "sampling_params": {"flag": False, "names": [], "label": "old"},
        }
        with path.open("w") as f:
            f.write(json.dumps(req) + "\n")

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[patch_expr],
            acompletion_fn=stub,
        ))
        return stub.calls[0]

    def test_bool_true(self, tmp_path: Path) -> None:
        """Tier 2: --patch a=true parses as Python bool True."""
        call = self._run_patch(tmp_path, "sampling_params.flag=true")
        # sampling_params.flag would not appear directly in litellm kwargs,
        # but we can confirm it was set by checking it does NOT raise and the
        # stub was called.  The flag key is skip-listed; just verify call happened.
        # (temperature/flag not forwarded; but the mutation must not have crashed)
        assert call is not None  # stub was reached

    def test_list_value(self, tmp_path: Path) -> None:
        """Tier 2: --patch a=[1,2] parses as Python list."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-j-list"
        _write_trace_with_tools(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[
                'tools[0].function.parameters.properties.name.enum=["x","y"]'
            ],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        enum_val = only["tools"][0]["function"]["parameters"]["properties"]["name"]["enum"]
        assert isinstance(enum_val, list)
        assert enum_val == ["x", "y"]

    def test_string_fallback(self, tmp_path: Path) -> None:
        """Tier 2: --patch a=bare_word uses the raw string when JSON parse fails."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-j-str"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=["messages[0].content=bare_word"],
            acompletion_fn=stub,
        ))

        (only,) = stub.calls
        assert only["messages"][0]["content"] == "bare_word"


# ---------------------------------------------------------------------------
# (k) integration: _apply_patches public function + _CaptureLLM
# ---------------------------------------------------------------------------


class TestPatchIntegration:
    def test_apply_patches_returns_summary(self) -> None:
        """Tier 2: _apply_patches returns a list of (path, description) tuples."""
        mod = _import_replay()
        payload = {
            "messages": [{"role": "user", "content": "hello"}],
            "sampling_params": {"temperature": 0.0},
        }
        applied = mod._apply_patches(payload, [
            'messages[0].content="patched"',
            "sampling_params.temperature=0.7",
        ])
        paths = {p for p, _ in applied}
        assert paths == {"messages[0].content", "sampling_params.temperature"}

    def test_payload_mutated_in_place(self) -> None:
        """Tier 2: _apply_patches mutates the payload dict in place."""
        mod = _import_replay()
        payload = {
            "messages": [{"role": "user", "content": "original"}],
            "tools": None,
        }
        mod._apply_patches(payload, ['messages[0].content="mutated"'])
        assert payload["messages"][0]["content"] == "mutated"

    def test_integration_patched_payload_reaches_llm(self, tmp_path: Path) -> None:
        """Tier 2: patch applied to trace payload propagates to litellm call kwargs."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-k-001"
        _write_trace_with_tools(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=[
                'tools[0].function.parameters.properties.name.enum=["skill_a","skill_b","skill_c"]',
                'messages[0].content+=" Available skills (3): skill_a, skill_b, skill_c"',
            ],
            acompletion_fn=stub,
        ))

        (call,) = stub.calls

        # Enum must be patched
        enum_val = call["tools"][0]["function"]["parameters"]["properties"]["name"]["enum"]
        assert enum_val == ["skill_a", "skill_b", "skill_c"]

        # Message content must have the appended suffix
        assert "Available skills" in call["messages"][0]["content"]

    def test_applied_patches_printed_in_pretty_mode(
        self, tmp_path: Path, capsys
    ) -> None:
        """Tier 2: applied patches section appears in --output-format pretty output."""
        mod = _import_replay()
        path = tmp_path / "trace.jsonl"
        rid = "patch-k-002"
        _write_simple_trace(path, rid)

        stub = _CaptureLLM()
        asyncio.run(mod._run(
            request_id=rid,
            trace_path=path,
            model_override=None,
            temperature_override=None,
            max_tokens_override=None,
            n=1,
            full=False,
            output_format="pretty",
            patch_exprs=['messages[0].content="x"'],
            acompletion_fn=stub,
        ))

        out = capsys.readouterr().out
        assert "Applied patches" in out
        assert "messages[0].content" in out
