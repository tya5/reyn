"""`reyn run-once [agent]` — one-shot, non-interactive agent invocation.

#187: drive the general agent (RouterLoop) on a SINGLE prompt read WHOLE from
stdin (not line-by-line), to completion (n tool_calls → 1 stop), print the final
reply, then exit. Conceptually distinct from interactive `reyn chat`: a one-shot
batch entry for programmatic / eval use (the SWE-bench runner pipes the whole
task as one message).

Reuses `reyn chat`'s scoped session construction (grant / exclude-tools /
env-backend / registry) by delegating to ``chat.run`` with ``once=True`` — the
construction path is shared, only the final drive differs (``send_to_agent_impl``
instead of the line-by-line REPL). This is why the line-fragmentation bug (the
REPL read stdin a line at a time) does not recur here, and why the scoped
capabilities are inherited rather than re-ported (a transport/delivery change,
not a construction change).
"""
from __future__ import annotations

import argparse

from reyn.interfaces.cli.env_backend import register_env_backend_args

from ..common_args import add_common_args
from . import chat as _chat


def register(sub) -> None:
    p = sub.add_parser(
        "run-once",
        help="Run the general agent once on a stdin prompt, print the reply, exit",
        description=(
            "One-shot, non-interactive agent invocation. Reads the WHOLE stdin as a "
            "single user message, drives the agent to completion, prints the final "
            "reply, exits. For programmatic / eval use (e.g. SWE-bench)."
        ),
    )
    p.add_argument(
        "agent_name", nargs="?", default=None,
        help="Agent to drive (default: 'default').",
    )
    # Shared scoped surface — identical flags/help to `reyn chat` / `reyn run`.
    register_env_backend_args(p)
    p.add_argument(
        "--grant-file-write", dest="grant_file_write", action="store_true",
        default=False,
        help=(
            "Grant file.read+file.write at the resolver layer (the non-interactive "
            "agent edits its working tree without a prompt; bounded by the sandbox "
            "write-paths ∩). Same as `reyn chat --grant-file-write`."
        ),
    )
    p.add_argument(
        "--exclude-tools", dest="exclude_tools", default=None, metavar="NAMES",
        help=(
            "Comma-separated tool names to hide from the agent's LLM-visible "
            "catalog (e.g. 'web__search,web__fetch'). Same as `reyn chat "
            "--exclude-tools`."
        ),
    )
    p.add_argument(
        "--max-iterations", dest="max_iterations", type=int, default=80, metavar="N",
        help=(
            "Per-message tool-call budget for the autonomous loop (default 80). "
            "Higher than interactive chat (5) so the agent can iterate "
            "explore→edit→verify to completion."
        ),
    )
    add_common_args(p)
    # One-shot is always the plain-console (non-TUI) path with the one-shot drive,
    # and STATELESS (fresh=True): it does NOT load the agent's persisted
    # conversation history. A one-shot has no prior conversation to continue, and
    # loading a persisted agent's history (e.g. 'default') would contaminate the
    # run with unrelated prior context (#187 session-state contamination: a stale
    # 'default' history made the agent recall the old skill + hallucinate a fix).
    p.set_defaults(func=run, once=True, cui=True, fresh=True)


def run(args: argparse.Namespace) -> None:
    """Delegate to ``chat.run`` with ``once=True`` (set via ``set_defaults``).

    The one-shot branch in ``chat.run`` (after the registry attach) reads the
    whole stdin and drives ``send_to_agent_impl`` instead of ``run_repl``."""
    _chat.run(args)
