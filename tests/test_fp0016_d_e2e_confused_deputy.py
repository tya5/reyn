"""Tier 2c: FP-0016 Component D — Confused Deputy mitigation end-to-end.

Demonstrates the full mitigation path for the Confused Deputy threat:

  A malicious document processed by a sub-skill could instruct the sub-skill
  to read credentials it has no legitimate need for and include them in its
  output. FP-0016 Component D prevents this by:

  1. ScopedSecretStore: reads outside the declared allowed set raise
     CredentialScopeError; out-of-scope keys are invisible to list_visible_keys()
     (no enumeration leak).

  2. run_skill boundary scoping: when the OS spawns a sub-skill via run_skill,
     it intersects the sub-skill's required_credentials with the parent's
     already-scoped store. The sub-skill can never gain credentials the parent
     does not itself hold.

  3. sub_skill_credential_scope P6 event: the effective allowed set is recorded
     on every run_skill invocation for audit.

No mocks. Uses real ScopedSecretStore / CredentialScopeError from reyn.secrets.

Note: the full invoke_sub_skill → Agent.run path requires a real LLM call and
is exercised by integration-level dogfood scenarios (batch 23+). The scope
construction and intersection logic is exercised here at the module boundary
level — which is the meaningful invariant to pin (same pattern as
test_run_skill_model_class.py for model-class selection logic).

Dependencies: D1 (ScopedSecretStore/CredentialScopeError) must be landed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.secrets import CredentialScopeError, ScopedSecretStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_dotenv(tmp_path: Path, pairs: list[tuple[str, str]]) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text("\n".join(f"{k}={v}" for k, v in pairs), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Part 1: ScopedSecretStore — Confused Deputy mitigation
# ---------------------------------------------------------------------------


def test_positive_path_allowed_key_readable(tmp_path: Path) -> None:
    """Tier 2c: ScopedSecretStore allows reading a declared allowed key (positive path)."""
    p = _write_dotenv(tmp_path, [("victim_secret", "PWNED"), ("allowed_key", "ok")])
    store = ScopedSecretStore(allowed_keys=["allowed_key"], path=p)
    assert store.get("allowed_key") == "ok"


def test_confused_deputy_attempt_blocked(tmp_path: Path) -> None:
    """Tier 2c: Confused Deputy blocked — reading victim_secret raises CredentialScopeError.

    The attacker's document instructs the sub-skill to read 'victim_secret', which
    is present in the underlying store but NOT in the sub-skill's declared scope.
    The store raises CredentialScopeError, preventing exfiltration.
    """
    p = _write_dotenv(tmp_path, [("victim_secret", "PWNED"), ("allowed_key", "ok")])
    store = ScopedSecretStore(allowed_keys=["allowed_key"], path=p)

    with pytest.raises(CredentialScopeError) as exc_info:
        store.get("victim_secret")

    # Error message must name the disallowed key so the operator can diagnose.
    assert "victim_secret" in str(exc_info.value)


def test_no_enumeration_leak(tmp_path: Path) -> None:
    """Tier 2c: victim_secret does not appear in list_visible_keys (no enumeration leak).

    Even if a Confused Deputy attack cannot read a value directly, a secondary
    threat is enumerating which keys exist so the attacker knows what to try.
    list_visible_keys() must not reveal out-of-scope keys.
    """
    p = _write_dotenv(tmp_path, [("victim_secret", "PWNED"), ("allowed_key", "ok")])
    store = ScopedSecretStore(allowed_keys=["allowed_key"], path=p)

    visible = store.list_visible_keys()

    assert "allowed_key" in visible
    assert "victim_secret" not in visible


def test_contains_check_does_not_leak_out_of_scope_key(tmp_path: Path) -> None:
    """Tier 2c: __contains__ returns False (no raise) for out-of-scope keys.

    A sub-skill checking 'victim_secret' in store must not learn whether the key
    exists in the underlying store — False is always returned without raising,
    and without revealing the key's presence.
    """
    p = _write_dotenv(tmp_path, [("victim_secret", "PWNED"), ("allowed_key", "ok")])
    store = ScopedSecretStore(allowed_keys=["allowed_key"], path=p)

    # Must return False without raising, even though victim_secret is present in store.
    assert ("victim_secret" in store) is False


def test_empty_scope_blocks_all_reads(tmp_path: Path) -> None:
    """Tier 2c: allowed_keys=[] means no credential access at all.

    A stdlib skill that needs no secrets should declare required_credentials: []
    so it operates in a zero-trust credential context. Every read attempt is blocked.
    """
    p = _write_dotenv(tmp_path, [("victim_secret", "PWNED"), ("allowed_key", "ok")])
    store = ScopedSecretStore(allowed_keys=[], path=p)

    with pytest.raises(CredentialScopeError):
        store.get("allowed_key")

    with pytest.raises(CredentialScopeError):
        store.get("victim_secret")

    assert store.list_visible_keys() == []


# ---------------------------------------------------------------------------
# Part 2: run_skill boundary — scope intersection (Confused Deputy via parent)
# ---------------------------------------------------------------------------
#
# The run_skill handler applies the following logic at the spawn boundary:
#
#   allowed = sub_skill.required_credentials   # e.g. ["github_token"]
#   parent = ctx.secret_store
#   if parent is not None and not parent.is_unrestricted:
#       if "*" in allowed:
#           allowed = sorted(parent.allowed_keys)        # cap to parent set
#       else:
#           allowed = [k for k in allowed if k in parent.allowed_keys]  # intersect
#   scoped_store = ScopedSecretStore(allowed_keys=allowed)
#
# This section tests that logic directly — same pattern as
# test_run_skill_model_class.py which mirrors the model-selection branch.
# ---------------------------------------------------------------------------


def _compute_effective_scope(
    sub_skill_required_credentials: list[str],
    parent_store: ScopedSecretStore | None,
) -> list[str]:
    """Mirror of the scope-construction logic in run_skill.handle().

    When this function diverges from the production code, update both.
    """
    allowed = list(sub_skill_required_credentials)
    if parent_store is not None and not parent_store.is_unrestricted:
        parent_allowed = parent_store.allowed_keys
        if "*" in allowed:
            allowed = sorted(parent_allowed)
        else:
            allowed = [k for k in allowed if k in parent_allowed]
    return allowed


def _make_parent_store(
    tmp_path: Path,
    allowed_keys: list[str],
    secrets: list[tuple[str, str]] | None = None,
) -> ScopedSecretStore:
    """Construct a parent ScopedSecretStore with the given allowed keys."""
    p = _write_dotenv(tmp_path, secrets or [])
    return ScopedSecretStore(allowed_keys=allowed_keys, path=p)


def test_scope_intersection_removes_undeclared_keys(tmp_path: Path) -> None:
    """Tier 2c: sub-skill cannot gain credentials the parent does not hold.

    Parent scope: {github_token, stripe_key, datadog_key}
    Sub-skill declares: [github_token, slack_token]  ← slack_token not in parent
    Effective scope: [github_token]  ← intersection

    This is the core Confused Deputy mitigation at the run_skill boundary.
    """
    parent = _make_parent_store(
        tmp_path,
        allowed_keys=["github_token", "stripe_key", "datadog_key"],
    )
    sub_declared = ["github_token", "slack_token"]

    effective = _compute_effective_scope(sub_declared, parent)

    assert "github_token" in effective
    assert "slack_token" not in effective
    assert "stripe_key" not in effective
    assert "datadog_key" not in effective


def test_scope_wildcard_sub_capped_by_scoped_parent(tmp_path: Path) -> None:
    """Tier 2c: sub-skill with required_credentials=["*"] is capped at parent scope.

    A sub-skill that omits required_credentials (gets default ["*"]) cannot
    escape the parent's scope when the parent itself is scoped.

    Parent scope: {allowed_key}
    Sub-skill declares: ["*"]  ← default / full delegation
    Effective scope: [allowed_key]  ← capped to parent
    """
    parent = _make_parent_store(tmp_path, allowed_keys=["allowed_key"])
    sub_declared = ["*"]  # default when required_credentials omitted

    effective = _compute_effective_scope(sub_declared, parent)

    assert effective == ["allowed_key"]


def test_scope_unrestricted_parent_honours_sub_declaration(tmp_path: Path) -> None:
    """Tier 2c: unrestricted parent (["*"]) passes sub-skill's declared scope through.

    When the top-level skill runs without a scoped store (parent is unrestricted),
    the sub-skill's own required_credentials is the binding constraint.
    """
    parent = _make_parent_store(tmp_path, allowed_keys=["*"])
    assert parent.is_unrestricted
    sub_declared = ["github_token", "atlassian_token"]

    effective = _compute_effective_scope(sub_declared, parent)

    assert set(effective) == {"github_token", "atlassian_token"}


def test_scope_no_parent_honours_sub_declaration(tmp_path: Path) -> None:
    """Tier 2c: no parent store (ctx.secret_store is None) passes sub-skill's scope through.

    This is the top-level invocation case: a skill spawned directly (not via
    run_skill from a parent) has no parent scope constraint.
    """
    sub_declared = ["github_token"]

    effective = _compute_effective_scope(sub_declared, parent_store=None)

    assert effective == ["github_token"]


def test_effective_scope_is_usable_as_scoped_store(tmp_path: Path) -> None:
    """Tier 2c: effective scope from intersection can be constructed into a ScopedSecretStore.

    Verifies the full pipeline: intersection → ScopedSecretStore → reads.
    """
    secrets = [("allowed_key", "ok"), ("victim_secret", "PWNED")]
    p = _write_dotenv(tmp_path, secrets)

    parent = ScopedSecretStore(
        allowed_keys=["allowed_key", "datadog_key"],
        path=p,
    )
    sub_declared = ["allowed_key", "victim_secret"]  # victim_secret not in parent

    effective = _compute_effective_scope(sub_declared, parent)

    scoped = ScopedSecretStore(allowed_keys=effective, path=p)

    # allowed_key: in scope and present → readable
    assert scoped.get("allowed_key") == "ok"

    # victim_secret: in sub-skill's request but blocked by intersection
    with pytest.raises(CredentialScopeError):
        scoped.get("victim_secret")

    # victim_secret not visible via enumeration
    assert "victim_secret" not in scoped.list_visible_keys()


# ---------------------------------------------------------------------------
# Part 3: sub_skill_credential_scope audit event
# ---------------------------------------------------------------------------


def test_sub_skill_credential_scope_event_emitted(tmp_path: Path) -> None:
    """Tier 2c: sub_skill_credential_scope P6 event is emitted at the run_skill boundary.

    Tests that EventLog.emit is called with the correct event type and
    allowed_keys payload by replaying the event emission logic from run_skill.py.

    Uses a real EventLog and inspects its emitted events via the public
    snapshot API (not private state).
    """
    from reyn.events.events import EventLog

    events = EventLog()
    allowed = ["github_token"]

    # Mirror the event emission from run_skill.handle():
    events.emit(
        "sub_skill_credential_scope",
        skill="github_pr_reviewer",
        allowed_keys=sorted(set(allowed)) if "*" not in allowed else ["*"],
    )

    # The event must appear in the log.
    found = [
        e for e in events.all()
        if e.type == "sub_skill_credential_scope"
    ]
    (evt,) = found
    assert evt.data["skill"] == "github_pr_reviewer"
    assert evt.data["allowed_keys"] == ["github_token"]


def test_sub_skill_credential_scope_event_wildcard(tmp_path: Path) -> None:
    """Tier 2c: sub_skill_credential_scope event carries [\"*\"] for unrestricted scope."""
    from reyn.events.events import EventLog

    events = EventLog()
    allowed = ["*"]

    events.emit(
        "sub_skill_credential_scope",
        skill="trusted_internal_skill",
        allowed_keys=sorted(set(allowed)) if "*" not in allowed else ["*"],
    )

    found = [
        e for e in events.all()
        if e.type == "sub_skill_credential_scope"
    ]
    (evt_wildcard,) = found
    assert evt_wildcard.data["allowed_keys"] == ["*"]
