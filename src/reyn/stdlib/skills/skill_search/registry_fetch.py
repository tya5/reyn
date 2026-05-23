"""Deterministic preprocessor for skill_search — fetches skill registry results.

Called as a ``type: python`` preprocessor step. Signature contract:
  ``fetch_registry_results(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``text``  — the user's natural language capability request (e.g. "PDF を要約").

Output (placed at ``data.registry``):
  ``candidates`` — list of {name, source_url, description} dicts from the registry.
  ``source``     — ``"registry"`` | ``"registry_stale"`` | ``"error"``
  ``query``      — keyword extracted and used for the registry search.

Mirror of ``mcp_search/registry_fetch.py`` for the Anthropic skills registry
(default: ``github.com/anthropics/skills``). The registry layout is:

  <repo>/skills/<skill-name>/SKILL.md   (frontmatter: name + description)

Fetch strategy:
  1. List directories under ``<contents-url>`` (= 1 HTTP call, cached 24h).
  2. Keyword-filter the directory names against the user's query.
  3. For each top-K survivor (= up to 10), fetch SKILL.md and extract the
     ``description`` from its YAML frontmatter (cached 24h per skill).

Registry unreachable:
  On HTTP error or any exception, fall back to stale cache. If no cache
  exists, return empty candidates with ``source="error"``.
"""
from __future__ import annotations

import re

from reyn.safe.http import get as http_get
from reyn.safe.json import loads_strict

_USER_AGENT = "reyn/1.0"
_DEFAULT_LIST_URL = (
    "https://api.github.com/repos/anthropics/skills/contents/skills"
)
_DEFAULT_RAW_BASE = (
    "https://raw.githubusercontent.com/anthropics/skills/main/skills"
)


def _list_url() -> str:
    """GitHub Contents API URL for the skill directory listing.

    FP-0042 Phase 3 drift-fix (2026-05-23): the ``REYN_SKILL_REGISTRY_URL``
    env var override was dropped because ``os`` is not on the safe-mode
    import allowlist and reading env vars under safe-mode is part of
    the Issue #571 deferred design discussion (= config-controlled URL
    needs a permission gate). Skills using a non-default registry can
    declare mode: unsafe explicitly, or wait for the Issue #571
    resolution.
    """
    return _DEFAULT_LIST_URL.rstrip("/")


def _raw_base() -> str:
    """Raw-URL prefix for fetching ``<name>/SKILL.md``.

    Derived from the listing URL. FP-0042 Phase 3 drift-fix dropped
    the ``REYN_SKILL_REGISTRY_RAW_BASE`` override (= same reason as
    ``_list_url``).
    """
    # Default GitHub mapping: api.github.com/.../contents/<path>
    # → raw.githubusercontent.com/<owner>/<repo>/main/<path>
    list_url = _list_url()
    m = re.match(
        r"^https://api\.github\.com/repos/([^/]+)/([^/]+)/contents/(.*)$",
        list_url,
    )
    if m:
        owner, repo, path = m.group(1), m.group(2), m.group(3)
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
    return _DEFAULT_RAW_BASE


def _extract_keyword(text: str) -> str:
    """Extract a concise search keyword from the user's request.

    Strategy: find the first run of ASCII letters (≥ 3 chars) — these
    handle mixed Japanese/English text. Fallback to the first
    whitespace-separated token lowercased.

    (Mirror of mcp_search/_extract_keyword.)
    """
    match = re.search(r"[A-Za-z]{3,}", text)
    if match:
        return match.group(0).lower()
    token = text.split()[0] if text.split() else text
    return token.lower()


def _list_skill_dirs() -> list[str]:
    """Fetch the registry directory listing and return skill folder names.

    Raises ``RuntimeError`` on non-2xx status or JSON parse failure.
    """
    resp = http_get(_list_url(), headers={"User-Agent": _USER_AGENT})
    if resp["status"] >= 400:
        raise RuntimeError(f"Registry list returned HTTP {resp['status']}")
    entries = loads_strict(resp["body"])
    if not isinstance(entries, list):
        raise RuntimeError("Registry list did not return an array")
    return [
        e["name"] for e in entries
        if isinstance(e, dict) and e.get("type") == "dir" and e.get("name")
    ]


def _filter_by_query(skill_names: list[str], query: str) -> list[str]:
    """Keyword-filter directory names; preserve original order.

    Substring match against the lowercased folder name. Empty query
    returns all names unchanged (= LLM decides).
    """
    if not query:
        return list(skill_names)
    q = query.lower()
    return [n for n in skill_names if q in n.lower()]


_FRONTMATTER_DESC_RE = re.compile(
    r"^description:\s*(.+?)(?:\n[a-zA-Z_-]+:|\n---)",
    re.DOTALL | re.MULTILINE,
)


def _fetch_description(name: str) -> str:
    """Fetch ``<raw_base>/<name>/SKILL.md`` and extract the
    ``description`` field from its YAML frontmatter.

    Returns empty string on any failure (= skill listed without
    description rather than crashing the whole search).
    """
    url = f"{_raw_base()}/{name}/SKILL.md"
    try:
        resp = http_get(url, headers={"User-Agent": _USER_AGENT})
    except Exception:
        return ""
    if resp["status"] >= 400:
        return ""
    body = resp["body"]
    if not isinstance(body, str):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    # Locate the YAML frontmatter block (between the first two ``---``).
    if not body.startswith("---"):
        return ""
    end = body.find("---", 3)
    if end == -1:
        return ""
    fm = body[3:end]
    m = _FRONTMATTER_DESC_RE.search(fm + "\n---")
    if not m:
        return ""
    return " ".join(m.group(1).split())[:300]


def _build_candidate(name: str) -> dict:
    """Assemble one candidate dict (= name + source_url + description).

    Per-skill description is cached by ``_raw_base()`` + ``name`` so the
    description fetch happens at most once per 24h per skill — successive
    queries against the same registry reuse the result.
    """
    import reyn.safe.cache as cache

    cache_key = f"skill_search:desc:{_raw_base()}:{name}"
    cached = cache.get(cache_key)
    if cached is not None and "description" in cached:
        desc = cached["description"]
    else:
        desc = _fetch_description(name)
        # Cache even empty descriptions so we don't keep retrying on
        # skills with malformed / missing frontmatter.
        cache.set(cache_key, {"description": desc})
    return {
        "name": name,
        "source_url": f"{_raw_base()}/{name}/SKILL.md",
        "description": desc,
    }


def fetch_registry_results(artifact: dict) -> dict:
    """Python preprocessor entry point.

    Receives the phase input artifact dict. Returns a dict placed at
    ``data.registry`` in the enriched artifact.
    """
    import reyn.safe.cache as cache

    text: str = (artifact.get("data") or {}).get("text") or ""
    query = _extract_keyword(text) if text.strip() else ""

    # Cache key for the directory listing (= shared across queries).
    list_cache_key = f"skill_search:list:{_list_url()}"
    # Cap high enough to cover the canonical anthropics/skills registry
    # (= 17 entries as of 2026-05-23) without truncating the fallback
    # path. Per-skill descriptions are cached individually, so even
    # cold-cache "return all" stays bounded — 25 HTTP calls one-time.
    max_candidates = 25

    def _build_result(names: list[str], source: str) -> dict:
        # Name-filter narrows first. If the keyword doesn't match any
        # folder name (e.g. "code" against ["pdf", "docx", ...]), fall
        # back to the full list so the LLM phase can filter by
        # description instead. Per mcp_search's philosophy: preprocessor
        # fetches, LLM filters by relevance.
        keep = _filter_by_query(names, query)
        if not keep:
            keep = list(names)
        keep = keep[:max_candidates]
        return {
            "candidates": [_build_candidate(n) for n in keep],
            "source": source,
            "query": query,
        }

    cached = cache.get(list_cache_key)
    if cached is not None and isinstance(cached.get("names"), list):
        return _build_result(cached["names"], "registry")

    try:
        names = _list_skill_dirs()
        cache.set(list_cache_key, {"names": names})
        return _build_result(names, "registry")
    except Exception:
        stale = cache.get(list_cache_key)
        if stale is not None and isinstance(stale.get("names"), list):
            return _build_result(stale["names"], "registry_stale")
        return {"candidates": [], "source": "error", "query": query}
