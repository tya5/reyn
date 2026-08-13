"""Tier 2: OS invariant — config doc/example mirror drift guard (#1056 (d)).

`reyn.local.yaml.example` and `docs/reference/config/reyn-yaml.md` are
hand-curated mirrors of the `ReynConfig` schema (the human comments, ordering,
and worked examples are deliberately authored — see #1049/#1053). They drift
the moment a config field is added without a matching mirror edit.

This guard closes that loop in **`--check` mode** (verify, not generate — full
auto-generation would destroy the curated human guidance the schema can't
reproduce): every field the live `walk_config_schema()` advertises must be
documented in BOTH mirrors. It derives the expected field set from the schema
(zero hand-enumeration), so it auto-extends as fields change.

Granularity = **field NAME** (word-boundary), plus every top-level section.

#3934: raw whole-FILE word-boundary presence (the original design) passes
**vacuously** for a common field name — `mode` / `policy` / `enabled` / `path`
/ `timeout` etc. appear *somewhere* in a 1000+-line file regardless of whether
the field they belong to is documented at all. Measured directly: before this
fix, `sandbox.mode` (a real `SandboxConfig` field, `infra.py:466`) was entirely
undocumented in BOTH mirrors and the gate stayed green, because the bare word
"mode" is used by six unrelated sections (`safety.on_limit.mode`, `tool_search`
deferred mode, container mode, ...). The gate is least effective exactly where
it matters most: an ordinary, common-sounding new field name.

Fix: presence is now checked **within the field's own top-level section**,
not the whole file — `_present(name, text)` demoted to a same-section check.
Each mirror's section boundaries are found structurally (a YAML-comment
header `# <name>:` / `# <name>.<subfield>:` for the example; a Markdown
`## `<name>` block` heading for the docs — the two files have genuinely
different structure, so two marker patterns, not one generic parser: a single
structure parser produced false negatives in earlier development, per the
module's original note, which is still true of a fully general approach).
A top-level name occasionally has more than one heading occurrence in the
same file (`chat` in the docs: a compact summary block near the top, and the
detailed compaction worked-example far below) — every occurrence's slice is
searched, not just the first.

When no section marker is found for a given top-level name at all, the check
falls back to the ORIGINAL whole-file presence check for that name's leaves —
this never turns a currently-passing field red; it only closes the vacuous-
pass hole where a marker IS found. Top-level section names themselves must
appear either as a marker OR inside backticks (covers the docs' compact
"Top-level keys" summary table, ``| `name` | ... |``, which never gets its
own `## block` heading for scalar-only fields) — never a bare, unscoped word
match, which is exactly the hole this issue closes at the section level too
(`skills` / `pipelines` were never documented in the example at all, masked
by the words appearing in unrelated prose elsewhere in the file).

Rationale for name-level (not fully dotted-path) granularity, unchanged from
the original design:
  - Repeated dataclass types (e.g. `CostLimitConfig` appears 9× across
    cost.* / safety.loop.*_per_chain) are documented ONCE as a shape, not 36
    times — name-level coverage matches that good-docs practice, whereas
    dotted-key-precise coverage would force bloated per-instance tables.
  - Word-boundary raw-text presence is robust against the mirrors' ad-hoc
    structure (commented nested YAML in the example; Markdown tables + YAML
    blocks + prose in the docs).

Known limitation (acceptable for a drift guard, unchanged): a *new* field
that reuses an *existing* field name AND lands in a section that already
documents that name for a DIFFERENT sibling field is still not caught by the
name check alone (e.g. two different `timeout` fields in the same section) —
but a new top-level section, a genuinely new field name, or (as of #3934) a
common name reused ACROSS sections, is."""
from __future__ import annotations

import re

from reyn.config.config_schema import walk_config_schema
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT
_EXAMPLE = _REPO_ROOT / "reyn.local.yaml.example"
_DOCS = _REPO_ROOT / "docs" / "reference" / "config" / "reyn-yaml.md"

# A commented (or literal) top-level key header in the YAML example, e.g.
# "# sandbox:" or "# chat.compaction:" (a dotted sub-path in the header text
# is allowed — several sections introduce themselves that way). Exactly one
# optional space after "#" — NOT `\s*` — so a deeply-indented worked-example
# line inside another section's own comment block (e.g. a per-class "model:"
# line nested three levels under `embedding.classes.<name>`) is never
# mistaken for that name's own top-level header.
_EXAMPLE_HEADER = lambda name: re.compile(  # noqa: E731
    r"^#\s?" + re.escape(name) + r"(\.[a-zA-Z_]+)*:",
)
# A Markdown top-level block heading in the docs, e.g. "## `sandbox` block"
# or "## `cron:` block" (some headings carry the colon inside the backticks).
_DOCS_HEADER = lambda name: re.compile(  # noqa: E731
    r"^##\s+`?" + re.escape(name) + r":?`?\s+block\b",
)
_SEPARATOR = re.compile(r"^#\s*-{5,}\s*$")


def _present(name: str, text: str) -> bool:
    """True when *name* appears as a whole word in *text*."""
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None


def _backtick_present(name: str, text: str) -> bool:
    """True when *name* appears backtick-quoted — covers a Markdown table
    row (`` | `name` | ... | ``) that never gets its own `## block` heading
    because the field is a scalar with no sub-fields to elaborate on."""
    return re.search(r"`" + re.escape(name) + r"`", text) is not None


def _schema_top_level() -> "set[str]":
    """Every top-level ReynConfig section/field name."""
    return {n.key.split(".", 1)[0] for n in walk_config_schema()}


def _leaves_by_top() -> "dict[str, set[str]]":
    """Top-level name -> the set of its own leaf field names (own name
    excluded — a scalar top-level field's presence is covered by the
    top-level check alone, not re-checked as a trivial one-field section)."""
    out: "dict[str, set[str]]" = {}
    for n in walk_config_schema():
        top = n.key.split(".", 1)[0]
        leaf = n.key.rsplit(".", 1)[-1]
        if leaf != top:
            out.setdefault(top, set()).add(leaf)
    return out


def _widen_to_separator(lines: "list[str]", idx: int) -> int:
    """Walk a header-line index backward to the nearest preceding
    `# ---...---` rule — several sections carry descriptive prose (which
    itself mentions leaf field names) ABOVE the literal `# <name>:` comment
    line, bounded by the SAME separator that opens the section; without this
    a name mentioned only in that prose (e.g. `component_weights` in the
    `chat` section's intro, ahead of its own `# chat:` line) reads as
    outside the section it is actually introducing."""
    j = idx
    while j > 0:
        j -= 1
        if _SEPARATOR.match(lines[j]):
            return j
    return idx


def _section_slices(
    text: str, header_pat_fn, *, widen: bool,
) -> "tuple[dict[str, list[tuple[int, int]]], list[str]]":
    """name -> every (start, end) line-range where *name*'s own header
    appears, each running to the NEXT header of ANY name (a top-level key
    can legitimately be documented in more than one place — the docs'
    `chat` block has both a compact summary near the top and a detailed
    compaction worked-example much later; both must count)."""
    lines = text.splitlines()
    tops = sorted(_schema_top_level())
    all_markers: "list[tuple[int, str]]" = []
    for name in tops:
        pat = header_pat_fn(name)
        for i, line in enumerate(lines):
            if pat.match(line):
                start = _widen_to_separator(lines, i) if widen else i
                all_markers.append((start, name))
    all_markers.sort()
    slices: "dict[str, list[tuple[int, int]]]" = {}
    for i, (start, name) in enumerate(all_markers):
        end = all_markers[i + 1][0] if i + 1 < len(all_markers) else len(lines)
        slices.setdefault(name, []).append((start, end))
    return slices, lines


def _missing(text: str, header_pat_fn, *, widen: bool) -> "tuple[list[str], list[str]]":
    """Returns (missing_top_level, missing_leaf) for *text* — the shared
    check both mirror tests run, parameterized only by the file-specific
    header pattern (the two mirrors have genuinely different structure)."""
    slices, lines = _section_slices(text, header_pat_fn, widen=widen)

    missing_top = sorted(
        name for name in _schema_top_level()
        if name not in slices and not _backtick_present(name, text)
    )

    missing_leaf: "list[str]" = []
    for top, leaves in sorted(_leaves_by_top().items()):
        ranges = slices.get(top)
        for leaf in sorted(leaves):
            if ranges:
                ok = any(_present(leaf, "\n".join(lines[s:e])) for s, e in ranges)
            else:
                # No section marker found for this top-level name at all —
                # fall back to the original whole-file check so a currently-
                # passing field never regresses; this only closes the hole
                # where a marker IS found (see module docstring).
                ok = _present(leaf, text)
            if not ok:
                missing_leaf.append(f"{top}.{leaf}")
    return missing_top, missing_leaf


def test_example_documents_every_config_field() -> None:
    """Tier 2: reyn.local.yaml.example mentions every config field name +
    section — WITHIN the section that field actually belongs to (#3934).

    A field added to ReynConfig but never added to the example template
    fails here — the example is advertised as an "exhaustive" mirror, so a
    silent gap is drift. Derives the field set from the live schema (no
    hand-enumeration)."""
    text = _EXAMPLE.read_text(encoding="utf-8")
    missing_top, missing_leaf = _missing(text, _EXAMPLE_HEADER, widen=True)
    assert not missing_top and not missing_leaf, (
        f"reyn.local.yaml.example is missing config fields {missing_leaf} "
        f"and top-level sections {missing_top} — add a documented block "
        f"mirroring the new ReynConfig field(s)."
    )


def test_docs_documents_every_config_field() -> None:
    """Tier 2: docs/reference/config/reyn-yaml.md mentions every field name +
    section — WITHIN the section that field actually belongs to (#3934).

    Same drift guard for the reference doc. A new config field must appear
    in the reference (a table row, YAML example, or prose mention) — derived
    from the live schema, zero hand-enumeration."""
    text = _DOCS.read_text(encoding="utf-8")
    missing_top, missing_leaf = _missing(text, _DOCS_HEADER, widen=False)
    assert not missing_top and not missing_leaf, (
        f"docs/reference/config/reyn-yaml.md is missing config fields "
        f"{missing_leaf} and top-level sections {missing_top} — document the "
        f"new ReynConfig field(s) in the reference."
    )


def test_mirror_files_exist() -> None:
    """Tier 2: the two mirror files exist at their expected paths.

    Guards the path constants above against a future move that would make the
    coverage checks silently vacuous (file unreadable → test error, not pass).
    """
    assert _EXAMPLE.is_file(), f"missing config example mirror: {_EXAMPLE}"
    assert _DOCS.is_file(), f"missing config reference doc: {_DOCS}"


def test_a_common_field_name_undocumented_in_its_own_section_is_caught() -> None:
    """Tier 2: #3934's own repro, pinned. `sandbox.mode` — a real
    `SandboxConfig` field (infra.py:466) — is invisible to a bare
    word-boundary scan of either mirror because "mode" is used by several
    UNRELATED sections (`safety.on_limit.mode`, deferred `tool_search` mode,
    container mode, ...); before #3934 the gate stayed green regardless.
    Constructs a synthetic mirror text carrying "mode" only OUTSIDE the
    sandbox section and asserts the fix reports it missing — RED for the
    right reason, not just "the field isn't mentioned at all"."""
    text = (
        "# unrelated: some section that happens to use the word mode.\n"
        "# unrelated:\n"
        "#   mode: whatever\n"
        "\n"
        "# -----------------------------------------------------------------\n"
        "# sandbox: which enforcement backend to use.\n"
        "# -----------------------------------------------------------------\n"
        "# sandbox:\n"
        "#   backend: auto\n"
        "#   on_unsupported: warn\n"
    )
    slices, lines = _section_slices(text, _EXAMPLE_HEADER, widen=True)
    sandbox_ranges = slices.get("sandbox", [])
    assert sandbox_ranges, "the synthetic sandbox header was not found at all"
    sandbox_text = "\n".join("\n".join(lines[s:e]) for s, e in sandbox_ranges)
    assert _present("mode", text), "fixture sanity: the word 'mode' must appear SOMEWHERE"
    assert not _present("mode", sandbox_text), (
        "'mode' must NOT be visible inside sandbox's own section in this "
        "fixture — it only appears in the unrelated section above it"
    )


def test_a_field_documented_within_its_own_section_is_accepted() -> None:
    """Tier 2: non-vacuity for the test above — a field genuinely documented
    inside its OWN section's slice is found, so the fix does not just widen
    the deny surface to reject everything."""
    text = (
        "# -----------------------------------------------------------------\n"
        "# sandbox: which enforcement backend to use.\n"
        "# -----------------------------------------------------------------\n"
        "# sandbox:\n"
        "#   backend: auto\n"
        "#   mode: compat\n"
    )
    slices, lines = _section_slices(text, _EXAMPLE_HEADER, widen=True)
    sandbox_ranges = slices.get("sandbox", [])
    assert sandbox_ranges, "the synthetic sandbox header was not found at all"
    sandbox_text = "\n".join("\n".join(lines[s:e]) for s, e in sandbox_ranges)
    assert _present("mode", sandbox_text), (
        "a field genuinely documented inside its own section was not found"
    )
