"""The single place the inline CUI names a colour.

Every colour and emphasis decision the TUI makes is a token here, and every
widget's ``DEFAULT_CSS`` interpolates one rather than writing a value. Checking
what the interface paints is then reading this file, not sweeping for whatever
syntax a value happened to take.

That distinction is not theoretical. A census that grepped for ``$var NN%``
missed ``solid #3d434f`` — the hex sits behind a border keyword, so the pattern
never matched it — and a second census over ``$text-muted`` missed
``background: $accent 30%``, which is what an operator then reported as an
unreadable green block. Both were found only by enumerating the VALUES instead
of matching an expectation about their shape. Enumerating them is what this
module makes unnecessary: ``test_tui_colour_tokens.py`` fails if any colour is
written outside it.

**Terminal-owned meanings stay with the terminal.** Where the terminal already
has a meaning — its default foreground and background — the token names that
rather than pinning a value the user's theme is entitled to choose. Emphasis is
carried by SGR attributes (``dim``, ``bold``) for the same reason: under the
``ansi-*`` themes ``$text`` and ``$text-muted`` both resolve to the
``ansi_default`` marker, and alpha compositing DROPS that marker — so
``$text 40%`` and ``$accent 30%`` do not fade anything, they either do nothing
or paint a solid colour. An attribute leaves the hue to the terminal and
survives the merge.
"""
from __future__ import annotations

#: Every colour the inline CUI names, as ``@token@`` -> value. Widgets write
#: the marker and pass their sheet through :func:`css`; nothing else in
#: ``interfaces/`` may write a value.
#:
#: The marker syntax is deliberate. Textual CSS already uses ``$`` for its own
#: variables and ``{}`` for selectors, so neither an f-string nor ``%``
#: formatting can carry a template through untouched — and a token published as
#: an app-level CSS variable would make every widget depend on being mounted
#: inside the reyn app, which is not true of the tests that mount them alone.
#: ``@name@`` collides with nothing and resolves at class-definition time.
TOKENS: "dict[str, str]" = {
    #: Raised surfaces: drawer, completion popup, search bar, rewind picker,
    #: status line. The one place the CUI departs from the terminal's ground,
    #: so a panel reads as a distinct layer rather than as more conversation.
    "@surface@": "$panel",
    #: De-emphasised text — hints, placeholders, secondary labels.
    "@quiet@": "$text-muted",
    #: Something is waiting on the operator (the intervention panel's border
    #: and its prompt). The only semantic colour the CUI claims; anything else
    #: that must stand out uses an attribute instead.
    "@attention@": "$warning",
    #: The app's ground. ``ansi_default`` is the marker meaning "whatever the
    #: terminal's background is" — not a colour, deliberately.
    "@app-background@": "ansi_default",
    #: Hairline rules around the input row. A concrete value because it must
    #: read as a divider against BOTH terminal grounds; a theme variable would
    #: follow the theme and vanish into one of them.
    "@rule@": "#3d434f",
    #: Text that should recede: a count the interface produced, a heading that
    #: labels a region. An SGR ATTRIBUTE, not a colour — under the ansi themes
    #: ``$text-muted`` resolves to the same ``ansi_default`` marker as ordinary
    #: text, so it recedes by exactly nothing (measured on #3522/#3528). ``dim``
    #: leaves the hue to the terminal, which is the operator's to choose, and
    #: actually changes what is drawn.
    "@recede@": "dim",
    #: The selected row in a list the operator navigates (the sent-queue).
    #: ``bold`` rather than a background: a filled block is what
    #: ``$accent 30%`` produced once alpha was dropped — a solid ANSI-green bar
    #: under default-coloured text — and a full-row inversion was rejected on
    #: the conversation for the same reason (#3490: the mark has to be
    #: CONTENT). Widgets pair this with the row's OWN glyph filling in
    #: (``sent_queue``: ``▷`` unselected, ``▶`` selected), so the content half
    #: of that pairing costs no column and selection stays legible with every
    #: attribute stripped. Until #3777 the content half was a separate ``▸``
    #: in a column of its own; the requirement is unchanged, only what carries
    #: it. Pairing an attribute with SOMETHING in the text is the invariant —
    #: this token alone is not a selection mark.
    "@selected-style@": "bold",
    #: The drag-selection band. ``ansi_blue`` rather than Textual's default
    #: ``ansi_bright_blue``: the operator found the bright frame too loud
    #: against the conversation (#3542). Both are ANSI FRAMES, not colours —
    #: what blue looks like is the terminal theme's decision and stays that
    #: way; this only chooses which of the sixteen slots to ask for.
    "@selection-bg@": "ansi_blue",
    #: The text inside that band. Unchanged from Textual's default, and named
    #: here so the pair is legible in one place: a background chosen without
    #: its foreground beside it is how contrast regressions happen.
    "@selection-fg@": "ansi_black",
}

#: The two endpoints of the NOW row's travelling shine (#3777). Blended per
#: character at runtime by ``activity_row``, so these are plain values rather
#: than ``@name@`` markers: :func:`css` resolves markers inside a stylesheet
#: string, and the band is not a stylesheet \u2014 it is applied to a content span
#: via ``Content.stylize`` on every frame.
#:
#: RGB rather than ANSI-16. "a turn is running, and here is the light moving
#: through it" has no established colour convention the way red-means-error
#: does, which is precisely the case the CLAUDE.md carve-out (owner,
#: 2026-08-07) opens: a reyn-specific meaning may take a value outside
#: ANSI-16. That carve-out's condition is that the value still be NAMED here
#: rather than written inline in the widget \u2014 which is what these two are.
#: Naming them here also keeps them inside the thing this module promises:
#: reading this file tells you what the interface paints. A hex computed in
#: ``activity_row`` would paint a colour ``test_tui_colour_tokens.py`` cannot
#: see, because that gate reads CSS declarations and a runtime span is not
#: one \u2014 the same "written in a shape nobody searched for" that put the gate
#: here in the first place.
#:
#: Only the band's INTERIOR is painted. Where the cosine falls to nothing the
#: character is left unstyled rather than blended toward an assumed
#: background, so the shine fades into whatever ground the terminal actually
#: has instead of into a grey reyn guessed. That is the same reason
#: ``@app-background@`` is ``ansi_default``: the ground is the operator's.
SHINE_DIM = "#5c6478"
SHINE_PEAK = "#e6ecf8"


def css(sheet: str) -> str:
    """Resolve ``@token@`` markers in a widget stylesheet.

    Raises on an unknown marker rather than leaving it in place: Textual would
    reject the whole sheet for the unparseable value, and one blank interface
    is a poor way to learn about a typo.
    """
    import re as _re

    unknown = {
        m for m in _re.findall(r"@[a-z-]+@", sheet) if m not in TOKENS
    }
    if unknown:
        raise ValueError(
            f"unknown palette token(s) {sorted(unknown)} — declare them in "
            f"palette.TOKENS or fix the spelling"
        )
    for marker, value in TOKENS.items():
        sheet = sheet.replace(marker, value)
    return sheet


__all__ = ["SHINE_DIM", "SHINE_PEAK", "TOKENS", "css"]
