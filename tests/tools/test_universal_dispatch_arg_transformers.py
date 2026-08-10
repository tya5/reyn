"""Tier 2: invoke_action forwards args VERBATIM — there is no transform layer.

The file this replaces pinned ``universal_dispatch``'s per-action arg shapers.
Two survived to #3429: ``_multi_agent_list_peers_args`` mapped a ``cluster`` arg
onto ``list_agents``' ``path``, and ``_multi_agent_delegate_args`` renamed a
``message`` arg to ``delegate_to_agent``'s ``request``. Neither key appeared in
the action's advertised schema, so both were capability that existed only on the
qualified route — a model calling the bare tool with ``cluster=`` got a
``path``-less call, and one calling the qualified spelling got a working one.

The #3429 decision was taken per-difference and independently of which NAME
survived (see the PR body): the ADVERTISED SCHEMA is the contract, so the
schema's behaviour is what is kept and the undeclared remap is what goes.
``message`` was additionally documented in its own docstring as compatibility
for "LLMs that still emit ``message`` from the pre-#882 era" — back-compat,
which is not a reason.

What is pinned here is the property that replaced them: ``invoke_action`` hands
the handler exactly the ``args`` the caller supplied. Real registry, real
handlers, no fakes — the assertion is on the args the handler observes.
"""
from __future__ import annotations

import asyncio

from reyn.core.events.events import EventLog
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_invoke_action


def _ctx(**router_state_kwargs) -> ToolContext:
    return ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(**router_state_kwargs),
    )


def test_invoke_action_forwards_args_verbatim() -> None:
    """Tier 2: the handler receives the caller's args unchanged — no key is
    renamed, defaulted, or dropped between wrapper and handler."""
    observed: list[str] = []

    def _list_agents_fn(path):
        observed.append(path)
        return []

    ctx = _ctx(list_agents_fn=_list_agents_fn)
    asyncio.run(_handle_invoke_action(
        {"action_name": "list_agents", "args": {"path": "writers"}}, ctx,
    ))
    assert observed == ["writers"]


def test_invoke_action_does_not_map_cluster_onto_path() -> None:
    """Tier 2: #3429 — the undeclared ``cluster`` → ``path`` remap is gone.

    ``list_agents``' schema declares ``path`` and nothing else, so a caller
    passing ``cluster`` gets the handler's own default for the absent ``path``
    rather than a silently-renamed argument."""
    observed: list[str] = []

    def _list_agents_fn(path):
        observed.append(path)
        return []

    ctx = _ctx(list_agents_fn=_list_agents_fn)
    asyncio.run(_handle_invoke_action(
        {"action_name": "list_agents", "args": {"cluster": "writers"}}, ctx,
    ))
    assert observed == [""], (
        "an undeclared 'cluster' arg was mapped onto 'path' — the transform "
        f"layer #3429 removed is back: {observed!r}"
    )


# test_invoke_action_does_not_rename_message_to_request (formerly here) —
# deleted in proposal 0067 P6 (#3978). It dispatched
# ``action_name="delegate_to_agent"`` to pin that #3429's undeclared
# ``message`` → ``request`` remap stayed gone. delegate_to_agent retired
# with no replacement action_name sharing that specific historical
# message/request alias, so the route this test dispatched through is now
# unreachable from any live caller — the six-question test-review
# discriminator ("who would miss this test") answers nobody: the transform
# layer it guarded against can't be exercised through a dead action_name
# either way. test_invoke_action_forwards_args_verbatim and
# test_invoke_action_does_not_map_cluster_onto_path above still cover the
# general "invoke_action does not remap undeclared args" property through
# live routes.
