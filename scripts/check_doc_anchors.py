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

Run standalone: `python scripts/check_doc_anchors.py` (after `mkdocs build
--strict -f .mkdocs/mkdocs.yml`, which this script assumes already ran and
left its output in `site/` at the repo root — wired as an additional step
in the same CI job right after that build, in `.github/workflows/test.yml`,
so no second build and no new dependency).
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


def main() -> int:
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

    assert total_links >= 400, (
        f"Extracted only {total_links} anchor-bearing links from docs/ — "
        "the repo-wide count was 461 when this gate landed (#3667). `> 0` "
        "only catches total regex breakage; a partial regression (e.g. one "
        "of the two LINK_RE alternatives silently stops matching) would "
        "still pass a `> 0` guard while quietly checking a fraction of "
        "docs/. This floor is deliberately below 461 to tolerate organic "
        "doc growth/removal, not a hardcoded count pin."
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
