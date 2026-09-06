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

import logging
import platform
from pathlib import Path

from reyn.config.infra import SandboxConfig
from reyn.security.sandbox import get_default_backend
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session


def _session(tmp_path: Path, sandbox_config: "SandboxConfig | None", *, inject_noop: bool = True):
    """A REAL Session (no fakes). By default its sandbox backend is a REAL
    ``NoopBackend`` instance — passed explicitly rather than left to
    platform auto-selection, so a test measures the SAME thing on a macOS
    runner (which would auto-select Seatbelt) as on a Linux one.
    ``inject_noop=False`` leaves selection to the config, for the tests
    about what happens when that selection cannot succeed."""
    return make_session(
        agent_name="posture-test",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / ".reyn",
        sandbox_config=sandbox_config,
        sandbox_backend=NoopBackend() if inject_noop else None,
    )


def _foreign_backend_name() -> str:
    """A backend name that genuinely cannot be selected on THIS host —
    Seatbelt is macOS-only, Landlock is Linux-only — so "the backend cannot
    be selected" is a real platform fact here, not a fake. Chosen at run
    time so the same test means the same thing on either CI runner."""
    return "landlock" if platform.system() == "Darwin" else "seatbelt"


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


# ── #5892 co-vet 🔴-1: the display read is a QUERY, never the exec ACTION ──


def test_the_gap_read_never_raises_even_under_on_unsupported_error(tmp_path):
    """Tier 2: ``sandbox.on_unsupported: error`` is the fail-closed knob —
    "refuse to run AI code unsandboxed". Routed through the exec-site
    resolver, a mere DISPLAY read of the boundary raised ``RuntimeError``
    under it (measured by the architect on the previous head), so the knob
    whose job is to refuse a run was killing the status readout instead.
    The read must return a STRING naming both the reason and the
    consequence (the refusal), and never raise."""
    cfg = SandboxConfig(mode="strict", backend=_foreign_backend_name(), on_unsupported="error")
    session = _session(tmp_path, cfg, inject_noop=False)

    gap = session.network_enforcement_gap  # must not raise

    assert gap is not None, "an unselectable backend under a closed-network policy is a gap"
    assert "refused" in gap, gap


def test_the_gap_read_logs_nothing_across_repeated_reads(tmp_path, caplog):
    """Tier 2: a status snapshot is read on EVERY frame (``_refresh_live_
    chrome``'s own docstring) and on every server status ping. Routed
    through the exec-site resolver this read logged one WARNING per read
    under the shipped default (``on_unsupported: warn``) on any host whose
    backend cannot be verified — a log line per frame, bounded only by the
    frame rate (#5873's class). N reads must produce ZERO log records from
    the sandbox package: the read is a query, and the action's logging
    stays at the action's own site."""
    caplog.set_level(logging.DEBUG, logger="reyn.security.sandbox")
    cfg = SandboxConfig(mode="strict", backend=_foreign_backend_name(), on_unsupported="warn")
    session = _session(tmp_path, cfg, inject_noop=False)

    reads = [session.network_enforcement_gap for _ in range(5)]

    assert all(r is not None and "Noop" in r for r in reads), reads
    sandbox_records = [r for r in caplog.records if r.name.startswith("reyn.security.sandbox")]
    assert sandbox_records == [], [r.getMessage() for r in sandbox_records]


def test_the_exec_action_path_still_applies_the_knob(tmp_path):
    """Tier 2: the positive control that says the application MOVED rather
    than DISAPPEARED — the SAME unselectable-backend config, through the
    exec-site resolver (``get_default_backend``, what ``run_sandboxed_exec``
    calls via ``launcher.resolve_backend``), still refuses under ``error``.
    The full exec-path witnesses live in ``tests/security/test_sandbox_
    factory.py`` (``on_unsupported='error'`` raising for auto and for a
    forced foreign backend); this one pins that the split in
    ``security/sandbox/__init__.py`` left that path's behaviour intact."""
    import pytest

    cfg = SandboxConfig(backend=_foreign_backend_name(), on_unsupported="error")
    with pytest.raises(RuntimeError):
        get_default_backend(cfg)
