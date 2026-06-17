#!/usr/bin/env python3
"""Generate redirect stubs for the legacy ``/docs/*`` URL prefix.

#1733 unified the site at the Pages root: docs moved from ``/<base>/docs/<path>``
to ``/<base>/<path>``. GitHub Pages has no server-side redirects, so for every
built page we emit a meta-refresh stub at the OLD ``docs/<path>`` location
pointing at the new URL. The old docs home (``/docs/``) maps to ``/start/`` —
the orientation page that ``docs/index.md`` content moved to (the new root is
the project landing page).

Usage:
    gen_docs_redirects.py <mkdocs_site_dir> <out_docs_dir> [<url_base>]

  <mkdocs_site_dir>  the mkdocs build output (e.g. ``site``)
  <out_docs_dir>     where to write stubs (e.g. ``_deploy/docs``)
  <url_base>         absolute URL base, default ``/reyn/`` (the Pages project path)

Covers EVERY ``*.html`` under the site (completeness — no page left without a
legacy redirect).
"""
from __future__ import annotations

import sys
from pathlib import Path


def served_url(relpath: str, base: str) -> str:
    """Map a built file's relpath to the URL it is served at (new location)."""
    p = Path(relpath)
    if p.name == "index.html":
        d = p.parent.as_posix()
        if d == ".":
            # old /docs/ (docs home) → /start/ (where index.md content moved)
            return base + "start/"
        return f"{base}{d}/"
    return base + relpath


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    site = Path(argv[1])
    out = Path(argv[2])
    base = argv[3] if len(argv) > 3 else "/reyn/"
    if not base.endswith("/"):
        base += "/"

    count = 0
    for html in sorted(site.rglob("*.html")):
        rel = html.relative_to(site).as_posix()
        target = served_url(rel, base)
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={target}">'
            f'<link rel="canonical" href="{target}">'
            "<title>Redirecting…</title></head>"
            f'<body>This page has moved to <a href="{target}">{target}</a>.</body>'
            "</html>\n",
            encoding="utf-8",
        )
        count += 1
    print(f"gen_docs_redirects: wrote {count} legacy /docs/* stubs → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
