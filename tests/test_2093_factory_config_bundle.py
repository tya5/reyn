"""Tier 2: #2093 — the SessionFactoryConfig bundle is a byte-identical consolidation.

The five session-factory sites previously threaded 11 uniform, config-derived args by
hand into build_scoped_chat_session (8) + AgentRegistry (3) — a per-arg propagation gap
that twice silently missed a site (sandbox_config, delegation_capability_default). The
bundle's ``from_config`` is now the single mapping point.

This pins the consolidation as byte-identical: every bundle field resolves to the EXACT
same config source the sites passed before (object identity, so no value can drift). A
wrong/missing mapping in from_config → RED, naming the field.
"""
from __future__ import annotations

from reyn.config.loader import load_config
from reyn.runtime.factory_config import SessionFactoryConfig


def test_from_config_maps_each_field_to_its_config_source() -> None:
    """Tier 2: each bundle field is the SAME object the factories read directly — the
    byte-identical mapping. (A typo'd source in from_config breaks the matching
    identity assertion.)"""
    config = load_config()
    fc = SessionFactoryConfig.from_config(config)

    # build_scoped_chat_session uniform config (8)
    assert fc.sandbox_config is config.sandbox
    assert fc.multimodal_config is config.multimodal
    assert fc.action_retrieval_config is config.action_retrieval
    assert fc.embedding_config is config.embedding
    assert fc.router_config is config.llm.router
    assert fc.retry_config is config.llm.retry
    assert fc.tool_calls_op_loop_skills is config.tool_calls_op_loop_skills
    assert fc.chat_tool_use_scheme == config.tool_use.chat
    # AgentRegistry uniform config (3)
    assert fc.workspace_capture == config.time_travel.workspace_capture
    assert fc.act_turn_capture == config.time_travel.act_turn_capture
    assert fc.delegation_capability_default == config.delegation.capability_default


def test_bundle_is_frozen() -> None:
    """Tier 2: the bundle is immutable (a frozen dataclass) — a site can't mutate a
    shared bundle and leak the change to another consumer."""
    import dataclasses

    import pytest

    fc = SessionFactoryConfig.from_config(load_config())
    with pytest.raises(dataclasses.FrozenInstanceError):
        fc.delegation_capability_default = "deny"  # type: ignore[misc]
