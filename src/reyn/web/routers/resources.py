"""Resources router — HTTP fetch endpoint for path_ref bodies (#385 β
core impl sub-task 3, cross-host transport surface).

The companion to ``MediaStore`` (= same-host fs storage) on the network
side. When agent A produces a tool result and includes a path_ref in
its response, a cross-host consumer (= agent B on a different Reyn
process, an MCP client, a browser frontend) needs a transport to fetch
the body. This router exposes that surface as a plain HTTP GET so:

* A2A peers can resolve ``FilePart.uri`` URLs with standard HTTP
  semantics (= the A2A spec gap for resource fetch is handled the
  industry-standard way: URI in artifact, HTTP GET to fetch).
* MCP server ``resources/read`` adapter (= future, separate file) can
  dispatch via the same route, no duplicate fetch path.
* Browser / curl / any HTTP client works without Reyn knowledge.

URL convention (= #385 wire shape per lead-coder Q3 leaning toward
Reyn directory naming uniformity):

  GET /agents/<agent_name>/tool-results/<artifact>

``<agent_name>``  — for routing / audit (= which Reyn instance / agent
                    minted this artifact). Today's single-process Reyn
                    has a shared ``.reyn/tool-results/`` dir; the agent
                    segment is the identity that future multi-process
                    deployments dispatch on.
``<artifact>``    — the filename portion of the path_ref, as minted by
                    MediaStore (= ``YYYYMMDDTHHMMSS-<chain_short>-
                    <tool>-<seq>.<ext>``).

Same-host short-circuit (= when the resolved URL would point back to
this very Reyn instance) is the dispatcher's responsibility on the
consumer side; this router unconditionally serves the local file if it
exists. Path-traversal escapes (= ``..`` etc.) are rejected via the
same boundary check ``MediaStore.read_tool_result`` uses.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from reyn.web.deps import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["resources"])


# Conservative MIME inference from file extension. Same intent as
# MediaStore's _MIME_TO_EXT but the inverse direction; we don't import
# it to avoid coupling the HTTP layer to the storage internals.
_EXT_TO_MIME: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
    ".html": "text/html; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".xml":  "application/xml",
}


def _mime_for(artifact: str) -> str:
    """Return a Content-Type for the given artifact filename.

    Defaults to ``application/octet-stream`` for unknown extensions —
    the caller can still consume the bytes; only the auto-render in a
    browser degrades.
    """
    suffix = Path(artifact).suffix.lower()
    return _EXT_TO_MIME.get(suffix, "application/octet-stream")


# ── GET /agents/<agent>/tool-results/<artifact> ───────────────────────


@router.get("/agents/{agent_name}/tool-results/{artifact}")
async def get_tool_result(
    agent_name: str,
    artifact: str,
    request: Request,  # noqa: ARG001 — kept for symmetry with a2a routes
    registry=Depends(get_registry),
) -> Response:
    """Serve a tool-result file by ``<agent>/<artifact>`` route.

    Validates the agent exists in the registry (= 404 if not) and that
    the artifact resolves inside ``.reyn/tool-results/`` (= 400 if a
    path-traversal escape is attempted, 404 if the file is absent /
    deleted by the user). Returns the body bytes with a best-effort
    Content-Type derived from the artifact extension.

    Authentication: relies on whatever transport-layer protection
    ``reyn web`` is fronted by (= same posture as ``/a2a/agents/...``
    endpoints, no per-route auth in this MVP). When auth becomes a
    concern, it'd be added as middleware on the whole router.
    """
    # 1) Agent existence: prevents arbitrary path scans for
    #    ``/agents/<garbage>/tool-results/<probe>`` attempts.
    if not registry.exists(agent_name):
        raise HTTPException(
            status_code=404,
            detail=f"Reyn agent {agent_name!r} not found on this server.",
        )

    # 2) Path-traversal protection: the artifact must resolve inside
    #    the project's ``.reyn/tool-results/`` directory. We re-use
    #    MediaStore's existing boundary check so the route and the
    #    same-host fs reader share one rule (= no chance of drift).
    from reyn.workspace.media_store import MediaStore, MediaStoreConfig
    store = MediaStore(
        MediaStoreConfig(),
        project_root=Path.cwd(),
        agent_name=agent_name,
    )
    rel_path = str(
        (store.tool_results_dir / artifact).relative_to(Path.cwd()),
    )
    try:
        body, found = store.read_tool_result(rel_path)
    except PermissionError as exc:
        # MediaStore raises PermissionError when the resolved path
        # escapes ``tool_results_dir`` — this catches a malicious /
        # malformed ``artifact`` that includes ``..`` or absolute paths.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tool result {artifact!r} not found for agent "
                f"{agent_name!r} (= deleted by user, or never existed)"
            ),
        )

    return Response(content=body, media_type=_mime_for(artifact))
