"""AG-UI wire transport for the thin client (ADR-0039 P2).

The SECOND :class:`~reyn.interfaces.transport.client_transport.ClientTransport`,
over HTTP+SSE. P1 unified the CUI onto the transport seam with the local
:class:`~reyn.interfaces.transport.in_process.InProcessTransport`; this package
adds the remote sibling so ``reyn chat --connect`` drives the identical renderer,
a different transport (D2). renderer and stream_client are unchanged.

- :mod:`.protocol` — the reyn ``Frame`` ⇄ AG-UI-event codec (standard envelope +
  reyn-private ``_reyn`` reconstruction; graceful-degrade for generic clients).
- :mod:`.state` — the STATE_* status read-model (snapshot + delta) and the
  present-on-wire per-connection re-guard hook.
- :mod:`.emitter` — server side: a reyn ``Frame`` stream → AG-UI SSE text.
- :mod:`.client` — :class:`AgUiTransport`, the client that decodes SSE back into
  the renderer's ``Frame`` vocabulary.
- :mod:`.endpoint` — the FastAPI SSE endpoint + turn-submit POST, behind the P0
  auth gate.
"""
from __future__ import annotations

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.profile import (
    CUSTOM_PROFILE,
    CustomName,
    is_profiled,
    profiled_names,
)
from reyn.interfaces.transport.agui.protocol import (
    AgUiEvent,
    InterventionTool,
    InterventionToolResult,
    MessagesSnapshot,
    StateUpdate,
    decode_event,
    encode_frame,
    encode_frame_wire,
    encode_intervention_tool_result,
    encode_intervention_tool_start,
    encode_messages_snapshot,
    encode_state_delta,
    encode_state_snapshot,
    intervention_tool_name,
    parse_sse_blocks,
    to_sse,
)
from reyn.interfaces.transport.agui.state import (
    RemoteStatusView,
    StatusModel,
    project_status,
    reguard_nodes,
)
from reyn.interfaces.transport.agui.surface import (
    SurfaceManager,
    SurfaceRegistry,
    surface_registry,
)

__all__ = [
    "AgUiTransport",
    "AgUiEmitter",
    "AgUiEvent",
    "InterventionTool",
    "InterventionToolResult",
    "MessagesSnapshot",
    "StateUpdate",
    "decode_event",
    "encode_frame",
    "encode_frame_wire",
    "encode_intervention_tool_result",
    "encode_intervention_tool_start",
    "encode_messages_snapshot",
    "encode_state_delta",
    "encode_state_snapshot",
    "intervention_tool_name",
    "parse_sse_blocks",
    "to_sse",
    "RemoteStatusView",
    "StatusModel",
    "project_status",
    "reguard_nodes",
    "SurfaceManager",
    "SurfaceRegistry",
    "surface_registry",
    "CUSTOM_PROFILE",
    "CustomName",
    "is_profiled",
    "profiled_names",
]
