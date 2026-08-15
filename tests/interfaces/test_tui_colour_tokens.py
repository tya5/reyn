"""Tier 1: contract — the inline CUI names colours in exactly one file.

Three censuses of the TUI's colours each missed something, and the second
reached an operator as an unreadable green block. The first grepped for
``$var NN%`` and did not see ``solid #3d434f`` — the hex sits behind a border
keyword, so the pattern never matched it. The second grepped for
``$text-muted`` and did not see ``background: $accent 30%``. The third
(#4787) found a THIRD shape neither prior census's pattern could see at
all: ``_CC_DIM = "#6b7280"`` — a Python assignment, not a CSS declaration, so
``_DECLARATION``'s ``prop: value;`` shape never matches it regardless of the
property list. All three were found only by enumerating the VALUES rather
than matching an expected shape — the discipline this gate exists to remove
the need for, extended here to cover the shape the gate itself had missed.

Enumerating is what this gate removes the need for. Every colour lives in
``palette.py`` and every widget writes a ``$reyn-*`` token, so "what does this
interface paint" is one file, and a value written anywhere else fails here
rather than waiting for someone to grep for the right syntax.

Scope is reyn's own stylesheets under ``interfaces/``. Textual's own
``DEFAULT_CSS`` is out of scope — reyn does not own it, and #3525 tracks the
places it collides with the ansi themes. ``interfaces/web/`` is ALSO out of
scope for the Python-hex-literal check specifically (#4787): it serves a
browser, not a terminal — the whole premise this policy is built on ("the
terminal emulator's theme decides, reyn names the meaning") has no terminal
to defer to there, so a hex literal in ``web/`` (e.g. ``web_data.py``'s own
``_COLOR_CYCLE``) is not the same kind of decision this gate polices. The
CSS-declaration check above it is unaffected — ``web/`` has no ``.tcss``
files or Textual stylesheets to begin with.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.interfaces import palette
from tests._support.paths import REPO_ROOT

_INTERFACES = REPO_ROOT / "src" / "reyn" / "interfaces"

#: A colour-bearing CSS declaration: the property names that take one, and the
#: value up to the semicolon. ``border``/``outline`` are included because their
#: value carries a colour behind a style keyword — the shape the first census
#: missed.
_DECLARATION = re.compile(
    r"\b(background|color|tint|border|outline)([a-z-]*): *([^;\n]+);"
)

#: Values that name no colour, so a rule using one is not a colour decision.
_COLOURLESS = frozenset({"none", "transparent", "auto", "initial", "hidden"})

#: A bare hex-colour string literal — ``"#6b7280"``/``'#3a1c1a'``, 6 or 8 hex
#: digits (RGB or RGBA). #4787's own shape: a Python assignment, never a CSS
#: declaration, so ``_DECLARATION`` cannot see it regardless of which
#: property names it lists. Matches the LITERAL itself, not a reference to
#: one — ``f"italic {_CC_DIM}"`` (a later USE of an already-declared name)
#: does not match this pattern; only the assignment that first wrote the hex
#: string does.
#:
#: Known limit (lead-coder review, non-blocking): this is a LITERAL-string
#: pattern, not a value-flow analysis — ``"#" + "6b7280"`` or a hex string
#: built from a variable/expression is invisible to it, the same class of
#: gap ``_DECLARATION`` above already has for CSS. A green run here says "no
#: bare hex-string literal", not "no hex value anywhere" — enumerating the
#: actual escape shapes worth guarding is a separate, future exercise if one
#: is ever found in practice, not a promise this pattern already makes.
_PY_HEX_LITERAL = re.compile(r"""(['"])#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\1""")


def _declarations_only(sheet: str) -> str:
    """``sheet`` with its CSS comments removed.

    Block-aware rather than line-aware: a continuation line inside a ``/* */``
    comment carries no marker of its own, so a per-line test reads it as a
    declaration. Every site this module guards explains itself in a comment
    that names the very token it stopped using, so telling prose from rules
    has to be exact or the reasoning gets driven out of the files.
    """
    return re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)


def _stylesheet_lines() -> "list[tuple[Path, int, str]]":
    """Every line of reyn-owned source under ``interfaces/``, with its origin.

    Enumerated from the filesystem rather than a list of known widgets, so a
    new widget is covered the moment it lands.
    """
    out: "list[tuple[Path, int, str]]" = []
    for path in sorted(_INTERFACES.rglob("*.py")) + sorted(_INTERFACES.rglob("*.tcss")):
        if path.name == "palette.py":
            continue  # the one file allowed to hold values
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            out.append((path, number, line))
    return out


def _colour_values() -> "list[str]":
    """Every colour value declared outside ``palette.py``, as ``path:line -> value``."""
    offenders: "list[str]" = []
    for path, number, line in _stylesheet_lines():
        stripped = line.strip()
        # Skip prose: comment lines, and doc/rationale text that quotes CSS.
        if stripped.startswith(("#", "*", "/*")) or "``" in line:
            continue
        for match in _DECLARATION.finditer(line):
            value = match.group(3).strip()
            tokens = [t for t in value.split() if t not in _COLOURLESS]
            for token in tokens:
                if token.startswith("@") and token.endswith("@"):
                    continue  # a palette marker — the point of the exercise
                if token in _COLOURLESS or token in {"solid", "round", "dashed", "tall", "wide", "heavy", "outer", "inner", "thick"}:
                    continue
                if re.fullmatch(r"\d+", token):
                    continue  # a width, not a colour
                offenders.append(f"{path.relative_to(_INTERFACES)}:{number} -> {token}")
    return offenders


#: Directory names, relative to ``_INTERFACES``, whose top-level component is
#: excluded from :func:`_python_hex_literals` specifically — see the module
#: docstring for why ``web`` (no terminal to defer to) doesn't belong to this
#: check. The CSS-declaration check above (:func:`_colour_values`) is NOT
#: filtered by this — it never matched anything under ``web/`` in the first
#: place (no ``.tcss``/Textual stylesheets there), so there is nothing to
#: exempt it FROM.
_HEX_LITERAL_EXEMPT_DIRS = frozenset({"web"})


#: File names (relative to ``_INTERFACES``) already KNOWN to hold Python hex
#: literals as of #4787's own gate-broadening commit — tracked, not silently
#: exempted. #4787's own migration (moving these 8 constants' MEANING
#: assignment into ``palette.py``, per lead-coder's ordering: ①classify+move
#: ②broaden THIS gate) had not finished landing when the broadened gate did,
#: so enforcing immediately here would fail CI on sites already scheduled to
#: move rather than on a NEW regression. This allowance is scoped to the ONE
#: file #4787 measured (``renderer.py``, 8 constants / 66 call sites) — a NEW
#: hex literal anywhere else still fails immediately, which is this gate's
#: whole point. Remove entries as each constant actually moves; the file
#: drops out entirely once all 8 do.
_PY_HEX_LITERAL_TRACKED = {
    "repl/renderer.py": "#4787 — 8 constants, meaning-classification done, "
                        "migration to palette.py in progress",
}


def _python_hex_literals() -> "list[str]":
    """Every bare hex-colour string literal in a ``.py`` file outside
    ``palette.py`` (and outside :data:`_HEX_LITERAL_EXEMPT_DIRS`), as
    ``path:line -> value`` — #4787's own shape, which :func:`_colour_values`
    cannot see (its ``_DECLARATION`` regex requires a CSS ``prop: value;``
    shape; a Python assignment like ``_CC_DIM = "#6b7280"`` has neither a
    recognised property name nor a trailing semicolon)."""
    offenders: "list[str]" = []
    for path, number, line in _stylesheet_lines():
        if path.suffix != ".py":
            continue  # a .tcss hit is already a _colour_values() offender
        if path.relative_to(_INTERFACES).parts[0] in _HEX_LITERAL_EXEMPT_DIRS:
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "*", "/*")) or "``" in line:
            continue  # prose: comments, and doc/rationale text quoting a value
        for match in _PY_HEX_LITERAL.finditer(line):
            offenders.append(f"{path.relative_to(_INTERFACES)}:{number} -> {match.group(0)}")
    return offenders


def test_the_gate_is_looking_at_real_stylesheets() -> None:
    """Tier 1: setup — the enumeration finds the widgets it is meant to check.

    A glob that matched nothing would make the assertion below vacuously true,
    which is the exact shape the two failed censuses had.
    """
    lines = _stylesheet_lines()

    assert any("sent_queue.py" in str(p) for p, _, _ in lines)
    assert any("@quiet@" in line or "@surface@" in line for _, _, line in lines), (
        "no widget references a palette token — the tokens are not in use"
    )


def test_no_colour_is_named_outside_the_palette() -> None:
    """Tier 1: every colour value in reyn's own CSS is a ``$reyn-*`` token."""
    offenders = _colour_values()

    assert not offenders, (
        "colour values declared outside palette.py — add a token there and "
        "reference it instead:\n" + "\n".join(f"  {o}" for o in offenders)
    )


def test_no_python_hex_literal_is_declared_outside_the_palette() -> None:
    """Tier 1: #4787 — the shape this gate could not see until now.

    ``_colour_values``'s ``_DECLARATION`` regex requires a CSS ``prop:
    value;`` shape and never matched ``_CC_DIM = "#6b7280"`` (a Python
    assignment, no property name, no trailing semicolon) — the exact
    reason 8 hex constants across 66 call sites in ``interfaces/repl/
    renderer.py`` sat outside this gate's reach since #3525's own arc
    closed. A second, independent regex for the Python shape, rather than
    trying to widen ``_DECLARATION`` to cover both — the two shapes share
    no syntax to unify around (one needs a semicolon and a property name,
    the other needs neither), and conflating them would make either
    pattern's own failure harder to read.

    ``_PY_HEX_LITERAL_TRACKED`` exempts the sites #4787's own measurement
    already found and is actively migrating — a NEW site anywhere else
    still fails here, which is the actual regression this test guards."""
    offenders = [
        o for o in _python_hex_literals()
        if not any(o.startswith(f"{tracked}:") for tracked in _PY_HEX_LITERAL_TRACKED)
    ]

    assert not offenders, (
        "Python hex-colour literals declared outside palette.py — add a "
        "token there and reference it instead:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_the_tracked_hex_literal_allowance_stays_current() -> None:
    """Tier 1: ``_PY_HEX_LITERAL_TRACKED`` names files that genuinely still
    hold a Python hex literal — a real gap the main test above cannot
    catch on its own, since a stale entry there makes that test MORE
    likely to pass, not less (the same "is the allowance still true"
    question ``_QUIET_ONLY_ALLOWED``'s own sibling test in this file asks)."""
    offenders = _python_hex_literals()
    seen_files = {o.split(":")[0] for o in offenders}

    stale = sorted(set(_PY_HEX_LITERAL_TRACKED) - seen_files)
    assert not stale, (
        "these files no longer hold a Python hex literal — drop the "
        f"tracked allowance rather than leaving a reason for something "
        f"that is not there: {stale}"
    )


def test_every_marker_a_widget_uses_is_declared() -> None:
    """Tier 1: no widget references a token the palette does not define.

    An undeclared marker survives into the stylesheet as an unparseable value,
    and Textual rejects the WHOLE sheet for one bad value — so a typo here is a
    blank interface, not a wrong colour. ``palette.css`` raises on it instead;
    this asserts the widgets are clean today.
    """
    used: "set[str]" = set()
    for path, _, line in _stylesheet_lines():
        if path.suffix != ".py":
            continue
        used |= set(re.findall(r"@[a-z-]+@", line))

    undeclared = used - set(palette.TOKENS)

    assert not undeclared, f"markers used but not declared: {sorted(undeclared)}"


def test_an_unknown_marker_is_rejected_rather_than_left_in_place() -> None:
    """Tier 1: ``palette.css`` fails loudly on a token it does not know."""
    import pytest

    with pytest.raises(ValueError, match="unknown palette token"):
        palette.css("Foo { color: @not-a-real-token@; }")


def test_resolution_leaves_no_marker_behind() -> None:
    """Tier 1: a resolved sheet carries values, not markers."""
    resolved = palette.css("Foo { background: @surface@; color: @quiet@; }")

    assert "@" not in resolved
    assert palette.TOKENS["@surface@"] in resolved


#: Where ``@quiet@`` is the only thing declared, and why that is tolerated.
#:
#: Under the ansi themes ``$text-muted`` — which ``@quiet@`` resolves to —
#: lands on the same ``ansi_default`` marker as body text, so these sites do
#: not visibly recede there. #3523 measured all of them and the conclusion was
#: to leave them: each is already told apart by something that is not colour,
#: and #3686 fixed the two where nothing else was doing the work. What the
#: colour still buys is the non-ansi themes, where it resolves to a real value.
#:
#: The entry is the FILE plus what actually carries the distinction, so adding
#: a name here means stating that answer out loud.
_QUIET_ONLY_ALLOWED = {
    "app.py": "position: the ❯ gutter, the status row and the menu row are "
              "separated from the conversation by where they are and by the "
              "rule above them; the active tab additionally carries bold",
    "intervention_panel.py": "the pane it labels is bordered and headed in "
                             "@attention@, so the detail line reads as detail",
    "rewind_picker.py": "the heading carries text-style: @recede@ (#3686)",
}


def test_a_new_site_cannot_quietly_rely_on_quiet_alone() -> None:
    """Tier 2: a file that newly reaches for ``@quiet@`` has to say why.

    ``@quiet@`` reads like "make this quieter" and, under reyn's default
    theme, does not. #3523 measured seven chrome sites where the intent was
    therefore lost with nothing failing, and judged — per site, not as a rule —
    that each was already distinguished by something else. That judgement is
    only safe for the sites it was made about.

    An eighth arrived the day after (``ActivityRow``, #3693): the declaration
    looked right, read right in review, and did nothing. Nothing caught it
    because a one-off audit can only cover what existed when it ran. This is
    that audit as a standing gate — not a ban, since the colour still resolves
    to a real value under the non-ansi themes, but a requirement that a new
    site names what carries the distinction when it does not.
    """
    # DECLARATIONS only, via the same prose skip ``_colour_values`` uses. A
    # plain substring search over the file reads the comments too — and the
    # comments this very change leaves behind explain, by name, the token the
    # site stopped declaring. First run, the gate accused its own outcome:
    # ``activity_row.py`` was reported for the sentence recording that its
    # ``color: @quiet@`` had been REMOVED. A gate that cannot tell a rule from
    # prose about a rule forces the reasoning out of the files.
    seen = set()
    for path, _number, line in _stylesheet_lines():
        stripped = line.strip()
        if stripped.startswith(("#", "*", "/*")) or "``" in line:
            continue
        if "@quiet@" in line:
            seen.add(path.name)

    undeclared = sorted(seen - set(_QUIET_ONLY_ALLOWED))
    assert not undeclared, (
        "these files use @quiet@, which resolves to the same value as body "
        "text under reyn's default theme — so whatever is meant to recede "
        "there does not. Either give it text-style: @recede@ (an SGR "
        "attribute, which survives), or add it to _QUIET_ONLY_ALLOWED naming "
        f"what else tells it apart: {undeclared}"
    )

    stale = sorted(set(_QUIET_ONLY_ALLOWED) - seen)
    assert not stale, (
        "these files no longer use @quiet@ — drop the allowance rather than "
        f"leaving a reason for something that is not there: {stale}"
    )
