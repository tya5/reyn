"""Tier 2: #4206 Slice B (#4724) — Session/RouterHostAdapter wiring for the
7 ``cost.*.warn_ratio`` overrides.

Design C (lead-coder ruling): ``Session.warn_ratio_overrides()`` builds a
dotted-key -> ratio mapping from the SAME session/agent-preference files
slice 1/2 already read (``config.yaml``/``profile.yaml``), and hands it to
``BudgetTracker.check_pre_llm``/``record_llm`` via a callback chain
(``RouterHostAdapter.warn_ratio_overrides_fn`` -> ``RouterLoop.
_warn_ratio_overrides()`` -> ``call_llm_tools(warn_ratio_overrides=...)``)
— the tracker itself never resolves a session/agent identity.

Real ``Session`` (``make_session``) + real ``RouterHostAdapter`` reached
through ``session.router_host``, never a stand-in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.profile import AgentProfile
from tests._support.agent_session import make_session


def _agent_dir(session) -> Path:
    return session.workspace_dir


def _session_config_path(session) -> Path:
    return Path(session._snapshot_path).parent / "config.yaml"


def _write_agent_preferences(session, name: str, preferences: dict) -> None:
    profile = AgentProfile.new(name)
    object.__setattr__(profile, "preferences", preferences)
    profile.save(_agent_dir(session))


def test_warn_ratio_overrides_is_empty_with_no_preferences_set(tmp_path: Path):
    """Tier 2: (accept-side) the common case — no agent/session preference
    file at all — returns {} (tracker falls back to project defaults
    entirely, byte-identical to before Slice B)."""
    session = make_session(
        agent_name="warn-ratio-1", workspace_state_dir=tmp_path / ".reyn",
    )
    assert session.warn_ratio_overrides() == {}


def test_warn_ratio_overrides_includes_only_the_agent_set_key(tmp_path: Path):
    """Tier 2: an agent-layer preferences.cost.*.warn_ratio entry appears
    in the returned mapping; unset cost.* keys are simply absent (not
    filled with a default this Session doesn't itself hold)."""
    session = make_session(
        agent_name="warn-ratio-2", workspace_state_dir=tmp_path / ".reyn",
    )
    _write_agent_preferences(
        session, "warn-ratio-2",
        {"cost.per_agent_tokens.warn_ratio": 0.5},
    )

    overrides = session.warn_ratio_overrides()
    assert overrides == {"cost.per_agent_tokens.warn_ratio": 0.5}


def test_warn_ratio_overrides_session_layer_wins_over_agent_layer(tmp_path: Path):
    """Tier 2: THE composition witness — a session-layer config.yaml
    override wins over the agent-layer profile.yaml override for the SAME
    key, matching output_language/reasoning_display's own precedence."""
    import yaml

    session = make_session(
        agent_name="warn-ratio-3", workspace_state_dir=tmp_path / ".reyn",
    )
    _write_agent_preferences(
        session, "warn-ratio-3",
        {"cost.per_agent_tokens.warn_ratio": 0.5},
    )
    cfg_path = _session_config_path(session)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"name": "_session_x", "preferences": {"cost.per_agent_tokens.warn_ratio": 0.2}}),
        encoding="utf-8",
    )

    overrides = session.warn_ratio_overrides()
    assert overrides == {"cost.per_agent_tokens.warn_ratio": 0.2}


def test_warn_ratio_overrides_is_a_live_re_read(tmp_path: Path):
    """Tier 2: (accept-side) editing profile.yaml AFTER Session construction
    takes effect on the next call — same live-re-read shape output_language
    /reasoning_display already established."""
    session = make_session(
        agent_name="warn-ratio-4", workspace_state_dir=tmp_path / ".reyn",
    )
    assert session.warn_ratio_overrides() == {}

    _write_agent_preferences(
        session, "warn-ratio-4",
        {"cost.rate_limit_warn_ratio": 0.3},
    )

    assert session.warn_ratio_overrides() == {"cost.rate_limit_warn_ratio": 0.3}


# ── RouterHostAdapter callback wiring ────────────────────────────────────


def test_router_host_warn_ratio_overrides_reflects_the_live_session_property(tmp_path: Path):
    """Tier 2: THE end-to-end witness — RouterHostAdapter.warn_ratio_overrides()
    (what RouterLoop actually consults) reflects the SAME live
    session/agent-preference override as Session.warn_ratio_overrides(),
    via the warn_ratio_overrides_fn callback wired at construction."""
    session = make_session(
        agent_name="warn-ratio-5", workspace_state_dir=tmp_path / ".reyn",
    )
    assert session.router_host.warn_ratio_overrides() == {}

    _write_agent_preferences(
        session, "warn-ratio-5",
        {"cost.daily_tokens.warn_ratio": 0.6},
    )

    assert session.warn_ratio_overrides() == {"cost.daily_tokens.warn_ratio": 0.6}
    assert session.router_host.warn_ratio_overrides() == {"cost.daily_tokens.warn_ratio": 0.6}


def test_warn_ratio_overrides_fn_none_falls_back_to_empty_dict():
    """Tier 2: (accept-side) a RouterHostAdapter built WITHOUT
    warn_ratio_overrides_fn (every pre-Slice-B caller, every existing test
    host) returns {} — byte-identical to before this slice."""
    from tests._support.router_host_adapter import make_adapter

    adapter = make_adapter(universal_wrappers_enabled=False)
    assert adapter.warn_ratio_overrides() == {}


# ── end-to-end: RouterLoop -> call_llm_tools carries the resolved overrides ─


class _CapturingFinishLLM:
    """Real callable (testing policy, mirrors test_4700_prompt_cache_key.py's
    own class of the same name): records the kwargs of the LAST call and
    returns a finish (no tool_calls) so RouterLoop.run terminates after one
    round."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return LLMToolCallResult(
            content="ok", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )


@pytest.mark.asyncio
async def test_router_loop_carries_the_resolved_overrides_to_call_llm_tools(tmp_path: Path):
    """Tier 2: THE full end-to-end witness — a real turn through
    RouterLoop.run, driven by a real Session with an agent-layer
    cost.*.warn_ratio preference set, delivers that SAME resolved mapping
    as the ``warn_ratio_overrides`` kwarg the injected LLM callable
    receives (standing in for ``call_llm_tools`` itself)."""
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.router_loop import RouterLoop

    session = make_session(
        agent_name="warn-ratio-6", workspace_state_dir=tmp_path / ".reyn",
        resolver=ModelResolver({"standard": "openai/gpt-4o"}),
    )
    _write_agent_preferences(
        session, "warn-ratio-6",
        {"cost.per_agent_tokens.warn_ratio": 0.5},
    )

    llm = _CapturingFinishLLM()
    await RouterLoop(host=session.router_host, chain_id="c1", llm_caller=llm).run("hi", [])

    assert llm.last_kwargs.get("warn_ratio_overrides") == {
        "cost.per_agent_tokens.warn_ratio": 0.5,
    }
