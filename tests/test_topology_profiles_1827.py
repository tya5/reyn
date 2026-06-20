"""Tier 2: Topology.profiles — per-member capability_profile binding (#1827 S2b).

A topology may bind members to capability_profiles (member → profile name).
S3 resolves the name → a CapabilityProfile → the (ContextualPermission,
excluded_categories) threaded at session build. This pins the schema field:
YAML round-trip, the member-validity invariant, the accessor, and builder
propagation. Existing topologies (no profiles) are byte-identical.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.topology import Topology


def test_profiles_round_trip_non_default(tmp_path: Path):
    """Tier 2: a topology with profile bindings save→load round-trips."""
    t = Topology.new(
        "ci", kind="pipeline", members=["builder", "tester", "deployer"],
        profiles={"tester": "reviewer", "deployer": "deploy_only"},
    )
    path = tmp_path / "ci.yaml"
    t.save(path)
    # the profiles block is persisted...
    assert "profiles:" in path.read_text(encoding="utf-8")
    loaded = Topology.load(path)
    assert loaded.profiles == {"tester": "reviewer", "deployer": "deploy_only"}
    assert loaded.members == ("builder", "tester", "deployer")


def test_load_without_profiles_is_empty(tmp_path: Path):
    """Tier 2: a topology YAML with no profiles → empty dict (byte-identical)."""
    path = tmp_path / "n.yaml"
    path.write_text("name: n\nkind: network\nmembers: [a, b]\n", encoding="utf-8")
    loaded = Topology.load(path)
    assert loaded.profiles == {}
    # and save omits an empty profiles block
    out = tmp_path / "n2.yaml"
    loaded.save(out)
    assert "profiles:" not in out.read_text(encoding="utf-8")


def test_profile_for_accessor():
    """Tier 2: profile_for returns the bound name or None."""
    t = Topology.new(
        "team", kind="team", members=["lead", "a"], leader="lead",
        profiles={"a": "reviewer"},
    )
    assert t.profile_for("a") == "reviewer"
    assert t.profile_for("lead") is None


def test_profile_binding_non_member_rejected():
    """Tier 2: a profile bound to a non-member is a construction error."""
    with pytest.raises(ValueError, match="non-members"):
        Topology(name="x", kind="network", members=("a",), profiles={"ghost": "p"})


def test_with_member_added_preserves_profiles():
    """Tier 2: adding a member keeps existing bindings."""
    t = Topology.new("n", kind="network", members=["a"], profiles={"a": "reviewer"})
    t2 = t.with_member_added("b")
    assert t2.profiles == {"a": "reviewer"}
    assert t2.members == ("a", "b")


def test_with_member_removed_drops_binding():
    """Tier 2: removing a bound member drops its binding (no orphan)."""
    t = Topology.new(
        "n", kind="network", members=["a", "b"],
        profiles={"a": "reviewer", "b": "deploy_only"},
    )
    t2 = t.with_member_removed("a")
    assert t2.profiles == {"b": "deploy_only"}
    assert "a" not in t2.members
