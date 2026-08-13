"""Tier 2: #4482 PR-1 — ``GET /agents/<agent>/artifacts/<ref>``.

Mirrors ``test_resources_router.py``'s own structure/fixture for the
sibling ``/tool-results/<artifact>`` route, applied to this SEPARATE
ref-based route: happy path, traversal defense-in-depth, not_found
semantics, unknown agent. Real FastAPI TestClient + real on-disk state
throughout, no mocks — Tier 2 for the same reason the sibling file gives:
this is a cross-host integrity boundary.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.paths import REPO_ROOT

_WORKTREE_SRC = REPO_ROOT / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same fixture shape as test_resources_router.py's own — a minimal
    Reyn project with one agent registered, deps cleared, cwd chdir'd so
    the route's ``Path.cwd()``-rooted resolution matches the fixture."""
    reyn_dir = tmp_path / ".reyn"
    agents_dir = reyn_dir / "agents" / "researcher"
    agents_dir.mkdir(parents=True)

    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    (agents_dir / "profile.yaml").write_text(
        "name: researcher\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )

    import reyn.interfaces.web.deps as deps
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)
    yield tmp_path

    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None


def _client():
    from reyn.interfaces.web.server import app
    from tests._support.web_auth import local_operator_client
    return local_operator_client(app, raise_server_exceptions=False)


def _mint(tmp_project: Path, rel_path: str, content: bytes = b"hello world\n") -> str:
    """Write a real file under tmp_project and mint a ref for it — the
    candidate set #4482 targets (any project file, not just a MediaStore
    tool result)."""
    from reyn.data.workspace.artifact_ref import mint_ref

    target = tmp_project / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return mint_ref(tmp_project, "researcher", target)


# ── happy path ─────────────────────────────────────────────────────────


def test_get_serves_a_minted_artifact_body(tmp_project: Path):
    """Tier 2: a ref minted for an arbitrary project file (not a
    MediaStore tool result) is fetchable through this route — the whole
    point of a SEPARATE route from /tool-results/<artifact>."""
    ref = _mint(tmp_project, "report.html", content=b"<h1>hi</h1>")

    response = _client().get(f"/agents/researcher/artifacts/{ref}")

    assert response.status_code == 200, response.text
    assert response.content == b"<h1>hi</h1>"


def test_content_type_derives_from_the_resolved_filename(tmp_project: Path):
    """Tier 2: Content-Type is derived from the RESOLVED file's own name,
    not the opaque ref string."""
    ref = _mint(tmp_project, "report.html", content=b"<h1>hi</h1>")

    response = _client().get(f"/agents/researcher/artifacts/{ref}")

    assert response.headers["content-type"].startswith("text/html")


def test_regenerated_content_is_served_not_the_original_bytes(tmp_project: Path):
    """Tier 2: path (not content-hash) identity in action end-to-end —
    the file is rewritten AFTER minting, and the route serves TODAY's
    bytes, not a stale copy."""
    ref = _mint(tmp_project, "report.html", content=b"version one")
    (tmp_project / "report.html").write_bytes(b"version two")

    response = _client().get(f"/agents/researcher/artifacts/{ref}")

    assert response.content == b"version two"


# ── not_found semantics ────────────────────────────────────────────────


def test_get_unknown_ref_returns_404(tmp_project: Path):
    """Tier 2: a ref that was never minted returns 404, not a crash."""
    response = _client().get("/agents/researcher/artifacts/never-minted")
    assert response.status_code == 404


def test_get_deleted_target_returns_404(tmp_project: Path):
    """Tier 2: the file existed at mint time but was deleted before
    fetch — matches ``resolve_ref``'s own None-on-deletion contract
    surfaced as HTTP semantics."""
    ref = _mint(tmp_project, "report.html")
    (tmp_project / "report.html").unlink()

    response = _client().get(f"/agents/researcher/artifacts/{ref}")

    assert response.status_code == 404


def test_get_unknown_agent_returns_404(tmp_project: Path):
    """Tier 2: an unregistered agent returns 404, mirroring the sibling
    route's own probe-prevention posture."""
    _mint(tmp_project, "report.html")
    response = _client().get("/agents/nonexistent/artifacts/whatever")
    assert response.status_code == 404


def test_a_table_entry_pointing_outside_project_root_is_rejected(tmp_project: Path):
    """Tier 2: defense-in-depth falsify — mint_ref never validates what
    it's given (see artifact_ref.py's own docstring), so this route must
    NOT trust the table blindly. Simulates a table entry pointing outside
    project_root (a hand-crafted/compromised entry, since mint_ref itself
    only ever writes an already-normalized path under project_root in
    the real flow) and confirms the route's own re-check at serve time
    rejects it rather than leaking the file."""
    import json

    outside = tmp_project.parent / "outside-secret.txt"
    outside.write_bytes(b"SECRET")
    table = tmp_project / ".reyn" / "cache" / "artifact_refs.jsonl"
    table.parent.mkdir(parents=True, exist_ok=True)
    with table.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ref": "evil", "agent": "researcher", "path": str(outside)}) + "\n")

    response = _client().get("/agents/researcher/artifacts/evil")

    assert response.status_code == 400
    assert b"SECRET" not in response.content


def test_a_ref_minted_for_one_agent_is_not_visible_to_another(tmp_project: Path):
    """Tier 2: scope is per-agent — a ref minted under one agent's scope
    does not resolve when fetched under a DIFFERENT (but real,
    registered) agent's route. Registers a second agent directly rather
    than depending on any particular onboarding flow."""
    (tmp_project / ".reyn" / "agents" / "other").mkdir(parents=True)
    (tmp_project / ".reyn" / "agents" / "other" / "profile.yaml").write_text(
        "name: other\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    import reyn.interfaces.web.deps as deps
    deps._registry = None

    ref = _mint(tmp_project, "report.html")

    response = _client().get(f"/agents/other/artifacts/{ref}")

    assert response.status_code == 404
