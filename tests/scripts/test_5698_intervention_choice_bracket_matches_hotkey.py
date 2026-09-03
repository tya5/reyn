"""Tier 1: #5698 co-vet (lead-coder) — ``InterventionChoice.label`` and
``.hotkey`` are two INDEPENDENT hand-written strings (``label: str`` /
``hotkey: str | None``, ``src/reyn/user_intervention.py``), typed side by
side at every call site:

    InterventionChoice(id=YES, label="[y]es", hotkey="y")

Nothing in ``src/`` verified the two ever agreed. #4751/#5698 wired the
inline TUI's intervention panel to answer THIS field — ``hotkey`` — on a
keypress, and #5698's own review found a real, live mismatch already
sitting in ``src/reyn/runtime/limits/limit_handler.py`` (fixed in the same
PR this test lands with): ``label="[Y]es, continue", hotkey="y"``. The
panel now displays ``[Y]``, but the only key it actually answers to is
lowercase ``y`` — case-sensitively (``test_4751_intervention_panel_
hotkeys.py`` pins this: ``n``/``N`` are two DIFFERENT choices when both
sit in one set). A displayed bracket that names a key nothing answers to
is exactly the class of lie #4751/#5698/#5694 all closed a different
instance of tonight.

Scope (lead-coder's own explicit call): ``src/`` only — this is a
STRUCTURAL check on the values reyn ships, not a rewrite of the data
model (``label``/``hotkey`` stay independent fields; deriving one from
the other would touch 23 call sites plus the ``ask_user`` MCP-tool wire
projection, out of scope for this PR) and not a sweep of every
``tests/`` fixture that happens to reuse the SAME ``"[Y]es"``/``hotkey=
"y"`` shape (13 hits across the suite, all in
serialization/resume/roundtrip tests that never exercise the panel's
keypress-select behavior this gate is actually about — singling out one
of the 13 would be arbitrary; see the PR body for the explicit decision
not to touch them here).

Only LITERAL ``label=``/``hotkey=`` string pairs are checked (an
``ast.Constant`` on both) — the few call sites that build either from a
runtime value (``ask_user.py``'s ``f"[{i + 1}]"`` / ``str(i + 1)`` numeric
options, ``elicitation.py``'s mirror of the same shape,
``user_intervention.py``'s own ``from_dict`` deserializer reading
``c["label"]``/``c.get("hotkey")`` off already-persisted data) have no
independent literal pair a human could type out of sync with each other
— a static AST match on a non-constant expression cannot tell "matches"
from "doesn't", so those sites are correctly skipped rather than
producing a hollow pass/fail.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SRC_ROOT = REPO_ROOT / "src" / "reyn"


def _literal_str(node: "ast.expr | None") -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def find_intervention_choice_literal_pairs(
    root: Path,
) -> "list[tuple[Path, int, str, str]]":
    """Walk every ``.py`` file under ``root`` and return ``(file, lineno,
    label, hotkey)`` for every ``InterventionChoice(...)`` call whose
    ``label``/``hotkey`` keyword arguments are BOTH string literals.
    Real AST parse of the files as they exist on disk — never a
    hand-typed re-transcription of what the call sites currently say
    (mirrors ``test_5691_ci_diff_filter_excludes_deletions.py``'s own
    "read the real file, don't retype it" discipline)."""
    found: "list[tuple[Path, int, str, str]]" = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "InterventionChoice":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            label = _literal_str(kwargs.get("label"))
            hotkey = _literal_str(kwargs.get("hotkey"))
            if label is not None and hotkey is not None:
                found.append((path, node.lineno, label, hotkey))
    return found


def test_every_literal_pair_in_src_has_the_bracket_matching_the_hotkey():
    """Tier 1: the acceptance-item witness — accept side. Empty-set guard
    first (a walker that silently finds nothing would pass vacuously): a
    SPECIFIC known-real pair (``generic_yn_choices``'s own ``[y]es``/``y``,
    ``intervention_choices.py``) must be among the results — not a bare
    count (a magic number here would pin the CURRENT total, which grows
    every time a new call site is added, for no behavioral reason)."""
    pairs = find_intervention_choice_literal_pairs(_SRC_ROOT)
    found_labels = {(label, hotkey) for _path, _lineno, label, hotkey in pairs}
    assert ("[y]es", "y") in found_labels, (
        f"the walker did not find generic_yn_choices()'s own '[y]es'/'y' "
        f"pair (intervention_choices.py) among {len(pairs)} results — it "
        f"likely stopped matching real call sites, not that the source "
        f"lost this producer"
    )
    mismatched = [
        (path.relative_to(REPO_ROOT), lineno, label, hotkey)
        for path, lineno, label, hotkey in pairs
        if f"[{hotkey}]" not in label
    ]
    assert mismatched == [], (
        "InterventionChoice.label's bracketed letter must name the SAME "
        "character as .hotkey (case-sensitive — #4751 wired the panel to "
        "answer .hotkey on a keypress, so a mismatched bracket displays a "
        f"key that does not work): {mismatched}"
    )


def test_the_walker_itself_flags_a_deliberately_mismatched_pair():
    """Tier 1: falsify pair (deny side) — a synthetic file containing a
    genuine label/hotkey mismatch, parsed through the SAME walker, must be
    reported. Without this, the accept-side test above could pass simply
    because the walker matches nothing (an empty ``mismatched`` list is
    the SAME shape whether every real pair agrees or the walker is
    inert) — this proves the walker can actually see a break."""
    import tempfile

    src = (
        'from reyn.user_intervention import InterventionChoice\n\n'
        'CHOICES = [\n'
        '    InterventionChoice(id="yes", label="[K]eep", hotkey="y"),\n'
        ']\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "synthetic_mismatch.py").write_text(src, encoding="utf-8")
        pairs = find_intervention_choice_literal_pairs(tmp_path)
        assert pairs == [(tmp_path / "synthetic_mismatch.py", 4, "[K]eep", "y")]
        mismatched = [p for p in pairs if f"[{p[3]}]" not in p[2]]
        assert mismatched != [], (
            "the walker must flag a genuine label/hotkey mismatch — a "
            "walker that finds nothing wrong here would also find "
            "nothing wrong in a real regression"
        )
