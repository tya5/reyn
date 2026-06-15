"""Sandboxed execution primitives (FP-0017).

Exports:
    SandboxPolicy   — declarative policy (what is allowed)
    SandboxBackend  — Protocol for backends (how it's enforced)
    SandboxResult   — return type of `backend.run()`
    NoopBackend     — default fallback (no isolation enforced)
    get_default_backend() — factory; platform-aware lazy auto-selection

Platform-specific backends live in `reyn.sandbox.backends.*` and are
lazy-imported so a missing sibling file (e.g. before Component B / C lands
in a given checkout) gracefully degrades to NoopBackend.
"""
from __future__ import annotations

import logging
import platform
from typing import TYPE_CHECKING

from .backend import SandboxBackend, SandboxResult
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

    ``on_unsupported`` is applied whenever no real backend is available — BOTH
    when an explicit ``backend`` is forced-but-unavailable AND (#1660) when
    ``backend="auto"`` finds no platform backend (previously the auto path fell
    back to NoopBackend SILENTLY, ignoring the knob — so ``error`` was a no-op
    with the default backend). In all those cases:
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


def _auto_select(
    SeatbeltBackend: type | None,  # noqa: N803
    LandlockBackend: type | None,  # noqa: N803
    on_unsupported: str = "warn",
) -> SandboxBackend:
    """Platform-aware auto-selection for backend="auto". #1660: when no platform
    backend is available, apply ``on_unsupported`` (via ``_noop_with_policy``) instead
    of a silent NoopBackend fallback."""
    system = platform.system()

    if system == "Darwin":
        if SeatbeltBackend is not None:
            candidate = SeatbeltBackend()
            if candidate.available():
                return candidate
        return _noop_with_policy(on_unsupported, "macOS without sandbox-exec / SeatbeltBackend")

    if system == "Linux":
        if LandlockBackend is not None:
            candidate = LandlockBackend()
            if candidate.available():
                return candidate
        return _noop_with_policy(on_unsupported, "Linux without landlock / LandlockBackend")

    # FreeBSD, Windows, or anything else — no OS backend.
    return _noop_with_policy(on_unsupported, f"unsupported platform {system!r}")


def _resolve_explicit(
    cls: type | None,
    name: str,
    on_unsupported: str,
) -> SandboxBackend:
    """Resolve an explicitly-forced backend, applying the on_unsupported policy."""
    unavailable = cls is None or not cls().available()
    if not unavailable:
        return cls()  # type: ignore[misc]

    if on_unsupported == "error":
        system = platform.system()
        raise RuntimeError(
            f"Sandbox backend {name!r} was requested but is not available "
            f"on this platform ({system}). "
            f"Set sandbox.on_unsupported to 'warn' or 'ignore' to fall back "
            f"to NoopBackend, or choose a compatible backend."
        )
    if on_unsupported == "warn":
        system = platform.system()
        _logger.warning(
            "Sandbox backend %r is not available on %s; falling back to NoopBackend. "
            "Set sandbox.on_unsupported: ignore to suppress this warning.",
            name,
            system,
        )

    # "ignore" or "warn" — both fall back silently (warn already logged above).
    return NoopBackend()


__all__ = [
    "SandboxPolicy",
    "SandboxBackend",
    "SandboxResult",
    "NoopBackend",
    "get_default_backend",
]
