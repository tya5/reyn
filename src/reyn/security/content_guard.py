"""Content-threat guard helpers — config-gated scan + fence (FP-0050 / #1822, S2).

Pure glue between the S1 primitives (``threat_patterns.scan`` / ``content_fence.
fence``) and a ``ThreatScanConfig``-shaped config. No I/O, no events, no skill
knowledge — the caller wires telemetry (``scan_for_threats`` returns matches;
the caller emits) and decides fence-eligibility (``fence_if_enabled`` only checks
the config gate; the *source-trust* gate lives at the call site, FP-0050 §3).

``config`` is duck-typed (``enabled`` / ``fence_enabled`` / ``fail_open`` /
``custom_patterns``) to avoid a security→config import. Both helpers fail-open
(return the safe no-op) on scanner error when ``fail_open`` — detection must
never wedge a turn.

#4523: every ``getattr(config, "enabled"/"fence_enabled", ...)`` shadow
default below matches ``ThreatScanConfig``'s own declared default (``True``
for both) — was ``False`` until #4523, silently disagreeing with the
declared "default-on" posture the moment a config object reached these
helpers without the attribute (a duck-typed object with a genuine partial
shape, or a future rename that dropped the field). Unreachable today (every
real caller passes a full ``ThreatScanConfig``), but the shape this repo
explicitly forbids elsewhere (``hooks/sandbox_scope.py``'s own docstring:
"a security field that is silently ignored reads as an applied restriction
that was never applied") — a declared-vs-read default flip on a security
gate is exactly that shape, whether or not it has fired yet.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from reyn.security.content_fence import fence
from reyn.security.threat_patterns import ThreatMatch, scan

if TYPE_CHECKING:
    from reyn.config.chat import ThreatScanConfig


def scan_for_threats(
    content: str,
    config: "ThreatScanConfig | Any",
    *,
    scope: str = "context",
) -> list[ThreatMatch]:
    """Scan ``content`` at ``scope`` when enabled; [] when disabled / on fail-open."""
    if config is None or not getattr(config, "enabled", True):
        return []
    try:
        extra = getattr(config, "custom_patterns", None) or None
        return scan(content, scope, extra_patterns=extra)
    except Exception:  # noqa: BLE001 — fail-open: detection must never wedge a turn
        if getattr(config, "fail_open", True):
            return []
        raise


_SEVERITY_RANK = {"warn": 1, "block": 2}


def severity_blocks(severity: str, threshold: str = "block") -> bool:
    """True if a match of ``severity`` should BLOCK given the config threshold.

    ``threshold="block"`` (default) blocks only ``block``-severity matches;
    ``threshold="warn"`` also blocks ``warn``-severity (stricter).
    """
    return _SEVERITY_RANK.get(severity, 2) >= _SEVERITY_RANK.get(threshold, 2)


def first_blocking_match(matches: "list[ThreatMatch]", threshold: str = "block") -> "ThreatMatch | None":
    """Return the first match at/above the block threshold, or None."""
    for m in matches:
        if severity_blocks(m.severity, threshold):
            return m
    return None


def fence_if_enabled(content: str, config: "ThreatScanConfig | Any") -> str:
    """Structurally fence ``content`` when enabled + fence_enabled; else unchanged.

    The *source-trust* decision (only untrusted-source content is fenced) is the
    caller's — this only applies the config gate + the fence transform.
    """
    if config is None or not getattr(config, "enabled", True) or not getattr(config, "fence_enabled", True):
        return content
    try:
        return fence(content).wrapped
    except Exception:  # noqa: BLE001 — fail-open
        if getattr(config, "fail_open", True):
            return content
        raise
