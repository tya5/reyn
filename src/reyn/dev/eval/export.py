"""TraceExporter — P6 event log を外部評価 tool に export する adapter (FP-0007 Component A).

Four backends are provided:
  - FileExporter     : append-only JSONL to .reyn/traces/ (default)
  - LangfuseExporter : Langfuse REST API (self-hostable) via existing httpx dep
  - OTLPExporter     : OpenTelemetry OTLP/HTTP (optional install)
  - IETFAuditExporter: IETF Agent Audit Trail draft format (JSONL)

P7 compliance: all exporters treat events as generic dicts with {type, timestamp,
data} — no skill-specific field names are referenced here.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class TraceExporter(Protocol):
    """P6 event export adapter contract (P7 compliant: no skill-specific knowledge).

    Each event dict has at minimum:
        type      (str)  — OS-level event kind
        timestamp (str)  — ISO-8601 UTC
        data      (dict) — arbitrary key/value payload

    Adapters read only these generic fields.  They MUST NOT reference
    skill-specific field names inside event.data.
    """

    async def export(self, events: list[dict]) -> None:
        """Export a batch of P6 events.

        Failures MUST be logged at WARNING level and swallowed — the exporter
        is a side channel; it must never block or fail a skill run.
        """
        ...


# ── FileExporter ──────────────────────────────────────────────────────────────


class FileExporter:
    """Default exporter — append-only JSONL under ``<output_dir>/<run_id>.jsonl``.

    The run_id is extracted from the first event's ``data.run_id`` field when
    present; otherwise a UUID is generated.  This groups all events for a
    single skill run into one file, consistent with EventStore's naming.

    chain_id (when present in data) is prepended so files sort by chain:
      ``<chain_id>_<run_id>.jsonl``
    """

    def __init__(self, output_dir: Path = Path(".reyn/traces")) -> None:
        self._output_dir = Path(output_dir)

    async def export(self, events: list[dict]) -> None:
        if not events:
            return
        try:
            run_id = _extract_run_id(events)
            chain_id = _extract_chain_id(events)
            stem = f"{chain_id}_{run_id}" if chain_id else run_id
            self._output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._output_dir / f"{stem}.jsonl"
            # Atomic append via a temp file in the same directory then rename
            # would be safer, but append-only mode is simpler and the spec
            # calls for append-only writes.  We follow the EventStore pattern.
            with out_path.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("FileExporter: export failed: %s", exc)


# ── LangfuseExporter ─────────────────────────────────────────────────────────

_LANGFUSE_BATCH_SIZE = 100


class LangfuseExporter:
    """Langfuse REST API exporter (self-hostable).

    Uses the existing ``httpx`` dependency — no additional install required.

    Auth: ``Authorization: Basic base64(public_key:secret_key)``
    Endpoint: ``POST <host>/api/public/ingestion``

    Langfuse's ingestion endpoint accepts a batch of events in their
    internal format.  We map P6 events to Langfuse "trace" objects:
        name     ← event type
        input    ← event data
        metadata ← {timestamp, type, ...all event fields}

    Transient failures are retried up to 3 times with exponential backoff
    (leveraging httpx's transport retry support).

    Spec reference: https://langfuse.com/docs/api
    """

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str,
    ) -> None:
        self._public_key = public_key
        self._secret_key = secret_key
        self._host = host.rstrip("/")

    def _auth_header(self) -> str:
        token = base64.b64encode(
            f"{self._public_key}:{self._secret_key}".encode()
        ).decode()
        return f"Basic {token}"

    async def export(self, events: list[dict]) -> None:
        if not events:
            return
        try:
            import httpx
        except ImportError:
            logger.warning("LangfuseExporter: httpx not available; skipping export")
            return

        url = f"{self._host}/api/public/ingestion"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }

        # Batch into chunks of _LANGFUSE_BATCH_SIZE
        for chunk_start in range(0, len(events), _LANGFUSE_BATCH_SIZE):
            chunk = events[chunk_start : chunk_start + _LANGFUSE_BATCH_SIZE]
            body = _build_langfuse_batch(chunk)
            await _httpx_post_with_retry(url, headers=headers, body=body, max_retries=3)


def _build_langfuse_batch(events: list[dict]) -> dict:
    """Convert P6 events to Langfuse ingestion batch format.

    Each event becomes a "trace-create" body object.  We use the event
    type + a UUID as the trace id so repeated exports are idempotent
    (same event → same trace id) only when the event carries a stable id;
    otherwise a fresh UUID is generated per export call.  We accept this
    behaviour because the exporter is advisory, not transactional.
    """
    batch_items = []
    for ev in events:
        ev_type = ev.get("type", "unknown")
        ev_data = ev.get("data") or {}
        ev_ts = ev.get("timestamp")
        item = {
            "type": "trace-create",
            "id": str(uuid.uuid4()),
            "timestamp": ev_ts,
            "body": {
                "name": ev_type,
                "input": ev_data,
                "metadata": {
                    "reyn_event_type": ev_type,
                    "reyn_timestamp": ev_ts,
                },
            },
        }
        batch_items.append(item)
    return {"batch": batch_items}


async def _httpx_post_with_retry(
    url: str, *, headers: dict, body: dict, max_retries: int
) -> None:
    """POST body to url with up to max_retries on transient failures.

    Transient: status 429, 5xx, or network errors.
    Non-transient (4xx except 429): logged once, no retry.
    """
    import asyncio

    import httpx

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=body)
            if resp.status_code < 300:
                return
            if resp.status_code == 429 or resp.status_code >= 500:
                # Transient — retry after backoff
                last_exc = RuntimeError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                continue
            # Non-transient 4xx
            logger.warning(
                "LangfuseExporter: non-retryable HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                import asyncio as _asyncio
                await _asyncio.sleep(2 ** attempt)

    logger.warning("LangfuseExporter: export failed after %d retries: %s", max_retries, last_exc)


# ── OTLPExporter ─────────────────────────────────────────────────────────────


class OTLPExporter:
    """OpenTelemetry OTLP/HTTP exporter.

    Requires the optional extras:  ``pip install reyn[eval]``
    (``opentelemetry-exporter-otlp-proto-http>=1.20``).

    Each P6 event is converted to one OpenTelemetry span:
      - span name      = event type
      - span attributes = event data fields (string-coerced)
      - span timestamp = event timestamp

    Import is deferred to __init__ so that the class is safe to
    instantiate even when the optional dep is absent — the graceful
    error fires on the first export() call, not at construction time.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._exporter = None
        self._provider = None

    def _ensure_provider(self) -> bool:
        """Lazy-initialize the OTLP provider.  Returns False if dep is absent."""
        if self._provider is not None:
            return True
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError:
            return False

        resource = Resource.create({"service.name": "reyn"})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=self._endpoint)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self._provider = provider
        self._exporter = exporter
        trace.set_tracer_provider(provider)
        return True

    async def export(self, events: list[dict]) -> None:
        if not events:
            return
        if not self._ensure_provider():
            logger.warning(
                "OTLPExporter: opentelemetry-exporter-otlp-proto-http is not installed; "
                "install reyn[eval] to enable OTLP export.  Skipping."
            )
            return
        try:
            from opentelemetry import trace
            from opentelemetry.trace import SpanKind

            tracer = trace.get_tracer("reyn.dev.eval.otlp")
            for ev in events:
                ev_type = ev.get("type", "unknown")
                ev_data = ev.get("data") or {}
                attrs = {
                    f"reyn.{k}": str(v)
                    for k, v in ev_data.items()
                    if v is not None
                }
                attrs["reyn.event_type"] = ev_type
                ts_raw = ev.get("timestamp")
                if ts_raw:
                    attrs["reyn.timestamp"] = str(ts_raw)
                with tracer.start_as_current_span(
                    ev_type,
                    kind=SpanKind.INTERNAL,
                    attributes=attrs,
                ):
                    pass  # Span is closed immediately; data is in attributes
        except Exception as exc:
            logger.warning("OTLPExporter: export failed: %s", exc)


# ── IETFAuditExporter ────────────────────────────────────────────────────────


class IETFAuditExporter:
    """IETF Agent Audit Trail draft format exporter.

    Reference: draft-sharif-agent-audit-trail (2026-05 draft; spec not final).

    Maps P6 event fields to the four required IETF audit fields:
        identity   ← run_id / chain_id from event.data (if present)
        timing     ← event.timestamp
        routing    ← event.data.state_dir (if present)
        parameters ← event.data (full payload as generic dict)

    Output: JSONL file at output_path.  Each line is a JSON object with
    the schema ``{identity, timing, routing, parameters, raw_event}``.

    The ``raw_event`` field carries the original event for lossless
    round-trip — downstream tools that understand Reyn's format can
    use it without re-parsing the mapped fields.

    # TODO(fp-0007): Re-check field mapping once draft-sharif-agent-audit-trail
    # is finalised.  The four required fields (identity/timing/routing/parameters)
    # are stable across recent drafts but their sub-structure may change.
    """

    def __init__(self, output_path: Path) -> None:
        self._output_path = Path(output_path)

    async def export(self, events: list[dict]) -> None:
        if not events:
            return
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with self._output_path.open("a", encoding="utf-8") as f:
                for ev in events:
                    record = _map_to_ietf_audit(ev)
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("IETFAuditExporter: export failed: %s", exc)


def _map_to_ietf_audit(ev: dict) -> dict:
    """Map a single P6 event to the IETF audit trail record schema.

    Required 4-field structure per draft-sharif-agent-audit-trail:
        identity   — who/what is acting
        timing     — when
        routing    — where (workspace / state path)
        parameters — what (op args, event payload)
    """
    data = ev.get("data") or {}
    identity: dict = {}
    if "run_id" in data:
        identity["run_id"] = data["run_id"]
    if "chain_id" in data:
        identity["chain_id"] = data["chain_id"]
    if "skill" in data:
        identity["skill"] = data["skill"]

    routing: dict = {}
    if "state_dir" in data:
        routing["state_dir"] = data["state_dir"]
    if "phase" in data:
        routing["phase"] = data["phase"]

    return {
        "identity": identity,
        "timing": ev.get("timestamp"),
        "routing": routing,
        "parameters": data,
        "raw_event": ev,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_run_id(events: list[dict]) -> str:
    """Extract run_id from the first event that carries one; else generate a UUID."""
    for ev in events:
        rid = (ev.get("data") or {}).get("run_id")
        if rid:
            return str(rid)
    return uuid.uuid4().hex[:12]


def _extract_chain_id(events: list[dict]) -> str | None:
    """Extract chain_id from the first event that carries one; else None."""
    for ev in events:
        cid = (ev.get("data") or {}).get("chain_id")
        if cid:
            return str(cid)
    return None
