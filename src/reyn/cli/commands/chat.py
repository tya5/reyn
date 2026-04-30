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


_INTERNAL_SKILLS = ("skill_router", "recall_memory", "write_memory")


def run(args: argparse.Namespace) -> None:
    from reyn.chat.session import ChatSession
    from reyn.chat.repl import run_repl
    from reyn.compiler import load_dsl_skill
    from reyn.config import _find_project_root
    from reyn.permissions import PermissionResolver
    from reyn.skill_paths import stdlib_root

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

    # Memory skills (recall_memory, write_memory) declare ~/.reyn/memory in their
    # phase frontmatter, which would trigger startup_guard prompts. Two cases:
    #
    # - global_enabled=true: the skills DO access ~/.reyn/memory at runtime, so we
    #   need real approval. Run startup_guard synchronously here, BEFORE the asyncio
    #   loop starts — otherwise the runtime would call startup_guard from inside an
    #   async coroutine, blocking the event loop with input() while prompt_toolkit
    #   holds stdin. Approvals are persisted to .reyn/approvals.yaml.
    # - global_enabled=false: the skills declare the path but won't access it
    #   (we exclude global from scope_dirs at the ChatSession layer). Mark the
    #   declared paths as session-approved so the runtime's startup_guard finds
    #   them already covered and doesn't prompt.
    if mem_cfg.enabled:
        gm_path = str(Path("~/.reyn/memory").expanduser())
        if mem_cfg.global_enabled:
            sl = stdlib_root()
            for skill_name in _INTERNAL_SKILLS:
                skill_md = sl / "skills" / skill_name / "skill.md"
                if not skill_md.exists():
                    continue
                skill = load_dsl_skill(str(skill_md), dsl_root=str(sl))
                perm_resolver.startup_guard(skill, skill.name)
        else:
            perm_resolver.session_approve_path(
                gm_path, "recall_memory", "file.read", recursive=True,
            )
            perm_resolver.session_approve_path(
                gm_path, "write_memory", "file.read", recursive=True,
            )
            perm_resolver.session_approve_path(
                gm_path, "write_memory", "file.write", recursive=False,
            )

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
        memory_global_enabled=mem_cfg.global_enabled,
        memory_turn_threshold=mem_cfg.turn_threshold,
        memory_time_threshold=mem_cfg.time_threshold,
        memory_recall_top_k=mem_cfg.recall_top_k,
    )
    chat.load_history()

    asyncio.run(run_repl(chat))
