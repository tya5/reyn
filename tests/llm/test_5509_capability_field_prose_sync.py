"""Tier 1: #5509/#5517 — every ``supports_[a-z_]+``-shaped token anywhere in
``src/`` or ``docs/`` must be a real member of
``QUERIED_CAPABILITY_FIELDS_BY_MODALITY``'s own values, UNLESS it appears on
a line already recognised as pre-existing, unrelated litellm capability
usage that has nothing to do with media-capability overrides.

Real incident (#5517 review, architect + lead-coder): a hand-picked list of
"the prose surfaces that mention a capability field" moved 3 → 4 → 5 across
one review, each new find catching what the previous pass's own hand list
missed. Lead-coder's final ruling (PR #5517 comment, 2026-08-29): the
population must be ``git ls-files`` (not ``Path.rglob()`` filtered by a
hand-maintained excluded-directories list — the exact reasoning
``scripts/check_tests_path_literal_reference.py`` already gives for its own
scan), and the gate must NOT enumerate surfaces by name — the next modality
(PR2) creates a new surface the moment someone writes it, and a gate that
lists today's surfaces would silently miss it, becoming the very hand list
this gate exists to retire.

A genuine whole-tree sweep for ``supports_[a-z_]+`` was measured directly
against this repo (2026-08-29) and finds ~30 hits that have NOTHING to do
with media-capability overrides — ``supports_response_schema`` (structured-
output precheck, ``router_loop.py``/``llm.py``/``compaction/engine.py``/
``dogfood/verifiers/reply.py``/2 docs), ``supports_structured_output``,
``supports_native_streaming``, ``supports_function_calling``,
``supports_response_format`` — all pre-existing litellm capability checks
for OTHER features, unrelated to ``QUERIED_CAPABILITY_FIELDS_BY_MODALITY``.
A gate that flagged every one of those would be pure noise from the day it
shipped, not a real defect. This test therefore scopes the sweep to the
one genuine, structural marker that IS shared by every surface this gate
must guard — a line naming ``QUERIED_CAPABILITY_FIELDS_BY_MODALITY`` or
``model_capability_overrides`` itself (the constant + the config key it
governs; nothing about an unrelated feature would ever mention either) —
and only inspects tokens *on or immediately after* such a marker line,
never a whole file just because the marker appears somewhere else in it
(``router_loop.py`` itself co-hosts the unrelated ``supports_response_
schema`` precheck a few hundred lines away from this feature's own call
site — file-level scoping alone was measured and rejected for exactly this
reason).

Why the marker-line predicate is the right discriminator, not merely a
convenient one (architect condition, #5517, 2026-08-29): a line that names
NEITHER identifier is not talking about reyn's own capability-field
vocabulary at all — it is describing litellm's field directly (the ~30
``supports_response_schema`` / ``supports_structured_output`` / etc. hits
above are all exactly this: a different feature's own litellm precheck,
with no mention of ``QUERIED_CAPABILITY_FIELDS_BY_MODALITY`` or
``model_capability_overrides`` anywhere nearby). ``QUERIED_CAPABILITY_
FIELDS_BY_MODALITY`` is THIS feature's only closed vocabulary — a line
outside its own naming has no closed vocabulary to violate.

What this predicate deliberately does NOT catch, by design, not by
oversight: prose that discusses a stray ``supports_pdf_input``-shaped
field WITHOUT naming either identifier nearby (e.g. a doc paragraph that
says "declare it, like ``supports_pdf_input``" with the constant/config-key
mention several sentences earlier or missing entirely). Widening the scope
to catch that shape reintroduces the ~30-hit false-positive flood measured
above and returns this gate to permanent red — DO NOT widen it without
re-running that same measurement first.
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


def _tracked_files() -> list[str]:
    """``git ls-files`` scoped to ``src/`` + ``docs/`` — TRACKED files, not
    ``Path.rglob()`` filtered by a hand-maintained excluded-directories
    list (``check_tests_path_literal_reference.py:83-95``'s own reasoning,
    reused here per lead-coder's #5517 ruling: rglob needs an exclusion
    list, which is the exact hand-maintained shape this gate retires)."""
    out = subprocess.run(
        ["git", "ls-files", "--", "src/", "docs/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_no_capability_marker_line_names_an_unqueried_field() -> None:
    """Tier 1: on any tracked ``src/``/``docs/`` line that names this
    feature's own marker (the constant or the config key it governs), the
    ONLY ``supports_*``-shaped tokens allowed are real members of
    ``QUERIED_CAPABILITY_FIELDS_BY_MODALITY``'s own values — a wider
    litellm field there would be accepted by an operator's config but
    silently do nothing, the same silence class as a typo, just correctly
    spelled."""
    known = frozenset(QUERIED_CAPABILITY_FIELDS_BY_MODALITY.values())
    offenders: list[str] = []
    for rel_path in _tracked_files():
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines):
            if _DEFINITION_MARKER in line:
                continue
            if not _MARKER_RE.search(line):
                continue
            for match in _SUPPORTS_FIELD_RE.finditer(line):
                field = match.group(0)
                if field not in known:
                    offenders.append(f"{rel_path}:{i + 1}: {field!r}")
    assert not offenders, (
        f"a line naming QUERIED_CAPABILITY_FIELDS_BY_MODALITY or "
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
