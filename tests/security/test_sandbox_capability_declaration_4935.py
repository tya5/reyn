"""Tier 1/2: #4935 — CapabilityDeclaration (D1: declare, never probe) +
``sandbox.require_capabilities`` opt-in resolution.

Real instances only, per the testing policy: every backend under test is
the REAL ``SeatbeltBackend``/``LandlockBackend``/``NoopBackend``/
``DockerEnvironmentBackend`` class, never a Mock/hand-rolled stand-in — the
declaration IS a class attribute on the real classes, so constructing them
for real is the only honest way to read it.

⚠️ **What this file does NOT witness** (same lesson as #4938's "CI-portable"
correction, applied proactively here): every test below runs on CI (no
darwin-only skip — the declarations are plain class attributes, checkable
on any OS), so CI DOES protect ``SeatbeltBackend.supported_capabilities ==
SUPPORTED`` as a *literal claim*. What CI cannot protect is whether that
claim is still TRUE in reality — whether ``_build_sbpl_profile`` still
actually emits the ``com.apple.SecurityServer`` grant and that grant still
actually works through a real Mac's ``sandbox-exec``. 0 macOS CI runners
exist (verified for #4938); only a human running
``tests/security/test_sandbox_seatbelt.py``'s own darwin-only live tests on
a real Mac re-verifies that. This file's own tests would stay green even if
someone silently deleted the SBPL grant while leaving this class attribute
at ``SUPPORTED`` — see ``capability.py``'s own module docstring for the
full argument.
"""
from __future__ import annotations

import pytest

from reyn.config.infra import SandboxConfig
from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.security.sandbox import _apply_required_capabilities
from reyn.security.sandbox.backend import SandboxBackend
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.capability import (
    SANDBOX_CAPABILITY_NAMES,
    CapabilityDeclaration,
    CapabilitySupport,
)
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import unsupported_required_capabilities

# ─── 1. Registry shape — real, disclosed evidence only ────────────────────


def test_registry_has_exactly_one_member_today() -> None:
    """Tier 1: #4935 — the capability registry has exactly ONE member,
    ``ipc_named_service`` (owner ruling relayed through lead-coder: "don't
    widen the frame speculatively — leave gaps visible"). This test fails
    the day someone adds a second field WITHOUT the production evidence
    this module's own docstring requires for the first."""
    assert SANDBOX_CAPABILITY_NAMES == {"ipc_named_service"}


def test_declaration_has_no_default_for_the_field() -> None:
    """Tier 1: #4935 — mirrors AxisEnforcementDeclaration's own D1/D2
    discipline (#4039): a backend that forgets to declare
    ``ipc_named_service`` fails to CONSTRUCT, not silently reads as
    unsupported."""
    with pytest.raises(TypeError):
        CapabilityDeclaration()  # type: ignore[call-arg]


# ─── 2. Every shipped backend has a real, motivated declaration ───────────


@pytest.mark.parametrize(
    "backend_cls, expected",
    [
        pytest.param(SeatbeltBackend, CapabilitySupport.SUPPORTED, id="seatbelt"),
        pytest.param(LandlockBackend, CapabilitySupport.NOT_SUPPORTED, id="landlock"),
        pytest.param(NoopBackend, CapabilitySupport.NOT_SUPPORTED, id="noop"),
        pytest.param(DockerEnvironmentBackend, CapabilitySupport.NOT_SUPPORTED, id="docker"),
    ],
)
def test_every_shipped_backend_declares_ipc_named_service(backend_cls, expected) -> None:
    """Tier 1: #4935 — every concrete SandboxBackend this repo ships has a
    real ``supported_capabilities`` declaration (population test, same
    shape as #4039's own per-backend enforced_axes checks) — Seatbelt
    SUPPORTED (proven by #4937's own grant working through the real
    backend), Landlock NOT_SUPPORTED (architect's kernel-doc research:
    restrict-only, no grant operation exists), Docker NOT_SUPPORTED
    (macOS-only concept, no Linux/container equivalent).

    Noop NOT_SUPPORTED (corrected, architect + lead-coder post-merge
    review of #4941 — a real defect this test's own FIRST version pinned
    the WRONG value for): ``CapabilitySupport`` asks whether a backend can
    EXPRESS a named-capability class, a mechanism question — Noop has no
    grant mechanism, full stop, it simply never needed one because it
    restricts nothing. The original SUPPORTED answered a DIFFERENT
    question ("is a required capability reachable under this backend",
    trivially true by construction) that is not what the field means, and
    it had a disclosed, real operator-visible consequence:
    ``require_capabilities`` + ``on_unsupported: error`` used to REJECT
    the genuinely-enforcing Landlock while ACCEPTING the fully-unenforced
    Noop — inverted predictability. See
    ``test_error_now_rejects_unenforced_noop_not_enforced_landlock``
    below for that exact scenario, fixed."""
    assert backend_cls.supported_capabilities.ipc_named_service is expected


def test_every_shipped_backend_satisfies_the_protocol() -> None:
    """Tier 1: #4935 — ``supported_capabilities`` is now a REQUIRED
    ``SandboxBackend`` Protocol member (mirrors ``enforced_axes``); every
    real backend instance must still satisfy ``isinstance(x,
    SandboxBackend)`` after this PR — a regression here would mean the
    Protocol widened without every implementation following.

    Docker excluded from INSTANTIATION here (its ``__init__`` needs a real
    ``container``/``repo_dir`` this narrow check has no reason to build) —
    its class-level declaration is already checked in
    ``test_every_shipped_backend_declares_ipc_named_service`` above, same
    as #4039's own D4 witness test reads ``enforced_axes`` off the CLASS,
    never an instance, for exactly this reason."""
    for backend in (SeatbeltBackend(), LandlockBackend(), NoopBackend()):
        assert isinstance(backend, SandboxBackend), (
            f"{type(backend).__name__} no longer satisfies SandboxBackend "
            f"after gaining supported_capabilities"
        )


# ─── 3. unsupported_required_capabilities — pure function, D1 boundary ────


def test_unsupported_required_capabilities_empty_when_nothing_required() -> None:
    """Tier 1: no requirement -> nothing missing, for every backend
    (including the ones that declare NOT_SUPPORTED — an unrequired
    capability is never reported, mirrors unenforced_axes's own
    not-the-complement caveat)."""
    assert unsupported_required_capabilities(LandlockBackend(), []) == []
    assert unsupported_required_capabilities(SeatbeltBackend(), []) == []


def test_unsupported_required_capabilities_reports_the_gap() -> None:
    """Tier 1: requiring ipc_named_service against Landlock (NOT_SUPPORTED)
    reports it; against Seatbelt (SUPPORTED) reports nothing."""
    assert unsupported_required_capabilities(
        LandlockBackend(), ["ipc_named_service"],
    ) == ["ipc_named_service"]
    assert unsupported_required_capabilities(
        SeatbeltBackend(), ["ipc_named_service"],
    ) == []


# ─── 4. SandboxConfig.require_capabilities — unknown name raises loudly ───


def test_sandbox_config_accepts_the_known_capability_name() -> None:
    """Tier 2: production-reaches — SandboxConfig construction with a real,
    registered capability name succeeds."""
    cfg = SandboxConfig(require_capabilities=["ipc_named_service"])
    assert cfg.require_capabilities == ["ipc_named_service"]


def test_sandbox_config_rejects_an_unknown_capability_name() -> None:
    """Tier 1: an unrecognised name raises at construction — never silently
    resolves to "not required" (same discipline `backend`/`on_unsupported`/
    `mode` already apply, #4935 extends it rather than inventing a new
    validation shape)."""
    with pytest.raises(ValueError, match="require_capabilities"):
        SandboxConfig(require_capabilities=["not_a_real_capability"])


def test_sandbox_config_default_is_empty() -> None:
    """Tier 1: #4935 owner ruling — declaring nothing changes nothing.
    Default SandboxConfig() has an empty require_capabilities list."""
    assert SandboxConfig().require_capabilities == []


# ─── 5. _apply_required_capabilities — the on_unsupported 3-way, reused ───


def test_apply_required_capabilities_noop_when_supported() -> None:
    """Tier 1: SUPPORTED backend + a requirement — no raise, no warning
    (nothing to apply on_unsupported to)."""
    _apply_required_capabilities(SeatbeltBackend(), ["ipc_named_service"], "error")
    # No exception — the pass-line for this test.


def test_apply_required_capabilities_error_raises() -> None:
    """Tier 1: #4935 — NOT_SUPPORTED backend + on_unsupported='error' raises,
    reusing the EXISTING 3-way vocabulary (owner: no second mental model)."""
    with pytest.raises(RuntimeError, match="ipc_named_service"):
        _apply_required_capabilities(LandlockBackend(), ["ipc_named_service"], "error")


def test_apply_required_capabilities_warn_logs_and_continues(caplog) -> None:
    """Tier 1: on_unsupported='warn' logs and does NOT raise — the run
    continues with the resolved (capability-gap) backend anyway, same
    default posture ``_noop_with_policy`` already uses for "no backend
    available"."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        _apply_required_capabilities(LandlockBackend(), ["ipc_named_service"], "warn")
    assert any("ipc_named_service" in r.message for r in caplog.records)


def test_apply_required_capabilities_ignore_is_silent(caplog) -> None:
    """Tier 1: on_unsupported='ignore' — no raise, no log line."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="reyn.security.sandbox"):
        _apply_required_capabilities(LandlockBackend(), ["ipc_named_service"], "ignore")
    assert not caplog.records


def test_error_now_rejects_unenforced_noop_not_enforced_landlock() -> None:
    """Tier 1: #4935 — the exact predictability-inversion scenario architect
    found (post-merge review of #4941), now fixed. Before this correction:
    ``require_capabilities: [ipc_named_service]`` + ``on_unsupported:
    error`` REJECTED Landlock (genuinely enforcing every other axis) while
    ACCEPTING Noop (enforcing nothing) — backwards from what a strict
    opt-in refusal is supposed to protect against. Both backends now
    declare NOT_SUPPORTED for the SAME reason (no grant mechanism exists),
    so ``error`` rejects both uniformly — no capability-declaration axis
    treats the unenforced backend as the SAFER one to accept."""
    for backend in (NoopBackend(), LandlockBackend()):
        with pytest.raises(RuntimeError, match="ipc_named_service"):
            _apply_required_capabilities(backend, ["ipc_named_service"], "error")


# ─── 6. Strip-falsify witness for the empty-default no-op guarantee ───────


def test_require_capabilities_empty_never_calls_the_capability_check(monkeypatch) -> None:
    """Tier 1: #4935 owner ruling, verified not just asserted — with
    require_capabilities left at its default ([]), get_default_backend()
    never even CALLS unsupported_required_capabilities. Monkeypatches the
    check to explode if invoked; a real run with the default config must
    not trigger it."""
    import reyn.security.sandbox as sandbox_pkg

    def _boom(*a, **k):
        raise AssertionError("unsupported_required_capabilities must not be called "
                              "when require_capabilities is empty (the default)")

    monkeypatch.setattr(
        "reyn.security.sandbox.policy.unsupported_required_capabilities", _boom,
    )
    # A real resolution with the default (empty) config must not explode.
    sandbox_pkg.get_default_backend(SandboxConfig())
