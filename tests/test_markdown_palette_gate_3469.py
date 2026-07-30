"""#3469 — markdown palette gate: no rich DEFAULT_STYLES colour reaches the
screen from LLM-output markdown.

rich ships ``markdown.h2 = "underline magenta"``, ``markdown.item.number =
"cyan"``, … — colours from a different world than the app palette. Any
``markdown.*`` key NOT overridden resolves to that default, which is how
#3326's single-key fix (``markdown.code`` only) left H2/H3 headings rendering
in neon magenta (owner design review, #3469). The class-closing fix is
``renderer.CHAT_MARKDOWN_THEME_STYLES`` — the complete family derived from the
palette in one place, consumed by BOTH chat surfaces (the Textual app's
console push and the plain renderers' Console constructors).

The gate here renders a representative markdown sample through a Console
carrying that theme and walks the emitted ANSI: every foreground must be
either a palette truecolor or the terminal default. A future rich default
leaking through (a new ``markdown.*`` key, a changed default) emits a basic
ANSI colour code or a foreign truecolor and goes RED — the N+1th instance
fires the gate instead of shipping.

Fenced code blocks are deliberately OUT of the sample: they render through
``rich.syntax.Syntax`` (monokai), an intentional self-contained world with its
own colours, not a leak.

Falsification (performed while landing #3469): constructing the Console
WITHOUT the theme makes ``##``/``###`` emit the magenta basic-colour code and
the numbered-list ``cyan`` — both RED here.
"""
from __future__ import annotations

import re
from io import StringIO

from rich.console import Console

from reyn.interfaces.repl.renderer import (
    _CC_ACCENT,
    _CC_COOL,
    _CC_DIM,
    _CC_DONE,
    _CC_ERR,
    _CC_WARN,
    _body_renderable,
    chat_markdown_theme,
)

#: Markdown exercising every style family the theme overrides (headings 1-4,
#: bullets, numbered list, quote, inline code, link, hr) — fenced code
#: deliberately absent (see module docstring).
_SAMPLE = """\
# Heading one
## Heading two
### Heading three
#### Heading four
- bullet one
- bullet two
1. first
2. second
> a block quote
Body with `inline_code` and a [link](https://example.com).
---
"""

_PALETTE_RGB = {
    tuple(int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    for c in (_CC_DIM, _CC_DONE, _CC_ERR, _CC_WARN, _CC_ACCENT, _CC_COOL)
}

#: Foreground ANSI codes that are NOT the terminal default and NOT truecolor:
#: basic (30-37), bright (90-97), and 256-colour (38;5;N) forms. rich emits a
#: NAMED default colour ("magenta", "cyan", …) as one of these even on a
#: truecolor console — so their presence IS the leak signature.
_BASIC_FG = re.compile(r"\x1b\[[0-9;]*\b(?:3[0-7]|9[0-7])(?:;|m)")
_EIGHT_BIT_FG = re.compile(r"\x1b\[[0-9;]*38;5;\d+")
_TRUECOLOR_FG = re.compile(r"38;2;(\d+);(\d+);(\d+)")


def _render_ansi(*, themed: bool) -> str:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=80,
        highlight=False,
        theme=_memo_cleared_theme() if themed else None,
    )
    console.print(_body_renderable("agent", _SAMPLE, ""))
    return buf.getvalue()


def _memo_cleared_theme() -> "object":
    """``chat_markdown_theme()`` with every Style's memoized SGR cleared.

    rich (measured on 15.0.0, ``style.py`` ``_make_ansi_codes``) memoizes a
    Style's rendered escape on FIRST render without keying it by the
    console's ``color_system`` — and ``Style.parse`` is ``lru_cache``d
    process-wide, so the instances in this theme are shared with every other
    test in the same pytest process. Any earlier test that rendered the same
    style string on a lower-colour console bakes the DOWNGRADED escape into
    the shared instance (``#6b7280`` → bright_black ``'90'``), and this
    gate's truecolor console then re-emits it — a false leak that flaps with
    xdist test ordering (the #3472 CI-only red; reproduced deterministically
    in ``test_gate_measures_the_theme_not_another_consoles_downgrade_memo``).
    Clearing the memo (a rich-private field, acknowledged — no public reset
    exists: ``copy()`` and ``+ Style()`` both preserve or alias it) makes the
    gate measure what a real single-colour-system terminal process sees. A
    REAL terminal process is unaffected by the upstream bug: it has one
    colour system, so the memo is always computed for the system it serves.

    **Upstream status — deliberately NOT reported (owner decision).** This is a
    bug in rich, not in reyn and not in litellm: ``Style._make_ansi_codes``
    caches the rendered SGR in ``self._ansi`` and never keys that cache by
    ``color_system``. It is recorded here rather than filed upstream, so this
    docstring is the only place the mechanism lives — keep it accurate.

    **When can this helper go away?** Run this against the installed rich; it
    is the whole decision, and it needs no reyn code::

        from io import StringIO
        from rich.console import Console
        from rich.style import Style

        s = Style.parse("#6b7280")
        Console(file=StringIO(), force_terminal=True,
                color_system="standard").print("x", style=s)
        out = StringIO()
        Console(file=out, force_terminal=True,
                color_system="truecolor").print("x", style=s)
        print(repr(out.getvalue()))

    On rich 15.0.0 this prints ``'\x1b[90mx\x1b[0m\n'`` — the truecolor
    console re-emitting the *standard* console's memoized escape, i.e. the bug
    is present and this helper is still load-bearing. Once it prints an
    ``\x1b[38;2;107;114;128m`` sequence instead, rich keys the memo per colour
    system, and then: this function collapses to a plain
    ``chat_markdown_theme()`` call, and
    :func:`test_gate_measures_the_theme_not_another_consoles_downgrade_memo`
    (which only guards against the poisoning, and passes either way) loses its
    reason to exist. Do not delete either one on a version bump alone — run the
    snippet, because the fix could land in any release.
    """
    theme = chat_markdown_theme()
    for style in theme.styles.values():  # type: ignore[attr-defined]
        style._ansi = None
    return theme


def test_markdown_sample_emits_only_palette_or_default_foregrounds() -> None:
    """Tier 2: every foreground colour the markdown sample emits is a palette
    truecolor or the terminal default — no rich DEFAULT_STYLES colour leaks."""
    ansi = _render_ansi(themed=True)
    assert ansi.strip(), "test setup: the sample rendered to nothing"

    def _contexts(pattern: "re.Pattern[str]") -> "list[str]":
        """±40 chars of raw output around each match — names WHICH sample
        element leaked, so a red run localizes the leak instead of only
        reporting that one exists (a CI-only red is otherwise undebuggable
        from the assertion message alone)."""
        return [
            repr(ansi[max(0, m.start() - 40):m.end() + 40])
            for m in pattern.finditer(ansi)
        ]

    basic = _BASIC_FG.findall(ansi)
    diag = ""
    if basic:  # CI-only so far — have the red run report its own resolution
        import os
        from importlib.metadata import version as _pkg_version

        from rich.rule import Rule

        probe = Console(
            file=StringIO(), force_terminal=True, color_system="truecolor",
            width=80, highlight=False, theme=chat_markdown_theme(),
        )
        hr_style = probe.get_style("markdown.hr", default="none")
        probe.print(Rule(style=hr_style, characters="-"))
        rule_ansi = probe.file.getvalue()
        env = {k: os.environ.get(k) for k in ("TERM", "NO_COLOR", "COLORTERM", "TTY_COMPATIBLE", "FORCE_COLOR")}
        diag = (
            f" Diag: rich={_pkg_version('rich')} "
            f"color_system={probe.color_system} hr_style={hr_style!r} "
            f"fresh_rule_ansi={rule_ansi[:120]!r} env={env}"
        )
    assert not basic, (
        f"basic/bright ANSI foreground code(s) {basic} reached the screen — "
        "a named rich default colour (magenta/cyan/...) leaked past the theme. "
        f"Context: {_contexts(_BASIC_FG)}{diag}"
    )
    assert not _EIGHT_BIT_FG.findall(ansi), (
        "a 256-colour foreground reached the screen — not a palette colour"
    )
    foreign = {
        (int(r), int(g), int(b))
        for r, g, b in _TRUECOLOR_FG.findall(ansi)
        if (int(r), int(g), int(b)) not in _PALETTE_RGB
    }
    assert not foreign, (
        f"truecolor foreground(s) outside the app palette reached the screen: "
        f"{sorted(foreign)}"
    )


def test_gate_measures_the_theme_not_another_consoles_downgrade_memo() -> None:
    """Tier 2: the #3472 CI-only red, made deterministic and pinned.

    rich memoizes a Style's rendered SGR on first render WITHOUT keying it by
    colour system, and ``Style.parse`` shares instances process-wide — so a
    LOWER-colour console anywhere in the process rendering this theme's style
    strings poisons the shared memo with a downgraded escape (``#6b7280`` →
    bright_black ``'90'``), which a later truecolor render re-emits verbatim.
    That is what made this gate red only on CI, flapping with xdist ordering.
    This test performs that poisoning DELIBERATELY, then asserts the themed
    render still emits palette truecolor — i.e. the gate measures reyn's
    theme, not whichever console in the process happened to render first.
    Falsified: without ``_memo_cleared_theme``'s reset, the poisoned render
    emits exactly the CI signature (``\\x1b[90m`` on the horizontal rule)."""
    poison = Console(
        file=StringIO(), force_terminal=True, color_system="standard",
        width=80, highlight=False,
    )
    from reyn.interfaces.repl.renderer import CHAT_MARKDOWN_THEME_STYLES

    for definition in CHAT_MARKDOWN_THEME_STYLES.values():
        poison.print("x", style=definition)

    ansi = _render_ansi(themed=True)
    basic = _BASIC_FG.findall(ansi)
    assert not basic, (
        f"a poisoned cross-console Style memo leaked into the themed render: "
        f"{basic} — the gate is measuring another console's downgrade, not "
        "the theme"
    )


def test_gate_is_not_vacuous_untthemed_render_goes_red() -> None:
    """Tier 2: non-vacuity — the SAME sample WITHOUT the theme trips the gate
    (rich's own defaults emit the magenta/cyan the themed assertion forbids),
    so a silently-dropped theme cannot keep this file green."""
    ansi = _render_ansi(themed=False)
    assert _BASIC_FG.findall(ansi) or _EIGHT_BIT_FG.findall(ansi), (
        "the untamed rich defaults no longer emit any non-palette colour — "
        "if rich's defaults changed, re-derive this gate's leak signature"
    )


def test_reasoning_bold_markers_render_as_bold_not_literal_asterisks() -> None:
    """Tier 2: #3469 — ``**...**`` in reasoning text becomes a bold span; the
    raw asterisks (previously shown verbatim every turn) never reach the
    screen. Unpaired ``**`` stays literal (never silently swallowed)."""
    themed = Console(
        file=StringIO(), force_terminal=True, color_system="truecolor", width=80,
    )
    body = _body_renderable("reasoning", "a **Section Title** b", _CC_DIM)
    text = body.plain if hasattr(body, "plain") else str(body)
    assert "**" not in text, f"literal ** reached the reasoning body: {text!r}"
    assert "Section Title" in text

    bold_spans = [s for s in body.spans if "bold" in str(s.style)]
    assert bold_spans, "the marked section did not become a bold span"

    unpaired = _body_renderable("reasoning", "a ** b", _CC_DIM)
    assert unpaired.plain == "a ** b", "an unpaired ** was swallowed"
    del themed
