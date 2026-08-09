"""Tier 2: get_default_backend() auto-selection + SandboxConfig invariants (FP-0017).

Verifies:
- SandboxConfig dataclass defaults and validation.
- get_default_backend() auto-selection per platform (Darwin / Linux / other).
- Explicit backend forcing + on_unsupported policy (warn / error / ignore).
- None config behaves identically to SandboxConfig() defaults.
- Any returned backend conforms to the SandboxBackend Protocol.

No mocks of collaborators — monkeypatch platform.system() where needed;
use real SandboxConfig and real NoopBackend instances.
"""
from __future__ import annotations

import logging
import sys

import pytest

from reyn.config import SandboxConfig
from reyn.security.sandbox import NoopBackend, SandboxBackend, get_default_backend
from reyn.security.sandbox import noop_backend as _noop_module

# ─── 1. SandboxConfig dataclass ───────────────────────────────────────────────


def test_default_config_values():
    """Tier 2: SandboxConfig() defaults to backend='auto', on_unsupported='warn',
    mode='compat' (#3823 ②, owner-ruled "A")."""
    cfg = SandboxConfig()
    assert cfg.backend == "auto"
    assert cfg.on_unsupported == "warn"
    assert cfg.mode == "compat"


def test_config_rejects_invalid_mode():
    """Tier 2: #3823 — SandboxConfig with unknown mode raises ValueError listing
    the allowed set. Neither "off" nor "custom" are allowed — owner ruling:
    "off" is expressible as 'compat' with every axis at its compat default;
    "custom" was never a third DIRECTION, it was the symptom of mode and
    policy: not having a defined composition rule (#3823's resolution
    algorithm — mode decides only the default for an axis left unset —
    removes the need for it)."""
    with pytest.raises(ValueError, match="sandbox.mode") as exc_info:
        SandboxConfig(mode="off")
    msg = str(exc_info.value)
    assert "off" in msg
    for allowed in ("compat", "strict"):
        assert allowed in msg
    assert "custom" not in msg


def test_config_accepts_compat_mode():
    """Tier 2: #3823 — 'compat' round-trips byte-identical (no normalization)."""
    assert SandboxConfig(mode="compat").mode == "compat"


def test_config_accepts_strict_mode_and_resolves_it() -> None:
    """Tier 2: #3823 — 'strict' is now WIRED (was: raised "not implemented
    yet" — the earlier #3823 ② stub). Real end-to-end: a SandboxConfig with
    mode='strict' validates, AND resolve_sandbox_policy actually applies the
    strict defaults (network off, subprocess denied, env allow-list empty) —
    not just "the enum accepts the string"."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(mode="strict")
    assert cfg.mode == "strict"

    resolved = resolve_sandbox_policy(cfg.policy, write_paths=["/repo"], mode=cfg.mode)
    assert resolved["network"] is False
    assert resolved["deny_subprocess"] is True
    assert resolved["allow_env_names"] == []
    # write is UNAFFECTED by mode — stays the caller-supplied floor, not
    # emptied (lead-coder's #3823 co-vet correction: zeroing it would also
    # block writing to the op's own workspace).
    assert resolved["write_paths"] == ["/repo"]


def test_config_rejects_an_unknown_policy_key_rather_than_dropping_it() -> None:
    """Tier 2: #3823 — the config-vocabulary translation layer must FAIL LOUD
    on a key it does not recognize, not silently drop it. lead-coder's
    explicit requirement: for a security deny-list, a dropped-not-rejected
    unknown key (e.g. a typo'd `deny_subprocess` under the new vocabulary)
    reads as "nothing to deny" — a fail-OPEN regression. This asserts the
    load FAILS (ValueError), not that some default gets returned."""
    with pytest.raises(ValueError, match="unknown_totally_made_up_key"):
        SandboxConfig(policy={"unknown_totally_made_up_key": True})


def test_yaml_parse_defaults_mode_to_compat_when_absent():
    """Tier 2: #3823 ② — a `sandbox:` YAML block that omits `mode` parses to
    the compat default, not an error or a None. Real parser, not a hand-built
    SandboxConfig, since this is the actual reyn.yaml -> SandboxConfig seam."""
    from reyn.config.infra import _build_sandbox_config

    cfg = _build_sandbox_config({"backend": "noop"})
    assert cfg.mode == "compat"


def test_yaml_parse_honors_an_explicit_compat_mode():
    """Tier 2: #3823 ② — an operator-declared `sandbox: {mode: compat}` reaches
    SandboxConfig.mode unchanged (the one mode actually wired/accepted today)."""
    from reyn.config.infra import _build_sandbox_config

    cfg = _build_sandbox_config({"mode": "compat"})
    assert cfg.mode == "compat"


def test_yaml_parse_honors_an_explicit_strict_mode():
    """Tier 2: #3823 — an operator-declared `sandbox: {mode: strict}` parses
    through the real reyn.yaml -> SandboxConfig seam (was: raised "not
    implemented yet" through this exact seam — #3823's own prior test).
    strict is now real, exercised through the actual YAML-parse path, not
    just the dataclass directly."""
    from reyn.config.infra import _build_sandbox_config

    cfg = _build_sandbox_config({"mode": "strict"})
    assert cfg.mode == "strict"


def test_config_rejects_invalid_backend():
    """Tier 2: SandboxConfig with unknown backend raises ValueError listing allowed set."""
    with pytest.raises(ValueError, match="sandbox.backend") as exc_info:
        SandboxConfig(backend="docker")
    msg = str(exc_info.value)
    # Must name the bad value and the allowed set.
    assert "docker" in msg
    for allowed in ("auto", "seatbelt", "landlock", "noop"):
        assert allowed in msg


def test_config_rejects_invalid_on_unsupported():
    """Tier 2: SandboxConfig with unknown on_unsupported raises ValueError listing allowed set."""
    with pytest.raises(ValueError, match="sandbox.on_unsupported") as exc_info:
        SandboxConfig(on_unsupported="explode")
    msg = str(exc_info.value)
    assert "explode" in msg
    for allowed in ("warn", "error", "ignore"):
        assert allowed in msg


def test_valid_combinations_do_not_raise():
    """Tier 2: all documented backend/on_unsupported combos construct without error."""
    for backend in ("auto", "seatbelt", "landlock", "noop"):
        for policy in ("warn", "error", "ignore"):
            cfg = SandboxConfig(backend=backend, on_unsupported=policy)
            assert cfg.backend == backend
            assert cfg.on_unsupported == policy


# ─── 2. Platform auto-selection ───────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific test")
def test_auto_on_macos_picks_seatbelt_when_available():
    """Tier 2: on Darwin, auto-select returns SeatbeltBackend when available(), else Noop."""
    try:
        from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend  # type: ignore[import]
        seatbelt_cls = SeatbeltBackend
    except ImportError:
        seatbelt_cls = None

    result = get_default_backend(SandboxConfig(backend="auto"))

    if seatbelt_cls is not None and seatbelt_cls().available():
        assert result.name == "seatbelt", (
            f"Expected SeatbeltBackend on Darwin but got {result.name!r}"
        )
    else:
        # SeatbeltBackend not importable or not available — documented fallback.
        assert result.name == "noop"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-specific test")
def test_auto_on_linux_picks_landlock_when_available():
    """Tier 2: on Linux, auto-select returns LandlockBackend when available(), else Noop."""
    try:
        from reyn.security.sandbox.backends.landlock import LandlockBackend  # type: ignore[import]
        landlock_cls = LandlockBackend
    except ImportError:
        landlock_cls = None

    result = get_default_backend(SandboxConfig(backend="auto"))

    if landlock_cls is not None and landlock_cls().available():
        assert result.name == "landlock", (
            f"Expected LandlockBackend on Linux but got {result.name!r}"
        )
    else:
        # landlock pkg not installed or kernel < 5.13 — documented fallback.
        assert result.name == "noop"


def test_auto_on_unknown_platform_returns_noop(monkeypatch):
    """Tier 2: auto-selection falls back to NoopBackend on non-Darwin, non-Linux platforms."""
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    result = get_default_backend(SandboxConfig(backend="auto"))
    assert result.name == "noop"
    assert isinstance(result, NoopBackend)


# ─── #1660: the auto path honors on_unsupported (was silent / fail-closed broken) ──


def test_auto_unsupported_error_raises(monkeypatch):
    """Tier 2: #1660 (the bug-fix) — backend='auto' + on_unsupported='error' on a
    platform with NO OS sandbox RAISES (fail-closed). Previously the auto path
    ignored on_unsupported → the fail-closed knob was a silent no-op with the default
    backend, so AI code ran unsandboxed even when the operator asked to refuse."""
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    with pytest.raises(RuntimeError, match="No OS sandbox backend available"):
        get_default_backend(SandboxConfig(backend="auto", on_unsupported="error"))


def test_auto_unsupported_warn_is_loud_at_selection(monkeypatch, caplog):
    """Tier 2: #1660 — backend='auto' + on_unsupported='warn' (default) → NoopBackend
    AND a WARN logged AT SELECTION (not silent — the operator is told upfront that AI
    exec will run unsandboxed, vs the prior selection-time silence)."""
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        result = get_default_backend(SandboxConfig(backend="auto", on_unsupported="warn"))
    assert isinstance(result, NoopBackend)
    assert any("UNSANDBOXED" in r.message for r in caplog.records), (
        f"Expected a loud selection-time WARN; got: {[r.message for r in caplog.records]}"
    )


def test_auto_unsupported_ignore_is_silent(monkeypatch, caplog):
    """Tier 2: #1660 — on_unsupported='ignore' → NoopBackend with NO selection-time
    warn (explicit opt-in to silence)."""
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        result = get_default_backend(SandboxConfig(backend="auto", on_unsupported="ignore"))
    assert isinstance(result, NoopBackend)
    assert not any("UNSANDBOXED" in r.message for r in caplog.records)


def test_auto_unsupported_does_not_fire_when_backend_available(monkeypatch):
    """Tier 2: #1660 regression guard — on a SUPPORTED platform the policy is NOT
    consulted: auto returns the real backend even with on_unsupported='error' (no
    spurious raise). The policy applies ONLY on the no-backend fallback."""
    from reyn.security.sandbox import _auto_select

    monkeypatch.setattr("platform.system", lambda: "Linux")

    class _FakeLandlock:
        name = "landlock"

        def available(self) -> bool:
            return True

        def self_test(self) -> str | None:
            # #2983: a healthy backend — the mechanism is present AND a deny
            # actually fired. Both must hold for selection; this fake asserts the
            # policy is not consulted when they do.
            return None

    # Backend available + enforcing ⇒ returned, even with on_unsupported='error'.
    result = _auto_select(None, _FakeLandlock, "error")
    assert result.name == "landlock"


# ─── 3. Explicit backend forcing ──────────────────────────────────────────────


def test_force_noop_returns_noop_unconditionally():
    """Tier 2: backend='noop' always returns NoopBackend regardless of platform."""
    result = get_default_backend(SandboxConfig(backend="noop"))
    assert isinstance(result, NoopBackend)
    assert result.name == "noop"


def test_force_seatbelt_on_linux_warn_falls_back_to_noop(monkeypatch, caplog):
    """Tier 2: forcing seatbelt on Linux with on_unsupported='warn' → Noop + WARN logged."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    # Ensure SeatbeltBackend is not importable in this path (simulate missing sibling).
    # The monkeypatched platform.system="Linux" makes SeatbeltBackend.available()
    # return False if it is importable (correct cross-platform behaviour), so
    # we rely on that — no need to patch imports.
    _noop_module._reset_warning_for_tests()

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        result = get_default_backend(SandboxConfig(backend="seatbelt", on_unsupported="warn"))

    assert result.name == "noop"
    assert any("seatbelt" in r.message.lower() for r in caplog.records), (
        f"Expected a WARN mentioning 'seatbelt'; got: {[r.message for r in caplog.records]}"
    )


def test_force_seatbelt_on_linux_error_raises(monkeypatch):
    """Tier 2: forcing seatbelt on Linux with on_unsupported='error' raises RuntimeError."""
    monkeypatch.setattr("platform.system", lambda: "Linux")

    with pytest.raises(RuntimeError) as exc_info:
        get_default_backend(SandboxConfig(backend="seatbelt", on_unsupported="error"))

    msg = str(exc_info.value)
    assert "seatbelt" in msg.lower()
    # Message must also identify the platform or give enough context.
    assert "Linux" in msg or "not available" in msg


def test_force_seatbelt_on_linux_ignore_silently_falls_back(monkeypatch, caplog):
    """Tier 2: forcing seatbelt on Linux with on_unsupported='ignore' → Noop, no WARN."""
    monkeypatch.setattr("platform.system", lambda: "Linux")
    _noop_module._reset_warning_for_tests()

    with caplog.at_level(logging.WARNING, logger="reyn.security.sandbox"):
        result = get_default_backend(SandboxConfig(backend="seatbelt", on_unsupported="ignore"))

    assert result.name == "noop"
    # No WARNING about the backend choice should appear.
    warn_records = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "seatbelt" in r.message.lower()
    ]
    assert warn_records == [], (
        f"Expected no WARN about seatbelt when on_unsupported='ignore'; "
        f"got: {[r.message for r in warn_records]}"
    )


# ─── 4. None config / default equivalence ─────────────────────────────────────


def test_none_config_behaves_like_default_auto(monkeypatch):
    """Tier 2: get_default_backend(None) and get_default_backend(SandboxConfig()) agree."""
    # Pin platform so both calls see the same environment.
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")

    result_none = get_default_backend(None)
    result_default = get_default_backend(SandboxConfig())
    assert result_none.name == result_default.name


# ─── 5. Protocol conformance ──────────────────────────────────────────────────


def test_backend_conforms_to_protocol(monkeypatch):
    """Tier 2: get_default_backend() always returns a SandboxBackend Protocol instance."""
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    result = get_default_backend(SandboxConfig(backend="auto"))
    assert isinstance(result, SandboxBackend), (
        f"{result!r} does not conform to the SandboxBackend Protocol"
    )

    noop = get_default_backend(SandboxConfig(backend="noop"))
    assert isinstance(noop, SandboxBackend)
