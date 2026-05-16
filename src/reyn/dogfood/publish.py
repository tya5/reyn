"""GitHub Discussion publisher for FP-0036 dogfood batches.

Reads ``.reyn/dogfood/runs/<run_id>/summary.json``, renders the discussion
body from a Markdown template, and creates a thread in the configured
GitHub Discussions category via GraphQL.

Authentication: uses ``GH_TOKEN`` or ``GITHUB_TOKEN`` env vars (= same
convention as the ``gh`` CLI; operators already have one set).

GraphQL endpoint: ``https://api.github.com/graphql``
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.github.com/graphql"

# Shipped defaults — configurable via CLI flags
DEFAULT_REPO = "tya5/reyn"
DEFAULT_CATEGORY_SLUG = "dogfood-batches"
_DEFAULT_CATEGORY_ID = "DIC_kwDOSWAku84C9M8T"
_DEFAULT_REPO_NODE_ID = "R_kgDOSWAkuw"

_DEFAULT_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "docs"
    / "deep-dives"
    / "contributing"
    / "templates"
    / "dogfood-discussion-template.md"
)


@dataclass
class PublishConfig:
    """Configuration for a dogfood Discussion publish operation."""

    repo: str                  # "owner/name"
    category_slug: str         # "dogfood-batches"
    template_path: Path
    token: str | None          # GitHub auth token


def detect_repo_from_git() -> str | None:
    """Read ``git remote get-url origin`` and parse owner/name.

    Returns None on failure (= operator must pass --repo explicitly).
    """
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2.0,
        )
        if r.returncode != 0:
            return None
        url = r.stdout.strip()
        # Match git@github.com:owner/repo.git OR https://github.com/owner/repo(.git)?
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        return None
    except Exception:
        return None


def get_token() -> str | None:
    """Return GH_TOKEN or GITHUB_TOKEN env var; None if unset."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def render_body(summary: dict, template_path: Path) -> str:
    """Fill template placeholders from summary dict.

    Extracts the ``## Discussion body`` block from the template and
    substitutes ``<placeholder>`` markers with values derived from
    *summary*.

    Placeholders handled:
      <N>                 batch_id
      <YYYY-MM-DD>        date from started_at / completed_at
      <topic>             topic field
      <commit_hash>       framework_commit field (short sha)
      <set_name_1>        set_name field
      <count>             verified count
      <total>             total count
      <pct>               verified percentage (integer)
      <inconclusive>      inconclusive count (number only)
      <regressed_count>   regressed_count if present
      <scenario_id>       regressed_scenarios list (comma-joined) if present
      <prev_N>            baseline_batch_id if present
      <float>             brier_score (2 d.p.) if present
      <URL to commit...>  journal_link if present
      <PR links...>       fix_wave_prs if present

    Missing optional fields substitute to "—".
    """
    raw = template_path.read_text(encoding="utf-8")

    # Extract the fenced code block after "## Discussion body (paste this into GitHub)"
    body_match = re.search(
        r"## Discussion body.*?```markdown\n(.*?)```",
        raw,
        re.DOTALL,
    )
    if body_match:
        template_body = body_match.group(1)
    else:
        # Fallback: use a built-in minimal template
        template_body = (
            "**Batch <N> — <YYYY-MM-DD> — <topic>**\n\n"
            "- Framework: <FP-XXXX> framework `<commit_hash>`\n"
            "- Scenario sets: <set_name_1> (<count>)\n"
            "- Verified: <count>/<total> = <pct>%\n"
            "- Inconclusive: <inconclusive>\n"
            "- Regressed (vs baseline `b<prev_N>`): <regressed_count>"
            " [= `<scenario_id>` if count > 0]\n"
            "- Brier vs prediction: <float>\n"
            "- Journal: <URL to commit containing summary.md>\n"
            "- Fix-wave PRs: <PR links, or \"none yet\">\n\n"
            "[discussion follows in comments]\n"
        )

    agg_verified = summary.get("verified", 0)
    agg_total = summary.get("total", 0)
    agg_inconclusive = summary.get("inconclusive", 0)
    verified_pct = (
        int(round(agg_verified / agg_total * 100)) if agg_total > 0 else 0
    )

    batch_id = str(summary.get("batch_id", "?"))
    topic = summary.get("topic") or "—"
    commit_hash = summary.get("framework_commit") or "?"
    set_name = summary.get("set_name") or "?"

    # Date from started_at, then completed_at
    date_str = "?"
    for date_key in ("started_at", "completed_at"):
        raw_date = summary.get(date_key)
        if raw_date and isinstance(raw_date, str):
            # ISO 8601 — take the date part only
            date_str = raw_date[:10]
            break

    # Regressed info
    regressed_count = summary.get("regressed_count")
    if regressed_count is None:
        regressed_count_str = "—"
    else:
        regressed_count_str = str(regressed_count)

    regressed_scenarios = summary.get("regressed_scenarios") or []
    if regressed_scenarios:
        scenario_id_str = ", ".join(f"`{s}`" for s in regressed_scenarios)
    else:
        scenario_id_str = "—"

    prev_n = str(summary.get("baseline_batch_id", "?"))

    brier = summary.get("brier_score")
    brier_str = f"{brier:.2f}" if brier is not None else "—"

    journal_link = summary.get("journal_link") or "—"
    fix_wave_prs = summary.get("fix_wave_prs") or "none yet"

    # Apply substitutions — order matters for overlapping patterns
    replacements: list[tuple[str, str]] = [
        ("<set_name_1>", set_name),
        ("<set_name>", set_name),
        ("<commit_hash>", commit_hash),
        ("<topic>", topic),
        ("<YYYY-MM-DD>", date_str),
        ("<N>", batch_id),
        ("<prev_N>", prev_n),
        # counts — note <count> appears twice in the template (verified and set count);
        # replace the aggregate metrics first via more specific patterns, then <count>
        ("<total>", str(agg_total)),
        ("<pct>", str(verified_pct)),
        ("<inconclusive>", str(agg_inconclusive)),
        ("<regressed_count>", regressed_count_str),
        # "<scenario_id>" may appear in "= `<scenario_id>` if count > 0"
        ("[= `<scenario_id>` if count > 0]", f"(= {scenario_id_str})" if regressed_scenarios else ""),
        ("<scenario_id>", scenario_id_str),
        ("<float>", brier_str),
        ("<URL to commit containing summary.md>", journal_link),
        ('<PR links, or "none yet">', fix_wave_prs),
        ("<PR links>", fix_wave_prs),
        # generic <count> last (after the specific ones above)
        ("<count>", str(agg_verified)),
        # template has <FP-XXXX> as placeholder for framework version
        ("<FP-XXXX>", "FP-0036"),
    ]

    result = template_body
    for old, new in replacements:
        result = result.replace(old, new)

    return result


def _graphql_request(
    token: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    http_client: Any = None,
) -> dict[str, Any]:
    """Execute a synchronous GraphQL request against the GitHub API.

    *http_client* is an httpx.Client-compatible instance — tests pass an
    httpx.Client configured with a MockTransport so we avoid network I/O
    without introducing MagicMock.
    """
    import httpx

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    }

    owns_client = False
    if http_client is None:
        http_client = httpx.Client(timeout=15.0)
        owns_client = True

    try:
        resp = http_client.post(
            _GRAPHQL_URL,
            content=json.dumps(payload).encode(),
            headers=headers,
        )
    finally:
        if owns_client:
            http_client.close()

    if resp.status_code >= 400:
        raise RuntimeError(
            f"GitHub GraphQL request failed with HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )

    data = resp.json()
    if "errors" in data:
        raise RuntimeError(
            f"GitHub GraphQL errors: {json.dumps(data['errors'], indent=2)}"
        )
    return data


def resolve_category_id(
    token: str,
    repo: str,
    category_slug: str,
    *,
    http_client: Any = None,
) -> str:
    """Query the Discussion category node ID from the slug.

    Falls back to the shipped default if the slug matches ``dogfood-batches``
    and the default constants are set — avoids an extra network round-trip
    in the happy path.
    """
    if (
        repo == DEFAULT_REPO
        and category_slug == DEFAULT_CATEGORY_SLUG
    ):
        logger.debug(
            "Using shipped default category ID for %s / %s",
            repo, category_slug,
        )
        return _DEFAULT_CATEGORY_ID

    owner, name = _split_repo(repo)
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        discussionCategories(first: 50) {
          nodes { id slug }
        }
      }
    }
    """
    data = _graphql_request(
        token, query, {"owner": owner, "name": name}, http_client=http_client,
    )
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("discussionCategories", {})
        .get("nodes", [])
    )
    for node in nodes:
        if node.get("slug") == category_slug:
            return node["id"]
    raise ValueError(
        f"Discussion category '{category_slug}' not found in {repo}. "
        "Run `reyn dogfood publish --repo OWNER/REPO` with the correct "
        "--category slug, or create the category in GitHub Discussions UI."
    )


def resolve_repo_id(
    token: str,
    repo: str,
    *,
    http_client: Any = None,
) -> str:
    """Return the repository node ID (needed for createDiscussion mutation).

    Uses the shipped default when the repo matches DEFAULT_REPO.
    """
    if repo == DEFAULT_REPO:
        logger.debug("Using shipped default repo node ID for %s", repo)
        return _DEFAULT_REPO_NODE_ID

    owner, name = _split_repo(repo)
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) { id }
    }
    """
    data = _graphql_request(
        token, query, {"owner": owner, "name": name}, http_client=http_client,
    )
    repo_id = (
        data.get("data", {})
        .get("repository", {})
        .get("id")
    )
    if not repo_id:
        raise ValueError(f"Repository '{repo}' not found or no access.")
    return repo_id


def create_discussion(
    token: str,
    repo_id: str,
    category_id: str,
    title: str,
    body: str,
    *,
    http_client: Any = None,
) -> dict:
    """Call createDiscussion mutation; return {number, url, id}."""
    mutation = """
    mutation($input: CreateDiscussionInput!) {
      createDiscussion(input: $input) {
        discussion { id number url }
      }
    }
    """
    variables = {
        "input": {
            "repositoryId": repo_id,
            "categoryId": category_id,
            "title": title,
            "body": body,
        }
    }
    data = _graphql_request(
        token, mutation, variables, http_client=http_client,
    )
    disc = (
        data.get("data", {})
        .get("createDiscussion", {})
        .get("discussion", {})
    )
    if not disc:
        raise RuntimeError(
            "createDiscussion returned no discussion object. "
            f"Full response: {json.dumps(data, indent=2)}"
        )
    return {
        "number": disc.get("number"),
        "url": disc.get("url"),
        "id": disc.get("id"),
    }


def build_title(summary: dict) -> str:
    """Build the Discussion title from summary fields.

    Format (per dogfood-reporting.md Section 3):
      Batch <N> (YYYY-MM-DD): <topic> — <verified_pct>% verified, <regressed_count> regressed

    Raises ValueError if required fields (batch_id, topic) are absent.
    """
    batch_id = summary.get("batch_id")
    if batch_id is None:
        raise ValueError(
            "summary.json missing 'batch_id'. Pass --batch-id <N> to supply it."
        )

    topic = summary.get("topic")
    if not topic:
        raise ValueError(
            "summary.json missing 'topic'. Pass --topic <TOPIC> to supply it."
        )

    # Date
    date_str = "?"
    for date_key in ("started_at", "completed_at"):
        raw_date = summary.get(date_key)
        if raw_date and isinstance(raw_date, str):
            date_str = raw_date[:10]
            break

    agg_verified = summary.get("verified", 0)
    agg_total = summary.get("total", 0)
    verified_pct = (
        int(round(agg_verified / agg_total * 100)) if agg_total > 0 else 0
    )

    regressed_count = summary.get("regressed_count")
    if regressed_count is None:
        regressed_part = "— regressed"
    else:
        regressed_part = f"{regressed_count} regressed"

    return (
        f"Batch {batch_id} ({date_str}): {topic} "
        f"— {verified_pct}% verified, {regressed_part}"
    )


def publish_run(
    run_id: str,
    *,
    config: PublishConfig,
    storage_dir: Path,
    dry_run: bool = False,
    batch_id: int | str | None = None,
    topic: str | None = None,
    http_client: Any = None,
) -> dict:
    """Top-level: read summary, render body, create discussion.

    Returns ``{discussion_number, discussion_url, title, body}``.
    On dry_run, ``discussion_number`` and ``discussion_url`` are ``None``.

    Parameters
    ----------
    run_id:
        The run ID (used only for logging; the actual data is read from
        *storage_dir*).
    config:
        Publisher configuration (repo, category_slug, template_path, token).
    storage_dir:
        Path to the run directory containing ``summary.json``.
    dry_run:
        If True, render the body and title but do not make HTTP calls.
    batch_id:
        Override for summary.json's ``batch_id`` field.
    topic:
        Override for summary.json's ``topic`` field.
    http_client:
        Injectable httpx.Client for testing (= MockTransport pattern).
    """
    summary_path = storage_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(
            f"No summary.json found in {storage_dir}. "
            f"Run 'reyn dogfood run' first to generate it."
        )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    # Apply CLI overrides for optional fields
    if batch_id is not None:
        summary["batch_id"] = batch_id
    if topic is not None:
        summary["topic"] = topic

    title = build_title(summary)
    body = render_body(summary, config.template_path)

    if dry_run:
        return {
            "title": title,
            "body": body,
            "discussion_number": None,
            "discussion_url": None,
        }

    token = config.token
    if not token:
        raise RuntimeError(
            "No GitHub token available. Set the GH_TOKEN or GITHUB_TOKEN "
            "environment variable and retry."
        )

    repo_id = resolve_repo_id(token, config.repo, http_client=http_client)
    category_id = resolve_category_id(
        token, config.repo, config.category_slug, http_client=http_client,
    )
    disc = create_discussion(
        token, repo_id, category_id, title, body, http_client=http_client,
    )

    logger.info(
        "Created Discussion #%s: %s", disc["number"], disc["url"],
    )

    return {
        "title": title,
        "body": body,
        "discussion_number": disc["number"],
        "discussion_url": disc["url"],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _split_repo(repo: str) -> tuple[str, str]:
    """Split 'owner/name' into (owner, name). Raises ValueError on bad format."""
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"Invalid repo format '{repo}'. Expected 'OWNER/NAME' (e.g. 'tya5/reyn')."
        )
    return parts[0], parts[1]
