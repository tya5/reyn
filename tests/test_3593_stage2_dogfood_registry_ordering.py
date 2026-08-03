"""Tier 2: #3593 (2) - dogfood's session factory hands CapabilityVisibility a
REAL registry back-reference at construction, through the real dogfood
bootstrap path (``_build_live_runner``), not by reading the code.

Background: ``interfaces/cli/commands/dogfood.py``'s per-scenario session
factory used to read ``_reg_cell[0] if _reg_cell else None`` with a comment
claiming "the registry cell may be empty during bootstrap". That comment was
never actually true: the factory closure is captured as ``session_factory=``
on ``AgentRegistry.__init__`` and is only ever INVOKED later, lazily, from
``AgentRegistry._construct_session`` (``self._factory(profile, ...)``) - which
requires an already-returned ``AgentRegistry`` instance to call. ``_make_registry``
appends that instance into ``_reg_cell`` immediately after constructing it,
BEFORE returning it to its own caller - so by the time anything could call
the factory, the cell is always populated. The conditional defended a window
that call-order already forecloses; #3593 (2) removes the dead defensive
code and this file witnesses the invariant it removed, through the real path,
rather than a re-implementation of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import reyn.runtime.capability_visibility as capability_visibility_mod
from reyn.dev.dogfood.scenarios import Scenario
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests._support.agent_session import make_session


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def _spy_on_capability_visibility_registry(monkeypatch) -> "list[object]":
    """Wrap the REAL ``CapabilityVisibility.__init__`` to record the ``registry``
    it is constructed with, then delegate to the real implementation unchanged
    (a spy, not a fake collaborator — the constructed object and its behaviour
    are the genuine ones; only the call is observed)."""
    captured: "list[object]" = []
    original_init = capability_visibility_mod.CapabilityVisibility.__init__

    def _spy_init(self, *, registry, **kwargs):
        captured.append(registry)
        original_init(self, registry=registry, **kwargs)

    monkeypatch.setattr(
        capability_visibility_mod.CapabilityVisibility, "__init__", _spy_init,
    )
    return captured


@pytest.mark.asyncio
async def test_dogfood_bootstrap_hands_capability_visibility_a_real_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: driving a real dogfood scenario through ``_build_live_runner`` (the
    production entry point ``interfaces/cli/commands/dogfood.py::run_run`` uses)
    constructs ``CapabilityVisibility`` with a non-None registry that has
    ``resolved_profile_for`` — witnessed at the actual construction call, not
    inferred from reading ``_session_factory``'s source."""
    monkeypatch.chdir(tmp_path)
    captured = _spy_on_capability_visibility_registry(monkeypatch)

    from reyn.interfaces.cli.commands.dogfood import _build_live_runner

    runner_fn = _build_live_runner("default")

    async def fake_llm(**kw):
        return _text_result("ack")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)
    result = await runner_fn(Scenario(id="s1", input="hello"))

    assert result.scenario_id == "s1"
    assert captured, (
        "CapabilityVisibility was never constructed during the scenario run — "
        "the witness observed nothing"
    )
    assert all(reg is not None for reg in captured), (
        "dogfood's bootstrap constructed at least one CapabilityVisibility with "
        "registry=None — the ordering is NOT fixed"
    )
    assert all(hasattr(reg, "resolved_profile_for") for reg in captured), (
        "the registry dogfood hands CapabilityVisibility must expose "
        "resolved_profile_for (the _EnvelopeSource contract)"
    )


def test_the_spy_would_have_caught_a_missing_registry(monkeypatch, tmp_path: Path) -> None:
    """Tier 2: negative control for the witness above — the same spy, pointed at a
    session deliberately constructed with NO registry back-reference, DOES capture
    None. Without this, a spy that captures nothing meaningful (e.g. a stale
    reference, or a wrapper that never actually runs) would pass the positive
    test vacuously."""
    monkeypatch.chdir(tmp_path)
    captured = _spy_on_capability_visibility_registry(monkeypatch)

    make_session(agent_name="registry-less", registry=None)

    assert captured == [None], (
        "the spy must observe the exact registry a Session hands "
        "CapabilityVisibility, including a None one — otherwise it cannot be "
        "trusted to have witnessed the real dogfood path above"
    )
