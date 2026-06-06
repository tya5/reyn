"""Tier 2: #1406 — exclude_tools is enforced at EXECUTION, not just the catalog.

The #187 N=3 verdict found `web__search` executing despite #1400's catalog filter:
the LLM called it by name, the #229 salvage rewrote it to
`invoke_action(action_name=web__search)`, and universal_dispatch resolved + executed
it (the catalog gate checks the `invoke_action` wrapper, not the inner action). This
pins the execution-level block on the REAL dispatch path (`RouterLoop._execute_tool`),
which the catalog-advertisement unit (#1400) could not catch — covering all three
bypass shapes and asserting the excluded tool's handler is never invoked.
"""
from __future__ import annotations

import asyncio
import json

from reyn.chat.router_loop import RouterLoop


class _Events:
    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    def emit(self, *a, **k) -> None:  # dispatch_tool emits tool_called/failed
        self.emitted.append((a, k))


class _MiniHost:
    """Minimal real host for driving _execute_tool (no mock). The excluded path
    returns before dispatch; the non-excluded path reaches dispatch_tool, which
    needs only events + agent_name."""

    agent_name = "t"

    def __init__(self) -> None:
        self.events = _Events()
        self.web_search_calls: list[dict] = []

    async def web_search(self, **kw) -> dict:  # would run IF the tool executed
        self.web_search_calls.append(kw)
        return {"kind": "web_search", "results": ["LEAKED GOLD"]}


def _exec(loop: RouterLoop, name: str, args: dict) -> dict:
    return asyncio.run(
        loop._execute_tool({"function": {"name": name, "arguments": json.dumps(args)}})
    )


def test_excluded_tool_blocked_at_execution_all_paths() -> None:
    """Tier 2: an excluded tool is rejected on the real dispatch path via every
    call shape, and its handler never runs."""
    host = _MiniHost()
    loop = RouterLoop(
        host=host, chain_id="t", max_iterations=5,
        exclude_tools={"web__search", "web__fetch"},
    )

    # (a) native direct call by name; (b) is the salvaged form of (a) — same path;
    # (c) direct invoke_action(action_name=<excluded>).
    r_native = _exec(loop, "web__search", {"query": "site:github.com gold patch?"})
    r_wrapped = _exec(
        loop, "invoke_action",
        {"action_name": "web__search", "args": {"query": "gold?"}},
    )
    for r in (r_native, r_wrapped):
        assert r["status"] == "error"
        assert r["error"]["kind"] == "tool_excluded", r
        # decision-enabling message names the tool + says don't call it
        assert "web__search" in r["error"]["message"]

    # the excluded tool's handler must NEVER have executed (no gold leak)
    assert host.web_search_calls == [], "excluded tool executed despite the block"


def test_non_excluded_tool_not_blocked_by_exclude_logic() -> None:
    """Tier 2: the block is targeted — a non-excluded tool is not rejected as
    `tool_excluded` (it falls through to normal dispatch; here unknown_tool since
    the test catalog is empty, which is NOT the exclude path)."""
    host = _MiniHost()
    loop = RouterLoop(
        host=host, chain_id="t", max_iterations=5, exclude_tools={"web__search"},
    )
    r = _exec(loop, "file__read", {"path": "/testbed/x.py"})
    excluded = r.get("status") == "error" and r.get("error", {}).get("kind") == "tool_excluded"
    assert not excluded, "a non-excluded tool must not be rejected by the exclude block"


def test_no_exclusion_set_is_inert() -> None:
    """Tier 2: with no exclude_tools, the execution-block adds nothing (no false
    rejection) — interactive sessions are unaffected."""
    host = _MiniHost()
    loop = RouterLoop(host=host, chain_id="t", max_iterations=5)  # exclude_tools default
    r = _exec(loop, "invoke_action", {"action_name": "web__search", "args": {}})
    excluded = r.get("status") == "error" and r.get("error", {}).get("kind") == "tool_excluded"
    assert not excluded
