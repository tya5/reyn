"""OtelExporter — map P6 audit-events to OpenTelemetry spans, metrics, and logs.

The single adapter module for the OS's OpenTelemetry surface. It subscribes to a
P6 EventLog and emits OTLP telemetry off-loop, fire-and-forget, and fail-open:

* **Downstream only.** ``.reyn/events`` + the WAL remain the durable
  recovery/replay Source-of-Truth. This exporter never writes to either and is
  never a recovery source. OTEL absence changes nothing about what is recovered.
* **Fail-open.** Every export path swallows all exceptions (latched so a broken
  endpoint does not spam the logs). An OTLP endpoint that is unreachable,
  raising, or slow does not break the run — the exact inverse of the durability
  worker's fail-stop contract.
* **Off-loop.** Span/metric/log records are handed to batch processors whose
  OTLP HTTP export happens on background threads, so the event loop never blocks
  on the network.
* **Opt-in.** Nothing is attached unless an OTLP endpoint is configured (an
  ``observability.otel.endpoint`` config value or the standard
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var). With no endpoint the exporter is not
  built and behavior is byte-identical to having no OTEL at all.

The GenAI attribute vocabulary is pinned to a single semantic-convention version
(``GENAI_CONVENTION_VERSION``) and every ``gen_ai.*`` key is a named constant in
this module rather than a scattered string literal, so the convention version is
auditable in one place.

The mapping, at a glance (see ``docs/reference/runtime/observability.md``):

* ``session_started`` / ``session_completed`` → root span
* ``turn_started`` / ``turn_completed`` / ``turn_cancelled`` / ``turn_settled``
  → turn span (``gen_ai.operation.name``)
* ``llm_called`` + ``llm_response_received`` → ``chat <model>`` child span +
  ``gen_ai.usage.*`` token attributes + cost/token metric histograms
* ``tool_executed`` / ``mcp_called`` / ``web_*`` → ``execute_tool`` child spans
* ``permission_*`` / ``user_intervention_*`` / safety events → log records

Content/PII is off by default (``capture_content``): P6 events are refs, not raw
prompt/response bodies, and this exporter never promotes a raw body into a span
or log unless content capture is explicitly opted in.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from reyn.config.observability import ObservabilityConfig
    from reyn.schemas.models import Event

logger = logging.getLogger(__name__)

# ── Pinned GenAI semantic-convention version (stability: Development) ─────────
# The OpenTelemetry GenAI conventions are still "Development"-stability, so their
# attribute names can change between releases. Pin the version here; the mapping
# below uses ONLY the named constants derived from this version, and
# ``tests/observability/`` asserts every emitted ``gen_ai.*`` key belongs to the
# installed semantic-convention package for this pin.
GENAI_CONVENTION_VERSION = "1.37.0"
GENAI_SCHEMA_URL = f"https://opentelemetry.io/schemas/{GENAI_CONVENTION_VERSION}"

# GenAI attribute names (mirror opentelemetry.semconv gen_ai_attributes at the
# pinned version). Named here so the convention surface is auditable in one spot
# and never scattered as bare string literals across the mapping.
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"

# The full set of GenAI convention keys this adapter may emit. The conformance
# test asserts each of these is a real attribute of the pinned semconv package.
GENAI_ATTRIBUTE_NAMES: frozenset[str] = frozenset({
    GEN_AI_OPERATION_NAME,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_AGENT_ID,
    GEN_AI_AGENT_NAME,
    GEN_AI_TOOL_NAME,
})

# Metric instrument names (OTel GenAI metric conventions).
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_COST_USAGE = "gen_ai.client.cost.usd"

# Non-span audit-events that map to OTLP log records rather than spans.
_LOG_EVENT_TYPES: frozenset[str] = frozenset({
    "permission_granted",
    "permission_denied",
    "user_intervention_requested",
    "user_intervention_received",
    "safety_triggered",
    "safety_limit_reached",
})

# Tool-family events that open+close a child span at the event (post-hoc audit
# records are point-in-time, so the span is short-lived).
_TOOL_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_executed",
    "mcp_called",
    "mcp_failed",
    "mcp_cancelled",
    "web_fetch_started",
    "web_fetch_failed",
    "web_search_started",
    "web_search_completed",
    "web_search_failed",
})


def _corr_keys(data: dict[str, Any]) -> list[str]:
    """Correlation keys for an event, most-specific first.

    A run is correlated by ``run_id`` when present, else by ``agent_id`` (the
    session-scoped identity the EventLog auto-stamps onto chat events), else a
    process-default bucket. Both keys are returned when both are present so a
    root span stored under either can be found by a child that carries the
    other (robust to the chat path's run_id-absent-on-session_started gap).
    """
    keys: list[str] = []
    rid = data.get("run_id")
    aid = data.get("agent_id")
    if rid:
        keys.append(f"run:{rid}")
    if aid:
        keys.append(f"agent:{aid}")
    if not keys:
        keys.append("default:_")
    return keys


class OtelExporter:
    """A P6 EventLog subscriber that emits OpenTelemetry telemetry, fail-open.

    Construct via :func:`build_otel_exporter` for the OTLP-wired process
    singleton; the constructor takes already-built OTel primitives so tests can
    inject in-memory span/metric/log capture (real instances, no mocks).

    Fail-open contract: :meth:`__call__` wraps the entire per-event dispatch in a
    single guard that swallows every exception (latched). Stripping that guard is
    the SR5 falsification — an un-swallowed export error then propagates through
    ``EventLog.emit`` and breaks the run.
    """

    def __init__(
        self,
        *,
        tracer: Any,
        token_histogram: Any,
        cost_histogram: Any,
        otel_logger: Any,
        capture_content: bool = False,
        shutdown_hook: Callable[[], None] | None = None,
    ) -> None:
        self._tracer = tracer
        self._token_histogram = token_histogram
        self._cost_histogram = cost_histogram
        self._logger = otel_logger
        self._capture_content = capture_content
        self._shutdown_hook = shutdown_hook
        # Open spans, keyed by correlation key. A root/turn span may be stored
        # under several keys (see _corr_keys); llm/web spans are pending until
        # their paired completion event closes them.
        self._root_spans: dict[str, Any] = {}
        self._turn_spans: dict[str, Any] = {}
        self._llm_spans: dict[str, Any] = {}
        self._web_spans: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._error_latched = False
        self._shutdown = False

    # ── subscriber entry point ──────────────────────────────────────────────

    def __call__(self, event: "Event") -> None:
        """P6 subscriber callable. Never raises (fail-open, SR5).

        The whole dispatch runs under one guard: any exception from mapping or
        from the OTLP pipeline is swallowed and latched to a single warning so a
        broken endpoint neither breaks the run nor spams the log.
        """
        try:
            self._dispatch(event)
        except Exception as exc:  # noqa: BLE001 — fail-open is the whole point (SR5)
            self._latch_error(exc)

    def _latch_error(self, exc: BaseException) -> None:
        if not self._error_latched:
            self._error_latched = True
            logger.warning(
                "OtelExporter export failed; suppressing further OTEL export "
                "errors for this process (session + durable stores are "
                "unaffected): %s",
                exc,
            )

    # ── dispatch ────────────────────────────────────────────────────────────

    def _dispatch(self, event: "Event") -> None:
        etype = event.type
        data = event.data or {}
        if etype == "session_started":
            self._on_session_started(data)
        elif etype == "session_completed":
            self._on_session_completed(data)
        elif etype == "turn_started":
            self._on_turn_started(data)
        elif etype in ("turn_completed", "turn_cancelled", "turn_settled"):
            self._on_turn_ended(data, cancelled=(etype == "turn_cancelled"))
        elif etype == "llm_called":
            self._on_llm_called(data)
        elif etype == "llm_response_received":
            self._on_llm_response(data)
        elif etype in _TOOL_EVENT_TYPES:
            self._on_tool_event(etype, data)
        elif etype in _LOG_EVENT_TYPES:
            self._on_log_event(etype, event)
        # SR5b: any other event type is silently ignored — never a crash on a gap.

    # ── span helpers ────────────────────────────────────────────────────────

    def _parent_context(self, keys: list[str]):
        """OTel context for the innermost open span among *keys*, else None."""
        from opentelemetry.trace import set_span_in_context
        for k in keys:
            span = self._turn_spans.get(k) or self._root_spans.get(k)
            if span is not None:
                return set_span_in_context(span)
        return None

    def _base_attrs(self, data: dict[str, Any]) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if data.get("run_id"):
            attrs[GEN_AI_CONVERSATION_ID] = str(data["run_id"])
        if data.get("agent_id"):
            attrs[GEN_AI_AGENT_ID] = str(data["agent_id"])
        return attrs

    def _on_session_started(self, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        attrs = self._base_attrs(data)
        if data.get("agent_name"):
            attrs[GEN_AI_AGENT_NAME] = str(data["agent_name"])
        name = f"session {data.get('agent_name', 'agent')}"
        span = self._tracer.start_span(name, attributes=attrs)
        with self._lock:
            for k in keys:
                self._root_spans.setdefault(k, span)

    def _on_session_completed(self, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        with self._lock:
            # Close any dangling turn/llm/web span first (SR1: no orphan leak).
            for k in keys:
                for store in (self._turn_spans, self._llm_spans, self._web_spans):
                    sp = store.pop(k, None)
                    if sp is not None:
                        sp.end()
            seen: set[int] = set()
            for k in keys:
                span = self._root_spans.pop(k, None)
                if span is not None and id(span) not in seen:
                    seen.add(id(span))
                    span.end()

    def _on_turn_started(self, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        attrs = self._base_attrs(data)
        attrs[GEN_AI_OPERATION_NAME] = "invoke_agent"
        if data.get("kind"):
            attrs["reyn.turn.kind"] = str(data["kind"])
        ctx = self._parent_context(keys)
        span = self._tracer.start_span("turn", context=ctx, attributes=attrs)
        with self._lock:
            for k in keys:
                self._turn_spans[k] = span

    def _on_turn_ended(self, data: dict[str, Any], *, cancelled: bool) -> None:
        keys = _corr_keys(data)
        with self._lock:
            seen: set[int] = set()
            for k in keys:
                span = self._turn_spans.pop(k, None)
                if span is not None and id(span) not in seen:
                    seen.add(id(span))
                    if cancelled:
                        self._mark_cancelled(span)
                    span.end()

    @staticmethod
    def _mark_cancelled(span: Any) -> None:
        from opentelemetry.trace import Status, StatusCode
        span.set_status(Status(StatusCode.ERROR, "turn_cancelled"))

    def _on_llm_called(self, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        model = str(data.get("model", "unknown"))
        attrs = self._base_attrs(data)
        attrs[GEN_AI_OPERATION_NAME] = "chat"
        attrs[GEN_AI_REQUEST_MODEL] = model
        ctx = self._parent_context(keys)
        span = self._tracer.start_span(f"chat {model}", context=ctx, attributes=attrs)
        with self._lock:
            # Key the pending llm span under the primary correlation key only, so
            # the paired response closes exactly this span.
            self._llm_spans[keys[0]] = span

    def _on_llm_response(self, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        in_tok = data.get("prompt_tokens")
        out_tok = data.get("completion_tokens")
        cost = data.get("cost_usd")
        with self._lock:
            span = None
            for k in keys:
                span = self._llm_spans.pop(k, None)
                if span is not None:
                    break
        if span is not None:
            if in_tok is not None:
                span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, int(in_tok))
            if out_tok is not None:
                span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, int(out_tok))
            if cost is not None:
                # Cost is not a GenAI-convention attribute (the conventions cover
                # token usage, not price); keep it under the reyn.* namespace so
                # the pinned gen_ai.* surface stays exactly the semconv set.
                span.set_attribute("reyn.usage.cost_usd", float(cost))
            span.end()
        # Metrics — histograms are always recorded even if the paired span was
        # missing (SR5b: out-of-order / gap tolerant).
        if self._token_histogram is not None:
            if in_tok is not None:
                self._token_histogram.record(int(in_tok), {"gen_ai.token.type": "input"})
            if out_tok is not None:
                self._token_histogram.record(int(out_tok), {"gen_ai.token.type": "output"})
        if self._cost_histogram is not None and cost is not None:
            self._cost_histogram.record(float(cost))

    def _on_tool_event(self, etype: str, data: dict[str, Any]) -> None:
        keys = _corr_keys(data)
        # web_*_started/completed pair; other tool events are point-in-time.
        if etype in ("web_fetch_started", "web_search_started"):
            attrs = self._base_attrs(data)
            attrs[GEN_AI_OPERATION_NAME] = "execute_tool"
            attrs[GEN_AI_TOOL_NAME] = etype.rsplit("_", 1)[0]
            ctx = self._parent_context(keys)
            span = self._tracer.start_span("execute_tool", context=ctx, attributes=attrs)
            with self._lock:
                self._web_spans[keys[0]] = span
            return
        if etype in ("web_fetch_failed", "web_search_completed", "web_search_failed"):
            with self._lock:
                span = None
                for k in keys:
                    span = self._web_spans.pop(k, None)
                    if span is not None:
                        break
            if span is not None:
                if etype.endswith("_failed"):
                    self._mark_error(span, etype)
                span.end()
            return
        # tool_executed / mcp_* — point-in-time span.
        tool_name = str(data.get("op") or data.get("tool") or data.get("server") or etype)
        attrs = self._base_attrs(data)
        attrs[GEN_AI_OPERATION_NAME] = "execute_tool"
        attrs[GEN_AI_TOOL_NAME] = tool_name
        ctx = self._parent_context(keys)
        span = self._tracer.start_span(
            f"execute_tool {tool_name}", context=ctx, attributes=attrs,
        )
        if etype in ("mcp_failed", "mcp_cancelled"):
            self._mark_error(span, etype)
        span.end()

    @staticmethod
    def _mark_error(span: Any, detail: str) -> None:
        from opentelemetry.trace import Status, StatusCode
        span.set_status(Status(StatusCode.ERROR, detail))

    def _on_log_event(self, etype: str, event: "Event") -> None:
        if self._logger is None:
            return
        from opentelemetry._logs import SeverityNumber
        data = event.data or {}
        # SR3: emit refs/identifiers only — never a raw prompt/response body.
        attrs: dict[str, Any] = {"reyn.event.type": etype}
        for field in ("run_id", "agent_id", "actor", "phase", "intervention_id"):
            if data.get(field) is not None:
                attrs[field] = str(data[field])
        severity = (
            SeverityNumber.WARN
            if etype in ("permission_denied", "safety_triggered", "safety_limit_reached")
            else SeverityNumber.INFO
        )
        self._logger.emit(
            severity_number=severity,
            body=etype,
            attributes=attrs,
            event_name=etype,
        )

    # ── shutdown (SR1) ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Close every still-open span and flush the OTLP pipeline (SR1).

        Idempotent + fail-open: a crash between a start and its paired end must
        not leak an orphan span, so any span still open at shutdown is closed
        before the provider flush. Safe to call from ``atexit`` and from an
        explicit teardown seam.
        """
        try:
            with self._lock:
                if self._shutdown:
                    return
                self._shutdown = True
                for store in (
                    self._llm_spans,
                    self._web_spans,
                    self._turn_spans,
                    self._root_spans,
                ):
                    seen: set[int] = set()
                    for span in list(store.values()):
                        if id(span) not in seen:
                            seen.add(id(span))
                            span.end()
                    store.clear()
            if self._shutdown_hook is not None:
                self._shutdown_hook()
        except Exception as exc:  # noqa: BLE001 — shutdown is fail-open too
            self._latch_error(exc)


# ── construction / opt-in gate ───────────────────────────────────────────────

_singleton_lock = threading.Lock()
_singleton: OtelExporter | None = None
_singleton_key: tuple | None = None


def otel_endpoint_configured(config: "ObservabilityConfig | None") -> str | None:
    """Return the configured OTLP endpoint, or None when OTEL is not opt-in.

    Priority: ``observability.otel.endpoint`` config value, then the standard
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var. None → the exporter is not built and
    behavior is byte-identical to having no OTEL.
    """
    if config is not None:
        ep = getattr(getattr(config, "otel", None), "endpoint", "") or ""
        if ep:
            return ep
    env = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return env or None


def reset_otel_exporter_singleton() -> None:
    """Drop the memoized process exporter (test isolation only)."""
    global _singleton, _singleton_key
    with _singleton_lock:
        _singleton = None
        _singleton_key = None


def build_otel_exporter(
    config: "ObservabilityConfig | None",
    *,
    resource_attributes: dict[str, str] | None = None,
) -> OtelExporter | None:
    """Build the process-wide OtelExporter, or return None when not opt-in.

    Returns None (zero overhead, not attached) when no OTLP endpoint is
    configured, OR when the optional OpenTelemetry SDK is not installed — a
    configured endpoint without the SDK logs once and stays fail-open (never
    raises to the caller). The exporter is a process singleton (one OTLP pipeline
    shared by every session's EventLog); spans stay correlated per run via
    run_id/agent_id.
    """
    endpoint = otel_endpoint_configured(config)
    if endpoint is None:
        return None

    otel_cfg = getattr(config, "otel", None)
    capture_content = bool(getattr(otel_cfg, "capture_content", False))
    service_name = str(getattr(otel_cfg, "service_name", "reyn") or "reyn")
    headers = dict(getattr(otel_cfg, "headers", {}) or {})

    key = (endpoint, service_name, capture_content, tuple(sorted(headers.items())))
    global _singleton, _singleton_key
    with _singleton_lock:
        if _singleton is not None and _singleton_key == key:
            return _singleton
        try:
            exporter = _build_pipeline(
                endpoint=endpoint,
                headers=headers,
                service_name=service_name,
                capture_content=capture_content,
                resource_attributes=resource_attributes or {},
            )
        except Exception as exc:  # noqa: BLE001 — build must never break startup
            logger.warning(
                "OtelExporter disabled: OpenTelemetry SDK unavailable or "
                "misconfigured (endpoint=%s). Install reyn[observability] to "
                "enable OTLP export. Reason: %s",
                endpoint, exc,
            )
            return None
        _singleton = exporter
        _singleton_key = key
        return exporter


def _bridge_standard_ca_to_otel_env() -> None:
    """#3075 fix 3: bridge the standard CA env to OTEL's own env var.

    The OTLP HTTP exporters (``opentelemetry-exporter-otlp-proto-http``) read
    ``OTEL_EXPORTER_OTLP_CERTIFICATE`` for a custom CA bundle — a DIFFERENT
    variable than the standard ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE`` every
    other reyn egress honours, so an operator with a corporate CA configured
    the standard way was silently unauthenticated (or failing TLS) for OTEL
    only. Bridge once, here, right before the exporter reads its env — never
    overrides an operator who already set ``OTEL_EXPORTER_OTLP_CERTIFICATE``
    explicitly (``setdefault``).
    """
    if os.environ.get("OTEL_EXPORTER_OTLP_CERTIFICATE"):
        return
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        ca_path = os.environ.get(name, "").strip()
        if ca_path:
            os.environ["OTEL_EXPORTER_OTLP_CERTIFICATE"] = ca_path
            return


def _build_pipeline(
    *,
    endpoint: str,
    headers: dict[str, str],
    service_name: str,
    capture_content: bool,
    resource_attributes: dict[str, str],
) -> OtelExporter:
    """Wire the real OTLP HTTP pipeline (spans batched off-loop) + register
    atexit shutdown. Raises if the OpenTelemetry SDK is not installed — the
    caller catches and falls back to not-attached (fail-open)."""
    import atexit

    _bridge_standard_ca_to_otel_env()

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    res_attrs = {"service.name": service_name, **resource_attributes}
    resource = Resource.create(res_attrs)

    # Traces — BatchSpanProcessor exports off-loop on a background thread (SR2).
    # schema_url is a get_tracer() arg, not a TracerProvider() arg — the provider
    # takes only the resource.
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=_trace_endpoint(endpoint), headers=headers)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    tracer = tracer_provider.get_tracer("reyn.observability", schema_url=GENAI_SCHEMA_URL)

    # Metrics — periodic off-loop export.
    meter_provider = None
    token_hist = cost_hist = None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_metric_endpoint(endpoint), headers=headers),
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        meter = meter_provider.get_meter("reyn.observability")
        token_hist = meter.create_histogram(
            METRIC_TOKEN_USAGE, unit="{token}", description="GenAI token usage",
        )
        cost_hist = meter.create_histogram(
            METRIC_COST_USAGE, unit="USD", description="GenAI call cost",
        )
    except Exception:  # noqa: BLE001 — metrics are best-effort; spans still work
        meter_provider = None

    # Logs — best-effort (the OTel logs SDK is still unstable).
    logger_provider = None
    otel_logger = None
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=_log_endpoint(endpoint), headers=headers),
            ),
        )
        otel_logger = logger_provider.get_logger("reyn.observability")
    except Exception:  # noqa: BLE001 — logs are best-effort
        logger_provider = None

    def _shutdown_providers() -> None:
        for prov in (tracer_provider, meter_provider, logger_provider):
            if prov is None:
                continue
            try:
                prov.shutdown()
            except Exception:  # noqa: BLE001 — shutdown is fail-open
                pass

    exporter = OtelExporter(
        tracer=tracer,
        token_histogram=token_hist,
        cost_histogram=cost_hist,
        otel_logger=otel_logger,
        capture_content=capture_content,
        shutdown_hook=_shutdown_providers,
    )
    atexit.register(exporter.shutdown)
    return exporter


def _trace_endpoint(base: str) -> str:
    return base if base.rstrip("/").endswith("/v1/traces") else base.rstrip("/") + "/v1/traces"


def _metric_endpoint(base: str) -> str:
    return base if base.rstrip("/").endswith("/v1/metrics") else base.rstrip("/") + "/v1/metrics"


def _log_endpoint(base: str) -> str:
    return base if base.rstrip("/").endswith("/v1/logs") else base.rstrip("/") + "/v1/logs"
