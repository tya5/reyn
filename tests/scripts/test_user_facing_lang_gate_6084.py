"""Tier 1: `user_facing_lang_gate.py`'s own detector logic (#6084).

lead-coder's own ruling on #6084: the gate covers ONLY the sink-derivable
range (a string literal directly at a named sink's call site), never the
full population, and an i18n pair (`_FOO_EN`/`_FOO_JA`, matched by
STRUCTURE not by name/file list) is exempt. The fixtures under
`tests/scripts/fixtures/user_facing_lang_6084/` ARE that witness, permanently
checked in (not a strip-and-revert scratch) — one for a direct sink literal
(must flag), one for argument-side indirection (must NOT flag, out of
scope per ruling 1), one for the i18n-pair exception (must exempt the
paired name, must still flag an unpaired sibling-less name).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.user_facing_lang_gate import (
    findings,
    load_baseline,
    new_findings,
    write_baseline,
)
from tests._support.paths import REPO_ROOT

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "user_facing_lang_6084"


def test_a_direct_sink_literal_is_flagged():
    """Tier 1: the false-accept-side witness — a kana literal written
    directly at one of the confirmed sinks (`OutboxMessage(text=)`,
    `reply(ctx, ...)`, `DrawerRow(label=)`) is counted, one finding per
    call site."""
    fixture = _FIXTURES / "has_sink_literals.py"
    found = set(findings(fixture))
    expected_sinks = {"OutboxMessage", "reply", "DrawerRow"}
    assert {f.rsplit(":", 1)[1] for f in found} == expected_sinks, (
        "expected exactly one finding per confirmed sink in this fixture, "
        "no more and no fewer"
    )


def test_argument_side_indirection_is_not_flagged():
    """Tier 1: the false-reject-side witness, required alongside the test
    above so a detector that flags every sink call unconditionally (which
    would ALSO pass the direct-literal test) cannot pass this suite. Both
    of this fixture's sink calls carry kana text NEARBY (a dict, a
    function return) but never AS the literal argument at the call site —
    ruling 1's explicit out-of-scope shape."""
    assert findings(_FIXTURES / "indirect_not_flagged.py") == []


def test_i18n_pair_is_exempt_by_structure_not_name():
    """Tier 2: ruling 3's structural exception — `_FOO_JA` is exempt only
    because a module-level `_FOO_EN` sibling exists in the SAME file; an
    unpaired `_BAR_JA` with the identical nesting shape and no sibling is
    still flagged. This is the discriminating pair: a detector that
    exempted by matching the LITERAL `_JA`/`_EN` suffix alone (rather than
    requiring the sibling) would pass a version of this test missing the
    `_BAR_JA` half — both assertions are required."""
    fixture = _FIXTURES / "i18n_pair_exempt.py"
    found = findings(fixture)
    lines = fixture.read_text(encoding="utf-8").splitlines()
    bar_ja_lineno = next(i + 1 for i, line in enumerate(lines) if line.startswith("_BAR_JA ="))
    assert found == [f"{fixture}:{bar_ja_lineno}:OutboxMessage"], (
        "expected exactly one finding, at the unpaired _BAR_JA line — the "
        "paired _FOO_JA line must be exempt"
    )


def test_new_findings_flags_a_finding_absent_from_baseline():
    """Tier 2: the ratchet arithmetic itself (shared shape with
    `silent_except_ratchet.py`'s own `grown_files`) — a measured finding
    not in the baseline is new debt."""
    assert new_findings({"a.py:1:OutboxMessage"}, set()) == {"a.py:1:OutboxMessage"}


def test_new_findings_silently_allows_a_shrink():
    """Tier 2: a baselined finding that disappears from `measured` (the
    literal fixed, or the call site removed) is never reported — the
    silent-shrink contract every ratchet here shares."""
    assert new_findings(set(), {"a.py:1:OutboxMessage"}) == set()


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the baseline format round-trips through write/load."""
    path = tmp_path / "baseline.json"
    write_baseline({"b.py:2:reply", "a.py:1:OutboxMessage"}, path)
    assert load_baseline(path) == {"a.py:1:OutboxMessage", "b.py:2:reply"}


def test_a_file_that_fails_to_parse_raises_rather_than_finding_nothing(tmp_path: Path) -> None:
    """Tier 2: fail-closed on a scan error — a file this script cannot
    parse must propagate the parse error, not silently contribute nothing
    to the finding set (same contract `silent_except_ratchet.py` commits
    to, for the same reason).

    NON-VACUITY (strip-falsified locally, in-file Edit -> run -> Edit
    back): reverting `findings` to a `try/except: return []` shape makes
    this assertion fail — it would return `[]` instead of raising."""
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        findings(broken)


def test_the_real_scan_against_the_current_tree_matches_the_baseline() -> None:
    """Tier 1: the load-bearing witness — running the real scan against
    the real repo tree, right now, must find nothing beyond what's
    baselined. Mirrors `silent_except_ratchet.py`'s own identically-shaped
    test."""
    from scripts.user_facing_lang_gate import _BASELINE_PATH, measured

    baseline = load_baseline(_BASELINE_PATH)
    current, scanned = measured(REPO_ROOT)
    assert scanned > 0, "the scan found 0 files — a scanner failure, not a clean population"
    assert new_findings(current, baseline) == set()


def test_a_new_uncommitted_finding_would_be_caught() -> None:
    """Tier 2: integration — planting the direct-sink-literal fixture's
    own findings as a new "measured" set beyond an empty baseline is
    reported as grown debt. Confirms the ratchet arithmetic is wired to
    the real detector, not just unit-tested in isolation above."""
    measured_now = set(findings(_FIXTURES / "has_sink_literals.py"))
    assert new_findings(measured_now, set()) == measured_now
    assert measured_now, "expected at least one finding from this fixture, wiring is broken"


# ── gate-hole ⑴ (lead-coder, #6084): DrawerRow's registered kwargs must ──
# ── match what its OWN render property actually shows the operator ──────


def _fields_rendered_by(cls: type, property_name: str) -> "set[str]":
    """AST-derive every `self.<name>` attribute *cls*'s own *property_name*
    property reads, by walking that property's real source — never a
    second, hand-maintained guess at "which fields are text". This is the
    SAME shape `_collect_i18n_pair_exempt_ids` already uses elsewhere in
    this gate (a structural AST condition, not a name/file list): the
    class's own rendering code is the single source of truth for "what
    reaches the operator's screen" through it, since that is the literal
    reason the property exists."""
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(cls))
    class_node = ast.parse(source).body[0]
    assert isinstance(class_node, ast.ClassDef)
    (prop_node,) = [
        n for n in class_node.body
        if isinstance(n, ast.FunctionDef) and n.name == property_name
    ]
    fields: "set[str]" = set()
    for node in ast.walk(prop_node):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            fields.add(node.attr)
    return fields


def test_drawerrow_sink_spec_matches_its_own_render_property() -> None:
    """Tier 2: the derivation gate for gate-hole ⑴ itself. `DrawerRow.text`
    (`chrome.py`'s own docstring: "the row as the pane renders it") is the
    class's single declared rendering path — every `self.<field>` it reads
    is a field that can carry a kana literal all the way to the operator's
    screen. `_SINK_SPECS["DrawerRow"]` must register EXACTLY that set, in
    both directions: a field `text` reads but this spec does not register
    is the gate-hole class this test exists to close (the ORIGINAL defect
    — `note` was read by `text`, absent from the spec); a field registered
    here that `text` does NOT read would be a stale/over-broad entry
    (drift the other direction — question 4's own "would it stay green
    having nothing to bite on" concern, applied to a spec instead of a
    finding set).

    STRIP-FALSIFY (verified by hand, file-internal Edit only, reverted):
    removing `("kwarg", "note")` from `_SINK_SPECS["DrawerRow"]` turns
    this red (`text`'s own AST still reads `self.note`, the spec no
    longer does) — confirming this test genuinely depends on the spec
    covering what the property renders, not just a fixed literal set."""
    from reyn.interfaces.inline.textual_chat.chrome import DrawerRow
    from scripts.user_facing_lang_gate import _SINK_SPECS

    rendered = _fields_rendered_by(DrawerRow, "text")
    assert rendered, "arrange: text's own AST must reference at least one field"
    registered = {
        key for kind, key in _SINK_SPECS["DrawerRow"] if kind == "kwarg"
    }
    assert registered == rendered, (
        f"_SINK_SPECS['DrawerRow'] registers {registered!r} but "
        f"DrawerRow.text actually reads {rendered!r} -- a mismatch either "
        f"way means the gate is not watching (or is watching a stale set "
        f"of) exactly what reaches the operator's screen"
    )


def test_drawerrow_command_is_correctly_excluded() -> None:
    """Tier 2: deny side, named explicitly (question 1's own "who would
    miss this test" answer: a future reader wondering why `command` isn't
    in `_SINK_SPECS` needs this, not just the equality check above passing
    silently) — `command` is a slash-command identifier, never referenced
    by `DrawerRow.text`, so the render-derived set correctly excludes it
    without this test needing a hand-written exemption list."""
    from reyn.interfaces.inline.textual_chat.chrome import DrawerRow

    assert "command" not in _fields_rendered_by(DrawerRow, "text")
