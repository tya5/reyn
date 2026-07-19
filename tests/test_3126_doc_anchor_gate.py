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

- **mkdocs** (`markdown.extensions.toc.slugify_unicode`, ``unicode_slugs=True``
  is the actual runtime behavior — confirmed empirically below): collapses
  consecutive hyphens, so an em-dash with a space on each side produces a
  *single* hyphen.
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
from markdown.extensions.toc import slugify_unicode, unique

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
_CODE_ANCHOR_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)#([^\s()\[\]{}'\"<>,;]+)")
# ``[text](#anchor)`` — a same-file markdown link.
_DOC_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")


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
    ``slugify_unicode`` + its ``unique`` dedup counter, both imported for
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
            else slugify_unicode(heading_text, "-")
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
    assert len(candidates) == 1, (
        f"{py_path}:{lineno} references {doc_name!r} — expected exactly one "
        f"docs/**/{Path(doc_name).name} match, found {len(candidates)}: {candidates}"
    )
    slugs = _slugs_for(candidates[0])
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


def test_unicode_heading_slug_preserved_by_canonical_slugify(tmp_path: Path) -> None:
    """Tier 1: mkdocs' toc ``unicode_slugs=True`` runtime behavior — a
    pure-Japanese heading resolves UNCHANGED. This only happens with
    ``unicode_slugs=True`` (the default ``slugify`` would produce an empty
    string), confirming the gate imports ``slugify_unicode`` — the function
    mkdocs' `toc` extension actually calls — not the ASCII-only sibling."""
    doc = tmp_path / "sample.md"
    doc.write_text("## 判断フロー\n", encoding="utf-8")
    assert "判断フロー" in heading_slugs(doc, use_github=False)


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
