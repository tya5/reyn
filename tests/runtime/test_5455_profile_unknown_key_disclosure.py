"""Tier 2: #5455 — profile.yaml unknown-top-level-key disclosure.

Real cause: PR #5095 removed ``AgentProfile.broker_identity`` from core,
but ``AgentProfile.load()`` reads unknown top-level keys with bare
``.get(...)`` — a field removed from the dataclass keeps loading silently
from an operator's file, doing nothing, forever, with no signal anywhere
(neither ``reyn doctor`` nor ``reyn config validate``, which architect's
real measurement found actively declares "No unknown ... config keys
found" while never having looked at profile.yaml at all).

architect's structural design (issue #5455, final revision — the
band-aid first draft, folding profile.yaml into
``config_schema.unknown_config_keys()``, was explicitly withdrawn):

  ① ``AgentProfile.load()`` reports its own residual keys — the
     registry is ``dataclasses.fields(AgentProfile)`` itself (same
     "live dataclass is the complete population" idiom as #5416).
  ② ``_load_yaml`` requires an un-omittable ``vocabulary=`` argument —
     see ``tests/scripts/test_check_load_yaml_vocabulary_5455.py`` for
     that half (a DIFFERENT file, since profile.yaml isn't read via
     ``_load_yaml`` at all — it has its own read path).
  ③ ``reyn config validate``'s "no issues" message names the surfaces
     it walked, so it can no longer claim a false universal.

Witness ① (architect's own real-machine replacement for a synthetic
fixture, issue comment, final revision) is NOT a committed test here —
it runs against ``~/Workspace/reyn_dev/reyn-self``, a repo this test
suite has no business depending on existing. Verified by hand instead
(see the PR this file lands in) and left as lead-coder/architect's own
positive-control acceptance step per the issue's stated order (land →
verify against reyn-self → THEN remove the two live
``broker_identity:`` lines there).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.profile import AgentProfile, unknown_profile_keys

# ── witness ④: the population registry is non-empty (empty-set guard) ──────


def test_the_field_registry_is_not_empty() -> None:
    """Tier 2: witness ④ — dataclasses.fields(AgentProfile) is not empty.
    A broken/emptied registry would make witness ② (known-keys-only ->
    silence) vacuously true — this is what makes that witness mean
    something."""
    known = {f.name for f in __import__("dataclasses").fields(AgentProfile)}
    assert known, "AgentProfile has no fields — the registry is empty"
    assert "name" in known and "role" in known


# ── unknown_profile_keys itself ─────────────────────────────────────────────


def test_an_unknown_top_level_key_is_reported() -> None:
    """Tier 2: witness ① (mechanism) — a removed-from-core field like
    #5095's broker_identity is reported by name."""
    found = unknown_profile_keys({"name": "x", "broker_identity": "x"})
    assert found == frozenset({"broker_identity"})


def test_known_keys_only_report_nothing() -> None:
    """Tier 2: witness ② — noise guard. A profile using only real fields
    reports an empty set, not a false positive."""
    real_keys = {f.name for f in __import__("dataclasses").fields(AgentProfile)}
    found = unknown_profile_keys(dict.fromkeys(real_keys, None))
    assert found == frozenset()


# ── AgentProfile.load() integration ─────────────────────────────────────────


def test_load_warns_on_an_unknown_key_and_still_loads(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: witness ① at the real parse point — AgentProfile.load()
    WARNs (never raises) with the agent name AND the key, and the
    profile still loads (the same "operator's file stays usable, the
    log is where the mismatch surfaces" contract every other
    not-applied disclosure in this codebase already has)."""
    (tmp_path / "profile.yaml").write_text(
        "name: coder-smith\nrole: x\nbroker_identity: coder-smith\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        profile = AgentProfile.load(tmp_path)

    assert profile.name == "coder-smith"
    assert any(
        "coder-smith" in r.message and "broker_identity" in r.message
        for r in caplog.records
    ), f"expected a WARNING naming both the agent and the key: {caplog.records!r}"


def test_load_with_only_known_keys_warns_nothing(tmp_path: Path, caplog) -> None:
    """Tier 2: witness ② at the real parse point — noise guard."""
    (tmp_path / "profile.yaml").write_text(
        "name: clean-agent\nrole: x\n", encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        AgentProfile.load(tmp_path)
    assert not any("unrecognized" in r.message for r in caplog.records)


# ── witness ③: strip — removing the check silences witness ① ───────────────


def test_strip_the_check_silences_the_warning(tmp_path: Path, monkeypatch, caplog) -> None:
    """Tier 2: witness ③, executed for real (not reasoned-through) — the
    exact strip-falsifier: monkeypatch unknown_profile_keys to always
    return empty (simulating the check being removed) and confirm the
    warning this issue exists to add disappears, on the SAME fixture
    witness ① uses."""
    import reyn.runtime.profile as profile_mod

    monkeypatch.setattr(profile_mod, "unknown_profile_keys", lambda data: frozenset())

    (tmp_path / "profile.yaml").write_text(
        "name: coder-smith\nrole: x\nbroker_identity: coder-smith\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        AgentProfile.load(tmp_path)
    assert not any("unrecognized" in r.message for r in caplog.records), (
        "the strip should have silenced the warning (proving the real, "
        "unpatched check is what produces it) but it still fired"
    )


# ── witness ⑤: preferences/bounding validation is unweakened ───────────────


def test_an_unknown_preference_key_still_raises(tmp_path: Path) -> None:
    """Tier 2: witness ⑤ — preferences: still RAISES on an unknown key
    (UnknownPreferenceKeyError), unweakened by this issue's WARN-only
    treatment of top-level keys. Different vocabulary, different
    consequence — a preferences: typo is a functional error at
    resolve_preference time, not a dead declaration."""
    from reyn.runtime.preferences import UnknownPreferenceKeyError

    (tmp_path / "profile.yaml").write_text(
        "name: x\nrole: x\npreferences:\n  not_a_real_preference: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(UnknownPreferenceKeyError):
        AgentProfile.load(tmp_path)


# ── witness ⑥: reyn config validate enumerates its surfaces ────────────────


def test_config_validate_names_the_walked_surfaces_when_clean(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: witness ⑥ (half A) — the "no issues" message names what it
    walked, closing the exact false-universal defect this issue reports
    (architect's real measurement: "No unknown ... config keys found"
    while profile.yaml was never in the population)."""
    from reyn.interfaces.cli.commands.config import _validate

    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")

    _validate()
    out = capsys.readouterr().out
    assert "profile.yaml" in out, (
        f"the clean-tree message must name profile.yaml among the walked "
        f"surfaces, not just declare a bare all-clear: {out!r}"
    )


def test_config_validate_reports_a_real_profile_finding(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: witness ⑥ (half B) — a real unknown profile key reaches the
    CLI's own labeled section, naming the agent."""
    from reyn.interfaces.cli.commands.config import _validate

    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    agent_dir = tmp_path / ".reyn" / "agents" / "coder-brown"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: coder-brown\nrole: x\nbroker_identity: coder-brown\n",
        encoding="utf-8",
    )

    _validate()
    out = capsys.readouterr().out
    assert "coder-brown" in out and "broker_identity" in out, (
        f"expected the agent name and the unknown key in the CLI output: {out!r}"
    )
