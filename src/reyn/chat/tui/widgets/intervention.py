"""InterventionWidget — inline chip-based ask_user prompt.

Mounts inside ConversationView (not a modal dialog). The user can click
a chip button or type a free-text answer. Either path calls the registered
`answer_callback`.

Design: soft-tinted box with coral border. Chips are Textual Buttons.
Free-text fallback uses a one-line Input that appears when the user
selects "free response".
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label


class InterventionWidget(Widget):
    """Inline intervention prompt with chip buttons.

    Args:
        question:        The prompt text shown to the user.
        choices:         Chip definitions. Each entry is either a legacy
                         ``(label, choice_id)`` 2-tuple or a dict with
                         ``{"label", "id", "hotkey"?, "default"?}``. When the
                         list is empty, only a free-text input is shown.
        answer_callback: Coroutine called with the user's answer string.
        iv_id:           Intervention ID (for logging / targeting).
        queued_extra:    Number of additional pending interventions waiting
                         behind this one. When > 0, a dim ``+N more pending``
                         label is rendered in the widget header so the user
                         has a persistent signal that more questions follow.
    """

    DEFAULT_CSS = """
    InterventionWidget {
        background: #1e1510;
        border: solid $primary;
        padding: 1 2;
        height: auto;
        margin: 1 0;
        color: #eeddcc;
    }
    InterventionWidget Label.iv-question {
        color: #ffcc88;
        text-style: bold;
        padding-bottom: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget .iv-chips {
        layout: horizontal;
        height: auto;
        margin-top: 0;
    }
    InterventionWidget Button {
        /* Wave-6 IV4: tight chip sizing so all 5 buttons fit on a single
           row even when ``Ctrl+B`` opens the right panel and cuts the
           conv pane width roughly in half. ``min-width: 6`` is the
           shortest hotkey-format label (``"[y]es"``) + 1-cell breathing
           room; ``padding: 0`` removes the previous 2-cell side padding.
           Width is ``auto`` so longer labels (``"[A]lways"``,
           ``"free response…"``) expand to fit their text without
           forcing the shorter chips wider. Inter-chip gap drops from
           ``margin: 0 1 0 0`` to a single space between labels
           (= preserved by the existing 1-cell padding-x). Net width
           budget at default labels: ~42 cells (= ``5 + 8 + 4 + 7 +
           14 + 4-cell gap``), fits comfortably in a 50-cell narrow
           pane. */
        margin: 0 1 0 0;
        background: $primary;
        color: #ffffff;
        border: none;
        padding: 0;
        height: 1;
        width: auto;
        min-width: 6;
    }
    InterventionWidget Button:hover {
        background: #e0664e;
    }
    InterventionWidget Input {
        margin-top: 1;
        background: #2a1a10;
        border: solid $primary;
        color: #ffffff;
        height: 1;
    }
    InterventionWidget Label.iv-hint {
        /* #888888 on the widget bg (#1e1510) is ~4.1:1, close to WCAG AA
           4.5:1 for normal text; the prior #555555 was 2.41:1 (clearly
           sub-AA). The hint is secondary copy but rendered at body size,
           so the AA floor applies. */
        color: #888888;
        padding-top: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget Label.iv-queued {
        color: #888888;
        text-style: dim;
        padding-bottom: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget Label.iv-detail {
        /* Issue #163: detail is secondary copy beneath the prompt.
           #aaaaaa on #1e1510 is ~6.3:1 (passes WCAG AA for body text).
           Italic + slightly muted color separates the "what is being
           asked" header from the "what specific resource" detail. */
        color: #aaaaaa;
        text-style: italic;
        padding-bottom: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget Label.iv-source-agent {
        /* Issue #261: parent-delegation breadcrumb. Dim, no italic — the
           prompt itself is the load-bearing line; this is context. */
        color: #888888;
        text-style: dim;
        height: auto;
        width: 1fr;
    }
    """

    class Answered(Message):
        """Posted when the user answers the intervention."""
        def __init__(self, iv_id: str, answer: str) -> None:
            super().__init__()
            self.iv_id = iv_id
            self.answer = answer

    def __init__(
        self,
        *,
        question: str,
        choices: list[tuple[str, str] | dict] | None = None,
        answer_callback: Callable[[str], Awaitable[None]] | None = None,
        iv_id: str = "",
        queued_extra: int = 0,
        detail: str | None = None,
        source_agent: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id or f"iv_{iv_id[:8]}")
        self._question = question
        # Issue #163: detail is the secondary line (= "web fetch: <url>")
        # rendered with the ``iv-detail`` CSS class beneath the prompt.
        # None / empty skips the Label entirely so old callers that
        # don't supply detail keep the prior single-line layout.
        self._detail = detail
        # Issue #261: when the ``parent_delegate`` branch fires on an
        # upstream agent, the OutboxMessage carries ``source_agent``
        # naming that agent. Render as ``[parent: <name>]`` above the
        # prompt so the user can tell "this question came down from a
        # parent agent" vs "the attached agent asked directly". Omitted
        # entirely when None (= no delegation chain), matching the
        # outbox meta opt-in shape.
        self._source_agent = source_agent
        # Normalise both legacy 2-tuples and richer dict shapes into a single
        # internal list-of-dicts so compose() / hotkey lookup stays uniform.
        self._choices: list[dict[str, Any]] = [
            self._normalise_choice(c) for c in (choices or [])
        ]
        self._answer_callback = answer_callback
        self._iv_id = iv_id
        self._queued_extra = max(0, int(queued_extra))
        self._show_input = not self._choices  # no chips → always show free text

    @staticmethod
    def _normalise_choice(raw: tuple[str, str] | dict) -> dict[str, Any]:
        """Coerce one choice entry into ``{label, id, hotkey, default}``.

        Accepts the legacy ``(label, id)`` 2-tuple (no hotkey, not default)
        and the richer dict carrying ``hotkey`` / ``default``. Missing keys
        on the dict path fall back to safe blanks so a caller that only
        supplies label + id keeps working.
        """
        if isinstance(raw, dict):
            return {
                "label": raw.get("label", ""),
                "id": raw.get("id", ""),
                "hotkey": raw.get("hotkey") or "",
                "default": bool(raw.get("default", False)),
            }
        # Tuple path — historical shape is (label, id); we ignore extras
        # defensively in case a caller widened it.
        label = raw[0] if len(raw) > 0 else ""
        choice_id = raw[1] if len(raw) > 1 else ""
        return {"label": label, "id": choice_id, "hotkey": "", "default": False}

    @staticmethod
    def _format_chip_label(label: str, hotkey: str) -> str:
        """Render a chip label, avoiding a duplicate ``[h]`` prefix.

        The producer-side factories in ``reyn.intervention_choices`` embed the
        hotkey in the label itself (``"[y]es"``, ``"[A]lways"``, …) so the CLI
        renderer can show the hint without consulting the separate ``hotkey``
        field. When that same choice flows into the TUI chip we don't want to
        double-prefix into ``"[y] [y]es"``. If the label already starts with
        ``[<hotkey>]`` (case-sensitive — hotkeys are case-sensitive), pass it
        through unchanged; otherwise prepend ``[h] `` so callers that supply
        a bare label still get a visible hint.
        """
        if not hotkey:
            return label
        if label.startswith(f"[{hotkey}]"):
            return label
        return f"[{hotkey}] {label}"

    def compose(self) -> ComposeResult:
        # ``markup=False`` on the question Label and ``Text(...)`` wrapping
        # on chip Buttons keeps the literal "[y]" / "[A]" hotkey brackets
        # visible. Default markup parsing treats them as unknown style
        # tags and silently strips them — chip labels were rendering as
        # "es", "lways", "o", "ever" with no hotkey hint.
        if self._source_agent:
            # Issue #261: source-agent badge for delegated prompts. Dim
            # so the eye lands on the question first; the badge is
            # context, not the prompt itself.
            yield Label(
                f"  [parent: {self._source_agent}]",
                classes="iv-source-agent",
                markup=False,
            )
        yield Label(
            f"  {self._question}", classes="iv-question", markup=False,
        )
        if self._detail:
            # Issue #163: render detail as a separate Label with the
            # ``iv-detail`` class (italic + muted color) so the user can
            # visually parse "what is being asked" vs "what specific
            # resource is involved".
            yield Label(
                f"  {self._detail}", classes="iv-detail", markup=False,
            )
        if self._queued_extra > 0:
            yield Label(
                f"  +{self._queued_extra} more pending",
                classes="iv-queued",
                markup=False,
            )
        if self._choices:
            with Widget(classes="iv-chips"):
                for choice in self._choices:
                    label = choice["label"]
                    hotkey = choice["hotkey"]
                    display = self._format_chip_label(label, hotkey)
                    variant = "primary" if choice["default"] else "default"
                    yield Button(
                        Text(display),
                        id=f"chip_{choice['id']}",
                        variant=variant,
                    )
                # "free response" chip always at end
                yield Button(
                    Text("free response…"),
                    id="chip__free",
                    variant="default",
                )
            # Hint reflects the keyboard-first chip nav landed by the
            # wave-6 IV bundle: the first chip is auto-focused on mount,
            # so single-key hotkeys (``y`` / ``A`` / ``n`` / ``N``) fire
            # directly without the user having to Ctrl+O hunt for chip
            # focus. ``Tab`` cycles chips, ``Enter`` / ``Space`` activate,
            # ``Ctrl+C`` abandons the prompt entirely.
            yield Label(
                "hotkey · Tab cycles · Ctrl+C cancels · free response… for free text",
                classes="iv-hint",
                markup=False,
            )
        else:
            yield Input(placeholder="type your answer…", id="iv_input")

    def on_mount(self) -> None:
        """Auto-focus the first chip so chip hotkeys fire without Ctrl+O.

        Before this, the InputBar's TextArea held focus by default and
        consumed printable keys (``y`` / ``A`` / ``n`` / ``N``) into the
        editor buffer before they could bubble up to ``on_key``. Users
        either had to discover the undocumented ``Ctrl+O`` shortcut to
        cycle focus to the chip area, or click chips with the mouse.

        Focusing the first chip on mount means the chip area is
        keyboard-active immediately: single-key hotkeys (= the chip's
        ``[y]``-style mnemonic) match through the existing ``on_key``
        handler, ``Tab`` cycles between chips, ``Enter`` / ``Space``
        activates. The "free response…" chip is reachable by Tab and
        continues to mount its own ``Input`` field via
        ``_show_free_input`` when pressed.

        Free-text answer path is unchanged for the no-chip case (=
        ``Input`` widget at the bottom of ``compose``, still
        auto-focused there); only the chip case gains keyboard access.
        """
        if not self._choices:
            return
        try:
            first = self.query("Button").first()
        except Exception:
            return
        if first is None:
            return
        try:
            first.focus()
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        btn_id = event.button.id or ""
        if btn_id == "chip__free":
            self._show_free_input()
            return
        # Find the choice whose chip_ prefix matches, then submit its
        # *hotkey* — InterventionRegistry.deliver_answer routes through
        # match_choice() which compares against hotkey, NOT id. Sending
        # the id (e.g. "yes" instead of "y") silently fails the match,
        # leaves iv.future unresolved, and the widget removes itself —
        # the agent then waits forever for an answer the user already
        # gave. Fall back to the id only when the choice has no hotkey
        # (= producer-side anomaly; user gets the "unknown choice" hint
        # instead of a silent deadlock).
        choice_id = btn_id.removeprefix("chip_")
        choice = next(
            (c for c in self._choices if c["id"] == choice_id),
            None,
        )
        if choice is None:
            return
        hotkey = choice["hotkey"]
        await self._submit(hotkey if hotkey else choice_id)

    async def on_key(self, event) -> None:  # textual.events.Key
        """Activate a chip when its hotkey is pressed.

        Skipped while an Input widget has focus — otherwise a user typing a
        free answer that starts with a hotkey letter would be intercepted.
        Match is case-sensitive, mirroring the producer-side ``match_choice``
        contract in ``user_intervention.py``.
        """
        try:
            focused = self.app.focused
        except Exception:
            focused = None
        if isinstance(focused, Input):
            return
        key_char = getattr(event, "character", None) or getattr(event, "key", "")
        if not key_char or len(key_char) != 1:
            return
        for choice in self._choices:
            if choice["hotkey"] and choice["hotkey"] == key_char:
                event.stop()
                # Submit the hotkey (= what match_choice expects) rather
                # than the id — same deadlock concern as the chip-click
                # path. See on_button_pressed for the full rationale.
                await self._submit(choice["hotkey"])
                return

    def _show_free_input(self) -> None:
        """Show a text input for free-form answer."""
        # Try to mount an Input after the chips area
        try:
            existing = self.query_one("#iv_input")
            existing.focus()
            return
        except Exception:
            pass
        inp = Input(placeholder="type your answer…", id="iv_input")
        self.mount(inp)
        inp.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # Wave-9 E-F2: reject empty / whitespace-only answers. Without
        # this guard, an accidental Enter on the blank field dispatched
        # ``""`` to ``deliver_answer`` — which then either silently
        # mismatched ``match_choice`` (= agent stuck waiting for a real
        # answer) or routed an empty string into the conversation. The
        # Input retains focus + placeholder so the user can type and
        # submit for real.
        if not event.value.strip():
            return
        await self._submit(event.value)

    async def _submit(self, answer: str) -> None:
        """Deliver the answer and post Answered message."""
        if self._answer_callback is not None:
            try:
                await self._answer_callback(answer)
            except Exception:
                pass
        self.post_message(self.Answered(iv_id=self._iv_id, answer=answer))
        # Remove ourselves from the conversation view
        self.remove()
        # Restore focus to the input bar. Without an explicit call here
        # Textual's auto-focus-walker picks the next focusable widget in
        # DOM order on removal — often a non-editable widget (a child of
        # ConversationView, or the right panel if it's open), which
        # leaves the user typing into nothing. The input bar is the only
        # widget that the user can keep going from.
        try:
            from .input_bar import InputBar  # late import → avoid cycle
            self.app.query_one("#inputbar", InputBar).focus_input()
        except Exception:
            pass
