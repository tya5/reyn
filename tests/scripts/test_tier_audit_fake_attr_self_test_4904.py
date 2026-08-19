"""Tier 1: self-test for Rule 7 (#4873, ``scripts/test_tier_audit.py``'s
``_check_fake_attr_assignments``) — #4904.

Rule 7 shipped (#4900) with zero automated tests. The PR's own "falsify"
record was the AUTHOR manually reverting the detector and re-running the
script by hand — real evidence the detector worked at that moment, but not
a test anyone else's later edit is checked against. Scoping the walk back
to ``test_*``-only (the exact regression #4900 itself fixed — see
``_check_fake_attr_assignments``'s own docstring) would silently pass every
existing test file today; nothing in the corpus depends on the whole-file
behaviour being exercised.

Three requirements (lead-coder, architect-measured):
  ① a violation INSIDE A MODULE-LEVEL HELPER (not itself ``test_*``) is
    detected — the #4900 lesson itself, not a generic re-check of ①'s sibling
    rules' own per-test scoping.
  ② a bare READ of an already-flagged attribute does not fire — accept-side,
    the same "reading is a narrower, different complaint" scoping the rule's
    own docstring states in prose but which nothing had verified holds.
  ③ the RULE IS WIRED into the real CLI scan path (``main()``), not just
    correct as an isolated function — #4900's own gap: a manual "I moved the
    scope back and reran the script" is not what a later PR's own local gate
    run repeats; only an automated test that drives ``main()`` on a real file
    would catch a future revert of the wiring block itself.

Public surface only — real ``main()``, real ``_check_fake_attr_assignments``,
real filesystem via ``tmp_path``. No mocks; the script is stdlib-only.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "test_tier_audit.py"


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` without pytest collecting it
    as a test module (its filename starts with ``test_``) — same loader
    idiom as the sibling ``test_tier_audit_private_state_ast.py`` /
    ``test_4577_tier_audit_empty_target_set.py``."""
    spec = importlib.util.spec_from_file_location("_audit_fake_attr_4904", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_attr_findings(audit_mod, source: str) -> list:
    """Call ``_check_fake_attr_assignments`` directly on *source* — the
    isolated-function surface, whole-file (not routed through a single
    ``test_*`` FunctionDef like Rule 3's own helper does)."""
    tree = ast.parse(source)
    return audit_mod._check_fake_attr_assignments(source, tree)


# ── ① module-level helper (not test_*) — the #4900 lesson itself ───────────


def test_violation_inside_a_module_level_helper_is_detected() -> None:
    """Tier 1: a helper function NOT named ``test_*`` — called BY a test,
    but not itself a test body — still gets its fake-attr assignment
    flagged. This is exactly the #4900 real-world miss: a stale ignore
    comment inside ``_noop_handler`` (called from a test, never itself
    scanned by a per-test-function walk) went undetected until the rule was
    made whole-file."""
    audit = _load_audit_module()
    source = (
        "def _noop_handler(obj):\n"
        '    """Not a test_* function — a fixture helper a test calls."""\n'
        "    obj.injected = 1  # type: ignore[attr-defined]\n"
        "    return obj\n"
        "\n"
        "\n"
        "def test_uses_the_helper() -> None:\n"
        '    """Tier 2: example — the violation lives in the helper above,\n'
        "    not here.\"\"\"\n"
        "    assert _noop_handler(object()).injected == 1\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings, (
        "the module-level helper's own assignment must be found — "
        "a per-test-function-only walk would find nothing at all"
    )
    assert any(f.line == 3 and "injected" in f.message for f in findings), (
        "the finding must anchor to the helper's own assignment line, not "
        "somewhere else in the file"
    )


def test_violation_inside_the_test_body_itself_still_detected() -> None:
    """Tier 1: regression sanity — whole-file scanning must not have
    dropped the ORIGINAL (in-test-body) shape while fixing the helper gap."""
    audit = _load_audit_module()
    source = (
        "def test_thing() -> None:\n"
        '    """Tier 2: example."""\n'
        "    obj.injected = 1  # type: ignore[attr-defined]\n"
        "    assert obj.injected == 1\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert any(f.line == 3 for f in findings)


def test_multi_code_ignore_bracket_still_detected() -> None:
    """Tier 1: ``# type: ignore[attr-defined, assignment]`` (multiple codes
    in one bracket) is still caught — the rule's own docstring names this
    as a form the capture group must not miss."""
    audit = _load_audit_module()
    source = (
        "def _helper(obj):\n"
        '    """Fixture helper."""\n'
        "    obj.injected = 1  # type: ignore[attr-defined, assignment]\n"
        "    return obj\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert any(f.line == 3 for f in findings)


def test_annotated_assignment_form_detected() -> None:
    """Tier 1: ``obj.attr: int = value  # type: ignore[attr-defined]``
    (AnnAssign, not plain Assign) is a real branch in the detector — cover
    it explicitly, not just the plain-Assign shape above."""
    audit = _load_audit_module()
    source = (
        "def _helper(obj):\n"
        '    """Fixture helper."""\n'
        "    obj.injected: int = 1  # type: ignore[attr-defined]\n"
        "    return obj\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert any(f.line == 3 for f in findings)


# ── ② accept-side: reading (not assigning) never fires ─────────────────────


def test_bare_read_of_an_ignored_attribute_not_flagged() -> None:
    """Tier 1: ``x = obj._attr  # type: ignore[attr-defined]`` — the
    ASSIGNMENT TARGET here is ``x`` (a Name), not ``obj._attr`` (an
    Attribute) — reading an already-flagged private attribute is the
    rule's own disclosed OUT-OF-SCOPE case (a narrower, different
    complaint per architect's #4873 measurement), and must not fire."""
    audit = _load_audit_module()
    source = (
        "def test_thing() -> None:\n"
        '    """Tier 2: example."""\n'
        "    x = obj.injected  # type: ignore[attr-defined]\n"
        "    assert x == 1\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings == []


def test_assignment_without_the_ignore_comment_not_flagged() -> None:
    """Tier 1: ``obj.attr = value`` with NO suppression comment at all is
    out of scope — this rule bans the SUPPRESSION, not the underlying
    attr-defined-ness (mypy's own ratchet is the detector for that;
    see the rule's own docstring)."""
    audit = _load_audit_module()
    source = (
        "def _helper(obj):\n"
        '    """Fixture helper."""\n'
        "    obj.injected = 1\n"
        "    return obj\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings == []


def test_assignment_with_a_different_ignore_code_not_flagged() -> None:
    """Tier 1: ``# type: ignore[unreachable]`` (a different code entirely)
    must not be mistaken for ``attr-defined`` — the code-set parsing must
    be exact, not "any type: ignore comment present"."""
    audit = _load_audit_module()
    source = (
        "def _helper(obj):\n"
        '    """Fixture helper."""\n'
        "    obj.injected = 1  # type: ignore[unreachable]\n"
        "    return obj\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings == []


def test_hashless_string_literal_containing_the_phrase_not_flagged() -> None:
    """Tier 1: #4910 — an ATTRIBUTE assignment whose VALUE is a string
    literal that happens to contain the text ``type: ignore[attr-defined]``
    (no leading ``#`` anywhere on the source line, so it is DATA, not a
    real suppression comment) must not fire.

    Real-corpus-inspired (architect, #4910): ``tests/scripts/
    test_3726_mypy_ratchet.py`` builds a string mimicking mypy stdout that
    quotes the phrase ``"type: ignore[assignment]"`` as data — but that
    file's own assignment target is a plain NAME (``text = (...)``), which
    the rule already excludes by construction (only Attribute-target
    assignments are even considered) regardless of the ``#`` requirement,
    so it is not itself a witness for the ``#`` requirement specifically.
    THIS test puts the same shape of string value on an Attribute-target
    assignment instead — the one case that actually reaches
    ``_ignore_codes`` — so a future regex change that dropped the literal
    ``#`` requirement (matching the phrase anywhere on the line, comment
    or not) would flip this test red without dropping the target-type
    filter at the same time."""
    audit = _load_audit_module()
    source = (
        "def _helper(obj):\n"
        '    """Fixture helper."""\n'
        '    obj.injected = \'Error code not covered by "type: ignore[attr-defined]" comment\'\n'
        "    return obj\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings == []


def test_ordinary_self_attribute_assignment_not_flagged() -> None:
    """Tier 1: sanity — a normal, unsuppressed ``self.x = value`` inside a
    real class (the overwhelming majority of assignments in any file) must
    never fire."""
    audit = _load_audit_module()
    source = (
        "class Widget:\n"
        "    def __init__(self):\n"
        "        self.count = 0\n"
        "\n"
        "\n"
        "def test_widget() -> None:\n"
        '    """Tier 2: example."""\n'
        "    w = Widget()\n"
        "    assert w.count == 0\n"
    )
    findings = _fake_attr_findings(audit, source)
    assert findings == []


# ── ③ the rule is WIRED into main()'s real scan path, not just correct in ──
# ── isolation — #4900's own gap: nothing had driven main() end-to-end.    ──


def test_main_end_to_end_flags_a_fake_attr_violation(tmp_path: Path) -> None:
    """Tier 1: a real file on disk, scanned via the real ``main(argv)`` CLI
    entry point — not ``_check_fake_attr_assignments`` called directly.

    This is the positive control ③: proves the wiring block in ``main()``
    (the ``if check_rules is None or "fake-attr" in check_rules: ...
    _check_fake_attr_assignments(...)`` block) is actually reached and its
    findings actually flip the exit code — not merely that the detector
    function itself, called by a test, returns the right list. A future
    edit that silently drops or short-circuits that wiring block would
    leave every OTHER test in this file green (they call the function
    directly) while this one goes red."""
    audit = _load_audit_module()

    bad = tmp_path / "test_has_a_fake_attr.py"
    bad.write_text(
        '"""Tier 2: fixture module for the audit to reject."""\n'
        "\n"
        "\n"
        "def _helper(obj):\n"
        '    """Fixture helper — not itself test_*."""\n'
        "    obj.injected = 1  # type: ignore[attr-defined]\n"
        "    return obj\n"
        "\n"
        "\n"
        "def test_uses_helper() -> None:\n"
        '    """Tier 2: example."""\n'
        "    assert _helper(object()).injected == 1\n",
        encoding="utf-8",
    )

    assert audit.main(["--check", "fake-attr", str(bad)]) != 0


def test_main_end_to_end_accepts_a_clean_file(tmp_path: Path) -> None:
    """Tier 1: the accept side of ③ — a file with NO fake-attr violation
    still exits 0 through the same real ``main()`` path. Without this, a
    wiring bug that fires unconditionally (every file rejected regardless
    of content) would pass the reject-side test above and go unnoticed."""
    audit = _load_audit_module()

    good = tmp_path / "test_clean.py"
    good.write_text(
        '"""Tier 2: fixture module the audit should accept."""\n'
        "\n"
        "\n"
        "def _helper(obj):\n"
        '    """Fixture helper — no suppressed assignment."""\n'
        "    obj.count = 1\n"
        "    return obj\n"
        "\n"
        "\n"
        "def test_uses_helper() -> None:\n"
        '    """Tier 2: example."""\n'
        "    assert _helper(object()).count == 1\n",
        encoding="utf-8",
    )

    assert audit.main(["--check", "fake-attr", str(good)]) == 0
