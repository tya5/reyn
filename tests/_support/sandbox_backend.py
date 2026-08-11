"""Shared #4039 enforced_axes declaration for real (non-mock) SandboxBackend
test stand-ins across the suite.

A stand-in whose test purpose is unrelated to axis enforcement (argv0
resolution, denial classification, hook-subprocess wiring, ...) needs SOME
``enforced_axes`` value — the Protocol field has no default
(``AxisEnforcementDeclaration`` has none, by design, #4039) — but should not
introduce a NEW ``sandbox_axis_unenforced`` audit-event the test wasn't
written to expect. :data:`FULLY_ENFORCING_AXES` (every axis ENFORCES, the
same shape as the real ``SeatbeltBackend``) keeps ``unenforced_axes()``
returning ``[]`` for these stand-ins, so behavior stays neutral.
"""
from __future__ import annotations

from reyn.security.sandbox.backend import AxisEnforcement, AxisEnforcementDeclaration

FULLY_ENFORCING_AXES = AxisEnforcementDeclaration(
    write_paths=AxisEnforcement.ENFORCES,
    write_deny_paths=AxisEnforcement.ENFORCES,
    read_deny_paths=AxisEnforcement.ENFORCES,
    network=AxisEnforcement.ENFORCES,
    deny_subprocess=AxisEnforcement.ENFORCES,
    env_deny_names=AxisEnforcement.ENFORCES,
    allow_env_names=AxisEnforcement.ENFORCES,
)
