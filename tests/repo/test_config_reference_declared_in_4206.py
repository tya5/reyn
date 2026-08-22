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
preference axis) plus the ONE explicit agent-layer-only `AgentProfile` field
(`project_context_path` #5086). There were two: `broker_identity` #5085 was
removed by #5091 (owner ruling — "broker" is an external MCP server, not a
reyn-runtime concept), which also emptied the `agent`-alone vocabulary value
it was the sole instance of. Nothing here checked it (it had no row in the
table), so its removal did not turn this gate red — the doc prose citing it
did go stale, which is what #5091's follow-up corrected.

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


def _cell(table_text: str, key: str, index: int) -> str:
    """The cell at `index` for `` `key` ``'s own row — specifically NOT the
    whole row: the `Description` cell routinely mentions "agent"/"session"
    in its own prose (`project_context_path`'s own row explains an
    agent-layer override in words), so asserting against the full row text
    would pass vacuously regardless of what a SPECIFIC cell says. Raises if
    the row is missing entirely (a key that was RENAMED or REMOVED from
    this table would otherwise silently pass every assertion below, having
    nothing to check) or if the row doesn't have the expected 6-cell shape
    (a malformed row is a finding, not something to index past).

    `index`: 3=Declared in, 4=Reload, 5=File (cells[0] is "" — text before
    the leading `|`; cells[1]=Key, [2]=Type, [6]=Description, [7]="" —
    after the trailing `|`)."""
    pattern = re.compile(
        r"^\| `" + re.escape(key) + r"` \|.*$", re.MULTILINE,
    )
    match = pattern.search(table_text)
    assert match is not None, (
        f"expected a table row for `{key}` in {_DOC} — "
        f"the row is missing entirely, not merely wrong"
    )
    cells = [c.strip() for c in match.group(0).split("|")]
    assert len(cells) >= 7, (
        f"`{key}`'s row does not have the expected 6-cell shape "
        f"(Key/Type/Declared in/Reload/File/Description): {match.group(0)!r}"
    )
    return cells[index]


def _declared_in_cell(table_text: str, key: str) -> str:
    return _cell(table_text, key, 3)


def _reload_cell(table_text: str, key: str) -> str:
    return _cell(table_text, key, 4)


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


# ── Reload is ALSO agent/session-layer wrong for the PREFERENCE_KEYS rows ──
#
# lead-coder's own #5090 review finding (issuecomment-5379469534): the
# column split correctly surfaced a SECOND drift the merged column had been
# hiding — `output_language`/`chat.reasoning.display`/the 7
# `cost.*.warn_ratio` leaves are agent/session-layer LIVE re-reads
# (`Session.output_language`/`_resolve_session_preference`, verbatim "Live
# re-read on every access"/"Live re-read on every call (never cached)"),
# not `_HOT_RELOAD_FILES`-gated `restart` at that layer. Gated in the SAME
# PR as the fix (lead-coder's own ruling, issuecomment-5379492328-adjacent:
# "shipping a column with a false cell in it is not something to defer").


def test_every_preference_keys_top_level_parent_discloses_its_reload_caveat():
    """Tier 2: strip-falsifier for the #5090 `Reload`-column drift — a
    `PREFERENCE_KEYS`-backed row's `Reload` cell must not read a BARE
    `restart` (true only at the project layer); it must carry SOME marker
    (this repo's convention: a footnote reference like `restart⁵`)
    distinguishing it from every other row's genuinely-unqualified
    `restart`. Not asserting the specific footnote NUMBER — footnote
    numbering is presentation, the exact thing CLAUDE.md's testing policy
    says never to pin — only that the cell is NOT the bare, unqualified
    string.

    Strip-falsifier: reverting any of `output_language`/`chat`/`cost`'s
    `Reload` cell to a bare `restart` (the exact #5090 review finding)
    turns this red — verified locally."""
    table = _table_text()
    top_level_parents = {key.split(".", 1)[0] for key in PREFERENCE_KEYS}
    assert top_level_parents, "PREFERENCE_KEYS must not be empty for this test to bite"

    for parent in sorted(top_level_parents):
        cell = _reload_cell(table, parent)
        assert cell != "restart", (
            f"`{parent}` has a PREFERENCE_KEYS leaf, which is a LIVE "
            f"re-read at the agent/session layer, not `_HOT_RELOAD_FILES`- "
            f"gated `restart` — its `Reload` cell must carry a caveat "
            f"marker, not read the bare, unqualified `restart`: {cell!r}"
        )


# ── general syntactic gate: multi-layer rows can't have a bare Reload ──────
#
# lead-coder's own follow-up ruling (issuecomment-5379503229, superseding
# "defer the general form"): scoping the check to PREFERENCE_KEYS alone
# missed 2 REAL rows already in this same diff — `permissions` and
# `project_context_path` are BOTH multi-layer (`Declared in` names 2+
# layers) yet BOTH had a bare, unqualified `restart` in `Reload` before this
# fix, for reasons that have nothing to do with PREFERENCE_KEYS
# (`project_context_path`'s agent-layer override resolves once per agent at
# construction; `permissions`'s composes live via a #2285 reapply — neither
# is the ③ preference axis's live-property-reread mechanism ① already
# covers). The general, syntax-only form architect approved: a row whose
# `Declared in` cell names 2+ layers (contains "·") may NOT have a Reload
# cell that is a single bare token — it must carry a footnote marker, a
# layer-specific qualifier, or multiple tokens (e.g. `restart / hot`).
# Never checks VALUES, only shape — the same "syntax, not semantics" split
# `Declared in`'s own gate already draws against `Reload`/`File`.

_BARE_TOKEN = re.compile(r"^(restart|hot)$")


def _all_row_keys(table_text: str) -> "list[str]":
    """Every key with a row in the table, in source order — walks the SAME
    marker-bounded text every other helper reads, so a row this function
    can't see is a row nothing else in this module checks either."""
    return re.findall(r"^\| `([^`]+)` \|", table_text, re.MULTILINE)


def test_no_multi_layer_row_has_a_bare_single_token_reload_cell():
    """Tier 2: strip-falsifier for the GENERAL shape of the #5090 `Reload`
    drift — every row in the table (not just the `PREFERENCE_KEYS`-backed
    ones) whose `Declared in` cell spans 2+ layers must carry SOME marker
    on its `Reload` cell, syntactically: not a bare `restart` or `hot`.
    Checks shape only, never a specific value — a row could legitimately
    read `restart / hot` (multiple tokens) with no footnote at all.

    Strip-falsifier: reverting `permissions`' or `project_context_path`'s
    `Reload` cell to a bare `restart` (the exact 2 real rows this test
    caught, pre-fix) turns this red — verified locally."""
    table = _table_text()
    multi_layer_rows = [
        key for key in _all_row_keys(table) if "·" in _declared_in_cell(table, key)
    ]
    assert multi_layer_rows, "expected at least one multi-layer row for this test to bite"
    for key in multi_layer_rows:
        reload_cell = _reload_cell(table, key)
        assert not _BARE_TOKEN.match(reload_cell), (
            f"`{key}` is multi-layer (`Declared in` = "
            f"{_declared_in_cell(table, key)!r}) but "
            f"its `Reload` cell is a bare, unqualified token: {reload_cell!r} "
            f"— a multi-layer row's reload behaviour can differ BY layer; "
            f"a bare token asserts one value for all of them without saying so"
        )
