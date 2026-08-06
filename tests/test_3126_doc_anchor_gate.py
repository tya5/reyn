"""Tier 1: contract — #3126 CI gate: every ``<doc>.md#<anchor>`` cited from a
``src/reyn/`` code comment, and every same-file ``[text](#anchor)`` markdown
link inside ``docs/``, resolves to a REAL heading slug — computed with the
same slugify function mkdocs' ``toc`` extension (``permalink: true``,
``.mkdocs/mkdocs.yml``) uses at build time, never a hand-typed guess.

Recurrence this closes (see the issue for the full writeup): #3039 (one
anchor, caught in co-vet review) and #3124 (~50 pointer comments in
``session.py``, two of which cited anchors that did not exist at all). Both
times the anchor was typed by guessing at the heading text; review didn't
catch it until someone ran the real slugify function against the real
heading. This gate runs it every time, over every anchor reference the two
scopes (code-comment / doc-internal) contain — enumerated from the live
filesystem, never a hand-curated allowlist, so a newly written pointer
comment or doc link is covered automatically.

Two renderers, two slug algorithms — the caveat this gate exists to encode:

- **mkdocs** (`markdown.extensions.toc.slugify` — the ASCII-only default;
  ``.mkdocs/mkdocs.yml``'s ``toc:`` block sets only ``permalink: true``, no
  ``slugify:`` override, so `TocExtension`'s own default applies, confirmed
  live: ``TocExtension().getConfig("slugify")`` returns ``slugify``, not
  ``slugify_unicode``): collapses consecutive hyphens, so an em-dash with a
  space on each side produces a *single* hyphen, AND strips non-ASCII
  characters entirely — a CJK-only heading slugifies to ``""``, which
  `unique()` (imported for real, same as mkdocs' own treeprocessor calls)
  turns into an opaque, order-dependent ``_1``/``_2``/... fallback id. This
  gate previously imported ``slugify_unicode`` instead and asserted a pure-CJK
  heading survives unchanged (#3667 co-vet, lead-coder + docs-maintainer,
  2026-08): that model was never actually checked against a real mkdocs
  build, only against itself — a live build showed real ids like
  ``ad-hoc-inline`` (CJK suffix dropped) where the old model computed
  ``ad-hoc-inline-起動``, so citations correct under the old model were
  silently broken on the published site. Confirmed the real function
  produces the exact real-world id in both cases before making this fix.
- **GitHub's own renderer**: does NOT collapse consecutive hyphens, so the
  same em-dash heading gets a *double* hyphen there.

``docs/deep-dives/**`` (plus any other ``.mkdocs/mkdocs.yml``
``exclude_docs:`` entry, read from that file at test time — never
hardcoded) is excluded from the built mkdocs site, so anchors there are only
ever consumed via GitHub's own renderer (repo browsing); this gate validates
those against the GitHub algorithm instead of mkdocs'. Every other doc under
``docs/`` needs the mkdocs slug (the one the published site actually
mints).

Implementation choice (pytest vs. a standalone script like
``scripts/verify_module_docstrings.py``): pytest. #3000 made the ``pytest``
CI job blocking and up-to-date-required, so a pytest-based checker gets
enforcement with no new CI workflow to wire up — the tradeoff
``docs/deep-dives/contributing/testing.ja.md`` § 判断フロー Q1 asks
("who notices when this breaks?") is answered "the OS itself, mechanically,
on every push", which is exactly Tier 1's contract shape (an external-boundary
correctness fact a `pytest` collection run enforces), not a one-off
implementation-level pin.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from markdown.extensions.toc import slugify, unique

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs"
_SRC = _REPO_ROOT / "src" / "reyn"
_MKDOCS_CFG = _REPO_ROOT / ".mkdocs" / "mkdocs.yml"

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HTML_ANCHOR_RE = re.compile(r'<a\s+(?:id|name)="([^"]+)"')
# python-markdown attr_list shorthand for an explicit heading id: either
# ``{: #custom-id ...}`` or ``{#custom-id ...}``, trailing the heading text.
# When present it REPLACES the auto-computed slug (toc's treeprocessor only
# slugifies when the element has no id yet) — a real, live case in this repo
# (pipeline-dsl.ja.md, permission-model.md, mcp.ja.md,
# 0014-wal-size-safety-net.md all use this form), so skipping it would make
# the gate cry wolf on correct docs.
_ATTR_ID_RE = re.compile(r"\{:?\s*#([A-Za-z0-9_-]+)[^}]*\}\s*$")
_GH_STRIP_RE = re.compile(r"[^\w\- ]", re.UNICODE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# `<doc-basename>.md#<anchor>` inside a src/reyn/*.py comment or docstring —
# e.g. ``# ..., see session-construction.md#family-1-audit-event-spine-p6``.
# Scope: bare-word ``foo.md#anchor`` mentions only — not a reference-style
# markdown link and not a quoted/URL-wrapped form, both absent from
# src/reyn/*.py today (measured at #3667/#3126 co-vet, 2026-08).
_CODE_ANCHOR_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)#([^\s()\[\]{}'\"<>,;]+)")
# ``[text](#anchor)`` — a same-file markdown link. Inline syntax only — a
# reference-style link (``[text][ref]``) or a raw ``<a href="#anchor">``
# is not extracted (repo-wide count when this note was added: 0 of either
# form, per lead-coder's measurement, #3667).
_DOC_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")
# ``[text](other.md#anchor)`` — a cross-file markdown link. Only the
# EXCLUDED-target subset of these is this module's concern (#3672): a
# published target is already ground-truthed against real mkdocs-built
# HTML by ``scripts/check_doc_anchors.py``, which this gate would only
# duplicate with the less-reliable model. An excluded target (e.g.
# ``deep-dives/**``) is never built, so it has no HTML to check against —
# GitHub's renderer is the only real reader of that anchor, and this
# gate's ``_github_slugify`` is the only place that's modeled.
_CROSS_FILE_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)#([^)\s]+)\)")


def _exclude_prefixes() -> list[str]:
    """``exclude_docs:`` entries from ``.mkdocs/mkdocs.yml``, read live (not
    hardcoded) — the docs paths mkdocs never builds, so their anchors are
    GitHub-renderer-only."""
    cfg = yaml.safe_load(_MKDOCS_CFG.read_text(encoding="utf-8"))
    raw = cfg.get("exclude_docs", "") or ""
    return [
        line.strip().rstrip("/")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_excluded(doc_rel_posix: str, exclude_prefixes: list[str]) -> bool:
    return any(
        doc_rel_posix == prefix or doc_rel_posix.startswith(prefix + "/")
        for prefix in exclude_prefixes
    )


def _github_slugify(text: str) -> str:
    """GitHub's heading-anchor algorithm: lowercase, strip everything but
    word chars / space / hyphen, then spaces -> hyphens. Unlike mkdocs' toc
    extension, consecutive hyphens are NOT collapsed — the #3039 caveat."""
    text = text.lower()
    text = _GH_STRIP_RE.sub("", text)
    return text.replace(" ", "-")


def heading_slugs(md_path: Path, *, use_github: bool) -> set[str]:
    """The full set of real, resolvable anchors for every heading in
    *md_path*: either mirroring mkdocs' `toc` extension (canonical
    ``slugify`` + its ``unique`` dedup counter, both imported for
    real — never reimplemented/faked) or GitHub's renderer, per
    *use_github*. Also picks up explicit ``<a id="...">``/``<a name="...">``
    HTML anchors and attr_list ``{#id}`` heading-id overrides, both real
    anchor-declaration shapes present in this repo's docs today.
    """
    seen_ids: set[str] = set()
    slugs: set[str] = set()
    in_fence = False
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _HTML_ANCHOR_RE.finditer(line):
            slugs.add(m.group(1))
        heading_m = _HEADING_RE.match(line)
        if not heading_m:
            continue
        heading_text = heading_m.group(2)
        attr_m = _ATTR_ID_RE.search(heading_text)
        if attr_m:
            slugs.add(unique(attr_m.group(1), seen_ids))
            continue
        heading_text = _HTML_TAG_RE.sub("", heading_text).strip()
        if not heading_text:
            continue
        slug = (
            _github_slugify(heading_text)
            if use_github
            else slugify(heading_text, "-")
        )
        slugs.add(unique(slug, seen_ids))
    return slugs


def _all_md_files() -> list[Path]:
    return sorted(_DOCS.rglob("*.md"))


def _doc_internal_links(md_path: Path) -> list[tuple[int, str]]:
    """``(lineno, anchor)`` for every same-file ``[text](#anchor)`` link,
    skipping fenced code blocks (markdown syntax shown as an example, not a
    real link)."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _DOC_LINK_RE.finditer(line):
            out.append((i, m.group(1)))
    return out


def _cross_file_links(md_path: Path) -> list[tuple[int, str, str]]:
    """``(lineno, target_path, anchor)`` for every cross-file
    ``[text](other.md#anchor)`` link, skipping fenced code blocks."""
    out: list[tuple[int, str, str]] = []
    in_fence = False
    for i, line in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _CROSS_FILE_LINK_RE.finditer(line):
            target_path, anchor = m.groups()
            out.append((i, target_path, anchor))
    return out


def _code_comment_anchor_refs() -> list[tuple[Path, int, str, str]]:
    """``(py_path, lineno, doc_basename.md, anchor)`` for every
    ``<doc>.md#<anchor>`` reference under ``src/reyn/``."""
    refs: list[tuple[Path, int, str, str]] = []
    for py in sorted(_SRC.rglob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for m in _CODE_ANCHOR_REF_RE.finditer(line):
                doc_name, anchor = m.groups()
                anchor = anchor.rstrip(").,;:'\"")
                refs.append((py, i, doc_name, anchor))
    return refs


_EXCLUDE_PREFIXES = _exclude_prefixes()
_MD_FILES = _all_md_files()
_CODE_REFS = _code_comment_anchor_refs()
_DOC_LINKS: list[tuple[Path, int, str]] = [
    (md, lineno, anchor)
    for md in _MD_FILES
    for lineno, anchor in _doc_internal_links(md)
]

# Floors, not exact pins (#3667 co-vet) — measured 109 code-comment refs /
# 203 doc-internal links when added. `> 0` alone only catches TOTAL
# extraction failure; a partial regex regression (one alternative in
# _CODE_ANCHOR_REF_RE / _DOC_LINK_RE silently stops matching) would still
# pass a bare non-zero check while quietly covering a fraction of the repo.
# Deliberately below the measured counts to tolerate organic doc/comment
# growth and removal.
assert len(_CODE_REFS) >= 60, (
    f"Extracted only {len(_CODE_REFS)} code-comment anchor refs from "
    "src/reyn/ — measured 109 when this floor was added. Check "
    "_CODE_ANCHOR_REF_RE before trusting this gate."
)
assert len(_DOC_LINKS) >= 120, (
    f"Extracted only {len(_DOC_LINKS)} same-file doc-internal links from "
    "docs/ — measured 203 when this floor was added. Check _DOC_LINK_RE "
    "before trusting this gate."
)

# All cross-file links, regardless of target — the floor below guards
# _CROSS_FILE_LINK_RE itself, since the excluded-only subset filtered from
# it can legitimately shrink to 0 (every excluded-target citation removed
# is a valid end state, so THAT count can't carry a floor).
_CROSS_FILE_LINKS_ALL: list[tuple[Path, int, str, str]] = [
    (md, lineno, target_path, anchor)
    for md in _MD_FILES
    for lineno, target_path, anchor in _cross_file_links(md)
]
assert len(_CROSS_FILE_LINKS_ALL) >= 200, (
    f"Extracted only {len(_CROSS_FILE_LINKS_ALL)} cross-file markdown links "
    "from docs/ — check _CROSS_FILE_LINK_RE before trusting this gate."
)


def _resolve_cross_file_target(doc_path: Path, target_path: str) -> Path | None:
    """The docs/-relative path *target_path* resolves to, from *doc_path* —
    or ``None`` if it resolves outside ``docs/`` entirely (a different
    hazard ``mkdocs build --strict`` already fails on, not this gate's
    concern)."""
    target_abs = (doc_path.parent / target_path).resolve()
    try:
        return target_abs.relative_to(_DOCS.resolve())
    except ValueError:
        return None


# The one arm neither anchor gate previously covered (#3672): a link from a
# PUBLISHED doc to a cross-file anchor whose TARGET is excluded from the
# mkdocs build. check_doc_anchors.py only inspects published targets (an
# excluded target is unbuilt — no HTML to check); this gate's other scopes
# only ever look at the CITING file's own excluded-vs-published status,
# never the cross-file target's. Verified against GitHub's real rendering
# before adding this arm (2026-08-05, https://github.com/tya5/reyn/blob/
# main/docs/deep-dives/contributing/testing.md and
# .../proposals/0064-plugin-model.md and .../0066-...md — all 3 real
# citations' `_github_slugify` predictions matched the live page's actual
# `href="#..."` fragment exactly, including the em-dash double-hyphen case).
#
# Originally scoped to a PUBLISHED citing doc only (#3672's landing PR,
# #3696), deliberately excluding excluded-doc-to-excluded-doc links: the
# `docs/deep-dives/journal/**` historical dogfood/insight write-ups
# cross-link each other constantly, and building this arm's first version
# found 9 such links, 8 already dangling (#3697) — flagged rather than
# silently repaired, since fixing them read at the time as touching a
# historical corpus's own content. lead-coder ruling (#3697): repairing a link
# is NOT rewriting what a record claims — the claim, conclusion, and any
# measured value are untouched; only the citation's spelling changes to
# reach the same real section. All 8 repaired on that basis, and this arm
# widened to cover excluded-source links too, so the 8/9 density doesn't
# silently return the day the next citation goes stale — a one-time
# repair with no gate is the exact "covers what existed when it ran, not
# what exists now" shape #3718 named the same day.
_CROSS_FILE_EXCLUDED_LINKS: list[tuple[Path, int, Path, str]] = []
for _doc, _lineno, _target_path, _anchor in _CROSS_FILE_LINKS_ALL:
    _target_rel = _resolve_cross_file_target(_doc, _target_path)
    if _target_rel is not None and _is_excluded(
        _target_rel.as_posix(), _EXCLUDE_PREFIXES
    ):
        _CROSS_FILE_EXCLUDED_LINKS.append((_doc, _lineno, _DOCS / _target_rel, _anchor))


def _slugs_for(doc_path: Path) -> set[str]:
    rel = doc_path.relative_to(_DOCS).as_posix()
    excluded = _is_excluded(rel, _EXCLUDE_PREFIXES)
    return heading_slugs(doc_path, use_github=excluded)


# ─── scope 1: code-comment anchors (src/reyn/) ──────────────────────────────


@pytest.mark.parametrize(
    "py_path,lineno,doc_name,anchor",
    _CODE_REFS,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{ln}" for p, ln, _, _ in _CODE_REFS],
)
def test_code_comment_anchor_resolves(
    py_path: Path, lineno: int, doc_name: str, anchor: str
) -> None:
    """Tier 1: a ``<doc>.md#<anchor>`` pointer comment under ``src/reyn/``
    resolves to a real heading slug of the doc it names — not a hand-typed
    guess (#3039 / #3124 recurrence)."""
    candidates = [p for p in _MD_FILES if p.name == Path(doc_name).name]
    assert candidates, (
        f"{py_path}:{lineno} references {doc_name!r} — no docs/**/"
        f"{Path(doc_name).name} match found"
    )
    # A citation that carries directories (``docs/reference/cli/mcp.md``) already
    # says WHICH same-named doc it means, so honour it before declaring ambiguity:
    # match on the longest path SUFFIX the citation spells out. Resolving on the
    # bare basename alone made every colliding basename uncitable — 32 of the 744
    # docs share a name (``events.md`` x3, ``index.md`` x4, ``README.md`` x18), so
    # a fully-qualified, correct pointer to any of them failed as "ambiguous".
    # Falls through to the basename set when the citation is bare (one path
    # segment), which keeps the original ambiguity error for genuinely
    # under-specified references.
    if len(Path(doc_name).parts) > 1:
        suffix_matches = [
            p for p in candidates
            if p.as_posix().endswith("/" + Path(doc_name).as_posix())
        ]
        if suffix_matches:
            candidates = suffix_matches
    resolved_doc, *ambiguous_rest = candidates
    assert not ambiguous_rest, (
        f"{py_path}:{lineno} references {doc_name!r} ambiguously — multiple "
        f"docs/**/{Path(doc_name).name} matches: {candidates}"
    )
    slugs = _slugs_for(resolved_doc)
    assert anchor in slugs, (
        f"{py_path}:{lineno} cites anchor {anchor!r} in {doc_name!r}, which has "
        f"no such heading slug. Real slugs: {sorted(slugs)}"
    )


# ─── scope 2: doc-internal cross-links ([text](#anchor)) ───────────────────


@pytest.mark.parametrize(
    "doc_path,lineno,anchor",
    _DOC_LINKS,
    ids=[f"{d.relative_to(_REPO_ROOT)}:{ln}" for d, ln, _ in _DOC_LINKS],
)
def test_doc_internal_link_resolves(doc_path: Path, lineno: int, anchor: str) -> None:
    """Tier 1: a same-file ``[text](#anchor)`` markdown link resolves to a
    real heading slug in that same doc — the mkdocs slug for docs the site
    builds, the GitHub-renderer slug for ``.mkdocs/mkdocs.yml``
    ``exclude_docs:`` paths (e.g. ``docs/deep-dives/**``), which never get an
    mkdocs-built id."""
    slugs = _slugs_for(doc_path)
    assert anchor in slugs, (
        f"{doc_path}:{lineno} links to #{anchor}, no such heading slug in this "
        f"doc. Real slugs: {sorted(slugs)}"
    )


# ─── scope 3: cross-file links into excluded (unbuilt) targets (#3672) ─────


@pytest.mark.parametrize(
    "doc_path,lineno,target_path,anchor",
    _CROSS_FILE_EXCLUDED_LINKS,
    ids=[
        f"{d.relative_to(_REPO_ROOT)}:{ln}"
        for d, ln, _, _ in _CROSS_FILE_EXCLUDED_LINKS
    ],
)
def test_cross_file_link_into_excluded_target_resolves(
    doc_path: Path, lineno: int, target_path: Path, anchor: str
) -> None:
    """Tier 1: a cross-file ``[text](other.md#anchor)`` link whose TARGET is
    excluded from the mkdocs build (e.g. ``deep-dives/**``) resolves to a
    real heading slug there — checked against GitHub's renderer (the only
    real reader of an unbuilt page's anchor), never mkdocs' (#3672: the one
    arm neither this file's other scopes nor ``check_doc_anchors.py``'s
    HTML-ground-truth covered)."""
    slugs = heading_slugs(target_path, use_github=True)
    assert anchor in slugs, (
        f"{doc_path}:{lineno} links to {target_path.relative_to(_DOCS)}#{anchor}, "
        f"no such heading slug there (GitHub renderer). Real slugs: {sorted(slugs)}"
    )


# ─── regression witnesses: the two real #3124 bad-anchor shapes ────────────


def test_regression_3124_double_hyphen_anchor_is_rejected(tmp_path: Path) -> None:
    """Tier 1: regression witness for #3124 — a "Family N — X (Y)" heading's
    em-dash (spaces on both sides) collapses to a SINGLE hyphen under
    mkdocs' real slugify (``family-1-audit-event-spine-p6``); the
    double-hyphen anchor #3124 actually shipped
    (``family-1--audit-event-spine-p6``) must NOT validate against a
    non-excluded doc's real slugs."""
    doc = tmp_path / "sample.md"
    doc.write_text("## Family 1 — Audit-event spine (P6)\n", encoding="utf-8")
    slugs = heading_slugs(doc, use_github=False)
    assert "family-1-audit-event-spine-p6" in slugs
    assert "family-1--audit-event-spine-p6" not in slugs


def test_regression_3124_nonexistent_anchor_is_rejected(tmp_path: Path) -> None:
    """Tier 1: regression witness for #3124 — the shipped pointer comments
    also cited ``#identity`` and ``#family-4``, neither of which was ever a
    real heading (the real headings are "Identity (the `Agent` value
    object) — FP-0043 Stage 2" and "Family 3 — Hook-event / reactivity" /
    "Family 5 — Retrieval" — no bare "Family 4" section exists at all)."""
    doc = tmp_path / "sample.md"
    doc.write_text(
        "## Identity (the `Agent` value object) — FP-0043 Stage 2\n"
        "## Family 3 — Hook-event / reactivity\n"
        "## Family 5 — Retrieval\n",
        encoding="utf-8",
    )
    slugs = heading_slugs(doc, use_github=False)
    assert "identity" not in slugs
    assert "family-4" not in slugs


# ─── caveat witnesses: the two mechanisms this gate exists to distinguish ──


def test_caveat_github_and_mkdocs_diverge_on_consecutive_hyphens(
    tmp_path: Path,
) -> None:
    """Tier 1: the #3039 caveat, both real renderers' algorithms exercised
    directly — GitHub's own renderer preserves the double hyphen an em-dash
    with adjacent spaces produces; mkdocs' ``toc`` extension collapses it to
    one. A gate that used only one algorithm for every doc would misjudge
    the other renderer's docs."""
    heading = "Faking a data/state object — same ban, sharper failure mode"
    doc = tmp_path / "sample.md"
    doc.write_text(f"#### {heading}\n", encoding="utf-8")
    mkdocs_slugs = heading_slugs(doc, use_github=False)
    github_slugs = heading_slugs(doc, use_github=True)
    assert "faking-a-datastate-object-same-ban-sharper-failure-mode" in mkdocs_slugs
    assert "faking-a-datastate-object--same-ban-sharper-failure-mode" in github_slugs


def test_pure_cjk_heading_falls_back_to_opaque_ordered_id(tmp_path: Path) -> None:
    """Tier 1: mkdocs' actual runtime slugify (the ASCII-only default —
    ``.mkdocs/mkdocs.yml`` sets no ``slugify:`` override) strips a pure-CJK
    heading's text to ``""``; `unique()` then assigns an opaque,
    ORDER-DEPENDENT ``_1``/``_2``/... id — the same fallback mkdocs' own
    treeprocessor uses, since `unique` is imported for real, not
    reimplemented. Confirmed against a live build: this is why a
    pure-Japanese heading's citation must use an explicit
    ``{#custom-id}`` override, never the heading text itself, and why
    inserting a heading above shifts every anchor below it silently."""
    doc = tmp_path / "sample.md"
    doc.write_text("## 判断フロー\n## 別の見出し\n", encoding="utf-8")
    slugs = heading_slugs(doc, use_github=False)
    assert "判断フロー" not in slugs
    assert {"_1", "_2"} <= slugs


def test_attr_list_explicit_id_overrides_autoslug(tmp_path: Path) -> None:
    """Tier 1: ``## Heading text {#custom-id}`` (python-markdown attr_list
    shorthand) sets the heading's real id explicitly — the auto-computed
    slug of the heading text is NOT also registered. Encodes a false-positive
    class discovered while building this gate against the live repo
    (pipeline-dsl.ja.md, permission-model.md, mcp.ja.md,
    0014-wal-size-safety-net.md all use this form for a heading whose visible
    text would otherwise slugify to something else)."""
    doc = tmp_path / "sample.md"
    doc.write_text(
        "## ステップ間のデータフロー {#data-flow-between-steps}\n", encoding="utf-8"
    )
    slugs = heading_slugs(doc, use_github=False)
    assert "data-flow-between-steps" in slugs


def test_exclude_prefixes_read_from_mkdocs_config_not_hardcoded() -> None:
    """Tier 1: the excluded-docs classification comes from
    ``.mkdocs/mkdocs.yml`` at test time — if a maintainer adds or removes an
    ``exclude_docs:`` entry, this gate's mkdocs-vs-GitHub choice follows it
    without a code change here."""
    assert "deep-dives" in _EXCLUDE_PREFIXES
    assert _is_excluded("deep-dives/foo/bar.md", _EXCLUDE_PREFIXES) is True
    assert _is_excluded("reference/runtime/foo.md", _EXCLUDE_PREFIXES) is False
