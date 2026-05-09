"""Tests for PermissionResolver.require_mcp_install (ADR-0029).

Covers:
- decl.mcp_install=False → immediate PermissionError (decl guard)
- permissions.mcp_install: deny → PermissionError (config deny path)
- permissions.mcp_install: allow → passes without prompt (config allow path)
- permissions.mcp_install: ask (default/unset) → interactive prompt called
- approval key mcp_install:<server_id> persisted + reused on re-invoke
- scope tier interaction (project deny overridden by local allow)
- REYN_MCP_INSTALL_AUTO_APPROVE=1 → prompt skipped, auto-approved
- startup_guard emits warning when decl.mcp_install + config deny
- PermissionDecl.from_dict parses mcp_install field correctly
- existing require_mcp() unaffected by ADR-0029 changes
"""
from __future__ import annotations

import asyncio
import os
import warnings
from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_resolver(
    tmp_path: Path,
    *,
    config: dict | None = None,
    interactive: bool = False,
) -> PermissionResolver:
    """Build a PermissionResolver backed by tmp_path."""
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=interactive,
    )


class _RecordingBus:
    """Real InterventionBus implementation that records prompts and answers them.

    `answer_choice` controls what choice_id is returned for every prompt.
    Default is "no" (deny). Set to "yes" or "always" to approve.
    """

    def __init__(self, answer_choice: str = "no") -> None:
        self.requests: list[UserIntervention] = []
        self.answer_choice = answer_choice

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id=self.answer_choice)


def _run(coro):
    return asyncio.run(coro)


def _make_skill_with_decl(decl: PermissionDecl) -> Skill:
    """Construct a minimal in-memory Skill with the given PermissionDecl."""
    phase = Phase(
        name="main",
        input_schema={},
        instructions="test",
    )
    return Skill(
        name="test_skill",
        entry_phase="main",
        phases={"main": phase},
        graph=SkillGraph(transitions={}),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
        permissions=decl,
    )


# ── Tier 2: decl guard ────────────────────────────────────────────────────────


def test_decl_false_raises_immediately(tmp_path):
    """Tier 2: require_mcp_install raises PermissionError when decl.mcp_install=False.

    The decl check is the first gate — no config or prompt is consulted.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = PermissionDecl(mcp_install=False)
    bus = _RecordingBus()

    with pytest.raises(PermissionError, match="mcp_install: true"):
        _run(resolver.require_mcp_install(decl, "github", bus))

    # No prompt was issued — the decl guard short-circuits before any interaction
    assert bus.requests == []


# ── Tier 2: config deny path ──────────────────────────────────────────────────


def test_config_deny_raises(tmp_path):
    """Tier 2: permissions.mcp_install: deny in config raises PermissionError.

    Even when decl.mcp_install=True, a hard config deny must block the install.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "deny"})
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus()

    with pytest.raises(PermissionError, match="denied by config"):
        _run(resolver.require_mcp_install(decl, "some-server", bus))

    # No prompt — config deny short-circuits before prompting
    assert bus.requests == []


# ── Tier 2: config allow path ─────────────────────────────────────────────────


def test_config_allow_passes_without_prompt(tmp_path):
    """Tier 2: permissions.mcp_install: allow in config lets install proceed silently.

    No prompt should be issued; the call returns without raising.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus()

    _run(resolver.require_mcp_install(decl, "github", bus))

    # Config allow → no prompt needed
    assert bus.requests == []


# ── Tier 2: ask / prompt path ─────────────────────────────────────────────────


def test_ask_default_invokes_prompt(tmp_path):
    """Tier 2: default config (ask / unset) triggers interactive prompt via bus.

    The bus receives the permission prompt when no config policy is set.
    """
    resolver = _make_resolver(tmp_path, config={}, interactive=True)
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus(answer_choice="yes")  # approve once

    _run(resolver.require_mcp_install(decl, "my-server", bus))

    assert len(bus.requests) == 1
    iv = bus.requests[0]
    assert iv.kind == "permission.generic"
    assert "mcp_install:my-server" in iv.prompt or "my-server" in iv.detail


def test_ask_prompt_deny_raises(tmp_path):
    """Tier 2: prompt answer 'no' results in PermissionError.

    Ensures the interactive path correctly propagates user denial.
    """
    resolver = _make_resolver(tmp_path, config={}, interactive=True)
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus(answer_choice="no")

    with pytest.raises(PermissionError, match="denied by user"):
        _run(resolver.require_mcp_install(decl, "bad-server", bus))


# ── Tier 2: approval persistence + re-invoke uses saved ──────────────────────


def test_approval_persisted_and_reused(tmp_path):
    """Tier 2: 'always' approval persists mcp_install:<server_id> key and reuses it.

    Second call with a fresh resolver loaded from the same approvals.yaml must
    pass without prompting — the saved approval is honoured.
    """
    resolver = _make_resolver(tmp_path, config={}, interactive=True)
    decl = PermissionDecl(mcp_install=True)

    # First call: user answers 'always' → persisted to approvals.yaml
    bus1 = _RecordingBus(answer_choice="always")
    _run(resolver.require_mcp_install(decl, "persistent-server", bus1))
    assert len(bus1.requests) == 1

    # Verify approval key is in the persisted file
    approvals_path = tmp_path / ".reyn" / "approvals.yaml"
    assert approvals_path.exists()
    import yaml
    saved = yaml.safe_load(approvals_path.read_text(encoding="utf-8"))
    assert saved.get("mcp_install:persistent-server") is True

    # Second call: fresh resolver reads saved approvals — no prompt needed
    resolver2 = _make_resolver(tmp_path, config={}, interactive=True)
    bus2 = _RecordingBus(answer_choice="no")
    _run(resolver2.require_mcp_install(decl, "persistent-server", bus2))
    assert bus2.requests == []  # saved approval was used


# ── Tier 2: scope tier interaction ────────────────────────────────────────────


def test_project_deny_blocks_despite_decl(tmp_path):
    """Tier 2: project-scope deny overrides skill decl intent.

    Simulates a project config that prevents any mcp_install regardless of
    what individual skills declare.
    """
    # Only project-level config provided (no local override)
    resolver = _make_resolver(tmp_path, config={"mcp_install": "deny"})
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus()

    with pytest.raises(PermissionError, match="denied by config"):
        _run(resolver.require_mcp_install(decl, "any-server", bus))


def test_local_allow_overrides_project_deny_when_config_merged(tmp_path):
    """Tier 2: local-scope allow wins when merged config resolves to allow.

    The PermissionResolver receives already-merged config (merger is upstream
    in the config loader). When the merged result is 'allow', install proceeds.
    This test simulates the merged outcome: project=deny + local=allow → allow.
    """
    # The config loader merges tiers before constructing the resolver.
    # Local-scope 'allow' winning over project-scope 'deny' is expressed here
    # as the merged config containing 'allow'.
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus()

    _run(resolver.require_mcp_install(decl, "github", bus))
    assert bus.requests == []  # allow → no prompt


# ── Tier 2: REYN_MCP_INSTALL_AUTO_APPROVE escape hatch ───────────────────────


def test_auto_approve_env_skips_prompt(tmp_path, monkeypatch):
    """Tier 2: REYN_MCP_INSTALL_AUTO_APPROVE=1 skips prompt and auto-persists approval.

    CI / non-interactive environments set this env var to avoid hang.
    """
    monkeypatch.setenv("REYN_MCP_INSTALL_AUTO_APPROVE", "1")

    resolver = _make_resolver(tmp_path, config={}, interactive=True)
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus(answer_choice="no")  # would deny if prompted

    # Must succeed without prompting
    _run(resolver.require_mcp_install(decl, "ci-server", bus))
    assert bus.requests == []  # no prompt issued

    # Approval was persisted for future runs
    approvals_path = tmp_path / ".reyn" / "approvals.yaml"
    assert approvals_path.exists()
    import yaml
    saved = yaml.safe_load(approvals_path.read_text(encoding="utf-8"))
    assert saved.get("mcp_install:ci-server") is True


def test_auto_approve_env_not_set_does_prompt(tmp_path, monkeypatch):
    """Tier 2: Without REYN_MCP_INSTALL_AUTO_APPROVE, prompt fires normally.

    Negative counterpart to test_auto_approve_env_skips_prompt.
    """
    monkeypatch.delenv("REYN_MCP_INSTALL_AUTO_APPROVE", raising=False)

    resolver = _make_resolver(tmp_path, config={}, interactive=True)
    decl = PermissionDecl(mcp_install=True)
    bus = _RecordingBus(answer_choice="yes")

    _run(resolver.require_mcp_install(decl, "needs-prompt-server", bus))
    assert len(bus.requests) == 1  # prompt was issued


# ── Tier 2: startup_guard warning when decl.mcp_install + config deny ────────


def test_startup_guard_warns_on_decl_mcp_install_with_config_deny(tmp_path):
    """Tier 2: startup_guard emits UserWarning when skill declares mcp_install
    but config denies it — pre-flight signal before any install op runs.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "deny"})
    skill = _make_skill_with_decl(PermissionDecl(mcp_install=True))
    bus = _RecordingBus()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(resolver.startup_guard(skill, "test_skill", bus))

    matching = [w for w in caught if "mcp_install" in str(w.message)]
    assert matching, "Expected a UserWarning mentioning mcp_install"
    assert "deny" in str(matching[0].message).lower()


def test_startup_guard_no_warning_when_mcp_install_not_declared(tmp_path):
    """Tier 2: startup_guard emits no mcp_install warning when decl.mcp_install=False."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "deny"})
    skill = _make_skill_with_decl(PermissionDecl(mcp_install=False))
    bus = _RecordingBus()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(resolver.startup_guard(skill, "test_skill", bus))

    matching = [w for w in caught if "mcp_install" in str(w.message)]
    assert not matching, "No mcp_install warning expected when decl.mcp_install=False"


def test_startup_guard_no_warning_when_config_allow(tmp_path):
    """Tier 2: startup_guard emits no warning when config allows mcp_install."""
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    skill = _make_skill_with_decl(PermissionDecl(mcp_install=True))
    bus = _RecordingBus()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(resolver.startup_guard(skill, "test_skill", bus))

    matching = [w for w in caught if "mcp_install" in str(w.message)]
    assert not matching


# ── Tier 2: PermissionDecl.from_dict parses mcp_install ──────────────────────


def test_from_dict_parses_mcp_install_true():
    """Tier 2: PermissionDecl.from_dict reads mcp_install: true from frontmatter dict."""
    decl = PermissionDecl.from_dict({"mcp_install": True})
    assert decl.mcp_install is True


def test_from_dict_mcp_install_defaults_false():
    """Tier 2: PermissionDecl.from_dict defaults mcp_install to False when absent."""
    decl = PermissionDecl.from_dict({"shell": True})
    assert decl.mcp_install is False


def test_from_dict_empty_gives_false():
    """Tier 2: PermissionDecl.from_dict(None) produces mcp_install=False."""
    decl = PermissionDecl.from_dict(None)
    assert decl.mcp_install is False


# ── Tier 2: require_mcp() unaffected (semantic compatibility) ─────────────────


def test_require_mcp_unaffected_by_adr_0029(tmp_path):
    """Tier 2: existing require_mcp() behaviour is unaltered after ADR-0029 changes.

    mcp_install: True on decl must not interact with the mcp server runtime gate.
    """
    resolver = _make_resolver(tmp_path, config={"mcp.myserver": "allow"})
    # Skill declares both runtime mcp and install intent
    decl = PermissionDecl(mcp=["myserver"], mcp_install=True)
    bus = _RecordingBus()

    # require_mcp must still pass for a declared + config-approved server
    _run(resolver.require_mcp(decl, "myserver", bus))
    assert bus.requests == []

    # require_mcp must still raise for undeclared server regardless of mcp_install
    with pytest.raises(PermissionError, match="not declared in skill permissions"):
        _run(resolver.require_mcp(decl, "other-server", bus))
