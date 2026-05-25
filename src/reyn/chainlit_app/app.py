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
from reyn.chainlit_app.history import DEFAULT_REPLAY_CAP, history_to_chainlit
from reyn.chainlit_app.intervention import build_intervention_prompt
from reyn.chainlit_app.profiles import list_agent_profiles
from reyn.chainlit_app.settings import (
    AGENT_ROLE_SETTING_ID,
    LANGUAGE_ITEMS,
    LANGUAGE_SETTING_ID,
    MODEL_SETTING_ID,
    language_label_for,
    language_to_value,
    list_model_names,
    normalise_role,
    value_to_language,
    value_to_model,
)
from reyn.chainlit_app.slash_route import (
    QUICK_ACTIONS,
    QuickAction,
    action_name_for,
    is_chainlit_history_wipe,
    is_slash,
)
from reyn.chainlit_app.tool_step import build_tool_step_update

_TOOL_CALL_KINDS = frozenset({
    "tool_call_started",
    "tool_call_completed",
    "tool_call_failed",
})
_TOOL_STEP_SESSION_PREFIX = "reyn_tool_step_"
from reyn.chainlit_app.uploads import collect_image_blocks

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry

_DRAIN_KEY = "reyn_drain_task"
_REGISTRY_LOCK = asyncio.Lock()
_REGISTRY: "AgentRegistry | None" = None


def _agent_name_from_env() -> str:
    return os.environ.get("REYN_CHAINLIT_AGENT", "default")


def _history_cap_from_env() -> int | None:
    """Read the replay cap from ``REYN_CHAINLIT_HISTORY_CAP``.

    Returns:
      - ``DEFAULT_REPLAY_CAP`` when the env var is unset or unparseable
        (= keep the chat snappy on agents with long history.jsonl).
      - The parsed int when the env var holds a number. ``0`` or
        negative becomes "unlimited" by passing ``None`` to the
        helper, so the operator can opt back into the previous
        full-replay behavior with ``REYN_CHAINLIT_HISTORY_CAP=0``.
    """
    raw = os.environ.get("REYN_CHAINLIT_HISTORY_CAP")
    if raw is None:
        return DEFAULT_REPLAY_CAP
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_REPLAY_CAP
    if value <= 0:
        return None
    return value


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
        # ``interactive=True``: route permission prompts through the
        # intervention bus so they reach the chainlit surface as
        # ``kind="intervention"`` outbox messages. PR #907 wires
        # ``_handle_intervention`` in the drain loop to render those
        # via ``cl.AskActionMessage`` and post the user's choice back
        # via ``session.answer_pending_intervention``. With
        # ``interactive=False`` (the prior value), ``_prompt`` is
        # short-circuited at ``permissions.py:499`` and every gated
        # action auto-denies — operator sees a silent "permission
        # denied" without the chance to allow.
        perm_resolver = PermissionResolver(
            config_permissions=perm_config,
            project_root=project_root,
            interactive=True,
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
        if msg.kind == "intervention":
            # Intercept before the adapter — the adapter only knows how
            # to render IVs as plain author="intervention" text, but
            # `kind="intervention"` is the agent **blocking on user
            # input**. Hand it off to the round-trip helper so the
            # operator sees a real prompt + buttons (= AskActionMessage)
            # or input box (= AskUserMessage) and the answer flows back
            # to the awaiting skill via `answer_pending_intervention`.
            await _handle_intervention(registry, msg)
            continue

        if msg.kind in _TOOL_CALL_KINDS:
            # Render the tool invocation as a collapsible ``cl.Step``
            # (= operator can expand to see args / result / error).
            # The step lifecycle spans three messages
            # (started / completed / failed), keyed by ``op_id`` so
            # the right step is updated. Falls through to the adapter
            # path on any failure so the row at least renders as a
            # plain "🔧 tool" message.
            try:
                handled = await _handle_tool_call(msg)
            except Exception:
                handled = False
            if handled:
                continue

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
                type=payload.message_type,
            ).send()


async def _handle_tool_call(msg) -> bool:
    """Render a ``tool_call_*`` outbox message as a collapsible
    ``cl.Step`` so the operator can expand the row to see args /
    result / error inline with the rest of the chat thread.

    The lifecycle spans three events keyed by ``op_id`` (= the
    deterministic ``args_hash`` the lifecycle forwarder packs into
    ``meta``):

      - ``tool_call_started``    → create the step + show args
      - ``tool_call_completed``  → update the same step with result
      - ``tool_call_failed``     → update the same step with error

    Returns True when the row was rendered as a step (= adapter
    bypass), False when meta is malformed / chainlit raises so the
    caller falls back to the adapter's plain-text branch. Each
    interaction with chainlit is wrapped in try/except so a transient
    glitch never breaks the drain loop.
    """
    update = build_tool_step_update(msg.meta, msg.kind)
    if update is None:
        return False

    session_key = _TOOL_STEP_SESSION_PREFIX + update.op_id

    try:
        if update.phase == "started":
            step = cl.Step(
                name=update.tool_name,
                type="tool",
                show_input="json",
                default_open=False,
                auto_collapse=True,
            )
            if update.input_text:
                step.input = update.input_text
            await step.send()
            cl.user_session.set(session_key, step)
            return True

        # completed / failed — look up the started step.
        step = cl.user_session.get(session_key)
        if step is None:
            # Step pairing was lost (= drain restart / shadow event) →
            # fall back to adapter's plain-text branch so the row at
            # least shows the completion / failure.
            return False
        step.output = update.output_text
        if update.is_error:
            # Surface the failure visually on the step. ``is_error``
            # may not be on every chainlit version, so wrap setattr
            # in try/except.
            try:
                step.is_error = True
            except Exception:
                pass
        await step.update()
        cl.user_session.set(session_key, None)
        return True
    except Exception:
        return False


async def _handle_intervention(registry: "AgentRegistry", msg) -> None:
    """Render an IV outbox payload as a chainlit Ask* prompt + reply back.

    Both branches are wrapped in defensive try/except so chainlit
    version drift or an exception inside the Ask* round-trip leaves
    the agent's await intact (= reyn-side timeout / fallback still
    fires) and the drain loop keeps pumping subsequent messages.
    """
    prompt = build_intervention_prompt(msg.meta, text=msg.text or "")
    session = registry.attached_session()
    if session is None or prompt.intervention_id is None:
        # Can't dispatch the answer (= no attached session or malformed
        # meta lacking intervention_id) — fall back to plain-text render
        # so the operator at least sees what was asked.
        await cl.Message(
            content=prompt.content, author="intervention",
        ).send()
        return

    # Look up the live UserIntervention by id. ``_interventions.list_active``
    # is the public-shape (= used by slash commands), and the iv carries
    # its own future + choices, so we route through
    # ``session._deliver_answer_to(iv, text, choice_id_override=...)``
    # which works regardless of whether ``iv.run_id`` is set (permission
    # gate IVs from ``_prompt`` have no run_id; ``ask_user`` IVs do).
    def _find_iv():
        try:
            for iv in session._interventions.list_active():
                if getattr(iv, "id", None) == prompt.intervention_id:
                    return iv
        except AttributeError:
            return None
        return None

    iv_obj = _find_iv()
    if iv_obj is None:
        # IV already answered or vanished between announce and our
        # handler picking it up — render plain text, nothing to answer.
        await cl.Message(
            content=prompt.content, author="intervention",
        ).send()
        return

    # Empty-answer fallback used on every "no response" path below.
    # Without this, the agent stays awaiting ``iv.future`` forever and
    # ChatSession.run_one_iteration never picks up the next inbox
    # message — the entire chat appears frozen.
    async def _resolve_empty() -> None:
        try:
            await session._deliver_answer_to(iv_obj, "")
        except Exception:
            pass

    if prompt.is_choice:
        try:
            actions = [
                cl.Action(
                    name=f"iv_{spec.choice_id}",
                    label=spec.label,
                    payload={
                        "intervention_id": prompt.intervention_id,
                        "choice_id": spec.choice_id,
                    },
                )
                for spec in prompt.choices
            ]
            response = await cl.AskActionMessage(
                content=prompt.content,
                actions=actions,
                timeout=600,
            ).send()
        except Exception:
            response = None
        if response is None:
            await _resolve_empty()
            return
        # ``cl.AskActionMessage`` returns an ``AskActionResponse``
        # TypedDict with shape ``{name, payload, label, tooltip,
        # forId, id}``. The dict ``payload`` field is what carries
        # the per-Action dict we passed at construction time — that's
        # where ``choice_id`` actually lives. A prior version read
        # ``getattr(response, "payload", None) or response`` which
        # falls through to the OUTER dict and ends up reading
        # ``choice_id`` from the wrong layer → always None → reyn-side
        # validation classifies as "unknown choice".
        outer: dict = response if isinstance(response, dict) else {}
        nested_payload = outer.get("payload")
        if not isinstance(nested_payload, dict):
            nested_payload = {}
        choice_id = nested_payload.get("choice_id")
        label = (
            nested_payload.get("label")
            or outer.get("label")
            or choice_id
            or ""
        )
        if not isinstance(choice_id, str):
            await _resolve_empty()
            return
        try:
            await session._deliver_answer_to(
                iv_obj, str(label), choice_id_override=choice_id,
            )
        except Exception:
            await _resolve_empty()
        return

    # Free-text branch.
    try:
        reply = await cl.AskUserMessage(
            content=prompt.content,
            timeout=600,
        ).send()
    except Exception:
        reply = None
    if reply is None:
        await _resolve_empty()
        return
    # AskUserMessage returns a StepDict-like with ``output`` (or
    # ``content`` on older chainlit). Pull whichever is present.
    text = ""
    if isinstance(reply, dict):
        text = str(
            reply.get("output") or reply.get("content") or ""
        )
    else:
        text = str(getattr(reply, "output", None) or getattr(reply, "content", "") or "")
    try:
        await session._deliver_answer_to(iv_obj, text)
    except Exception:
        await _resolve_empty()


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
    session = await registry.attach(name)

    # Register the chainlit surface as the canonical chat-channel
    # intervention listener. The id MUST match ``DEFAULT_CHAT_CHANNEL_ID``
    # (= "tui") because ``ChatInterventionBus`` stamps every iv with
    # that channel id (session.py:1661 / 1891 / 4388), and the agent
    # layer's origin-pin routing then expects a listener registered
    # under the SAME id. Registering as "chainlit" instead leaves the
    # iv as ``route="user_channel_stalled"`` — surfaced in the event
    # log but never dispatched → silent "permission denied" for
    # web_fetch et al. (Discovered after #908 by inspecting
    # ``events/agents/*/chat/*.jsonl`` ``intervention_routed`` rows.)
    #
    # This is the same id the TUI surface registers under
    # (``chat/tui/app.py``); only one of TUI / chainlit is attached
    # to a given process so there's no collision.
    from reyn.chat.session import DEFAULT_CHAT_CHANNEL_ID
    try:
        session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    except AttributeError:
        # Stripped / mocked session in tests — degrade gracefully.
        pass

    # Replay prior chat turns from ``ChatSession.history`` (= what
    # ``load_history`` read from ``history.jsonl``) so the operator
    # sees the conversation they had previously with this agent
    # instead of an empty thread on every re-attach / browser open.
    history = getattr(session, "history", None) or []
    for entry in history_to_chainlit(history, cap=_history_cap_from_env()):
        await cl.Message(
            content=entry.content, author=entry.author,
        ).send()

    task = asyncio.create_task(_drain_loop(registry))
    cl.user_session.set(_DRAIN_KEY, task)

    # Settings panel: surface ``output_language`` as a per-cl-session
    # knob so the operator can flip Auto / 日本語 / English / 中文 / 한국어
    # without restarting `reyn chainlit`. ``cl.ChatSettings(...).send()``
    # renders the gear icon next to the input box; selecting an item
    # fires ``@cl.on_settings_update`` below.
    try:
        widgets = [
            cl.input_widget.Select(
                id=LANGUAGE_SETTING_ID,
                label="Output language",
                items=dict(LANGUAGE_ITEMS),
                initial_value=language_to_value(
                    getattr(session, "output_language", None),
                ),
                tooltip=(
                    "LLM の応答言語の指定。 Auto で LLM が user 入力に"
                    "合わせる。 変更は次の turn から有効。"
                ),
            ),
        ]
        # Model select: list tier names from the resolver (= builtins +
        # reyn.yaml::models). Only render when the resolver actually
        # exposes its resolved namespace, so a stripped-down test
        # session degrades to a single-widget panel.
        model_names = list_model_names(getattr(session, "_resolver", None))
        if model_names:
            current_model = getattr(session, "model", "") or ""
            widgets.append(
                cl.input_widget.Select(
                    id=MODEL_SETTING_ID,
                    label="Model",
                    values=model_names,
                    initial_value=current_model if current_model in model_names else model_names[0],
                    tooltip=(
                        "LLM モデル tier (= reyn.yaml::models / builtin)。 "
                        "変更は次の turn から有効。 temperature / max_tokens 等"
                        "は tier ごとの kwargs に bundle されるため、 tier 切替で"
                        "まとめて入れ替わる。"
                    ),
                )
            )
        # Agent role TextInput — surface the attached agent's persona
        # so the operator can edit it without typing
        # ``/agent edit role <text>`` slash. Same 2-side update as the
        # slash command (= profile.yaml on disk + ``session._agent_role``
        # in-memory for next turn).
        current_role = getattr(session, "agent_role", "") or ""
        widgets.append(
            cl.input_widget.TextInput(
                id=AGENT_ROLE_SETTING_ID,
                label="Agent role",
                initial=current_role,
                multiline=True,
                placeholder=(
                    "Persona text injected into the LLM system prompt. "
                    "Blank to leave as-is."
                ),
                tooltip=(
                    "Edits both ``profile.yaml`` on disk and "
                    "``session._agent_role`` in memory; the next user "
                    "turn picks up the new role."
                ),
            )
        )
        await cl.ChatSettings(widgets).send()
    except Exception:
        pass

    actions = [
        cl.Action(
            name=action_name_for(qa),
            label=qa.label,
            payload={"slash": qa.slash_text},
        )
        for qa in QUICK_ACTIONS
    ]
    await cl.Message(
        content=f"Connected to agent **{name}**.",
        author="system",
        actions=actions,
    ).send()


@cl.on_settings_update
async def _on_settings_update(settings: dict) -> None:
    """Apply the gear-icon settings panel updates to the attached session.

    Currently only handles ``output_language``. Future per-session knobs
    (= e.g. ``REYN_CHAINLIT_HISTORY_CAP`` mid-session change, allowed
    skill filter) plug into this same dispatcher.
    """
    registry = await _get_or_build_registry()
    session = registry.attached_session()
    if session is None:
        return
    if LANGUAGE_SETTING_ID in settings:
        value = settings.get(LANGUAGE_SETTING_ID)
        new_lang = value_to_language(value)
        session.output_language = new_lang
        await cl.Message(
            content=(
                f"Output language → **{language_label_for(value or 'auto')}**."
            ),
            author="system",
        ).send()
    if MODEL_SETTING_ID in settings:
        value = settings.get(MODEL_SETTING_ID)
        new_model = value_to_model(value, default=getattr(session, "model", "") or "")
        if new_model and new_model != getattr(session, "model", None):
            session.model = new_model
            await cl.Message(
                content=f"Model → **{new_model}**.",
                author="system",
            ).send()
    if AGENT_ROLE_SETTING_ID in settings:
        new_role = normalise_role(settings.get(AGENT_ROLE_SETTING_ID))
        current = getattr(session, "agent_role", "") or ""
        # Skip when blank (= operator left field empty; preserve current)
        # OR unchanged (= avoid spurious confirm message when the
        # operator toggles a different widget without touching role).
        if new_role is not None and new_role != current:
            await _persist_agent_role(registry, session, new_role)


async def _persist_agent_role(registry, session, new_role: str) -> None:
    """Mirror ``/agent edit role <text>`` from chainlit's settings panel.

    Two-side update: rewrite ``profile.yaml`` via
    ``AgentProfile.save`` so the change survives restart, and assign
    ``session._agent_role`` so the next router turn picks up the new
    role without restart. Both sides wrapped in try/except so a disk
    glitch surfaces in the chat as an error message rather than
    crashing the settings handler.
    """
    from dataclasses import replace

    from reyn.chat.profile import AgentProfile

    agent_name = getattr(session, "agent_name", None)
    if not agent_name:
        return
    project_dir = getattr(registry, "_dir", None)
    if project_dir is None:
        return
    agent_dir = project_dir / agent_name
    try:
        profile = AgentProfile.load(agent_dir)
    except FileNotFoundError:
        await cl.ErrorMessage(
            content=f"agent profile not found at {agent_dir}/profile.yaml",
        ).send()
        return
    updated = replace(profile, role=new_role)
    try:
        updated.save(agent_dir)
    except OSError as exc:
        await cl.ErrorMessage(
            content=f"failed to save agent role: {exc}",
        ).send()
        return
    session._agent_role = new_role
    await cl.Message(
        content=(
            f"Agent **{agent_name}** role updated.\n"
            "Next user turn will use the new role."
        ),
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

    # Slash routing parity with the CUI / TUI surfaces. ``submit_user_text``
    # bypasses slash dispatch — slash handling lives on the wrapper layer
    # via ``session._maybe_handle_slash``. Without this, typing ``/help``
    # would be sent to the agent as plain text instead of running the
    # slash command. Returning True from the dispatcher consumes the line
    # (including unknown slashes, which get a hint on the outbox).
    text = message.content or ""
    if is_slash(text):
        await session._maybe_handle_slash(text)
        # Chainlit-side cleanup: when reyn just wiped its own history,
        # remove the corresponding rendered messages from the browser
        # thread too. Without this, the operator sees a clean reyn
        # state but a stale-looking chat UI until they reload.
        if is_chainlit_history_wipe(text):
            await _clear_chainlit_thread()
        return

    await session.submit_user_text(text)


async def _clear_chainlit_thread() -> None:
    """Remove every rendered ``cl.Message`` from the browser thread.

    Iterates chainlit's per-session message list, calls ``.remove()``
    on each (= emits ``delete_message`` to the browser), then empties
    the server-side bookkeeping list. Best-effort: chainlit version
    drift / context unavailability is swallowed so the original slash
    response still surfaces.
    """
    try:
        from chainlit import chat_context
    except Exception:
        return
    try:
        messages = list(chat_context.get())
    except Exception:
        messages = []
    for msg in messages:
        try:
            await msg.remove()
        except Exception:
            continue
    try:
        chat_context.clear()
    except Exception:
        pass


async def _run_quick_action(action_payload: dict) -> None:
    """Shared handler body for every ``slash_<name>`` action callback.

    Lives outside the decorator so the per-action wrappers stay tiny
    and the dispatch logic is testable on its own.
    """
    registry = await _get_or_build_registry()
    session = registry.attached_session()
    if session is None:
        await cl.ErrorMessage(
            content="No agent attached. Reload the page to reconnect.",
        ).send()
        return
    slash_text = action_payload.get("slash") if action_payload else None
    if not isinstance(slash_text, str) or not is_slash(slash_text):
        return
    await session._maybe_handle_slash(slash_text)


def _register_action_callbacks() -> None:
    """Bind one ``@cl.action_callback("slash_<name>")`` per QuickAction.

    Done at module load via a loop so adding a new entry to
    ``QUICK_ACTIONS`` is the only edit needed — no duplicated handler
    boilerplate per command.
    """

    def _make_handler(qa: QuickAction):
        async def _handler(action: cl.Action) -> None:
            await _run_quick_action(getattr(action, "payload", None) or {})
        _handler.__name__ = f"_on_action_{qa.name}"
        return _handler

    for qa in QUICK_ACTIONS:
        cl.action_callback(action_name_for(qa))(_make_handler(qa))


_register_action_callbacks()


@cl.on_chat_end
async def _on_chat_end() -> None:
    task = cl.user_session.get(_DRAIN_KEY)
    if task is not None:
        task.cancel()
    # Drop the chainlit listener registration so a subsequent
    # ``_on_chat_start`` re-adds cleanly and idle-state IVs aren't
    # mis-classified as listener-present. Best-effort: registry
    # missing / session detached is a no-op.
    try:
        registry = await _get_or_build_registry()
        session = registry.attached_session()
        if session is not None:
            from reyn.chat.session import DEFAULT_CHAT_CHANNEL_ID
            session.unregister_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    except Exception:
        pass
