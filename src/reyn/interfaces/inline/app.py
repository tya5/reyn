"""Claude Code-style interactive input driver for the inline CUI.

A long-lived prompt_toolkit Application that drives input for the interactive
(TTY) inline renderer: a rule-bar sandwiched input, an animated working row, and
a navigable status menu (↓ to focus, ←→ to select a chip, enter to open a
read-only detail dropdown, ↑/esc to go back).

Integration: run_repl's `_output_loop` prints conversation output ABOVE this app
via `run_in_terminal` (the app stays a live region at the bottom); user input is
fed to the session via `submit_user_text`, so intervention answers / slash
commands / new turns route through the session exactly as the PromptSession path
did — the app never inspects the text.

`--cui` / non-TTY keep the existing PromptSession `_input_loop` (plain invariance).
The status menu reads live values through public sync accessors only; an
actionable model picker (selecting a class) is a follow-up.
"""
from __future__ import annotations

import asyncio
import logging
import time

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout

from reyn.interfaces.inline.intervention_region import build_intervention_element
from reyn.interfaces.inline.region import Region
from reyn.interfaces.repl.renderer import _CC_ACCENT, _CC_DIM, _CC_DONE, _SPINNER

logger = logging.getLogger(__name__)

# Status-row chips, in display order.
_CHIPS = ["model", "agents", "skills", "cost", "ctx"]
# Chips whose dropdown is an actionable picker (↑↓ a row, enter applies) rather
# than a read-only detail panel. Today only the model picker.
_ACTIONABLE = frozenset({"model"})


def is_actionable_picker(label: str, model_classes) -> bool:
    """Pure: whether a chip's open dropdown is an actionable picker right now.

    The model chip is a picker only when it actually has classes to pick; with
    none configured its dropdown is the read-only current-model fallback, which
    has no selectable rows — so no cursor must be drawn on it (otherwise the
    fallback lines look selectable but Enter does nothing).
    """
    return label in _ACTIONABLE and bool(model_classes)


def model_switch_text(model_classes: list, row: int) -> str | None:
    """Pure: the slash command a model-picker Enter submits for the row.

    Returns ``/model <class>`` for an in-range row, or None (out of range /
    empty list) so the caller submits nothing. The switch itself — cost-warn
    confirm, budget rebuild — is the existing ``/model`` slash path.
    """
    if 0 <= row < len(model_classes):
        return f"/model {model_classes[row]}"
    return None


def working_line(thinking: bool, think_start: float, now: float) -> list:
    """Pure: working-row fragments while a turn runs (empty list when idle).

    The spinner frame derives from `now` so it advances smoothly regardless of
    refresh jitter; elapsed is whole seconds since `think_start`.
    """
    if not thinking:
        return []
    frame = _SPINNER[int(now * 8) % len(_SPINNER)]
    elapsed = max(0, int(now - think_start))
    return [
        (f"fg:{_CC_ACCENT}", f" {frame} "),
        (f"fg:{_CC_DIM}", f"Working… {elapsed}s"),
    ]


def status_chips(model: str, n_agents: int, n_skills: int, cost_usd: float,
                 total_tokens: int) -> list:
    """Pure: (label, value) status chips from plain live values."""
    ctx = f"{total_tokens // 1000}k" if total_tokens >= 1000 else str(total_tokens)
    return [
        ("model", str(model)),
        ("agents", str(n_agents)),
        ("skills", str(n_skills)),
        ("cost", f"${cost_usd:.4f}"),
        ("ctx", ctx),
    ]


def dropdown_lines(label: str, *, model: str, agent_names: list, attached_name,
                   skill_run_ids: list, usage: tuple, cost_usd: float,
                   model_classes: list | None = None) -> list:
    """Pure: detail/picker lines for an opened status chip.

    Lines starting with ``▸`` mark the focused/attached/current entry. `usage` is
    ``(prompt, completion, total)`` token counts. For ``model`` the lines are the
    selectable classes (current marked ``▸``); with no configured classes it
    falls back to the read-only current-model hint.
    """
    if label == "agents":
        if not agent_names:
            return ["(none)"]
        return [f"▸ {n}" if n == attached_name else f"  {n}" for n in agent_names]
    if label == "skills":
        return [f"  {r}" for r in skill_run_ids] or ["(no running skills)"]
    if label in ("cost", "ctx"):
        p, c, t = usage
        return [
            f"prompt {p}", f"completion {c}", f"total {t}",
            f"cost ${cost_usd:.4f}",
        ]
    if label == "model":
        if not model_classes:
            return [f"current: {model}", "change with /model"]
        return [f"▸ {c}" if c == model else f"  {c}" for c in model_classes]
    return ["(no detail)"]


def _snapshot(registry):
    """Read live status values off the attached session via sync accessors."""
    s = registry.attached_session()
    if s is None:
        return None
    u = s.total_usage
    return {
        "model": s.model,
        "model_classes": list(s.known_model_classes()),
        "agent_names": list(registry.loaded_names()),
        "attached_name": registry.attached_name,
        "skill_run_ids": list(s.running_skills.keys()),
        "usage": (u.prompt_tokens, u.completion_tokens, u.total_tokens),
        "cost_usd": s.total_cost_usd,
    }


async def run_inline_input(registry, renderer) -> None:
    """Run the interactive inline input Application until the user quits.

    Returns on quit (Ctrl-C/D/Q or /quit /exit) so run_repl can tear down (cost
    summary) via its FIRST_COMPLETED wait.
    """
    attached = registry.attached_session()
    history = FileHistory(str(attached.workspace_dir / ".input_history"))
    buf = Buffer(multiline=False, history=history)
    # sel: which chip; open: detail/picker shown; row: cursor within an
    # actionable picker (model). row is reset to 0 when a picker opens.
    menu = {"sel": 0, "open": False, "row": 0}
    # Guards against a second quit (rapid Ctrl-C / `/quit` then Ctrl-C) racing the
    # first: shutdown has a grace window, so two _quit tasks could both reach
    # app.exit() — the second raises "Return value already set". _quit checks+sets
    # this before its first await, so only one ever runs to app.exit().
    quitting: dict = {}
    # Above-input interactive region: hosts the active closed-set intervention
    # (confirm / select / grant-deny) as a selectable list, poll-driven off the
    # session head (like the status chips). Free-text interventions keep using the
    # input field, so they get no element. Empty region collapses (inert).
    above_region = Region()
    iv_holder: dict = {"iv_id": None}  # which intervention the region is showing

    def _working_frags() -> list:
        return working_line(
            getattr(renderer, "_thinking", False),
            getattr(renderer, "_think_start", 0.0),
            time.monotonic(),
        )

    working = ConditionalContainer(
        Window(FormattedTextControl(_working_frags), height=1),
        filter=Condition(lambda: getattr(renderer, "_thinking", False)),
    )
    top_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    bottom_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    prompt_sym = Window(
        FormattedTextControl([(f"fg:{_CC_ACCENT} bold", "> ")]), width=2, height=1
    )
    input_win = Window(BufferControl(buffer=buf), height=1)
    inputrow = VSplit([prompt_sym, input_win])

    def _current_dropdown_lines(snap) -> list:
        label = _CHIPS[menu["sel"]]
        return dropdown_lines(
            label, model=snap["model"], agent_names=snap["agent_names"],
            attached_name=snap["attached_name"],
            skill_run_ids=snap["skill_run_ids"], usage=snap["usage"],
            cost_usd=snap["cost_usd"], model_classes=snap["model_classes"],
        )

    def status_fragments() -> list:
        snap = _snapshot(registry)
        if snap is None:
            return [(f"fg:{_CC_DIM}", " /quit to exit · ↑ history")]
        chips = status_chips(
            snap["model"], len(snap["agent_names"]), len(snap["skill_run_ids"]),
            snap["cost_usd"], snap["usage"][2],
        )
        focused = get_app().layout.has_focus(status_win)
        frags: list = []
        for i, (label, val) in enumerate(chips):
            open_mark = " ▾" if (focused and menu["open"] and i == menu["sel"]) else ""
            text = f" {label} {val}{open_mark} "
            if focused and i == menu["sel"]:
                frags.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", text))
            else:
                frags.append((f"fg:{_CC_DIM}", text))
            frags.append(("", " "))
        if not focused:
            hint = "  [↓ menu · ↑ history · /quit]"
        elif menu["open"] and _CHIPS[menu["sel"]] in _ACTIONABLE:
            hint = "  [↑↓ select · enter switch · esc close]"
        elif menu["open"]:
            hint = "  [esc / ↑ close]"
        else:
            hint = "  [↑ input · ←→ select · enter open]"
        frags.append((f"fg:{_CC_DIM}", hint))
        return frags

    status_win = Window(
        FormattedTextControl(status_fragments, focusable=True), height=1
    )

    def dropdown_frags() -> list:
        snap = _snapshot(registry)
        if snap is None:
            return []
        actionable = is_actionable_picker(_CHIPS[menu["sel"]], snap["model_classes"])
        out: list = []
        for i, ln in enumerate(_current_dropdown_lines(snap)):
            if i:
                out.append(("", "\n"))
            if actionable and i == menu["row"]:
                # picker cursor: reverse-highlight the selectable row
                out.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {ln} "))
            else:
                style = _CC_DONE if ln.startswith("▸") else _CC_DIM
                out.append((f"fg:{style}", f"   {ln}"))
        return out

    def dropdown_height() -> Dimension:
        snap = _snapshot(registry)
        if snap is None:
            return Dimension.exact(0)
        return Dimension.exact(len(_current_dropdown_lines(snap)))

    dropdown = ConditionalContainer(
        Window(FormattedTextControl(dropdown_frags), height=dropdown_height),
        filter=Condition(
            lambda: menu["open"] and get_app().layout.has_focus(status_win)
        ),
    )

    def above_region_frags() -> list:
        out: list = []
        for i, ln in enumerate(above_region.lines()):
            if i:
                out.append(("", "\n"))
            if i == above_region.cursor:
                out.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {ln} "))
            else:
                out.append((f"fg:{_CC_DIM}", f"   {ln}"))
        return out

    def above_region_height() -> Dimension:
        return Dimension.exact(len(above_region.lines()))

    above_region_win = Window(
        FormattedTextControl(above_region_frags, focusable=True),
        height=above_region_height,
    )
    above_region_box = ConditionalContainer(
        above_region_win, filter=Condition(lambda: above_region.visible)
    )

    kb = KeyBindings()

    @kb.add("enter", filter=has_focus(input_win))
    def _accept(event) -> None:
        text = buf.text
        buf.reset(append_to_history=True)
        stripped = text.strip()
        if not stripped:
            return
        # /quit /exit tear the REPL down (mirrors _input_loop); everything else —
        # plain text, intervention answers, other slash — flows through
        # submit_user_text and routes inside the session unchanged.
        if stripped in ("/quit", "/exit"):
            event.app.create_background_task(_quit(registry, event.app, quitting))
        else:
            event.app.create_background_task(_submit(registry, stripped))

    @kb.add("down", filter=has_focus(input_win))
    def _to_menu(event) -> None:
        event.app.layout.focus(status_win)

    def _actionable_open() -> bool:
        return menu["open"] and _CHIPS[menu["sel"]] in _ACTIONABLE

    @kb.add("up", filter=has_focus(status_win))
    def _menu_up(event) -> None:
        # In an open picker, ↑ moves the cursor up; at the top row it closes.
        if _actionable_open() and menu["row"] > 0:
            menu["row"] -= 1
        elif menu["open"]:
            menu["open"] = False
        else:
            event.app.layout.focus(input_win)

    @kb.add("down", filter=has_focus(status_win))
    def _menu_down(event) -> None:
        if _actionable_open():
            snap = _snapshot(registry)
            n = len(snap["model_classes"]) if snap else 0
            if n:
                menu["row"] = min(menu["row"] + 1, n - 1)

    @kb.add("escape", filter=has_focus(status_win))
    def _menu_esc(event) -> None:
        if menu["open"]:
            menu["open"] = False
        else:
            event.app.layout.focus(input_win)

    @kb.add("left", filter=has_focus(status_win))
    def _menu_left(event) -> None:
        if not menu["open"]:
            menu["sel"] = (menu["sel"] - 1) % len(_CHIPS)

    @kb.add("right", filter=has_focus(status_win))
    def _menu_right(event) -> None:
        if not menu["open"]:
            menu["sel"] = (menu["sel"] + 1) % len(_CHIPS)

    @kb.add("enter", filter=has_focus(status_win))
    def _menu_enter(event) -> None:
        # Read-only chip: enter toggles the detail panel. Actionable picker:
        # when open, enter applies the cursor row via the existing /model slash
        # path (cost-warn + budget rebuild reused); when closed, it opens.
        if _actionable_open():
            snap = _snapshot(registry)
            classes = snap["model_classes"] if snap else []
            text = model_switch_text(classes, menu["row"])
            if text is not None:
                event.app.create_background_task(_submit(registry, text))
            menu["open"] = False
        else:
            menu["open"] = not menu["open"]
            menu["row"] = 0

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("c-q")
    def _quit_key(event) -> None:
        event.app.create_background_task(_quit(registry, event.app, quitting))

    # Above-region focus navigation (inert until a consumer registers an element,
    # since the region stays invisible + unfocusable while empty). ↑↓ move the
    # cursor, enter activates the focused row, esc returns to the input.
    @kb.add("up", filter=has_focus(above_region_win))
    def _region_up(event) -> None:
        above_region.navigate(-1)

    @kb.add("down", filter=has_focus(above_region_win))
    def _region_down(event) -> None:
        above_region.navigate(1)

    @kb.add("enter", filter=has_focus(above_region_win))
    def _region_select(event) -> None:
        above_region.select()

    @kb.add("escape", filter=has_focus(above_region_win))
    def _region_esc(event) -> None:
        event.app.layout.focus(input_win)

    def _sync_intervention_region() -> None:
        """Sync the above-region with the session's head closed-set intervention.

        Poll-driven (like the status chips). When a new closed-set intervention is
        pending, register a selector element + auto-focus it; when it resolves /
        is gone, clear it and hand focus back to the input. Inert when nothing is
        pending — same as an empty region.
        """
        s = registry.attached_session()
        head = s.interventions.head() if s is not None else None
        head_id = head.id if (head is not None and getattr(head, "choices", None)) else None
        if head_id == iv_holder["iv_id"]:
            return
        above_region.clear()
        iv_holder["iv_id"] = head_id
        if head_id is None:
            app.layout.focus(input_win)
            return
        element = build_intervention_element(
            head,
            lambda cid, label: app.create_background_task(
                _deliver_intervention_choice(registry, cid, label)
            ),
        )
        if element is not None:
            above_region.register(element)
            app.layout.focus(above_region_win)

    async def _intervention_poll() -> None:
        while True:
            await asyncio.sleep(0.15)
            try:
                _sync_intervention_region()
                app.invalidate()
            except Exception:
                logger.exception("inline: intervention region poll failed")

    body = HSplit(
        [working, above_region_box, top_rule, inputrow, bottom_rule, status_win,
         dropdown]
    )
    app: Application = Application(
        layout=Layout(body, focused_element=input_win),
        key_bindings=kb,
        full_screen=False,
        refresh_interval=0.1,
    )
    # patch_stdout so stray stdout/stderr (e.g. library warnings) prints cleanly
    # above the live input region instead of corrupting it — mirrors the
    # PromptSession path. Renderer output (sys.__stdout__ via run_in_terminal in
    # _output_loop) is unaffected.
    poll_task = asyncio.create_task(_intervention_poll())
    try:
        with patch_stdout():
            await app.run_async()
    finally:
        poll_task.cancel()


async def _deliver_intervention_choice(registry, choice_id: str, label: str) -> None:
    """Deliver a region-selected intervention choice + echo it to scrollback.

    The chosen choice id is delivered authoritatively (choice_id_override). On
    success, a uniform ``answered: <label>`` line is put on the outbox so EVERY
    resolved intervention leaves a trace in the conversation — not just the ones
    (like permission) whose side effect happens to be visible. ask_user /
    safety-limit interventions otherwise vanish from scrollback on resolution.
    """
    s = registry.attached_session()
    if s is None:
        return
    try:
        delivered = await s.answer_oldest_intervention_choice(choice_id)
    except Exception:
        logger.exception("inline: delivering intervention choice failed")
        return
    if delivered:
        from reyn.runtime.outbox import OutboxMessage
        # kind="intervention" (not "status") so the echo PERSISTS in scrollback:
        # "status"/"trace" are transient (cleared by the next message), and the
        # intervention_resolved event fires right after, so a status echo would be
        # erased before the user sees it.
        registry.repl_outbox.put_nowait(
            OutboxMessage(kind="intervention", text=f"answered: {label}")
        )


async def _submit(registry, text: str) -> None:
    s = registry.attached_session()
    if s is None:
        return
    # Launched as a background task, so an uncaught error here goes to asyncio's
    # exception handler (invisible above the live app) and the user sees the input
    # field clear with no response — a silent failure. Contain it: log + surface a
    # visible error line via the outbox the output loop already drains.
    try:
        await s.submit_user_text(text)
    except Exception:
        logger.exception("inline submit failed")
        try:
            from reyn.runtime.outbox import OutboxMessage
            registry.repl_outbox.put_nowait(
                OutboxMessage(kind="error", text="input could not be submitted (see logs)")
            )
        except Exception:
            pass


async def _quit(registry, app, state: dict) -> None:
    # Idempotent: shutdown has a grace window, so a second quit (rapid Ctrl-C /
    # `/quit` then Ctrl-C) could race the first to app.exit() ("Return value
    # already set"). The check+set is synchronous (before the first await), so in
    # the single-threaded loop only the first task proceeds to app.exit().
    if state.get("quitting"):
        return
    state["quitting"] = True
    await registry.shutdown()
    app.exit()
