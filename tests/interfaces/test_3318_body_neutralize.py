"""Tier 1/2: #3318 — opt-in ESC/OSC neutralize on the agent-reply/tool-result
BODY text (owner ruling B: default OFF, opt-in via ``chat.neutralize_body``).

#3302 fixed LLM-derived CHOICE LABELS / intervention prompts (unconditional,
``presenter._neutralized_label``) but left the conversation BODY text — the
agent reply and tool-result content itself — unneutralized on both the TUI
presenter and the plain (``--cui``) renderer. This closes that gap, opt-in
(owner: "UX/predictability over security, security is opt-in").

Three points the issue named to nail down before landing (quoted in the
issue body) map to the sections below:
  ① markdown safety — a real ESC/control byte does not survive, and real
     markdown (heading/bold/code fence) is NOT corrupted by the strip.
  ② same strength as the #3302 label-side neutralizer.
  ③ live AND restore render through the identical function — no branch on
     the restore marker.
Plus the required off/on witness pair per surface (accept-side: off is a
true no-op; reject-side: on actually strips).
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text
from textual_flowview import FlowModel

from reyn.config.chat import ChatConfig, _build_chat_config
from reyn.config.root import ReynConfig
from reyn.core.present.guard import get_neutralizer
from reyn.interfaces.cli.logger_factory import make_chat_renderer
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.presenter import _body_and_background
from reyn.interfaces.inline.textual_chat.restore import RESTORED_META_KEY
from reyn.interfaces.repl import renderer as renderer_module
from reyn.interfaces.repl.renderer import (
    ConsoleChatRenderer,
    _body_renderable,
    format_inline_message,
)
from reyn.runtime.outbox import OutboxMessage
from tests._support.textual_chat_test_helpers import QueueTransport

_RAW_ESC = "\x1bdanger\x1b[31m"  # a bare ESC + a CSI (color) sequence


def _render_to_text(renderable) -> str:
    """Render any Rich renderable to a plain string via a no-color Console —
    needed for the ``agent`` kind's Markdown object, which has no ``.plain``."""
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=100).print(renderable)
    return buf.getvalue()


# ── config parsing ────────────────────────────────────────────────────────


def test_neutralize_body_defaults_off() -> None:
    """Tier 1: ChatConfig.neutralize_body defaults False (owner ruling B)."""
    assert ChatConfig().neutralize_body is False


def test_chat_neutralize_body_parses_true() -> None:
    """Tier 1: `chat.neutralize_body: true` in reyn.yaml parses through."""
    assert _build_chat_config({"neutralize_body": True}).neutralize_body is True


def test_chat_neutralize_body_absent_stays_default() -> None:
    """Tier 1: falsification pair — omitting the key keeps the False default
    (this flag does not accidentally flip on for any other chat: block)."""
    assert _build_chat_config({"render_mode": "plain"}).neutralize_body is False


# ── _body_renderable: off/on witness (①③ base, ②) ──────────────────────────


def test_body_renderable_off_is_a_true_passthrough() -> None:
    """Tier 2: accept-side control — with neutralize_body=False (the default),
    a raw ESC/control byte survives verbatim. Proves "off" isn't secretly
    neutralizing anyway (the reject-side test below is what makes this a
    real pair, not a vacuous accept)."""
    body = _body_renderable("status", _RAW_ESC, "dim", neutralize_body=False)
    assert isinstance(body, Text)
    assert "\x1b" in body.plain


def test_body_renderable_on_strips_control_bytes() -> None:
    """Tier 2: reject-side — neutralize_body=True strips the raw ESC/CSI
    sequence from a plain (non-markdown) kind's body."""
    body = _body_renderable("status", _RAW_ESC, "dim", neutralize_body=True)
    assert isinstance(body, Text)
    assert "\x1b" not in body.plain
    assert "danger" in body.plain


def test_body_renderable_matches_the_3302_label_neutralizer_strength() -> None:
    """Tier 2: ② same strength as #3302's label-side fix — the body path
    must not invent a weaker or stronger filter than
    ``get_neutralizer("terminal")``, the exact primitive
    ``presenter._neutralized_label`` uses for choice labels."""
    raw = "line1\x00\x07" + _RAW_ESC + "line2\x9f"
    expected, _ = get_neutralizer("terminal").neutralize(raw)
    body = _body_renderable("status", raw, "dim", neutralize_body=True)
    assert body.plain == expected


def test_body_renderable_reasoning_kind_also_neutralizes() -> None:
    """Tier 2: the ``reasoning`` kind (bold-marked Text, a THIRD code path
    inside _body_renderable distinct from markdown/plain-Text) also strips —
    the flag is applied once, before the per-kind dispatch, not per-branch."""
    body = _body_renderable(
        "reasoning", f"**Thinking**{_RAW_ESC}", "dim", neutralize_body=True
    )
    assert "\x1b" not in body.plain


# ── ① markdown safety: the strip must not corrupt real markdown ───────────


def test_body_renderable_agent_kind_strips_esc_without_corrupting_markdown() -> None:
    """Tier 2: ① — for kind="agent" (the ONLY kind that parses markdown), a
    control byte embedded in otherwise-valid markdown is removed, and the
    markdown around it still renders (bold span survives) — the strip
    operates on bytes CommonMark never assigns meaning to, so it cannot
    corrupt heading/bold/fence syntax it runs before parsing."""
    raw = f"# Heading\n\nSome **bold**{_RAW_ESC} text and a list:\n\n- one\n- two\n"
    md = _body_renderable("agent", raw, "text", neutralize_body=True)
    rendered = _render_to_text(md)
    assert "\x1b" not in rendered
    assert "Heading" in rendered
    assert "bold" in rendered  # survived the bold span
    assert "one" in rendered and "two" in rendered  # list items survived


def test_body_renderable_agent_kind_code_fence_content_untouched_by_stripping() -> None:
    """Tier 2: ① — content INSIDE a fenced code block has no control bytes in
    this fixture (a real ESC there would still be stripped — the neutralizer
    runs before markdown parsing sees the fence at all, so it cannot special-
    case "inside a fence"); this pins that ordinary code-fence TEXT is
    reproduced verbatim when there is nothing to strip, i.e. the neutralize
    pass introduces no incidental corruption of fenced content."""
    raw = "```python\ndef f():\n    return 1\n```\n"
    md = _body_renderable("agent", raw, "text", neutralize_body=True)
    rendered = _render_to_text(md)
    assert "def f():" in rendered
    assert "return 1" in rendered


# ── format_inline_message: forwards the flag (no live prod caller — see its
#    own docstring — but pinned as a direct unit contract) ─────────────────


def test_format_inline_message_forwards_neutralize_body() -> None:
    """Tier 2: format_inline_message threads neutralize_body through to the
    shared _body_renderable seam for an ordinary (non-user/presentation/
    intervention-nodes) kind."""
    msg = OutboxMessage(kind="status", text=_RAW_ESC)
    off = _render_to_text(format_inline_message(msg, neutralize_body=False))
    on = _render_to_text(format_inline_message(msg, neutralize_body=True))
    assert "\x1b" in off
    assert "\x1b" not in on
    assert "danger" in on


# ── ConsoleChatRenderer: the actual LIVE plain-renderer surface (writes
#    msg.text RAW, no _body_renderable pass — wired at its own call site) ──


def _capture_stdout(monkeypatch) -> io.StringIO:
    captured = io.StringIO()
    monkeypatch.setattr(renderer_module.sys, "__stdout__", captured)
    return captured


def test_console_renderer_off_by_default_is_a_true_passthrough(monkeypatch) -> None:
    """Tier 2: accept-side — ConsoleChatRenderer() with no neutralize_body
    (the compat default) writes the raw ESC byte verbatim, unchanged from
    before #3318. Guards against #3318 silently becoming the new default."""
    out = _capture_stdout(monkeypatch)
    r = ConsoleChatRenderer()
    r.message(OutboxMessage(kind="agent", text=_RAW_ESC))
    assert "\x1b" in out.getvalue()


def test_console_renderer_on_strips_the_raw_body(monkeypatch) -> None:
    """Tier 2: reject-side — ConsoleChatRenderer(neutralize_body=True) strips
    the raw ESC/CSI sequence from the body text actually written to the
    terminal. This is the method's own RAW write path (no _body_renderable
    call at all), so this test is the only live witness for it."""
    out = _capture_stdout(monkeypatch)
    r = ConsoleChatRenderer(neutralize_body=True)
    r.message(OutboxMessage(kind="agent", text=_RAW_ESC))
    rendered = out.getvalue()
    assert "\x1b" not in rendered
    assert "danger" in rendered


# ── ReynPresenter / _body_and_background: the LIVE TUI surface, AND ③ the
#    live/restore parity — the SAME function, no branch on the restore
#    marker ─────────────────────────────────────────────────────────────


def test_body_and_background_off_by_default_is_a_true_passthrough() -> None:
    """Tier 2: accept-side — the TUI seam with neutralize_body=False (its
    own compat default) leaves a raw ESC byte in place."""
    body, _ = _body_and_background(OutboxMessage(kind="status", text=_RAW_ESC))
    assert "\x1b" in body.plain


def test_body_and_background_on_strips_the_body() -> None:
    """Tier 2: reject-side — the TUI seam strips when the flag is set."""
    body, _ = _body_and_background(
        OutboxMessage(kind="status", text=_RAW_ESC), neutralize_body=True
    )
    assert "\x1b" not in body.plain
    assert "danger" in body.plain


def test_body_and_background_restored_message_neutralizes_identically() -> None:
    """Tier 2: ③ — an OutboxMessage carrying the restore-path's own marker
    (``RESTORED_META_KEY``, the exact meta key ``restore.py`` stamps on every
    projected frame) neutralizes IDENTICALLY to a live one with the same
    kind/text. ``_body_and_background`` never reads this key, so live and
    restored frames of the same kind cannot diverge — this is the structural
    guarantee, exercised directly rather than by re-running the whole restore
    pipeline (which builds this exact OutboxMessage shape — see
    ``restore.py``'s own ``OutboxMessage(kind="agent", ..., meta={RESTORED_META_KEY: True})``
    construction)."""
    live = OutboxMessage(kind="agent", text=_RAW_ESC)
    restored = OutboxMessage(
        kind="agent", text=_RAW_ESC, meta={RESTORED_META_KEY: True}
    )
    live_body, _ = _body_and_background(live, neutralize_body=True)
    restored_body, _ = _body_and_background(restored, neutralize_body=True)
    live_text = _render_to_text(live_body)
    restored_text = _render_to_text(restored_body)
    assert "\x1b" not in live_text
    assert "\x1b" not in restored_text
    assert live_text == restored_text


# ── config → constructor wiring: chat.neutralize_body actually reaches the
#    live renderer/presenter, not just the isolated units above ─────────────


def test_make_chat_renderer_wires_config_flag_into_live_output(monkeypatch) -> None:
    """Tier 2: the CLI factory seam (``make_chat_renderer``, what
    ``reyn chat --cui`` / ``chat.render_mode: plain`` actually constructs)
    forwards ``neutralize_body`` all the way to the rendered/written output —
    not merely stored on an attribute nothing reads."""
    out = _capture_stdout(monkeypatch)
    r = make_chat_renderer(neutralize_body=True)
    r.message(OutboxMessage(kind="agent", text=_RAW_ESC))
    rendered = out.getvalue()
    assert "\x1b" not in rendered
    assert "danger" in rendered


@pytest.mark.asyncio
async def test_textual_chat_app_wires_config_neutralize_body_into_default_presenter() -> None:
    """Tier 2: ``TextualChatApp`` reads ``config.chat.neutralize_body`` off a
    REAL :class:`ReynConfig` (not a stub — the app reads the genuine
    dataclass chain, same idiom as
    ``test_textual_chat_gutter_toggle_3352.py``'s ``_config`` helper) into
    the DEFAULT presenter it builds when no ``presenter=`` is injected —
    exercised through the presenter's own public ``present()`` behavior
    (the rendered body), not by reading a private attribute."""
    config = ReynConfig(chat=ChatConfig(neutralize_body=True))
    app = TextualChatApp(transport=QueueTransport(), config=config)
    presentation = await app._presenter.present(
        FlowModel().append(OutboxMessage(kind="status", text=_RAW_ESC)), width=80
    )
    rendered = _render_to_text(presentation.renderable)
    assert "\x1b" not in rendered
    assert "danger" in rendered


@pytest.mark.asyncio
async def test_textual_chat_app_default_config_leaves_presenter_off() -> None:
    """Tier 2: falsification pair — with no config injected (the ``config=None``
    default), the default presenter stays off (compat), so the raw ESC byte
    survives. Guards against the wiring test above passing vacuously (e.g. a
    presenter that always neutralizes regardless of the flag)."""
    app = TextualChatApp(transport=QueueTransport())
    presentation = await app._presenter.present(
        FlowModel().append(OutboxMessage(kind="status", text=_RAW_ESC)), width=80
    )
    rendered = _render_to_text(presentation.renderable)
    assert "\x1b" in rendered
