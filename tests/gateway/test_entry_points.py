"""Tier 2: reyn.webhooks entry points resolve to the gateway plugin modules.

Regression backstop for #1807. The plugins→gateway rename (#1807/#1816) moved
the code (``src/reyn/plugins`` → ``src/reyn/gateway``) but the ``pyproject.toml``
entry points still pointed at the deleted ``reyn.plugins.*`` — so webhook plugin
discovery (``load_webhook_plugins`` → ``ep.load()``) silently failed at runtime
(``ModuleNotFoundError`` caught + skipped). No test exercised entry-point
resolution, so CI stayed green while inbound webhook plugins were broken.

This pins resolution against the installed entry-point metadata: ``ep.load()``
raises ``ModuleNotFoundError`` if the target module is wrong, so a future rename
that forgets the entry points fails here instead of silently in production.
"""
from __future__ import annotations

import importlib.metadata

import pytest


@pytest.mark.parametrize("plugin_name", ["sample_slack", "sample_line"])
def test_webhook_entry_point_resolves_to_gateway(plugin_name):
    """Tier 2: the reyn.webhooks entry point for *plugin_name* points at the
    ``reyn.gateway`` module and loads its ``register_router`` (not the deleted
    ``reyn.plugins``)."""
    eps = [
        ep
        for ep in importlib.metadata.entry_points(group="reyn.webhooks")
        if ep.name == plugin_name
    ]
    assert eps, f"no reyn.webhooks entry point named {plugin_name!r}"
    ep = eps[0]
    assert ep.module.startswith("reyn.gateway."), (
        f"entry point {plugin_name!r} points at module {ep.module!r}; "
        f"expected reyn.gateway.* (the #1807 rename target)"
    )
    # Would raise ModuleNotFoundError on the #1807 stale-entry-point bug.
    register_router = ep.load()
    assert callable(register_router)
