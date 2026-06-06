"""Tier 2: #187 — `reyn chat --exclude-tools` hides tools from the MAIN agent loop.

#187 solves SWE with the general agent (`reyn chat` / RouterLoop). The agent has
web__search/web__fetch and would (and did, in the smoke) web-search the gold PR =
a leak of the benchmark answer. The faithful SWE-eval must exclude web so the
agent solves from the issue + repo only.

Mechanism: RouterLoop already filters its LLM-visible catalog by `exclude_tools`
(router_loop.py:1791-1796); the sub-loops use `exclude_tools={"plan"}`
(planner.py:1136). #187 exposes this via `reyn chat --exclude-tools <names>`,
threaded ChatSession → the MAIN agent loop (session.py).

The load-bearing requirement (lead-coder): the exclusion reaches the **MAIN** chat
loop (not just sub-loops). This file pins the threading on the public surface:
  (a) the MAIN RouterLoop construction passes `exclude_tools=self._exclude_tools`
      (the reach — distinct from the sub-loops' `{"plan"}`);
  (b) `reyn chat` exposes `--exclude-tools`;
  (c) the faithful SWE runner excludes web__search/web__fetch in the chat invocation.
(ChatSession threading the param into the main loop is exercised end-to-end by the
faithful dogfood; we do not assert on the private `_exclude_tools` field here.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SESSION_PY = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "chat" / "session.py"
)


def test_main_chat_loop_threads_exclude_tools() -> None:
    """Tier 2: the MAIN chat RouterLoop is constructed with exclude_tools=self._exclude_tools.

    The reach lead-coder required: not just the sub-loops (planner.py uses
    `exclude_tools={"plan"}`), but the MAIN agent loop. The main loop is the
    RouterLoop built with `host=self._router_host` / `max_iterations=5` in
    session.py; assert that construction threads the session's exclude_tools.
    """
    src = _SESSION_PY.read_text(encoding="utf-8")
    # The main-loop construction must pass the session's exclude_tools through.
    assert "exclude_tools=self._exclude_tools" in src, (
        "the MAIN chat RouterLoop must be constructed with "
        "exclude_tools=self._exclude_tools so the exclusion reaches the main "
        "agent loop's LLM-visible catalog (not only sub-loops)."
    )


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
        "run_reyn_chat_in_container must pass --exclude-tools web__search,web__fetch "
        "to reyn chat so the agent cannot web-look-up the gold solution (faithful eval)."
    )
