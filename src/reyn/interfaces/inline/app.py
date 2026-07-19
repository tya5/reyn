"""Claude Code-style interactive input driver for the inline CUI.

A long-lived prompt_toolkit Application that drives input for the interactive
(TTY) inline renderer: a rule-bar sandwiched input, an animated working row, and
a navigable status bar (↓ to focus, ←→ to select a chip, enter to open a
read-only detail dropdown, ↑/esc to go back).

Integration: run_repl's `_output_loop` prints conversation output ABOVE this app
via `run_in_terminal` (the app stays a live region at the bottom); user input is
fed to the session via `submit_user_text`, so intervention answers / slash
commands / new turns route through the session exactly as the PromptSession path
did — the app never inspects the text.

`--cui` / non-TTY keep the existing PromptSession `_input_loop` (plain invariance).
The status bar reads live values through public sync accessors only; an
actionable model picker (selecting a class) is a follow-up.
"""
from __future__ import annotations

import asyncio
import logging
import re
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
from reyn.interfaces.repl.renderer import (
    _CC_ACCENT,
    _CC_COOL,
    _CC_DIM,
    _CC_DONE,
    _CC_WARN,
    _SPINNER,
)
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
        # Slash commands are inherently single-line; a "\n" means the input
        # (now multiline-capable, see run_inline_input's Buffer) has moved past
        # a bare command word, so stop suggesting — same intent as the
        # existing " " in text check, just extended to the newline case.
        if not text.startswith("/") or " " in text or "\n" in text:
            return
        prefix = text[1:]
        for name, summary in slash_command_completions(prefix):
            yield Completion(
                name, start_position=-len(prefix),
                display=f"/{name}", display_meta=summary,
            )


_SLASH_COMPLETER = _SlashCompleter()


@dataclass(frozen=True)
class _SkillCompletionCandidate:
    """Adapts a ``read_model.snapshot()["skills"]`` dict (``{"name": ...}``,
    see ``_extract_skills``) to the attribute shape
    ``reyn.interfaces.skill_invoke.skill_invoke_completions`` expects
    (mirrors ``SkillEntry``'s ``name``/``description``/``enabled``/
    ``visibility``), so the TUI completer reuses the SAME pure filtering
    function the invocation path's tests exercise, rather than
    re-implementing the menu/on_demand/hidden filter inline."""

    name: str
    description: str = ""
    enabled: bool = True
    visibility: str = "menu"


class _SkillInvokeCompleter(Completer):
    """Autocomplete the CURRENTLY-TYPED ``:name`` token (#3100 Axis 6, owner-
    mandated TUI completion, reusing this exact completion mechanism —
    ``_SlashCompleter`` above — for the separate ``:`` namespace).

    Only the LAST ``:name`` token (no following space) is a completion
    target: once a space follows a resolved ``:name``, we're past the point
    of completing IT — a further ``:name2`` (stacking, #3100 Axis 3) or
    trailing args are not name-completion targets. Reads skill names from
    the SAME ``read_model.snapshot()["skills"]`` list the status-bar "more"
    chip already displays (``_extract_skills``) — a config-derived name
    list, not visibility-filtered by the live per-session toggle; a stale/
    hidden suggestion here is a completion-UX nicety gap, not a security
    concern (``:name`` invocation itself still enforces the `menu`/
    `on_demand`/`hidden` surface, see ``Session._maybe_handle_skill_invoke``
    -> ``invocable_skill_names``).
    """

    _TOKEN_RE = re.compile(r"(?:^|\s):([A-Za-z0-9_-]*)$")

    def __init__(self, read_model) -> None:
        self._read_model = read_model

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text:
            return
        m = self._TOKEN_RE.search(text)
        if not m:
            return
        prefix = m.group(1)
        try:
            snap = self._read_model.snapshot() or {}
        except Exception:  # noqa: BLE001 — a completer must never crash input
            return
        from reyn.interfaces.skill_invoke import skill_invoke_completions
        candidates = [
            _SkillCompletionCandidate(name=str(s.get("name", "")))
            for s in (snap.get("skills") or [])
            if s.get("name")
        ]
        for name, summary in skill_invoke_completions(prefix, candidates):
            yield Completion(
                name, start_position=-len(prefix),
                display=f":{name}", display_meta=summary,
            )

# Maximum rows the above-input region (interventions, /rewind picker) may occupy.
# Capping prevents prompt_toolkit "Window too small" when the picker has more rows
# than the terminal height minus the fixed chrome (rules + input + status).
_ABOVE_REGION_MAX_HEIGHT = 12

# A closed-set intervention packs its choices onto one row when their combined
# width (labels + inter-item spacing) fits under this — comfortably inside any
# terminal width the app already assumes elsewhere (the 80-col rule chrome).
# Past this, above_region_frags falls back to a vertical stack instead of
# wrapping or truncating a label (file_access_choices' recursive option can
# run 100+ chars once a real path is interpolated — never safe to truncate a
# security-relevant path).
_IV_INLINE_MAX_WIDTH = 70

# Same cap, for the below-input status-bar dropdown (menu_region). The "…"
# overflow chip's panel can list one row per tool-visibility toggle (dozens of
# entries in a real session) — without this cap dropdown_height() requests an
# unbounded Dimension.exact(), which prompt_toolkit cannot satisfy past the
# terminal's remaining rows and renders "Window too small" instead of the menu.
_MENU_REGION_MAX_HEIGHT = 12

# Same cap, for the multiline input box itself. A fixed height=1 (the
# pre-multiline default) only ever showed the buffer's current line — once
# Shift+Enter/Ctrl+J can insert real newlines, a fixed height=1 window hides
# every line except the one the cursor is on (confirmed live via tmux: typing
# "line one", Shift+Enter, "line two" showed only "line two", though the
# newline itself DID insert correctly — a rendering gap, not a submit bug).
# Capped (not unbounded) for the same "Window too small" reason as the two
# constants above.
_INPUT_MAX_HEIGHT = 8


# Owner spec for the inline input: Enter=submit, Shift+Enter=newline — the
# OPPOSITE of prompt_toolkit's own multiline default (Enter=newline,
# Meta+Enter=submit; see run_inline_input's "enter" binding, which inverts it).
#
# Hard limit (confirmed via direct prompt_toolkit source read + an empirical
# tmux byte-probe, not guesswork): most terminals send an IDENTICAL "\r" for
# Enter and Shift+Enter — the classic VT100 protocol has no way to encode
# "Enter + Shift" as a distinct byte sequence, so this is a terminal-layer
# limit reyn's code cannot work around for those terminals (macOS
# Terminal.app, GNOME Terminal, default Windows Terminal, PuTTY, conhost).
#
# Some terminals DO send a disambiguated escape sequence when an extended
# keyboard protocol is active — critically, mintty (Windows Git Bash) sends
# the legacy xterm modifyOtherKeys form below BY DEFAULT since 2009.
# prompt_toolkit's OWN ansi_escape_sequences.py maps that sequence back to
# plain Keys.ControlM (its own comment: "currently unsupported, so just
# re-map... to the unmodified versions") — but the raw KeyPress.data still
# carries the full original escape string, so the distinction survives and can
# be recovered without any prompt_toolkit monkeypatch. Verified empirically: a
# byte-probe app confirmed `\x1b[27;2;13~` parses as ONE Keys.ControlM event
# whose .data is the full escape string, distinct from plain Enter's .data ==
# "\r". Ctrl+J (LF, a byte distinct from Enter's CR on every VT100-compatible
# terminal) is the guaranteed-always-works fallback for terminals where
# Shift+Enter is genuinely undetectable — see the "c-j" binding.
_SHIFT_ENTER_RAW_DATA = frozenset({
    "\x1b[27;2;13~",  # xterm modifyOtherKeys (mintty default) — Shift+Enter
})


def _is_shift_enter_escape(data: str) -> bool:
    """True if ``data`` (a KeyPress's raw ``.data``) is the disambiguated
    Shift+Enter escape sequence prompt_toolkit's own key-name resolution
    collapses to plain Enter. See ``_SHIFT_ENTER_RAW_DATA`` above for the full
    investigation."""
    return data in _SHIFT_ENTER_RAW_DATA


def _down_arrow_action(has_text: bool, cursor_row: int, line_count: int) -> str:
    """Pure decision for the multiline input buffer's ↓ key, mirroring
    ``Buffer.auto_down``'s row-awareness (this custom binding replaces
    prompt_toolkit's default auto_down entirely, so it must reproduce that
    part) plus reyn's own empty-box "drop to status bar" affordance.

    Returns one of ``"focus_status"`` / ``"cursor_down"`` / ``"history_forward"``.
    """
    if not has_text:
        return "focus_status"
    if cursor_row < line_count - 1:
        return "cursor_down"
    return "history_forward"


def _input_window_height(line_count: int) -> int:
    """Pure: how many rows the input Window should occupy for a buffer with
    ``line_count`` lines — at least 1, capped at ``_INPUT_MAX_HEIGHT`` (see its
    own docstring for why a fixed height=1 broke multiline display)."""
    return min(max(1, line_count), _INPUT_MAX_HEIGHT)


def _picker_hint(has_picker_focus: bool, key: str | None) -> str:
    """Return the status-bar hint for the above-region picker state.

    Pure function so the focus-dependent and hint-selection logic are
    separately testable: the caller resolves ``has_picker_focus`` from
    ``get_app().layout.has_focus()`` and passes the result in.
    """
    if not has_picker_focus:
        return "  [↓ menu · ↑ history · /quit]"
    if key and key.startswith("iv:"):
        return "  [↑↓ select · enter confirm]"
    return "  [↑↓ select · enter · esc cancel]"


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
    # Colour for the chip's (bold) value text; the label stays dim. Per-chip so the
    # eye separates them at a glance.
    value_color: str = _CC_DIM


def _model_expansion(snap, dispatch):
    classes = list(snap.get("model_classes") or [])
    if not classes:
        return DetailElement(lambda: [f"current: {snap['model']}", "change with /model"])
    # Use the pre-resolved class name: when no override is set, snap["model"] is
    # the full LiteLLM model ID (e.g. "claude-opus-4-8") which never matches any
    # class name ("opus") → the ▸ marker never appeared. active_model_class()
    # reverse-looks up the class so the active entry is always highlighted.
    active = snap.get("model_active_class") or snap["model"]
    rows = [f"▸ {c}" if c == active else f"  {c}" for c in classes]
    return CommandUIElement(rows, [f"/model {c}" for c in classes], dispatch)


def _cache_hit_line(label: str, cached: int, prompt: int, *, note: str = "") -> str:
    """One "cache X% hit (a / b prompt tokens)" line, label padded to the same
    9-char column every other cost/ctx dropdown line uses (was misaligned
    when the label itself carried the qualifier, e.g. "cache (cumulative)")."""
    pct = round(100 * cached / prompt) if prompt > 0 else 0
    tail = f", {note}" if note else ""
    return f"{label:<9}{pct}% hit ({cached:,} / {prompt:,} prompt tokens{tail})"


# Cost-panel breakdown (#cost-panel-breakdown): the >200k tiered-pricing guard
# tolerance. estimate_cost_breakdown() does not replicate litellm's >200k
# tiered rates (see its docstring), so the 4 components' sum can legitimately
# diverge from the litellm-accurate Total at very high token volumes. A pure
# floating-point rounding residual from summing many small per-call floats is
# NOT the same thing as tiered pricing kicking in — the relative tolerance
# below absorbs float noise while still catching a real tiered-rate mismatch
# (which is typically a multi-percent divergence, not a rounding-error one).
_COST_BREAKDOWN_EPSILON_ABS = 1e-6
_COST_BREAKDOWN_EPSILON_REL = 1e-4


def _cost_scope_state(breakdown, authoritative_total: float) -> "tuple[float, float, float, float, str]":
    """One scope column's (input_cost, output_cost, saved, saved_pct, state).

    ``input_cost`` = the cache-aware cost actually paid for input (prompt +
    cache-read + cache-creation components). ``saved_pct`` = Saved /
    (Input + Saved) — the no-cache-baseline denominator (what input would have
    cost WITHOUT caching), NOT Saved / Total (falsified explicitly: pinning
    the wrong denominator would silently under/over-state the savings %).
    Divide-by-zero guarded (0% when Input+Saved == 0, i.e. no priced input
    tokens recorded yet).

    ``state`` is one of three cases — the panel renders each distinctly so it
    never MISATTRIBUTES a cause:
      - ``"ok"``      — the 4 components reconcile with the authoritative Total
                        (within float-noise tolerance): show exact numbers.
      - ``"approx"``  — components are present (sum > 0) but diverge from Total
                        beyond tolerance = genuine >200k TIERED pricing, which
                        ``estimate_cost_breakdown`` does not replicate: mark
                        the component rows "~" + a tiered-pricing footnote.
      - ``"unavail"`` — components are ~0 while Total > 0 = the breakdown is
                        UNAVAILABLE, not diverging (the durable per-agent Total
                        survives a restart via the ledger, but the in-memory
                        CostBreakdown resets to 0 — it is NOT ledger-persisted;
                        it can also be 0 before the first accumulation). This
                        is NOT tiered pricing, so it must NOT fire the "~"/
                        tiered footnote (the false-fire the architect caught);
                        the Total stays authoritative and the component cells
                        blank to "—" with a distinct "unavailable" note.
    """
    input_cost = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    output_cost = breakdown.completion_cost
    saved = breakdown.cache_savings
    no_cache_baseline = input_cost + saved
    saved_pct = (saved / no_cache_baseline) if no_cache_baseline > 0 else 0.0

    component_sum = input_cost + output_cost
    tol = max(_COST_BREAKDOWN_EPSILON_ABS, abs(authoritative_total) * _COST_BREAKDOWN_EPSILON_REL)
    if component_sum <= _COST_BREAKDOWN_EPSILON_ABS and authoritative_total > tol:
        # Breakdown absent while a real Total exists → unavailable, not tiered.
        state = "unavail"
    elif abs(component_sum - authoritative_total) > tol:
        # Components present but don't reconcile → genuine >200k tiered pricing.
        state = "approx"
    else:
        state = "ok"
    return input_cost, output_cost, saved, saved_pct, state


def _cost_breakdown_table(snap) -> list[str]:
    """The 5-row (Total/Input/Output/Saved/Saved%) x 3-column
    (Session/Agent/Project) cost-panel breakdown table.

    Total is always the litellm-accurate authoritative figure (``cost_usd`` /
    ``cost_agent`` / ``cost_total`` — already computed via ``estimate_cost``,
    unaffected by the >200k breakdown limitation). Input/Output/Saved/Saved%
    are derived from the accumulated ``CostBreakdown`` per scope. Per-scope
    ``state`` (see ``_cost_scope_state``) decides how the component cells
    render: exact ("ok"), "~"-marked with a tiered-pricing footnote ("approx"
    = genuine >200k tiering), or "—" with an "unavailable" note ("unavail" =
    breakdown reset post-restart / not-yet-accumulated — NEVER misattributed
    to tiered pricing).
    """
    from reyn.llm.pricing import CostBreakdown

    scopes = [
        ("Ses", snap.get("cost_breakdown_session", CostBreakdown()), snap["cost_usd"]),
        ("Agt", snap.get("cost_breakdown_agent", CostBreakdown()), snap.get("cost_agent", snap["cost_usd"])),
        ("Prj", snap.get("cost_breakdown_project", CostBreakdown()), snap.get("cost_total", snap["cost_usd"])),
    ]
    col_w = 9
    header = "COST" + "".join(f"{name:>{col_w}}" for name, _, _ in scopes)

    per_scope = [
        (name, total, *_cost_scope_state(breakdown, total))
        for name, breakdown, total in scopes
    ]
    any_approx = any(state == "approx" for *_rest, state in per_scope)
    any_unavail = any(state == "unavail" for *_rest, state in per_scope)

    total_row = "Total" + "".join(f"{'$' + format(total, '.4f'):>{col_w}}" for _, total, *_ in per_scope)

    def _cell(value: float, state: str) -> str:
        if state == "unavail":
            return "—"
        s = f"${value:.4f}"
        return ("~" + s)[:col_w] if state == "approx" else s

    input_row = "Input" + "".join(
        f"{_cell(inp, state):>{col_w}}" for _, _, inp, _out, _sav, _pct, state in per_scope
    )
    output_row = "Output" + "".join(
        f"{_cell(out, state):>{col_w}}" for _, _, _inp, out, _sav, _pct, state in per_scope
    )
    saved_row = "Saved" + "".join(
        f"{_cell(sav, state):>{col_w}}" for _, _, _inp, _out, sav, _pct, state in per_scope
    )
    pct_row = "Saved%" + "".join(
        ("—".rjust(col_w) if state == "unavail" else f"{round(100 * pct)}%".rjust(col_w))
        for _, _, _inp, _out, _sav, pct, state in per_scope
    )

    rows = [header, total_row, input_row, output_row, saved_row, pct_row]
    if any_approx:
        rows.append("~ approx at high volume (>200k tiered pricing)")
    if any_unavail:
        rows.append("— breakdown unavailable this session (Total is exact)")
    return rows


def _cost_expansion(snap, dispatch):
    def lines():
        p, c, t = snap["usage"]
        agent_t = snap.get("agent_tokens", t)
        cached = snap.get("session_cached_tokens", 0)
        return [
            *_cost_breakdown_table(snap),
            f"tokens   prompt {p} · completion {c} · total {agent_t}",
            _cache_hit_line("cache", cached, p, note="cumulative"),
        ]
    return DetailElement(lines)


def _ctx_pct(snap) -> str:
    window = snap.get("ctx_window", 0)
    used = snap.get("ctx_used", 0)
    # used <= 0 means no LLM call has completed yet this session — show "—"
    # rather than a misleading "0%" (a real completed call's prompt_tokens is
    # never actually 0; the system prompt alone is nonzero).
    if window <= 0 or used <= 0:
        return "—"
    return f"{round(100 * used / window)}%"


def _ctx_expansion(snap, dispatch):
    # Current-state only (this is the ctx chip's whole reason to exist —
    # cumulative figures live in the cost chip instead, see _cost_expansion).
    #
    # Two DISTINCT figures, kept visually separated so they don't collapse
    # back into one ambiguous number:
    #   - prompt/window/free/cache: the REAL last-call size against the
    #     model's REAL context limit — "how close to the hard limit".
    #   - compaction: the compaction subsystem's OWN lightweight estimate
    #     (history only, excl. system prompt/tools) against ITS internal
    #     trigger threshold — "when will auto-compaction fire". A smaller,
    #     already-adjusted number; not comparable to the block above.
    def lines():
        window = snap.get("ctx_window", 0)
        prompt_tokens = snap.get("ctx_used", 0)
        free = max(0, window - prompt_tokens)
        pct = round(100 * prompt_tokens / window) if window > 0 else 0
        recent_prompt, recent_cached = snap.get("ctx_recent_usage", (0, 0))
        # Lazy: only computed while this dropdown is actually open (see
        # _snapshot's comment on why context_window_status() must not run on
        # every render frame).
        status_fn = snap.get("ctx_compaction_status_fn")
        status = status_fn() if status_fn is not None else {}
        comp_trigger = status.get("effective_trigger", 0)
        comp_est = max(0, comp_trigger - status.get("free_window", 0))
        comp_pct = round(100 * comp_est / comp_trigger) if comp_trigger > 0 else 0
        return [
            f"window       {window:,} tokens  ({snap.get('ctx_source', 'unknown')})",
            f"prompt       {prompt_tokens:,} tokens  ({pct}% of window)",
            f"free         {free:,} tokens",
            _cache_hit_line("cache", recent_cached, recent_prompt),
            f"compaction   {comp_est:,} / {comp_trigger:,} tokens est.  ({comp_pct}% to trigger)",
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


def _task_rows(nodes: list[dict], depth: int) -> list[str]:
    out = []
    for node in nodes:
        out.append(f"{'  ' * depth}{node['status']}  {node['name']}")
        out.extend(_task_rows(node["children"], depth + 1))
    return out


def _task_expansion(snap, dispatch):
    # Phase 3: task tree. Depth-first indented rows (2 spaces per depth).
    # NOTE: tree is a snapshot captured at call time — callers that need live
    # updates (e.g. the open dropdown) should substitute a live-reading element.
    tree = snap.get("task_tree") or []
    if not tree:
        return DetailElement(lambda: ["(no active tasks)"])
    return DetailElement(lambda: _task_rows(tree, 0))


def _visibility_items_by_kind(snap, kind: str) -> list[dict]:
    """The session-backed visibility toggle items for one kind (tool/mcp/skill/…)."""
    return [it for it in (snap.get("visibility_items") or []) if it.get("kind") == kind]


def _toggle_category_expansion(snap, dispatch, kind: str, fallback_key: "str | None"):
    """A togglable category's dropdown: CommandUIElement rows when the session
    has wired visibility state for ``kind``, else a read-only fallback listing
    (``snap[fallback_key]``, name-only — no toggle state available). ``None``
    fallback_key (tool has no config-declared name source) shows "(none)".

    Shared by the tool / mcp / skill sub-bar categories (#2285's
    /visibility on|off <kind> <name> dispatch, unchanged)."""
    items = _visibility_items_by_kind(snap, kind)
    if items:
        rows = [f"[{'on' if it['on'] else 'off'}] {it['name']}" for it in items]
        cmds = [
            f"/visibility {'off' if it['on'] else 'on'} {kind} {it['name']}"
            for it in items
        ]
        return CommandUIElement(rows, cmds, dispatch)
    names = [d["name"] for d in (snap.get(fallback_key) or [])] if fallback_key else []
    lines = [f"{n}" for n in names] or ["(none)"]
    return DetailElement(lambda ls=lines: ls)


def _tool_category_expansion(snap, dispatch):
    return _toggle_category_expansion(snap, dispatch, "tool", None)


def _mcp_category_expansion(snap, dispatch):
    return _toggle_category_expansion(snap, dispatch, "mcp", "mcp_servers")


def _skill_category_expansion(snap, dispatch):
    return _toggle_category_expansion(snap, dispatch, "skill", "skills")


def _hook_category_expansion(snap, dispatch):
    """Hook applicability toggles: CommandUIElement rows when the session has
    wired hook_items, else a read-only config-derived fallback listing."""
    items = snap.get("hook_items") or []
    if items:
        rows = [
            f"[{'on' if h['on'] else 'off'}] {h['name']}"
            + (f"  · {h['scope']}" if h.get("scope") else "")
            for h in items
        ]
        cmds = [f"/hook {'off' if h['on'] else 'on'} {h['name']}" for h in items]
        return CommandUIElement(rows, cmds, dispatch)
    hooks = snap.get("hooks") or []
    lines = [f"{h['label']}" for h in hooks] or ["(none)"]
    return DetailElement(lambda ls=lines: ls)


def _pipe_category_expansion(snap, dispatch):
    """Registered pipelines: always read-only (no on/off toggle mechanism —
    explicitly out of scope for this slice, unlike tool/mcp/skill/hook)."""
    pipelines = snap.get("pipelines") or []
    lines = [
        f"{p['name']}  {p['description']}" if p.get("description") else f"{p['name']}"
        for p in pipelines
    ] or ["(none)"]
    return DetailElement(lambda ls=lines: ls)


def _cron_category_expansion(snap, dispatch):
    """Cron jobs: always read-only (no on/off toggle mechanism — explicitly
    out of scope for this slice, unlike tool/mcp/skill/hook)."""
    cron_jobs = snap.get("cron_jobs") or []
    lines = [
        f"[{'on' if j.get('enabled') else 'off'}] {j['name']}  {j['schedule']}"
        for j in cron_jobs
    ] or ["(none)"]
    return DetailElement(lambda ls=lines: ls)


def _mcp_count(snap) -> int:
    items = _visibility_items_by_kind(snap, "mcp")
    return len(items) if items else len(snap.get("mcp_servers") or [])


def _skill_count(snap) -> int:
    items = _visibility_items_by_kind(snap, "skill")
    return len(items) if items else len(snap.get("skills") or [])


def _hook_count(snap) -> int:
    items = snap.get("hook_items") or []
    return len(items) if items else len(snap.get("hooks") or [])


_MORE_SUB_CHIP_SPECS = [
    ChipSpec("tool",  "tool",  lambda s: str(len(_visibility_items_by_kind(s, "tool"))),
             _tool_category_expansion),
    ChipSpec("mcp",   "mcp",   lambda s: str(_mcp_count(s)), _mcp_category_expansion),
    ChipSpec("skill", "skill", lambda s: str(_skill_count(s)), _skill_category_expansion),
    ChipSpec("pipe",  "pipe",  lambda s: str(len(s.get("pipelines") or [])),
             _pipe_category_expansion),
    ChipSpec("hook",  "hook",  lambda s: str(_hook_count(s)), _hook_category_expansion),
    ChipSpec("cron",  "cron",  lambda s: str(len(s.get("cron_jobs") or [])),
             _cron_category_expansion),
]


_CHIP_SPECS = [
    ChipSpec("model", "model", lambda s: str(s["model"]), _model_expansion,
             value_color=_CC_ACCENT),
    ChipSpec("agent", "agent", lambda s: str(s["attached_name"] or "—"), _agent_expansion,
             value_color=_CC_COOL),
    ChipSpec("task",  "task",  lambda s: str(s.get("task_count", 0)), _task_expansion,
             value_color=_CC_WARN),
    ChipSpec("cost",  "cost",  lambda s: f"${s['cost_agent']:.4f}", _cost_expansion,
             value_color=_CC_DONE),
    ChipSpec("ctx",   "ctx",   _ctx_pct, _ctx_expansion,
             value_color=_CC_COOL),
    # "more" has no `expansion` — Enter on it opens the level-2 sub-bar
    # (_MORE_SUB_CHIP_SPECS) instead of a menu_region dropdown directly; see
    # _is_more()/_sub_bar_visible() in run_inline_input.
    ChipSpec("more",  "",      lambda s: "…", None,
             value_color=_CC_DIM),
]


@dataclass(frozen=True)
class WaitingOn:
    """What the current turn is blocked on, for the working indicator.

    Owner: "Working… もっと状態細分化できないの?" — the intent was knowing WHAT
    is blocking progress (a slow tool? the model? a question only the user can
    answer?), not just "is something happening". Table-driven (see
    ``_WAITING_ON_BY_EVENT`` below) rather than growing an if/elif chain in the
    renderer — a new axis is one new table entry, not a new branch.

    Every ``turn`` in reyn funnels through exactly THREE await chokepoints
    (verified by reading the actual dispatch code, not assumed):
    ``call_llm_tools`` (the LLM call — the default/idle state below),
    ``dispatch_tool`` (any tool execution — sub-agent delegation / MCP / shell
    / web all go through this same call, and #2344's owner design decision
    made chat-axis tool_calls run SERIALLY in declaration order, so a single
    ``detail`` slot is correct, never a set), and ``intervention_bus.request``
    (ANY human-in-the-loop pause — ask_user, permission confirm, cost-warn,
    safety-limit checkpoint, MCP install confirm, elicitation, hook confirm
    all fan into this one primitive). This dataclass models exactly those
    three plus "reached via a mid-turn compaction pass" as a fourth, all
    optional/extensible via the table.
    """

    label: str
    detail: "str | None" = None
    # True → render as a static amber line matching the above-input
    # intervention region's visual weight, NOT the "the AI is busy" shimmer —
    # the ball is in the user's court, not the model's, and the shimmer
    # animation was actively misleading here (owner's original complaint was
    # exactly this: the spinner kept ticking through an ask_user pause).
    is_user_wait: bool = False

    def text(self) -> str:
        return f"{self.label} {self.detail}" if self.detail else self.label


_WAITING_ON_THINKING = WaitingOn(label="Thinking")  # default: LLM response in flight
_WAITING_ON_FOR_USER = WaitingOn(label="Waiting for you", is_user_wait=True)

# Event → WaitingOn transition table. tool_called's data is
# {caller_kind, caller_id, tool, chain_id, args, args_hash} (dispatch/dispatcher.py
# via lifecycle_forwarder.py's on_tool_called/on_tool_returned/on_tool_failed
# — the SAME events the scrollback's "▸ tool(...)"/"⎿ ..." trace lines already
# come from). Extending to a new axis (e.g. compaction, once desired — the
# compaction_check(outcome="forced_sync", candidate_count>0) / completed /
# failed events already exist and could bracket a "Compacting" state) is one
# new entry here, not a new branch in the renderer.
_WAITING_ON_BY_EVENT: "dict[str, Callable[[dict], WaitingOn]]" = {
    "tool_called": lambda d: WaitingOn(label="Running", detail=d.get("tool")),
    "tool_returned": lambda d: _WAITING_ON_THINKING,
    "tool_failed": lambda d: _WAITING_ON_THINKING,
}


def is_intervention_region_key(key: "str | None") -> bool:
    """True when a region_holder key names a closed-set intervention
    ("iv:<id>", stamped by _sync_region) rather than a command-UI picker
    (e.g. "cmd:..." for /rewind) or nothing (None). Pure — the region-key
    naming convention lives here so it's one importable fact, not duplicated
    string-prefix logic at each call site."""
    return bool(key) and key.startswith("iv:")


def iv_choices_fit_one_row(labels: list[str], max_width: int = _IV_INLINE_MAX_WIDTH) -> bool:
    """A closed-set intervention's choices render on ONE inline row when their
    combined width (labels + a 2-char gap between each) fits under
    *max_width*. Longer or variable-length label sets (file_access_choices'
    recursive option interpolates a real filesystem path — measured, 100+
    chars is routine) fall back to a vertical stack instead of wrapping or
    truncating a security-relevant path. Pure — no prompt_toolkit, no region
    state; the closures in run_inline_input pass in what they already have."""
    if not labels:
        return False
    width = sum(len(label) for label in labels) + 2 * (len(labels) - 1)
    return width <= max_width


def working_line(
    thinking: bool,
    think_start: float,
    now: float,
    *,
    cancelling: bool = False,
    waiting_on: "WaitingOn | None" = None,
    waiting_on_since: "float | None" = None,
) -> list:
    """Pure: working-row fragments while a turn runs (empty list when idle).

    The spinner frame derives from `now` so it advances smoothly regardless of
    refresh jitter. The label carries a shimmer — a bright crest sweeping
    left→right across the text (a moving light) over a dim base, also
    clock-driven so it animates on each refresh.

    ``waiting_on`` (default ``None`` → the "Thinking" default, byte-identical
    to every pre-existing caller/test) names WHAT is currently blocking
    progress — see ``WaitingOn``'s docstring. ``waiting_on_since`` is when
    THAT state began (defaults to ``think_start``, i.e. turn start, if not
    given) — elapsed seconds shown is time-in-THIS-state, not turn-total, so
    "Running grep_files… 45s" answers "where exactly is it stuck", not just
    "the turn has been going for a while".

    When ``cancelling=True`` (ctrl-c was pressed mid-turn), the shimmer is replaced
    by a static "Cancelling…" indicator — the cancel is cooperative so the turn
    completes at the next tool boundary; the indicator reassures the user it's noted.
    Takes priority over ``waiting_on`` (a cancel-in-progress is the one thing
    that always wins, regardless of what the turn happened to be doing).
    """
    if not thinking:
        return []
    if cancelling:
        return [(f"fg:{_CC_WARN}", " ✗ Cancelling…")]
    wo = waiting_on if waiting_on is not None else _WAITING_ON_THINKING
    since = waiting_on_since if waiting_on_since is not None else think_start
    elapsed = max(0, int(now - since))
    label = f"{wo.text()}… {elapsed}s"
    if wo.is_user_wait:
        return [(f"fg:{_CC_WARN}", f" ◆ {label} · ctrl-c to interrupt")]
    frame = _SPINNER[int(now * 8) % len(_SPINNER)]
    frags = [(f"fg:{_CC_ACCENT}", f" {frame} ")]
    # The crest sweeps across the label then pauses in a short trailing gap before
    # restarting, so the light reads as a repeating left→right pass.
    head = int(now * 16) % (len(label) + 6)
    for i, ch in enumerate(label):
        offset = head - i
        if offset == 0:
            frags.append((f"fg:{_CC_ACCENT} bold", ch))   # bright crest
        elif offset == 1:
            frags.append((f"fg:{_CC_ACCENT}", ch))         # trailing glow
        else:
            frags.append((f"fg:{_CC_DIM}", ch))            # dim base
    frags.append((f"fg:{_CC_DIM}", " · ctrl-c to interrupt"))
    return frags


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


def _extract_skills(config) -> list[dict]:
    """Extract skill name dicts from config. Returns [] on any missing/malformed section.

    Mirrors ``_extract_mcp_servers`` — the config-only fallback shown when the
    session hasn't wired ``visibility_items`` for kind="skill" yet."""
    skills = getattr(config, "skills", None)
    if not isinstance(skills, dict):
        return []
    entries = skills.get("entries")
    if not isinstance(entries, dict):
        return []
    return [{"name": name} for name in entries]


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


def _session_visibility_items(session) -> list[dict]:
    """Read visibility toggle state from the session (#2285 backend seam).

    Returns [] until e2e lands ``capability_visibility_state`` on the Session.
    Shape when available: [{kind, name, on}, ...] where on = not hidden_by_session.
    """
    getter = getattr(session, "capability_visibility_state", None)
    if getter is None:
        return []
    try:
        state = getter()
        authorized = state.get("authorized") or []
        hidden = {(h["kind"], h["name"]) for h in (state.get("hidden_by_session") or [])}
        return [
            {"kind": a["kind"], "name": a["name"], "on": (a["kind"], a["name"]) not in hidden}
            for a in authorized
        ]
    except Exception:  # noqa: BLE001
        logger.warning("capability_visibility_state() raised; visibility panel degraded to []", exc_info=True)
        return []


def _session_hook_items(session) -> list[dict]:
    """Read hook applicability state from the session (#2285 backend seam).

    Returns [] until e2e lands ``hook_state`` on the Session.
    Shape when available: [{name, scope, on}, ...].
    """
    getter = getattr(session, "hook_state", None)
    if getter is None:
        return []
    try:
        return [
            {"name": h["name"], "scope": h.get("scope", ""), "on": h.get("enabled", True)}
            for h in (getter() or [])
        ]
    except Exception:  # noqa: BLE001
        logger.warning("hook_state() raised; hooks panel degraded to []", exc_info=True)
        return []


def _session_pipelines(session) -> list[dict]:
    """Read registered pipeline names + descriptions from the session's
    PipelineRegistry — always constructed at Session.__init__ (never a "not
    wired yet" seam like visibility_items/hook_items); the try/except is
    defensive against an unexpected attribute-shape drift, not a feature gate.
    Shape: [{name, description}, ...]."""
    getter = getattr(session, "pipeline_registry", None)
    if getter is None:
        return []
    try:
        return [{"name": name, "description": desc} for name, desc in getter.entries()]
    except Exception:  # noqa: BLE001
        logger.warning("pipeline_registry.entries() raised; pipe panel degraded to []", exc_info=True)
        return []


def _snapshot(registry, task_cache=None, config=None):
    """Read live status values off the attached session via sync accessors."""
    s = registry.attached_session()
    if s is None:
        return None
    u = s.total_usage
    # Cost breakdown (all via registry.agent_cost_usd — the single source of
    # truth for per-agent cost aggregation across all sids).
    cost_total = sum(registry.agent_cost_usd(name) for name in registry.loaded_names())
    cost_agent = (
        registry.agent_cost_usd(registry.attached_name)
        if registry.attached_name else s.total_cost_usd
    )
    agent_tokens = (
        registry.agent_tokens(registry.attached_name)
        if registry.attached_name else u.total_tokens
    )
    # Headline figure: the single most recent LLM call's prompt_tokens against
    # the model's REAL context window (get_max_input_tokens) — "how close to
    # the model's hard limit am I", matching the Claude Code-style % owners
    # expect. last_call_usage (NOT total_usage or a turn-summed figure) — a
    # turn can make several LLM calls via tool-loop iterations, each re-
    # sending nearly the same growing context, so summing them would wildly
    # overstate current occupancy. raw_context_window() is a cheap dict
    # lookup, safe to call every render frame (_snapshot runs on every frame).
    raw_window = s.raw_context_window()
    ctx_window = raw_window["window"]
    ctx_source = raw_window["source"]
    recent = s.last_call_usage
    ctx_used = recent.prompt_tokens
    # Supplementary figure: the compaction subsystem's OWN lightweight estimate
    # (history only, excl. system prompt/tools) against ITS internal trigger
    # threshold (already SP/head/tail-adjusted, not the model's real window) —
    # answers "when will auto-compaction fire", a different question from the
    # headline one above. Keeping both avoids collapsing two distinct
    # measurements into one ambiguous number (the original "used" bug).
    #
    # UNLIKE raw_context_window, Session.context_window_status() is NOT cheap
    # (json.dumps + token-estimate of the full router-view history) — do not
    # call it eagerly here, since _snapshot() runs on every render frame
    # regardless of whether the ctx dropdown is even open. Store the bound
    # method itself; _ctx_expansion's lines() calls it lazily, only while the
    # dropdown is actually open (and only once per redraw of THAT dropdown,
    # not the whole app).
    ctx_compaction_status_fn = s.context_window_status
    return {
        "model": s.model,
        "model_active_class": s.active_model_class(),
        "model_classes": list(s.known_model_classes()),
        "agent_names": list(registry.loaded_names()),
        "attached_name": registry.attached_name,
        "session_tree": registry.session_tree(),
        "usage": (u.prompt_tokens, u.completion_tokens, u.total_tokens),
        "cost_usd": s.total_cost_usd,
        "cost_total": cost_total,
        "cost_agent": cost_agent,
        "agent_tokens": agent_tokens,
        # Cost-panel breakdown (#cost-panel-breakdown): per-scope CostBreakdown
        # (Input/Output/Saved/Saved% rows) mirroring the 3 $ totals above.
        "cost_breakdown_session": s.total_cost_breakdown,
        "cost_breakdown_agent": (
            registry.agent_cost_breakdown(registry.attached_name)
            if registry.attached_name else s.total_cost_breakdown
        ),
        "cost_breakdown_project": registry.project_cost_breakdown(),
        "ctx_used": ctx_used,
        "ctx_window": ctx_window,
        "ctx_source": ctx_source,
        "session_cached_tokens": u.cached_tokens,
        "ctx_recent_usage": (recent.prompt_tokens, recent.cached_tokens),
        "ctx_compaction_status_fn": ctx_compaction_status_fn,
        "task_count": task_cache["count"] if task_cache else 0,
        "task_tree": task_cache["tree"] if task_cache else [],
        "cron_jobs": _extract_cron_jobs(config) if config is not None else [],
        "mcp_servers": _extract_mcp_servers(config) if config is not None else [],
        "hooks": _extract_hooks(config) if config is not None else [],
        "skills": _extract_skills(config) if config is not None else [],
        # #2285: session-scoped capability visibility + hook applicability toggles.
        # Populated once e2e lands the backend; graceful fallback to [] until then.
        "visibility_items": _session_visibility_items(s),
        "hook_items": _session_hook_items(s),
        # Always available (Session owns a PipelineRegistry from __init__) —
        # not a "not wired yet" seam like the two lines above.
        "pipelines": _session_pipelines(s),
    }


async def run_inline_input(read_model, renderer, config=None, transport=None) -> None:
    """Run the interactive inline input Application until the user quits.

    Returns on quit (Ctrl-C/D/Q or /quit /exit) so the driver can tear down (cost
    summary) via its FIRST_COMPLETED wait.

    ``config`` is the loaded ReynConfig (or None), threaded read-only into the
    status snapshot so the ``…`` overflow chip can list cron jobs / mcp servers /
    hooks. When None (--cui / non-inline path) the overflow panel shows empty
    sections — backward-compatible.

    ``read_model`` is the :class:`~reyn.interfaces.repl.read_model.ChatReadModel`
    (ADR-0039 P3) this driver READS all of its status/region/task state through —
    the status snapshot, the intervention-region head, the ``/rewind`` command-UI,
    the task poll, and the input-history path. A :class:`RegistryReadModel` (local)
    reads them off the attached session; a :class:`RemoteReadModel` reads the
    server's ``STATE_*`` status view over the wire. This is what removed the last
    local-only coupling, so the inline CUI now renders on ``reyn chat --connect``.

    ``transport`` is the :class:`~reyn.interfaces.transport.client_transport.ClientTransport`
    (ADR-0039 P1) this driver's WRITE side routes through — turn submit,
    intervention answers, the user echo, cancel, and shutdown all go through the
    transport's send seam.
    """
    history = FileHistory(str(read_model.history_path))
    # multiline=True: Enter=submit / Shift+Enter=newline (owner spec) instead of
    # prompt_toolkit's own multiline default (Enter=newline, Meta+Enter=submit) —
    # see the "enter" binding below, which inverts it. FileHistory already
    # round-trips multi-line entries natively ("+"-prefixed continuation lines,
    # history.py:297-306) — no reyn-side change needed there.
    # #3100 Axis 6: merge the `/` slash completer with a per-app `:` skill
    # completer (the latter needs a live `read_model` reference, so it can't
    # be a module-level singleton like `_SLASH_COMPLETER`).
    from prompt_toolkit.completion import merge_completers
    buf = Buffer(
        multiline=True, history=history,
        completer=merge_completers([_SLASH_COMPLETER, _SkillInvokeCompleter(read_model)]),
        complete_while_typing=True,
    )
    # sel: which main chip; open: its detail/picker shown. The dropdown's
    # selectable cursor lives in `menu_region` (the below-input Region hosting
    # the opened chip's element), not here — the same selection mechanism as
    # the above-input region (interventions / the /rewind picker), unified in F5.
    #
    # The "…" ("more") chip is a special 2-level case: opening it (open=True)
    # shows a SUB-status-bar row (_MORE_SUB_CHIP_SPECS: tool/mcp/skill/pipe/
    # hook/cron) instead of a menu_region dropdown directly. sub_sel is which
    # sub-chip is highlighted; cat_open is whether THAT sub-chip's own
    # menu_region dropdown is showing (level 2). For every other main chip,
    # sub_sel/cat_open are unused — open=True goes straight to its dropdown,
    # unchanged from before this 2-level "more" redesign.
    menu = {"sel": 0, "open": False, "sub_sel": 0, "cat_open": False}
    # Async-polled task cache: updated every ~1 s by _task_poll; read by
    # _snapshot so the status bar and dropdown reflect live active tasks.
    task_cache: dict = {"tree": [], "count": 0}
    # Below-input region: hosts the opened chip's dropdown as a region element —
    # a CommandUIElement model picker (selectable) or a read-only DetailElement
    # (live detail, no cursor). Empty (cleared) while the menu is closed.
    menu_region = Region()
    menu_region.set_max_visible(_MENU_REGION_MAX_HEIGHT)
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
    above_region.set_max_visible(_ABOVE_REGION_MAX_HEIGHT)
    # What the region is currently showing: "iv:<id>" (intervention) or
    # "cmd:<id>" (command-UI) or None. Lets the poll skip rebuilding each tick.
    region_holder: dict = {"key": None}

    def _current_spec() -> ChipSpec:
        return _CHIP_SPECS[menu["sel"]]

    def _is_more() -> bool:
        return _current_spec().key == "more"

    def _sub_bar_visible() -> bool:
        # The "more" chip's level-1 sub-bar shows once open, until its own
        # category (level 2) is opened.
        return menu["open"] and _is_more() and not menu["cat_open"]

    def _dropdown_visible() -> bool:
        # Whether menu_region/dropdown should currently render: every other
        # chip shows it as soon as it's open (unchanged); "more" only shows it
        # once a sub-bar category has been entered (cat_open).
        if not menu["open"]:
            return False
        return menu["cat_open"] if _is_more() else True

    def _working_frags() -> list:
        wf = getattr(renderer, "working_frags", None)
        if callable(wf):
            return wf(time.monotonic())
        return []

    working = ConditionalContainer(
        Window(FormattedTextControl(_working_frags), height=1),
        filter=Condition(lambda: getattr(renderer, "_thinking", False)),
    )
    top_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    bottom_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    prompt_sym = Window(
        FormattedTextControl([(f"fg:{_CC_ACCENT} bold", "❯ ")]), width=2, height=1
    )
    input_win = Window(
        BufferControl(buffer=buf),
        height=lambda: _input_window_height(buf.document.line_count),
    )
    inputrow = VSplit([prompt_sym, input_win])

    def status_fragments() -> list:
        snap = read_model.snapshot(task_cache, config)
        if snap is None:
            return [(f"fg:{_CC_DIM}", " /quit to exit · ↑ history")]
        focused = get_app().layout.has_focus(status_win)
        frags: list = []
        for i, spec in enumerate(_CHIP_SPECS):
            val = spec.value(snap)
            selected = focused and i == menu["sel"]
            if selected:
                # the focused chip is reverse-highlighted as one block.
                mark = " ▾" if menu["open"] else ""
                label = f" {spec.label} " if spec.label else " "
                frags.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f"{label}{val}{mark} "))
            else:
                # label stays dim; the value is bold in the chip's own colour, so
                # the eye separates the chips at a glance.
                frags.append((f"fg:{_CC_DIM}", f" {spec.label} " if spec.label else " "))
                frags.append((f"fg:{spec.value_color} bold", val))
                frags.append((f"fg:{_CC_DIM}", " "))
            if i < len(_CHIP_SPECS) - 1:
                frags.append((f"fg:{_CC_DIM}", "│"))
        if not focused:
            hint = _picker_hint(
                get_app().layout.has_focus(above_region_win),
                region_holder["key"],
            )
        elif _is_more() and menu["open"]:
            # Detailed navigation hint lives on the sub-bar row itself
            # (sub_status_fragments) — the main bar just shows a generic close.
            hint = "  [esc / ↑ close]"
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

    def sub_status_fragments() -> list:
        # The "more" chip's level-1 sub-bar: tool/mcp/skill/pipe/hook/cron.
        # Same rendering shape as status_fragments, one level down. Stays
        # visible (as a breadcrumb) even while a sub-chip's own category
        # dropdown (level 2) is open below it.
        snap = read_model.snapshot(task_cache, config)
        if snap is None:
            return []
        frags: list = []
        for i, spec in enumerate(_MORE_SUB_CHIP_SPECS):
            val = spec.value(snap)
            selected = i == menu["sub_sel"]
            if selected:
                mark = " ▾" if menu["cat_open"] else ""
                frags.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {spec.label} {val}{mark} "))
            else:
                frags.append((f"fg:{_CC_DIM}", f" {spec.label} "))
                frags.append((f"fg:{spec.value_color} bold", val))
                frags.append((f"fg:{_CC_DIM}", " "))
            if i < len(_MORE_SUB_CHIP_SPECS) - 1:
                frags.append((f"fg:{_CC_DIM}", "│"))
        if menu["cat_open"] and menu_region.cursor_on_selectable:
            hint = "  [↑↓ select · enter toggle · esc back]"
        elif menu["cat_open"]:
            hint = "  [esc / ↑ back]"
        else:
            hint = "  [←→ select · enter open · esc back]"
        frags.append((f"fg:{_CC_DIM}", hint))
        return frags

    sub_status_win = ConditionalContainer(
        Window(FormattedTextControl(sub_status_fragments), height=1),
        filter=Condition(lambda: menu["open"] and _is_more()),
    )

    def dropdown_frags() -> list:
        # The opened chip's element lives in menu_region; render its live rows.
        # The focus cursor is drawn only on a selectable row (the model picker) —
        # a read-only DetailElement reports cursor_on_selectable False, so its
        # detail panel shows no highlight, exactly as before.
        # Windowed like above_region_frags: the "…" overflow chip can list one
        # row per tool-visibility toggle (dozens in a real session), so this
        # slices to _MENU_REGION_MAX_HEIGHT rows + a "↓ N more" hint instead of
        # requesting an unbounded height (prompt_toolkit "Window too small").
        draw_cursor = menu_region.cursor_on_selectable
        scroll = menu_region.scroll
        lines = menu_region.lines()
        visible = lines[scroll : scroll + _MENU_REGION_MAX_HEIGHT]
        cursor_local = menu_region.cursor - scroll
        out: list = []
        for i, ln in enumerate(visible):
            if i:
                out.append(("", "\n"))
            if draw_cursor and i == cursor_local:
                out.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {ln} "))
            else:
                style = _CC_DONE if ln.startswith("▸") else _CC_DIM
                out.append((f"fg:{style}", f"   {ln}"))
        items_below = len(lines) - scroll - _MENU_REGION_MAX_HEIGHT
        if items_below > 0:
            out.append(("", "\n"))
            out.append((f"fg:{_CC_DIM}", f"   ↓ {items_below} more"))
        return out

    def dropdown_height() -> Dimension:
        lines = menu_region.lines()
        n = len(lines)
        scroll = menu_region.scroll
        visible = min(n, _MENU_REGION_MAX_HEIGHT)
        hint = 1 if n > scroll + _MENU_REGION_MAX_HEIGHT else 0
        return Dimension.exact(visible + hint)

    dropdown = ConditionalContainer(
        Window(FormattedTextControl(dropdown_frags), height=dropdown_height),
        filter=Condition(
            lambda: _dropdown_visible() and get_app().layout.has_focus(status_win)
        ),
    )

    def above_region_frags() -> list:
        draw_cursor = above_region.cursor_on_selectable
        scroll = above_region.scroll
        lines = above_region.lines()
        visible = lines[scroll : scroll + _ABOVE_REGION_MAX_HEIGHT]
        cursor_local = above_region.cursor - scroll

        if is_intervention_region_key(region_holder["key"]):
            # Equal weight at rest for EVERY choice, regardless of what it
            # does — accent+bold marks only the cursor's current position,
            # wherever that is. A prior draft gave "grant" choices standing
            # accent color and left "decline" dim; a lead review caught that
            # a PERMANENT weight difference reads as "this one is safer to
            # skim past" — exactly backwards for a permission prompt. There
            # is deliberately no separate "deny" hue either: this surface
            # never adds a color the rest of the app doesn't already use.
            cursor_style = f"fg:{_CC_ACCENT} bold"
            rest_style = f"fg:{_CC_DIM}"
            if iv_choices_fit_one_row(visible):
                out: list = [(rest_style, "   ")]
                for i, ln in enumerate(visible):
                    if i:
                        out.append((rest_style, "  "))
                    style = cursor_style if (draw_cursor and i == cursor_local) else rest_style
                    out.append((style, ln))
                return out
            # Long/variable-length labels (e.g. file_access_choices'
            # recursive-path option) — same weight language, stacked
            # vertically instead of packed onto one row.
            out = []
            for i, ln in enumerate(visible):
                if i:
                    out.append(("", "\n"))
                style = cursor_style if (draw_cursor and i == cursor_local) else rest_style
                out.append((style, f"   {ln}"))
            items_below = len(lines) - scroll - _ABOVE_REGION_MAX_HEIGHT
            if items_below > 0:
                out.append(("", "\n"))
                out.append((rest_style, f"   ↓ {items_below} more"))
            return out

        # Non-intervention region content (e.g. the /rewind picker) — unchanged.
        out = []
        for i, ln in enumerate(visible):
            if i:
                out.append(("", "\n"))
            if draw_cursor and i == cursor_local:
                out.append((f"fg:#0d0f12 bg:{_CC_ACCENT} bold", f" {ln} "))
            else:
                out.append((f"fg:{_CC_DIM}", f"   {ln}"))
        items_below = len(lines) - scroll - _ABOVE_REGION_MAX_HEIGHT
        if items_below > 0:
            out.append(("", "\n"))
            out.append((f"fg:{_CC_DIM}", f"   ↓ {items_below} more"))
        return out

    def above_region_height() -> Dimension:
        lines = above_region.lines()
        n = len(lines)
        scroll = above_region.scroll
        if is_intervention_region_key(region_holder["key"]):
            visible = lines[scroll : scroll + _ABOVE_REGION_MAX_HEIGHT]
            if iv_choices_fit_one_row(visible):
                return Dimension.exact(1)
        visible = min(n, _ABOVE_REGION_MAX_HEIGHT)
        hint = 1 if n > scroll + _ABOVE_REGION_MAX_HEIGHT else 0
        return Dimension.exact(visible + hint)

    above_region_win = Window(
        FormattedTextControl(above_region_frags, focusable=True),
        height=above_region_height,
    )
    above_region_box = ConditionalContainer(
        above_region_win, filter=Condition(lambda: above_region.visible)
    )

    kb = KeyBindings()

    def _do_submit(event) -> None:
        text = buf.text
        buf.reset(append_to_history=True)
        # Force the "input is empty" repaint to land BEFORE the async submit
        # path's own scrollback echo does. Without this, the user's own line
        # coming back through session.outbox → broadcast → run_in_terminal
        # (called moments later, once the background _submit task's outbox put
        # is drained) suspends the live app for its own erase/print/redraw
        # cycle — prompt_toolkit's `in_terminal()` (application/run_in_terminal.py)
        # ERASES the live region first, then redraws at the very end, so
        # buf.reset()'s own clear only becomes visually true at THAT redraw,
        # not at this line. An explicit invalidate here paints the empty input
        # immediately, ahead of that race.
        event.app.invalidate()
        stripped = text.strip()
        if not stripped:
            return
        # /quit /exit tear the REPL down (mirrors _input_loop); everything else —
        # plain text, intervention answers, other slash — flows through
        # submit_user_text and routes inside the session unchanged.
        if stripped in ("/quit", "/exit"):
            event.app.create_background_task(_quit(transport, event.app, quitting))
        else:
            # The submitted line is NOT echoed locally here (ADR-0039
            # multi-client input-broadcast fix — removed the local-only
            # `transport.put_display(kind="user", ...)` injection that used to
            # live in this branch). The live input field still clears on
            # submit (buf.reset above), but the scrollback line now comes from
            # `session.outbox` instead: `Session.submit_user_text` (normal
            # turns) / `InterventionHandler.deliver_answer_to` (intervention
            # answers) both put a kind="user" frame on the outbox, which
            # broadcasts via `outbox_hub` to EVERY attached surface — this
            # client renders its OWN line from that SAME broadcast frame,
            # identical to how a peer thin client sees it (single source of
            # truth, local == remote by construction). Without the broadcast
            # frame the user's own message would never appear (only the agent
            # reply does) — this invariant is now carried by the outbox path
            # instead of a local echo.
            event.app.create_background_task(_submit(transport, stripped))

    # Owner spec: Enter=submit, Shift+Enter=newline. See the module-level
    # _SHIFT_ENTER_RAW_DATA / _is_shift_enter_escape / _down_arrow_action
    # docstrings (above _picker_hint) for the full cross-terminal investigation.
    @kb.add("enter", filter=has_focus(input_win))
    def _accept(event) -> None:
        last = event.key_sequence[-1] if event.key_sequence else None
        if last is not None and _is_shift_enter_escape(last.data):
            buf.newline(copy_margin=False)
            return
        _do_submit(event)

    # Kitty keyboard protocol form of Shift+Enter (`ESC [ 1 3 ; 2 u`) — enabled
    # by DEFAULT on iTerm2/kitty/Ghostty/Alacritty/Rio/Warp/Contour (not on
    # mintty, which uses the legacy form the "enter" binding above handles
    # instead). Registered as a raw multi-key sequence since prompt_toolkit has
    # no built-in "s-enter" name — confirmed via its own key-bindings docs,
    # which list every other shift-combination (s-up/s-down/s-left/s-right/
    # s-tab/etc.) but not s-enter. prompt_toolkit's KeyProcessor already
    # buffers-and-matches multi-byte sequences this way for every other
    # escape-coded key (arrows, F-keys, …), so this is the same mechanism, not
    # a special case.
    @kb.add("escape", "[", "1", "3", ";", "2", "u", filter=has_focus(input_win))
    def _shift_enter_kitty(event) -> None:
        buf.newline(copy_margin=False)

    # Ctrl+J (raw LF, 0x0A) — a byte distinct from Enter's CR (0x0D) on EVERY
    # VT100-compatible terminal, needing no protocol negotiation. The
    # guaranteed-always-works newline binding for terminals where Shift+Enter
    # is genuinely undetectable (see _SHIFT_ENTER_RAW_DATA's docstring) — the
    # same fallback pattern used by other CLIs facing this exact cross-terminal
    # gap (e.g. Claude Code's own tracking issue on this recommends Ctrl+J/
    # Alt+Enter as the documented alternative). Overrides prompt_toolkit's own
    # default c-j binding, which normally re-feeds it as if it were Enter (some
    # terminals, e.g. WSL, send \n instead of \r for plain Enter) — reyn's
    # input already only ever reaches here via an explicit Enter/Shift+Enter
    # path, so that default's rationale doesn't apply and this rebinding is safe.
    @kb.add("c-j", filter=has_focus(input_win))
    def _ctrl_j_newline(event) -> None:
        buf.newline(copy_margin=False)

    @kb.add("down", filter=has_focus(input_win) & ~has_completions)
    def _down(event) -> None:
        # Gated on ~has_completions so that while the slash menu is open ↓
        # falls through to the default binding (navigate the completion
        # list). ↑ stays on prompt_toolkit's own default (auto_up), which
        # already has the same row-aware behavior built in — only ↓ needed
        # reyn's empty-box "drop to status bar" special case, see
        # _down_arrow_action's docstring.
        action = _down_arrow_action(
            bool(buf.text), buf.document.cursor_position_row, buf.document.line_count,
        )
        if action == "focus_status":
            event.app.layout.focus(status_win)
        elif action == "cursor_down":
            buf.cursor_down()
        else:
            buf.history_forward()

    def _actionable_open() -> bool:
        # Dropdown showing AND the opened element is a selectable picker (a
        # CommandUIElement). Check the live menu_region cursor state — a selectable
        # element (CommandUIElement with classes) reports True; a read-only
        # DetailElement always reports False.
        return _dropdown_visible() and menu_region.cursor_on_selectable

    def _menu_submit(text: str) -> None:
        # A picker row was selected → close the menu and run /model or
        # /visibility or /hook via the normal slash path (cost-warn confirm +
        # budget rebuild reused). For "more"'s categories this closes back to
        # the sub-bar (cat_close), not all the way to the main bar — matches
        # every other picker staying open at its OWN level after a submit for
        # non-"more" chips (menu["open"] stays True; only menu_region clears).
        if _is_more():
            _cat_close()
        else:
            _menu_close()
        app.create_background_task(_submit(transport, text))

    def _fill_menu_region(expansion, snap, *, live_task: bool = False) -> None:
        """Build one chip/category's element(s) fresh and host them in
        menu_region. Expansion functions may return a single element (model /
        agent / a category / …) or a list of mixed elements (multiple
        DetailElement / CommandUIElement rows). Both paths are supported.

        ``live_task=True`` (task chip only): snap["task_tree"] is frozen at
        open time but task_cache updates every second (_task_poll) — swap in
        a live-reading provider so the dropdown reflects task state changes
        while it stays open, BEFORE registering (not after — a post-hoc
        re-register would double the work for no benefit)."""
        menu_region.clear()
        if expansion is None or snap is None:
            return
        result = expansion(snap, _menu_submit)
        if live_task and isinstance(result, DetailElement):
            _tc = task_cache
            def _live_tasks() -> list[str]:
                return _task_rows(_tc.get("tree") or [], 0) or ["(no active tasks)"]
            result = DetailElement(_live_tasks)
        if isinstance(result, list):
            for el in result:
                menu_region.register(el)
        else:
            menu_region.register(result)

    def _menu_open() -> None:
        # Enter the selected MAIN chip. "more" enters its level-1 sub-bar
        # (no menu_region content yet — that's _cat_open, one level down);
        # every other chip goes straight to its dropdown, unchanged.
        spec = _current_spec()
        if spec.key == "more":
            menu["open"] = True
            menu["cat_open"] = False
            return
        snap = read_model.snapshot(task_cache, config)
        _fill_menu_region(spec.expansion, snap, live_task=(spec.key == "task"))
        menu["open"] = True

    def _menu_close() -> None:
        # Fully closes back to the main bar — used by the non-"more" chips'
        # existing close path and by Esc from the "more" sub-bar (level 1).
        menu["open"] = False
        menu["cat_open"] = False
        menu_region.clear()

    def _cat_open() -> None:
        # Enter the selected SUB-chip's category (level 2) — "more" only.
        spec = _MORE_SUB_CHIP_SPECS[menu["sub_sel"]]
        snap = read_model.snapshot(task_cache, config)
        _fill_menu_region(spec.expansion, snap)
        menu["cat_open"] = True

    def _cat_close() -> None:
        # Close the category dropdown back to the sub-bar — "more" only;
        # menu["open"] stays True (still showing the sub-bar).
        menu["cat_open"] = False
        menu_region.clear()

    @kb.add("up", filter=has_focus(status_win))
    def _menu_up(event) -> None:
        # In an open picker, ↑ moves the cursor up; at the top row it closes
        # one level. A read-only panel has no cursor, so ↑ closes immediately.
        if _actionable_open() and not menu_region.at_first_selectable:
            menu_region.navigate(-1)
        elif _dropdown_visible():
            if _is_more():
                _cat_close()  # back to the sub-bar, still in status_win
            else:
                _menu_close()
                event.app.layout.focus(input_win)
        elif _sub_bar_visible():
            _menu_close()  # back to the main bar, still in status_win
        else:
            # Nothing open — user is browsing chips in the status bar.
            # ↑ returns to input AND navigates one step back in history so the
            # experience feels like a single "go up" rather than two keypresses.
            event.app.layout.focus(input_win)
            buf.history_backward()

    @kb.add("down", filter=has_focus(status_win))
    def _menu_down(event) -> None:
        if _actionable_open():
            menu_region.navigate(1)

    @kb.add("escape", filter=has_focus(status_win))
    def _menu_esc(event) -> None:
        if _dropdown_visible():
            if _is_more():
                _cat_close()
                return
            _menu_close()
        elif _sub_bar_visible():
            _menu_close()
            return
        event.app.layout.focus(input_win)

    @kb.add("left", filter=has_focus(status_win))
    def _menu_left(event) -> None:
        if _sub_bar_visible():
            menu["sub_sel"] = (menu["sub_sel"] - 1) % len(_MORE_SUB_CHIP_SPECS)
        elif not menu["open"]:
            menu["sel"] = (menu["sel"] - 1) % len(_CHIP_SPECS)

    @kb.add("right", filter=has_focus(status_win))
    def _menu_right(event) -> None:
        if _sub_bar_visible():
            menu["sub_sel"] = (menu["sub_sel"] + 1) % len(_MORE_SUB_CHIP_SPECS)
        elif not menu["open"]:
            menu["sel"] = (menu["sel"] + 1) % len(_CHIP_SPECS)

    @kb.add("enter", filter=has_focus(status_win))
    def _menu_enter(event) -> None:
        # Read-only chip/category: enter toggles the detail panel closed.
        # Actionable picker: when open, enter applies the cursor row
        # (region.select → on_submit → /model or /visibility or /hook via the
        # slash path); when closed, it opens the next level down.
        if _actionable_open():
            menu_region.select()
        elif _dropdown_visible():
            if _is_more():
                _cat_close()
            else:
                _menu_close()
                event.app.layout.focus(input_win)
        elif _sub_bar_visible():
            _cat_open()
        else:
            _menu_open()

    @kb.add("c-c")
    def _interrupt_or_quit(event) -> None:
        # During an active turn, first ctrl-c cancels the turn cooperatively.
        # A second ctrl-c (while still "thinking" / cancelling) falls through to
        # quit, matching the standard "ctrl-c to interrupt, ctrl-c again to exit"
        # pattern. Ctrl-D/Q always quit regardless of turn state.
        if getattr(renderer, "_thinking", False) and not getattr(renderer, "_cancelling", False):
            rc = getattr(renderer, "request_cancel", None)
            if callable(rc):
                rc()
            event.app.create_background_task(_cancel_turn(transport))
        else:
            event.app.create_background_task(_quit(transport, event.app, quitting))

    @kb.add("c-d")
    @kb.add("c-q")
    def _quit_key(event) -> None:
        event.app.create_background_task(_quit(transport, event.app, quitting))

    # Above-region focus navigation (inert until a consumer registers an element,
    # since the region stays invisible + unfocusable while empty). ↑↓ move the
    # cursor, enter activates the focused row, esc dismisses (cmd) or is blocked (iv).
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
        # Command-UI (rewind picker etc.): Escape dismisses — clear the pending
        # request so _sync_region collapses the region on the next poll, then
        # return focus to the input box.
        # Intervention (confirm / select): Escape is a no-op. The same-key skip
        # in _sync_region prevents re-grabbing focus if we move away here, which
        # would leave the user unable to resolve a blocking intervention.
        key = region_holder["key"]
        if key and key.startswith("cmd:"):
            read_model.clear_pending_command_ui()
            above_region.clear()
            region_holder["key"] = None
            event.app.layout.focus(input_win)

    def _show(element, key: str) -> None:
        above_region.clear()
        region_holder["key"] = key
        above_region.register(element)
        app.layout.focus(above_region_win)

    def _cmd_submit(text: str) -> None:
        read_model.clear_pending_command_ui()  # consume the request
        app.create_background_task(_submit(transport, text))

    def _sync_region() -> None:
        """Sync the above-region with the pending UI: the head closed-set
        intervention (priority — it blocks a turn), else a command-UI request (the
        /rewind picker etc.). Poll-driven, like the status chips. Inert when
        nothing is pending — same as an empty region.

        Both reads come through the read-model: a REMOTE read-model returns None
        for both (interventions ride the display prompt + input line over the wire;
        command-UI is not on the wire), so the region stays inert on a remote
        client and closed-set answers flow through the input field instead.
        """
        head = read_model.intervention_head()
        if head is not None and getattr(head, "choices", None):
            key = f"iv:{head.id}"
            if key != region_holder["key"]:
                _show(build_intervention_element(
                    head,
                    lambda cid, label: app.create_background_task(
                        _deliver_intervention_choice(transport, cid, label)
                    ),
                ), key)
            return
        cmd = read_model.pending_command_ui()
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
                active = await read_model.list_active_tasks()
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
         sub_status_win, dropdown]
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
            # #2786: this Application.run_async() is the input driver for the
            # DEFAULT interactive `reyn chat` (this renderer's uses_app_input()
            # is what selects run_inline_input over repl.py's PromptSession
            # path) -- prompt_toolkit's default (set_exception_handler=True)
            # would mask #2637's durable asyncio exception capture for the
            # exact same reason as the PromptSession path in
            # interfaces/repl/repl.py (see that call site's comment): the app
            # owns the loop's exception handler for its whole run, which is
            # most of the REPL's wall-clock time. False keeps reyn's handler
            # wired throughout.
            await app.run_async(set_exception_handler=False)
    finally:
        poll_task.cancel()
        task_poll_task.cancel()


async def _deliver_intervention_choice(transport, choice_id: str, label: str) -> None:
    """Deliver a region-selected intervention choice.

    The chosen choice id is delivered authoritatively via the transport send
    seam (``answer_intervention_choice``), which resolves through
    ``InterventionHandler.deliver_answer_to`` — the ONE funnel every answer
    path (TUI free-text, TUI choice-region, A2A peer, AG-UI HITL) shares.

    No local echo here anymore (ADR-0039 multi-client input-broadcast fix):
    this used to ``transport.put_display(kind="system", text=f"answered:
    {label}")`` on success — a LOCAL-ONLY injection that never reached a peer
    thin client. ``deliver_answer_to`` now puts a ``kind="user"`` frame
    (``text=label``, neutralized) on the SESSION outbox for every resolved
    answer, which broadcasts via ``outbox_hub`` to this AND every other
    attached surface. Re-adding a local echo here would double-render this
    client's own line (once from this call, once from the broadcast it also
    receives) — ``label`` is kept as a parameter for call-site compatibility
    with ``build_intervention_element``'s callback shape, even though it is no
    longer read in this body.
    """
    try:
        await transport.answer_intervention_choice(choice_id)
    except Exception:
        logger.exception("inline: delivering intervention choice failed")


async def _submit(transport, text: str) -> None:
    # Launched as a background task, so an uncaught error here goes to asyncio's
    # exception handler (invisible above the live app) and the user sees the input
    # field clear with no response — a silent failure. Contain it: log + surface a
    # visible error line via the transport's display seam the output loop drains.
    try:
        # Route free-text input to any pending free-text intervention rather than
        # starting a new chat turn. Free-text = no choices (ask_user, mcp_install.secret,
        # etc.). Closed-set interventions (choices non-empty) are handled by the region
        # dropdown and never reach this path — matching build_intervention_element logic.
        # Shape-safe: the LOCAL transport's head is an Intervention (``.choices``);
        # the REMOTE transport's head is a bare intervention-id string (ADR-0039
        # P3 — choices are not carried, the prompt rides the display frame). Either
        # way, a free-text (no-choices) intervention takes the answer path; a
        # remote head (no ``.choices`` attr) is always answered as free text here.
        head = transport.pending_intervention_head()
        if head is not None and not getattr(head, "choices", None):
            await transport.answer_intervention_text(text)
            return
        await transport.submit_user_text(text)
    except Exception as e:
        logger.exception("inline submit failed")
        detail = f"{type(e).__name__}: {e}"
        if len(detail) > 72:
            detail = detail[:69] + "…"
        try:
            from reyn.runtime.outbox import OutboxMessage
            transport.put_display(
                OutboxMessage(kind="error", text=f"input could not be submitted: {detail}")
            )
        except Exception:
            pass


async def _cancel_turn(transport) -> None:
    """Cancel the in-flight turn via the transport's cancel seam — cooperative."""
    try:
        await transport.cancel_inflight()
    except Exception:
        logger.exception("cancel_inflight failed")


async def _quit(transport, app, state: dict) -> None:
    # Idempotent: shutdown has a grace window, so a second quit (rapid Ctrl-C /
    # `/quit` then Ctrl-C) could race the first to app.exit() ("Return value
    # already set"). The check+set is synchronous (before the first await), so in
    # the single-threaded loop only the first task proceeds to app.exit().
    if state.get("quitting"):
        return
    state["quitting"] = True
    try:
        await transport.shutdown()
    except Exception:
        # Log and suppress — a shutdown exception must not prevent app.exit()
        # from running (the PT app would hang with no escape path).
        logger.exception("transport shutdown failed during quit")
    finally:
        # asyncio.CancelledError (BaseException, not caught by `except Exception`
        # above) must also reach app.exit() — without finally, a cancelled _quit
        # task leaves state["quitting"]=True with app.exit() never called, hanging
        # the PT application with no escape path.
        app.exit()
