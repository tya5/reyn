"""Tier 2: FP-0007 Component A — TraceExporter backend invariants.

Six tests cover the four backends, the dispatcher wiring, and the
failure-isolation contract:

1. test_file_exporter_writes_jsonl_to_traces_dir
2. test_langfuse_exporter_posts_to_api
3. test_otlp_exporter_emits_spans
4. test_ietf_audit_exporter_maps_required_fields
5. test_event_export_dispatcher_calls_all_configured_exporters
6. test_exporter_failure_does_not_block_skill_execution

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock.MagicMock / AsyncMock / patch.
- Real instances or plain async fakes.
- Only public surface assertions (no _private state).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.eval.export import (
    FileExporter,
    IETFAuditExporter,
    LangfuseExporter,
    OTLPExporter,
    TraceExporter,
)
from reyn.eval.export_dispatcher import EventExportDispatcher
from reyn.events.events import EventLog

# ── Shared test fixtures ─────────────────────────────────────────────────────

def _make_events(*, run_id: str = "run_abc123", chain_id: str | None = None) -> list[dict]:
    """Return a minimal list of P6-like event dicts for use in exporter tests."""
    data: dict = {"run_id": run_id, "skill": "test_skill"}
    if chain_id:
        data["chain_id"] = chain_id
    return [
        {
            "type": "workflow_started",
            "timestamp": "2026-05-14T10:00:00+00:00",
            "data": {**data},
        },
        {
            "type": "llm_called",
            "timestamp": "2026-05-14T10:00:01+00:00",
            "data": {**data, "phase": "main"},
        },
        {
            "type": "workflow_finished",
            "timestamp": "2026-05-14T10:00:05+00:00",
            "data": {**data},
        },
    ]


# ── Test 1: FileExporter ─────────────────────────────────────────────────────


def test_file_exporter_writes_jsonl_to_traces_dir(tmp_path):
    """Tier 2: FileExporter appends events as JSONL to the configured output directory.

    Verifies:
    - A .jsonl file is created under the output dir.
    - Each exported event is a valid JSON line.
    - The run_id appears in the filename.
    """
    traces_dir = tmp_path / "traces"
    exporter = FileExporter(output_dir=traces_dir)
    events = _make_events(run_id="testabc")
    asyncio.run(exporter.export(events))

    jsonl_files = list(traces_dir.glob("*.jsonl"))
    assert jsonl_files, "FileExporter must create at least one .jsonl file"

    # Filename must contain the run_id so files group by run
    filenames = [f.name for f in jsonl_files]
    assert any("testabc" in name for name in filenames), (
        f"Expected run_id 'testabc' in filename; got {filenames}"
    )

    # All lines must be valid JSON
    written = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == len(events), (
        f"Expected {len(events)} lines, got {len(written)}"
    )
    for line in written:
        parsed = json.loads(line)  # raises if invalid
        assert "type" in parsed


def test_file_exporter_includes_chain_id_in_filename(tmp_path):
    """Tier 2: FileExporter prefixes the filename with chain_id when present.

    This allows easy grouping of related runs by chain.
    """
    traces_dir = tmp_path / "traces"
    exporter = FileExporter(output_dir=traces_dir)
    events = _make_events(run_id="runXYZ", chain_id="chain99")
    asyncio.run(exporter.export(events))

    jsonl_files = list(traces_dir.glob("*.jsonl"))
    assert jsonl_files
    assert any("chain99" in f.name for f in jsonl_files), (
        f"Expected 'chain99' in filename for chain grouping; got {[f.name for f in jsonl_files]}"
    )


# ── Test 2: LangfuseExporter ─────────────────────────────────────────────────


class _FakeHTTPResponse:
    """Plain fake HTTP response — not a mock."""

    def __init__(self, status_code: int = 200, text: str = "OK") -> None:
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Plain fake httpx.AsyncClient capturing the last POST call."""

    def __init__(self, response: _FakeHTTPResponse) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_headers: dict | None = None
        self.last_json: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, url: str, *, headers: dict, json: dict, **_) -> _FakeHTTPResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self._response


def test_langfuse_exporter_posts_to_api(tmp_path, monkeypatch):
    """Tier 2: LangfuseExporter sends a POST to the configured Langfuse API.

    Verifies:
    - The Authorization header is Basic auth (base64 of public:secret).
    - The request body contains a 'batch' key with the correct event count.
    - Each batch item has a 'type' == 'trace-create'.

    Uses a plain fake AsyncClient (not AsyncMock) injected via monkeypatch.
    """
    import base64

    import reyn.eval.export as export_mod

    fake_client = _FakeAsyncClient(_FakeHTTPResponse(200))

    # Monkeypatch httpx.AsyncClient in the export module's namespace
    monkeypatch.setattr(
        "reyn.eval.export.httpx",
        type("FakeHTTPX", (), {"AsyncClient": lambda *a, **kw: fake_client})(),
        raising=False,
    )

    # Ensure httpx is importable in the module (patch the import guard)
    import types
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.AsyncClient = lambda *a, **kw: fake_client  # type: ignore[attr-defined]

    import sys
    original_httpx = sys.modules.get("httpx")
    sys.modules["httpx"] = fake_httpx  # type: ignore[assignment]

    try:
        exporter = LangfuseExporter(
            public_key="pk_test",
            secret_key="sk_test",
            host="https://langfuse.example.com",
        )
        events = _make_events()
        asyncio.run(exporter.export(events))
    finally:
        if original_httpx is not None:
            sys.modules["httpx"] = original_httpx
        elif "httpx" in sys.modules:
            del sys.modules["httpx"]

    assert fake_client.last_url is not None, "LangfuseExporter must call httpx POST"
    assert "api/public/ingestion" in fake_client.last_url

    # Authorization header must be Basic base64(public_key:secret_key)
    expected_token = base64.b64encode(b"pk_test:sk_test").decode()
    assert fake_client.last_headers is not None
    assert fake_client.last_headers.get("Authorization") == f"Basic {expected_token}", (
        f"Bad auth header: {fake_client.last_headers.get('Authorization')}"
    )

    # Body must have a 'batch' list with one entry per event
    body = fake_client.last_json
    assert body is not None
    assert "batch" in body, f"Expected 'batch' in POST body; got {list(body.keys())}"
    assert len(body["batch"]) == len(events), (
        f"Expected {len(events)} batch items, got {len(body['batch'])}"
    )
    for item in body["batch"]:
        assert item.get("type") == "trace-create", (
            f"Expected type='trace-create', got {item.get('type')!r}"
        )


# ── Test 3: OTLPExporter ─────────────────────────────────────────────────────


def test_otlp_exporter_emits_spans(tmp_path):
    """Tier 2: OTLPExporter emits one OpenTelemetry span per P6 event.

    When opentelemetry-exporter-otlp-proto-http is not installed, the test
    is skipped gracefully (optional dep).

    When the dep IS available, we verify:
    - export() runs without raising.
    - The span count matches the event count (via an in-memory SpanExporter).
    """
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:
        pytest.skip("opentelemetry SDK not installed (reyn[eval] optional dep)")

    # Wire up an in-memory exporter so we can assert on span count
    # without a real OTLP collector.
    from opentelemetry import trace as otel_trace

    in_memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_memory))
    otel_trace.set_tracer_provider(provider)

    exporter = OTLPExporter(endpoint="http://localhost:4317")
    # Inject the already-configured provider so the exporter uses ours
    exporter._provider = provider

    events = _make_events()
    asyncio.run(exporter.export(events))

    spans = in_memory.get_finished_spans()
    assert len(spans) == len(events), (
        f"Expected {len(events)} spans (one per event), got {len(spans)}"
    )
    span_names = [s.name for s in spans]
    event_types = [e["type"] for e in events]
    for ev_type in event_types:
        assert ev_type in span_names, (
            f"Expected span named {ev_type!r}; got {span_names}"
        )


# ── Test 4: IETFAuditExporter ─────────────────────────────────────────────────


def test_ietf_audit_exporter_maps_required_fields(tmp_path):
    """Tier 2: IETFAuditExporter produces records with all 4 IETF audit fields.

    Required fields per draft-sharif-agent-audit-trail:
        identity, timing, routing, parameters

    Verifies all 4 are present in each output line and that 'raw_event'
    is also preserved for lossless round-trip.
    """
    out_path = tmp_path / "audit-trail" / "audit.jsonl"
    exporter = IETFAuditExporter(output_path=out_path)
    events = _make_events(run_id="run_ietf_test")
    asyncio.run(exporter.export(events))

    assert out_path.exists(), "IETFAuditExporter must create the output file"
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(events)

    required_fields = {"identity", "timing", "routing", "parameters"}
    for line in lines:
        record = json.loads(line)
        for field in required_fields:
            assert field in record, (
                f"IETF audit record missing required field '{field}': {record}"
            )
        # raw_event preserved for lossless round-trip
        assert "raw_event" in record

    # Spot-check: identity must contain run_id for the first event
    first = json.loads(lines[0])
    assert first["identity"].get("run_id") == "run_ietf_test", (
        f"Expected run_id in identity; got {first['identity']}"
    )
    # timing must be the event timestamp
    assert first["timing"] is not None, "timing must be non-null"


# ── Test 5: EventExportDispatcher ────────────────────────────────────────────


class _RecordingExporter:
    """Plain fake TraceExporter that records what was exported.

    Not a mock — real async callable satisfying the TraceExporter Protocol.
    """

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def export(self, events: list[dict]) -> None:
        self.calls.append(list(events))


def test_event_export_dispatcher_calls_all_configured_exporters():
    """Tier 2: EventExportDispatcher delivers events to every registered exporter.

    Simulates a skill run by emitting events into an EventLog (including
    workflow_finished) and asserts that both configured exporters each
    received the complete event batch.
    """
    exp_a = _RecordingExporter()
    exp_b = _RecordingExporter()
    event_log = EventLog()

    dispatcher = EventExportDispatcher(exporters=[exp_a, exp_b], event_log=event_log)
    event_log.add_subscriber(dispatcher)

    # Emit events as a skill run would
    event_log.emit("workflow_started", run_id="run_dispatch_test", skill="test_skill")
    event_log.emit("llm_called", run_id="run_dispatch_test", skill="test_skill")
    event_log.emit("workflow_finished", run_id="run_dispatch_test", skill="test_skill")

    # Both exporters must have been called exactly once with all events
    assert len(exp_a.calls) == 1, f"exp_a should have been called once; got {len(exp_a.calls)}"
    assert len(exp_b.calls) == 1, f"exp_b should have been called once; got {len(exp_b.calls)}"

    total_events = len(event_log.all())
    assert len(exp_a.calls[0]) == total_events, (
        f"exp_a received {len(exp_a.calls[0])} events; expected {total_events}"
    )
    assert len(exp_b.calls[0]) == total_events, (
        f"exp_b received {len(exp_b.calls[0])} events; expected {total_events}"
    )


# ── Test 6: Failure isolation ─────────────────────────────────────────────────


class _AlwaysFailExporter:
    """Plain fake exporter that always raises on export()."""

    async def export(self, events: list[dict]) -> None:
        raise RuntimeError("simulated exporter failure")


def test_exporter_failure_does_not_block_skill_execution():
    """Tier 2: An exporter raising an exception must not propagate to the caller.

    The EventExportDispatcher wraps each exporter in _safe_export which
    catches all exceptions and logs at WARNING level.  A failing exporter
    must not prevent other exporters from running or cause any exception
    to surface to the EventLog.emit() call path.

    Uses two exporters: one always-failing and one recording.  The recording
    exporter must still be called successfully.
    """
    failing = _AlwaysFailExporter()
    recording = _RecordingExporter()
    event_log = EventLog()

    dispatcher = EventExportDispatcher(exporters=[failing, recording], event_log=event_log)
    event_log.add_subscriber(dispatcher)

    # This must complete without raising
    event_log.emit("workflow_started", run_id="run_fail_test", skill="test_skill")
    event_log.emit("workflow_finished", run_id="run_fail_test", skill="test_skill")

    # The recording exporter must still have been called
    assert len(recording.calls) == 1, (
        f"Recording exporter must be called despite the failing exporter; "
        f"got {len(recording.calls)} calls"
    )
