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
    #: The selected row in a list the operator navigates (the sent-queue).
    #: ``bold`` rather than a background: a filled block is what
    #: ``$accent 30%`` produced once alpha was dropped — a solid ANSI-green bar
    #: under default-coloured text — and a full-row inversion was rejected on
    #: the conversation for the same reason (#3490: the mark has to be
    #: CONTENT). Widgets pair this with a marker in the row text.
    "@selected-style@": "bold",
}

#: The marker a selected row carries in its own text, so selection survives
#: without a colour at all. Not a CSS value — it is content.
SELECTED_MARKER = "\u25b8"


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


__all__ = ["SELECTED_MARKER", "TOKENS", "css"]
