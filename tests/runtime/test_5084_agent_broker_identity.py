"""Tier 2: #5084 ③ — ``AgentProfile.broker_identity``, a FLAT identity
column, the SAME shape as ``name``/``role`` — not a layered override axis
like ``preferences``/``bounding``/``base_dir``.

Architect ruling (issuecomment-5378399712 on #5084): the project layer has
no such value to override in the first place ("N agents, no single broker
id" — there is nothing for a project-wide default to mean), so this is not
a ①-capability/②-bounding/③-preference axis at all, just a plain per-agent
field. ``None`` (absent) means "this agent does not participate in broker
coordination" — owner's own words: the default agent stays admin-only and
does not need to join development. No workspace bound, no protect-at-use
gate: unlike ``base_dir`` (#5080/#5081), this value carries no filesystem
or exec capability, so there is nothing for a write-time-only check to
leave open at a second read path.

Real ``AgentProfile``/``AgentRegistry`` construction throughout — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(profile):
    raise RuntimeError("session factory not used in these write-side tests")


# ── round-trip ────────────────────────────────────────────────────────────


def test_broker_identity_round_trips_through_profile_yaml(tmp_path: Path) -> None:
    """Tier 2: a ``broker_identity`` written via
    ``AgentProfile.new(...).save(...)`` reads back unchanged — the
    write/read pair a human hand-editing
    ``profile.yaml`` relies on (#5084's own stated goal: declare an agent
    purely by writing the file, no slash command needed)."""
    agent_dir = tmp_path / "agents" / "coder-smith"
    profile = AgentProfile.new(
        "coder-smith", role="pairs on the coder role", broker_identity="coder-smith",
    )
    profile.save(agent_dir)

    loaded = AgentProfile.load(agent_dir)
    assert loaded.broker_identity == "coder-smith"
    # Positive control: an enumeration/parse bug that silently drops every
    # unrecognised key would also make this pass trivially — assert a
    # SIBLING field survives the same round trip too.
    assert loaded.role == "pairs on the coder role"


def test_broker_identity_omitted_from_yaml_when_absent(tmp_path: Path) -> None:
    """Tier 2: a profile with no ``broker_identity`` set writes no such key
    at all (the SAME "keep the on-disk shape minimal" convention
    ``allowed_mcp``/
    ``base_dir`` already follow — no ``null`` residue for a human editing
    the file by hand to puzzle over)."""
    agent_dir = tmp_path / "agents" / "plain"
    AgentProfile.new("plain").save(agent_dir)

    raw = (agent_dir / "profile.yaml").read_text(encoding="utf-8")
    assert "broker_identity" not in raw

    loaded = AgentProfile.load(agent_dir)
    assert loaded.broker_identity is None


# ── default is absent ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_created_through_the_normal_seam_has_no_broker_identity_by_default(
    tmp_path: Path,
) -> None:
    """Tier 2: ``registry.create_agent(name)`` — the ONE creation seam every
    surface (CLI / web / slash / the ``spawn_agent`` LLM tool) routes
    through — does not set ``broker_identity``. Absent is the correct
    default for every agent created this way — no name-based special case
    is needed anywhere because absence already means non-participation."""
    project_root = tmp_path / "project"
    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)

    profile = await reg.create_agent("coder-jones")

    assert profile.broker_identity is None
    reloaded = AgentProfile.load(project_root / ".reyn" / "agents" / "coder-jones")
    assert reloaded.broker_identity is None


def test_the_project_default_agent_bootstraps_with_no_broker_identity(
    tmp_path: Path,
) -> None:
    """Tier 2: the project's own ``default`` agent — auto-bootstrapped by
    ``AgentRegistry.__init__`` itself (``AgentProfile.new(DEFAULT_AGENT_
    NAME, ...)``, bypassing ``create_agent``'s seam entirely, so
    ``reyn chat`` works before any agent is explicitly created) — has no
    ``broker_identity`` either. Owner's own words: the default agent stays
    admin-only and does not need to join development; this is what makes
    that true without a single name-based branch anywhere in the runtime."""
    project_root = tmp_path / "project"
    AgentRegistry(project_root=project_root, session_factory=_no_factory)

    default_profile = AgentProfile.load(project_root / ".reyn" / "agents" / "default")
    assert default_profile.broker_identity is None
