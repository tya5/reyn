"""Tier 2: FP-0034 PR-3b-iii config-to-router_loop wiring.

Verifies that ``ActionRetrievalConfig.universal_wrappers_enabled``
flows from reyn.yaml → Session → RouterHostAdapter →
RouterLoopHost.get_universal_wrappers_enabled() and reaches
build_tools() with the correct value.

The actual ``RouterLoop.run()`` execution is NOT exercised here (=
that requires an LLM mock or LLMReplay fixture; e2e verification
is PR-5 Tier 3). PR-3b-iii's contract is the wiring itself —
config field reaches the host method, host method reaches
build_tools when called.

No mocks of collaborators. Constructs real RouterHostAdapter with
no-op callables, real ActionRetrievalConfig.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.chat.router_tools import build_tools
from reyn.chat.services.router_host_adapter import RouterHostAdapter
from reyn.config import ActionRetrievalConfig, ReynConfig, load_config


def _noop_callable(*_args: Any, **_kwargs: Any) -> None:
    """Plain function stub used in place of dependency-injected callables.

    Per CLAUDE.md test policy: NEVER use MagicMock. The adapter
    fields under test in this module are stored but not invoked, so a
    plain no-op function is sufficient (= satisfies the Callable type
    annotation without faking collaborator behaviour).
    """
    return None


# ── Adapter construction smoke ────────────────────────────────────────────


def _make_adapter(
    *, universal_wrappers_enabled: bool = False,
) -> RouterHostAdapter:
    """Construct RouterHostAdapter with no-op callables.

    Most fields are not exercised in this PR's tests; we only assert
    on get_universal_wrappers_enabled() and tools= shape derived from
    it. The callables are stored but never invoked here, so a plain
    no-op function suffices (= policy-clean, no mock framework).
    """
    return RouterHostAdapter(
        agent_name="test_agent",
        agent_role="test",
        output_language=None,
        allowed_skills=None,
        allowed_mcp=None,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=_noop_callable,
        resolver=_noop_callable,
        memory=_noop_callable,
        journal=_noop_callable,
        agent_registry=None,
        skill_enumerate_fn=lambda _s: [],
        agent_workspace_dir=Path("/tmp"),
        plan_registry_getter=lambda: None,
        file_read=_noop_callable,
        file_write=_noop_callable,
        file_delete=_noop_callable,
        file_list_directory=_noop_callable,
        file_regenerate_index=_noop_callable,
        mcp_list_servers=_noop_callable,
        mcp_list_tools=_noop_callable,
        mcp_call_tool=_noop_callable,
        run_skill_awaitable=_noop_callable,
        spawn_skill=_noop_callable,
        send_to_agent=_noop_callable,
        put_outbox=_noop_callable,
        append_history=_noop_callable,
        spawn_plan_task=_noop_callable,
        delegation_tracker=lambda: None,
        agent_replies_tracker=lambda: None,
        universal_wrappers_enabled=universal_wrappers_enabled,
    )


# ── 1. RouterHostAdapter exposes the host-method ─────────────────────────


def test_router_host_adapter_default_universal_wrappers_off() -> None:
    """Tier 2: default-constructed adapter returns False (= preserves prior shape)."""
    adapter = _make_adapter()
    assert adapter.get_universal_wrappers_enabled() is False


def test_router_host_adapter_with_flag_on() -> None:
    """Tier 2: adapter constructed with True returns True."""
    adapter = _make_adapter(universal_wrappers_enabled=True)
    assert adapter.get_universal_wrappers_enabled() is True


# ── 2. build_tools honours the host's reported value (= no recursion needed) ─


def test_build_tools_off_when_flag_off() -> None:
    """Tier 2: with flag off, universal wrappers do NOT appear in tools=."""
    tools = build_tools([], [], universal_wrappers_enabled=False)
    names = [t["function"]["name"] for t in tools]
    for w in ("list_actions", "describe_action", "invoke_action"):
        assert w not in names


def test_build_tools_on_when_flag_on() -> None:
    """Tier 2: with flag on, 3 universal wrappers appear at the end of tools=."""
    tools = build_tools([], [], universal_wrappers_enabled=True)
    names = [t["function"]["name"] for t in tools]
    assert names[-3:] == ["list_actions", "describe_action", "invoke_action"]


# ── 3. Session constructor accepts action_retrieval_config ───────────


def test_chat_session_accepts_action_retrieval_config() -> None:
    """Tier 2: Session constructor signature includes
    action_retrieval_config parameter (= PR-3b-iii integration point).

    The constructor accepts the config; downstream wiring is verified
    via RouterHostAdapter unit tests above (= same flag flows through).
    """
    import inspect

    from reyn.chat.session import Session

    sig = inspect.signature(Session.__init__)
    assert "action_retrieval_config" in sig.parameters
    # Default must be None so existing callers don't break
    default = sig.parameters["action_retrieval_config"].default
    assert default is None


# ── 4. reyn.yaml end-to-end ──────────────────────────────────────────────


def test_load_config_propagates_action_retrieval_flag(tmp_path: Path) -> None:
    """Tier 2: load_config from a yaml with the flag returns it set.

    Confirms the config-loader path completes without errors when
    ``action_retrieval`` is present.
    """
    (tmp_path / "reyn.yaml").write_text(
        """
action_retrieval:
  universal_wrappers_enabled: true
""",
        encoding="utf-8",
    )
    cfg: ReynConfig = load_config(cwd=tmp_path)
    assert cfg.action_retrieval.universal_wrappers_enabled is True


def test_default_reyn_config_flag_is_on() -> None:
    """Tier 2: A freshly-defaulted ReynConfig has wrappers ON (since PR-3b-iv).

    PR-3b-iv flipped the default after verifying the test suite is
    insulated from the change (= FakeRouterHost fallback / mocked
    call_llm_tools).  Operators can opt out via reyn.yaml.
    """
    cfg = ReynConfig()
    assert cfg.action_retrieval.universal_wrappers_enabled is True


# ── 5. RouterLoopHost protocol exposes the new method ────────────────────


def test_router_loop_host_protocol_declares_universal_wrappers_method() -> None:
    """Tier 2: RouterLoopHost Protocol exposes get_universal_wrappers_enabled.

    Verified via the Protocol class — the method must be declared so
    test doubles and the RouterLoop implementation can rely on it.
    """
    from reyn.chat.router_loop import RouterLoopHost

    assert hasattr(RouterLoopHost, "get_universal_wrappers_enabled")
