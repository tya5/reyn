"""Tier 2: the UI transport is consolidated onto AG-UI — ws/chat is gone and the
single-writer session outbox has one drain (ADR-0039 P6b).

Two structural invariants, source-scanned so a regression fails CI instead of
silently re-forking the UI transport:

1. **SR4 completeness (clean-break delete):** no ``ws_chat`` / ``/ws/chat``
   reference survives anywhere under ``src/`` — the legacy WebSocket chat route
   is fully retired, not shimmed.
2. **Consumer consolidation:** the AG-UI UI transport (``interfaces/web`` +
   ``interfaces/transport/agui``) contains NO direct ``session.outbox.get()``.
   The legacy route drained ``session.outbox`` directly WHILE
   ``registry.attach`` started the forwarder on the same shared session (two
   concurrent getters that stole frames). With it deleted, every UI surface
   consumes through the P6b broadcast hub's single drain; the shared session's
   outbox has exactly one getter.
"""
from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

# The consolidated AG-UI UI transport surface. The invariant is that none of
# these drain a Session outbox directly — they subscribe to the OutboxHub.
_UI_TRANSPORT_DIRS = (
    _SRC / "interfaces" / "web",
    _SRC / "interfaces" / "transport" / "agui",
)


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_ws_chat_reference_survives_in_src() -> None:
    """Tier 2: SR4 — zero ``ws_chat`` / ``/ws/chat`` / ``ws/chat`` tokens in src.

    Falsify: re-add ``app.include_router(_ws_chat.router)`` (or any ``/ws/chat``
    string) → this goes RED, catching a partial/reverted delete."""
    offenders: list[str] = []
    for path in _py_files(_SRC):
        text = path.read_text(encoding="utf-8")
        for token in ("ws_chat", "/ws/chat", "ws/chat"):
            if token in text:
                offenders.append(f"{path.relative_to(_SRC)}: {token!r}")
    assert not offenders, "ws/chat must be fully retired (SR4):\n" + "\n".join(offenders)


def test_ui_transport_has_no_direct_session_outbox_drain() -> None:
    """Tier 2: the AG-UI UI transport drains no session outbox directly — every
    surface consumes via the OutboxHub, so the shared session's outbox has ONE
    getter (the hub ``_drain``).

    Falsify: reintroduce a per-connection ``session.outbox.get()`` in the AG-UI
    endpoint (the legacy ws/chat drain shape) → this goes RED."""
    offenders: list[str] = []
    for root in _UI_TRANSPORT_DIRS:
        for path in _py_files(root):
            text = path.read_text(encoding="utf-8")
            # A direct drain call on an outbox attribute. The hub subscription
            # path is ``outbox_hub.subscribe(...)`` / ``sub.get()`` — never
            # ``.outbox.get(``.
            if ".outbox.get(" in text:
                offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, (
        "UI transport must consume the session outbox via the hub, not a direct "
        ".outbox.get() drain:\n" + "\n".join(offenders)
    )
