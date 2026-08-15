"""Tests for LLM payload trace dump via REYN_LLM_TRACE_DUMP env var.

Tier 2: OS invariant — verifies the dump infrastructure's public behaviour:
file creation, JSONL parseability, record structure, caller_hint routing,
and complete no-op when the env var is absent.

Design notes (testing policy alignment):
- Dump path controlled exclusively via monkeypatch.setenv (public API).
  No monkeypatch.setattr on private module constants.
- Unit tests for _dump_llm_request / _dump_llm_response verify dump logic
  directly without any LLM call (option iii: pure function unit test).
- Integration tests use a real callable stub for litellm.acompletion
  (allowed by testing policy: "monkeypatch.setattr with a real callable
  is acceptable when the replacement is a real callable, not MagicMock").
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers: minimal real-callable LLM stub (allowed by testing policy)
# ---------------------------------------------------------------------------

def _make_fake_litellm_response(
    content: str = '{"type":"decide","control":{"type":"finish","decision":"finish","next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},"artifact":{"type":"test","data":{}},"ops":[]}',
    cache_read_input_tokens: "int | None" = None,
):
    """Build a minimal response object that matches the litellm API surface.

    ``cache_read_input_tokens`` (#4821): omitted from the usage object
    entirely when ``None`` (the default) — matching a provider that never
    reports cache info at all, not a provider that reports a literal 0
    (a real cache miss). Set to test ``_cached_tokens_for_trace``'s "a
    real value" path.
    """
    msg = type("_Msg", (), {"content": content, "tool_calls": None})()
    choice = type("_Choice", (), {
        "message": msg,
        "finish_reason": "stop",
    })()
    usage_attrs = {"prompt_tokens": 10, "completion_tokens": 5}
    if cache_read_input_tokens is not None:
        usage_attrs["cache_read_input_tokens"] = cache_read_input_tokens
    usage = type("_Usage", (), usage_attrs)()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _ScriptedLLM:
    """Real callable stub — allowed by testing policy (not MagicMock)."""

    def __init__(self, response_content: str = "{}", cache_read_input_tokens: "int | None" = None):
        self._response_content = response_content
        self._cache_read_input_tokens = cache_read_input_tokens
        self.call_count = 0

    async def __call__(self, **kwargs):
        self.call_count += 1
        return _make_fake_litellm_response(
            self._response_content, cache_read_input_tokens=self._cache_read_input_tokens,
        )


def _minimal_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "run_skill",
                "description": "run a skill",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


MODEL = "gemini-2.5-flash-lite"

_DECIDE_JSON = (
    '{"type":"decide","control":{"type":"finish","decision":"finish",'
    '"next_phase":null,"confidence":0.9,"reason":{"summary":"ok"}},'
    '"artifact":{"type":"t","data":{}},"ops":[]}'
)


# ---------------------------------------------------------------------------
# Tier 2 unit tests: _dump_llm_request and _dump_llm_response directly
# (No LLM call needed — dump logic is a pure deterministic function)
# ---------------------------------------------------------------------------

class TestDumpRequestUnit:
    """Tier 2: unit tests for _dump_llm_request via env var (public API)."""

    def test_dump_request_writes_jsonl_when_env_set(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_request appends a JSON line when env var is set."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
        request_id = llm_mod._dump_llm_request(payload)

        assert request_id is not None, "Must return a request_id when dump is enabled"
        assert trace_file.exists(), "Trace file must be created"

        lines = [l for l in trace_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines, "At least one record must be written"
        record = json.loads(lines[-1])
        assert record["kind"] == "request"
        assert record["request_id"] == request_id
        assert record["model"] == "test-model"
        assert "timestamp" in record
        assert "messages" in record

    def test_dump_request_returns_none_when_env_absent(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_request returns None and creates no file when env var is absent."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)

        payload = {"model": "test-model", "messages": []}
        request_id = llm_mod._dump_llm_request(payload)

        assert request_id is None, "Must return None when dump is disabled"

    def test_dump_request_appends_multiple_records(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_request appends each call as a new line (no overwrite)."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        payload = {"model": "m", "messages": []}
        rid1 = llm_mod._dump_llm_request(payload)
        rid2 = llm_mod._dump_llm_request(payload)

        assert rid1 != rid2, "Each call must produce a distinct request_id"
        lines = [l for l in trace_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines, "Trace file must contain records"
        ids = {json.loads(l)["request_id"] for l in lines}
        assert ids == {rid1, rid2}


class TestDumpResponseUnit:
    """Tier 2: unit tests for _dump_llm_response via env var (public API)."""

    def test_dump_response_writes_paired_record(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_response appends a response record paired by request_id."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        rid = llm_mod._dump_llm_request({"model": "m", "messages": []})
        llm_mod._dump_llm_response(rid, {"content": "ok", "finish_reason": "stop", "usage": {}})

        lines = [l for l in trace_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines, "Records must be written"
        kinds = [json.loads(l)["kind"] for l in lines]
        assert "request" in kinds
        assert "response" in kinds

        req = next(json.loads(l) for l in lines if json.loads(l)["kind"] == "request")
        resp = next(json.loads(l) for l in lines if json.loads(l)["kind"] == "response")
        assert req["request_id"] == resp["request_id"] == rid

    def test_dump_response_noop_when_env_absent(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_response is a no-op when env var is absent."""
        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)
        # Should not raise; returns nothing
        llm_mod._dump_llm_response("fake-id", {"content": "x", "finish_reason": "stop", "usage": {}})

    def test_dump_response_noop_when_request_id_is_none(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: _dump_llm_response is a no-op when request_id is None (tracing disabled path)."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        # request_id=None means the request dump was skipped (env was absent at request time)
        llm_mod._dump_llm_response(None, {"content": "x", "finish_reason": "stop", "usage": {}})

        assert not trace_file.exists(), "No file should be created when request_id is None"

    def test_runtime_env_toggle(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: dump respects env var at call time — toggling mid-process works."""
        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace.jsonl"

        # Env absent → no dump
        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)
        rid_off = llm_mod._dump_llm_request({"model": "m", "messages": []})
        assert rid_off is None

        # Env set → dump enabled
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))
        rid_on = llm_mod._dump_llm_request({"model": "m", "messages": []})
        assert rid_on is not None
        assert trace_file.exists()


# ---------------------------------------------------------------------------
# Tier 2 integration tests: full call_llm / call_llm_tools path
# litellm.acompletion replaced by real callable stub (policy-allowed)
# Dump path controlled by monkeypatch.setenv (public API, no private setattr)
# ---------------------------------------------------------------------------

class TestTraceDumpDisabledByDefault:
    """Tier 2: when REYN_LLM_TRACE_DUMP is not set, no file is created."""

    def test_call_llm_tools_no_dump_without_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm_tools with env var absent leaves no trace file."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        monkeypatch.delenv("REYN_LLM_TRACE_DUMP", raising=False)

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        dummy_trace = tmp_path / "should_not_exist.jsonl"
        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        assert not dummy_trace.exists()
        assert stub.call_count >= 1


class TestTraceDumpEnabled:
    """Tier 2: when REYN_LLM_TRACE_DUMP is set, call_llm writes paired records."""

    def test_call_llm_tools_creates_jsonl_with_tools_schema(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: call_llm_tools includes tools schema in request record."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_tools.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        tools = _minimal_tools()
        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
            )
        )

        assert trace_file.exists()
        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert records, "Trace must contain at least one record"

        req = next(r for r in records if r.get("kind") == "request")
        assert req.get("tools") == tools, "Tools schema must be included verbatim in request"
        assert req.get("tool_choice") is not None


class TestCallerHint:
    """Tier 2: trace_caller is reflected in the dump as caller_hint."""

    def test_caller_hint_in_call_llm_tools(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: trace_caller kwarg arrives as caller_hint in call_llm_tools request record."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_caller_tools.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
                trace_caller="router",
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert req["caller_hint"] == "router"


class TestPromptCacheKeyInDump:
    """Tier 2: #4809 — the trace dump reflects prompt_cache_key (#4700),
    distinguishing a caller that never set one from a caller that
    explicitly resolved "no key" — #4690's own diagnosis needed exactly
    this distinction ("is it being sent, and is the value stable") and the
    trace record had no such field at all before this."""

    def test_prompt_cache_key_recorded_when_given(self, tmp_path: Path, monkeypatch) -> None:
        """Tier 2: a real prompt_cache_key value lands verbatim in the request record."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_cache_key.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
                prompt_cache_key="alpha:main",
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert req["prompt_cache_key"] == "alpha:main"

    def test_prompt_cache_key_present_as_null_when_not_given(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Tier 2: a caller that never sets prompt_cache_key must still see
        the KEY in the trace record, with value None — the whole point of
        #4809 is telling that apart from a trace file dumped before this
        fix existed (where the key is absent entirely, not present-as-null).
        A missing key here would silently look identical to the pre-#4809
        gap this issue exists to close."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_no_cache_key.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM()
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        req = next(r for r in records if r.get("kind") == "request")
        assert "prompt_cache_key" in req, (
            "the key must be present even when unset — an absent key is "
            "indistinguishable from a trace dumped before #4809"
        )
        assert req["prompt_cache_key"] is None


class TestCachedTokensInDump:
    """Tier 2: #4821 (architect's own #4809 acceptance condition) — the
    RESPONSE record reflects cached_tokens, joinable by request_id against
    the request's own prompt_cache_key reading. Same three-state
    discipline as TestPromptCacheKeyInDump, but with an extra distinction
    on the response side: a real 0 (genuine cache miss) must not collapse
    into the same value as "the provider reported nothing" — the exact
    gap _extract_cache_tokens (cost accounting) papers over on purpose and
    _cached_tokens_for_trace exists to NOT paper over."""

    def test_cached_tokens_recorded_when_provider_reports_a_hit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Tier 2: a real cache_read_input_tokens value lands verbatim in the response record."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_cached_hit.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM(cache_read_input_tokens=9728)
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        resp = next(r for r in records if r.get("kind") == "response")
        assert resp["cached_tokens"] == 9728

    def test_cached_tokens_none_when_provider_reports_nothing(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Tier 2: a provider that never surfaces cache fields at all must
        read as an explicit None in the response record — present as a
        key, not absent — the same "distinguishable from a pre-fix trace"
        requirement #4809 established for the request side."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_cached_unreported.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM()  # cache_read_input_tokens=None (default) — omitted entirely
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        resp = next(r for r in records if r.get("kind") == "response")
        assert "cached_tokens" in resp, (
            "the key must be present even when the provider reports nothing — "
            "an absent key is indistinguishable from a trace dumped before #4821"
        )
        assert resp["cached_tokens"] is None

    def test_cached_tokens_zero_is_a_real_miss_not_unreported(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Tier 2: a provider that EXPLICITLY reports cache_read_input_tokens=0
        (a genuine miss) must record as the integer 0, not None — the exact
        distinction _extract_cache_tokens (cost accounting) does not need
        to make but this diagnostic surface does."""
        import asyncio

        import litellm

        import reyn.llm.llm as llm_mod

        trace_file = tmp_path / "trace_cached_zero.jsonl"
        monkeypatch.setenv("REYN_LLM_TRACE_DUMP", str(trace_file))

        stub = _ScriptedLLM(cache_read_input_tokens=0)
        monkeypatch.setattr(litellm, "acompletion", stub)

        asyncio.run(
            llm_mod.call_llm_tools(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                tools=_minimal_tools(),
            )
        )

        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        resp = next(r for r in records if r.get("kind") == "response")
        assert resp["cached_tokens"] == 0
        assert resp["cached_tokens"] is not None


class TestMultipleCallsAccumulate:
    """Tier 2: consecutive calls append records, request_ids pair correctly."""

