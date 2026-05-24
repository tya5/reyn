"""Chainlit entry point for reyn — loaded by ``python -m chainlit run``.

The `reyn chainlit` CLI subcommand shells out to chainlit with this
file as the target. ``@cl.on_chat_start`` builds (or reuses) an
``AgentRegistry``, attaches the default agent, and starts a background
task that drains ``registry.repl_outbox`` → ``cl.Message.send()``.
``@cl.on_message`` forwards user input via ``submit_user_text`` so the
existing ``session.run()`` loop picks it up.

PoC scope (= explicitly out of scope for this PR, follow-ons):
- multi-user isolation: today all browser sessions share one process
  ``AgentRegistry`` and compete for the single ``attached`` slot; true
  per-cl-session sandboxing needs a registry-per-session refactor or
  an N-attached multi-foreground extension.
- intervention UI: ``kind="intervention"`` is rendered as plain text;
  no ``cl.AskUserMessage`` round-trip wiring yet.
- streaming: ``__stream_*__`` incremental frames are dropped; only the
  final ``agent`` kind reaches the browser.
- skill selection / per-agent attach / cost panel: none of the right-panel
  TUI affordances exist; only the central chat thread.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import chainlit as cl

from reyn.chainlit_app.adapter import outbox_to_chainlit
from reyn.chainlit_app.profiles import list_agent_profiles
from reyn.chainlit_app.uploads import collect_image_blocks

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry

_DRAIN_KEY = "reyn_drain_task"
_REGISTRY_LOCK = asyncio.Lock()
_REGISTRY: "AgentRegistry | None" = None


def _agent_name_from_env() -> str:
    return os.environ.get("REYN_CHAINLIT_AGENT", "default")


async def _get_or_build_registry() -> "AgentRegistry":
    """Process-singleton registry, built lazily on first chat start.

    Mirrors the construction order in ``cli/commands/chat.py``:
    BudgetTracker → hydrate → PermissionResolver → AgentRegistry.

    The web (``reyn.web.deps``) and chainlit gateways intentionally do
    not share their bootstrap helper today: web/deps imports fastapi at
    module level, so chainlit can't reach it without the ``[web]``
    extra. A follow-on can extract a shared ``reyn.chat.bootstrap``
    when there is a third surface (= when the duplication actually
    costs something).
    """
    global _REGISTRY
    async with _REGISTRY_LOCK:
        if _REGISTRY is not None:
            return _REGISTRY

        import argparse

        from reyn.budget.budget import BudgetTracker
        from reyn.chat.profile import AgentProfile
        from reyn.chat.registry import AgentRegistry
        from reyn.chat.session import ChatSession
        from reyn.cli.session import Session
        from reyn.config import _find_project_root, load_project_context
        from reyn.events.state_log import StateLog
        from reyn.permissions.permissions import PermissionResolver

        # Reuse the same yaml-loading path the CLI uses so reyn.yaml /
        # reyn.local.yaml / env overrides Just Work. The *_for(args)
        # helpers do ``getattr(args, "...", None)`` everywhere, so an
        # empty Namespace gives us config defaults across the board.
        empty_args = argparse.Namespace()
        session_cfg = Session.from_args(empty_args)
        model, _resolved = session_cfg.model_for(empty_args)
        output_language = session_cfg.output_language_for(empty_args)
        safety = session_cfg.safety_for(empty_args)

        project_root = _find_project_root(Path.cwd()) or Path.cwd()
        state_log = StateLog(project_root / ".reyn" / "state" / "wal.jsonl")
        budget_tracker = BudgetTracker(session_cfg.config.cost, safety=safety)
        budget_tracker.hydrate(
            project_root / ".reyn" / "state" / "budget_ledger.jsonl"
        )
        budget_state_path = (
            project_root / ".reyn" / "state" / "budget_state.json"
        )
        budget_tracker.load_state(budget_state_path)
        budget_tracker.set_state_path(budget_state_path)

        perm_config = getattr(session_cfg.config, "permissions", {}) or {}
        perm_resolver = PermissionResolver(
            config_permissions=perm_config,
            project_root=project_root,
            interactive=False,
            unsafe_python_allowed=False,
        )

        project_context = load_project_context(session_cfg.config, project_root)

        registry_ref: list = []

        def _session_factory(profile: AgentProfile) -> ChatSession:
            s = ChatSession(
                agent_name=profile.name,
                model=model,
                resolver=session_cfg.resolver,
                permission_resolver=perm_resolver,
                safety=safety,
                mcp_servers=session_cfg.config.mcp,
                output_language=output_language,
                prompt_cache_enabled=session_cfg.config.prompt_cache_enabled,
                project_context=project_context,
                agent_role=profile.role,
                compaction_config=session_cfg.config.chat.compaction,
                registry=registry_ref[0],
                allowed_skills=profile.allowed_skills,
                allowed_mcp=profile.allowed_mcp,
                events_config=session_cfg.config.events,
                state_log=state_log,
                budget_tracker=budget_tracker,
                sandbox_config=session_cfg.config.sandbox,
                multimodal_config=session_cfg.config.multimodal,
                action_retrieval_config=session_cfg.config.action_retrieval,
                embedding_config=session_cfg.config.embedding,
                agent_id=session_cfg.config.agent.id,
            )
            s.load_history()
            return s

        registry = AgentRegistry(
            project_root=project_root,
            session_factory=_session_factory,
            state_log=state_log,
        )
        registry_ref.append(registry)
        _REGISTRY = registry
        return registry


async def _drain_loop(registry: "AgentRegistry") -> None:
    """Pump ``registry.repl_outbox`` to the Chainlit browser session.

    Runs as a per-cl-session background task. Terminates on ``__end__``
    sentinel (= session shutdown) or task cancellation.
    """
    while True:
        msg = await registry.repl_outbox.get()
        payload = outbox_to_chainlit(msg)
        if payload is None:
            continue
        if payload.role == "end":
            return
        if payload.role == "error":
            await cl.ErrorMessage(content=payload.content).send()
        else:
            await cl.Message(
                content=payload.content,
                author=payload.author,
            ).send()


@cl.set_chat_profiles
async def _chat_profiles() -> list[cl.ChatProfile]:
    """Expose every agent on disk as a chainlit chat profile picker entry.

    The browser's chat-profile dropdown lets the operator choose which
    reyn agent to attach for the current chat session. With multiple
    agents declared on disk, the picker appears at the top of the
    welcome screen; with only one (= `default`) chainlit hides it.

    Selection is stored on ``cl.user_session["chat_profile"]`` and read
    in ``_on_chat_start`` to drive ``registry.attach(<picked>)``.
    """
    registry = await _get_or_build_registry()
    return [
        cl.ChatProfile(**dct.as_kwargs())
        for dct in list_agent_profiles(registry)
    ]


@cl.set_starters
async def _starters() -> list[cl.Starter]:
    """Welcome-screen quick prompts (= reyn-flavored examples).

    Visible only on the very first message; replaced by the live chat
    once the user sends anything. Keep these grounded in capabilities
    that work today (= no streaming / IV round-trip dependency).
    """
    return [
        cl.Starter(
            label="自分の reyn agent を一覧する",
            message="このプロジェクトに設定されている agent を一覧してください。",
        ),
        cl.Starter(
            label="reyn の Skill を 1 つ動かしてみる",
            message=(
                "今このプロジェクトで使える stdlib skill を 1 つ選んで、"
                "短い試走をしてください。 何が起きるか教えてください。"
            ),
        ),
        cl.Starter(
            label="MEMORY.md を読んで自己紹介",
            message=(
                "プロジェクト root の MEMORY.md と CLAUDE.md を読んで、"
                "あなた (= attach されている agent) の役割を 3 行で説明してください。"
            ),
        ),
        cl.Starter(
            label="今の cost と events を要約",
            message=(
                "直近の events log と今セッションの token / cost を"
                "簡潔に教えてください。"
            ),
        ),
    ]


def _picked_agent_name(default: str) -> str:
    """Resolve which agent to attach for this cl session.

    Priority: chat-profile picker selection > REYN_CHAINLIT_AGENT env > ``default``.
    Picker selection lands on ``cl.user_session["chat_profile"]`` when
    the operator picks one — chainlit only shows the picker when
    ``set_chat_profiles`` returns >= 2 entries, so the env fallback
    covers the single-agent case where no picker is rendered.
    """
    picked = cl.user_session.get("chat_profile")
    if isinstance(picked, str) and picked.strip():
        return picked.strip()
    return default


@cl.on_chat_start
async def _on_chat_start() -> None:
    registry = await _get_or_build_registry()
    name = _picked_agent_name(_agent_name_from_env())
    if not registry.exists(name):
        await cl.ErrorMessage(
            content=(
                f"Agent {name!r} not found. Create it with "
                f"`reyn agent new {name}` and reload."
            )
        ).send()
        return

    await registry.restore_all()
    await registry.attach(name)
    task = asyncio.create_task(_drain_loop(registry))
    cl.user_session.set(_DRAIN_KEY, task)
    await cl.Message(
        content=f"Connected to agent **{name}**.",
        author="system",
    ).send()


@cl.on_message
async def _on_message(message: cl.Message) -> None:
    registry = await _get_or_build_registry()
    session = registry.attached_session()
    if session is None:
        await cl.ErrorMessage(
            content="No agent attached. Reload the page to reconnect.",
        ).send()
        return

    # Multimodal upload bridge: any image element dropped via the
    # chainlit attachment button rides the same ``_pending_user_images``
    # queue that ``/image PATH`` uses. The queue is drained on the
    # next user turn by ``ChatSession._handle_user_message``.
    elements = getattr(message, "elements", None) or []
    if elements:
        blocks = collect_image_blocks(elements)
        queue = getattr(session, "_pending_user_images", None)
        if queue is not None and blocks:
            queue.extend(blocks)

    await session.submit_user_text(message.content)


@cl.on_chat_end
async def _on_chat_end() -> None:
    task = cl.user_session.get(_DRAIN_KEY)
    if task is not None:
        task.cancel()
