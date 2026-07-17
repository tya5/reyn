"""Sandboxed execution primitives (FP-0017).

Exports:
    SandboxPolicy   — declarative policy (what is allowed)
    SandboxBackend  — Protocol for backends (how it's enforced)
    SandboxResult   — return type of `backend.run()`
    NoopBackend     — default fallback (no isolation enforced)
    get_default_backend() — factory; platform-aware lazy auto-selection
    kill_process_tree() — shared cancel/timeout reaper (POSIX killpg / Windows
                          taskkill /T tree-kill), used by every subprocess site

Platform-specific backends live in `reyn.security.sandbox.backends.*` and are
lazy-imported so a missing sibling file (e.g. before Component B / C lands
in a given checkout) gracefully degrades to NoopBackend.
"""
from __future__ import annotations

import logging
import platform
from typing import TYPE_CHECKING

from ._subprocess_io import kill_process_tree
from .backend import SandboxBackend, SandboxResult, WrappedCommand
from .noop_backend import NoopBackend
from .policy import SandboxPolicy

if TYPE_CHECKING:
    from reyn.config import SandboxConfig

_logger = logging.getLogger(__name__)


def get_default_backend(config: "SandboxConfig | None" = None) -> SandboxBackend:
    """Return the appropriate backend per platform and config.

    Selection table (from FP-0017):

    | Platform       | Condition                     | Backend     |
    |----------------|-------------------------------|-------------|
    | macOS          | < 26 + sandbox-exec available | Seatbelt    |
    | macOS          | >= 26 (future)                | (deferred)  |
    | Linux          | kernel >= 5.13 + landlock pkg | Landlock    |
    | other / fallback | any                         | Noop        |

    #2983 — "available" here means ENFORCING, not merely present. Every selected
    backend is self-tested at resolution (a real subprocess through its own wrap,
    attempting a write that MUST be denied), and one that does not deny is
    treated exactly like one that is absent. Presence used to be the whole check,
    and all three sandbox layers passed it while enforcing nothing, so the knob
    below never fired on the failure that actually happened. Cost: one probe per
    process (cached on the backend name), paid only by a run that resolves a real
    backend — a run that never touches the sandbox never pays it.

    ``on_unsupported`` is applied whenever no real backend is available — when an
    explicit ``backend`` is forced-but-unavailable, (#1660) when ``backend="auto"``
    finds no platform backend (previously the auto path fell back to NoopBackend
    SILENTLY, ignoring the knob — so ``error`` was a no-op with the default
    backend), AND (#2983) when the selected backend fails its self-test. In all
    those cases:
    - ``on_unsupported == "warn"``:  log WARN and fall back to NoopBackend (default)
    - ``on_unsupported == "error"``: raise RuntimeError (fail-closed — refuse to run
      AI code unsandboxed; now works with ``backend="auto"`` too)
    - ``on_unsupported == "ignore"``: silently fall back to NoopBackend

    Backend modules are lazy-imported so missing sibling files degrade
    gracefully to NoopBackend without breaking module load.
    """
    # Lazy-import platform-specific backends so missing files don't break import.
    try:
        from .backends.seatbelt import SeatbeltBackend  # type: ignore[import]
    except ImportError:
        SeatbeltBackend = None  # type: ignore[misc,assignment]

    try:
        from .backends.landlock import LandlockBackend  # type: ignore[import]
    except ImportError:
        LandlockBackend = None  # type: ignore[misc,assignment]

    # Treat None config the same as SandboxConfig() defaults (= backend="auto").
    if config is None:
        backend_name = "auto"
        on_unsupported = "warn"
    else:
        backend_name = config.backend
        on_unsupported = config.on_unsupported

    if backend_name == "auto":
        # #1660: pass on_unsupported so the auto path applies it when no platform
        # backend is available (previously a SILENT NoopBackend fallback — the
        # selection-time silence + the broken fail-closed knob bug). The supported-
        # platform selection (Seatbelt/Landlock available) is unaffected.
        return _auto_select(SeatbeltBackend, LandlockBackend, on_unsupported)

    # Explicit backend requested — construct and check availability.
    if backend_name == "noop":
        return NoopBackend()

    if backend_name == "seatbelt":
        return _resolve_explicit(
            cls=SeatbeltBackend,
            name="seatbelt",
            on_unsupported=on_unsupported,
        )

    if backend_name == "landlock":
        return _resolve_explicit(
            cls=LandlockBackend,
            name="landlock",
            on_unsupported=on_unsupported,
        )

    # Should not be reached — SandboxConfig.__post_init__ rejects other values.
    return NoopBackend()


def _noop_with_policy(on_unsupported: str, detail: str) -> SandboxBackend:
    """#1660: apply ``sandbox.on_unsupported`` when no OS sandbox backend is
    available on the auto path. Previously the auto path fell back to NoopBackend
    SILENTLY (ignoring the knob) — so the selection was silent AND
    ``on_unsupported: error`` (the fail-closed knob) was a no-op with the default
    ``backend: auto``. Now unified with the explicit path:
    - ``error``  → RAISE (fail-closed: refuse to run AI code unsandboxed).
    - ``warn``   → LOUD log at selection + NoopBackend (default; not silent).
    - ``ignore`` → silent NoopBackend (explicit opt-in to silence).
    """
    if on_unsupported == "error":
        raise RuntimeError(
            f"No OS sandbox backend available ({detail}) and sandbox.on_unsupported="
            "'error' → refusing to run AI-generated code unsandboxed. Use a supported "
            "platform (macOS + sandbox-exec / Linux + landlock), or set "
            "sandbox.on_unsupported to 'warn' / 'ignore' to allow the NoopBackend fallback."
        )
    if on_unsupported == "warn":
        _logger.warning(
            "Sandbox: no OS enforcement backend available (%s) — AI-generated code "
            "(sandboxed_exec) will run UNSANDBOXED via NoopBackend. Set "
            "sandbox.on_unsupported: error to refuse, or use a supported platform "
            "(macOS + sandbox-exec / Linux + landlock).",
            detail,
        )
    return NoopBackend()


def _verify(cls: type | None, name: str) -> "tuple[SandboxBackend | None, str]":
    """Return ``(backend, "")`` when *cls* is importable, its mechanism is PRESENT,
    AND it actually fired a deny when self-tested; otherwise ``(None, reason)``.

    The single place "usable backend" is decided, for both the auto and the
    explicit path (#2983). It asks two questions where there used to be one:

    - ``available()`` — is the mechanism PRESENT (right OS, package, ABI)?
    - ``self_test()`` — does a deny actually FIRE on this host?

    The second question is the point. Presence was the only thing ever checked,
    and all three sandbox layers passed that check while enforcing nothing
    (#2962 / #2980 / #2978) — so ``on_unsupported``, the operator's fail-closed
    knob, could not fire on the failure mode that actually happened. A backend
    that cannot enforce is now treated exactly like one that is absent, because
    that is what it is.

    Also constructs the backend ONCE. The previous explicit path built one
    instance to ask `available()` and threw it away for a second — harmless when
    the answer was a cheap platform probe, wasteful now that answering costs a
    subprocess and caches its result.
    """
    if cls is None:
        return None, f"the {name} backend module is not importable in this checkout"

    backend: SandboxBackend = cls()
    if not backend.available():
        return None, f"the {name} enforcement mechanism is not present on this platform"

    reason = backend.self_test()
    if reason is not None:
        return None, (
            f"{name} is present but did NOT enforce when self-tested — {reason}"
        )
    return backend, ""


def _auto_select(
    SeatbeltBackend: type | None,  # noqa: N803
    LandlockBackend: type | None,  # noqa: N803
    on_unsupported: str = "warn",
) -> SandboxBackend:
    """Platform-aware auto-selection for backend="auto". #1660: when no platform
    backend is available, apply ``on_unsupported`` (via ``_noop_with_policy``) instead
    of a silent NoopBackend fallback. #2983: "available" now additionally requires
    that the backend fired a deny when self-tested (see :func:`_verify`), so a
    present-but-dead backend takes the same fallback as an absent one."""
    system = platform.system()

    if system == "Darwin":
        backend, reason = _verify(SeatbeltBackend, "seatbelt")
        if backend is not None:
            return backend
        return _noop_with_policy(on_unsupported, f"macOS: {reason}")

    if system == "Linux":
        backend, reason = _verify(LandlockBackend, "landlock")
        if backend is not None:
            return backend
        return _noop_with_policy(on_unsupported, f"Linux: {reason}")

    # FreeBSD, Windows, or anything else — no OS backend.
    return _noop_with_policy(on_unsupported, f"unsupported platform {system!r}")


def _resolve_explicit(
    cls: type | None,
    name: str,
    on_unsupported: str,
) -> SandboxBackend:
    """Resolve an explicitly-forced backend, applying the on_unsupported policy.

    #2983: "not available" now covers BOTH "the mechanism is absent" and "the
    mechanism is present but did not deny when self-tested" — see :func:`_verify`.
    """
    backend, reason = _verify(cls, name)
    if backend is not None:
        return backend

    if on_unsupported == "error":
        system = platform.system()
        raise RuntimeError(
            f"Sandbox backend {name!r} was requested but is not available "
            f"on this platform ({system}): {reason}. "
            f"Set sandbox.on_unsupported to 'warn' or 'ignore' to fall back "
            f"to NoopBackend, or choose a compatible backend."
        )
    if on_unsupported == "warn":
        system = platform.system()
        _logger.warning(
            "Sandbox backend %r is not available on %s (%s); falling back to "
            "NoopBackend. Set sandbox.on_unsupported: ignore to suppress this warning.",
            name,
            system,
            reason,
        )

    # "ignore" or "warn" — both fall back silently (warn already logged above).
    return NoopBackend()


__all__ = [
    "SandboxPolicy",
    "SandboxBackend",
    "SandboxResult",
    "WrappedCommand",
    "NoopBackend",
    "get_default_backend",
    "kill_process_tree",
]
