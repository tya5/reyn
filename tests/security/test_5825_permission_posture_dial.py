"""Tier 1: Contract — the FP-0069 §2 permission posture dial's stage-1
surface (#5825 stage 1): ``reyn.security.permissions.posture``'s parsing,
total ordering, and the ``permissions.disable_unbounded_mode`` sticky-OR
merge in ``reyn.config.loader._merge``.

Scope, matching #5825 stage 1's own dispatch: the config KEY, the 3
values this stage accepts, their order, and the "remove unbounded by
config" seam. Wiring a mode's value into actual enforcement (a denied
capability staying denied in every mode; ``bounded``'s own boundary) is a
later stage's own test file, not this one's.
"""
from __future__ import annotations

import itertools

import pytest

from reyn.config.loader import _merge
from reyn.security.permissions.permissions import (
    KEY_DISABLE_UNBOUNDED_MODE,
    KEY_MODE,
    unknown_permissions_config_keys,
)
from reyn.security.permissions.posture import (
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODE_ORDER,
    BoundedModeNotImplementedError,
    PermissionMode,
    PermissionModeError,
    is_at_least_as_strict,
    parse_permission_mode,
    rank,
    resolve_permission_mode,
)

# ── parse_permission_mode ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("read_only", PermissionMode.READ_ONLY),
        ("ask", PermissionMode.ASK),
        ("unbounded", PermissionMode.UNBOUNDED),
    ],
)
def test_parse_permission_mode_accepts_all_3_stage_1_values(
    raw: str, expected: PermissionMode,
) -> None:
    """Tier 1: the 3 values #5825 stage 1 dispatches ("read_only / ask /
    unbounded の3値") each parse to their own distinct member."""
    assert parse_permission_mode(raw) is expected


def test_bounded_is_refused_not_silently_aliased_to_ask() -> None:
    """Tier 1: `bounded` is a REAL doc-named value (§2) this stage
    explicitly does not accept (§6 requires a boundary that does not
    exist yet, #5825 stage 2) — the dispatch's own explicit prohibition:
    "ask の別名として黙って受けるのは禁止". Must raise, never return
    PermissionMode.ASK."""
    with pytest.raises(BoundedModeNotImplementedError) as exc_info:
        parse_permission_mode("bounded")
    # #5825 stage 2 must be named in the message -- an operator hitting
    # this needs to know WHERE the real answer lives, not just that
    # "bounded" failed.
    assert "#5825" in str(exc_info.value)
    assert "stage 2" in str(exc_info.value)


def test_bounded_not_implemented_error_is_a_permission_mode_error() -> None:
    """Tier 1: a caller catching the general PermissionModeError (e.g. to
    refuse startup with one message) still catches the bounded-specific
    case -- BoundedModeNotImplementedError subclasses it."""
    assert issubclass(BoundedModeNotImplementedError, PermissionModeError)


@pytest.mark.parametrize("raw", ["yolo", "", "READ_ONLY", "Ask", "strict"])
def test_an_unrecognized_value_raises_the_generic_error(raw: str) -> None:
    """Tier 1: a typo or a value that never existed at all gets the
    generic error, distinct from `bounded`'s own specific one -- and the
    message names the valid set so `reyn config validate`'s own output
    is self-sufficient.

    Disclosed (six questions ④): the loop below would stay green over an
    EMPTY PERMISSION_MODE_ORDER too -- that vacuous case is killed by
    test_permission_mode_order_covers_every_enum_member's own membership
    invariant, not by a guard duplicated in this test."""
    with pytest.raises(PermissionModeError) as exc_info:
        parse_permission_mode(raw)
    assert not isinstance(exc_info.value, BoundedModeNotImplementedError)
    for mode in PERMISSION_MODE_ORDER:
        assert mode.value in str(exc_info.value)


# ── total ordering ───────────────────────────────────────────────────────


def test_permission_mode_order_covers_every_enum_member() -> None:
    """Tier 1: lead-coder BLOCKING — PERMISSION_MODE_ORDER is a hand-
    maintained tuple, separate from the PermissionMode enum it orders;
    nothing makes them drift together automatically. rank()'s own
    .index() means a member present in the enum but MISSING from the
    tuple raises ValueError in production the first time anything ranks
    it (e.g. stage 2 adding `bounded` to the enum and forgetting to
    extend this tuple).

    Deliberately does NOT pin the COUNT (3 today) — only that the two
    collections' MEMBERSHIP matches, so stage 2 adding a real 4th value
    to both together stays green; only a genuine update-miss (one
    changed, the other not) goes red."""
    assert set(PERMISSION_MODE_ORDER) == set(PermissionMode)
    assert len(PERMISSION_MODE_ORDER) == len(set(PermissionMode))


def test_the_order_is_total_every_pair_has_one_answer() -> None:
    """Tier 1: doc §2's own acceptance line — "Ordering is total and
    strict-to-permissive, so 'at least as strict as X' is expressible."
    Exhaustive over every pair PERMISSION_MODE_ORDER actually holds
    (3x3 = 9 today, NOT a number this test itself pins), not a sample:
    for every (a, b), "a at least as strict as b" and "b at least as
    strict as a" must never BOTH be false (a total order has an answer
    for every pair), and for a != b must never both be true (a total
    order is antisymmetric once equality is excluded).

    Disclosed (six questions ④): itertools.product over an EMPTY
    PERMISSION_MODE_ORDER iterates zero times and this loop's body never
    runs — that vacuous case is killed by
    test_permission_mode_order_covers_every_enum_member's own membership
    invariant, not by a guard duplicated here."""
    for a, b in itertools.product(PERMISSION_MODE_ORDER, repeat=2):
        a_vs_b = is_at_least_as_strict(a, b)
        b_vs_a = is_at_least_as_strict(b, a)
        assert a_vs_b or b_vs_a, f"no answer for the pair ({a!r}, {b!r})"
        if a is not b:
            assert not (a_vs_b and b_vs_a), f"both directions true for ({a!r}, {b!r})"


def test_read_only_is_the_strictest_and_unbounded_the_most_permissive() -> None:
    """Tier 1: the concrete order the doc's table lists, pinned directly
    (not inferred from the total-ordering test above, which only proves
    A total order exists -- this proves it is THIS one)."""
    assert rank(PermissionMode.READ_ONLY) < rank(PermissionMode.ASK) < rank(
        PermissionMode.UNBOUNDED
    )
    assert is_at_least_as_strict(PermissionMode.READ_ONLY, PermissionMode.UNBOUNDED)
    assert not is_at_least_as_strict(PermissionMode.UNBOUNDED, PermissionMode.READ_ONLY)


def test_a_mode_is_at_least_as_strict_as_itself() -> None:
    """Tier 1: reflexivity -- "at least as strict as" must include equal,
    per the phrase's own ordinary meaning (a floor of `ask` is satisfied
    BY `ask`, not only by something stricter).

    Disclosed (six questions ④): same vacuity risk as the two tests
    above -- an empty PERMISSION_MODE_ORDER would pass this loop too,
    and is killed by the same sibling membership invariant, not by a
    guard duplicated here."""
    for mode in PERMISSION_MODE_ORDER:
        assert is_at_least_as_strict(mode, mode)


# ── resolve_permission_mode ──────────────────────────────────────────────


def test_unset_mode_resolves_to_the_stated_default() -> None:
    """Tier 1: doc §7 (Migration) -- "Existing projects keep working with
    mode: ask" -- the one place this default is declared, and this test
    is what reads it (#5825 stage 1's own acceptance criterion)."""
    assert resolve_permission_mode(None, unbounded_disabled=False) is DEFAULT_PERMISSION_MODE
    assert DEFAULT_PERMISSION_MODE is PermissionMode.ASK


def test_unbounded_configured_and_allowed_stays_unbounded() -> None:
    """Tier 1: deny side of the lock -- when NOT disabled, `unbounded`
    resolves to itself, unchanged."""
    assert resolve_permission_mode(
        "unbounded", unbounded_disabled=False,
    ) is PermissionMode.UNBOUNDED


def test_unbounded_configured_but_disabled_falls_back_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: doc §8 -- "Removing unbounded by config is possible" — a
    disabled `unbounded` does not crash the session (a locked-down fleet's
    config should not refuse to start); it falls back to the same safe
    default an unset mode gets, and the fallback is disclosed via a
    WARNING naming what was refused (CLAUDE.md observability band)."""
    caplog.set_level("WARNING")
    resolved = resolve_permission_mode("unbounded", unbounded_disabled=True)
    assert resolved is DEFAULT_PERMISSION_MODE
    assert any("unbounded" in r.message and "disable_unbounded_mode" in r.message for r in caplog.records)


def test_a_genuinely_invalid_mode_still_raises_even_with_the_lock_off() -> None:
    """Tier 1: the disabled-fallback above is a SAFE-VALUE substitution,
    not a general "never raise" policy -- an unrecognized value (never a
    real mode to begin with) has no safe value to substitute, so it still
    raises regardless of the lock's state."""
    with pytest.raises(PermissionModeError):
        resolve_permission_mode("yolo", unbounded_disabled=False)
    with pytest.raises(BoundedModeNotImplementedError):
        resolve_permission_mode("bounded", unbounded_disabled=True)


# ── config-key recognition ───────────────────────────────────────────────


def test_mode_and_disable_unbounded_mode_are_recognized_permissions_keys() -> None:
    """Tier 1: #5825 stage 1's own new keys must not surface as
    "unrecognized key(s) (not applied)" -- the exact #5849-class defect
    this repo already closed once for every OTHER permissions.* key."""
    unknown = unknown_permissions_config_keys(
        {KEY_MODE: "ask", KEY_DISABLE_UNBOUNDED_MODE: True},
    )
    assert unknown == frozenset()


def test_a_genuine_typo_key_is_still_reported_unknown() -> None:
    """Tier 1: deny side -- adding the 2 new keys must not accidentally
    widen the registry into accepting everything."""
    unknown = unknown_permissions_config_keys({"mdoe": "ask"})
    assert unknown == frozenset({"mdoe"})


# ── the sticky-OR merge (disable_unbounded_mode "cannot be re-enabled") ──


def test_disable_unbounded_mode_set_by_an_earlier_tier_survives_a_later_tiers_silence() -> None:
    """Tier 1: doc §8 -- "a session cannot re-enable it". A later tier
    that never mentions the key at all (the common case: a project's
    reyn.yaml simply has no disable_unbounded_mode line) must not clear a
    TRUE an earlier tier set."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": True}})
    merged = _merge(merged, {"permissions": {"mode": "unbounded"}})
    assert merged["permissions"]["disable_unbounded_mode"] is True


def test_disable_unbounded_mode_survives_an_explicit_later_false() -> None:
    """Tier 1: the sharper case -- a LATER tier explicitly writing
    `disable_unbounded_mode: false` (an operator trying to undo the
    lock from a lower-authority file) must still not clear it. This is
    the concrete shape "a session cannot re-enable it" rules out."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": True}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": False}}, tier_label="project")
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": False}}, tier_label="project_local")
    assert merged["permissions"]["disable_unbounded_mode"] is True


def test_disable_unbounded_mode_survives_a_later_tier_writing_a_non_dict_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: #5825 stage 1 security co-vet (architect finding A,
    reproduced independently by lead-coder) -- the ORIGINAL bug this test
    guards: `permissions: "ask"` (a plausible typo now that stage 1
    nests `mode` UNDER this same `permissions:` key, instead of the
    correct `permissions: {mode: "ask"}`) used to fall through to
    `_merge`'s generic `result[key] = val` tail, replacing the WHOLE
    permissions dict -- lock included -- with the string, SILENTLY
    (downstream `_as_config_dict` defaults a non-dict `permissions:` to
    `{}` with only a WARNING, never a raise, so reyn still started with
    the lock gone and nobody told). The lock must survive; the malformed
    override itself must be loudly ignored, not silently applied as a
    mode or anything else."""
    caplog.set_level("WARNING")
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": True}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": "ask"}, tier_label="project_local")
    assert merged["permissions"]["disable_unbounded_mode"] is True
    assert isinstance(merged["permissions"], dict), (
        "a non-dict override must not replace the permissions dict wholesale"
    )
    assert any("permissions" in r.message for r in caplog.records), (
        "a malformed permissions: override must warn, not fail silently"
    )
    assert any("Did you mean" in r.message for r in caplog.records), (
        "a string override IS a plausible mode-value typo -- the hint should fire"
    )


def test_a_non_string_malformed_override_gets_no_broken_advice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: lead-coder BLOCKING (co-vet re-check) -- the "Did you mean
    'permissions: {mode: ...}'?" hint is only SAFE for a string value (a
    plausible dial-name typo). A non-string, non-dict value (e.g.
    `permissions: 3`) would suggest `{mode: 3}` -- YAML-valid, but
    `parse_permission_mode(3)` itself raises, turning a WARNING an
    operator follows into a startup failure. Never advise a fix that is
    itself broken."""
    caplog.set_level("WARNING")
    _merge({}, {"permissions": 3})
    assert any("permissions" in r.message for r in caplog.records)
    assert not any("Did you mean" in r.message for r in caplog.records), (
        "a non-string override must not get mode-value advice -- the "
        "suggested fix would itself fail to parse"
    )


def test_disable_unbounded_mode_survives_a_later_tier_writing_a_non_dict_list() -> None:
    """Tier 1: the SAME class of malformed override as the string case
    above, a different non-dict shape -- proves the fix is "any non-dict
    value", not a string-specific special case."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"disable_unbounded_mode": True}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": ["ask"]}, tier_label="project_local")
    assert merged["permissions"]["disable_unbounded_mode"] is True
    assert isinstance(merged["permissions"], dict)


def test_a_malformed_override_preserves_an_unrelated_prior_grant_too() -> None:
    """Tier 1: lead-coder BLOCKING (co-vet re-check) -- the fix's own
    behavior change is broader than the lock: a malformed
    `permissions:` override now preserves EVERY prior tier's grant, not
    just `disable_unbounded_mode` (the OLD code wholesale-replaced the
    dict with `{}`, losing everything). Only the lock has its own
    dedicated test above; without this one, a future change that goes
    back to "wipe to {} on malformed input" would leave
    disable_unbounded_mode's own tests green (nothing else was ever
    locked) while silently discarding every OTHER prior grant -- pin the
    broader claim directly, not just its one security-critical instance."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"network": "deny"}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": "ask"}, tier_label="project_local")
    assert merged["permissions"]["network"] == "deny"


def test_disable_unbounded_mode_off_by_default_stays_off_through_ordinary_merges() -> None:
    """Tier 1: deny side -- when NO tier ever sets the lock, it must not
    spuriously become True from the merge machinery itself (the sticky
    logic must trigger only on a genuine True somewhere in the chain)."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"mode": "ask"}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": {"file.write": "allow"}}, tier_label="project")
    assert not merged["permissions"].get("disable_unbounded_mode")


def test_other_permissions_keys_keep_ordinary_last_tier_wins_merge() -> None:
    """Tier 1: the sticky-OR exception is scoped to `disable_unbounded_mode`
    ONLY -- every other permissions.* key (mode included) still uses the
    ordinary override-wins merge, unaffected by this change."""
    merged: dict = {}
    merged = _merge(merged, {"permissions": {"mode": "ask"}}, tier_label="user_global")
    merged = _merge(merged, {"permissions": {"mode": "unbounded"}}, tier_label="project_local")
    assert merged["permissions"]["mode"] == "unbounded"
