"""OtelExporter — P6 audit-event → OTLP span/metric/log, fail-open + downstream.

Tier 2 OS-invariant coverage for the ADR-0039 P5 observability surface. Every
test uses real OpenTelemetry SDK in-memory capture (InMemorySpanExporter /
InMemoryMetricReader / InMemoryLogExporter) — no mocks — so the OTLP contract
and the reyn event→telemetry adapter are exercised against real instances.

The two load-bearing gates:

* SR5 (fail-open): an OTEL export that raises must not break the run and must
  leave ``.reyn/events`` intact. The strip-falsify (remove the swallow → the
  raise propagates → RED) is recorded in the PR body.
* SR4 (recovery-independence): the recovery source is byte-identical whether or
  not OTEL is attached — OTEL is never a recovery source (the inverted
  truncate-falsify).
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.observability.otel_exporter import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GENAI_ATTRIBUTE_NAMES,
    METRIC_COST_USAGE,
    METRIC_TOKEN_USAGE,
    OtelExporter,
    build_otel_exporter,
    otel_endpoint_configured,
    reset_otel_exporter_singleton,
)
from reyn.schemas.models import Event

# The OTEL SDK is an OPT-IN dependency (reyn[observability]). CI installs the
# extra so these tests run with real in-memory OTLP capture; a dev/env without
# the extra skips the whole module gracefully rather than erroring on import.
pytest.importorskip("opentelemetry.sdk.trace")

pytestmark = pytest.mark.filterwarnings("ignore")


# ── real in-memory OTEL capture harness (no mocks) ───────────────────────────


class _Capture:
    """Holds an OtelExporter wired to real in-memory OTEL SDK exporters."""

    def __init__(self, *, capture_content: bool = False) -> None:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        self._span_exp = InMemorySpanExporter()
        tp = TracerProvider()
        tp.add_span_processor(SimpleSpanProcessor(self._span_exp))
        tracer = tp.get_tracer("test")

        self._metric_reader = InMemoryMetricReader()
        mp = MeterProvider(metric_readers=[self._metric_reader])
        meter = mp.get_meter("test")
        token_hist = meter.create_histogram(METRIC_TOKEN_USAGE)
        cost_hist = meter.create_histogram(METRIC_COST_USAGE)

        self._log_exp = InMemoryLogExporter()
        lp = LoggerProvider()
        lp.add_log_record_processor(SimpleLogRecordProcessor(self._log_exp))
        otel_logger = lp.get_logger("test")

        self.exporter = OtelExporter(
            tracer=tracer,
            token_histogram=token_hist,
            cost_histogram=cost_hist,
            otel_logger=otel_logger,
            capture_content=capture_content,
        )

    def spans(self) -> list[Any]:
        return list(self._span_exp.get_finished_spans())

    def metric_names(self) -> set[str]:
        names: set[str] = set()
        data = self._metric_reader.get_metrics_data()
        if data is None:
            return names
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    names.add(m.name)
        return names

    def metric_sum(self, name: str) -> float:
        total = 0.0
        data = self._metric_reader.get_metrics_data()
        if data is None:
            return total
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name != name:
                        continue
                    for pt in m.data.data_points:
                        total += pt.sum
        return total

    def log_bodies(self) -> list[str]:
        return [str(lr.log_record.body) for lr in self._log_exp.get_finished_logs()]


def _ev(etype: str, ts: datetime | None = None, **data: Any) -> Event:
    if ts is None:
        return Event(type=etype, data=data)
    return Event(type=etype, timestamp=ts, data=data)


_RID = "run-abc"
_AID = "agent-xyz"


def _full_run(ts_base: datetime | None = None) -> list[Event]:
    """A representative single-run event stream (session→turn→llm→tool→turn)."""
    def _t(i: int) -> datetime | None:
        return None if ts_base is None else ts_base.replace(microsecond=i)

    return [
        _ev("session_started", _t(1), agent_name="alice", agent_id=_AID),
        _ev("turn_started", _t(2), kind="user", run_id=_RID, agent_id=_AID),
        _ev("llm_called", _t(3), model="gpt-4o", run_id=_RID, agent_id=_AID),
        _ev(
            "llm_response_received", _t(4),
            prompt_tokens=100, completion_tokens=20, cost_usd=0.003,
            run_id=_RID, agent_id=_AID,
        ),
        _ev("tool_executed", _t(5), op="read_file", path="/x", run_id=_RID, agent_id=_AID),
        _ev("permission_denied", _t(6), run_id=_RID, actor="a", phase="p", agent_id=_AID),
        _ev("turn_completed", _t(7), chain_id="c", run_id=_RID, agent_id=_AID),
        _ev("session_completed", _t(8), agent_name="alice", agent_id=_AID),
    ]


@pytest.fixture(autouse=True)
def _reset_singleton_and_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    reset_otel_exporter_singleton()
    yield
    reset_otel_exporter_singleton()


# ── a real (non-mock) collaborator that always raises, for the fail-open gate ─


class _RaisingTracer:
    """A real tracer-shaped object whose start_span always raises.

    Not a mock — a concrete stand-in for a broken/unreachable OTLP pipeline, so
    the fail-open guard is exercised against a genuine exception source.
    """

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("otlp endpoint unreachable")


# ── 1. SR5 fail-open gate ────────────────────────────────────────────────────


def test_sr5_fail_open_export_error_does_not_break_run(tmp_path: Path) -> None:
    """Tier 2: an OTEL export that raises leaves the run + .reyn/events intact."""
    store = EventStore(tmp_path / "events", max_bytes=0, max_age_seconds=0)
    exporter = OtelExporter(
        tracer=_RaisingTracer(),
        token_histogram=None,
        cost_histogram=None,
        otel_logger=None,
    )
    log = EventLog(subscribers=[store, exporter])

    # Emitting through the EventLog must NOT raise even though every span start
    # raises inside the exporter (fail-open swallow).
    for e in _full_run():
        log.emit(e.type, **e.data)

    # The audit/recovery source is written exactly as without OTEL — every
    # emitted event landed in the EventStore file.
    written = [ev.type for ev in store.iter_all()]
    for e in _full_run():
        assert e.type in written
    # in-memory EventLog record is complete too
    assert [ev.type for ev in log.all()] == [e.type for e in _full_run()]


# ── 1b. SR4 recovery-independence gate (inverted truncate-falsify) ────────────


def test_sr4_recovery_source_unchanged_with_and_without_otel(
    tmp_path: Path,
) -> None:
    """Tier 2: the recovery source is byte-for-byte unchanged whether OTEL attaches."""
    ts = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = _full_run(ts_base=ts)

    store_with = EventStore(tmp_path / "with", max_bytes=0, max_age_seconds=0)
    store_without = EventStore(tmp_path / "without", max_bytes=0, max_age_seconds=0)
    cap = _Capture()

    # Same fixed-timestamp Event objects fan out to each store's subscriber
    # chain; the OTEL-attached chain ALSO drives the exporter.
    for e in events:
        store_with(e)
        cap.exporter(e)
    for e in events:
        store_without(e)

    with_lines = _event_lines(store_with)
    without_lines = _event_lines(store_without)
    # OTEL attachment contributes ZERO bytes to the recovery source.
    assert with_lines == without_lines
    assert without_lines  # sanity: the streams are non-empty

    # Inverted truncate-falsify: drop the OTEL exporter entirely, re-derive the
    # recovery source from the same events → still byte-identical. OTEL absence
    # does not change what is recovered.
    store_replay = EventStore(tmp_path / "replay", max_bytes=0, max_age_seconds=0)
    for e in events:
        store_replay(e)
    assert _event_lines(store_replay) == with_lines


def _event_lines(store: EventStore) -> list[str]:
    """The store's on-disk JSONL, one raw line per recovered event."""
    lines: list[str] = []
    for path in store.iter_files():
        lines.extend(
            ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
        )
    return lines


# ── 2. span-tree correlation ─────────────────────────────────────────────────


def test_span_tree_parentage_root_turn_child() -> None:
    """Tier 2: session→turn→llm/tool spans nest into one correlated trace."""
    cap = _Capture()
    for e in _full_run():
        cap.exporter(e)
    cap.exporter.shutdown()

    spans = cap.spans()
    by_name = {s.name: s for s in spans}
    assert "session alice" in by_name
    assert "turn" in by_name
    assert "chat gpt-4o" in by_name
    assert any(n.startswith("execute_tool") for n in by_name)

    # single correlated trace: every span shares the root's trace id
    root = by_name["session alice"]
    root_trace = root.context.trace_id
    assert all(s.context.trace_id == root_trace for s in spans)

    turn = by_name["turn"]
    llm = by_name["chat gpt-4o"]
    assert root.parent is None
    assert turn.parent is not None and turn.parent.span_id == root.context.span_id
    assert llm.parent is not None and llm.parent.span_id == turn.context.span_id


# ── 3. GenAI convention conformance (pinned version) ─────────────────────────


def test_genai_attribute_names_belong_to_pinned_semconv() -> None:
    """Tier 2: every emitted gen_ai.* key exists in the pinned semconv package."""
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g

    known = {v for k, v in vars(g).items() if k.startswith("GEN_AI") and isinstance(v, str)}
    unknown = {a for a in GENAI_ATTRIBUTE_NAMES if a.startswith("gen_ai.")} - known
    assert not unknown, f"gen_ai.* keys not in pinned semconv: {unknown}"


def test_emitted_span_attrs_only_use_pinned_genai_keys() -> None:
    """Tier 2: spans emit only gen_ai.* keys declared in GENAI_ATTRIBUTE_NAMES."""
    cap = _Capture()
    for e in _full_run():
        cap.exporter(e)
    cap.exporter.shutdown()

    for span in cap.spans():
        for key in span.attributes or {}:
            if key.startswith("gen_ai."):
                assert key in GENAI_ATTRIBUTE_NAMES, f"undeclared gen_ai key {key}"
    # spot-check the pinned keys are actually present where expected
    llm = next(s for s in cap.spans() if s.name == "chat gpt-4o")
    assert llm.attributes[GEN_AI_REQUEST_MODEL] == "gpt-4o"
    assert llm.attributes[GEN_AI_OPERATION_NAME] == "chat"


# ── 4. metrics (cost/token histogram) ────────────────────────────────────────


def test_cost_and_token_metrics_recorded() -> None:
    """Tier 2: llm_response_received records token + cost histograms."""
    cap = _Capture()
    for e in _full_run():
        cap.exporter(e)

    names = cap.metric_names()
    assert METRIC_TOKEN_USAGE in names
    assert METRIC_COST_USAGE in names
    # 100 input + 20 output tokens recorded
    assert cap.metric_sum(METRIC_TOKEN_USAGE) == pytest.approx(120.0)
    assert cap.metric_sum(METRIC_COST_USAGE) == pytest.approx(0.003)

    llm = next(s for s in cap.spans() if s.name == "chat gpt-4o")
    assert llm.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 100  # noqa: PLR2004
    assert llm.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 20  # noqa: PLR2004


# ── 5. content-off default (SR3 privacy) ─────────────────────────────────────


def test_content_off_by_default_no_raw_body_in_telemetry() -> None:
    """Tier 2: with capture_content off (default), no raw prompt/response leaks."""
    cap = _Capture()  # capture_content defaults False
    secret = "SECRET-PROMPT-BODY-should-not-appear"
    resp = "SECRET-RESPONSE-BODY-should-not-appear"
    events = [
        _ev("session_started", agent_name="alice", agent_id=_AID),
        _ev("turn_started", kind="user", run_id=_RID, agent_id=_AID),
        _ev("llm_called", model="gpt-4o", prompt=secret, run_id=_RID, agent_id=_AID),
        _ev(
            "llm_response_received", completion=resp, prompt_tokens=5,
            completion_tokens=5, cost_usd=0.0, run_id=_RID, agent_id=_AID,
        ),
        _ev("user_intervention_received", run_id=_RID, actor="a",
            intervention_id="iv1", answer=secret, agent_id=_AID),
    ]
    for e in events:
        cap.exporter(e)
    cap.exporter.shutdown()

    blob = _all_telemetry_text(cap)
    assert secret not in blob
    assert resp not in blob


def test_reasoning_cot_not_routed_to_otel_under_default() -> None:
    """Tier 2: SR3 (P6a boundary) — reasoning chain-of-thought never reaches the
    OTEL export under the content-off default. The AG-UI display path (P6a) surfaces
    reasoning to a connected operator client, but the observability backend must NOT
    receive CoT: reasoning is a transport-frame concern, not an audit-event, so no
    reasoning event is subscribed, and even a reasoning string carried on an
    llm_response payload stays out of every span attribute / log body."""
    cap = _Capture()  # capture_content defaults False
    cot = "REASONING-COT-should-never-reach-observability-17*23=391"
    events = [
        _ev("session_started", agent_name="alice", agent_id=_AID),
        _ev("turn_started", kind="user", run_id=_RID, agent_id=_AID),
        _ev("llm_called", model="gpt-4o", run_id=_RID, agent_id=_AID),
        _ev(
            "llm_response_received",
            reasoning=cot, reasoning_content=cot,
            prompt_tokens=5, completion_tokens=5, cost_usd=0.0,
            run_id=_RID, agent_id=_AID,
        ),
        _ev("turn_completed", chain_id="c", run_id=_RID, agent_id=_AID),
        _ev("session_completed", agent_name="alice", agent_id=_AID),
    ]
    for e in events:
        cap.exporter(e)
    cap.exporter.shutdown()

    assert cot not in _all_telemetry_text(cap)


def _all_telemetry_text(cap: _Capture) -> str:
    parts: list[str] = []
    for span in cap.spans():
        parts.append(span.name)
        for k, v in (span.attributes or {}).items():
            parts.append(f"{k}={v}")
    parts.extend(cap.log_bodies())
    return "\n".join(parts)


# ── 6. opt-in / off-by-default (zero attach when no endpoint) ─────────────────


def test_off_by_default_build_returns_none_no_attach() -> None:
    """Tier 2: no endpoint → build returns None (zero attach, byte-identical)."""
    from reyn.config.observability import ObservabilityConfig

    cfg = ObservabilityConfig()
    assert otel_endpoint_configured(cfg) is None
    assert build_otel_exporter(cfg) is None
    assert build_otel_exporter(None) is None


def test_endpoint_opt_in_detected_from_config_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: an endpoint (config value OR env var) is the opt-in signal."""
    from reyn.config.observability import ObservabilityConfig, OtelConfig

    cfg = ObservabilityConfig(otel=OtelConfig(endpoint="http://localhost:4318"))
    assert otel_endpoint_configured(cfg) == "http://localhost:4318"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert otel_endpoint_configured(ObservabilityConfig()) == "http://collector:4318"


def test_real_otlp_pipeline_builds_with_endpoint_and_sdk() -> None:
    """Tier 2: with the SDK installed + an endpoint, the real OTLP pipeline builds.

    Guards the real provider-construction contract (SDK ctor kwargs) that the
    in-memory capture path can't exercise — a build that raises a ctor TypeError
    is swallowed to not-attached, so ``is not None`` is the regression signal.
    """
    pytest.importorskip("opentelemetry.sdk.trace")
    from reyn.config.observability import ObservabilityConfig, OtelConfig

    cfg = ObservabilityConfig(otel=OtelConfig(endpoint="http://127.0.0.1:4318"))
    exporter = build_otel_exporter(cfg)
    assert exporter is not None
    # empty flush (no spans emitted → no network) closes the real providers.
    exporter.shutdown()


# ── 7. orphan-span flush on shutdown (SR1) ───────────────────────────────────


def test_orphan_span_flushed_and_closed_on_shutdown() -> None:
    """Tier 2: spans unclosed at shutdown are ended + flushed (no orphan leak)."""
    cap = _Capture()
    # Open session + turn + llm but never emit their completion events.
    cap.exporter(_ev("session_started", agent_name="alice", agent_id=_AID))
    cap.exporter(_ev("turn_started", kind="user", run_id=_RID, agent_id=_AID))
    cap.exporter(_ev("llm_called", model="gpt-4o", run_id=_RID, agent_id=_AID))

    # Before shutdown nothing has ended (SimpleSpanProcessor exports on end).
    assert cap.spans() == []

    cap.exporter.shutdown()

    finished = {s.name for s in cap.spans()}
    assert "session alice" in finished
    assert "turn" in finished
    assert "chat gpt-4o" in finished
    # every finished span carries an end timestamp (closed, not leaked)
    for s in cap.spans():
        assert s.end_time is not None
