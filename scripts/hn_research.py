"""hn_research.py — site-scoped HN topic research via DuckDuckGo + Algolia API.

Automates the manual industry-research pipeline: run a site-scoped DuckDuckGo
search for a topic on news.ycombinator.com, fetch full thread JSON for each
matching item from the Algolia HN API, then produce a digest of top posts with
their top comments. Use this for repeatable positioning / design research
instead of ad-hoc web searches.

Example invocations:
    venv/bin/python scripts/hn_research.py --topic "AI agent" --max-results 10
    venv/bin/python scripts/hn_research.py --ids 47733217,48035677 --top-comments 5
    venv/bin/python scripts/hn_research.py --topic "eval framework" --json --out /tmp/out.json

JSON output schema (when --json is passed):
    {
      "topic": str | null,          # from --topic, or null when --ids used
      "fetched": int,               # number of items successfully fetched
      "total_comments_analysed": int,
      "top_k": int,
      "points_fallback": bool,      # true if comment points were unavailable → used created_at order
      "items": [
        {
          "id": str,
          "title": str,
          "url": str | null,        # external link, or null for self posts
          "date": str,              # YYYY-MM-DD
          "points": int | null,
          "num_comments": int,
          "body": str,              # first 500 chars of self-post text, empty string if none
          "top_comments": [
            {
              "author": str,
              "points": int | null,
              "created_at": str,    # ISO-8601
              "text": str           # first 280 chars, newlines collapsed
            }
          ]
        }
      ]
    }

Cache behavior:
    Algolia responses are cached as JSON files under --cache-dir (default
    /tmp/hn_research_cache/). The cache key is the HN item ID. On each run,
    a cached file is used if it exists and contains valid JSON; a corrupt or
    missing file triggers a live fetch. Cache files are never expired
    automatically — delete the directory to force a full re-fetch.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALGOLIA_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
HN_ITEM_RE = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")

# ── HTML stripping ──────────────────────────────────────────────────────────

def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", text)
    stripped = html.unescape(stripped)
    # collapse whitespace / newlines to single spaces
    return re.sub(r"\s+", " ", stripped).strip()


# ── Algolia fetch + cache ───────────────────────────────────────────────────

def _cache_path(item_id: str, cache_dir: Path) -> Path:
    return cache_dir / f"{item_id}.json"


def _fetch_item(item_id: str, cache_dir: Path) -> dict[str, Any] | None:
    """Fetch an item from Algolia, using cache if available."""
    cache_file = _cache_path(item_id, cache_dir)

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            print(f"[warn] cache corrupt for {item_id}, re-fetching", file=sys.stderr)

    url = ALGOLIA_ITEM_URL.format(id=item_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hn_research.py/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"[warn] Algolia HTTP {exc.code} for item {item_id} — skipping", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] network error for item {item_id}: {exc} — skipping", file=sys.stderr)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[warn] invalid JSON from Algolia for item {item_id}: {exc} — skipping", file=sys.stderr)
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(raw, encoding="utf-8")
    return data


# ── Comment tree traversal ──────────────────────────────────────────────────

def _collect_top_level_comments(children: list[dict]) -> list[dict]:
    """Return all direct (depth-1) children that have non-empty text."""
    result = []
    for child in children or []:
        text = _strip_html(child.get("text") or "")
        if text:
            result.append(child)
    return result


def _points_or_none(node: dict) -> int | None:
    pts = node.get("points")
    if pts is None:
        return None
    try:
        return int(pts)
    except (TypeError, ValueError):
        return None


def _created_at_ts(node: dict) -> int:
    """Return unix timestamp for created_at, defaulting to 0."""
    ts = node.get("created_at_i") or 0
    if isinstance(ts, int):
        return ts
    try:
        return int(ts)
    except (TypeError, ValueError):
        return 0


def _select_top_comments(item_data: dict, k: int) -> tuple[list[dict], bool]:
    """
    Select top-k comments for the item.

    Returns (comments, points_fallback) where points_fallback=True means
    comment points were not available and created_at ordering was used instead.
    """
    top_level = _collect_top_level_comments(item_data.get("children") or [])
    if not top_level:
        return [], False

    # Try points-based ordering first
    all_have_points = all(_points_or_none(c) is not None for c in top_level)
    any_have_points = any(_points_or_none(c) is not None for c in top_level)

    if any_have_points:
        # Sort by points descending, None treated as 0
        sorted_comments = sorted(
            top_level,
            key=lambda c: (_points_or_none(c) or 0),
            reverse=True,
        )
        return sorted_comments[:k], not all_have_points
    else:
        # No points at all — fall back to created_at ascending
        sorted_comments = sorted(top_level, key=_created_at_ts)
        return sorted_comments[:k], True


# ── Item digesting ──────────────────────────────────────────────────────────

def _parse_date(item_data: dict) -> str:
    ts = item_data.get("created_at_i") or 0
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "unknown"


def _digest_item(item_data: dict, top_k: int) -> tuple[dict, bool]:
    """
    Convert raw Algolia item JSON into a digest dict.

    Returns (digest_dict, points_fallback).
    """
    item_id = str(item_data.get("id", ""))
    title = item_data.get("title") or "(no title)"
    url = item_data.get("url") or None
    date = _parse_date(item_data)
    points = _points_or_none(item_data)
    num_comments = item_data.get("num_comments") or 0

    body_raw = _strip_html(item_data.get("text") or "")
    body = body_raw[:500] if body_raw else ""

    top_comments_raw, fallback = _select_top_comments(item_data, top_k)
    top_comments = []
    for c in top_comments_raw:
        text_raw = _strip_html(c.get("text") or "")
        text_single = text_raw[:280]
        top_comments.append(
            {
                "author": c.get("author") or "(anon)",
                "points": _points_or_none(c),
                "created_at": c.get("created_at") or "",
                "text": text_single,
            }
        )

    return (
        {
            "id": item_id,
            "title": title,
            "url": url,
            "date": date,
            "points": points,
            "num_comments": num_comments,
            "body": body,
            "top_comments": top_comments,
        },
        fallback,
    )


# ── Text rendering ──────────────────────────────────────────────────────────

def _render_text(items: list[dict], top_k: int, points_fallback: bool) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(f"=== {item['id']}: {item['title']} ===")
        url_display = item["url"] if item["url"] else "(self post)"
        lines.append(f"  url: {url_display}")
        pts_display = str(item["points"]) if item["points"] is not None else "n/a"
        lines.append(
            f"  date: {item['date']}  points: {pts_display}  comments: {item['num_comments']}"
        )
        if item["body"]:
            lines.append(f"  body: {item['body']}")
        lines.append(f"  top {len(item['top_comments'])} comments:")
        for c in item["top_comments"]:
            pts_str = f"{c['points']}p" if c["points"] is not None else "n/ap"
            lines.append(f"    [{pts_str} {c['author']}] {c['text']}")
        lines.append("")

    total_comments = sum(len(it["top_comments"]) for it in items)
    lines.append("=== summary ===")
    summary = f"fetched {len(items)} items, total {total_comments} comments analysed (top-{top_k} per item)."
    if points_fallback:
        summary += " points unavailable for comments — using created_at order."
    lines.append(summary)
    return "\n".join(lines)


# ── DDG search → IDs ────────────────────────────────────────────────────────

def _search_hn_ids(topic: str, max_results: int) -> list[str]:
    """
    Run a site-scoped DuckDuckGo search and extract HN item IDs.

    Requests 2*max_results from DDG so non-item results (front page, paginated
    lists) can be filtered out, returning up to max_results unique item IDs.
    """
    try:
        from reyn.search_backends.duckduckgo import DuckDuckGoBackend
    except ImportError as exc:
        print(
            f"[error] could not import reyn.search_backends.duckduckgo: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    query = f"site:news.ycombinator.com {topic}"
    backend = DuckDuckGoBackend()
    try:
        results = backend.search(query, max_results=max_results * 2)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] DuckDuckGo search failed: {exc}", file=sys.stderr)
        sys.exit(1)

    seen: set[str] = set()
    ids: list[str] = []
    for result in results:
        url = result.get("url", "")
        m = HN_ITEM_RE.search(url)
        if m:
            item_id = m.group(1)
            if item_id not in seen:
                seen.add(item_id)
                ids.append(item_id)
        else:
            if url:
                print(
                    f"[info] skipping non-item HN URL: {url}",
                    file=sys.stderr,
                )

    return ids[:max_results]


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch HN threads via Algolia and produce a research digest.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--topic",
        metavar="TOPIC",
        help="Run site:news.ycombinator.com search via DuckDuckGo for this topic.",
    )
    source.add_argument(
        "--ids",
        metavar="ID,...",
        help="Comma-separated HN item IDs. Skips the search step.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        metavar="N",
        help="Max items to fetch/digest (default: 10).",
    )
    parser.add_argument(
        "--top-comments",
        type=int,
        default=5,
        metavar="K",
        help="Number of top comments per thread (default: 5).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Write digest to this file (default: stdout).",
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit structured JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--cache-dir",
        metavar="PATH",
        default="/tmp/hn_research_cache/",
        help="Directory for caching Algolia responses (default: /tmp/hn_research_cache/).",
    )

    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)

    # ── Resolve item IDs ──
    if args.topic:
        ids = _search_hn_ids(args.topic, args.max_results)
        if not ids:
            print(
                f"[error] no HN items found for topic '{args.topic}'",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        raw_ids = [id_.strip() for id_ in args.ids.split(",") if id_.strip()]
        ids = raw_ids[: args.max_results]

    # ── Fetch concurrently ──
    digests: list[dict] = []
    any_fallback = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_id = {
            executor.submit(_fetch_item, item_id, cache_dir): item_id
            for item_id in ids
        }
        for future in concurrent.futures.as_completed(future_to_id):
            item_id = future_to_id[future]
            try:
                data = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] unexpected error fetching {item_id}: {exc}", file=sys.stderr)
                continue
            if data is None:
                continue
            digest, fallback = _digest_item(data, args.top_comments)
            if fallback:
                any_fallback = True
            digests.append(digest)

    # Restore original order (concurrent futures complete out of order)
    id_order = {item_id: i for i, item_id in enumerate(ids)}
    digests.sort(key=lambda d: id_order.get(d["id"], len(ids)))

    # ── Format output ──
    if args.emit_json:
        total_comments = sum(len(d["top_comments"]) for d in digests)
        output_obj = {
            "topic": args.topic,
            "fetched": len(digests),
            "total_comments_analysed": total_comments,
            "top_k": args.top_comments,
            "points_fallback": any_fallback,
            "items": digests,
        }
        output = json.dumps(output_obj, ensure_ascii=False, indent=2)
    else:
        output = _render_text(digests, args.top_comments, any_fallback)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
