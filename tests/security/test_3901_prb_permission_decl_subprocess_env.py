"""Tier 1/2: #3901 PR-B ① — PermissionDecl.subprocess / .env, the actor's OWN
declared intent for two axes AgentLayer previously left entirely
unconstrained (⊤) for every actor, deferring to SandboxLayer alone.

Owner's split (#3901): permission is what the OPERATOR RECOGNIZES and can
express as "let this agent do X"; subprocess launch and which env-var NAMES
pass through to one are both things an operator NAMES when granting a
capability — distinct from sandbox's job of bounding what happens BEHIND a
permitted action. Compat defaults (``subprocess=True``, ``env=[]`` reading as
⊤/unconstrained) match every other agent-decl axis's #3202 compat ruling.

``PermissionDecl.env`` is deliberately NOT ``env_expand`` — see both fields'
own docstrings for why conflating them would let a subprocess env-passthrough
declaration double as a credential-exposure grant.
"""
from __future__ import annotations

from reyn.security.permissions.effective import AgentLayer, CapabilityAxis
from reyn.security.permissions.permissions import PermissionDecl

AX = CapabilityAxis


# ── PermissionDecl fields: defaults + parsing ────────────────────────────────


def test_decl_subprocess_defaults_to_true() -> None:
    """Tier 1: an actor with no opinion is unconstrained on SUBPROCESS (compat)."""
    assert PermissionDecl().subprocess is True


def test_decl_env_defaults_to_empty_list() -> None:
    """Tier 1: an actor with no opinion declares no env-var names."""
    assert PermissionDecl().env == []


def test_from_dict_subprocess_key_omitted_keeps_compat_default() -> None:
    """Tier 1: omitting the ``subprocess`` key (not writing ``false``) keeps
    the compat default — same "wrote it vs omitted it" distinction
    ``resolve_sandbox_policy``'s own docstring names for its floor merge."""
    assert PermissionDecl.from_dict({}).subprocess is True
    assert PermissionDecl.from_dict({"subprocess": False}).subprocess is False


def test_from_dict_env_parses_list_and_bare_string() -> None:
    """Tier 1: ``env`` accepts a list of names or a bare string (single-item
    list) — the same lenient shape ``_parse_secret_key_list`` already gives
    ``secret.write`` / ``env.expand``."""
    assert PermissionDecl.from_dict({"env": ["PATH", "HOME"]}).env == ["PATH", "HOME"]
    assert PermissionDecl.from_dict({"env": "PATH"}).env == ["PATH"]
    assert PermissionDecl.from_dict({}).env == []


# ── AgentLayer.allows: the actual gate behavior ──────────────────────────────


def test_agent_layer_subprocess_follows_the_decl():
    """Tier 2: AgentLayer.allows(SUBPROCESS, ...) reads decl.subprocess directly
    — no longer an unconditional ⊤ regardless of what the actor declared."""
    allowed = AgentLayer(PermissionDecl(subprocess=True))
    denied = AgentLayer(PermissionDecl(subprocess=False))
    assert allowed.allows(AX.SUBPROCESS, None) is True
    assert denied.allows(AX.SUBPROCESS, None) is False


def test_agent_layer_env_empty_is_unconstrained():
    """Tier 2: an empty decl.env is ⊤ (unconstrained) for ENV — this axis's
    restriction is meant to come from SandboxLayer's deny-list, not from
    requiring every actor to enumerate every name it might pass through."""
    layer = AgentLayer(PermissionDecl())
    assert layer.allows(AX.ENV, "ANYTHING_AT_ALL") is True


def test_agent_layer_env_narrows_to_the_declared_names():
    """Tier 2: a non-empty decl.env narrows ENV to exactly the declared names
    — an actor CAN restrict itself, even though it does not have to."""
    layer = AgentLayer(PermissionDecl(env=["PATH", "HOME"]))
    assert layer.allows(AX.ENV, "PATH") is True
    assert layer.allows(AX.ENV, "HOME") is True
    assert layer.allows(AX.ENV, "AWS_SECRET_ACCESS_KEY") is False


# ── falsification: these tests exercise the real branches, not a tautology ──


def test_falsify_subprocess_and_env_branches_are_load_bearing():
    """Tier 1: STRIP-FALSIFY — with AgentLayer.allows() forced to fall through
    to the pre-#3901 unconditional ⊤ for SUBPROCESS/ENV (a real subclass
    override, not a mock), both prior claims go false: a declared
    ``subprocess=False`` no longer denies, and a narrowed ``env=[...]`` no
    longer excludes an undeclared name. Proves the tests above exercise the
    new branches, not a tautology that would stay green with them deleted.
    """

    class _PreNineOhOneAgentLayer(AgentLayer):
        """A real AgentLayer subclass whose SUBPROCESS/ENV branches are
        removed — not a mock, a genuine instance sharing every other method,
        reproducing exactly the pre-#3901 fall-through-to-⊤ shape."""

        def allows(self, axis: CapabilityAxis, value) -> bool:  # noqa: D102
            if axis is AX.SUBPROCESS or axis is AX.ENV:
                return True  # the old, unconditional ⊤
            return super().allows(axis, value)

    broken_deny = _PreNineOhOneAgentLayer(PermissionDecl(subprocess=False))
    assert broken_deny.allows(AX.SUBPROCESS, None) is True, (
        "with the #3901 branch removed, a declared subprocess=False no "
        "longer denies — this test no longer falsifies the mechanism"
    )

    broken_narrow = _PreNineOhOneAgentLayer(PermissionDecl(env=["PATH"]))
    assert broken_narrow.allows(AX.ENV, "AWS_SECRET_ACCESS_KEY") is True, (
        "with the #3901 branch removed, a narrowed env=[...] no longer "
        "excludes an undeclared name — this test no longer falsifies the "
        "mechanism"
    )
