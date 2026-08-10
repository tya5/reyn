#!/usr/bin/env python3
"""Check every ](path#anchor) / ](#anchor) markdown link in docs/ against the
anchors mkdocs actually generated — not a slug re-derivation.

Motivation (#3557/#3592/architect's anchor audit, 2026-08): `docs/concepts/
architecture/charter.md` cited its 42 evidence anchors as `feature-map.md:NNN`
line numbers. A follow-up census found 0/42 still correct (12 dead, 30
pointing at the wrong row) — `feature-map.md` is edited far more often than
citations referencing it, and a line number degrades silently: mkdocs never
complains, because the number still "resolves" to *some* line, just not the
one the citation meant. Converting those 42 citations to quoted-text
substrings surfaced the defect (#3603); this script generalizes the same
self-verifying idea to heading ANCHORS (`#some-heading`), which have their
own silent-drift shape — `mkdocs build --strict` already flags a dangling
*file* reference, but never checks whether `#anchor` actually exists on the
target page.

A parallel audit (461 links repo-wide) found 44 such dangling anchors, in
several distinct sub-shapes: an em-dash heading slugified differently than
the citer assumed (GitHub's slugger doubles the hyphen; mkdocs' Python-
Markdown slugifier collapses it to one), a missing underscore, a citation
into a section that was renamed or never existed, and JA headings with no
ASCII/code-span content, which mkdocs assigns an opaque, ORDER-DEPENDENT
`_N` id (inserting one heading above shifts every anchor below it to point
at a different section without ever failing to resolve).

## Two categories, one gate

Only ONE of the two: a genuinely dangling anchor.

- **`ANCHOR_NOT_FOUND`** — the target page IS published, but the cited
  anchor is not among its actual ids. This is the class this gate FAILS on.
- **`EXCLUDED_TARGET`** — the linking page is published, but the target file
  matches an `exclude_docs` pattern in `.mkdocs/mkdocs.yml` (`deep-dives/` and
  friends) and was never built at all, so there is no HTML to check the
  anchor against. This is a real, DIFFERENT hazard (dead for a site reader,
  live for a GitHub reader — the same "answer depends which face you read
  from" shape as #3039's dual-slug problem) but a distinct decision from
  "cited the wrong slug." Reported, not failed: fixing it means either
  un-excluding the target or removing ~66 pre-existing cross-references
  repo-wide, a call for whoever owns the `exclude_docs` boundary, not a
  side effect of landing this gate. Silently ignoring it would be the
  exact defect this doc calls out elsewhere ("known, therefore fine" belongs
  in code as an explicit, named category — not dropped from a report).

## Vacuity guard

If this script's link-extraction regex ever finds ZERO links, that means
the regex broke, not that docs/ suddenly has no cross-references — it exits
loud rather than reporting a meaningless "0 problems found."

## A second, independent gate lives in this same file: `check_deep_dives_link_existence`

The `EXCLUDED_TARGET` category above is real but not enforced — the site
build correctly skips `deep-dives/` (never published, so a dead link there
can't break the site), but `deep-dives/decisions/` and `deep-dives/proposals/`
carry ADRs and proposals a reader follows directly on GitHub: a broken
supersede-chain link there strands the reader even though the site itself
is fine (#4021, found when `ADR-0034` was unreadable-by-broken-link for
three months and nobody's "gate passed" report ever covered it — the
EXISTING gate here excludes `deep-dives/` explicitly, and no OTHER gate in
the repo checks a non-anchor `.md` link's target at all, anchor-bearing or
not). `check_deep_dives_link_existence()` closes that gap for exactly the
subdirs that are read as current reference (`decisions/`, `proposals/`,
`contributing/`, `spec/` — not `journal/`/`research/`, which are dated
records where an old link is historically correct, not broken) — existence
only, no anchor check (there's no built HTML to check an anchor against).
`main()` runs it first, unconditionally (no `site/` dependency), and folds
its exit code into the script's own.

Run standalone: `python scripts/check_doc_anchors.py` (the deep-dives check
runs immediately; the anchor check below it needs `mkdocs build --strict -f
.mkdocs/mkdocs.yml` first, which this script assumes already ran and left
its output in `site/` at the repo root — wired as an additional step in the
same CI job right after that build, in `.github/workflows/test.yml`, so no
second build and no new dependency).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
SITE = REPO / "site"
MKDOCS_YML = REPO / ".mkdocs" / "mkdocs.yml"

# Inline `[text](path#anchor)` / `[text](#anchor)` only — reference-style
# links (`[text][ref]`) and raw `<a id="...">` anchors are not extracted
# (repo-wide count as of this gate landing: 0 reference-style, 0 raw HTML
# anchors, 461 inline — lead-coder measured, #3667).
LINK_RE = re.compile(r"\]\(([^)\s]+\.md#[^)\s]+|#[^)\s]+)\)")
ID_RE = re.compile(r'id="([^"]+)"')

# Bare relative `.md` link, anchor optional — for the deep-dives
# existence-only gate below (`check_deep_dives_link_existence`). LINK_RE
# above deliberately requires an anchor (or is anchor-only); a plain
# `[text](0067-....md)` link with no `#` never matches it, so this is a
# second, narrower pattern rather than a LINK_RE rewrite — the two gates
# check different things (built-page anchor existence vs. raw file
# existence) and reusing one regex for both would make either widen to fit
# the other's shape.
MD_LINK_RE = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)\s]+)?\)")


def _load_exclude_patterns() -> list[str]:
    """Read the gitignore-style patterns under `exclude_docs:` in mkdocs.yml."""
    text = MKDOCS_YML.read_text(encoding="utf-8")
    marker = "exclude_docs: |"
    start = text.index(marker) + len(marker)
    block_lines: list[str] = []
    for line in text[start:].splitlines():
        if line and not line.startswith(" "):
            break
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            block_lines.append(stripped.rstrip("/"))
    return block_lines


def _is_excluded(doc_relpath: Path, patterns: list[str]) -> bool:
    posix = doc_relpath.as_posix()
    return any(posix == p or posix.startswith(p + "/") for p in patterns)


def _require_vacuity_floor(count: int, floor: int, message: str) -> None:
    """Fail loud (not an `assert`) when a link-extraction count falls below
    its measured floor — `assert` is stripped entirely under `python -O`
    (lead-coder, 2026-08-10, follow-up to #4021: fixing only one of this
    script's two floors to survive `-O` would make "a floor exists" true
    only in appearance, since CI doesn't pass `-O` today but nothing pins
    that). `SystemExit` has no such escape hatch."""
    if count < floor:
        raise SystemExit(message)


def _md_to_html_path(md_relpath: str) -> Path:
    """Mirror mkdocs' directory-url convention: foo/bar.md -> foo/bar/index.html,
    foo/bar.ja.md -> ja/foo/bar/index.html, index.md -> index.html."""
    p = Path(md_relpath)
    if p.name.endswith(".ja.md"):
        stem = p.name[: -len(".ja.md")]
        rel = p.parent / stem
        prefix = Path("ja")
    else:
        stem = p.stem
        rel = p.parent / stem
        prefix = Path(".")
    if stem == "index":
        html_rel = rel.parent / "index.html" if str(rel.parent) != "." else Path("index.html")
    else:
        html_rel = rel / "index.html"
    return SITE / prefix / html_rel


def _get_ids(html_path: Path, cache: dict[Path, set[str] | None]) -> set[str] | None:
    if html_path not in cache:
        if html_path.exists():
            text = html_path.read_text(encoding="utf-8", errors="replace")
            cache[html_path] = set(ID_RE.findall(text))
        else:
            cache[html_path] = None
    return cache[html_path]


# The 4 deep-dives/ subdirs read as CURRENT reference material — an ADR's
# supersede chain, a proposal's own links, the contributing/spec docs — where
# a dead relative link is a real defect for anyone reading the repo directly
# (#4021). `journal/` and `research/` are deliberately excluded: they are
# dated, point-in-time records, so a link into a since-removed `docs/en/`
# layout is HISTORICALLY correct (it was real when written) — "fixing" it
# would rewrite what the record actually said, the same mistake as editing
# an ADR's own Context section (#4020/#4023 landed the same distinction for
# ADR bodies the same night this gate was built).
DEEP_DIVES_LINK_EXISTENCE_SUBDIRS = ("decisions", "proposals", "contributing", "spec")

# `tmp/` is repo-gitignored (`.gitignore:42`) and deliberately referenced by
# `deep-dives/spec/design/design-author-guide.md` as a locally-generated,
# never-committed bundled artifact (see that file's own originating commit
# message: "gets the same fence treatment ... gitignored, not in this
# commit"). A link into it can never resolve in a fresh checkout by design —
# excluded here rather than "fixed", the same non-fix as EXCLUDED_TARGET
# above for build-excluded pages.
_GITIGNORED_LINK_PREFIXES = ("tmp/",)


def check_deep_dives_link_existence() -> int:
    """Existence-only check for relative `.md` links inside
    `docs/deep-dives/{decisions,proposals,contributing,spec}/` (#4021).

    Deliberately does NOT check anchors: these 4 subdirs are excluded from
    the mkdocs build (`exclude_docs:` in `.mkdocs/mkdocs.yml`), so there is
    no built HTML to check an anchor's id against — unlike `main()` above,
    this needs no `site/` and can run standalone, before or without a build.
    Only "does the linked FILE exist" is checked.

    Scans every file in the 4 subdirs on every run (not a diff) — the
    breaking end of a link can live in a file that never moved (the file
    doing the pointing), so limiting the scan to files that changed in one
    PR would miss exactly the shape this gate exists to catch (lead-coder,
    #4021: "母集団は動く集合でなく動く集合を指す全体").
    """
    total_links = 0
    broken: list[tuple[str, str]] = []

    for subdir in DEEP_DIVES_LINK_EXISTENCE_SUBDIRS:
        base = DOCS / "deep-dives" / subdir
        for md_file in sorted(base.rglob("*.md")):
            rel = md_file.relative_to(DOCS)
            text = md_file.read_text(encoding="utf-8", errors="replace")
            for m in MD_LINK_RE.finditer(text):
                link = m.group(1)
                target = (md_file.parent / link).resolve()
                try:
                    target_from_repo = target.relative_to(REPO)
                except ValueError:
                    target_from_repo = target
                if target_from_repo.as_posix().startswith(_GITIGNORED_LINK_PREFIXES):
                    continue
                total_links += 1
                if not target.is_file():
                    broken.append((str(rel), link))

    # Same shape as main()'s `assert total_links >= 400` (#3667): a silent
    # regex regression (e.g. MD_LINK_RE stops matching one of its two
    # capture branches) must fail loud, not report "0 broken" as if that
    # meant "0 links reviewed and all fine." 283 measured at gate-landing
    # (2026-08-10, #4021); floor set below that to tolerate organic growth.
    _require_vacuity_floor(
        total_links, 200,
        f"Extracted only {total_links} relative .md links from "
        f"docs/deep-dives/{{{','.join(DEEP_DIVES_LINK_EXISTENCE_SUBDIRS)}}}/ "
        "— 283 measured when this gate landed (#4021). A `> 0` guard only "
        "catches total regex breakage; a partial regression would still "
        "pass while quietly checking a fraction of the target subdirs.",
    )

    print(
        f"checked {total_links} relative .md links in "
        f"deep-dives/{{{','.join(DEEP_DIVES_LINK_EXISTENCE_SUBDIRS)}}}/"
    )
    if broken:
        print(f"\nBROKEN_LINK: {len(broken)}")
        for src, link in broken:
            print(f"  {src} -> {link}")
        return 1

    print("no broken links in decisions/proposals/contributing/spec")
    return 0


def main() -> int:
    # Runs before the SITE check below — needs no mkdocs build (#4021).
    deep_dives_exit = check_deep_dives_link_existence()
    print()

    if not SITE.is_dir():
        print(
            f"FATAL: {SITE} does not exist — run "
            "`mkdocs build --strict -f .mkdocs/mkdocs.yml` first.",
            file=sys.stderr,
        )
        return 2

    exclude_patterns = _load_exclude_patterns()
    id_cache: dict[Path, set[str] | None] = {}

    anchor_not_found: list[tuple[str, str]] = []
    excluded_target: list[tuple[str, str]] = []
    total_links = 0

    for md_file in sorted(DOCS.rglob("*.md")):
        rel = md_file.relative_to(DOCS)
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            link = m.group(1)
            total_links += 1
            if link.startswith("#"):
                target_rel, anchor = rel, link[1:]
            else:
                target_path, anchor = link.split("#", 1)
                target_abs = (DOCS / rel.parent / target_path).resolve()
                try:
                    target_rel = target_abs.relative_to(DOCS.resolve())
                except ValueError:
                    # Target resolves outside docs/ entirely — a different
                    # hazard (mkdocs --strict already fails the build for
                    # this), not this gate's concern.
                    continue

            if _is_excluded(target_rel, exclude_patterns):
                excluded_target.append((str(rel), link))
                continue

            html_path = _md_to_html_path(str(target_rel.as_posix()))
            ids = _get_ids(html_path, id_cache)
            if ids is None:
                # A non-excluded target with no built HTML should be
                # impossible — mkdocs --strict already fails the build for
                # a docs/ page it can't build, before this script runs.
                # Enforced, not just declared: `_is_excluded` is a
                # prefix/exact match against gitignore-STYLE patterns
                # (docstring, line 74) but doesn't actually interpret
                # globs — a future glob pattern in exclude_docs would make
                # a real exclusion invisible to `_is_excluded` (False) AND
                # to the `ids is None` check silently skipped, so a link
                # into that now-unbuilt page would pass through neither
                # category and never be checked at all. Fail loud instead
                # of let that gap open silently.
                assert False, (  # noqa: B011
                    f"{rel} -> {link}: target has no built HTML and is not "
                    "recognized as excluded. Either mkdocs --strict should "
                    "have failed already, or exclude_docs grew a pattern "
                    "_is_excluded's prefix/exact match can't interpret "
                    "(e.g. a glob) — fix _is_excluded, don't ignore this."
                )
            if anchor not in ids:
                anchor_not_found.append((str(rel), link))

    _require_vacuity_floor(
        total_links, 400,
        f"Extracted only {total_links} anchor-bearing links from docs/ — "
        "the repo-wide count was 461 when this gate landed (#3667). `> 0` "
        "only catches total regex breakage; a partial regression (e.g. one "
        "of the two LINK_RE alternatives silently stops matching) would "
        "still pass a `> 0` guard while quietly checking a fraction of "
        "docs/. This floor is deliberately below 461 to tolerate organic "
        "doc growth/removal, not a hardcoded count pin.",
    )

    print(f"checked {total_links} anchor-bearing links")
    print(f"excluded-target (deep-dives/ etc., not built): {len(excluded_target)}")
    if excluded_target:
        for src, link in excluded_target:
            print(f"  {src} -> {link}")

    if anchor_not_found:
        print(f"\nANCHOR_NOT_FOUND: {len(anchor_not_found)}")
        for src, link in anchor_not_found:
            print(f"  {src} -> {link}")
        return 1

    print("\nno dangling anchors into published pages")
    return 1 if deep_dives_exit else 0


if __name__ == "__main__":
    sys.exit(main())
