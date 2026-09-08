"""Tier 1: no `scripts/check_*.py` gate module reads a PRIVATE attribute off
another dynamically-loaded gate module — a repo-wide census, both of whose
populations are DERIVED, never a curated list (#5967).

## Why this replaces a literal list

`test_check_blocking_has_reread_note_5453.py`'s own
`test_no_private_attribute_is_read_off_tests_read_or_the_old_blocking_
module` (the #5919 stage 3 acceptance) hardcoded the ONE file it checked
(`check_blocking_has_reread_note.py`) and the TWO alias names that file's
own `_load(...)` calls happen to bind today (`_tests_read`, `_blocking`).
That is exactly the shape architect ruled against on #5959 the same
night, for a different pair of populations ("母集団は2つ、どちらも導出
—手で維持する列挙表を作らない"): a THIRD gate module that starts
dynamically loading a sibling (its own `_load("_alias", "check_
whatever.py")` call) would go unseen by that test forever, because
neither "which files to scan" nor "which aliases inside them count as
cross-module borrows" was ever computed from the source — both were
typed once, by hand, when the test was written.

This test derives both:

1. **Which files to scan** — `sorted(Path("scripts").glob("check_*.py"))`,
   not a named list. A brand-new `scripts/check_whatever.py` is in scope
   the moment it exists, with no test edit.
2. **Which local aliases, inside each file, are "another gate module"**
   — every `<alias> = _load(<module_name>, <filename>)` assignment found
   by walking that file's OWN `ast.parse()`, not a hardcoded set of
   names. A file that starts borrowing a THIRD (or first) sibling gains
   census coverage for that alias automatically, no test edit.

`_markers.py` needs no exemption carved out: every symbol it exports
today is public (no leading underscore — `scripts/_markers.py`'s own
`role_prefixed_marker`, `MARKER_BLOCKING`, `undecorated_after_role_
prefix`, etc.), so an alias bound to it never appears in `offenders`
regardless — the census does not special-case "this loaded module is
the shared recognition module", it simply finds nothing private to
flag there, the same way it would for any other gate module that kept
its own surface public.

Fails **closed** if either derivation goes empty (`assert candidates`,
`assert borrow_aliases_found`, both before the substantive empty-offenders
assertion) — the #4846 ratchet's own "a scan returning 0 is a scan that
broke, not a scan that found nothing" discipline, applied here because
this test's own "0 offenders" IS the accept shape (an empty-collection
assert, CLAUDE.md's own six-questions #4: green having never run wears
the SAME colour as green having genuinely checked something)."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SCRIPTS_DIR = REPO_ROOT / "scripts"


def _gate_module_paths() -> "list[Path]":
    """The derived population of gate-module source files — every
    `scripts/check_*.py`, sorted for a stable (never asserted-on) walk
    order. Not `_markers.py` itself (it doesn't match `check_*.py`), and
    not anything under `tests/` (a different tree)."""
    return sorted(_SCRIPTS_DIR.glob("check_*.py"))


def _load_aliases(tree: "ast.Module") -> "dict[str, tuple[str, str]]":
    """Every local name this file's OWN source binds via
    `<alias> = _load(<module_name>, <filename>)` — the derived
    "these names ARE another gate module" population for ONE file.
    Returns ``{alias: (module_name_literal, filename_literal)}``; a
    call whose arguments aren't literal strings is skipped (this is a
    STATIC census, not an interpreter — every real `_load` call site in
    this repo passes literals, by the same convention `_load`'s own
    docstring names)."""
    aliases: "dict[str, tuple[str, str]]" = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_load"):
            continue
        if len(call.args) != 2:
            continue
        mod_name_node, filename_node = call.args
        if not (isinstance(mod_name_node, ast.Constant) and isinstance(mod_name_node.value, str)
                and isinstance(filename_node, ast.Constant) and isinstance(filename_node.value, str)):
            continue
        aliases[node.targets[0].id] = (mod_name_node.value, filename_node.value)
    return aliases


def _private_borrow_offenders(tree: "ast.Module", aliases: "dict[str, tuple[str, str]]") -> "list[str]":
    """Every `<alias>.<name>` attribute read in *tree* where *alias* is one
    of the derived cross-module names and `<name>` starts with `_` — the
    same shape #5919 stage 3's own acceptance pinned, generalised to
    every alias this file itself declares rather than two hardcoded
    ones."""
    return [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
        and node.attr.startswith("_")
    ]


def test_gate_module_population_is_nonempty():
    """Tier 1: the #4846-shaped fail-closed guard for claim (1) below — a
    glob returning 0 files is a BROKEN scan (wrong cwd, moved directory),
    not "nothing to check", and must not let the census pass vacuously."""
    candidates = _gate_module_paths()
    assert candidates, (
        f"glob({_SCRIPTS_DIR}/check_*.py) returned no files -- the census "
        "population derivation is broken, this is not evidence of zero "
        "gate modules"
    )


def test_at_least_one_gate_module_actually_borrows_a_sibling():
    """Tier 1: the #4846-shaped fail-closed guard for claim (2) — if NO
    file in the repo currently binds an alias via `_load(...)`, the
    private-borrow census below would pass by finding nothing to check,
    not by checking something and finding it clean. Names the one file
    known to do this today (`check_blocking_has_reread_note.py`,
    `_tests_read`/`_markers`) as a witness that the alias-derivation
    itself actually recognises a real `_load(...)` call shape — if this
    goes red, `_load_aliases`'s own pattern-matching broke, not the repo."""
    found_any = False
    for path in _gate_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _load_aliases(tree):
            found_any = True
            break
    assert found_any, (
        "no scripts/check_*.py file was found to bind any alias via "
        "_load(...) -- either every dynamic cross-module load was removed "
        "from the repo, or _load_aliases's own AST pattern stopped "
        "recognising the real call shape (check check_blocking_has_reread_"
        "note.py's own `_tests_read = _load(...)` line first)"
    )


def test_no_gate_module_reads_a_private_attribute_off_a_loaded_sibling():
    """Tier 1: LOAD-BEARING — the actual census. For every derived gate
    module file, for every derived cross-module alias inside it, no
    `Attribute` access on that alias may name a `_`-prefixed attribute.
    Both populations come from `_gate_module_paths`/`_load_aliases`
    above -- neither is typed into this test by hand."""
    all_offenders: "list[str]" = []
    for path in _gate_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _load_aliases(tree)
        if not aliases:
            continue
        offenders = _private_borrow_offenders(tree, aliases)
        all_offenders.extend(f"{path.name}: {o}" for o in offenders)

    assert all_offenders == [], (
        f"private cross-module attribute read(s): {all_offenders} -- every "
        "read off a dynamically-loaded sibling gate module must be either "
        "a PUBLIC name on the module that owns the decision, or a name "
        "from a shared recognition module (e.g. _markers.py) whose own "
        "surface is public -- never a private one"
    )


def test_strip_a_third_alias_with_a_private_borrow_is_caught():
    """Tier 1: LOAD-BEARING deny-side witness (lead-coder's own addition
    to #5967's acceptance) — the exact property a hardcoded 2-alias list
    could never have: a BRAND NEW gate module, dynamically loading a
    BRAND NEW sibling under a THIRD alias name never seen by this test's
    own source, and reading a private attribute off it, is still caught
    -- because the census walks whatever `_load(...)` calls the file
    ACTUALLY contains, not a fixed set. Constructs the probe source
    directly (never touches the real `scripts/` tree) and drives the
    SAME `_load_aliases`/`_private_borrow_offenders` helpers this test
    file's own census function uses."""
    probe_source = textwrap.dedent('''
        def _load(module_name, filename):
            ...

        _brand_new_third_alias = _load("_probe_third", "check_pr_closing_intent.py")

        def _uses_it():
            return _brand_new_third_alias._never_seen_before_private_thing
    ''')
    tree = ast.parse(probe_source)
    aliases = _load_aliases(tree)
    assert "_brand_new_third_alias" in aliases, (
        "the alias derivation must recognise a THIRD, never-before-seen "
        "alias name purely from its own _load(...) call shape"
    )
    offenders = _private_borrow_offenders(tree, aliases)
    assert offenders == ["_brand_new_third_alias._never_seen_before_private_thing"], (
        f"expected exactly one offender naming the new alias, got {offenders!r} "
        "-- a hardcoded {'_tests_read', '_blocking'} set would have found "
        "nothing here at all"
    )


def test_a_public_attribute_on_a_loaded_sibling_is_not_an_offender():
    """Tier 1: accept-side control for the strip above — without this, a
    census that flagged EVERY attribute access (not just private ones)
    would also pass the deny-side test's "found something" bar, for the
    wrong reason."""
    probe_source = textwrap.dedent('''
        def _load(module_name, filename):
            ...

        _alias = _load("_probe", "check_pr_closing_intent.py")

        def _uses_it():
            return _alias.a_public_decision_function()
    ''')
    tree = ast.parse(probe_source)
    aliases = _load_aliases(tree)
    offenders = _private_borrow_offenders(tree, aliases)
    assert offenders == []
