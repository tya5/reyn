"""Tier 2: #2093 — the SessionFactoryConfig bundle is a byte-identical consolidation.

The five session-factory sites previously threaded the uniform, config-derived args by
hand into build_scoped_chat_session (8) + AgentRegistry — a per-arg propagation gap
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
    assert fc.embedding_config is config.embedding
    assert fc.router_config is config.llm.router
    assert fc.retry_config is config.llm.retry
    # FP-0066 P4b: chat_tool_use_scheme is now the RESOLVED concrete scheme
    # name (via P4a's resolve_scheme_for_transport over config.tool_use.
    # scheme x .transport), not a bare passthrough of a single config field —
    # the default (scheme=enumerate-all, transport=tool_calls) resolves to
    # the SAME concrete name the old default (chat=enumerate-all) did
    # (byte-identical default behavior).
    from reyn.tools.transport import Transport, resolve_scheme_for_transport
    assert fc.chat_tool_use_scheme == resolve_scheme_for_transport(
        config.tool_use.scheme, Transport(config.tool_use.transport)
    )
    assert fc.chat_tool_use_scheme == "enumerate-all"
    # AgentRegistry uniform config
    assert fc.delegation_capability_default == config.delegation.capability_default


def test_bundle_is_frozen() -> None:
    """Tier 2: the bundle is immutable (a frozen dataclass) — a site can't mutate a
    shared bundle and leak the change to another consumer."""
    import dataclasses

    import pytest

    fc = SessionFactoryConfig.from_config(load_config())
    with pytest.raises(dataclasses.FrozenInstanceError):
        fc.delegation_capability_default = "deny"  # type: ignore[misc]
