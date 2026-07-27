"""#3352 — gutter show/hide as a reachable USER OPERATION.

The TTY conversation pane's two gutters cost a fixed column count on EVERY
row (left state marker = 2, right elapsed/tokens = 12), taken straight off the
conversation body. #3283 ④ made the right gutter's 12 columns permanent;
the owner's adjudication was that a permanent cost is acceptable *if the user
can turn it off*. flowview (pinned at ``4ea1e067``) supplies the mechanism —
two INDEPENDENT flags (``left_gutter_visible`` / ``right_gutter_visible``),
``toggle_gutter``, and the width accounting itself (``body_width``,
``left_gutter_effective_width`` / ``right_gutter_effective_width``, which
return 0 for a hidden gutter). reyn supplies the *operation*: two key
bindings, a config-backed start state, and the Help entry that makes them
discoverable.

These gates pin the reyn side of that seam:

- **the operation is reachable** (Tier 2b): the width recovery is driven by
  ``pilot.press("ctrl+t")`` / ``pilot.press("ctrl+g")`` through the real
  Textual key-dispatch path with focus where it actually sits (the Composer),
  never by calling ``FlowView.toggle_gutter`` directly.
- **the body genuinely gets the width back** (Tier 2b): asserted on
  ``FlowView.body_width`` — the public accessor the ``4ea1e067`` pin adds —
  AND on the width the presenter is actually re-invoked with. Both are
  needed: ``body_width`` reporting a larger number while nothing re-presents
  would satisfy the property and still look broken. ``region.width`` is
  asserted UNCHANGED in the same gate, pinning why it is the wrong plane
  (#3337).
- **granularity is upstream's** (Tier 2b): left and right hide independently,
  each returning exactly its own configured width and nothing else.
- **containment survives the relayout** (Tier 2b): every mounted widget stays
  fully on-screen on BOTH axes at BOTH edges, in all four gutter states, at
  three terminal sizes (#3341: a parent-only, horizontal-only gate let child
  tabs go off-screen while staying green).
- **discoverability + collision** (Tier 1): both keys reach the Help pane
  through the app's own ``BINDINGS`` (the Help pane's fourth source of
  truth alongside ``COMPOSER_KEYS``/``MENUBAR_KEYS``/``SENTQUEUE_KEYS``,
  #3314), and neither collides with any other key the app can see.
- **persistence is the configured decision** (Tier 1 + Tier 2b):
  ``chat.gutters.left`` / ``chat.gutters.right`` set the START state (round-
  tripped at a NON-default value), and a runtime toggle never writes back.

Each behavioural gate is paired with a strip that makes it go RED, so none of
them is a tautology. All use real instances — a real mounted
:class:`TextualChatApp`, a real :class:`~reyn.config.root.ReynConfig`, the
real transports/presenter subclass from the Phase-④ suite — per the testing
policy.
"""
from __future__ import annotations

import pytest
from test_textual_chat_phase4_right_gutter_3283 import (  # noqa: E402 — sibling test module
    QueueTransport,
    _started,
    _WidthRecordingPresenter,
)
from textual.app import App
from textual.screen import Screen
from textual.widgets import OptionList, TextArea

from reyn.config.chat import ChatConfig, GutterConfig, _build_chat_config
from reyn.config.root import ReynConfig
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _GUTTER_WIDTH
from reyn.interfaces.inline.textual_chat.chrome import (
    COMPOSER_KEYS,
    MENUBAR_KEYS,
    RESERVED_KEYS,
    SENTQUEUE_KEYS,
    Composer,
    MenuBar,
)
from reyn.interfaces.inline.textual_chat.completion import CompletionPopup
from reyn.interfaces.inline.textual_chat.gutter import RIGHT_GUTTER_WIDTH
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue

#: The two keys under test, and the action each is expected to run.
LEFT_KEY = "ctrl+g"
RIGHT_KEY = "ctrl+t"


def _config(*, left: bool = True, right: bool = True) -> ReynConfig:
    """A REAL :class:`ReynConfig` carrying the gutter start state (not a stub —
    the app reads ``config.chat.gutters`` off the genuine dataclass chain)."""
    return ReynConfig(chat=ChatConfig(gutters=GutterConfig(left=left, right=right)))


def _offenders(
    app: TextualChatApp, *, bounds: "tuple[int, int] | None" = None
) -> "dict[str, tuple[int, int, int, int]]":
    """Every mounted widget whose region escapes the screen on EITHER axis at
    EITHER edge — the #3341 child-plane check, applied to the whole tree rather
    than to a hand-picked parent pair.

    ``bounds`` overrides the screen size the regions are compared against; it
    exists only so the containment gate can falsify its own detector against a
    deliberately-too-small frame (a detector that never reports anything would
    make the gate green for free)."""
    screen = app.screen.size if bounds is None else type(app.screen.size)(*bounds)
    out: dict[str, tuple[int, int, int, int]] = {}
    for widget in app.screen.query("*"):
        region = widget.region
        if not widget.display or region.area == 0:
            continue
        if (
            region.x < 0
            or region.right > screen.width
            or region.y < 0
            or region.bottom > screen.height
        ):
            out[f"{type(widget).__name__}#{widget.id}"] = (
                region.x, region.right, region.y, region.bottom,
            )
    return out


# ── Tier 1: the keys exist, are discoverable, and collide with nothing ────────


def test_both_gutter_keys_are_bound_to_actions_the_app_implements() -> None:
    """Tier 1: each key resolves to exactly the gutter action it is meant to
    run, and that action method actually exists on the app. Two ways this
    silently breaks: a binding naming a MISSING action is a no-op at runtime
    (Textual logs and carries on), and a SECOND binding for the same key
    shadows the first — so the assertion is on the SET of actions each key
    maps to, which catches both."""
    expected = {LEFT_KEY: "toggle_left_gutter", RIGHT_KEY: "toggle_right_gutter"}
    bound = [b for b in TextualChatApp.BINDINGS if isinstance(b, tuple)]
    for key, action in expected.items():
        assert {b[1] for b in bound if b[0] == key} == {action}, (
            f"{key} does not map to exactly action_{action}"
        )
        assert callable(getattr(TextualChatApp, f"action_{action}", None)), (
            f"{key} -> action_{action} is not implemented on the app"
        )


def test_both_gutter_keys_reach_the_help_pane() -> None:
    """Tier 1: both keys surface in the Help readout — a key absent from Help
    is undiscoverable (#3314). Sourced the way the pane really builds it (the
    app's ``BINDINGS`` fed through ``help_pane_lines``), never by re-typing the
    key strings into a second table."""
    app = TextualChatApp(transport=QueueTransport())
    help_text = "\n".join(app._pane_rows("help"))
    assert LEFT_KEY in help_text
    assert RIGHT_KEY in help_text
    assert "gutter" in help_text.lower()
    # STRIP: with the app's BINDINGS contribution removed, the SAME pane text
    # loses both keys — proving they arrive via the binding table (the Help
    # pane's source of truth) and are not incidental prose in some other row.
    from reyn.interfaces.inline.textual_chat.chrome import help_pane_lines

    stripped = "\n".join(help_pane_lines(app_bindings=()))
    assert LEFT_KEY not in stripped
    assert RIGHT_KEY not in stripped


def test_neither_gutter_key_collides_with_any_other_key_the_app_can_see() -> None:
    """Tier 1: ``ctrl+g``/``ctrl+t`` appear in NO other binding table reachable
    from this app — Textual's own ``App``/``Screen`` defaults, the focusable
    widgets the pane mounts (``TextArea`` backs the Composer and holds focus
    most of the time; ``OptionList`` backs the drawer panes), reyn's own
    ``SentQueue``/``InterventionPanel``/``MenuBar``/``CompletionPopup``
    bindings, the composer's ``_EDIT_KEYS`` (the #3354 completion-recompute
    set, which consumes its keys before they can bubble), and the three
    imperative key tables the Help pane reads.

    ★ TWO distinct properties are asserted here, because the first draft of
    this gate stated both in prose and covered only one:

    1. **the gutter keys are free** — neither appears in ``live`` (the real
       bindings) nor in :data:`RESERVED_KEYS`;
    2. **no reserved key is taken by anyone** — ``RESERVED_KEYS`` and ``live``
       are disjoint.

    Property 2 is what protects the RESERVATION rather than this feature.
    Folding ``RESERVED_KEYS`` into the ``taken`` set only ever guards property
    1: adding ``ctrl+r`` to some widget's ``BINDINGS`` — the exact scenario
    "a new binding cannot silently take a reserved key" names — leaves both
    gutter keys free and the gate green. The two sets must therefore be kept
    APART and intersected, never merged and membership-tested.

    ``RESERVED_KEYS`` exists at all because a live-binding sweep is
    structurally blind to a key claimed by an approved-but-unimplemented
    feature (#2193's ``ctrl+r``/``f2`` for voice STT): the implementation was
    deleted and the claim survives only in an issue, so the key looks free in
    every grep of the tree and collides the day the feature lands. That is
    what this arc's first key choice got wrong.

    Enumerated from the CLASSES and their real key constants, not from a
    hardcoded list, so a future Textual upgrade — or a future reyn widget —
    that binds either key fails here instead of silently shadowing the
    toggle."""
    live: set[str] = set(Composer._EDIT_KEYS)
    for cls in (
        App, Screen, TextArea, OptionList,
        SentQueue, InterventionPanel, MenuBar, CompletionPopup,
    ):
        for ancestor in cls.__mro__:
            for binding in ancestor.__dict__.get("BINDINGS", []) or []:
                raw = binding[0] if isinstance(binding, tuple) else getattr(binding, "key", "")
                live.update(part.strip() for part in str(raw).split(","))
    for key, _desc in (*COMPOSER_KEYS, *MENUBAR_KEYS, *SENTQUEUE_KEYS):
        live.update(part.strip() for part in key.replace("/", " ").split())
    reserved = set(RESERVED_KEYS)

    # The live enumeration is non-vacuous: keys this app really does bind are
    # in it. Asserted on ``live`` specifically — asserting it on the union
    # would pass on the strength of RESERVED_KEYS alone.
    assert {"escape", "enter", "tab", "ctrl+c"} <= live
    assert reserved, "the reserved-key table is empty — property 2 would be vacuous"

    # Property 2: nobody has taken a reserved key.
    assert not (reserved & live), (
        f"live bindings have taken reserved keys {sorted(reserved & live)} — "
        f"each is claimed by an unimplemented feature (see RESERVED_KEYS)"
    )
    # Property 1: the two gutter keys are free of both planes.
    assert LEFT_KEY not in live | reserved
    assert RIGHT_KEY not in live | reserved


def test_gutter_start_state_round_trips_a_non_default_value() -> None:
    """Tier 1: ``chat.gutters`` parses both flags, and a NON-default (``False``)
    value survives the parse — a round-trip that only checked the default would
    pass against a parser that ignored the key entirely. The partial form
    (only one side given) leaves the other at its default."""
    assert _build_chat_config({}).gutters == GutterConfig(left=True, right=True)
    parsed = _build_chat_config({"gutters": {"left": False, "right": False}}).gutters
    assert parsed == GutterConfig(left=False, right=False)
    assert _build_chat_config({"gutters": {"right": False}}).gutters.left is True


# ── Tier 2b: the operation, driven through the real key-dispatch path ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(100, 30), (80, 24), (60, 20)])
async def test_hiding_the_right_gutter_hands_its_whole_column_back_to_the_body(
    screen_size: "tuple[int, int]",
) -> None:
    """Tier 2b: pressing ``ctrl+t`` grows ``FlowView.body_width`` by EXACTLY
    ``RIGHT_GUTTER_WIDTH`` and drops ``right_gutter_effective_width`` to 0,
    and pressing it again restores both. Driven by a real key press with focus
    where it actually sits (the Composer), so this witnesses the binding →
    action → flowview path, not the API in isolation.

    ``region.width`` is asserted UNCHANGED across the same toggle: it is the
    plane a naive gate would measure and it does not respond to gutter
    configuration at all (#3337), so pinning its non-response here keeps the
    reason this gate reads ``body_width`` visible at the assertion site."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await transport.push(_started("op-right"))
        await pilot.pause()
        flow = app._flow
        before, region_before = flow.body_width, flow.region.width
        assert flow.right_gutter_effective_width == RIGHT_GUTTER_WIDTH

        await pilot.press(RIGHT_KEY)
        await pilot.pause()
        assert flow.right_gutter_visible is False
        assert flow.right_gutter_effective_width == 0
        assert flow.body_width == before + RIGHT_GUTTER_WIDTH, (
            f"hiding the right gutter recovered "
            f"{flow.body_width - before} columns, expected {RIGHT_GUTTER_WIDTH}"
        )
        assert flow.region.width == region_before

        await pilot.press(RIGHT_KEY)
        await pilot.pause()
        assert flow.right_gutter_visible is True
        assert flow.body_width == before


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(100, 30), (80, 24), (60, 20)])
async def test_hiding_the_left_gutter_hands_back_only_its_own_column(
    screen_size: "tuple[int, int]",
) -> None:
    """Tier 2b: the sibling of the gate above on the LEFT gutter, and the
    granularity check — ``ctrl+g`` recovers exactly ``_GUTTER_WIDTH`` and
    leaves the RIGHT gutter untouched (upstream's two flags are independent;
    reyn must not have collapsed them into one switch)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await transport.push(_started("op-left"))
        await pilot.pause()
        flow = app._flow
        before = flow.body_width

        await pilot.press(LEFT_KEY)
        await pilot.pause()
        assert flow.left_gutter_visible is False
        assert flow.left_gutter_effective_width == 0
        assert flow.body_width == before + _GUTTER_WIDTH
        # Independence: the right gutter is untouched by the left key.
        assert flow.right_gutter_visible is True
        assert flow.right_gutter_effective_width == RIGHT_GUTTER_WIDTH

        await pilot.press(LEFT_KEY)
        await pilot.pause()
        assert flow.body_width == before


@pytest.mark.asyncio
async def test_the_body_is_actually_re_presented_at_the_recovered_width() -> None:
    """Tier 2b: hiding a gutter re-invokes the PRESENTER at the new, wider
    body width — the content genuinely reflows rather than ``body_width``
    merely reporting a larger number while the rendered rows stay laid out for
    the old width. Read off the real collaboration seam
    (:class:`_WidthRecordingPresenter`, a real ``ReynPresenter`` subclass that
    records what FlowView hands ``present()``)."""
    transport = QueueTransport()
    presenter = _WidthRecordingPresenter()
    app = TextualChatApp(transport=transport, presenter=presenter)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await transport.push(_started("op-reflow"))
        await pilot.pause()
        flow = app._flow
        seen_before = len(presenter.widths)
        recovered = flow.body_width + RIGHT_GUTTER_WIDTH

        await pilot.press(RIGHT_KEY)
        await pilot.pause()
        after = presenter.widths[seen_before:]
        assert after, "hiding the gutter re-presented nothing — the body never reflowed"
        assert set(after) == {recovered}, (
            f"the body was re-presented at {sorted(set(after))}, expected every "
            f"re-present at the recovered width {recovered}"
        )
        assert flow.body_width == recovered


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_size", [(100, 30), (80, 24), (60, 20)])
@pytest.mark.parametrize("keys", [(), (LEFT_KEY,), (RIGHT_KEY,), (LEFT_KEY, RIGHT_KEY)])
async def test_every_widget_stays_on_screen_in_every_gutter_state(
    screen_size: "tuple[int, int]", keys: "tuple[str, ...]"
) -> None:
    """Tier 2b: ★ #3341 pattern — in ALL FOUR gutter states, at three terminal
    sizes, EVERY mounted widget's region is fully inside the screen on BOTH
    axes at BOTH edges (``x >= 0`` and ``x + width <= screen_width``;
    ``y >= 0`` and ``y + height <= screen_height``).

    Whole-tree, not a hand-picked parent pair: #3341 found that a parent-only,
    horizontal-only gate stayed green while child tabs were laid out past the
    right edge. Hiding a gutter changes the body width like a resize, so the
    check is re-run per state rather than assumed to carry over from the
    always-visible layout the earlier arcs measured."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=screen_size) as pilot:
        await pilot.pause()
        await transport.push(_started("op-geo"))
        await pilot.pause()
        flow = app._flow
        for key in keys:
            await pilot.press(key)
        await pilot.pause()
        assert not _offenders(app), (
            f"widgets off-screen at {screen_size} after {keys or 'no toggle'}: "
            f"{_offenders(app)}"
        )
        # Not-squashed floor: a "contained" layout that collapsed the pane to
        # nothing would satisfy the bounds above (#3311's lesson).
        assert flow.region.height > 0
        assert flow.body_width > 0
        # DETECTOR FALSIFICATION: the same widgets, compared against a frame one
        # cell smaller on each axis, MUST be reported — otherwise "no offenders"
        # above would be a property of the detector, not of the layout.
        shrunk = _offenders(app, bounds=(screen_size[0] - 1, screen_size[1] - 1))
        assert shrunk, (
            "the containment detector reported nothing even against a "
            "deliberately-too-small frame — it is not actually comparing bounds"
        )


@pytest.mark.asyncio
async def test_configured_start_state_opens_with_both_gutters_hidden() -> None:
    """Tier 2b: ``chat.gutters.{left,right}: false`` is honoured at MOUNT —
    the pane opens with both gutters hidden and the body already holding the
    full terminal width, with no key press involved. This is the persistence
    decision made observable: the config sets the START state, and the runtime
    key toggle (session-scoped) is what changes it afterwards."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, config=_config(left=False, right=False))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await transport.push(_started("op-cfg"))
        await pilot.pause()
        flow = app._flow
        assert flow.left_gutter_visible is False
        assert flow.right_gutter_visible is False
        assert flow.body_width == flow.region.width

        # The runtime toggle still works from the configured-off state, and
        # brings back exactly the configured width.
        await pilot.press(RIGHT_KEY)
        await pilot.pause()
        assert flow.right_gutter_visible is True
        assert flow.body_width == flow.region.width - RIGHT_GUTTER_WIDTH


@pytest.mark.asyncio
async def test_a_runtime_toggle_never_writes_back_to_the_config() -> None:
    """Tier 2b: the persistence decision, stated as a gate — toggling from the
    keyboard leaves ``chat.gutters`` exactly as configured. A keypress in a
    chat pane must not silently rewrite the operator's stated preference; the
    live state lives on the widget for the session and nowhere else."""
    config = _config(left=True, right=True)
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, config=config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press(RIGHT_KEY)
        await pilot.press(LEFT_KEY)
        await pilot.pause()
        flow = app._flow
        assert flow.left_gutter_visible is False
        assert flow.right_gutter_visible is False
    assert config.chat.gutters == GutterConfig(left=True, right=True)


# ── Strip-falsify: each behavioural gate above is load-bearing ────────────────


@pytest.mark.asyncio
async def test_width_recovery_gate_is_load_bearing_against_a_dead_action() -> None:
    """Tier 2b: the non-vacuity strip for the width-recovery gates — with the
    app's ``action_toggle_right_gutter`` body stripped to a no-op (the
    production call site that reaches flowview), the SAME key press leaves
    ``body_width`` and ``right_gutter_effective_width`` untouched, so those
    gates go RED.

    This strips the WIRING, not the mechanism: flowview's toggle still works,
    but nothing reyn-side calls it — the exact "reachable as an API, not as an
    operation" defect shape #3352 exists to close. The failure is caught and
    re-asserted as this test's own expectation so the suite stays green."""
    originals = (
        TextualChatApp.action_toggle_right_gutter,
        TextualChatApp.action_toggle_left_gutter,
    )
    TextualChatApp.action_toggle_right_gutter = lambda self: None  # type: ignore[assignment]
    TextualChatApp.action_toggle_left_gutter = lambda self: None  # type: ignore[assignment]
    try:
        transport = QueueTransport()
        presenter = _WidthRecordingPresenter()
        app = TextualChatApp(transport=transport, presenter=presenter)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await transport.push(_started("op-strip"))
            await pilot.pause()
            flow = app._flow
            before = flow.body_width
            seen_before = len(presenter.widths)
            await pilot.press(RIGHT_KEY)
            await pilot.press(LEFT_KEY)
            await pilot.pause()
            assert flow.body_width == before, (
                "a stripped toggle action still changed the body width — the "
                "width-recovery gates are not measuring the reyn-side wiring"
            )
            assert flow.right_gutter_effective_width == RIGHT_GUTTER_WIDTH
            assert flow.left_gutter_effective_width == _GUTTER_WIDTH
            # The reflow gate falls with them: nothing re-presents at a wider
            # width, because nothing changed the width.
            assert set(presenter.widths[seen_before:]) <= {before}
    finally:
        (
            TextualChatApp.action_toggle_right_gutter,  # type: ignore[assignment]
            TextualChatApp.action_toggle_left_gutter,  # type: ignore[assignment]
        ) = originals


@pytest.mark.asyncio
async def test_configured_start_state_gate_is_load_bearing_against_an_ignored_config() -> None:
    """Tier 2b: the non-vacuity strip for the configured-start-state gate —
    neutralising the resolved start state the app carries into ``compose``
    (``_gutter_start``, the value ``_configured_gutter_visibility`` produced
    from ``chat.gutters``) makes the SAME config-off app mount with both
    gutters VISIBLE, so that gate goes RED.

    Anchor uniqueness was checked before choosing it: ``_gutter_start`` is
    assigned exactly once and read exactly once in ``app.py``. Stripping it
    changes behaviour, so it is the live path — not a second, shadowed
    declaration."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, config=_config(left=False, right=False))
    app._gutter_start = (True, True)  # the strip
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        flow = app._flow
        assert flow.left_gutter_visible is True, (
            "compose ignored the resolved start state even when it was stripped "
            "— the configured-start-state gate is not load-bearing"
        )
        assert flow.right_gutter_visible is True
        assert flow.body_width == flow.region.width - _GUTTER_WIDTH - RIGHT_GUTTER_WIDTH
