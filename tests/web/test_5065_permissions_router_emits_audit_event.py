"""Tier 2: #5065 — the ``/api/permissions`` REST router's own management
operations (revoke a single approval, clear all approvals) each emit a
P6 audit-event, closing the band violation (permission x audit-events)
this issue names: the approvals store had multiple writers (the
security-side ``_persist`` flow, and this router's own) and only one of
them was observable through ``.reyn/events``. #5153 later moved BOTH onto
the same ``ApprovalLedger`` (append-only ``approvals.jsonl``, replacing
the ``.reyn/approvals.yaml`` snapshot's read-modify-write) — this test's
seeding still uses the legacy YAML format, exercising the migration path
``_load`` runs through on first touch.

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

# fastapi is a core dependency since #5051 (pyproject.toml's `dependencies`,
# no marker) -- an importorskip here would be exactly the silent-skip-on-a-
# broken-install shape #5058 closed (architect ruling, found reviewing this
# PR: the correct behavior on a genuinely broken install is red, not a
# skip). Hard import.
import fastapi  # noqa: F401

# httpx is NOT yet a declared dependency (that's #5059's own content) --
# this importorskip is legitimate today. Remove it the same PR #5059 lands
# (declaring httpx would make this the same #5058-class violation as the
# fastapi guard above).


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

    # The write itself is unaffected by the audit addition -- #5153: folding
    # the ledger no longer shows this key as approved (the row itself
    # SURVIVES, as an explicit revoke record -- audit-events acceptance).
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    saved, _bound, _scopes = ApprovalLedger(
        tmp_project / ".reyn" / "approvals.jsonl"
    ).fold()
    assert saved.get("chat_router/http.get/example.com") is False


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

    from reyn.security.permissions.approval_ledger import ApprovalLedger
    saved, _bound, _scopes = ApprovalLedger(
        tmp_project / ".reyn" / "approvals.jsonl"
    ).fold()
    assert saved == {
        "chat_router/http.get/a.example.com": False,
        "chat_router/http.get/b.example.com": False,
    }


# ── #5153 acceptance ⑥ — cross-writer identity clear ─────────────────────


def test_web_revoke_clears_the_bound_identity_a_live_resolver_would_have_held(
    tmp_project: Path,
):
    """Tier 2: #5153 acceptance ⑥ — the cross-writer witness architect
    explicitly asked to be MEASURED (issuecomment-5383848849), web's own
    half of the SAME check #5153's CLI test makes: this router's
    ``revoke_permission`` builds an ``ApprovalLedger`` directly from
    ``project_root``, entirely bypassing ``deps._get_perm_resolver()``'s
    own module-level singleton (the resolver a running session actually
    gates permission checks through) — so a web-surface revoke must not
    leave THAT resolver's own bound-identity record stale. Verified via a
    FRESH ``PermissionResolver`` (the process-boundary analogue) reading
    the SAME project after the HTTP revoke."""
    import asyncio

    from reyn.security.permissions.approval_ledger import ApprovalLedger
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

    key = "chat_router/file.write/some_dir/"
    target = tmp_project / "some_dir"
    target.mkdir()

    # A live resolver binds the identity (simulating the server's own
    # deps._get_perm_resolver() singleton having gated a real write).
    resolver = PermissionResolver({}, project_root=tmp_project)
    resolver._saved[key] = True
    resolver._persist(key, True)
    asyncio.run(
        resolver.require_file_write(
            PermissionDecl(), str(target / "f.txt"), "chat_router",
        ),
    )
    # #5431: read via a fresh `ApprovalLedger.fold()` (the same production
    # surface `GET /api/permissions` itself uses) rather than the removed
    # `bound_identity_get` accessor, whose only callers were tests.
    ledger_path = tmp_project / ".reyn" / "approvals.jsonl"
    _saved, bound, _scopes = ApprovalLedger(ledger_path).fold()
    assert key in bound

    response = _client().delete(
        "/api/permissions/chat_router%2Ffile.write%2Fsome_dir%2F",
    )
    assert response.status_code == 204, response.text

    # No live PermissionResolver needed to observe this -- the DELETE
    # route writes straight to the SAME on-disk ledger, so a fresh fold
    # (the process-boundary analogue) must show NO bound identity for
    # this key.
    _saved, bound, _scopes = ApprovalLedger(ledger_path).fold()
    assert key not in bound, (
        "a web-surface revoke must clear the bound identity too, not "
        "just the approval row"
    )
