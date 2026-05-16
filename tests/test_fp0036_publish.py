"""Tier 1/2: FP-0036 dogfood Discussion publisher (reyn.dogfood.publish).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock / AsyncMock / patch.
- Real dataclass instances; httpx.Client with httpx.MockTransport for
  GraphQL HTTP calls (same Fake pattern as test_fp0016_b_oauth_refresh.py).
- Assertions against public surface only.
- Each test docstring's first line starts with 'Tier 1:' or 'Tier 2:'.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest

from reyn.dogfood.publish import (
    _DEFAULT_TEMPLATE_PATH,
    DEFAULT_CATEGORY_SLUG,
    DEFAULT_REPO,
    PublishConfig,
    _split_repo,
    build_title,
    create_discussion,
    detect_repo_from_git,
    get_token,
    publish_run,
    render_body,
    resolve_category_id,
    resolve_repo_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    *,
    batch_id: int = 27,
    topic: str = "chat router smoke",
    verified: int = 12,
    total: int = 16,
    inconclusive: int = 3,
    refuted: int = 1,
    blocked: int = 0,
    started_at: str = "2026-05-17T10:00:00+00:00",
    brier_score: float | None = 0.21,
    regressed_count: int | None = None,
    regressed_scenarios: list | None = None,
    **extra,
) -> dict:
    d = {
        "run_id": "run-abc123",
        "set_name": "chat_router_smoke",
        "batch_id": batch_id,
        "topic": topic,
        "started_at": started_at,
        "completed_at": "2026-05-17T10:12:00+00:00",
        "verified": verified,
        "inconclusive": inconclusive,
        "refuted": refuted,
        "blocked": blocked,
        "total": total,
        "verified_rate": verified / total if total else 0.0,
        "brier_score": brier_score,
    }
    if regressed_count is not None:
        d["regressed_count"] = regressed_count
    if regressed_scenarios is not None:
        d["regressed_scenarios"] = regressed_scenarios
    d.update(extra)
    return d


def _make_config(
    *,
    repo: str = DEFAULT_REPO,
    category_slug: str = DEFAULT_CATEGORY_SLUG,
    template_path: Path | None = None,
    token: str | None = "gh-test-token",
) -> PublishConfig:
    return PublishConfig(
        repo=repo,
        category_slug=category_slug,
        template_path=template_path or _DEFAULT_TEMPLATE_PATH,
        token=token,
    )


def _mock_client(handler) -> httpx.Client:
    """Build an httpx.Client with a MockTransport for testing GraphQL calls."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# 1. detect_repo_from_git — SSH URL
# ---------------------------------------------------------------------------

def test_detect_repo_ssh_url(monkeypatch) -> None:
    """Tier 1: detect_repo_from_git parses git@github.com:owner/repo.git correctly."""
    import subprocess

    _called = {}

    def _fake_run(cmd, **kwargs):
        _called["cmd"] = cmd
        result = subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="git@github.com:tya5/reyn.git\n",
        )
        return result

    monkeypatch.setattr("reyn.dogfood.publish.subprocess.run", _fake_run)

    result = detect_repo_from_git()
    assert result == "tya5/reyn"


# ---------------------------------------------------------------------------
# 2. detect_repo_from_git — HTTPS URL
# ---------------------------------------------------------------------------

def test_detect_repo_https_url(monkeypatch) -> None:
    """Tier 1: detect_repo_from_git parses https://github.com/owner/repo correctly."""
    import subprocess

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="https://github.com/acme/my-project\n",
        )

    monkeypatch.setattr("reyn.dogfood.publish.subprocess.run", _fake_run)

    result = detect_repo_from_git()
    assert result == "acme/my-project"


# ---------------------------------------------------------------------------
# 3. detect_repo_from_git — non-GitHub URL → None
# ---------------------------------------------------------------------------

def test_detect_repo_non_github_url(monkeypatch) -> None:
    """Tier 1: detect_repo_from_git returns None when the remote is not a GitHub URL."""
    import subprocess

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="https://gitlab.com/acme/my-project.git\n",
        )

    monkeypatch.setattr("reyn.dogfood.publish.subprocess.run", _fake_run)

    result = detect_repo_from_git()
    assert result is None


# ---------------------------------------------------------------------------
# 4. get_token — env var precedence
# ---------------------------------------------------------------------------

def test_get_token_gh_token(monkeypatch) -> None:
    """Tier 1: get_token returns GH_TOKEN when set."""
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_token() == "gh-secret"


def test_get_token_github_token_fallback(monkeypatch) -> None:
    """Tier 1: get_token falls back to GITHUB_TOKEN when GH_TOKEN is unset."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    assert get_token() == "fallback-token"


def test_get_token_none(monkeypatch) -> None:
    """Tier 1: get_token returns None when neither token env var is set."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_token() is None


# ---------------------------------------------------------------------------
# 5. render_body — placeholder substitution
# ---------------------------------------------------------------------------

def test_render_body_substitutes_placeholders(tmp_path) -> None:
    """Tier 1: render_body substitutes placeholders from a mock summary dict."""
    # Build a minimal template matching the marker convention
    template = tmp_path / "tmpl.md"
    template.write_text(
        "## Discussion body (paste this into GitHub)\n\n"
        "```markdown\n"
        "**Batch <N> — <YYYY-MM-DD> — <topic>**\n\n"
        "- Verified: <count>/<total> = <pct>%\n"
        "- Inconclusive: <inconclusive>\n"
        "- Regressed (vs baseline `b<prev_N>`): <regressed_count>"
        " [= `<scenario_id>` if count > 0]\n"
        "- Brier vs prediction: <float>\n"
        "```\n",
        encoding="utf-8",
    )

    summary = _make_summary(
        batch_id=27,
        topic="chat router smoke",
        verified=12,
        total=16,
        inconclusive=3,
        brier_score=0.21,
        baseline_batch_id=26,
    )

    body = render_body(summary, template)

    assert "Batch 27" in body
    assert "2026-05-17" in body
    assert "chat router smoke" in body
    assert "12/16" in body
    assert "75%" in body
    assert "3" in body           # inconclusive count
    assert "0.21" in body


# ---------------------------------------------------------------------------
# 6. render_body — missing optional fields → "—"
# ---------------------------------------------------------------------------

def test_render_body_missing_optional_fields(tmp_path) -> None:
    """Tier 1: render_body handles missing optional fields gracefully (regressed_count → '—')."""
    template = tmp_path / "tmpl.md"
    template.write_text(
        "## Discussion body (paste this into GitHub)\n\n"
        "```markdown\n"
        "- Regressed: <regressed_count>\n"
        "- Brier: <float>\n"
        "```\n",
        encoding="utf-8",
    )

    summary = _make_summary(brier_score=None)
    # Ensure regressed_count is absent
    summary.pop("regressed_count", None)

    body = render_body(summary, template)

    assert "—" in body   # both regressed_count and brier_score fall back to "—"


# ---------------------------------------------------------------------------
# 7. publish_run dry-run — no HTTP calls
# ---------------------------------------------------------------------------

def test_publish_run_dry_run_no_http(tmp_path) -> None:
    """Tier 1: publish_run with dry_run=True returns body/title without making HTTP calls."""
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    summary = _make_summary()
    (run_dir / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8",
    )

    config = _make_config()

    # No http_client passed → if any HTTP were attempted, it would fail with
    # a real network error; the test passes only if no request is made.
    result = publish_run(
        "run1",
        config=config,
        storage_dir=run_dir,
        dry_run=True,
    )

    assert result["discussion_number"] is None
    assert result["discussion_url"] is None
    assert "Batch 27" in result["title"]
    assert isinstance(result["body"], str)
    assert len(result["body"]) > 0


# ---------------------------------------------------------------------------
# 8. publish_run — mock transport simulating success
# ---------------------------------------------------------------------------

def test_publish_run_success_with_mock_transport(tmp_path) -> None:
    """Tier 2: publish_run with mock transport returns {discussion_number, discussion_url}."""
    run_dir = tmp_path / "run2"
    run_dir.mkdir()
    summary = _make_summary()
    (run_dir / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8",
    )

    # GraphQL returns differ by mutation vs query: we respond to all with a
    # pre-canned response that covers createDiscussion.
    # For DEFAULT_REPO the resolve_* calls use shipped defaults (no HTTP).
    # Only createDiscussion hits the wire.
    def _handler(request: httpx.Request) -> httpx.Response:
        body_text = request.content.decode()
        body_json = json.loads(body_text)
        query = body_json.get("query", "")

        if "createDiscussion" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "createDiscussion": {
                            "discussion": {
                                "id": "D_kwDOSWAku84AAAA",
                                "number": 42,
                                "url": "https://github.com/tya5/reyn/discussions/42",
                            }
                        }
                    }
                },
            )
        # Fallback for any other query
        return httpx.Response(200, json={"data": {}})

    client = _mock_client(_handler)
    config = _make_config()

    result = publish_run(
        "run2",
        config=config,
        storage_dir=run_dir,
        dry_run=False,
        http_client=client,
    )

    assert result["discussion_number"] == 42
    assert result["discussion_url"] == "https://github.com/tya5/reyn/discussions/42"
    assert "Batch 27" in result["title"]


# ---------------------------------------------------------------------------
# 9. publish_run — fails clearly when no token
# ---------------------------------------------------------------------------

def test_publish_run_no_token_raises(tmp_path) -> None:
    """Tier 1: publish_run raises RuntimeError with a clear message when no token is set."""
    run_dir = tmp_path / "run3"
    run_dir.mkdir()
    summary = _make_summary()
    (run_dir / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8",
    )

    config = _make_config(token=None)

    with pytest.raises(RuntimeError, match="No GitHub token available"):
        publish_run(
            "run3",
            config=config,
            storage_dir=run_dir,
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# 10. CLI argparse — publish subcommand
# ---------------------------------------------------------------------------

def test_cli_publish_argparse() -> None:
    """Tier 1: CLI argparse correctly parses 'reyn dogfood publish RUN_ID --repo X/Y --dry-run'."""
    from reyn.cli.commands.dogfood import register

    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="cmd")
    register(sub)

    args = root.parse_args(
        ["dogfood", "publish", "my-run-id", "--repo", "acme/test-repo", "--dry-run"]
    )

    assert args.dogfood_cmd == "publish"
    assert args.run_id == "my-run-id"
    assert args.repo == "acme/test-repo"
    assert args.dry_run is True
    assert args.category == DEFAULT_CATEGORY_SLUG  # default


def test_cli_publish_argparse_defaults() -> None:
    """Tier 1: CLI argparse applies expected defaults for publish subcommand."""
    from reyn.cli.commands.dogfood import register

    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="cmd")
    register(sub)

    args = root.parse_args(["dogfood", "publish", "some-run"])

    assert args.dogfood_cmd == "publish"
    assert args.run_id == "some-run"
    assert args.repo is None          # not defaulted in argparse; resolved at runtime
    assert args.dry_run is False
    assert args.template is None
    assert args.batch_id is None
    assert args.topic is None


# ---------------------------------------------------------------------------
# Bonus: _split_repo contract
# ---------------------------------------------------------------------------

def test_split_repo_valid() -> None:
    """Tier 1: _split_repo parses 'owner/name' into a 2-tuple."""
    assert _split_repo("tya5/reyn") == ("tya5", "reyn")


def test_split_repo_invalid() -> None:
    """Tier 1: _split_repo raises ValueError on malformed input."""
    with pytest.raises(ValueError, match="Invalid repo format"):
        _split_repo("notaslash")

    with pytest.raises(ValueError, match="Invalid repo format"):
        _split_repo("a/b/c")


# ---------------------------------------------------------------------------
# Bonus: build_title — missing required fields raise ValueError
# ---------------------------------------------------------------------------

def test_build_title_missing_batch_id() -> None:
    """Tier 1: build_title raises ValueError when batch_id is absent."""
    summary = _make_summary()
    del summary["batch_id"]
    with pytest.raises(ValueError, match="batch_id"):
        build_title(summary)


def test_build_title_missing_topic() -> None:
    """Tier 1: build_title raises ValueError when topic is absent."""
    summary = _make_summary()
    del summary["topic"]
    with pytest.raises(ValueError, match="topic"):
        build_title(summary)


def test_build_title_format() -> None:
    """Tier 1: build_title produces the correct title format."""
    summary = _make_summary(
        batch_id=27,
        topic="chat router smoke",
        verified=12,
        total=16,
        regressed_count=1,
        started_at="2026-05-17T10:00:00+00:00",
    )
    title = build_title(summary)

    # Must match: Batch <N> (YYYY-MM-DD): <topic> — <pct>% verified, <N> regressed
    assert title.startswith("Batch 27 (2026-05-17): chat router smoke")
    assert "75% verified" in title
    assert "1 regressed" in title


def test_build_title_no_regressed_count() -> None:
    """Tier 1: build_title uses '— regressed' when regressed_count is absent."""
    summary = _make_summary(verified=10, total=10)
    summary.pop("regressed_count", None)
    title = build_title(summary)
    assert "— regressed" in title


# ---------------------------------------------------------------------------
# Bonus: resolve_category_id uses shipped defaults for default repo
# ---------------------------------------------------------------------------

def test_resolve_category_id_default_repo_no_http() -> None:
    """Tier 2: resolve_category_id returns the shipped default without HTTP calls."""
    from reyn.dogfood.publish import _DEFAULT_CATEGORY_ID

    # If HTTP were attempted without a transport, httpx would raise.
    # Passing no http_client and matching DEFAULT_REPO triggers the fast path.
    result = resolve_category_id(
        "any-token", DEFAULT_REPO, DEFAULT_CATEGORY_SLUG,
    )
    assert result == _DEFAULT_CATEGORY_ID


def test_resolve_repo_id_default_repo_no_http() -> None:
    """Tier 2: resolve_repo_id returns the shipped default without HTTP calls."""
    from reyn.dogfood.publish import _DEFAULT_REPO_NODE_ID

    result = resolve_repo_id("any-token", DEFAULT_REPO)
    assert result == _DEFAULT_REPO_NODE_ID
