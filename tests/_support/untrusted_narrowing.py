"""Shared test helper: opt IN to the untrusted-content capability narrowing.

The narrowing is OFF by default (#3501), so every test that exercises it has to
turn it on. Going through one named helper rather than five hand-built
``SafetyConfig``s makes the opt-in greppable: a test whose subject is the
narrowing calls this, and a test that does NOT call it is asserting the default
posture. Both are real ``reyn.config.chat`` objects — nothing here is a stand-in.
"""
from __future__ import annotations

from reyn.config.chat import SafetyConfig, ThreatScanConfig


def narrowing_on(mode: str = "turn") -> SafetyConfig:
    """A ``SafetyConfig`` with ``safety.threat_scan.capability_narrowing = mode``.

    ``mode`` is validated by ``ThreatScanConfig`` itself, so a typo here fails at
    construction rather than silently testing the ``off`` posture — which would
    make every assertion about the narrowing vacuous.
    """
    return SafetyConfig(threat_scan=ThreatScanConfig(capability_narrowing=mode))
