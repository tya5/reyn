"""Tier 2: #5067 — the ``/api/budget/caps`` REST router's own management
operation (raising/lowering a live hard cap) emits a P6 audit-event,
closing the band violation this issue names on the OTHER band pairing
#5065 closed on permissions.py: cost-budget x audit-events, not
permission x audit-events. Before this, ``PATCH /api/budget/caps`` mutated
``tracker.config`` in place with no observable trace.

Real FastAPI TestClient + a real on-disk ``.reyn/`` tree throughout — no
mocks. Reuses #5065's ``emit_direct_event`` seam (``core/events/events.py``)
directly, so this witness reads the same ``.reyn/events/direct/web/``
files that PR wrote — mirrors ``test_5065_permissions_router_emits_audit_
event.py``'s own reader.

Strip-falsifier: comment out the ``emit_direct_event(...)`` call in
``routers/budget.py`` — the PATCH still applies the new caps (the write
itself is untouched) but no new ``.reyn/events`` file/line appears,
turning the witness red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.paths import REPO_ROOT

_WORKTREE_SRC = REPO_ROOT / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# fastapi is a core dependency since #5051 (pyproject.toml's `dependencies`,
# no marker) -- an importorskip here would be the silent-skip-on-a-broken-
# install shape #5058 closed (architect ruling, #5068). Hard import.
import fastapi  # noqa: F401

# httpx is NOT yet a declared dependency (that's #5059's own content) --
# this importorskip is legitimate today. Remove it the same PR #5059 lands.


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same fixture shape as ``test_5065_permissions_router_emits_audit_
    event.py``'s own — a minimal Reyn project, deps cleared, cwd chdir'd."""
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


def _read_direct_web_events(tmp_project: Path) -> list[dict]:
    """Read every event line under .reyn/events/direct/web/ (#5065's
    ``surface="web"`` directory, month-dir nested — see
    ``EventStore._open_new_file``)."""
    web_dir = tmp_project / ".reyn" / "events" / "direct" / "web"
    if not web_dir.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(web_dir.glob("*/*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ── witness ① — a cap change emits exactly one event, naming what changed ──


def test_patch_budget_caps_emits_one_audit_event_naming_the_changed_fields(
    tmp_project: Path,
):
    """Tier 2: PATCH /api/budget/caps changing two fields -> exactly one
    new .reyn/events entry, naming BOTH changed fields with their old AND
    new values (not just "one more event appeared" -- lead-coder's #5065
    strengthening applied here too: a count-only witness is blind to a
    mismatched field/value; the "from" side matters MORE here than in
    #5065, since this change is not persisted anywhere else -- this
    event is the only place the prior value survives at all)."""
    assert _read_direct_web_events(tmp_project) == []

    response = _client().patch(
        "/api/budget/caps",
        json={
            "daily_tokens_hard_limit": 500000,
            "per_agent_cost_usd_hard_limit": 12.5,
        },
    )
    assert response.status_code == 200, response.text

    events = _read_direct_web_events(tmp_project)
    assert events, "expected an audit event, got none"
    assert events[1:] == [], f"expected exactly one audit event, got {events}"
    assert events[0]["type"] == "budget_caps_updated"
    changes = events[0]["data"]["changes"]
    assert changes["daily_tokens"]["to"] == 500000
    assert changes["daily_tokens"]["from"] is None  # default config: unset
    assert changes["per_agent_cost_usd"]["to"] == 12.5
    assert changes["per_agent_cost_usd"]["from"] is None
    assert events[0]["data"]["surface"] == "web"

    # The write itself is unaffected by the audit addition.
    body = response.json()
    assert body["caps"]["daily_tokens"]["hard_limit"] == 500000
    assert body["caps"]["per_agent_cost_usd"]["hard_limit"] == 12.5


def test_patch_budget_caps_second_change_records_the_real_prior_value(
    tmp_project: Path,
):
    """Tier 2: a SECOND PATCH's ``from`` reflects the value the FIRST
    PATCH actually set — not always None. This is the only place either
    value survives (the change is never persisted, lead-coder's own
    measurement), so a ``from`` that silently stayed None on a genuine
    transition would be a fabricated record, not a missing one."""
    client = _client()
    client.patch("/api/budget/caps", json={"daily_tokens_hard_limit": 500000})

    response = client.patch(
        "/api/budget/caps", json={"daily_tokens_hard_limit": 750000},
    )
    assert response.status_code == 200, response.text

    events = _read_direct_web_events(tmp_project)
    assert events[:2], "expected two audit events (one per PATCH), got fewer"
    assert events[2:] == [], f"expected exactly two audit events, got {events}"
    second = events[1]
    assert second["type"] == "budget_caps_updated"
    assert second["data"]["changes"]["daily_tokens"] == {"from": 500000, "to": 750000}


# ── witness ② — an all-None PATCH changes nothing, emits nothing ──────────


def test_patch_budget_caps_with_no_changes_emits_no_event(tmp_project: Path):
    """Tier 2: a PATCH body with every field left ``None`` (no actual
    cap change) does not fabricate an event -- ``changes`` would be
    empty, and #5067's own ruling (mirroring #5065) is never to write a
    field the request cannot answer. This test alone is not a witness for
    the emit MECHANISM (it would stay green even if the whole emit call
    were deleted) -- that witness is carried by the two tests above."""
    assert _read_direct_web_events(tmp_project) == []

    response = _client().patch("/api/budget/caps", json={})
    assert response.status_code == 200, response.text

    assert _read_direct_web_events(tmp_project) == []
