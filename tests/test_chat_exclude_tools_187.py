"""Tier 2: #187 — `reyn chat --exclude-tools` hides tools from the MAIN agent loop.

#187 solves SWE with the general agent (`reyn chat` / RouterLoop). The agent has
web__search/web__fetch and would (and did, in the smoke) web-search the gold PR =
a leak of the benchmark answer. The faithful SWE-eval must exclude web so the
agent solves from the issue + repo only.

Mechanism: RouterLoop already filters its LLM-visible catalog by `exclude_tools`
(router_loop.py:1791-1796); the sub-loops use `exclude_tools={"plan"}`
(planner.py:1136). #187 exposes this via `reyn chat --exclude-tools <names>`,
threaded ChatSession → the MAIN agent loop (session.py).

The load-bearing constraint (lead-coder): the web-exclusion must be the real
catalog-filter behavior, not a source-string check. This file pins:
  (a) the catalog filter actually drops web tools (behavioral — the function
      that builds the RouterLoop's LLM-visible + dispatch catalog at
      router_loop.py:~1791);
  (b) `reyn chat` exposes `--exclude-tools`;
  (c) the faithful SWE runner excludes web__search/web__fetch in the chat invocation.
(The MAIN-loop reach — session.py passing `exclude_tools=self._exclude_tools` to
the main RouterLoop — is lead-reviewed code + dogfood-netted; the filter behavior
is what a refactor could silently break, so that is unit-pinned here.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _tool(name: str) -> dict:
    """An OpenAI-style tool catalog entry, as RouterLoop builds them."""
    return {"type": "function", "function": {"name": name, "description": ""}}


def test_catalog_filter_hides_web_keeps_others() -> None:
    """Tier 2: the catalog filter drops excluded (web) tools, keeps the rest.

    `_apply_tool_exclusions` is the exact post-build filter that produces the
    RouterLoop's LLM-visible catalog (`self._catalog`, router_loop.py:~1791).
    Exercising it directly proves the web-exclusion *behavior* (refactor-robust,
    no source-string): with web excluded, the catalog the LLM sees no longer
    contains web__search/web__fetch but still offers the repo-editing tools.
    """
    from reyn.chat.router_loop import _apply_tool_exclusions

    catalog = [
        _tool("web__search"),
        _tool("web__fetch"),
        _tool("file__read"),
        _tool("file__write"),
        _tool("exec__sandboxed_exec"),
    ]
    filtered = _apply_tool_exclusions(catalog, frozenset({"web__search", "web__fetch"}))
    names = {t["function"]["name"] for t in filtered}
    assert "web__search" not in names and "web__fetch" not in names, (
        "the faithful SWE catalog must hide web tools so the agent cannot "
        "web-look-up the gold solution"
    )
    # the repo-editing tools the agent actually needs survive the exclusion
    assert {"file__read", "file__write", "exec__sandboxed_exec"} <= names
    # empty exclusion = no filtering (the default, non-faithful path)
    assert len(_apply_tool_exclusions(catalog, frozenset())) == len(catalog)


def test_chat_parser_exposes_exclude_tools_flag() -> None:
    """Tier 2: `reyn chat` registers --exclude-tools (dest=exclude_tools)."""
    from reyn.cli.commands.chat import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    ns = parser.parse_args(["chat", "--exclude-tools", "web__search,web__fetch"])
    assert ns.exclude_tools == "web__search,web__fetch"
    assert parser.parse_args(["chat"]).exclude_tools is None


def test_swe_runner_excludes_web_tools_in_chat_path() -> None:
    """Tier 2: the faithful SWE chat-path invocation excludes web tools.

    The general agent must solve from the issue + repo, not a web lookup of the
    gold PR. The exec network path is already sandbox-gated off; web__search /
    web__fetch are the only internet→gold surface, so the runner excludes them.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    src = (
        Path(__file__).resolve().parent.parent / "scripts" / "swe_bench_runner.py"
    ).read_text(encoding="utf-8")
    assert '"--exclude-tools", "web__search,web__fetch"' in src, (
        "run_reyn_once_in_container must pass --exclude-tools web__search,web__fetch "
        "to reyn run-once so the agent cannot web-look-up the gold solution (faithful eval)."
    )
