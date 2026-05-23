"""Deterministic preprocessor for skill_importer.convert — fetch sibling
markdown files from the source skill's directory.

Anthropic skills routinely defer detail to sibling files
(``REFERENCE.md`` / ``FORMS.md`` / ``forms.md`` / ``reference.md`` /
etc.) referenced from the root ``SKILL.md`` body. The default import
path grabs only the root file and silently drops everything those
references contain.

This preprocessor:

  1. Parses ``source_url`` (= a GitHub raw URL) to extract the
     repository owner / repo / branch / parent directory.
  2. Calls the GitHub Contents API on the parent directory to list
     all files at the skill's level.
  3. Selects ``.md`` siblings (= not ``SKILL.md`` itself, not
     ``LICENSE.txt``, not anything binary).
  4. Fetches each via the raw URL using
     ``reyn.api.unsafe.http.get`` (= same I/O route the other
     preprocessors use).
  5. Returns the collected siblings as a list under
     ``data._sibling_files`` for the convert phase LLM to write to
     ``reyn/local/<slug>/references/<lowercase_name>``.

Failure handling: any HTTP / parse failure returns
``{siblings: [], fetched_count: 0, error: "<msg>", parent_listing_status:
"<status>"}`` so the LLM falls through to import-without-siblings (= the
behaviour before this preprocessor existed). Never crashes the convert
phase.

Caps:
  - Max 6 sibling .md files (= protects against an unbounded directory).
  - Each fetched body capped at 80,000 chars (= protects against huge
    REFERENCE files that would blow the LLM context).
"""
from __future__ import annotations

import re

from reyn.safe.http import get as http_get
from reyn.safe.json import loads_strict

_USER_AGENT = "reyn/1.0"

# Max number of sibling .md files to fetch.
_MAX_SIBLINGS = 6

# Per-file body cap. Sized to keep the convert phase prompt under
# control: the full convert.md instructions + selected_candidate +
# multiple siblings can otherwise drift the LLM into hallucinating
# the source URL or losing track of which artifact field is which.
# 4 KB × 6 siblings = 24 KB worst-case payload, which the convert
# phase handles cleanly. Files exceeding this are truncated + flagged.
_MAX_BODY_CHARS = 4_000

# Files to exclude from the sibling sweep.
_EXCLUDED_NAMES = frozenset({
    "SKILL.md",            # the source itself
    "LICENSE.txt", "LICENSE.md", "LICENSE",
    "NOTICE.md", "NOTICE.txt",
    "CHANGELOG.md", "CHANGES.md",
})


_GH_RAW_URL_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/"
    r"(?P<branch>[^/]+)/(?P<path>.+)$",
)


def _parse_github_raw_url(url: str) -> dict | None:
    """Decompose a raw.githubusercontent.com URL into owner/repo/branch/path.

    Returns None when ``url`` is not a recognised GitHub raw URL — the
    multi-file follow only applies to GitHub-hosted sources right now
    (= the canonical Anthropic registry).
    """
    m = _GH_RAW_URL_RE.match(url)
    if not m:
        return None
    return {
        "owner": m["owner"],
        "repo": m["repo"],
        "branch": m["branch"],
        "path": m["path"],   # = e.g. "skills/pdf/SKILL.md"
    }


def _parent_dir_path(file_path: str) -> str:
    """Return the parent directory path for a file path (no trailing slash)."""
    if "/" not in file_path:
        return ""
    return file_path.rsplit("/", 1)[0]


def _list_dir(owner: str, repo: str, path: str, branch: str) -> tuple[list[dict], str]:
    """GitHub Contents API listing for a directory.

    Returns ``(entries, status)``. On HTTP failure entries is an empty
    list and status describes the failure.
    """
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        f"?ref={branch}"
    )
    try:
        resp = http_get(api_url, headers={"User-Agent": _USER_AGENT})
    except Exception as exc:
        return [], f"http_error:{type(exc).__name__}"
    status = int(resp.get("status") or 0)
    if status >= 400:
        return [], f"http_{status}"
    body = resp.get("body") or ""
    if not isinstance(body, str):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return [], "decode_error"
    try:
        entries = loads_strict(body)
    except Exception:
        return [], "json_parse_error"
    if not isinstance(entries, list):
        return [], "unexpected_shape"
    return entries, "ok"


def _fetch_text(url: str) -> tuple[str, str]:
    """Fetch ``url`` and return ``(text, status)``."""
    try:
        resp = http_get(url, headers={"User-Agent": _USER_AGENT})
    except Exception as exc:
        return "", f"http_error:{type(exc).__name__}"
    status = int(resp.get("status") or 0)
    if status >= 400:
        return "", f"http_{status}"
    body = resp.get("body") or ""
    if not isinstance(body, str):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return "", "decode_error"
    return body, "ok"


def fetch(artifact: dict) -> dict:
    """Preprocessor entry — list + fetch .md siblings of the source file.

    Returns a dict placed at ``data._sibling_files`` with shape:

      siblings: [{name, content, was_truncated, raw_url}]
      fetched_count: int
      parent_url: str          (= GitHub Contents API URL of the dir)
      parent_listing_status: str ("ok" / "http_<code>" / error)
      error: str               ("" on success, message on failure)

    Defensive: any failure → empty siblings + status fields populated.
    """
    data = artifact.get("data") or {}
    source_url = data.get("source_url") or ""
    if not isinstance(source_url, str) or not source_url.strip():
        return {
            "siblings": [],
            "fetched_count": 0,
            "parent_url": "",
            "parent_listing_status": "no_source_url",
            "error": "source_url missing",
        }

    parsed = _parse_github_raw_url(source_url)
    if parsed is None:
        # Non-GitHub source — multi-file follow doesn't apply.
        return {
            "siblings": [],
            "fetched_count": 0,
            "parent_url": "",
            "parent_listing_status": "non_github_source",
            "error": "",
        }

    parent_path = _parent_dir_path(parsed["path"])
    parent_url = (
        f"https://api.github.com/repos/{parsed['owner']}/{parsed['repo']}"
        f"/contents/{parent_path}?ref={parsed['branch']}"
    )

    entries, listing_status = _list_dir(
        parsed["owner"], parsed["repo"], parent_path, parsed["branch"],
    )
    if listing_status != "ok":
        return {
            "siblings": [],
            "fetched_count": 0,
            "parent_url": parent_url,
            "parent_listing_status": listing_status,
            "error": f"directory listing failed: {listing_status}",
        }

    siblings: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "file":
            continue
        name = entry.get("name") or ""
        if not isinstance(name, str) or not name.endswith(".md"):
            continue
        if name in _EXCLUDED_NAMES:
            continue

        raw_url = (
            f"https://raw.githubusercontent.com/{parsed['owner']}"
            f"/{parsed['repo']}/{parsed['branch']}/{parent_path}/{name}"
        )
        body, fetch_status = _fetch_text(raw_url)
        if fetch_status != "ok":
            continue

        truncated = False
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS]
            truncated = True

        siblings.append({
            "name": name,
            "content": body,
            "was_truncated": truncated,
            "raw_url": raw_url,
        })
        if len(siblings) >= _MAX_SIBLINGS:
            break

    return {
        "siblings": siblings,
        "fetched_count": len(siblings),
        "parent_url": parent_url,
        "parent_listing_status": "ok",
        "error": "",
    }
