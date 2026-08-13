"""Tier 2: #187 — `reyn chat --exclude-tools` hides tools from the MAIN agent loop.

#187 solves SWE with the general agent (`reyn chat` / RouterLoop). The agent has
web_search/web_fetch and would (and did, in the smoke) web-search the gold PR =
a leak of the benchmark answer. The faithful SWE-eval must exclude web so the
agent solves from the issue + repo only.

Mechanism: RouterLoop already filters its LLM-visible catalog by `exclude_tools`
(router_loop.py:1791-1796); the sub-loops use `exclude_tools={"plan"}`
(planner.py:1136). #187 exposes this via `reyn chat --exclude-tools <names>`,
threaded Session → the MAIN agent loop (session.py).

The load-bearing constraint (lead-coder): the web-exclusion must be the real
catalog-filter behavior, not a source-string check. This file pins:
  (a) the catalog filter actually drops web tools (behavioral — the function
      that builds the RouterLoop's LLM-visible + dispatch catalog at
      router_loop.py:~1791);
  (b) `reyn chat` exposes `--exclude-tools`;
  (c) the faithful SWE runner excludes web_search/web_fetch in the chat invocation.
(The MAIN-loop reach — session.py passing `exclude_tools=self._exclude_tools` to
the main RouterLoop — is lead-reviewed code + dogfood-netted; the filter behavior
is what a refactor could silently break, so that is unit-pinned here.)
"""
from __future__ import annotations

import argparse
import sys

from tests._support.paths import REPO_ROOT


def _tool(name: str) -> dict:
    """An OpenAI-style tool catalog entry, as RouterLoop builds them."""
    return {"type": "function", "function": {"name": name, "description": ""}}


def test_catalog_filter_hides_web_keeps_others() -> None:
    """Tier 2: the catalog filter drops excluded (web) tools, keeps the rest.

    `apply_contextual_visibility` is the exact post-build filter that produces the
    RouterLoop's LLM-visible catalog (`self._catalog`). #3378 re-keyed it from the
    raw `exclude_tools` name set onto the session's EFFECTIVE contextual narrowing —
    which `exclude_tools` composes into (`RouterLoop._with_exclude_tools`), so the
    #187 exclusion is expressed here as the ContextualPermission it becomes.
    Exercising it directly proves the web-exclusion *behavior* (refactor-robust,
    no source-string): with web excluded, the catalog the LLM sees no longer
    contains web_search/web_fetch but still offers the repo-editing tools.
    """
    from reyn.runtime.router_loop import apply_contextual_visibility
    from reyn.security.permissions.effective import ContextualPermission

    catalog = [
        _tool("web_search"),
        _tool("web_fetch"),
        _tool("read_file"),
        _tool("write_file"),
        _tool("exec"),
    ]
    excluded = ContextualPermission(tool_deny=frozenset({"web_search", "web_fetch"}))
    filtered = apply_contextual_visibility(catalog, excluded)
    names = {t["function"]["name"] for t in filtered}
    assert "web_search" not in names and "web_fetch" not in names, (
        "the faithful SWE catalog must hide web tools so the agent cannot "
        "web-look-up the gold solution"
    )
    # the repo-editing tools the agent actually needs survive the exclusion
    assert {"read_file", "write_file", "exec"} <= names
    # no narrowing at all = no filtering (the default, non-faithful path)
    unfiltered = apply_contextual_visibility(catalog, None)
    assert {t["function"]["name"] for t in unfiltered} == {
        t["function"]["name"] for t in catalog
    }


def test_chat_parser_exposes_exclude_tools_flag() -> None:
    """Tier 2: `reyn chat` registers --exclude-tools (dest=exclude_tools)."""
    from reyn.interfaces.cli.commands.chat import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register(sub)
    ns = parser.parse_args(["chat", "--exclude-tools", "web_search,web_fetch"])
    assert ns.exclude_tools == "web_search,web_fetch"
    assert parser.parse_args(["chat"]).exclude_tools is None


def test_swe_runner_excludes_web_tools_in_chat_path() -> None:
    """Tier 2: the faithful SWE chat-path invocation excludes web tools.

    The general agent must solve from the issue + repo, not a web lookup of the
    gold PR. The exec network path is already sandbox-gated off; web_search /
    web_fetch are the only internet→gold surface, so the runner excludes them.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    src = (
        REPO_ROOT / "scripts" / "swe_bench_runner.py"
    ).read_text(encoding="utf-8")
    assert '"--exclude-tools", "web_search,web_fetch"' in src, (
        "run_reyn_once_in_container must pass --exclude-tools web_search,web_fetch "
        "to reyn run-once so the agent cannot web-look-up the gold solution (faithful eval)."
    )
