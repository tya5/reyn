"""Tier 1: `session_write_scope.py` — the DECLARED (never effective)
write-path scope for `describe_session` (#5012-A).

Pins the three-way discriminator architect ruled on: "no `sandbox.policy`
at all" is a different state from "`sandbox.policy` present but empty",
which is different again from "declared with real values" — collapsing
any two of these into one shape is exactly the fabrication class #5009's
own vocabulary exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reyn.runtime.session_write_scope import describe_write_scope


@dataclass
class _FakeSandboxConfig:
    policy: Any = None


def test_no_policy_at_all_reports_declared_false():
    """Tier 1: `sandbox.policy` never set (the real live default in THIS
    repo, confirmed #5012-A) — declared=False, not a fabricated `[]`."""
    result = describe_write_scope(_FakeSandboxConfig(policy=None))
    assert result == {"declared": False}


def test_an_explicitly_empty_policy_block_is_NOT_the_same_as_absent():
    """Tier 1: `sandbox.policy: {}` (a real, present, empty block) must NOT
    collapse into the "no policy at all" state via bare truthiness — that
    conflation is the exact bug this test pins.

    FALSIFY: `if not declared_policy:` (truthy check instead of `is None`)
    would report `declared: False` here too, indistinguishably from the
    previous test — the discriminator this whole module exists for would
    be silently broken."""
    result = describe_write_scope(_FakeSandboxConfig(policy={}))
    assert result["declared"] is True
    assert result["allow_write_paths"] is None
    assert result["deny_write_paths"] is None


def test_declared_with_real_values_reports_them_verbatim():
    """Tier 1: real declared paths pass through unchanged."""
    result = describe_write_scope(
        _FakeSandboxConfig(policy={"allow_write_paths": ["/tmp/scratch"]}),
    )
    assert result == {
        "declared": True,
        "allow_write_paths": ["/tmp/scratch"],
        "deny_write_paths": None,
    }


def test_declared_with_both_keys_reports_both():
    """Tier 1: both write-scope keys reported when both are present."""
    result = describe_write_scope(
        _FakeSandboxConfig(
            policy={"allow_write_paths": ["/a"], "deny_write_paths": ["/b"]},
        ),
    )
    assert result == {
        "declared": True,
        "allow_write_paths": ["/a"],
        "deny_write_paths": ["/b"],
    }


def test_missing_policy_attribute_entirely_is_treated_as_undeclared():
    """Tier 1: a config object with no `.policy` attribute at all (e.g. a
    minimal test double) degrades to declared=False, not an AttributeError
    — `getattr(..., None)` covers this without a caller having to guard."""

    class _NoPolicyAttr:
        pass

    result = describe_write_scope(_NoPolicyAttr())
    assert result == {"declared": False}
