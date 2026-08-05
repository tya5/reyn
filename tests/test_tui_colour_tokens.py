"""Tier 1: contract — the inline CUI names colours in exactly one file.

Two censuses of the TUI's colours each missed something, and the second miss
reached an operator as an unreadable green block. The first grepped for
``$var NN%`` and did not see ``solid #3d434f`` — the hex sits behind a border
keyword, so the pattern never matched it. The second grepped for
``$text-muted`` and did not see ``background: $accent 30%``. Both were found
only by enumerating the VALUES rather than matching an expected shape.

Enumerating is what this gate removes the need for. Every colour lives in
``palette.py`` and every widget writes a ``$reyn-*`` token, so "what does this
interface paint" is one file, and a value written anywhere else fails here
rather than waiting for someone to grep for the right syntax.

Scope is reyn's own stylesheets under ``interfaces/``. Textual's own
``DEFAULT_CSS`` is out of scope — reyn does not own it, and #3525 tracks the
places it collides with the ansi themes.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.interfaces.inline.textual_chat import palette

_INTERFACES = Path(__file__).resolve().parent.parent / "src" / "reyn" / "interfaces"

#: A colour-bearing CSS declaration: the property names that take one, and the
#: value up to the semicolon. ``border``/``outline`` are included because their
#: value carries a colour behind a style keyword — the shape the first census
#: missed.
_DECLARATION = re.compile(
    r"\b(background|color|tint|border|outline)([a-z-]*): *([^;\n]+);"
)

#: Values that name no colour, so a rule using one is not a colour decision.
_COLOURLESS = frozenset({"none", "transparent", "auto", "initial", "hidden"})


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


def test_nothing_asks_to_recede_with_a_colour_that_does_not() -> None:
    """Tier 2: ``@quiet@`` is not used as the way to make something quieter.

    Under the ansi themes ``$text-muted`` — which ``@quiet@`` resolves to —
    lands on the same ``ansi_default`` marker as ordinary text. #3523 measured
    seven chrome sites where "de-emphasised" therefore rendered at full body
    brightness: the intent was lost and nothing failed.

    That audit could only cover the sites that existed when it ran, and an
    eighth arrived the next day (``ActivityRow``, #3693) — a declaration that
    looked right, read right in review, and did nothing. So the audit becomes a
    gate. What replaces it per site is a judgement, not a rule: ``@recede@``
    where receding is the intent, and no declaration where it is not (the
    status row carries the ``⚠ HALTED`` banner, and a halt must never render
    dimmer than ordinary text).

    Comment text is skipped deliberately — the sites removed here say in prose
    what they used to declare, and a gate that could not tell the difference
    would force that reasoning out of the file.
    """
    offenders = []
    for path in sorted(_INTERFACES.rglob("*.py")) + sorted(_INTERFACES.rglob("*.tcss")):
        if path.name == "palette.py":
            continue  # where the token is DEFINED, which is not a use of it
        source = _declarations_only(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path.name}: {line.strip()}"
            for line in source.splitlines()
            if "@quiet@" in line
        ]
    assert not offenders, (
        "these declarations ask for de-emphasis with @quiet@, which resolves to "
        "the same value as body text under the ansi themes — use @recede@ (an "
        f"SGR attribute) or declare no colour: {offenders}"
    )
