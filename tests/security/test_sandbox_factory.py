"""Tier 2: get_default_backend() auto-selection + SandboxConfig invariants (FP-0017).

Verifies:
- SandboxConfig dataclass defaults and validation.
- get_default_backend() auto-selection per platform (Darwin / Linux / other).
- Explicit backend forcing + on_unsupported policy (warn / error / ignore).
- None config behaves identically to SandboxConfig() defaults.
- Any returned backend conforms to the SandboxBackend Protocol.

No mocks of collaborators — monkeypatch platform.system() where needed;
use real SandboxConfig and real NoopBackend instances.

★ CI-visibility gap (#3881, owner ruling 2026-09-04: no macOS CI runner will
be added — every job in this repo's CI runs on ubuntu-latest): 1 of this
module's 38 collected tests is genuinely Darwin-gated (`skipif(sys.platform
!= "darwin", ...)`) and has therefore NEVER executed in CI — the sibling
Linux-gated test right next to it (`skipif(sys.platform != "linux", ...)`)
DOES run, so this module's own green is mostly real signal; only that ONE
test's green means "never ran", not "passed".
"""
from __future__ import annotations

import logging
import sys

import pytest

from reyn.config import SandboxConfig
from reyn.security.sandbox import NoopBackend, SandboxBackend, get_default_backend
from reyn.security.sandbox import noop_backend as _noop_module

# ─── 1. SandboxConfig dataclass ───────────────────────────────────────────────


def test_default_backend_and_on_unsupported_values():
    """Tier 1: :class:`SandboxConfig`'s ``backend``/``on_unsupported`` defaults
    are asserted HERE ONLY (grep-confirmed, #3878 co-vet) — no other test in
    the suite pins these two values, and a sample-config doc comment
    elsewhere describes them without a seam test of its own. If either
    dataclass default silently drifted, nothing else would catch it, so this
    is the sole witness of a doc-vs-code default-value contract, not a
    redundant transcription.

    ``mode``'s default is deliberately NOT asserted here — that one IS
    redundant: ``test_yaml_parse_defaults_mode_to_compat_when_absent``
    (below) already pins it through the real ``reyn.yaml -> SandboxConfig``
    seam, a stronger witness than the bare dataclass (#3878 Phase 3
    correction: the original three-assert form declared Tier 2 for all
    three, but only ``mode`` had a genuine duplicate)."""
    cfg = SandboxConfig()
    assert cfg.backend == "auto"
    assert cfg.on_unsupported == "warn"


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


def test_strict_mode_default_yields_to_an_explicit_operator_allow() -> None:
    """Tier 2: #3823's spec headline — "mode decides only the DEFAULT for an
    axis left unset; mode never decides DIRECTION, an explicit operator write
    always wins" — has no witness (#3957, architect co-vet on #3953, PR
    comment 5231152103). Every existing strict-mode test (including
    ``test_config_accepts_strict_mode_and_resolves_it`` immediately above)
    only ever calls ``resolve_sandbox_policy`` with an EMPTY/absent
    ``config_policy``, so ``explicit`` is always ``{}`` and the
    ``if key not in explicit`` branch at ``policy.py``'s
    ``resolve_sandbox_policy`` is never taken with a non-empty ``explicit`` —
    rewriting it to ``if True`` still passes every test that existed before
    this one.

    ⚠️ ``if key not in explicit: -> if True:`` (#3957's own originally-named
    falsify target) is NOT a working falsify recipe for the two tests below
    either — it is a NO-OP against the current code, verified directly: the
    unconditional ``floor.update(explicit)`` right after the strict loop
    already makes ``explicit`` win regardless of that inner ``if``, so both
    new tests stay green under that exact mutation. The recipe that DOES
    isolate each test: (1) strict leg — move ``floor.update(explicit)`` to
    run BEFORE the strict-mode loop instead of after, with the loop then
    unconditionally overwriting; (2) compat leg — scope
    ``floor.update(explicit)`` inside the ``if mode == "strict":`` block so
    an explicit write is silently dropped under compat. Each isolates to
    exactly the one test naming that leg.

    strict leg: an operator who explicitly writes ``network: true`` under
    ``mode: strict`` gets network ON — the strict default (off) applies only
    to axes the operator left UNSET, never overriding an axis they wrote."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    resolved = resolve_sandbox_policy(
        {"network": True}, write_paths=["/repo"], mode="strict"
    )
    assert resolved["network"] is True


def test_compat_mode_still_respects_an_explicit_operator_deny() -> None:
    """Tier 2: #3823 — companion to the strict-leg test above, closing the
    other direction (#3957's explicit two-direction requirement: a
    single-direction test alone stays green under an implementation that
    only consults ``explicit`` when ``mode == "strict"``, which is a
    narrower — and wrong — reading of the spec's "mode never decides
    direction" promise).

    compat leg: an operator who explicitly denies subprocess under
    ``mode: compat`` (compat's own dataclass default allows it) still gets
    subprocess denied — an explicit write is honored under EITHER mode, not
    just strict."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    resolved = resolve_sandbox_policy(
        {"subprocess": False}, write_paths=["/repo"], mode="compat"
    )
    assert resolved["deny_subprocess"] is True


def test_config_no_longer_raises_on_an_unknown_policy_key() -> None:
    """Tier 2: #4174 T0 (owner ruling — "no hard-fail anywhere, don't
    special-case sandbox.policy") supersedes #3823's original fail-loud
    posture asserted by this test's predecessor
    (test_config_rejects_an_unknown_policy_key_rather_than_dropping_it).
    Construction no longer raises — the unknown key is caught by the
    unified #4174 T0 warn-not-fail path instead (see
    test_unknown_config_keys_flags_an_unknown_sandbox_policy_key below for
    that half). This test's own job: prove the RELAXATION — a key that used
    to crash construction no longer does."""
    cfg = SandboxConfig(policy={"unknown_totally_made_up_key": True})
    assert cfg.policy == {"unknown_totally_made_up_key": True}


def test_config_silently_filters_an_unknown_policy_key_at_resolve_time() -> None:
    """Tier 2: #4174 T0 accept-side — a known, well-formed policy key
    resolves normally and an unknown key alongside it is dropped without
    raising (the warn already happened at config-load time, upstream of
    this call — see _translate_sandbox_policy_config's docstring)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(
        policy={"network": False, "unknown_totally_made_up_key": True}
    )
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["network"] is False
    assert "unknown_totally_made_up_key" not in resolved


def test_config_clamps_a_default_timeout_above_the_default_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #3903① / #4174 T0 — with max_timeout_seconds left unset
    (defaults to 600, DEFAULT_MAX_EXEC_TIMEOUT_SECONDS), an
    operator-declared timeout_seconds above it no longer raises
    (superseding this test's predecessor,
    test_config_rejects_a_default_timeout_above_the_default_cap) — it WARNS
    and clamps the effective default down to the max, per the owner's
    blanket no-hard-fail ruling (folded in per architect's explicit flag
    that this raise would otherwise be the one hard-fail left standing)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(policy={"timeout_seconds": 601})
    with caplog.at_level(logging.WARNING):
        resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["timeout_seconds"] == 600
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "timeout_seconds" in m and "max_timeout_seconds" in m for m in messages
    ), f"expected a warning naming both values, got: {messages!r}"


def test_config_accepts_a_timeout_exactly_at_the_default_cap() -> None:
    """Tier 2: #3903① — the check is inclusive: exactly
    DEFAULT_MAX_EXEC_TIMEOUT_SECONDS (600) is a valid operator
    timeout_seconds value when max_timeout_seconds is left unset, not
    rejected."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(policy={"timeout_seconds": 600})
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["timeout_seconds"] == 600


def test_config_max_timeout_seconds_is_operator_settable_below_the_default() -> None:
    """Tier 2: #3903① — architect's conditional-approval requirement: the
    LLM's timeout ceiling is the OPERATOR's own configured
    max_timeout_seconds, never a hardcoded 600 — an operator narrowing it
    below the 600 default must actually take effect (this is the whole
    point of the ruling: a hardcoded LLM ceiling would let the LLM widen
    an operator's own envelope, a Security pass-line violation)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(policy={"timeout_seconds": 30, "max_timeout_seconds": 90})
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["timeout_seconds"] == 30
    assert resolved["max_timeout_seconds"] == 90


def test_config_clamps_default_timeout_above_an_explicit_lower_max(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #3903① / #4174 T0 — the self-consistency check reads BOTH
    operator-declared values, not just timeout_seconds against the 600
    default: a default of 100 against an explicitly-lowered max of 90 warns
    and clamps to 90, no longer raises (supersedes
    test_config_rejects_default_timeout_above_an_explicit_lower_max)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(policy={"timeout_seconds": 100, "max_timeout_seconds": 90})
    with caplog.at_level(logging.WARNING):
        resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["timeout_seconds"] == 90
    messages = [r.getMessage() for r in caplog.records]
    assert any("max_timeout_seconds" in m for m in messages), messages


def test_unknown_config_keys_flags_an_unknown_sandbox_policy_key() -> None:
    """Tier 2: #4174 T0 — the unified unknown-key walk
    (config_schema.unknown_config_keys), not SandboxConfig construction, is
    now where an unknown sandbox.policy key surfaces (see
    test_config_no_longer_raises_on_an_unknown_policy_key above for the
    construction-side half of this same change). A totally unrecognized
    key gets a None hint (no rename to point to)."""
    from reyn.config import config_schema

    unknown = config_schema.unknown_config_keys(
        {"sandbox": {"policy": {"unknown_totally_made_up_key": True}}}
    )
    assert unknown == {"sandbox.policy.unknown_totally_made_up_key": None}


def test_config_background_timeout_defaults_independently_of_foreground() -> None:
    """Tier 2: #3903 a-2 — background_timeout_seconds/background_max_timeout_seconds
    resolve to their OWN dataclass defaults (3600 / None) when the operator
    sets only the foreground pair — the single-shared-field shape #3903's
    issue body named as the problem ("一時セッション内でも同じ 60 秒") is gone:
    setting foreground's timeout_seconds must not also move background's.
    Constructs the real ``SandboxPolicy(**resolved)`` the op handler builds
    (``sandboxed_exec.py``), not just the delta dict ``resolve_sandbox_policy``
    returns — the delta is empty for an unset key by design (only explicit
    writes are represented), so the field-level default only shows up once
    the dataclass is actually constructed from it."""
    from reyn.security.sandbox.policy import SandboxPolicy, resolve_sandbox_policy

    cfg = SandboxConfig(policy={"timeout_seconds": 30})
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["timeout_seconds"] == 30
    assert "background_timeout_seconds" not in resolved, (
        "an unset operator key must not appear in the delta dict at all"
    )
    policy = SandboxPolicy(**resolved)
    assert policy.timeout_seconds == 30
    assert policy.background_timeout_seconds == 3600, (
        "background_timeout_seconds must resolve to its OWN default (3600), "
        "not be dragged by the foreground override"
    )
    assert policy.background_max_timeout_seconds is None


def test_config_background_max_timeout_seconds_operator_settable() -> None:
    """Tier 2: #3903 a-2 ③ — an operator CAN configure a real background
    ceiling (overriding the None/"no cap" default) — the field is `int |
    None`, not `None`-only. Registered now that ③ gives these keys a real
    reader (sandboxed_exec.py's ctx.ephemeral branch)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(
        policy={"background_timeout_seconds": 300, "background_max_timeout_seconds": 900}
    )
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["background_timeout_seconds"] == 300
    assert resolved["background_max_timeout_seconds"] == 900


def test_config_clamps_background_default_above_an_explicit_background_max(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #3903 a-2 ③ — the default<=max self-consistency check applies
    to the background pair too, matching #4174 T0's warn+clamp posture
    (not the raise #4186's own first draft used, since T0 landed on main
    between #4186 and this PR — see test_config_clamps_default_timeout_above_an_explicit_lower_max
    above, the foreground sibling this mirrors)."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(
        policy={"background_timeout_seconds": 1000, "background_max_timeout_seconds": 900}
    )
    with caplog.at_level(logging.WARNING):
        resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["background_timeout_seconds"] == 900
    messages = [r.getMessage() for r in caplog.records]
    assert any("background_max_timeout_seconds" in m for m in messages), messages


def test_config_background_max_timeout_seconds_none_skips_the_consistency_check() -> None:
    """Tier 2: #3903 a-2 ③ — an explicit background_max_timeout_seconds=None
    (the "no cap" case, the actual default) must not warn/clamp regardless
    of how large background_timeout_seconds is — there is no ceiling to
    exceed."""
    from reyn.security.sandbox.policy import resolve_sandbox_policy

    cfg = SandboxConfig(
        policy={"background_timeout_seconds": 999999, "background_max_timeout_seconds": None}
    )
    resolved = resolve_sandbox_policy(cfg.policy, write_paths=[], mode=cfg.mode)
    assert resolved["background_timeout_seconds"] == 999999


def test_unknown_config_keys_flags_a_renamed_sandbox_policy_key_with_guidance() -> None:
    """Tier 2: #3823 / #4174 T0 — an operator on a pre-#3823 (or pre-#3901)
    config who still writes an OLD internal-vocabulary key (`write_paths`,
    the pre-#3823 name for `allow_write_paths`) gets the RENAMED-KEY
    guidance naming the new key via the unified walk (supersedes
    test_config_guides_an_old_internal_vocabulary_key_by_name_not_as_unknown,
    which asserted this through a SandboxConfig-construction raise that
    #4174 T0 removed) — a real witness for `_RENAMED_SANDBOX_POLICY_KEYS`
    reaching the new mechanism, not a stale/dropped registration."""
    from reyn.config import config_schema

    unknown = config_schema.unknown_config_keys(
        {"sandbox": {"policy": {"write_paths": ["/x"]}}}
    )
    hint = unknown["sandbox.policy.write_paths"]
    assert hint is not None and "allow_write_paths" in hint.note, (
        f"expected the renamed-key guidance naming 'allow_write_paths', got: {hint!r}"
    )
    # #4174 T0 (lead-coder's block on #4190): a sandbox.policy rename's
    # guidance describes a per-key VALUE TRANSFORM, not a plain
    # relocation — `destination` must stay None so `reyn config migrate`
    # never guesses at the transform.
    assert hint.destination is None


def test_unknown_config_keys_is_silent_for_a_well_formed_sandbox_policy() -> None:
    """Tier 2: #4174 T0 accept-side (lead-coder's explicit false-positive-noise
    concern — a correctly-configured section must never be flagged, or the
    warning as a whole becomes something operators learn to ignore). A
    policy using only real config-vocabulary keys across the whole nested
    sandbox.* section produces zero unknown-key hits."""
    from reyn.config import config_schema

    unknown = config_schema.unknown_config_keys({
        "sandbox": {
            "backend": "auto",
            "mode": "strict",
            "policy": {
                "network": False,
                "subprocess": True,
                "allow_write_paths": ["/repo"],
                "deny_read_paths": ["~/.ssh"],
                "timeout_seconds": 30,
                "max_timeout_seconds": 90,
            },
        },
    })
    assert unknown == {}


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
