"""Client-transport seam for the chat client (ADR-0039 P1).

The inline CUI consumes its session through a single :class:`ClientTransport`
seam — a unified, tagged frame stream (display + renderer-relevant audit-events)
plus a send side — so a local run exercises the same client path a remote
client (P2, AG-UI / SSE) will. :class:`InProcessTransport` is the local
implementation composing the existing forwarder + audit-event subscription
behind the seam; the frame vocabulary lives in
:mod:`reyn.interfaces.transport.frames`.
"""
from __future__ import annotations

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    Frame,
    FrameTag,
    forwarded_frame_kinds,
)
from reyn.interfaces.transport.in_process import InProcessTransport

__all__ = [
    "ClientTransport",
    "DisplayFrame",
    "EventFrame",
    "Frame",
    "FrameTag",
    "InProcessTransport",
    "forwarded_frame_kinds",
]
