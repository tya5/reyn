"""`observability` config section — the OpenTelemetry (OTLP) export surface.

Opt-in and off by default: with no ``observability.otel.endpoint`` (and no
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var) the OtelExporter is never built and
behavior is byte-identical to having no OTEL. This is a lossy downstream
observability surface — it never affects the durable ``.reyn/events`` + WAL
recovery Source-of-Truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OtelConfig:
    """`observability.otel` — OTLP HTTP exporter settings.

    Fields:
        endpoint:
            The OTLP HTTP base URL (e.g. ``http://localhost:4318``). Empty
            (default) means OTEL is not attached — the standard
            ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is honored as a fallback, so
            OTEL can be enabled purely from the environment.
        headers:
            Optional per-request HTTP headers (auth tokens, tenant ids). Values
            support the same ``${VAR}`` env interpolation as MCP headers.
        service_name:
            The ``service.name`` resource attribute reported to the collector.
        capture_content:
            SR3 privacy gate. When False (default) the exporter emits refs and
            usage counts only — never a raw prompt/response body into a span or
            log. Set True to opt into GenAI content capture (Development-stability
            convention; only enable against a trusted collector).
    """

    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    service_name: str = "reyn"
    capture_content: bool = False


@dataclass
class ObservabilityConfig:
    """`observability:` — downstream telemetry export (currently OTLP/OTEL)."""

    otel: OtelConfig = field(default_factory=OtelConfig)


def _build_observability_config(raw: object) -> ObservabilityConfig:
    """Parse the ``observability:`` block. Absent/invalid → defaults (OTEL off)."""
    defaults = ObservabilityConfig()
    if not isinstance(raw, dict):
        return defaults
    otel_raw = raw.get("otel")
    if not isinstance(otel_raw, dict):
        return defaults
    otel_defaults = OtelConfig()
    headers_raw = otel_raw.get("headers")
    headers = (
        {str(k): str(v) for k, v in headers_raw.items()}
        if isinstance(headers_raw, dict)
        else dict(otel_defaults.headers)
    )
    return ObservabilityConfig(
        otel=OtelConfig(
            endpoint=str(otel_raw.get("endpoint", otel_defaults.endpoint) or ""),
            headers=headers,
            service_name=str(
                otel_raw.get("service_name", otel_defaults.service_name)
                or otel_defaults.service_name
            ),
            capture_content=bool(
                otel_raw.get("capture_content", otel_defaults.capture_content)
            ),
        ),
    )
