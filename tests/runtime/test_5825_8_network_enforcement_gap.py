"""Tier 2: OS invariant — #5825 item 8 (architect design, 2026-09-06):
``Session.network_enforcement_gap`` reports, from the session's OWN resolved
sandbox config, whether the configured backend can actually enforce a closed
network.

FP-0069 §8's own acceptance line, verbatim: "When the configured sandbox
backend cannot enforce the network deny (``sandbox_policy_not_applied``),
``bounded`` is shown as degraded in the posture surface — a boundary that is
not enforced is not silently called one."

The predicate itself (``policy.unenforced_axes``) is #3901/#4039's, tested
directly in ``tests/core/test_3901_sandbox_axis_unenforced.py``; what THIS
file measures is the seam that reads it PROACTIVELY off a real Session's own
config — i.e. that an operator learns their boundary is not real without
having to first run a command and read an audit event. The rendering half
lives in ``tests/interfaces/test_5825_8_network_posture_display.py``.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.infra import SandboxConfig
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session


def _session(tmp_path: Path, sandbox_config: "SandboxConfig | None"):
    """A REAL Session (no fakes) whose sandbox backend is a REAL
    ``NoopBackend`` instance — passed explicitly rather than left to
    platform auto-selection, so this test measures the SAME thing on a
    macOS runner (which would auto-select Seatbelt) as on a Linux one."""
    return make_session(
        agent_name="posture-test",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
        sandbox_config=sandbox_config,
        sandbox_backend=NoopBackend(),
    )


def test_a_closed_network_policy_a_backend_cannot_enforce_is_reported(tmp_path):
    """Tier 2: ``sandbox.mode: strict`` closes network (#3823's own
    ``_SANDBOX_STRICT_MODE_DEFAULTS``) and ``NoopBackend`` enforces nothing
    (its own ``enforced_axes`` declaration, #4039) — the session must name
    that gap, and name the REASON, not merely a boolean."""
    session = _session(tmp_path, SandboxConfig(mode="strict"))

    gap = session.network_enforcement_gap

    assert gap is not None, (
        "a strict-mode session on a backend that enforces nothing has a "
        "network boundary in name only — it must not read as enforced"
    )
    assert "no isolation" in gap, gap


def test_an_open_network_policy_reports_no_gap(tmp_path):
    """Tier 2: the discriminating control — SAME non-enforcing backend,
    but ``compat`` mode leaves network OPEN, so there is no boundary to
    fail at. Without this the test above would pass for a version that
    reported a gap unconditionally (i.e. for the backend alone, ignoring
    what the policy actually asked for) — which is exactly the
    over-reporting ``unenforced_axes``'s own docstring warns against
    ("not the complement of the declaration")."""
    session = _session(tmp_path, SandboxConfig(mode="compat"))

    assert session.network_enforcement_gap is None


def test_no_sandbox_config_at_all_reports_no_gap(tmp_path):
    """Tier 2: a session with no operator sandbox config (every bare
    programmatic Session — ``reyn pipe run``'s default identity, most
    tests) resolves to the compat floor: network open, nothing configured,
    nothing to fail at. Reported as "no gap", never as a degraded
    boundary — a fabricated warning is the same class of untrue display
    as a fabricated reassurance."""
    session = _session(tmp_path, None)

    assert session.network_enforcement_gap is None
