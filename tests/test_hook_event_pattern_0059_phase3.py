"""Tests for the Hook-Event Redesign Phase 3 — the ``EventPattern`` match
grammar (proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md``
§10 Q-reyn-4).

Coverage plan
-------------
Tier 1 (contract): ``EventPattern``/``matches`` — the pure predicate,
  including the byte-identical-backward-compat cross-check against the
  pre-Phase-3 ``reyn.hooks.matcher.matches`` for every payload-only case
  (empty/None, exact, glob, absent-field, multi-field), plus the NEW
  kind/source predicates.
Tier 1 (contract): ``validate_against_schema`` — the NEW static-validation
  capability (a typo'd payload field is flagged; a schema-conformant one
  passes; an unknown/future kind is a silent no-op).
Tier 2 (OS invariant, dispatcher-unit): ``HookDispatcher.dispatch`` — driving
  the REAL dispatcher through the EventPattern path and confirming its
  fire/skip decisions are unchanged from ``test_2608_h2_hook_matcher.py``
  (that suite exercises the dispatcher directly and must stay green
  unmodified — this file adds the EventPattern-specific coverage only).
Strip-falsify: breaking the payload predicate's absent-field semantics flips
  the backward-compat invariant to RED; restoring goes GREEN.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.hooks import event_pattern
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern, from_legacy_matcher, validate_against_schema
from reyn.hooks.loader import load_hooks
from reyn.hooks.matcher import matches as legacy_matches
from reyn.hooks.schema import HookConfigError
from reyn.hooks.schema_registry import HookSchemaError, canonical_kind

# ---------------------------------------------------------------------------
# Recording seam (mirrors test_2608_h2_hook_matcher.py's _Recorder)
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable — generic seam stand-in for
    ``put_inbox``/``stage_next_turn_context`` (accepts any args/kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _mcp_vars(*, server: str = "github", uri: str = "file:///repo/a.txt") -> dict:
    return {
        "point": "mcp_resource_updated",
        "server": server,
        "uri": uri,
        "agent_name": "test-agent",
        "resync": False,
    }


def _mcp_event(**kw) -> HookEvent:
    return HookEvent(kind=canonical_kind("mcp_resource_updated"), payload=_mcp_vars(**kw))


# ===========================================================================
# Tier 1 — Contract: byte-identical backward-compat (payload-only EventPattern
# vs the legacy reyn.hooks.matcher.matches, same table of cases)
# ===========================================================================


@pytest.mark.parametrize(
    ("legacy_matcher", "template_vars"),
    [
        (None, {}),
        (None, _mcp_vars()),
        ({}, _mcp_vars()),
        ({"server": "github"}, _mcp_vars(server="github")),
        ({"server": "github"}, _mcp_vars(server="gitlab")),
        ({"server": "git*"}, _mcp_vars(server="github")),  # no glob for non uri/path fields
        ({"uri": "file:///repo/**"}, _mcp_vars(uri="file:///repo/a.txt")),
        ({"uri": "file:///repo/*.txt"}, _mcp_vars(uri="file:///other/a.txt")),
        ({"server": "github", "uri": "file:///repo/**"},
         _mcp_vars(server="github", uri="file:///repo/a.txt")),
        ({"server": "github", "uri": "file:///repo/**"},
         _mcp_vars(server="github", uri="file:///other/a.txt")),
        ({"server": "github"}, {"point": "turn_end"}),  # absent field -> never match
    ],
)
def test_payload_only_event_pattern_matches_the_same_as_legacy_matcher(
    legacy_matcher, template_vars,
) -> None:
    """Tier 1: for every case the pre-Phase-3 ``matcher.matches`` decides, the
    EventPattern generalization (kind/source unset) decides IDENTICALLY."""
    event = HookEvent(kind=canonical_kind("mcp_resource_updated"), payload=template_vars)
    legacy_decision = legacy_matches(legacy_matcher, template_vars)
    pattern_decision = event_pattern.matches(from_legacy_matcher(legacy_matcher), event)
    assert pattern_decision == legacy_decision


def test_none_pattern_always_matches() -> None:
    """Tier 1: a ``None`` EventPattern (no pattern at all) always matches —
    generalizes the legacy None/empty-matcher default."""
    assert event_pattern.matches(None, _mcp_event()) is True


def test_default_event_pattern_always_matches() -> None:
    """Tier 1: an ``EventPattern()`` with every predicate unset always
    matches — same fire-always default as the legacy bare matcher."""
    assert event_pattern.matches(EventPattern(), _mcp_event()) is True


# ===========================================================================
# Tier 1 — Contract: the NEW kind/source predicates
# ===========================================================================


def test_kind_predicate_matches_bare_or_canonical_form() -> None:
    """Tier 1: ``EventPattern.kind`` accepts either the bare short-form or the
    canonical namespaced kind — both normalize to the same comparison."""
    event = _mcp_event()
    assert event_pattern.matches(EventPattern(kind="mcp_resource_updated"), event) is True
    assert event_pattern.matches(
        EventPattern(kind="builtin:external:mcp_resource_updated"), event,
    ) is True
    assert event_pattern.matches(EventPattern(kind="file_changed"), event) is False


def test_source_predicate_exact_match() -> None:
    """Tier 1: ``EventPattern.source`` matches by exact string equality."""
    event = HookEvent(kind="mcp:github:resource_updated", payload={}, source="mcp:github")
    assert event_pattern.matches(EventPattern(source="mcp:github"), event) is True
    assert event_pattern.matches(EventPattern(source="mcp:gitlab"), event) is False


def test_kind_source_payload_predicates_all_must_hold() -> None:
    """Tier 1: a combined kind+source+payload EventPattern requires EVERY
    predicate to hold — narrowing further than payload-only, non-regressive."""
    event = _mcp_event(server="github", uri="file:///repo/a.txt")
    pattern = EventPattern(
        kind="mcp_resource_updated", source="builtin", payload={"server": "github"},
    )
    assert event_pattern.matches(pattern, event) is True

    # kind mismatches -> overall no match even though payload/source would pass
    assert event_pattern.matches(
        EventPattern(kind="file_changed", source="builtin", payload={"server": "github"}),
        event,
    ) is False
    # source mismatches
    assert event_pattern.matches(
        EventPattern(kind="mcp_resource_updated", source="mcp:other", payload={"server": "github"}),
        event,
    ) is False
    # payload mismatches
    assert event_pattern.matches(
        EventPattern(kind="mcp_resource_updated", source="builtin", payload={"server": "gitlab"}),
        event,
    ) is False


# ===========================================================================
# Tier 1 — Contract: static validation against the Phase-1 Schema Registry
# ===========================================================================


def test_validate_against_schema_passes_for_real_field() -> None:
    """Tier 1: a payload field that IS in the kind's builtin schema passes
    silently."""
    validate_against_schema(
        EventPattern(payload={"server": "github", "uri": "file:///repo/**"}),
        "mcp_resource_updated",
    )  # no raise


def test_validate_against_schema_flags_typo_field() -> None:
    """Tier 1: a payload field NOT in the kind's builtin schema (a typo, e.g.
    ``srever`` instead of ``server``) is flagged — the new typo-resistance
    capability the Phase-1 Schema Registry enables."""
    with pytest.raises(HookSchemaError, match="srever"):
        validate_against_schema(EventPattern(payload={"srever": "github"}), "mcp_resource_updated")


def test_validate_against_schema_accepts_bare_or_canonical_kind() -> None:
    """Tier 1: schema lookup normalizes bare/canonical kind spellings, same
    as every other Phase-1 registry consumer."""
    with pytest.raises(HookSchemaError):
        validate_against_schema(
            EventPattern(payload={"nope": "x"}), "builtin:external:mcp_resource_updated",
        )


def test_validate_against_schema_unknown_kind_is_noop() -> None:
    """Tier 1: a kind with no builtin schema entry (future/non-builtin point
    — the schema-driven open set) is a silent no-op, not an error."""
    validate_against_schema(EventPattern(payload={"anything": "x"}), "some_future_point")  # no raise


def test_validate_against_schema_empty_payload_is_noop() -> None:
    """Tier 1: an EventPattern with no payload predicate has nothing to
    validate."""
    validate_against_schema(EventPattern(kind="mcp_resource_updated"), "mcp_resource_updated")


# ===========================================================================
# Tier 1 — Contract: static validation is ENFORCED AT LOAD (the reachability
# proof — the typo-resistance is ACTIVE, not a dormant opt-in capability).
# ===========================================================================


def test_load_hooks_rejects_schema_external_matcher_field() -> None:
    """Tier 1: reachability — a matcher naming a payload field the hook-point's
    builtin schema does NOT carry (a ``srever`` typo on ``mcp_resource_updated``,
    whose real field is ``server``) fails LOUD as a ``HookConfigError`` at
    load, instead of silently never firing. This is what makes Phase-3's
    typo-resistance an active guarantee rather than a dormant capability."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "x"},
            "matcher": {"srever": "github"},  # typo: should be "server"
        },
    ]
    with pytest.raises(HookConfigError, match="srever"):
        load_hooks(raw)


def test_load_hooks_accepts_schema_valid_matcher_field() -> None:
    """Tier 1: a matcher naming a field the point's schema DOES carry loads
    fine — enforcement is additive (only INVALID matchers fail-loud; every
    valid matcher still parses byte-identically)."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "x"},
            "matcher": {"server": "github", "uri": "file:///repo/**"},
        },
    ]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("mcp_resource_updated")
    assert hook.matcher == {"server": "github", "uri": "file:///repo/**"}


def test_load_hooks_lifecycle_matcher_field_validated_against_its_schema() -> None:
    """Tier 1: enforcement applies to lifecycle points too — a ``turn_end``
    matcher on the schema-external ``server`` field (the pre-Phase-3 fixtures'
    arbitrary placeholder) now fails loud, while a schema-valid ``agent_name``
    matcher loads. Proves the pre-Phase-3 permissiveness was an accident, now
    corrected."""
    bad = [{"on": "turn_end", "template_push": {"message": "x"}, "matcher": {"server": "y"}}]
    with pytest.raises(HookConfigError, match="server"):
        load_hooks(bad)

    good = [{"on": "turn_end", "template_push": {"message": "x"}, "matcher": {"agent_name": "y"}}]
    registry = load_hooks(good)
    (hook,) = registry.hooks_for("turn_end")
    assert hook.matcher == {"agent_name": "y"}


def test_load_hooks_open_set_point_matcher_stays_permissive() -> None:
    """Tier 1: the open-set is preserved — a hook-point with NO builtin schema
    entry (a future/custom point) is not schema-validated, so any matcher field
    loads permissively. Verified via the loader-internal validation seam
    directly against an unknown kind (no such point is registrable through the
    public ``on:`` allowlist yet — that's the whole point of the open set)."""
    # A future kind with no BUILTIN_HOOK_SCHEMAS entry → validation is a no-op,
    # never raises, whatever the matcher field names.
    validate_against_schema(
        EventPattern(payload={"any_future_field": "v"}), "builtin:lifecycle:pre_tool_use",
    )  # no raise — open set stays permissive


# ===========================================================================
# Tier 2 — OS invariant: HookDispatcher's matcher check now runs through the
# EventPattern grammar and still produces IDENTICAL fire/skip decisions.
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatch_via_event_pattern_path_still_skips_non_matching_hook() -> None:
    """Tier 2: driven through the REAL dispatcher (which now evaluates
    ``hook.matcher`` via ``reyn.hooks.event_pattern``), a hook whose matcher
    doesn't match is skipped — identical to the pre-Phase-3 direct
    ``matcher.matches`` call (test_2608_h2_hook_matcher.py's own suite stays
    green, unmodified, verifying this at the module level too)."""
    raw = [
        {
            "on": "mcp_resource_updated",
            "template_push": {"message": "{{ uri }} updated"},
            "matcher": {"server": "github"},
        },
    ]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="gitlab"))
    assert recorder.calls == []  # skipped — server didn't match

    await disp.dispatch("mcp_resource_updated", _mcp_vars(server="github"))
    (_,) = recorder.calls  # exactly one call — fired, server matched


@pytest.mark.asyncio
async def test_dispatch_no_matcher_still_fires_always_via_event_pattern_path() -> None:
    """Tier 2: a hook with no matcher still fires for every event through the
    EventPattern path — the fire-always default is preserved end-to-end."""
    raw = [{"on": "turn_end", "template_push": {"message": "turn done"}}]
    registry = load_hooks(raw)
    disp = HookDispatcher(
        registry,
        put_inbox=(recorder := _Recorder()),
        stage_next_turn_context=_Recorder(),
    )
    await disp.dispatch("turn_end", {})
    (_,) = recorder.calls  # exactly one call — fired unchanged


# ===========================================================================
# Strip-falsify — breaking the payload predicate's absent-field semantics
# flips the backward-compat invariant to RED; restoring goes GREEN.
# ===========================================================================


def test_strip_falsify_absent_field_never_match_invariant(monkeypatch) -> None:
    """Tier 1: strip-falsify — monkeypatching the payload predicate so an
    ABSENT field "matches" (the bug this module's docstring explicitly rules
    out) flips a real backward-compat case from False to True — RED relative
    to the true invariant. Restoring the real predicate goes GREEN again."""
    event = HookEvent(kind=canonical_kind("turn_end"), payload={"point": "turn_end"})
    pattern = EventPattern(payload={"server": "github"})

    # Real (byte-identical) behavior: a matcher field absent from the
    # event's payload never matches.
    assert event_pattern.matches(pattern, event) is False

    original = event_pattern._payload_matches

    def _broken_payload_matches(payload_matcher, payload) -> bool:
        return True  # BUG: absent field would now "match" — must never ship

    monkeypatch.setattr(event_pattern, "_payload_matches", _broken_payload_matches)
    assert event_pattern.matches(pattern, event) is True  # RED — invariant broken

    monkeypatch.setattr(event_pattern, "_payload_matches", original)
    assert event_pattern.matches(pattern, event) is False  # GREEN — invariant restored


def test_strip_falsify_kind_predicate_ignored_would_widen_matching(monkeypatch) -> None:
    """Tier 1: strip-falsify — monkeypatching the kind check out of ``matches``
    (simulating a regression that drops the kind predicate entirely) would
    make a kind-scoped pattern match an event of a DIFFERENT kind — RED.
    Restoring the real check goes GREEN."""
    file_changed_event = HookEvent(
        kind=canonical_kind("file_changed"), payload={"point": "file_changed"},
    )
    pattern = EventPattern(kind="mcp_resource_updated")

    assert event_pattern.matches(pattern, file_changed_event) is False  # real behavior

    def _broken_matches(pat, evt) -> bool:
        if pat is None:
            return True
        if pat.source is not None and pat.source != evt.source:
            return False
        return event_pattern._payload_matches(pat.payload, evt.payload)  # BUG: no kind check

    monkeypatch.setattr(event_pattern, "matches", _broken_matches)
    assert event_pattern.matches(pattern, file_changed_event) is True  # RED — kind predicate lost

    # monkeypatch's fixture teardown restores the real `matches`; assert here
    # too so the GREEN restoration is explicit within the test body.
    monkeypatch.undo()
    assert event_pattern.matches(pattern, file_changed_event) is False  # GREEN — restored
