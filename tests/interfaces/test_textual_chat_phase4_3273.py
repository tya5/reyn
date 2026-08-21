"""Phase 4 TUI-rebuild gates (#3273): the bottom-chrome drawer wired to reyn's
REAL data (registries + live status snapshot), replacing the Phase-3 placeholders.

The graded invariants:

- **Derive-from-registry completeness** (Tier 1 + Tier 2): every ENUMERATING pane
  (Model / Agent / Menu) renders the FULL canonical set, never a hand-curated
  subset — a fake entry added to the registry surfaces in the drawer. Model/Agent
  derive from the status snapshot's ``model_classes`` / ``agent_names`` (=
  ``ModelResolver.known_classes()`` / ``AgentRegistry.loaded_names()``); Menu
  derives from the whole :data:`reyn.interfaces.slash.REGISTRY`.
- **No placeholder residue** (Tier 1): with no snapshot the pickers are EMPTY
  (not the old hardcoded ``sonnet/opus/…`` list) and the readouts show zeros, not
  ``(placeholder)`` — content comes only from real sources.
- **Cost/ctx visible — F5b** (Tier 1 + Tier 2): the live cost/ctx figures surface
  both on the always-visible status line and in the Cost/Ctx drawer panes.
- **Import isolation preserved** (Tier 2c): the Phase-4 wiring adds no top-level
  textual import to an always-loaded module — the plain path still imports green
  with ``textual`` / ``textual_flowview`` unimportable.

Enumerating-pane completeness is proved as a chain: the pure formatter fully
enumerates its input (a fake entry appears), AND the app feeds it the WHOLE
registry (the drawer options equal the real registry's contents) — so a newly
registered command / configured class / loaded agent appears with no code change.
All app-level tests use real instances (a concrete ``ClientTransport`` +
``ChatReadModel`` seam impl + the real app/pilot) — no mocks — per the policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat.chrome import (
    agent_pane_options,
    cost_pane_lines,
    ctx_pane_lines,
    menu_pane_options,
    model_pane_options,
    pane_payload,
    status_line_text,
)
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.slash import REGISTRY, SlashCommand, SlashRegistry
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT

# A real-shaped status snapshot (ONLY keys ``interfaces/inline/app.py:_snapshot``
# actually produces — no invented field). Reused across the wiring tests.
_SNAP = {
    "model": "claude-opus-4-8",
    "model_active_class": "opus",
    "model_classes": ["light", "opus", "strong", "zzz-fake-class"],
    "agent_names": ["default", "planner", "zzz-fake-agent"],
    "attached_name": "default",
    "session_tree": [],
    "usage": (1200, 340, 1540),
    "cost_usd": 0.0123,
    "cost_agent": 0.0123,
    "cost_total": 0.0500,
    "agent_tokens": 1540,
    "ctx_used": 90000,
    "ctx_window": 200000,
    "ctx_source": "model",
    "ctx_recent_usage": (90000, 40000),
}


# ── Tier 1: derive-from-registry completeness of the pure formatters ──────────


def test_model_pane_enumerates_full_class_set_no_subset() -> None:
    """Tier 1: the Model pane renders EVERY configured class (the active one
    marked), never a curated subset — a fake class added to the input appears, so
    a newly-configured ``ModelResolver`` class surfaces automatically."""
    classes = ["light", "opus", "strong", "zzz-fake-class"]
    rows = model_pane_options(classes, active="opus")
    # Every class is present (map row → underlying class by stripping the marker).
    rendered = {r.split("  · active")[0] for r in rows}
    assert rendered == set(classes), "Model pane dropped/added a class vs the registry"
    assert "zzz-fake-class" in rendered, "a fresh registry class did not appear"
    assert rows[classes.index("opus")] == "opus  · active", "active class not marked"


def test_model_pane_marks_raw_passthrough_model_when_no_class_matches() -> None:
    """Tier 1: #3324 — when ``--model <raw-id>`` bypasses the class system
    (``active`` is a raw LiteLLM model string matching no configured class,
    the shape ``Session.active_model_class() is None`` falls back to), the
    Model pane surfaces it as its own informational row rather than
    silently marking nothing.

    Falsification: pre-fix, none of the rendered rows contained the raw
    model string or any active marker at all."""
    classes = ["light", "standard", "strong"]
    raw_model = "gemini-2.5-flash-lite"  # not declared as a class name above
    rows = model_pane_options(classes, active=raw_model)
    assert any(raw_model in row for row in rows), (
        f"raw passthrough model {raw_model!r} does not appear anywhere in the pane: {rows}"
    )
    # No configured class is spuriously marked active.
    assert not any("· active" in row for row in rows), (
        f"a configured class was marked active for a raw passthrough model: {rows}"
    )
    # Every configured class is still present, unmodified.
    assert {row for row in rows if row in classes} == set(classes)


def test_agent_pane_enumerates_full_agent_set_no_subset() -> None:
    """Tier 1: the Agent pane renders EVERY loaded agent (the attached one marked),
    never a curated subset — a fake agent added to the input appears, so a freshly
    loaded/attached agent surfaces automatically."""
    names = ["default", "planner", "zzz-fake-agent"]
    rows = agent_pane_options(names, active="default")
    rendered = {r.split("  · active")[0] for r in rows}
    assert rendered == set(names), "Agent pane dropped/added an agent vs the registry"
    assert "zzz-fake-agent" in rendered, "a fresh registry agent did not appear"
    assert rows[0] == "default  · active", "attached agent not marked"


def test_menu_pane_enumerates_full_slash_registry_no_subset() -> None:
    """Tier 1: the Menu pane renders EVERY non-hidden slash command from a REAL
    ``SlashRegistry`` — a freshly-registered command appears, a hidden one is
    excluded. Proves the formatter enumerates the whole registry (no subset)."""
    reg = SlashRegistry()
    reg.register(SlashCommand("realcmd", "a real command", handler=_noop))
    reg.register(SlashCommand("zzz-fake", "the fresh entry", handler=_noop))
    reg.register(SlashCommand("secret", "hidden one", handler=_noop, hidden=True))

    rows = menu_pane_options(reg.all_commands())
    names = {r.split(" — ")[0] for r in rows}
    assert names == {"/realcmd", "/zzz-fake"}, f"menu != full visible registry: {names}"
    assert "/zzz-fake — the fresh entry" in rows, "a fresh registry command did not appear"
    assert not any(r.startswith("/secret") for r in rows), "hidden command leaked into menu"


# ── Tier 1: no placeholder residue (behavioral — empty/zero, not hardcoded) ───


def test_no_placeholder_residue_pickers_empty_readouts_zero() -> None:
    """Tier 1: with NO snapshot the pickers are empty (not the Phase-3 hardcoded
    ``sonnet/opus/haiku/gemini`` list) and the readouts show zeros, not
    ``(placeholder)`` — content is sourced only from real data."""
    assert pane_payload("model", snapshot=None) == [], "Model fell back to a hardcoded list"
    assert pane_payload("agent", snapshot=None) == [], "Agent fell back to a hardcoded list"
    cost_blob = " ".join(cost_pane_lines(None))
    ctx_blob = " ".join(ctx_pane_lines(None))
    assert "placeholder" not in cost_blob.lower()
    assert "placeholder" not in ctx_blob.lower()
    assert "$0.0000" in cost_blob, "cost readout should show a real (zero) figure"


# ── Tier 1: F5b — cost/ctx surfaced on the status line + Cost/Ctx panes ───────


def test_f5b_cost_and_ctx_surface_on_status_line() -> None:
    """Tier 1: the status-values line reflects the live cost + context percent
    from the snapshot (F5b: cost is legible in the Textual TTY, drawer closed).

    #4542: 90k/200k = 45%, below CTX_WARN_PERCENT (80) — the bare percent
    renders WITHOUT the "ctx" label at this level (labelling is reserved for
    the over-threshold case; see test_ctx_percent_gains_ctx_label_past_warn_threshold)."""
    line = status_line_text(_SNAP, "default")
    assert "$0.0123" in line, "running cost missing from the status line"
    assert "45%" in line, "context percent (90k/200k) missing from the status line"
    assert "ctx 45%" not in line, "below CTX_WARN_PERCENT must stay unlabelled"
    assert "opus" in line and "default" in line, "model/agent missing from the status line"


def test_ctx_percent_gains_ctx_label_past_warn_threshold() -> None:
    """Tier 1: #4542 — context percent gains the "ctx" label ONLY at/past
    CTX_WARN_PERCENT; below it, the bare percent is unambiguous next to the
    cost figure (see the test above for the below-threshold case)."""
    from reyn.interfaces.inline.textual_chat.chrome import CTX_WARN_PERCENT

    hot_snap = {**_SNAP, "ctx_used": 180000, "ctx_window": 200000}  # 90%
    line = status_line_text(hot_snap, "default")
    assert "ctx 90%" in line, f"90% is past CTX_WARN_PERCENT ({CTX_WARN_PERCENT}) and must be labelled"


def test_f5b_cost_and_ctx_panes_reflect_usage() -> None:
    """Tier 1: the Cost + Ctx drawer readouts reflect the snapshot's live token /
    cost / context figures (F5b), not a placeholder constant."""
    cost = " ".join(cost_pane_lines(_SNAP))
    assert "$0.0123" in cost and "$0.0500" in cost, "cost pane missing agent/total cost"
    assert "1,200" in cost and "340" in cost, "cost pane missing token counts"
    ctx = " ".join(ctx_pane_lines(_SNAP))
    assert "90,000" in ctx and "200,000" in ctx, "ctx pane missing used/window figures"
    assert "45%" in ctx, "ctx pane missing occupancy percent"


# ── App wiring (Tier 2): the mounted drawer reflects the canonical sources ────


def _noop(session=None, args: str = "") -> None:  # pragma: no cover - handler stub
    return None


class _SnapshotReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (like ``RegistryReadModel`` /
    ``RemoteReadModel``) returning a fixed real-shaped snapshot — the app reads
    model/agent/cost/ctx off this same seam the plain status bar uses."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, snap: "dict | None") -> None:
        self._snap = snap

    def snapshot(self, config=None):
        return self._snap

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_phase4_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class ScriptedTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport`. ``end=False`` keeps the stream
    open so the app stays mounted for drawer inspection; submissions are captured
    so a picker-issued ``/model`` / ``/attach`` slash can be asserted."""

    def __init__(self, messages: "list[OutboxMessage] | None" = None) -> None:
        self._messages = list(messages or [])
        self.submitted: list[str] = []
        # #3595 S5: a menu row dispatches its slash as a COMMAND, not a turn.
        self.commands: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def run_slash_command(self, name: str, args: str) -> bool:
        self.commands.append(f"/{name} {args}".rstrip())
        return True

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _option_prompts(option_list) -> list[str]:
    return [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]


@pytest.mark.asyncio
async def test_model_drawer_reflects_snapshot_classes_full_set() -> None:
    """Tier 2: opening the Model drawer shows EVERY class from the read-model
    snapshot (the registry projection), including a fake one — proving the pane
    derives from the canonical source, not a hardcoded list (the KEY GATE at the
    app boundary)."""
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(_SNAP)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("model")
        await pilot.pause()
        prompts = _option_prompts(app.query_one("#model", OptionList))
        rendered = {p.split("  · active")[0] for p in prompts}
        assert rendered == set(_SNAP["model_classes"]), (
            f"Model drawer != full class set: {rendered}"
        )
        assert "zzz-fake-class" in rendered, "a fresh registry class did not appear in the drawer"


@pytest.mark.asyncio
async def test_menu_drawer_equals_full_slash_registry() -> None:
    """Tier 2: the Menu drawer options equal the REAL global slash ``REGISTRY``'s
    visible command set (no hand-curated subset) — a command registered anywhere
    via ``@slash`` is already present."""
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(_SNAP)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("menu")
        await pilot.pause()
        prompts = _option_prompts(app.query_one("#menu", OptionList))
        drawer_names = {p.split(" — ")[0].lstrip("/") for p in prompts}
        assert drawer_names == set(REGISTRY.names()), (
            "Menu drawer is a curated subset of the slash registry"
        )


@pytest.mark.asyncio
async def test_status_line_and_cost_pane_show_live_cost_f5b() -> None:
    """Tier 2: F5b end-to-end — the mounted status line and the Cost drawer pane
    both reflect the snapshot's live cost/ctx (previously the Textual path showed
    no cost at all).

    #4542: 90k/200k = 45%, below CTX_WARN_PERCENT — asserts the bare percent
    (see test_ctx_percent_gains_ctx_label_past_warn_threshold for the
    labelled, over-threshold case)."""
    from textual.widgets import Static

    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(_SNAP)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        status = str(app.query_one(StatusLine).render())
        assert "$0.0123" in status and "45%" in status, f"status line lacks cost/ctx: {status}"
        app._open_drawer("cost")
        await pilot.pause()
        cost_text = str(app.query_one("#cost", Static).render())
        assert "$0.0123" in cost_text and "1,200" in cost_text, (
            f"Cost pane lacks live figures: {cost_text}"
        )


@pytest.mark.asyncio
async def test_selecting_model_row_routes_model_slash() -> None:
    """Tier 2: selecting a Model row applies the pick by routing the equivalent
    ``/model <class>`` slash through the transport (the same contract the plain
    path's status-bar picker dispatches), then collapses the drawer."""
    from textual.widgets import ContentSwitcher, OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    transport = ScriptedTransport()
    app = TextualChatApp(transport=transport, read_model=_SnapshotReadModel(_SNAP))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("model")
        await pilot.pause()
        option_list = app.query_one("#model", OptionList)
        # Select the "strong" class (index 2 in model_classes).
        idx = _SNAP["model_classes"].index("strong")
        option_list.post_message(OptionList.OptionSelected(option_list, option_list.get_option_at_index(idx), idx))
        await pilot.pause()
        assert "/model strong" in transport.commands, f"picker did not route /model: {transport.commands}"
        assert app.query_one("#drawer", ContentSwitcher).display is False, "drawer did not collapse"


# ── Tier 2c: import isolation preserved (Phase 4 adds no always-loaded import) ─

_ISOLATION_SUBPROCESS = '''
import sys


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("textual", "textual_flowview"):
            raise ModuleNotFoundError("blocked for isolation test: " + name)
        return None


sys.meta_path.insert(0, _Block())

import reyn.interfaces.repl.client_driver  # noqa: E402,F401
import reyn.interfaces.repl.stream_client  # noqa: E402,F401
import reyn.interfaces.cli.commands.chat  # noqa: E402,F401
import reyn.interfaces.slash  # noqa: E402,F401  (menu pane source — must stay textual-free)

assert "textual_flowview" not in sys.modules, "flowview imported at module load"
assert "textual" not in sys.modules, "textual imported at module load"
print("ISOLATION_OK")
'''


def test_phase4_wiring_imports_stay_tty_only() -> None:
    """Tier 2c: with ``textual`` / ``textual_flowview`` unimportable, the plain /
    non-TTY path (plus the slash registry the Menu pane reads) still imports green
    — Phase 4's registry wiring added no top-level textual import to an
    always-loaded module. Runs the strip in a clean subprocess."""
    import subprocess
    import sys

    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SUBPROCESS],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ISOLATION_OK" in proc.stdout, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ── History drawer pane (#3302 fix-class): OptionList markup + neutralize ────
# The History tab (``chrome.py``'s ``_LIST_PANES``) is the ONE drawer pane
# whose rows are live conversation content — unlike Model/Agent/Menu
# (operator/config-derived identifiers, out of scope — see the PR body), a
# History row is LLM-/user-derived text reaching an ``OptionList``, which
# markup-parses a bare ``str`` option exactly like ``Static``/``RadioButton``
# do (verified live against the installed Textual 8.2.8:
# ``OptionList("[y]es")`` renders as ``"es"`` — the SAME #3302 bracket-eating
# class through a different widget). Two independent guards:
#
# - fidelity (``Content`` wrap, ``chrome._history_option_content``) — applied
#   at TWO separate call sites (the initial ``build_drawer_pane`` construction
#   at ``compose`` time, and ``TextualChatApp._refresh_pane``'s re-derive on
#   every drawer open) — each gets its OWN witness below, so a fix landing at
#   only one site cannot hide behind the other's green.
# - neutralize (ESC/control strip, ``_neutralized_label``) — applied ONCE,
#   upstream, in ``_history_turns`` — both consumers read that single output,
#   so one witness covers both call sites for this half.


def _history_option_plain(option_list, index: int) -> str:
    """The rendered text of an ``OptionList`` row AFTER Textual's own
    markup-parse (``OptionList._get_visual`` → ``textual.visual.visualize``)
    — never ``option.prompt`` (the pre-render original, which stays intact
    even when the render pipeline eats a bracket). Mirrors how the
    intervention-panel tests read ``RadioButton.label.plain`` for the same
    reason. Goes through the widget's own visual-cache accessor rather than
    a full strip render (``OptionList._get_option_render``), which raises on
    the currently-installed Textual/rich combination for unrelated reasons —
    ``_get_visual(...).plain`` is the narrowest slice of the SAME production
    call path that still observes the actual markup-parse outcome."""
    option = option_list.get_option_at_index(index)
    return option_list._get_visual(option).plain


@pytest.mark.asyncio
async def test_history_pane_initial_build_preserves_bracket_labels() -> None:
    """Tier 1: the History pane's INITIAL build (``chrome.build_drawer_pane``,
    the ``compose``-time call) must not markup-parse conversation text.

    NON-VACUITY (falsification, verified locally): reverting ONLY
    ``build_drawer_pane``'s ``Content`` wrap for the History tab (passing the
    bare ``rows`` straight to ``OptionList`` unconditionally) makes this
    assertion FAIL — ``"you · [y]es I did it"`` renders as
    ``"you · es I did it"``, reproducing the #3302 defect through a
    different widget. Reverting ONLY the REFRESH path's wrap (the sibling
    test below) does NOT affect this assertion — this exercises compose-time
    construction only, never ``_refresh_pane``."""
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat.chrome import build_drawer_pane

    class _PaneHost(App):
        def compose(self) -> ComposeResult:
            yield build_drawer_pane("history", ["you · [y]es I did it"])

    app = _PaneHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = app.query_one(OptionList)
        rendered = _history_option_plain(ol, 0)
    assert rendered == "you · [y]es I did it", (
        f"bracket-decorated History row dropped a character at initial build: {rendered!r}"
    )


@pytest.mark.asyncio
async def test_history_pane_refresh_preserves_bracket_labels() -> None:
    """Tier 2: the History pane's REFRESH path (``TextualChatApp._refresh_pane``,
    re-invoked every time the drawer is opened) must not markup-parse
    conversation text either — a SEPARATE call site from the initial build.

    NON-VACUITY (falsification, verified locally): reverting ONLY
    ``_refresh_pane``'s ``Content`` wrap (calling ``child.add_options(rows)``
    with the bare ``rows`` unconditionally) makes this assertion FAIL
    identically. Reverting ONLY the INITIAL build's wrap (the sibling test
    above) does NOT affect this assertion — opening the drawer ALWAYS
    re-derives via ``_refresh_pane`` (``_open_drawer`` calls it
    unconditionally), so a fix landing at only the initial-build site would
    still leave the exploit reachable on every real open."""
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(_SNAP)
    )
    app.conversation.append(OutboxMessage(kind="user", text="[y]es I did it"))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("history")
        await pilot.pause()
        ol = app.query_one("#history", OptionList)
        rendered = _history_option_plain(ol, 0)
    assert "[y]es I did it" in rendered, (
        f"bracket-decorated History row dropped a character on refresh: {rendered!r}"
    )


# ── #3380: the SAME markup-parse eats reyn's OWN row markers ─────────────────
# The comment above scoped the wrap to History on the premise that every other
# pane's rows are identifiers carrying no brackets. That premise was false for the
# visibility panes, whose rows reyn itself decorates with ``[on]``/``[off]``/``[--]``
# — witnessed in a real TTY on #3380: an operator-hidden tool rendered identically
# to an available one, so #3379's "two axes, two markers" had one visible axis.
# Both widget-construction call sites get their own witness, for the same reason
# the History pair does.

_VIS_ROW = "[off] read_file"


@pytest.mark.asyncio
async def test_tool_pane_initial_build_preserves_the_state_marker() -> None:
    """Tier 1: the Tool pane's INITIAL build keeps the ``[off]`` marker the
    formatter emitted — the marker IS the state readout, so eating it makes a
    hidden capability indistinguishable from an available one."""
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat.chrome import build_drawer_pane

    class _PaneHost(App):
        def compose(self) -> ComposeResult:
            yield build_drawer_pane("tool", [_VIS_ROW])

    app = _PaneHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = _history_option_plain(app.query_one(OptionList), 0)
    assert rendered == _VIS_ROW, (
        f"the Tool pane's state marker was eaten at initial build: {rendered!r}"
    )


@pytest.mark.asyncio
async def test_tool_pane_refresh_preserves_the_state_marker() -> None:
    """Tier 2: the Tool pane's REFRESH path keeps the marker too — a separate
    call site, and the one every real drawer open goes through."""
    from textual.widgets import OptionList

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    snap = dict(_SNAP)
    snap["visibility_items"] = [
        {"kind": "tool", "name": "read_file", "on": False, "denied": False,
         "denied_reason": None},
    ]
    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(snap)
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._open_drawer("tool")
        await pilot.pause()
        rendered = _history_option_plain(app.query_one("#tool", OptionList), 0)
    assert rendered == _VIS_ROW, (
        f"the Tool pane's state marker was eaten on refresh: {rendered!r}"
    )


def test_history_turns_neutralizes_raw_esc_osc() -> None:
    """Tier 1: a conversation turn carrying raw terminal control sequences
    must not leak into the History pane's row text — the SAME
    ``core.present.guard.get_neutralizer("terminal")`` seam every other
    #3302-class site uses. Both the initial-build and refresh consumers read
    this SAME ``_history_turns()`` output, so this one witness covers both
    call sites for the neutralize half (unlike fidelity, which is guarded
    separately at each widget-construction site, above).

    NON-VACUITY (falsification, verified locally): reverting ONLY the
    ``_neutralized_label`` call in ``_history_turns`` (reading the raw
    ``msg.text`` directly) makes this assertion FAIL — the raw ``\\x1b``
    survives into the row string. Reverting either fidelity wrap above does
    NOT affect this assertion — neutralize is checked here purely at the
    string level, before any widget is involved."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    app = TextualChatApp(
        transport=ScriptedTransport(), read_model=_SnapshotReadModel(_SNAP)
    )
    payload = "\x1b[31mDANGER\x1b]0;pwn\x07"
    app.conversation.append(OutboxMessage(kind="user", text=payload))
    rows = app._history_turns()
    assert rows, "the appended turn produced no History row at all"
    blob = " ".join(rows)
    assert "\x1b" not in blob, f"raw ESC leaked into the History row: {blob!r}"
    assert "\x07" not in blob, f"raw BEL leaked into the History row: {blob!r}"
    assert "DANGER" in blob
