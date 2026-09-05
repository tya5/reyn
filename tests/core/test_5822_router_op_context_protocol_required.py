"""Tier 2: OS invariant -- #5822 (architect ruling), security co-vet finding
on PR #5821.

`RouterCallerState.op_context_factory` used to be populated via
`getattr(host, "make_router_op_context", None)` (`reyn.tools.types.
build_resource_caller_state`) -- a STRUCTURAL Protocol check that answers
"no" with a silent `None`, never an exception. `None` here means
`tools/exec.py`'s minimal-synthesis OpContext path, which -- per #5818,
the real incident this traces back to -- can never see the operator's
own `reyn.yaml` `sandbox.mode: strict`. A host that simply forgot to
implement `make_router_op_context` (not a deliberate "no capability"
declaration) silently degraded every op it touches to that same
unenforced floor, with zero signal.

architect's own measurement (co-vet on this PR): only ONE test in the
whole suite actually drives the minimal-synthesis branch and asserts on
it (`test_sandbox_model_completion_1339.py::
test_minimal_synthesis_path_enforces_the_floor_not_the_op_default`),
and it names its own condition explicitly (`router_state=None`) rather
than depending on a host lacking a method -- so making `FakeRouterHost`
NOT structurally satisfy the Protocol (the type-level alternative
considered and REJECTED) would have preserved 36 other tests' merely
INCIDENTAL pass-through of that branch, never actual coverage of it
("a helper that's incomplete, so branches reached [through it] are
testing the helper's incompleteness, not the branch itself" --
architect, verbatim).

Ruling: read `host.make_router_op_context` DIRECTLY (no `getattr`
default) -- a host reaching `build_resource_caller_state` must
genuinely implement the method; one that doesn't now raises at
construction instead of manufacturing a `None` indistinguishable from
the legitimate `rs is None` / direct-`RouterCallerState`-construction
paths (`capability_visibility.py`, `router_tools.py`) that
`tools/exec.py`'s own minimal-synthesis branch still serves -- that
branch itself is NOT removed (`rs is None` stays a real, structural
reason to reach it).

Real `RouterCallerState`/`build_resource_caller_state`/`FakeRouterHost`
throughout -- no mocks.
"""
from __future__ import annotations

import pytest

from reyn.tools.types import build_resource_caller_state
from tests._support.router_loop import FakeRouterHost


class _HostMissingOpContextFactory:
    """A minimal, real object genuinely lacking `make_router_op_context`
    -- not a mock, just a plain class implementing only what
    `build_resource_caller_state` reads BEFORE it would reach the
    now-direct attribute access, so the AttributeError this test expects
    comes from that ONE line, not an earlier, unrelated omission."""

    agent_name = "no-op-context-factory-host"
    agent_role = ""

    def list_available_skills(self) -> list[dict]:
        return []

    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> "dict | None":
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def get_chains(self):
        return None

    def get_inbox_depth(self) -> "int | None":
        return None

    def get_web_fetch_allowed(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_host_missing_make_router_op_context_raises_at_construction() -> None:
    """Tier 2: #5822 -- a host that does not implement `make_router_op_context`
    must fail LOUDLY, at `build_resource_caller_state` construction time --
    never silently produce `op_context_factory=None` (the #5818-class hazard:
    a real operator setting, unenforced, with nothing to notice)."""
    host = _HostMissingOpContextFactory()
    with pytest.raises(AttributeError, match="make_router_op_context"):
        await build_resource_caller_state(host)


@pytest.mark.asyncio
async def test_a_conforming_host_still_builds_a_working_factory() -> None:
    """Tier 2: #5822 accept-side -- a host that DOES implement
    `make_router_op_context` (here, the shared `FakeRouterHost` test
    fixture, which gained the method in this same PR) builds a
    `RouterCallerState` whose `op_context_factory` is real and callable,
    producing a genuine `OpContext` -- not merely "did not raise"."""
    host = FakeRouterHost()
    router_state = await build_resource_caller_state(host)
    assert router_state.op_context_factory is not None
    op_ctx = router_state.op_context_factory()
    assert op_ctx.actor == "fake_router_host"
