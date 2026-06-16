"""Tier 2: ``_serialize`` augments intervention meta with queued_count.

Issue #276 Phase B (3/5). The WS server adds ``queued_count`` to
forwarded ``kind="intervention"`` frames so the TUI in ``--connect``
mode can populate its ``+N more pending`` badge from meta when the
proxy's local ``_interventions`` registry is absent.

Pins the augmentation contract via direct calls to ``_serialize``
with a stub session — no FastAPI / WebSocket bootstrap needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
from reyn.chat.outbox import OutboxMessage  # noqa: E402
from reyn.interfaces.web.ws.chat import _serialize  # noqa: E402


class _StubRegistry:
    def __init__(self, count: int) -> None:
        self._count = count

    def queued_count(self) -> int:
        return self._count


class _StubSession:
    def __init__(self, count: int) -> None:
        self._interventions = _StubRegistry(count)


def test_serialize_intervention_inlines_queued_count_from_session() -> None:
    """Tier 2: intervention kind + session with registry → meta gains
    ``queued_count``."""
    msg = OutboxMessage(
        kind="intervention",
        text="Permission request",
        meta={"intervention_id": "iv-abc", "prompt": "OK?"},
    )
    payload = json.loads(_serialize(msg, session=_StubSession(count=3)))
    assert payload["meta"]["queued_count"] == 3
    # Existing meta keys preserved.
    assert payload["meta"]["intervention_id"] == "iv-abc"
    assert payload["meta"]["prompt"] == "OK?"


def test_serialize_non_intervention_kinds_not_augmented() -> None:
    """Tier 2: ``status`` / ``agent`` / ``error`` etc. don't get the
    augmentation. The ``queued_count`` field is intervention-specific
    metadata; spraying it on every frame would bloat the wire +
    confuse downstream consumers."""
    for kind in ("status", "agent", "error", "trace"):
        msg = OutboxMessage(kind=kind, text="x", meta={})
        payload = json.loads(_serialize(msg, session=_StubSession(count=5)))
        assert "queued_count" not in payload["meta"], kind


def test_serialize_no_session_or_missing_registry_is_noop() -> None:
    """Tier 2: ``session=None`` or session without ``_interventions``
    → no augmentation, no crash. Defensive — keeps the WS forwarder
    robust against test stubs / mid-shutdown sessions."""
    msg = OutboxMessage(kind="intervention", text="x", meta={})
    payload_no_session = json.loads(_serialize(msg, session=None))
    assert "queued_count" not in payload_no_session["meta"]

    class _NoRegistrySession:
        _interventions = None

    payload_no_reg = json.loads(_serialize(msg, session=_NoRegistrySession()))
    assert "queued_count" not in payload_no_reg["meta"]


def test_serialize_intervention_preserves_caller_supplied_queued_count() -> None:
    """Tier 2: when the caller already populated ``queued_count`` (e.g.
    a future OS-side _iv_meta refactor inlines it), the augmentation
    doesn't overwrite the existing value."""
    msg = OutboxMessage(
        kind="intervention",
        text="x",
        meta={"queued_count": 7},
    )
    payload = json.loads(_serialize(msg, session=_StubSession(count=99)))
    assert payload["meta"]["queued_count"] == 7
