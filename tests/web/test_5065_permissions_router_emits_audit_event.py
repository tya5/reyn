"""Tier 2: #5065 — the ``/api/permissions`` REST router's own management
operations (revoke a single approval, clear all approvals) each emit a
P6 audit-event, closing the band violation (permission x audit-events)
this issue names: ``.reyn/approvals.yaml`` had two writers (the
security-side ``_persist`` flow, which journals, and this router's own
``_save``, a raw ``path.write_text`` with no audit trail of its own) and
only one of them was observable through ``.reyn/events``.

Real FastAPI TestClient + a real on-disk ``.reyn/`` tree throughout — no
mocks. Witness reads the actual ``.reyn/events/`` files this PR's new
``emit_direct_event`` seam writes (mirrors ``emit_cli_event``'s own
``.reyn/events/direct/cli/`` shape, generalized to
``.reyn/events/direct/<surface>/`` — here ``surface="web"``).

Strip-falsifier for each witness: comment out the corresponding
``emit_direct_event(...)`` call in ``routers/permissions.py`` — the
route still returns 204 (the write itself is untouched) but no new
``.reyn/events`` file/line appears, turning the witness red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.paths import REPO_ROOT

_WORKTREE_SRC = REPO_ROOT / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed (core dependency since #5051 -- stale environment)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same fixture shape as ``test_4482_get_artifact_by_ref.py``'s own — a
    minimal Reyn project, deps cleared, cwd chdir'd."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir(parents=True)
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")

    import reyn.interfaces.web.deps as deps
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    monkeypatch.setattr("reyn.config._find_project_root", lambda _cwd: tmp_path)
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


def _seed_approval(tmp_project: Path, key: str, approved: bool = True) -> None:
    path = tmp_project / ".reyn" / "approvals.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data[key] = approved
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _read_direct_web_events(tmp_project: Path) -> list[dict]:
    """Read every event line under .reyn/events/direct/web/ (this PR's new
    ``surface="web"`` directory — mirrors ``emit_cli_event``'s own
    ``direct/cli/`` shape, see that seam's own docstring)."""
    web_dir = tmp_project / ".reyn" / "events" / "direct" / "web"
    if not web_dir.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(web_dir.glob("*/*.jsonl")):  # month-dir nesting, see EventStore._open_new_file
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ── witness ① — revoke_permission ─────────────────────────────────────────


def test_revoke_permission_emits_one_audit_event_naming_the_key(tmp_project: Path):
    """Tier 2: DELETE /api/permissions/{key} once -> exactly one new
    ``.reyn/events`` entry, naming the REVOKED key (not just "one more
    event appeared" -- a count-only witness is blind to a mismatched key,
    per lead-coder's own strengthening of this issue's accept criterion)."""
    _seed_approval(tmp_project, "chat_router/http.get/example.com")
    assert _read_direct_web_events(tmp_project) == []

    response = _client().delete("/api/permissions/chat_router%2Fhttp.get%2Fexample.com")
    assert response.status_code == 204, response.text

    events = _read_direct_web_events(tmp_project)
    assert events, "expected an audit event, got none"
    assert events[1:] == [], f"expected exactly one audit event, got {events}"
    assert events[0]["type"] == "permission_approval_revoked"
    assert events[0]["data"]["key"] == "chat_router/http.get/example.com"
    assert events[0]["data"]["surface"] == "web"

    # The write itself is unaffected by the audit addition -- approvals.yaml
    # no longer carries the revoked key.
    saved = yaml.safe_load(
        (tmp_project / ".reyn" / "approvals.yaml").read_text(encoding="utf-8")
    ) or {}
    assert "chat_router/http.get/example.com" not in saved


# ── witness ② — clear_permissions ─────────────────────────────────────────


def test_clear_permissions_emits_one_audit_event_with_the_cleared_count(
    tmp_project: Path,
):
    """Tier 2: DELETE /api/permissions (bulk clear) also emits an
    audit-event -- lead-coder's explicit strengthening: "clear_permissions
    must carry the SAME witness -- a form where only one of the two
    writers is structurally impossible." No single key to name for a bulk
    clear, so the event carries the count of entries actually cleared."""
    _seed_approval(tmp_project, "chat_router/http.get/a.example.com")
    _seed_approval(tmp_project, "chat_router/http.get/b.example.com")
    assert _read_direct_web_events(tmp_project) == []

    response = _client().request(
        "DELETE", "/api/permissions", json={"confirm": True},
    )
    assert response.status_code == 204, response.text

    events = _read_direct_web_events(tmp_project)
    assert events, "expected an audit event, got none"
    assert events[1:] == [], f"expected exactly one audit event, got {events}"
    assert events[0]["type"] == "permission_approvals_cleared"
    assert events[0]["data"]["count"] == 2
    assert events[0]["data"]["surface"] == "web"

    saved = yaml.safe_load(
        (tmp_project / ".reyn" / "approvals.yaml").read_text(encoding="utf-8")
    ) or {}
    assert saved == {}
