"""The single place ``interfaces/`` names a colour.

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

**Colour meanings map to the Textual theme, not the terminal (owner ruling,
#4840, 2026-08-16).** Deferring to the terminal's own default colours — an
``ansi_default``/``ansi_blue``/``ansi_black`` marker naming one of its
sixteen slots rather than an RGB value — made sense only while reyn had no
theme of its own to defer to instead: "端末の既定色に従う意味は、テーマを
採用した時点で消えます" (owner, verbatim). ``@app-background@`` maps to
Textual's own ``$background`` under this ruling — the active theme decides
the RGB, not the terminal. ``@selection-bg@``/``@selection-fg@`` name the
SAME retired premise but are NOT remapped yet — landing them ahead of reyn's
own default theme would regress #3542 (measured: ``ansi-dark``'s own
``$screen-selection-background`` is the loud ``ansi_bright_blue`` #3542
moved away from), so lead-coder's ruling sequences them after that theme
exists (see the tokens' own comments below). ``@rule@`` stays a literal hex
for an unrelated reason (documented on the token itself): it must read as a
divider against both of the app's OWN light/dark grounds, which a theme
variable would just vanish into on one of them — that is a THEME question,
never a terminal one, and was never in this paragraph's scope.

Emphasis is still carried by SGR attributes (``dim``, ``bold``), which this
ruling does not reach — ``dim``/``bold`` are not colours, ANSI or otherwise,
they are text-style attributes the terminal (or Textual's own ANSI-to-
truecolor filter) applies on top of whatever colour is already chosen. Under
a full-colour theme, ``$text``/``$text-muted`` resolve to concrete RGB, so
``$text-muted`` alone is no longer the "recedes by exactly nothing" case
#3522/#3528 measured under the ``ansi-*`` themes — but ``@recede@``/
``@telemetry@`` stay ``dim`` regardless, because an attribute still changes
what is drawn on top of a concrete colour the same way it always did, and
introduces no new failure mode by staying.

**Location (#4787, lead-coder ruling):** lives directly under
``interfaces/``, not inside ``interfaces/inline/textual_chat/`` where it
started — that package's own ``__init__.py`` eagerly imports ``textual``/
``textual_flowview`` through its submodules ("must only ever be imported on
the TTY path", its own docstring), and Python always runs a package's
``__init__.py`` before any of its submodules, so ANY import of
``textual_chat.palette`` — even a direct one, even though this module itself
has zero framework imports — paid that cost regardless. Verified directly:
``'textual' in sys.modules`` was ``False`` after importing
``interfaces.repl.renderer`` alone, ``True`` the moment
``textual_chat.palette`` was imported. ``interfaces/repl/renderer.py`` (the
plain/``--cui``/non-TTY chat path, which must stay usable without
``flowview`` installed) needs these same token values without paying that
cost — this module has none of its own for the SAME reason it never did:
``interfaces/__init__.py`` is docstring-only.
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
    #: The app's ground. Was ``ansi_default`` ("whatever the terminal's
    #: background is") until #4840's owner ruling retired that deferral —
    #: reyn now has its own theme to defer to instead, so this maps to
    #: Textual's own semantic token for exactly this role (``Screen``'s own
    #: ``DEFAULT_CSS`` uses the identical ``background: $background;``).
    #: The active theme's RGB shows through; no terminal slot is named.
    "@app-background@": "$background",
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
    #: #4542: the status bar's Telemetry segment (model/agent/cost/context%) —
    #: "one step weaker than Navigation's own normal intensity" (owner's
    #: proposal), so the row reads as two visually distinct halves without a
    #: literal separator. A DEDICATED token, not a reuse of ``@recede@``
    #: (same underlying SGR value today, but a distinct semantic role — a
    #: heading/count receding and the Telemetry segment's own steady-state
    #: tone are different concepts that happen to share a mechanism; keeping
    #: them separate here means changing one's value later can't silently
    #: move the other). ``dim``, not ``@quiet@``/``$text-muted``, for the
    #: same reason ``@recede@`` isn't a colour either: under the ``ansi-*``
    #: themes ``$text-muted`` resolves to the same ``ansi_default`` marker as
    #: ordinary text (#3522/#3528's own measurement) — using it here would
    #: make Telemetry read IDENTICAL to Navigation under exactly the themes
    #: this redesign's "visually distinct halves" goal most needs to survive.
    "@telemetry@": "dim",
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
    #:
    #: #4840 (owner ruling, 2026-08-16) retires this deferral in principle —
    #: see ``@app-background@`` above, mapped already — but NOT here yet.
    #: Mapping straight to Textual's own ``$screen-selection-background``
    #: would regress #3542 under the CURRENT default theme: ``ansi-dark``'s
    #: own value for that token is ``ansi_bright_blue`` (measured), the exact
    #: loud one #3542 moved away from. lead-coder's ruling (#4840): land
    #: ``@app-background@`` now (no such regression there — measured), build
    #: reyn's own default theme next, and land this token + #3542's test
    #: THEN — a theme `variables` shim on `ansi-dark` was explicitly rejected
    #: ("1つのテーマの欠点を、全テーマに効く層で埋める" — the opposite of
    #: mapping to theme meaning). Whether reyn's own theme preserves #3542's
    #: quiet choice is undecided — read #3542 when that theme is designed.
    "@selection-bg@": "ansi_blue",
    #: The text inside that band. Unchanged from Textual's default, and named
    #: here so the pair is legible in one place: a background chosen without
    #: its foreground beside it is how contrast regressions happen. Same
    #: #4840 deferral as ``@selection-bg@`` above — not mapped yet.
    "@selection-fg@": "ansi_black",
    #: #4787: the first 5 of ``interfaces/repl/renderer.py``'s 8 former
    #: ``_CC_*`` hex constants — the ones whose value #4787's own
    #: classification found reusable AS-IS (no new colour chosen; the
    #: MEANING decision was the whole exercise, per CLAUDE.md's "Never pick
    #: a colour first"). Consumed DIRECTLY as Python values
    #: (``palette.TOKENS["@success@"]``) by both ``renderer.py`` (the plain
    #: REPL, ``rich.Text(style=...)``, no live Textual App to resolve a
    #: ``$token`` against — #4840's own open question) and
    #: ``presenter.py``/``gutter.py`` (the TUI, same values, same reason: no
    #: reyn Textual theme exists yet to resolve ``$success`` etc. against).
    #: NOT yet ``@name@``-embedded in any stylesheet string — these values
    #: are plain dict lookups here, the marker-in-CSS mechanism above
    #: doesn't apply to this batch.
    #:
    #: Each is expected to become a Textual token reference (``$success``,
    #: ``$error``, ``$warning``, ``$accent``, ``$secondary``) once #4840's
    #: reyn theme module exists — this is the meaning-assignment half of
    #: that migration, landing first per lead-coder's own ordering (①
    #: classify+move, ② broaden the gate — #4851, ③ relocate palette out
    #: of textual_chat's TTY-only import boundary — #4857, ④ the colour
    #: direction itself, on hold for the owner).
    "@success@": "#7ee787",     # green — completion (was _CC_DONE)
    "@error@": "#f97066",       # red — failure (was _CC_ERR)
    #: Textual's own state-palette meaning (gutter.py's own comment: "RUNNING
    #: amber, SUCCESS green, ERROR coral"), covering BOTH "in progress" and
    #: "needs you" — broader in scope than @attention@ above, which stays
    #: scoped to its own one widget (the intervention panel's border/prompt)
    #: and is UNCHANGED by this addition. Textual's own vocabulary has one
    #: amber-family token (``$warning``) for both meanings, so the two reyn
    #: tokens are expected to converge on the SAME eventual Textual value
    #: without being the same token — they answer different "why is this
    #: amber" questions even when the colour ends up equal.
    "@warning@": "#e3b341",     # amber — state-palette + SESSION HALTED (was _CC_WARN)
    "@accent@": "#d97757",      # terracotta — primary interactive accent (was _CC_ACCENT)
    "@secondary@": "#6cb6ff",   # blue — secondary/markdown accent (was _CC_COOL)
    #: The 6th of the 8 — moved alone, NOT together with ``_CC_USER_BG``/
    #: ``_CC_ERR_BG`` (renderer.py's own remaining two), which stay blocked
    #: on #4840's colour-direction question. Safe to move independently:
    #: this changes WHERE the value is declared, never the value itself
    #: (``#6b7280``, unchanged) — renderer.py's own comment on the
    #: constant this replaces documents a WCAG-measured contrast PAIRING
    #: against ``_CC_USER_BG`` (#3371: 3.30 at this exact value, below WCAG
    #: AA-large's 3.0 threshold at the prior one); since neither value
    #: changes, only the declaration site, that measured pairing is
    #: untouched. Low-importance/ambient is its own clear meaning (unlike
    #: the backgrounds, which double as candidates for #4840's
    #: still-undecided ``background``/``panel`` — see #4787's own comment
    #: thread), so this one didn't need to wait for that ruling.
    #:
    #: **★changing this value breaks renderer.py's own measured 3.30
    #: contrast ratio against ``_CC_USER_BG`` there** (architect finding,
    #: #4787) — the two halves of that ONE measurement now live in
    #: different files, since ``_CC_USER_BG`` itself stays put pending
    #: #4840. Re-measure BOTH sides together, not just this one, if either
    #: changes.
    #:
    #: **Unlike the 5 above, this one does NOT become a Textual token
    #: reference once #4840's theme exists** — it stays a permanently
    #: CONCRETE value. renderer.py's own comment on the constant this
    #: replaces documents two independent reasons: ``prompt_toolkit``
    #: rejects an SGR-only style outright (``fg:dim`` raises
    #: ``ValueError``, measured), and #3367's contrast gate "skips any
    #: segment whose foreground is not concrete" — a ``$token`` reference
    #: resolved at Textual-theme time would be invisible to both.
    "@dim@": "#6b7280",  # low-importance / ambient, as a COLOUR (was _CC_DIM)
}

#: The NOW row's travelling shine (#3777), as a GROUND and a PEAK per terminal
#: ground. Blended per character at runtime by ``activity_row``, so these are
#: plain values rather than ``@name@`` markers: :func:`css` resolves markers
#: inside a stylesheet string, and the band is not a stylesheet — it is applied
#: to a content span on every frame.
#:
#: RGB rather than ANSI-16. "a turn is running, and here is the light moving
#: through it" has no established colour convention the way red-means-error
#: does, which is precisely the case the CLAUDE.md carve-out (owner,
#: 2026-08-07) opens: a reyn-specific meaning may take a value outside
#: ANSI-16. The carve-out's condition is that the value still be NAMED here
#: rather than written inline in the widget — which is what these four are.
#:
#: **The GROUND is painted across every character, not just the band's edge.**
#: The first cut painted only the band and left the rest at the terminal's own
#: foreground, reasoning that the shine should fade into the real ground rather
#: than into a grey reyn guessed. That produced a dark cell at each end of the
#: band sitting directly against an undimmed ground, and the operator read the
#: result as THREE things moving instead of one (compared against another tool's
#: single travelling highlight). Respecting the terminal's ground does not mean
#: leaving a high-contrast colour next to it. A uniform ground with one bright
#: band over it is what reads as a single light.
#:
#: Two pairs because one cannot work on both grounds: a near-white peak is
#: invisible on a white terminal. The rule for both is the same — the PEAK is
#: the high-contrast end against that terminal's background and the GROUND sits
#: between the two, so the band always moves away from the background rather
#: than toward it.
#: The waiting blink (#3860 follow-up): the pulse shown while the effect's frame
#: cache is still being built.
#:
#: Same two-pair shape as the shine below, for the same reason — one pair cannot
#: work on both grounds, and the PEAK is the high-contrast end against that
#: terminal's background. The pulse runs PEAK -> GROUND -> PEAK, so the text
#: fades toward the background and returns rather than blinking on and off:
#: a hard on/off reads as a fault, a slow breath reads as work in progress.
#:
#: RGB rather than ANSI-16 by the operator's own ruling for this surface. There
#: is no conventional colour for "a cache is being built", and sixteen colours
#: carry no midpoints to interpolate through — the fade IS the message here, so
#: dropping to ANSI would not dim the effect, it would delete it.
BLINK_PEAK_DARK = "#cdd6f4"
BLINK_GROUND_DARK = "#2b3040"
BLINK_PEAK_LIGHT = "#242a38"
BLINK_GROUND_LIGHT = "#c9cfdd"

SHINE_GROUND_DARK = "#5c6478"
SHINE_PEAK_DARK = "#e6ecf8"
SHINE_GROUND_LIGHT = "#9aa1b1"
SHINE_PEAK_LIGHT = "#1b2130"


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


__all__ = [
    "SHINE_GROUND_DARK",
    "SHINE_GROUND_LIGHT",
    "SHINE_PEAK_DARK",
    "SHINE_PEAK_LIGHT",
    "TOKENS",
    "css",
]
