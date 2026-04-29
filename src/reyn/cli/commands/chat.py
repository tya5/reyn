"""`reyn chat` — interactive chat with implicit skill invocation."""
from __future__ import annotations
import argparse
import asyncio

from ..session import Session


def register(sub) -> None:
    p = sub.add_parser("chat", help="Start an interactive chat session")
    p.add_argument(
        "--chat-id", dest="chat_id", default=None,
        help="Resume an existing chat by id (default: new id)",
    )
    p.add_argument("--model", default=None,
                   help="Model class or LiteLLM string (default: from reyn.yaml)")
    p.add_argument(
        "--output-language", default=None, dest="output_language",
        help="Output language code (default: from reyn.yaml or ja)",
    )
    p.add_argument(
        "--max-phase-visits", dest="max_phase_visits", type=int, default=None,
        help="Cap per-phase visits inside spawned skill runs (default: from reyn.yaml or 25)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.chat.session import ChatSession
    from reyn.chat.repl import run_repl

    session_cfg = Session.from_args(args)
    model, _ = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    max_phase_visits = session_cfg.max_phase_visits_for(args)

    chat = ChatSession(
        chat_id=args.chat_id,
        model=model,
        state_root=session_cfg.config.state_dir,
        resolver=session_cfg.resolver,
        max_phase_visits=max_phase_visits,
        mcp_servers=session_cfg.config.mcp,
        output_language=output_language,
    )
    chat.load_history()

    asyncio.run(run_repl(chat))
