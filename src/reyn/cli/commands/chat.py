"""`reyn chat` — interactive chat with implicit skill invocation."""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

from ..common_args import add_common_args
from ..session import Session


def register(sub) -> None:
    p = sub.add_parser("chat", help="Start an interactive chat session")
    p.add_argument(
        "--chat-id", dest="chat_id", default=None,
        help="Resume an existing chat by id (default: new id)",
    )
    add_common_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.chat.session import ChatSession
    from reyn.chat.repl import run_repl
    from reyn.config import _find_project_root
    from reyn.permissions import PermissionResolver

    session_cfg = Session.from_args(args)
    model, _ = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    limits = session_cfg.limits_for(args)

    perm_config = getattr(session_cfg.config, "permissions", {}) or {}
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=_find_project_root(Path.cwd()),
        interactive=sys.stdin.isatty(),
    )

    mem_cfg = session_cfg.config.chat.memory
    chat = ChatSession(
        chat_id=args.chat_id,
        model=model,
        state_root=session_cfg.config.state_dir,
        resolver=session_cfg.resolver,
        permission_resolver=perm_resolver,
        limits=limits,
        mcp_servers=session_cfg.config.mcp,
        output_language=output_language,
        memory_enabled=mem_cfg.enabled,
        memory_turn_threshold=mem_cfg.turn_threshold,
        memory_time_threshold=mem_cfg.time_threshold,
        memory_recall_top_k=mem_cfg.recall_top_k,
    )
    chat.load_history()

    asyncio.run(run_repl(chat))
