"""Tier 2: UserIntervention contract — to_dict / from_dict round-trip.

PR-intervention-link L1. Crash recovery requires UserIntervention to be
serialized into the agent snapshot's ``outstanding_interventions`` dict
on dispatch, and deserialized back into the InterventionRegistry on
restore. The ``future`` field is intentionally excluded — futures are
volatile and the restored intervention gets a fresh future when re-enqueued.

Round-trip invariants:
  - All persistent fields (kind, prompt, detail, choices, suggestions,
    run_id, skill_name, id) survive a to_dict → from_dict cycle.
  - InterventionChoice nested objects serialize / deserialize correctly.
  - The dict shape is JSON-safe (only str / list / dict / None values)
    so it can flow through ``AgentSnapshot.outstanding_interventions``
    and the WAL ``intervention_dispatched.iv_dict`` field unchanged.
  - ``future`` is reset to a fresh asyncio.Future on from_dict so the
    re-enqueued intervention can resolve normally.
"""
from __future__ import annotations

import asyncio
import json

from reyn.user_intervention import (
    InterventionChoice,
    UserIntervention,
)


def _sample_iv(*, with_choices: bool = False) -> UserIntervention:
    """Build a UserIntervention in a known asyncio loop context."""
    choices = []
    if with_choices:
        choices = [
            InterventionChoice(id="yes", label="[Y]es", hotkey="Y"),
            InterventionChoice(id="no", label="[N]o", hotkey="N"),
            InterventionChoice(id="always", label="[A]lways", hotkey="A"),
        ]
    return UserIntervention(
        kind="permission.generic",
        prompt="Allow file/write to /tmp/foo?",
        detail="Reason: skill X requested it",
        choices=choices,
        suggestions=["yes", "no"],
        run_id="run_alpha_001",
        skill_name="my_skill",
    )


def test_to_dict_returns_json_safe_dict():
    """Tier 2: to_dict produces a dict with only JSON-safe value types."""
    iv = _sample_iv(with_choices=True)
    d = iv.to_dict()

    # Must be JSON serializable (= no asyncio.Future, no custom classes)
    json.dumps(d)  # raises if not serializable

    # All persistent fields present
    assert d["kind"] == "permission.generic"
    assert d["prompt"] == "Allow file/write to /tmp/foo?"
    assert d["detail"] == "Reason: skill X requested it"
    assert d["run_id"] == "run_alpha_001"
    assert d["skill_name"] == "my_skill"
    assert d["id"] == iv.id
    assert d["suggestions"] == ["yes", "no"]
    assert len(d["choices"]) == 3
    assert d["choices"][0] == {"id": "yes", "label": "[Y]es", "hotkey": "Y"}
    # future must NOT appear (asyncio.Future is not JSON-safe)
    assert "future" not in d


def test_from_dict_round_trip_preserves_persistent_fields():
    """Tier 2: from_dict(to_dict(iv)) preserves all persistent fields."""
    iv = _sample_iv(with_choices=True)
    iv2 = UserIntervention.from_dict(iv.to_dict())

    assert iv2.kind == iv.kind
    assert iv2.prompt == iv.prompt
    assert iv2.detail == iv.detail
    assert iv2.suggestions == iv.suggestions
    assert iv2.run_id == iv.run_id
    assert iv2.skill_name == iv.skill_name
    assert iv2.id == iv.id
    assert iv2.choices == iv.choices


def test_from_dict_resets_future_to_fresh():
    """Tier 2: restored intervention gets a fresh, unresolved future.

    The original future is volatile (its waiter has gone away with the
    crashed process). The restored intervention must be ready to await
    again.
    """
    iv = _sample_iv()
    d = iv.to_dict()
    iv2 = UserIntervention.from_dict(d)

    assert iv2.future is not iv.future
    assert isinstance(iv2.future, asyncio.Future)
    assert not iv2.future.done()


def test_from_dict_handles_empty_choices_and_suggestions():
    """Tier 2: free-text interventions (no choices, no suggestions) round-trip."""
    iv = UserIntervention(
        kind="ask_user",
        prompt="What's your name?",
        run_id="run_beta",
    )
    iv2 = UserIntervention.from_dict(iv.to_dict())
    assert iv2.choices == []
    assert iv2.suggestions == []
    assert iv2.kind == "ask_user"
    assert iv2.skill_name is None  # default None preserved


def test_from_dict_handles_none_run_id_and_skill_name():
    """Tier 2: intervention created before bus fills metadata round-trips."""
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    d = iv.to_dict()
    assert d["run_id"] is None
    assert d["skill_name"] is None
    iv2 = UserIntervention.from_dict(d)
    assert iv2.run_id is None
    assert iv2.skill_name is None


def test_to_dict_choices_preserve_none_hotkey():
    """Tier 2: choices with hotkey=None survive (free-text choice not allowed
    to bypass hotkey serialization)."""
    iv = UserIntervention(
        kind="permission.generic",
        prompt="?",
        choices=[InterventionChoice(id="x", label="X", hotkey=None)],
    )
    d = iv.to_dict()
    assert d["choices"][0]["hotkey"] is None
    iv2 = UserIntervention.from_dict(d)
    assert iv2.choices[0].hotkey is None


def test_from_dict_is_resilient_to_missing_optional_fields():
    """Tier 2: forward-compat — older snapshots may lack newer fields.

    An older recorded iv_dict missing ``suggestions`` (or ``detail``) must
    still load with sensible defaults so resume doesn't crash on a snapshot
    written by a previous Reyn version.
    """
    minimal = {
        "kind": "ask_user",
        "prompt": "?",
        "id": "abcdef",
    }
    iv = UserIntervention.from_dict(minimal)
    assert iv.kind == "ask_user"
    assert iv.id == "abcdef"
    assert iv.detail == ""
    assert iv.choices == []
    assert iv.suggestions == []
    assert iv.run_id is None


def test_round_trip_is_idempotent():
    """Tier 2: to_dict(from_dict(to_dict(iv))) == to_dict(iv)."""
    iv = _sample_iv(with_choices=True)
    d1 = iv.to_dict()
    iv2 = UserIntervention.from_dict(d1)
    d2 = iv2.to_dict()
    assert d1 == d2
