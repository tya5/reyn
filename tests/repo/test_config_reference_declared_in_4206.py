"""Tier 2: `docs/reference/config/reyn-yaml.md`'s `Declared in` column is a
CHECKED-IN-CI doc↔code gate (#4206), not a hand-maintained claim — the same
shape `tests/core/test_audit_event_kind_vocabulary_3410.py` already
establishes for `events.md`'s kind-vocabulary enumeration
(`docs/reference/runtime/events.md:16-20`, verbatim: "checked against it in
CI, and that declaration is in turn checked against the emitting code — in
both directions").

The defect this closes (architect, #4206's own issue thread): PR #5086 shipped
`project_context_path` becoming agent-layer-overridable while the doc's
"Written on / reload" column still said `PRJ only` — caught only by a
reviewer's own re-reading, not by any gate. `output_language` (#4206 slice 1,
commit 7edb831bf) shipped the SAME gap and has never been caught at all.

Scope, explicit (architect's own scoping — issuecomment-5379310759, relayed;
corrected same-night after `Reload` itself was caught stale for the exact
rows this file already checks — issuecomment-5379469534/5379489719): only
the **`Declared in`** column is derived/checked here — NOT because
`Reload`/`File` can't go stale (they can and did: `output_language`'s own
`Reload` cell was wrong until this same PR fixed it), but because "does
this call site re-read live?" is a property of the call site itself, with
no file-based registry (`_HOT_RELOAD_FILES`) to mechanically derive it
from. `Declared in` is the one column this repo currently has a source of
truth for.

Source of truth: `PREFERENCE_KEYS` (`src/reyn/runtime/preferences.py`, the ③
preference axis) plus the 2 explicit agent-layer-only `AgentProfile` fields
(`project_context_path` #5086, `broker_identity` #5085 — `broker_identity`
itself has no project-wide row in this table at all, per architect's own
note that it is the `agent`-alone boundary case, so it is not checked here).

Real file reads throughout (the actual doc, the actual `PREFERENCE_KEYS`
declaration) — no mocks.
"""
from __future__ import annotations

import re

from reyn.runtime.preferences import PREFERENCE_KEYS
from tests._support.paths import REPO_ROOT

_DOC = REPO_ROOT / "docs" / "reference" / "config" / "reyn-yaml.md"

_BEGIN = "<!-- BEGIN config-declared-in -->"
_END = "<!-- END config-declared-in -->"


def _table_text() -> str:
    """The doc's `Declared in`-bearing table, bounded by its own markers —
    same "read the derived section, not the whole file" pattern as
    `test_audit_event_kind_vocabulary_3410.py`'s `_DOC_BEGIN`/`_DOC_END`."""
    text = _DOC.read_text(encoding="utf-8")
    start = text.index(_BEGIN)
    end = text.index(_END)
    return text[start:end]


def _declared_in_cell(table_text: str, key: str) -> str:
    """The `Declared in` CELL for `` `key` `` — specifically NOT the whole
    row: the `Description` cell routinely mentions "agent"/"session" in its
    own prose (`project_context_path`'s own row explains an agent-layer
    override in words), so asserting against the full row text would pass
    vacuously regardless of what the `Declared in` cell itself says. Raises
    if the row is missing entirely (a key that was RENAMED or REMOVED from
    this table would otherwise silently pass every assertion below, having
    nothing to check) or if the row doesn't have the expected 6-cell shape
    (a malformed row is a finding, not something to index past)."""
    pattern = re.compile(
        r"^\| `" + re.escape(key) + r"` \|.*$", re.MULTILINE,
    )
    match = pattern.search(table_text)
    assert match is not None, (
        f"expected a `Declared in` table row for `{key}` in {_DOC} — "
        f"the row is missing entirely, not merely wrong"
    )
    cells = [c.strip() for c in match.group(0).split("|")]
    # cells[0] is "" (text before the leading "|"); cells[1]=Key, [2]=Type,
    # [3]=Declared in, [4]=Reload, [5]=File, [6]=Description, [7]="" (after
    # the trailing "|").
    assert len(cells) >= 7, (
        f"`{key}`'s row does not have the expected 6-cell shape "
        f"(Key/Type/Declared in/Reload/File/Description): {match.group(0)!r}"
    )
    return cells[3]


# ── PREFERENCE_KEYS' top-level parents must show agent + session ───────────


def test_every_preference_keys_top_level_parent_is_declared_agent_and_session():
    """Tier 2: strip-falsifier for the doc↔code drift #4206 exists to catch —
    for every dotted path in `PREFERENCE_KEYS`, that path's TOP-LEVEL PARENT
    key's own table row must name BOTH `agent` and `session` in its
    `Declared in` cell. `output_language` is both the key AND its own parent
    (a bare, non-dotted preference key); the rest (`chat.*`, `cost.*`) are
    nested — their PARENT row is what this table has a row for.

    Strip-falsifier: reverting `chat`'s or `cost`'s row to the pre-#4206
    `PRJ only` wording (this test's own reason for existing) turns this red
    — verified locally."""
    table = _table_text()
    top_level_parents = {key.split(".", 1)[0] for key in PREFERENCE_KEYS}
    assert top_level_parents, "PREFERENCE_KEYS must not be empty for this test to bite"

    for parent in sorted(top_level_parents):
        cell = _declared_in_cell(table, parent)
        assert "agent" in cell, (
            f"`{parent}` has a PREFERENCE_KEYS leaf (agent+session overridable) "
            f"but its `Declared in` cell never names `agent`: {cell!r}"
        )
        assert "session" in cell, (
            f"`{parent}` has a PREFERENCE_KEYS leaf (agent+session overridable) "
            f"but its `Declared in` cell never names `session`: {cell!r}"
        )


def test_output_language_specifically_is_project_agent_session():
    """Tier 2: regression guard for the SPECIFIC defect architect measured —
    `output_language` (#4206 slice 1, 7edb831bf) shipped agent/session
    preference-overridable and the doc never caught up until this PR. Named
    explicitly (not just covered by the generic sweep above) since this is
    the exact case that motivated writing the generic test in the first
    place."""
    table = _table_text()
    cell = _declared_in_cell(table, "output_language")
    assert "project" in cell and "agent" in cell and "session" in cell, (
        f"`output_language` must read `project · agent · session`; got {cell!r}"
    )


# ── the explicit agent-layer-only fields ────────────────────────────────────


def test_project_context_path_is_declared_agent_overridable():
    """Tier 2: strip-falsifier for the #5086 defect architect actually
    caught mid-review (PR #5086's own `Declared in` cell still said `PRJ
    only` after the agent-layer override landed) — `project_context_path`
    is NOT in `PREFERENCE_KEYS` (a separate, REPLACE-not-merge agent-layer
    mechanism, #5086's own docstring), so it needs its own explicit check
    rather than riding the generic PREFERENCE_KEYS sweep above.

    Strip-falsifier: reverting this row to `PRJ only` (the exact #5086
    review finding) turns this red — verified locally."""
    table = _table_text()
    cell = _declared_in_cell(table, "project_context_path")
    assert "agent" in cell, (
        f"`project_context_path` is agent-layer-overridable (#5086) but its "
        f"`Declared in` cell never names `agent`: {cell!r}"
    )
