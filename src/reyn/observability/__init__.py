"""Observability surface — downstream exporters for the P6 audit-event stream.

This package holds the OS's *lossy, downstream* observability adapters. They
subscribe to the P6 EventLog (the audit-event Source-of-Truth) and translate its
events into external telemetry formats. They are additive and fail-open: an
adapter that raises, hangs, or loses its endpoint must never affect the session
or any durable store. The durable recovery/replay substrate (``.reyn/events`` +
the WAL) is unchanged by anything in this package.
"""
from __future__ import annotations

from reyn.observability.otel_exporter import (
    GENAI_ATTRIBUTE_NAMES,
    GENAI_CONVENTION_VERSION,
    OtelExporter,
    build_otel_exporter,
    otel_endpoint_configured,
    reset_otel_exporter_singleton,
)

__all__ = [
    "GENAI_ATTRIBUTE_NAMES",
    "GENAI_CONVENTION_VERSION",
    "OtelExporter",
    "build_otel_exporter",
    "otel_endpoint_configured",
    "reset_otel_exporter_singleton",
]
