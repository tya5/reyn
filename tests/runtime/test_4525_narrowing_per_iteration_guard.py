"""Tier 2: #4525 — router_loop_driver._narrowing_per_iteration's own
getattr-guarded branch.

Architect's measurement (#4525): the module's docstring names a real reason
for the guard ("test hosts pass partial safety objects") and one real test
(``tests/llm/test_slash_model_router_integration.py:87``) genuinely passes
``safety=None`` — but no test exercises the branch the guard actually
protects (missing/partial ``safety``/``threat_scan`` -> ``False``). This
passes CLAUDE.md's Q3 discriminator (a production consumer really reaches
this shape; it is not a configuration only a test would construct), so
architect's own ruling is to close the gap with a test, not to treat it as
Tier 4.

``_narrowing_per_iteration`` is a private, pure module-level function with
no collaborator state to fake — directly importing and testing it matches
this repo's own established pattern for pure helpers (e.g.
``test_events_pure_helpers.py``).
"""
from __future__ import annotations

from types import SimpleNamespace

from reyn.config.chat import SafetyConfig, ThreatScanConfig
from reyn.runtime.services.router_loop_driver import _narrowing_per_iteration


def test_missing_safety_object_does_not_narrow():
    """Tier 2: the guarded branch, verbatim — safety=None (the real shape
    test_slash_model_router_integration.py:87 passes) -> False, not a
    crash."""
    assert _narrowing_per_iteration(None) is False


def test_safety_object_missing_threat_scan_does_not_narrow():
    """Tier 2: a partial safety object (has other fields, but no
    threat_scan) -> False. The docstring's own named reason ("test hosts
    pass partial safety objects")."""
    partial_safety = SimpleNamespace()  # no threat_scan attribute at all
    assert _narrowing_per_iteration(partial_safety) is False


def test_threat_scan_present_but_narrowing_off_does_not_narrow():
    """Tier 2: (accept-side) a FULL safety object with capability_narrowing
    at its default ("off") also returns False — same outcome as the
    missing-attribute case, but through the real predicate, not the guard.
    Distinguishes "the guard fired" from "the real config says off"."""
    safety = SafetyConfig(threat_scan=ThreatScanConfig(capability_narrowing="off"))
    assert _narrowing_per_iteration(safety) is False


def test_threat_scan_present_and_narrowing_on_does_narrow():
    """Tier 2: (accept-side) the guard does not ALWAYS return False — a
    full safety object with capability_narrowing="iteration" returns True,
    proving the guard only fires on the genuinely-missing case, not on
    every input."""
    safety = SafetyConfig(threat_scan=ThreatScanConfig(capability_narrowing="iteration"))
    assert _narrowing_per_iteration(safety) is True
