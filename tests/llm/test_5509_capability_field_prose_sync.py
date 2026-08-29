"""Tier 1: #5509/#5517 — every ``supports_[a-z_]+``-shaped token in a
blank-line-delimited BLOCK that names ``QUERIED_CAPABILITY_FIELDS_BY_
MODALITY`` or ``model_capability_overrides`` must be a real member of
that constant's own values.

Real incident (#5517 review, architect + lead-coder): a hand-picked list of
"the prose surfaces that mention a capability field" moved 3 → 4 → 5 across
one review. Lead-coder's ruling (PR #5517, 2026-08-29): the population must
be ``git ls-files`` (not ``Path.rglob()`` filtered by a hand-maintained
excluded-directories list — ``check_tests_path_literal_reference.py``'s own
reasoning), and the gate must NOT enumerate surfaces by name — the next
modality (PR2) creates a new surface the moment someone writes it, and a
named-surface gate would silently miss it, becoming the hand list this
gate exists to retire.

A whole-tree, unscoped sweep for ``supports_[a-z_]+`` was measured directly
against this repo and finds ~30 hits with nothing to do with media-
capability overrides (``supports_response_schema`` / ``supports_
structured_output`` / ``supports_native_streaming`` / ``supports_function_
calling`` / ``supports_response_format`` — other features' own,
independent litellm precheck code) — that design was rejected as
permanent-red noise, not a real defect.

**SAME-LINE scoping was tried next and is ALSO wrong** — a real BLOCKING
(architect + lead-coder, independently, same night): a marker and the
field name it governs are frequently on DIFFERENT lines of the same
prose paragraph or docstring (long-form English wraps). Real, measured
misses under same-line scoping: ``src/reyn/config/media.py`` — marker on
line 337, ``supports_vision`` on 333/338; ``reyn.local.yaml.example`` —
marker on line 1209, the field on line 1221. Same-line scoping caught 1
of 3 example surfaces (only ``docs/reference/config/reyn-yaml.md``, whose
mention happens to fit one long line) — the six-questions-review's own
"green because nothing to bite on" shape (`docs/deep-dives/contributing/
test-review-six-questions.md` Q4), self-inflicted.

**The fix: scope to the whole BLANK-LINE-DELIMITED BLOCK containing the
marker, not the single line.** Measured against the real tree: catches
all ~10 real capability-field mentions across every current surface,
misses 0 of the ~30 unrelated hits (none of them share a blank-line block
with either marker string — a different feature's own litellm precheck
lives nowhere near this constant or this config key in the source), and
produces exactly ONE false positive: ``src/reyn/config/infra.py``'s own
validator docstring, which illustrates the "did you mean" hint with a
DELIBERATE typo (``supports_vison``) inside the same block as the marker
comment above it. That one line carries an explicit inline exclusion tag
(``prose-sync:allow-example``) — the same shape as a ``# noqa`` comment:
the reason sits next to the line it excuses, not in a list somewhere
else, so it is not the hand-list shape this gate exists to retire.
"""
from __future__ import annotations

import re
import subprocess

from reyn.llm.model_media_capability import QUERIED_CAPABILITY_FIELDS_BY_MODALITY
from tests._support.paths import REPO_ROOT

_SUPPORTS_FIELD_RE = re.compile(r"supports_[a-z_]+")
_MARKER_RE = re.compile(r"QUERIED_CAPABILITY_FIELDS_BY_MODALITY|model_capability_overrides")

# The constant's own definition line legitimately names every real field
# (that IS its job) — excluded by construction, not by a hand-picked path.
_DEFINITION_MARKER = "QUERIED_CAPABILITY_FIELDS_BY_MODALITY: "

# Inline exclusion tag (same shape as ruff's own suppression comments —
# the reason sits next to the excused line, never in a list elsewhere)
# for a deliberate illustrative near-miss that is not itself the
# vocabulary — today only ``infra.py``'s own "did you mean" docstring
# example.
_ALLOW_EXAMPLE_MARKER = "prose-sync:allow-example"


def offenders_in_text(text: str, known: frozenset) -> list[tuple[int, str]]:
    """Pure predicate (architect condition, #5517: extracted so it can be
    exercised on literal fixture strings, not only the live tree) —
    returns ``(1-indexed line number, field)`` for every ``supports_*``
    token that is (a) inside a blank-line-delimited block containing a
    marker, (b) not on the constant's own definition line, (c) not tagged
    ``prose-sync:allow-example``, and (d) not a real member of *known*."""
    lines = text.splitlines()
    blocks: list[list[int]] = []
    current: list[int] = []
    for i, line in enumerate(lines):
        if line.strip() == "":
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(i)
    if current:
        blocks.append(current)

    found: list[tuple[int, str]] = []
    for block in blocks:
        if not any(_MARKER_RE.search(lines[i]) for i in block):
            continue
        for i in block:
            line = lines[i]
            if _DEFINITION_MARKER in line or _ALLOW_EXAMPLE_MARKER in line:
                continue
            for match in _SUPPORTS_FIELD_RE.finditer(line):
                field = match.group(0)
                if field not in known:
                    found.append((i + 1, field))
    return found


def _tracked_files() -> list[str]:
    """``git ls-files`` over the WHOLE tracked tree except ``tests/`` —
    TRACKED files, not ``Path.rglob()`` filtered by a hand-maintained
    excluded-directories list (``check_tests_path_literal_reference.
    py:83-95``'s own reasoning, reused here per lead-coder's #5517
    ruling: rglob needs an exclusion list, which is the exact hand-
    maintained shape this gate retires).

    NOT scoped to ``src/`` + ``docs/`` — a real example surface,
    ``reyn.local.yaml.example``, lives at the REPO ROOT (measured, real
    miss caught while implementing this gate: a ``src/``+``docs/``-only
    scope silently never inspects it, the exact "surface invisible to
    the gate" failure this whole arc exists to close). ``tests/`` is
    excluded for a different, principled reason — not path-convenience:
    this test module's own fixture literals below deliberately co-locate
    a marker with a bogus field IN THE SAME block (that's the point of a
    unit-test fixture), and a self-scan would flag its own accept-side
    test data as an offender. That is a structural distinction (fixture
    data vs. real prose), not a name-list — every other tracked file, in
    every other directory, is still swept."""
    out = subprocess.run(
        ["git", "ls-files", "--", ":!tests/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_no_capability_block_names_an_unqueried_field() -> None:
    """Tier 1: every tracked ``src/``/``docs/`` file, swept via
    :func:`offenders_in_text` — see module docstring for why block-scope,
    not line-scope or whole-file scope."""
    known = frozenset(QUERIED_CAPABILITY_FIELDS_BY_MODALITY.values())
    offenders: list[str] = []
    for rel_path in _tracked_files():
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, field in offenders_in_text(text, known):
            offenders.append(f"{rel_path}:{lineno}: {field!r}")
    assert not offenders, (
        f"a block naming QUERIED_CAPABILITY_FIELDS_BY_MODALITY or "
        f"model_capability_overrides also names a capability field reyn "
        f"does not actually query (update the text, or add the field to "
        f"QUERIED_CAPABILITY_FIELDS_BY_MODALITY if it's now real): "
        f"{offenders}"
    )


def test_the_constant_itself_is_not_empty() -> None:
    """Tier 1: accept-side / noise guard — the assert above passing over an
    EMPTY known-set would be vacuous. This test names that precondition
    explicitly rather than leaving it implicit."""
    assert QUERIED_CAPABILITY_FIELDS_BY_MODALITY


def test_the_marker_regex_actually_matches_something_in_the_tree() -> None:
    """Tier 1: accept-side / noise guard for the marker scan itself — if
    NEITHER marker string ever matched any tracked line, the test above
    would pass vacuously (0 lines inspected), not because prose is in
    sync. Confirms the population this gate inspects is non-empty."""
    hit = False
    for rel_path in _tracked_files():
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _MARKER_RE.search(text):
            hit = True
            break
    assert hit


# Architect's acceptance condition (#5517, 2026-08-29), lead-coder's final
# ruling: "the falsify you ran by hand, as a test" — the predicate as a
# pure function, fed the LITERAL shapes measured from the real surfaces (a
# field on a different line than its marker), asserting each is caught;
# plus a deny-side fixture for the unrelated ``supports_response_schema``
# shape, asserting it is NOT caught. These are frozen snapshots (never
# read from the live tree) — the live-tree test above is the one that
# catches real drift; these exist so the PREDICATE's own shape can never
# silently regress.
#
# ★ These 4 literal constants are unit-test INPUT FIXTURES for the
# predicate function — they are NOT a scan population list. The scan
# itself stays ``git ls-files`` over the whole tracked tree, always (see
# ``test_no_capability_block_names_an_unqueried_field`` above). Treating
# these 4 literals as "the surfaces we check" would silently regrow the
# exact hand-maintained-surface-list shape this gate exists to retire —
# lead-coder's explicit condition, #5517, 2026-08-29.
_KNOWN = frozenset(QUERIED_CAPABILITY_FIELDS_BY_MODALITY.values())

_MEDIA_PY_SHAPE = (
    "        model_capability_overrides:\n"
    "            #5509 (architect ruling) — ... ``reyn.llm.model_media_capability.\n"
    "            QUERIED_CAPABILITY_FIELDS_BY_MODALITY``'s own values (today\n"
    "            just ``\"supports_bogus_field\"``) — ...\n"
)

_YAML_EXAMPLE_SHAPE = (
    "#   model_capability_overrides: (#5509) declares a model's media capability\n"
    "#                     ``reyn.llm.model_media_capability.\n"
    "#                     QUERIED_CAPABILITY_FIELDS_BY_MODALITY`` for the current\n"
    "#     #   supports_bogus_field: true\n"
)

_MODEL_MEDIA_CAPABILITY_PY_SHAPE = (
    "    #: single source of truth for three places that must stay in sync\n"
    "    #: (enforced by test_5509_capability_field_prose_sync.py):\n"
    "    QUERIED_CAPABILITY_FIELDS_BY_MODALITY: \"dict[str, str]\" = {\n"
    "        \"image\": \"supports_vision\",\n"
    "    }\n"
    "    # a caller should pass supports_bogus_field only if it is real\n"
)

_REYN_YAML_MD_SHAPE = (
    "| `model_capability_overrides` | ... only the ones reyn's own code "
    "actually queries (`reyn.llm.model_media_capability."
    "QUERIED_CAPABILITY_FIELDS_BY_MODALITY`'s own values; today just "
    "`supports_bogus_field`) — a wider litellm field ... |\n"
)

# Deny-side: an unrelated litellm precheck, no marker anywhere nearby —
# the exact shape of the ~30 measured false positives this gate must NOT
# flag.
_UNRELATED_STRUCTURED_OUTPUT_SHAPE = (
    "    if not litellm.supports_response_schema(_precheck_model):\n"
    "        raise TypedError(\n"
    "            \"(litellm.supports_response_schema returned False) — schema-\"\n"
    "        )\n"
)


def test_all_four_surface_fixtures_are_present_and_nonvacuous() -> None:
    """Tier 1: six-questions Q4 guard — each accept-side test below asserts
    a SPECIFIC non-empty result, which is already non-vacuous on its own
    (a 0-block predicate would return ``[]``, not the expected match, and
    the assert would fail). This test names that precondition explicitly
    anyway, per lead-coder's #5517 ruling: confirm the 4 fixtures the
    tests below depend on are actually populated and actually contain
    both a marker and a deliberately-bogus field, so "the fixture set
    itself silently shrank to 0" can never read as green."""
    media_py, yaml_example, model_media_capability_py, reyn_yaml_md = (
        _MEDIA_PY_SHAPE,
        _YAML_EXAMPLE_SHAPE,
        _MODEL_MEDIA_CAPABILITY_PY_SHAPE,
        _REYN_YAML_MD_SHAPE,
    )
    for shape in (media_py, yaml_example, model_media_capability_py, reyn_yaml_md):
        assert _MARKER_RE.search(shape)
        assert "supports_bogus_field" in shape


def test_predicate_catches_the_media_py_shape() -> None:
    """Tier 1: real, measured miss under same-line scoping — the marker
    (line 337) and the field (line 338) are on different lines; the
    block-scoped predicate must still catch it."""
    assert offenders_in_text(_MEDIA_PY_SHAPE, _KNOWN) == [(4, "supports_bogus_field")]


def test_predicate_catches_the_yaml_example_shape() -> None:
    """Tier 1: real, measured miss under same-line scoping — marker on
    line 1209 of ``reyn.local.yaml.example``, field on line 1221."""
    assert offenders_in_text(_YAML_EXAMPLE_SHAPE, _KNOWN) == [(4, "supports_bogus_field")]


def test_predicate_catches_the_model_media_capability_py_shape() -> None:
    """Tier 1: the constant's own module — a field mention can appear a
    few lines after the constant's definition, inside the same block."""
    offenders = offenders_in_text(_MODEL_MEDIA_CAPABILITY_PY_SHAPE, _KNOWN)
    assert offenders == [(6, "supports_bogus_field")]


def test_predicate_catches_the_reyn_yaml_md_shape() -> None:
    """Tier 1: the one surface where marker and field DO share a line
    (a long markdown table cell) — same-line scoping caught this one
    surface only; block-scoping must still catch it too."""
    assert offenders_in_text(_REYN_YAML_MD_SHAPE, _KNOWN) == [(1, "supports_bogus_field")]


def test_predicate_does_not_flag_an_unrelated_litellm_precheck() -> None:
    """Tier 1: deny-side sibling — the exact shape of the ~30 measured
    false positives (no marker anywhere in the block) must never be
    caught."""
    assert offenders_in_text(_UNRELATED_STRUCTURED_OUTPUT_SHAPE, _KNOWN) == []


def test_predicate_respects_the_inline_allow_example_tag() -> None:
    """Tier 1: the one real false positive block-scoping produces
    (``infra.py``'s own deliberate ``supports_vison`` typo illustration)
    must be suppressed by its inline exclusion tag, not by widening the
    known-field set itself."""
    text = (
        "# QUERIED_CAPABILITY_FIELDS_BY_MODALITY governs this validator\n"
        "def f():\n"
        "    \"\"\"A near-miss (``supports_vison`` (prose-sync:allow-example)) hint.\"\"\"\n"
    )
    assert offenders_in_text(text, _KNOWN) == []
