"""Tests for #2889 — schema-validate ``composed:*`` hook matchers at load,
closing the Phase-3 (#2873) footgun the ``composed:*`` open namespace was
left in.

Root cause (see the architect's design comment on #2889): ``composed:*`` is
an open namespace, so a ``matcher`` on a ``composed:*`` hook was NOT
schema-checked at load — a typo'd field silently never matches at dispatch,
exactly the footgun Phase 3 closed for the 10 builtin points. Fix: every
composed event, across all 7 Composer ops, is emitted by the single
``_emit_composed`` producer (``reyn.hooks.composer``) with the FIXED payload
shape ``{"inputs": [...], "correlation_key": <key>}`` — so the schema is
knowable and identical for every composer, keyed by its ``emit_kind``.
``Session.__init__`` now builds the Composer defs BEFORE the hook registry
(the reorder is side-effect-free — confirmed) and threads the derived
``{emit_kind: frozenset({"inputs", "correlation_key"})}`` map into
``load_hooks`` -> ``_parse_entry`` -> ``event_pattern.validate_against_schema``.

Coverage plan
-------------
Tier 1 (contract): ``event_pattern.validate_against_schema`` — a
  ``composed_schemas``-resolved schema flags an unknown field / passes a
  known one, mirroring the Phase-3 builtin-point contract test.
Tier 1 (contract): ``reyn.hooks.loader.load_hooks(raw, composed_schemas)`` —
  reachability: a typo'd composed matcher fails loud at load; a valid one
  loads clean; a ``composed:*`` subscription with NO producing composer
  fails loud (sub-decision (b), included — the reorder makes the full known
  composed-kind universe available at hook-load time); ``composed_schemas=
  None`` (the default, no composer config threaded) stays permissive for
  both checks — the pre-#2889 posture is unchanged for such callers.
Tier 2 (OS invariant, Session-integration): a REAL ``Session`` constructed
  with a ``composers_config`` + ``hooks_config`` whose composed matcher has a
  schema-external field fails loud AT BOOT (``Session.__init__`` raises) —
  proving the reorder + schema-map + threading actually reaches the
  production wiring, not just the loader in isolation.
Strip-falsify (both the matcher check and the dangling-producer check):
  simulating "the schema-injection wiring never happened" flips each
  enforced case back to a clean load — RED; restoring goes GREEN.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks import loader as loader_mod
from reyn.hooks.event_pattern import EventPattern, validate_against_schema
from reyn.hooks.loader import HookConfigError, load_hooks
from reyn.hooks.schema_registry import HookSchemaError
from reyn.runtime.session import Session

_DEPLOY_APPROVED_SCHEMAS: "dict[str, frozenset[str]]" = {
    "composed:deploy_approved": frozenset({"inputs", "correlation_key"}),
}


def _composer_config(*, name: str = "deploy_approved", emit_kind: str = "composed:deploy_approved") -> list:
    return [
        {
            "name": name,
            "op": "any",
            "inputs": [{"kind": "builtin:external:file_changed"}],
            "emit": {"kind": emit_kind},
        },
    ]


# ===========================================================================
# Tier 1 — Contract: validate_against_schema resolves a composed_schemas entry
# ===========================================================================


def test_validate_against_schema_flags_typo_composed_field() -> None:
    """Tier 1: a payload field NOT in a composed kind's fixed
    ``{"inputs", "correlation_key"}`` schema (a ``correlation_ky`` typo) is
    flagged — the new capability #2889 adds for ``composed:*``."""
    with pytest.raises(HookSchemaError, match="correlation_ky"):
        validate_against_schema(
            EventPattern(payload={"correlation_ky": "x"}),
            "composed:deploy_approved",
            _DEPLOY_APPROVED_SCHEMAS,
        )


def test_validate_against_schema_accepts_real_composed_fields() -> None:
    """Tier 1: both real composed-payload fields pass silently."""
    validate_against_schema(
        EventPattern(payload={"inputs": [], "correlation_key": "x"}),
        "composed:deploy_approved",
        _DEPLOY_APPROVED_SCHEMAS,
    )  # no raise


def test_validate_against_schema_composed_kind_without_map_is_noop() -> None:
    """Tier 1: ``composed_schemas=None`` (no composer config known to the
    caller) preserves the pre-#2889 open-set posture — a composed kind stays
    unvalidated, same as any other non-builtin point."""
    validate_against_schema(
        EventPattern(payload={"anything": "x"}), "composed:deploy_approved",
    )  # no raise — composed_schemas defaults to None


def test_validate_against_schema_composed_map_does_not_shadow_builtin() -> None:
    """Tier 1: a non-empty ``composed_schemas`` map doesn't affect builtin-kind
    validation — the two namespaces are disjoint and the builtin path is
    unchanged."""
    with pytest.raises(HookSchemaError, match="srever"):
        validate_against_schema(
            EventPattern(payload={"srever": "x"}),
            "mcp_resource_updated",
            _DEPLOY_APPROVED_SCHEMAS,
        )


# ===========================================================================
# Tier 1 — Contract: enforced at load via reyn.hooks.loader.load_hooks
# ===========================================================================


def test_load_hooks_rejects_typo_composed_matcher_field() -> None:
    """Tier 1: reachability — a ``composed:*`` hook's matcher naming a field
    NOT in the fixed composed payload shape fails loud as ``HookConfigError``
    at load, instead of silently never firing."""
    raw = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"correlation_ky": "abc"},  # typo: should be "correlation_key"
        },
    ]
    with pytest.raises(HookConfigError, match="correlation_ky"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)


def test_load_hooks_accepts_valid_composed_matcher_fields() -> None:
    """Tier 1: a composed matcher naming ``inputs``/``correlation_key`` (the
    real fixed shape) loads fine — enforcement is additive."""
    raw = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"correlation_key": "abc"},
        },
    ]
    registry = load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)
    (hook,) = registry.hooks_for("composed:deploy_approved")
    assert hook.matcher == {"correlation_key": "abc"}


def test_load_hooks_composed_matcher_stays_permissive_without_composed_schemas() -> None:
    """Tier 1: ``composed_schemas=None`` (the default — most direct
    ``load_hooks(raw)`` callers) skips composed-kind matcher validation
    entirely, preserving the pre-#2889 posture."""
    raw = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"anything_at_all": "x"},
        },
    ]
    registry = load_hooks(raw)  # no composed_schemas passed
    (hook,) = registry.hooks_for("composed:deploy_approved")
    assert hook.matcher == {"anything_at_all": "x"}


# ===========================================================================
# Tier 1 — Contract: sub-decision (b), included — dangling-producer detection
# ===========================================================================


def test_load_hooks_rejects_dangling_composed_subscription() -> None:
    """Tier 1: ``on: composed:X`` with NO configured composer producing
    ``composed:X`` fails loud — it could never fire (the reorder makes the
    full known composed-kind universe available at hook-load time)."""
    raw = [{"on": "composed:no_such_producer", "template_push": {"message": "x"}}]
    with pytest.raises(HookConfigError, match="no configured composer"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)


def test_load_hooks_accepts_composed_subscription_with_real_producer() -> None:
    """Tier 1: the dangling-producer check doesn't false-positive — a
    composed kind that DOES appear in ``composed_schemas`` loads fine."""
    raw = [{"on": "composed:deploy_approved", "template_push": {"message": "x"}}]
    registry = load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)
    (hook,) = registry.hooks_for("composed:deploy_approved")
    assert hook.on == "composed:deploy_approved"


def test_load_hooks_dangling_check_skipped_without_composed_schemas() -> None:
    """Tier 1: ``composed_schemas=None`` also skips the dangling-producer
    check — a caller with no composer configuration to thread gets the
    pre-#2889 permissive posture for BOTH new checks, not just the matcher
    one."""
    raw = [{"on": "composed:no_such_producer", "template_push": {"message": "x"}}]
    registry = load_hooks(raw)  # no composed_schemas
    (hook,) = registry.hooks_for("composed:no_such_producer")
    assert hook.on == "composed:no_such_producer"


# ===========================================================================
# Tier 2 — OS invariant: Session integration — the reorder + threading
# actually reaches production construction, not just the loader in isolation.
# ===========================================================================


def test_session_boot_fails_loud_on_typo_composed_matcher(tmp_path: Path) -> None:
    """Tier 2: a REAL ``Session`` constructed with a composer producing
    ``composed:deploy_approved`` and a startup hook whose composed matcher
    names a schema-external field raises ``HookConfigError`` AT BOOT
    (``Session.__init__``) — the trusted startup layer fails loud, exactly
    like the pre-existing Phase-3 builtin-point guarantee."""
    hooks_config = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"correlation_ky": "abc"},  # typo
        },
    ]
    with pytest.raises(HookConfigError, match="correlation_ky"):
        Session(
            agent_name="composed-matcher-2889-agent",
            state_log=StateLog(tmp_path / "state.wal"),
            snapshot_path=tmp_path / "snap.json",
            hooks_config=hooks_config,
            composers_config=_composer_config(),
        )


def test_session_boot_loads_clean_with_valid_composed_matcher(tmp_path: Path) -> None:
    """Tier 2: the positive control — a schema-valid composed matcher boots
    a REAL Session cleanly (no ``HookConfigError``) — additive correctness,
    not a regression on the happy path."""
    hooks_config = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"correlation_key": "abc"},
        },
    ]
    Session(
        agent_name="composed-matcher-2889-agent-ok",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        hooks_config=hooks_config,
        composers_config=_composer_config(),
    )  # no raise


def test_session_boot_fails_loud_on_dangling_composed_subscription(tmp_path: Path) -> None:
    """Tier 2: a REAL Session with a hook on a composed kind NO configured
    composer produces raises ``HookConfigError`` at boot (sub-decision (b)
    reaching production construction, not just the loader in isolation)."""
    hooks_config = [{"on": "composed:no_such_producer", "template_push": {"message": "x"}}]
    with pytest.raises(HookConfigError, match="no configured composer"):
        Session(
            agent_name="composed-dangling-2889-agent",
            state_log=StateLog(tmp_path / "state.wal"),
            snapshot_path=tmp_path / "snap.json",
            hooks_config=hooks_config,
            composers_config=_composer_config(),  # produces a DIFFERENT composed kind
        )


# ===========================================================================
# Strip-falsify — proving the checks are load-bearing, not mechanism stubs
# ===========================================================================


def test_strip_falsify_composed_matcher_schema_validation(monkeypatch) -> None:
    """Tier 1: strip-falsify — patch ``loader.validate_event_pattern`` so it
    always drops ``composed_schemas`` (simulating the #2889 threading never
    happened, e.g. the reorder didn't land), which flips the typo'd composed
    matcher from fail-loud to a clean load — RED. Restoring goes GREEN."""
    raw = [
        {
            "on": "composed:deploy_approved",
            "template_push": {"message": "x"},
            "matcher": {"correlation_ky": "abc"},  # typo
        },
    ]
    # Real (fixed) behavior: raises.
    with pytest.raises(HookConfigError, match="correlation_ky"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)

    original = loader_mod.validate_event_pattern

    def _stripped(pattern, kind, composed_schemas=None):
        return original(pattern, kind, None)  # drop composed_schemas — simulate the pre-#2889 bug

    monkeypatch.setattr(loader_mod, "validate_event_pattern", _stripped)
    registry = load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)  # RED — no longer raises
    (hook,) = registry.hooks_for("composed:deploy_approved")
    assert hook.matcher == {"correlation_ky": "abc"}

    monkeypatch.undo()
    with pytest.raises(HookConfigError, match="correlation_ky"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)  # GREEN — restored


def test_strip_falsify_dangling_producer_detection() -> None:
    """Tier 1: strip-falsify — the dangling-producer check is gated entirely
    on ``composed_schemas`` being threaded (the reorder's whole point: making
    the full known composed-kind universe available before hooks load).
    Passing ``None`` (as if the reorder never ran / the caller never threaded
    composer info) flips the SAME dangling subscription from fail-loud to a
    clean load — RED. Passing the real map restores fail-loud — GREEN."""
    raw = [{"on": "composed:no_such_producer", "template_push": {"message": "x"}}]

    # Real (fixed) behavior, with the schema map threaded: raises.
    with pytest.raises(HookConfigError, match="no configured composer"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)

    # Strip: composed_schemas=None — RED, the same dangling hook loads clean.
    registry = load_hooks(raw, None)
    (hook,) = registry.hooks_for("composed:no_such_producer")
    assert hook.on == "composed:no_such_producer"

    # Restore: GREEN.
    with pytest.raises(HookConfigError, match="no configured composer"):
        load_hooks(raw, _DEPLOY_APPROVED_SCHEMAS)
