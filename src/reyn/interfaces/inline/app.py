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
from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition, has_completions, has_focus
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout

from reyn.interfaces.inline.intervention_region import build_intervention_element
from reyn.interfaces.inline.region import DetailElement, Region
from reyn.interfaces.inline.region_command import (
    CommandUIElement,
    build_rewind_command_ui,
)
from reyn.interfaces.repl.renderer import _CC_ACCENT, _CC_DIM, _CC_DONE, _SPINNER
from reyn.interfaces.slash import slash_command_completions

logger = logging.getLogger(__name__)


class _SlashCompleter(Completer):
    """Autocomplete a leading ``/command`` token from the slash registry.

    Only the command word is completed — once a space is typed (args begin) the
    completer goes quiet, so ``/model standard`` doesn't keep suggesting commands.
    Each completion shows ``/name`` with the command's one-line summary as meta.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        prefix = text[1:]
        for name, summary in slash_command_completions(prefix):
            yield Completion(
                name, start_position=-len(prefix),
                display=f"/{name}", display_meta=summary,
            )


_SLASH_COMPLETER = _SlashCompleter()


@dataclass(frozen=True)
class ChipSpec:
    """One status-bar chip: label + live value + optional expansion element.

    The status bar renders by iterating the registered specs, so a new chip is one
    spec — no per-chip branching in the renderer or key bindings. ``expansion`` is
    called with ``(snapshot, dispatch)`` and returns a region element (DetailElement
    / CommandUIElement / future Tree/Toggle), or None for a value-only chip.
    """

    key: str
    label: str
    value: Callable[[dict], str]
    expansion: "Callable[[dict, Callable[[str], None]], object] | None" = None


def _model_expansion(snap, dispatch):
    classes = list(snap.get("model_classes") or [])
    if not classes:
        return DetailElement(lambda: [f"current: {snap['model']}", "change with /model"])
    model = snap["model"]
    rows = [f"▸ {c}" if c == model else f"  {c}" for c in classes]
    return CommandUIElement(rows, [f"/model {c}" for c in classes], dispatch)


def _cost_expansion(snap, dispatch):
    def lines():
        p, c, t = snap["usage"]
        session = snap["cost_usd"]
        return [
            f"total    ${snap.get('cost_total', session):.4f}",
            f"agent    ${snap.get('cost_agent', session):.4f}",
            f"session  ${session:.4f}",
            f"tokens   prompt {p} · completion {c} · total {t}",
        ]
    return DetailElement(lines)


def _agent_expansion(snap, dispatch):
    # Phase 2: agent/session tree; selecting a row attaches / switches.
    tree = snap.get("session_tree") or []
    rows: list[str] = []
    cmds: list[str] = []
    for agent in tree:
        amark = "▸" if agent["attached"] else " "
        rows.append(f"{amark} {agent['agent']}")
        cmds.append(f"/attach {agent['agent']}")
        for sess in agent["sessions"]:
            smark = "▸" if sess["attached"] else " "
            rows.append(f"    {smark} {sess['sid']}")
            # switch the session when its agent is already attached; otherwise
            # attach the agent first (the user can then switch session).
            if agent["attached"]:
                cmds.append(f"/session switch {sess['sid']}")
            else:
                cmds.append(f"/attach {agent['agent']}")
    if not rows:
        return DetailElement(lambda: ["(no agents)"])
    return CommandUIElement(rows, cmds, dispatch)


def _build_task_tree(task_dicts: list[dict]) -> list[dict]:
    """Build a nested task tree from a flat list of Task.to_dict() projections.

    Roots are tasks whose requester_kind is not "task" or whose requester is
    not the task_id of any task in the input. Children are tasks with
    requester_kind == "task" and requester == parent task_id. Siblings are
    sorted by task_id for determinism. Cycles are guarded by tracking visited
    task_ids so no task appears twice.
    """
    by_id: dict[str, dict] = {d["task_id"]: d for d in task_dicts}
    task_ids: frozenset[str] = frozenset(by_id)

    def _is_root(d: dict) -> bool:
        return d.get("requester_kind") != "task" or d.get("requester") not in task_ids

    def _children_of(parent_id: str, visited: set[str]) -> list[dict]:
        kids = [
            d for d in task_dicts
            if d.get("requester_kind") == "task"
            and d.get("requester") == parent_id
            and d["task_id"] not in visited
        ]
        kids.sort(key=lambda d: d["task_id"])
        result = []
        for k in kids:
            visited.add(k["task_id"])
            result.append({
                "task_id": k["task_id"],
                "name": k["name"],
                "status": k["status"],
                "children": _children_of(k["task_id"], visited),
            })
        return result

    roots = sorted(
        [d for d in task_dicts if _is_root(d)],
        key=lambda d: d["task_id"],
    )
    visited: set[str] = {d["task_id"] for d in roots}
    return [
        {
            "task_id": r["task_id"],
            "name": r["name"],
            "status": r["status"],
            "children": _children_of(r["task_id"], visited),
        }
        for r in roots
    ]


def _task_expansion(snap, dispatch):
    # Phase 3: task tree. Depth-first indented rows (2 spaces per depth).
    tree = snap.get("task_tree") or []
    if not tree:
        return DetailElement(lambda: ["(no active tasks)"])

    def _rows(nodes: list[dict], depth: int) -> list[str]:
        out = []
        for node in nodes:
            out.append(f"{'  ' * depth}{node['status']}  {node['name']}")
            out.extend(_rows(node["children"], depth + 1))
        return out

    rows = _rows(tree, 0)
    return DetailElement(lambda: rows)


def _more_expansion(snap, dispatch):
    """Read-only overflow panel: cron jobs / mcp servers / hooks from config.

    Renders a sectioned listing — one header per category (always shown, even
    when the section is empty) with indented item rows. No actions, no dispatch
    calls — purely informational. Toggles are a separate OS-lane design (#2285).
    """
    cron_jobs = snap.get("cron_jobs") or []
    mcp_servers = snap.get("mcp_servers") or []
    hooks = snap.get("hooks") or []

    lines: list[str] = []

    lines.append(f"cron  ({len(cron_jobs)})")
    if cron_jobs:
        for j in cron_jobs:
            marker = "on" if j["enabled"] else "off"
            lines.append(f"  [{marker}] {j['name']}  {j['schedule']}")
    else:
        lines.append("  (none)")

    lines.append(f"mcp  ({len(mcp_servers)})")
    if mcp_servers:
        for s in mcp_servers:
            lines.append(f"  {s['name']}")
    else:
        lines.append("  (none)")

    lines.append(f"hooks  ({len(hooks)})")
    if hooks:
        for h in hooks:
            lines.append(f"  {h['label']}")
    else:
        lines.append("  (none)")

    captured = lines[:]
    return DetailElement(lambda: captured)


_CHIP_SPECS = [
    ChipSpec("model", "model", lambda s: str(s["model"]), _model_expansion),
    ChipSpec("cost",  "cost",  lambda s: f"${s['cost_usd']:.4f}", _cost_expansion),
    ChipSpec("agent", "agent", lambda s: str(s["attached_name"] or "—"), _agent_expansion),
    ChipSpec("task",  "task",  lambda s: str(s.get("task_count", 0)), _task_expansion),
    ChipSpec("more",  "",      lambda s: "…", _more_expansion),   # overflow submenu — Phase 5a
]


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


def _extract_cron_jobs(config) -> list[dict]:
    """Extract cron job dicts from config. Returns [] on any missing/malformed section."""
    cron = getattr(config, "cron", None)
    jobs = getattr(cron, "jobs", None) if cron is not None else None
    if not jobs:
        return []
    result = []
    for j in jobs:
        try:
            result.append({
                "name": j.name,
                "schedule": j.schedule,
                "enabled": bool(j.enabled),
            })
        except Exception:  # noqa: BLE001
            pass
    return result


def _extract_mcp_servers(config) -> list[dict]:
    """Extract mcp server name dicts from config. Returns [] on any missing/malformed section."""
    mcp = getattr(config, "mcp", None)
    if mcp is None:
        return []
    # mcp may be a dict with a "servers" sub-key, or a flat {name: cfg} dict.
    if isinstance(mcp, dict):
        servers = mcp.get("servers", None)
        if isinstance(servers, dict):
            source = servers
        else:
            # Flat dict — values should be dicts (server configs).
            source = {k: v for k, v in mcp.items() if isinstance(v, dict)}
    else:
        return []
    return [{"name": name} for name in source]


def _extract_hooks(config) -> list[dict]:
    """Extract hook label dicts from config. Returns [] on any missing/malformed section."""
    hooks_raw = getattr(config, "hooks", None)
    if not hooks_raw:
        return []
    result = []
    _HOOK_EVENT_KEYS = frozenset({
        "event", "hook", "on", "trigger", "type", "name", "hook_point",
    })
    for i, entry in enumerate(hooks_raw):
        try:
            if isinstance(entry, dict):
                # Best-effort label: prefer a hook-point/event-ish key.
                label_key = next(
                    (k for k in _HOOK_EVENT_KEYS if k in entry), None
                )
                if label_key is None:
                    label_key = next(iter(entry), None)
                label = str(entry[label_key])[:40] if label_key else f"hook {i}"
            else:
                label = str(entry)[:40]
        except Exception:  # noqa: BLE001
            label = f"hook {i}"
        result.append({"label": label})
    return result


def _snapshot(registry, task_cache=None, config=None):
    """Read live status values off the attached session via sync accessors."""
    s = registry.attached_session()
    if s is None:
        return None
    u = s.total_usage
    # Cost breakdown (all via public sync accessors). Sum across ALL sessions
    # (every sid), not just each agent's "main" — a spawned sub-session accrues
    # cost too. Nested: session ≤ agent (all the attached agent's sids) ≤ total
    # (all agents, all sids).
    def _agent_cost(name: str) -> float:
        total = 0.0
        for sid in registry.session_ids(name):
            sess = registry.get_session(name, sid)
            if sess is not None:
                total += sess.total_cost_usd
        return total

    cost_total = sum(_agent_cost(name) for name in registry.loaded_names())
    cost_agent = (
        _agent_cost(registry.attached_name)
        if registry.attached_name else s.total_cost_usd
    )
    return {
        "model": s.model,
        "model_classes": list(s.known_model_classes()),
        "agent_names": list(registry.loaded_names()),
        "attached_name": registry.attached_name,
        "session_tree": registry.session_tree(),
        "skill_run_ids": list(s.running_skills.keys()),
        "usage": (u.prompt_tokens, u.completion_tokens, u.total_tokens),
        "cost_usd": s.total_cost_usd,
        "cost_total": cost_total,
        "cost_agent": cost_agent,
        "task_count": task_cache["count"] if task_cache else 0,
        "task_tree": task_cache["tree"] if task_cache else [],
        "cron_jobs": _extract_cron_jobs(config) if config is not None else [],
        "mcp_servers": _extract_mcp_servers(config) if config is not None else [],
        "hooks": _extract_hooks(config) if config is not None else [],
    }


async def run_inline_input(registry, renderer, config=None) -> None:
    """Run the interactive inline input Application until the user quits.

    Returns on quit (Ctrl-C/D/Q or /quit /exit) so run_repl can tear down (cost
    summary) via its FIRST_COMPLETED wait.

    ``config`` is the loaded ReynConfig (or None), threaded read-only into
    ``_snapshot`` so the ``…`` overflow chip can list cron jobs / mcp servers /
    hooks. When None (--cui / non-inline path) the overflow panel shows empty
    sections — backward-compatible.
    """
    attached = registry.attached_session()
    history = FileHistory(str(attached.workspace_dir / ".input_history"))
    buf = Buffer(
        multiline=False, history=history,
        completer=_SLASH_COMPLETER, complete_while_typing=True,
    )
    # sel: which chip; open: detail/picker shown. The dropdown's selectable
    # cursor lives in `menu_region` (the below-input Region hosting the opened
    # chip's element), not here — the same selection mechanism as the above-input
    # region (interventions / the /rewind picker), unified in F5.
    menu = {"sel": 0, "open": False}
    # Async-polled task cache: updated every ~1 s by _task_poll; read by
    # _snapshot so the status bar and dropdown reflect live active tasks.
    task_cache: dict = {"tree": [], "count": 0}
    # Below-input region: hosts the opened chip's dropdown as a region element —
    # a CommandUIElement model picker (selectable) or a read-only DetailElement
    # (live detail, no cursor). Empty (cleared) while the menu is closed.
    menu_region = Region()
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
    # What the region is currently showing: "iv:<id>" (intervention) or
    # "cmd:<id>" (command-UI) or None. Lets the poll skip rebuilding each tick.
    region_holder: dict = {"key": None}

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
        FormattedTextControl([(f"fg:{_CC_ACCENT} bold", "❯ ")]), width=2, height=1
    )
    input_win = Window(BufferControl(buffer=buf), height=1)
    inputrow = VSplit([prompt_sym, input_win])

    def status_fragments() -> list:
        snap = _snapshot(registry, task_cache, config)
        if snap is None:
            return [(f"fg:{_CC_DIM}", " /quit to exit · ↑ history")]
        focused = get_app().layout.has_focus(status_win)
        frags: list = []
        for i, spec in enumerate(_CHIP_SPECS):
            val = spec.value(snap)
            if spec.label:
                text = f" {spec.label} {val} "
            else:
                text = f" {val} "
            selected = focused and i == menu["sel"]
            if selected and menu["open"]:
                text = text.rstrip() + " ▾ "
            if selected:
                frags.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", text))
            else:
                frags.append((f"fg:{_CC_DIM}", text))
            if i < len(_CHIP_SPECS) - 1:
                frags.append((f"fg:{_CC_DIM}", "│"))
        if not focused:
            hint = "  [↓ menu · ↑ history · /quit]"
        elif menu["open"] and menu_region.cursor_on_selectable:
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
        # The opened chip's element lives in menu_region; render its live rows.
        # The focus cursor is drawn only on a selectable row (the model picker) —
        # a read-only DetailElement reports cursor_on_selectable False, so its
        # detail panel shows no highlight, exactly as before.
        draw_cursor = menu_region.cursor_on_selectable
        out: list = []
        for i, ln in enumerate(menu_region.lines()):
            if i:
                out.append(("", "\n"))
            if draw_cursor and i == menu_region.cursor:
                out.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {ln} "))
            else:
                style = _CC_DONE if ln.startswith("▸") else _CC_DIM
                out.append((f"fg:{style}", f"   {ln}"))
        return out

    def dropdown_height() -> Dimension:
        return Dimension.exact(len(menu_region.lines()))

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
            # Echo the submitted line into the scrollback BEFORE the turn runs.
            # The live input field is cleared on submit (buf.reset above), so
            # without this the user's own message never appears in the
            # conversation (only the agent reply does). kind="user" persists (not
            # a transient status), and the output loop drains the outbox FIFO, so
            # it lands just above the agent reply — mirroring the PromptSession
            # path, where the typed line stays committed in the terminal.
            from reyn.runtime.outbox import OutboxMessage
            registry.repl_outbox.put_nowait(OutboxMessage(kind="user", text=stripped))
            event.app.create_background_task(_submit(registry, stripped))

    @kb.add("down", filter=has_focus(input_win) & ~has_completions)
    def _down(event) -> None:
        # With text in the box, ↓ is history navigation (forward, shell-like) so
        # it doesn't yank focus away mid-edit. With an empty box, ↓ drops into the
        # status menu (its discoverable affordance). ↑ stays history either way.
        # Gated on ~has_completions so that while the slash menu is open ↓ falls
        # through to the default binding (navigate the completion list).
        if buf.text:
            buf.history_forward()
        else:
            event.app.layout.focus(status_win)

    def _actionable_open() -> bool:
        # Open AND the opened chip's element is the selectable model picker (a
        # CommandUIElement). Check the live menu_region cursor state — a selectable
        # element (CommandUIElement with classes) reports True; a read-only
        # DetailElement always reports False.
        return menu["open"] and menu_region.cursor_on_selectable

    def _menu_submit(text: str) -> None:
        # A picker row was selected → close the menu and run /model <class> via the
        # normal slash path (cost-warn confirm + budget rebuild reused).
        _menu_close()
        app.create_background_task(_submit(registry, text))

    def _menu_open() -> None:
        # Build the opened chip's element fresh and host it in menu_region. The
        # picker rows are static (classes); a read-only panel's lines stay live.
        spec = _CHIP_SPECS[menu["sel"]]
        menu_region.clear()
        if spec.expansion is not None:
            el = spec.expansion(_snapshot(registry, task_cache, config), _menu_submit)
            menu_region.register(el)
        menu["open"] = True

    def _menu_close() -> None:
        menu["open"] = False
        menu_region.clear()

    @kb.add("up", filter=has_focus(status_win))
    def _menu_up(event) -> None:
        # In an open picker, ↑ moves the cursor up; at the top row it closes. A
        # read-only panel has no cursor, so ↑ simply closes it.
        if _actionable_open() and not menu_region.at_first_selectable:
            menu_region.navigate(-1)
        elif menu["open"]:
            _menu_close()
        else:
            event.app.layout.focus(input_win)

    @kb.add("down", filter=has_focus(status_win))
    def _menu_down(event) -> None:
        if _actionable_open():
            menu_region.navigate(1)

    @kb.add("escape", filter=has_focus(status_win))
    def _menu_esc(event) -> None:
        if menu["open"]:
            _menu_close()
        else:
            event.app.layout.focus(input_win)

    @kb.add("left", filter=has_focus(status_win))
    def _menu_left(event) -> None:
        if not menu["open"]:
            menu["sel"] = (menu["sel"] - 1) % len(_CHIP_SPECS)

    @kb.add("right", filter=has_focus(status_win))
    def _menu_right(event) -> None:
        if not menu["open"]:
            menu["sel"] = (menu["sel"] + 1) % len(_CHIP_SPECS)

    @kb.add("enter", filter=has_focus(status_win))
    def _menu_enter(event) -> None:
        # Read-only chip: enter toggles the detail panel. Actionable picker: when
        # open, enter applies the cursor row (region.select → on_submit → /model
        # via the slash path); when closed, it opens the dropdown.
        if _actionable_open():
            menu_region.select()
        elif menu["open"]:
            _menu_close()
        else:
            _menu_open()

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

    def _show(element, key: str) -> None:
        above_region.clear()
        region_holder["key"] = key
        above_region.register(element)
        app.layout.focus(above_region_win)

    def _cmd_submit(text: str) -> None:
        s = registry.attached_session()
        if s is not None:
            s.set_pending_command_ui(None)  # consume the request
        app.create_background_task(_submit(registry, text))

    def _sync_region() -> None:
        """Sync the above-region with the session's pending UI: the head closed-set
        intervention (priority — it blocks a turn), else a command-UI request (the
        /rewind picker etc.). Poll-driven, like the status chips. Inert when
        nothing is pending — same as an empty region.
        """
        s = registry.attached_session()
        head = s.interventions.head() if s is not None else None
        if head is not None and getattr(head, "choices", None):
            key = f"iv:{head.id}"
            if key != region_holder["key"]:
                _show(build_intervention_element(
                    head,
                    lambda cid, label: app.create_background_task(
                        _deliver_intervention_choice(registry, cid, label)
                    ),
                ), key)
            return
        cmd = s.pending_command_ui if s is not None else None
        if cmd is not None and cmd.get("kind") == "rewind":
            key = f"cmd:{id(cmd)}"
            if key != region_holder["key"]:
                _show(build_rewind_command_ui(cmd.get("points") or [], _cmd_submit), key)
            return
        # Nothing pending → collapse the region and return focus to the input.
        if region_holder["key"] is not None:
            above_region.clear()
            region_holder["key"] = None
            app.layout.focus(input_win)

    async def _task_poll() -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                tasks = await registry.task_backend.list()
                dicts = [t.to_dict() for t in tasks]
                active = [d for d in dicts if d["status"] not in ("done", "failed", "aborted")]
                task_cache["tree"] = _build_task_tree(active)
                task_cache["count"] = len(active)
            except Exception:
                logger.debug("task poll failed", exc_info=True)

    async def _intervention_poll() -> None:
        while True:
            await asyncio.sleep(0.15)
            try:
                _sync_region()
                app.invalidate()
            except Exception:
                logger.exception("inline: region poll failed")

    body = HSplit(
        [working, above_region_box, top_rule, inputrow, bottom_rule, status_win,
         dropdown]
    )
    # FloatContainer so the slash-command completions menu can float at the cursor
    # (typing `/` opens it; ↑↓ navigate, Tab/Enter accept — see _SlashCompleter).
    root = FloatContainer(
        content=body,
        floats=[Float(
            xcursor=True, ycursor=True,
            content=CompletionsMenu(max_height=8, scroll_offset=1),
        )],
    )
    app: Application = Application(
        layout=Layout(root, focused_element=input_win),
        key_bindings=kb,
        full_screen=False,
        refresh_interval=0.1,
    )
    # patch_stdout so stray stdout/stderr (e.g. library warnings) prints cleanly
    # above the live input region instead of corrupting it — mirrors the
    # PromptSession path. Renderer output (sys.__stdout__ via run_in_terminal in
    # _output_loop) is unaffected.
    poll_task = asyncio.create_task(_intervention_poll())
    task_poll_task = asyncio.create_task(_task_poll())
    try:
        with patch_stdout():
            await app.run_async()
    finally:
        poll_task.cancel()
        task_poll_task.cancel()


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
