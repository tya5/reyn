"""reyn.interfaces.web.openui — OPENUI host adapter implementation.

This package houses Reyn's implementation of the OpenUI Layer 0 host
adapter (window.OPENUI_HOST + invoke + listen) and routes between
Reyn's gateway (FastAPI / WebSocket) and the design's expectations
under the reyn-ui/v1 Layer 1 schema.

See ../../../docs/deep-dives/spec/openui/ for the protocol specification.

This file is intentionally minimal — the actual adapter implementation
lands in PR30. The package is created here so that PR29's docs can
reference its location.
"""
