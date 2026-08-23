"""Tier 2: #4206 slice 1 — per-agent-profile keys declare their own reload
class, and the doc's own "Per-agent profile key reload classes" section is
a projection of that declaration, not a second hand-written source.

Mirrors `tests/repo/test_config_reference_declared_in_4206.py`'s own
doc↔code gate shape (read the doc's own BEGIN/END-bounded section, assert
its cells against the real registry) plus #5190's own completeness-gate
shape (a real vocabulary — here, `AgentProfile`'s own fields +
`PREFERENCE_KEYS` + `BOUNDING_KEYS` — minus a hand-maintained registry
must be empty).

Real imports throughout (the actual `AGENT_PROFILE_RELOAD_CLASSES`, the
actual doc file, the actual `PREFERENCE_KEYS`/`BOUNDING_KEYS`) — no mocks.
"""
from __future__ import annotations

import re
from dataclasses import fields as dataclass_fields

from reyn.runtime.bounding import BOUNDING_KEYS
from reyn.runtime.preferences import PREFERENCE_KEYS
from reyn.runtime.profile import AgentProfile
from reyn.runtime.profile_reload import (
    _NON_RELOAD_CLASS_FIELDS,
    AGENT_PROFILE_RELOAD_CLASSES,
    CONSTRUCTION_ONCE,
    EXPLICIT_TRIGGER,
    LIVE,
    RESTART,
    declared_agent_profile_keys,
    missing_reload_class_declarations,
)
from tests._support.paths import REPO_ROOT

_DOC = REPO_ROOT / "docs" / "reference" / "config" / "reyn-yaml.md"

_BEGIN = "<!-- BEGIN agent-profile-reload-class -->"
_END = "<!-- END agent-profile-reload-class -->"

_VALID_CLASSES = {LIVE, CONSTRUCTION_ONCE, EXPLICIT_TRIGGER, RESTART}


# ── the completeness gate (#5190-shape: real vocabulary minus registry) ──


def test_the_real_vocabularys_diff_is_currently_empty():
    """Tier 2: the acceptance witness — every real per-agent-profile key
    (AgentProfile's own fields + PREFERENCE_KEYS + BOUNDING_KEYS) has a
    registered reload class, right now. This gate's own starting
    population is zero, so any hit here is a new regression (a key added
    to one of the 3 real sources with no matching declaration), not
    inherited debt."""
    offenders = missing_reload_class_declarations()
    assert offenders == [], (
        f"real regression(s) found: {offenders} — a key was added to "
        "AgentProfile/PREFERENCE_KEYS/BOUNDING_KEYS with no matching "
        "AGENT_PROFILE_RELOAD_CLASSES entry"
    )


def test_a_new_preference_key_with_no_declared_class_is_flagged():
    """Tier 2: strip-falsifier — a hypothetical new PREFERENCE_KEYS entry
    with no matching registry entry must appear in the diff. Without this
    check, a future key could be added to PREFERENCE_KEYS and this gate
    would stay silently green."""
    import reyn.runtime.profile_reload as mod

    real_keys = mod.declared_agent_profile_keys
    try:
        mod.declared_agent_profile_keys = lambda: real_keys() | {"preferences.new_thing"}
        offenders = mod.missing_reload_class_declarations()
    finally:
        mod.declared_agent_profile_keys = real_keys
    assert offenders == ["preferences.new_thing"]


def test_every_declared_key_names_a_valid_reload_class():
    """Tier 2: non-vacuity — every value in the registry is one of the 4
    named classes, not an ad-hoc string that would silently pass the
    completeness check while meaning nothing to a reader."""
    for key, cls in AGENT_PROFILE_RELOAD_CLASSES.items():
        assert cls in _VALID_CLASSES, f"{key!r} declares an unknown reload class {cls!r}"


def test_the_registry_names_exactly_the_real_vocabulary_not_a_superset():
    """Tier 2: the flip side of completeness — the registry's own key set
    must equal the real vocabulary, not merely be a superset that happens
    to swallow the diff (a stale entry for a removed key masking a
    genuinely missing one)."""
    assert set(AGENT_PROFILE_RELOAD_CLASSES) == declared_agent_profile_keys()


def test_preference_keys_and_bounding_keys_are_genuinely_nonempty():
    """Tier 2: positive control — PREFERENCE_KEYS/BOUNDING_KEYS must not
    be empty for the tests above to bite (an empty source would make the
    completeness gate vacuously green)."""
    assert PREFERENCE_KEYS
    assert BOUNDING_KEYS


def test_every_excluded_field_name_is_a_real_agentprofile_field():
    """Tier 2: TESTS-READ A block ① witness (architect, issuecomment-
    5384894847) — `_NON_RELOAD_CLASS_FIELDS` must name REAL fields on
    `AgentProfile`, checked against `dataclasses.fields` directly, never a
    bare equality assert against the same literal set on both sides (that
    would prove nothing per CLAUDE.md's 6-questions #2 — "is it the
    implementation, transcribed?"). A typo'd/renamed exclusion name here
    would silently leave a stale entry in `declared_agent_profile_keys()`
    that no longer subtracts anything real."""
    real_field_names = {f.name for f in dataclass_fields(AgentProfile)}
    for excluded in _NON_RELOAD_CLASS_FIELDS:
        assert excluded in real_field_names, (
            f"{excluded!r} is excluded from the reload-class vocabulary "
            f"but is not a real AgentProfile field — real fields: "
            f"{sorted(real_field_names)}"
        )


def test_agentprofile_field_derivation_is_not_a_hand_typed_literal():
    """Tier 2: TESTS-READ A block ① strip-falsifier — confirms the REAL,
    UNMODIFIED `declared_agent_profile_keys()` genuinely reads
    `AgentProfile`'s own fields via `dataclasses.fields` rather than a
    hand-typed literal that happens to match today.

    Monkeypatches `reyn.runtime.profile.AgentProfile` ITSELF (the module
    attribute the function's own local `from reyn.runtime.profile import
    AgentProfile` re-fetches on every call) to a stand-in carrying one
    SYNTHETIC extra field, then calls the real function with NOTHING else
    replaced. If the AgentProfile-scoped leg were still the pre-block-①
    hand-typed literal, this synthetic field could never reach the
    derived set no matter what the (unread) class declares — that vacuity
    is exactly what this test rules out, by patching the DEPENDENCY, not
    the function under test."""
    from dataclasses import dataclass, field

    import reyn.runtime.profile as profile_mod

    @dataclass(frozen=True)
    class _FakeProfileWithExtraField:
        name: str = ""
        role: str = ""
        created_at: str = ""
        allowed_mcp: "list[str] | None" = None
        preferences: "dict[str, object]" = field(default_factory=dict)
        bounding: "dict[str, object]" = field(default_factory=dict)
        base_dir: "str | None" = None
        project_context_path: "str | None" = None
        a_brand_new_field: "str | None" = None

    real_class = profile_mod.AgentProfile
    try:
        profile_mod.AgentProfile = _FakeProfileWithExtraField
        keys = declared_agent_profile_keys()
    finally:
        profile_mod.AgentProfile = real_class
    assert "a_brand_new_field" in keys, (
        "declared_agent_profile_keys() must derive from AgentProfile's "
        "OWN fields at call time (dataclasses.fields), not a hand-typed "
        f"literal — a synthetic extra field never reached the result: {keys!r}"
    )


# ── the doc's own section is a projection of the registry ────────────────


def _table_text() -> str:
    text = _DOC.read_text(encoding="utf-8")
    start = text.index(_BEGIN)
    end = text.index(_END)
    return text[start:end]


def _doc_row(table_text: str, key: str) -> "tuple[str, str]":
    pattern = re.compile(r"^\| `" + re.escape(key) + r"` \| `([^`]+)` \|$", re.MULTILINE)
    match = pattern.search(table_text)
    assert match is not None, (
        f"expected a row for `{key}` in {_DOC}'s agent-profile-reload-class "
        f"section — the row is missing entirely, not merely wrong"
    )
    return key, match.group(1)


def test_every_registered_key_has_a_matching_doc_row():
    """Tier 2: doc↔code — every key in AGENT_PROFILE_RELOAD_CLASSES has a
    row in the doc's own section, and that row's reload class matches the
    registry exactly. A key present in the registry but missing (or wrong)
    in the doc is the drift this gate exists to catch — the doc is
    supposed to be a projection, not an independent source."""
    table = _table_text()
    for key, expected_class in AGENT_PROFILE_RELOAD_CLASSES.items():
        _, doc_class = _doc_row(table, key)
        assert doc_class == expected_class, (
            f"`{key}`'s doc row says {doc_class!r} but the registry says "
            f"{expected_class!r} — the doc has drifted from its own source"
        )


def test_the_doc_section_has_no_row_the_registry_does_not():
    """Tier 2: the other direction — a doc row for a key the registry does
    NOT declare would be a fabricated claim (the doc asserting a reload
    class nothing in the source tree backs). Counts rows in the section
    and compares to the registry's own size, catching an orphaned row a
    per-key sweep (the test above) would not notice on its own."""
    table = _table_text()
    row_count = len(re.findall(r"^\| `[^`]+` \| `[^`]+` \|$", table, re.MULTILINE))
    assert row_count == len(AGENT_PROFILE_RELOAD_CLASSES), (
        f"doc section has {row_count} rows but the registry has "
        f"{len(AGENT_PROFILE_RELOAD_CLASSES)} entries — a row was added or "
        f"removed on only one side"
    )


def test_allowed_mcp_is_the_explicit_trigger_class_not_construction_once_or_restart():
    """Tier 2: regression guard for this slice's own discovery — measured
    (session.py's `_reapply_per_agent_capability`, the `per_agent_
    capability` hot-reload seam) as neither a per-access live re-read, nor
    frozen for a session's lifetime, nor requiring a process restart, but
    an explicit-trigger-then-turn-boundary reapply. NOT the project-layer
    `hot` class (TESTS-READ A block ②, architect, issuecomment-5384894847)
    — `profile.yaml` is not in `_HOT_RELOAD_FILES`, so a bare hand-edit
    does nothing on its own, the opposite polarity from `hot`'s own
    defining property (the file write itself is the trigger there)."""
    assert AGENT_PROFILE_RELOAD_CLASSES["allowed_mcp"] == EXPLICIT_TRIGGER


def test_role_and_project_context_path_are_construction_once():
    """Tier 2: regression guard — `role` (a frozen `Agent` identity field,
    `agent.py`'s own "Frozen — identity is immutable for a session's
    lifetime") and `project_context_path` (owner ruling B / #3787,
    "resolved ONCE per agent, at session construction") are both
    construction-once, matching the pre-measured #3787 example this
    slice's own assignment named."""
    assert AGENT_PROFILE_RELOAD_CLASSES["role"] == CONSTRUCTION_ONCE
    assert AGENT_PROFILE_RELOAD_CLASSES["project_context_path"] == CONSTRUCTION_ONCE
