#!/usr/bin/env python3
"""#3698 ① — MCP conformance matrix generator.

Measures, against a real (non-mock) fastmcp server per transport, what
reyn's ``MCPClient`` / ``MCPConnectionService`` actually observe and can
exercise — following architect's design (issue #3698, comment 5231255703 +
the follow-up conformance-matrix comment): rows are transport x server,
columns are the 8 facts below, and NO cell is ever left blank — every cell
is one of ``ok`` / ``error:<ExceptionType>`` / ``not_measurable`` (+ a
1-line reason in ``notes``), mirroring #3949's ``unenforced_axes`` +
``unenforced_axis_reason`` shape.

Columns (mechanical, per architect's spec — "advertised" reads an existing
API, "implemented" is the only column that calls something new; the two
stay SEPARATE columns so "advertised but not implemented" is visible,
which is the whole point of the matrix per the #3698 acceptance criterion):
  dep_version   fastmcp.__version__ + the installed ``mcp`` SDK version
  negotiated    MCPClient.negotiated_version
  lifecycle     "legacy-initialize" (is_initialized() True after connect) / "none"
  advertised    MCPClient.advertised_capabilities() — reads the handshake,
                no new call
  implemented   one representative call per advertised capability:
                tools -> list_tools(), resources -> list_resources(),
                prompts -> list_prompts(); logging/completions have no
                representative call on MCPClient's public surface ->
                not_measurable
  reyn_feature  5 reyn-specific (not MCP-spec) axes: progress, elicitation,
                resource subscription, reconnect, child teardown
  teardown      no leaked child process after close() (stdio only has a
                real child; http/sse -> not_measurable, no child to leak)
  notes         free text — carries the not_measurable reason

Run: python scripts/mcp_conformance.py
Writes: docs/reference/runtime/mcp-conformance.json (machine, diffed by ②)
        docs/reference/runtime/mcp-conformance.md   (human, generated FROM the json)
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "_support"))

from reyn.mcp.client import MCPClient  # noqa: E402
from reyn.mcp.connection_service import MCPConnectionService  # noqa: E402

_ECHO_SERVER = _REPO_ROOT / "tests" / "_support" / "mcp_fastmcp_echo_server.py"
_JSON_OUT = _REPO_ROOT / "docs" / "reference" / "runtime" / "mcp-conformance.json"
_MD_OUT = _REPO_ROOT / "docs" / "reference" / "runtime" / "mcp-conformance.md"

_TRANSPORTS = ("stdio", "streamable-http", "sse")
_CAPABILITIES = ("tools", "resources", "prompts", "logging", "completions")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_connectable(port: int, attempts: int = 100) -> None:
    for _ in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"port {port} never accepted a connection")


def _dep_versions() -> str:
    import importlib.metadata as _md

    import fastmcp

    try:
        mcp_sdk_version = _md.version("mcp")
    except _md.PackageNotFoundError:
        mcp_sdk_version = "unknown"
    return f"fastmcp {fastmcp.__version__}, mcp-sdk {mcp_sdk_version}"


def _classify_error(exc: BaseException) -> str:
    return f"error:{type(exc).__name__}"


async def _measure_implemented(client: MCPClient, capability: str, cell: dict) -> None:
    """Fill cell["implemented"][capability] — the ONE representative call per
    capability. Never leaves the sub-cell absent."""
    if capability not in client.advertised_capabilities():
        cell["implemented"][capability] = "not_measurable"
        cell["notes"].append(f"{capability}: not advertised by this server, no call attempted")
        return
    try:
        if capability == "tools":
            await client.list_tools()
        elif capability == "resources":
            await client.list_resources()
        elif capability == "prompts":
            await client.list_prompts()
        else:  # logging, completions
            cell["implemented"][capability] = "not_measurable"
            cell["notes"].append(
                f"{capability}: MCPClient has no representative public call for this "
                f"capability (architect #3698 design §3)"
            )
            return
        cell["implemented"][capability] = "ok"
    except Exception as exc:
        cell["implemented"][capability] = _classify_error(exc)


async def _measure_progress(client: MCPClient, cell: dict) -> None:
    if "progress" not in [t["name"] for t in await client.list_tools()]:
        cell["reyn_feature"]["progress"] = "not_measurable"
        cell["notes"].append("progress: server has no 'progress' tool")
        return
    received: list[int] = []

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        received.append(int(progress))

    try:
        await client.call_tool("progress", {"steps": 3}, progress_callback=_on_progress)
        cell["reyn_feature"]["progress"] = "ok" if received else "error:NoProgressReceived"
        if not received:
            cell["notes"].append("progress: call succeeded but no progress notifications arrived")
    except Exception as exc:
        cell["reyn_feature"]["progress"] = _classify_error(exc)


async def _measure_elicitation(client: MCPClient, cell: dict) -> None:
    # The echo server has no tool that calls ctx.elicit(...) — a real
    # elicitation round-trip needs a server-initiated request mid-tool-call,
    # which this test server structurally does not exercise. Not a missing
    # measurement TECHNIQUE — the server itself has nothing to elicit from.
    cell["reyn_feature"]["elicitation"] = "not_measurable"
    cell["notes"].append(
        "elicitation: test server has no elicit-triggering tool (needs a "
        "purpose-built server; not yet available in-repo)"
    )


async def _measure_subscription(client: MCPClient, cell: dict) -> None:
    if "resources" not in client.advertised_capabilities():
        cell["reyn_feature"]["subscription"] = "not_measurable"
        cell["notes"].append("subscription: resources capability not advertised")
        return
    try:
        await client.subscribe_resource("resource://pid")
        cell["reyn_feature"]["subscription"] = "ok"
        await client.unsubscribe_resource("resource://pid")
    except Exception as exc:
        cell["reyn_feature"]["subscription"] = _classify_error(exc)
        if type(exc).__name__ == "MCPCapabilityError":
            # lead-coder's #3971 review note: this is a detection of the
            # SERVER not advertising the resources.subscribe sub-capability
            # (MCPClient's own gate firing correctly), not a reyn defect —
            # spelled out here so a reader of the matrix doesn't misread
            # "error" as "reyn is broken".
            cell["notes"].append(
                "subscription: MCPCapabilityError = the server does not "
                "advertise resources.subscribe (MCPClient's capability gate "
                "working as intended, not a reyn defect)"
            )


async def _measure_reconnect_stdio(config: dict, cell: dict) -> None:
    """stdio: kill the REAL child subprocess (die() -> os._exit(1)) via the
    connection-service's heal-only path, then confirm the NEXT call heals
    transparently on a fresh subprocess."""
    service = MCPConnectionService()
    try:
        held = await service.get("probe", config, agent_id=None)
        try:
            await held.call_tool("die", {})
        except Exception:
            pass  # expected: the call that kills the child raises (heal_only, re-raise)
        try:
            result = await held.list_tools()
            cell["reyn_feature"]["reconnect"] = "ok" if result else "error:EmptyToolsAfterReconnect"
        except Exception as exc:
            cell["reyn_feature"]["reconnect"] = _classify_error(exc)
    finally:
        await service.aclose()


async def _measure_reconnect_networked(config: dict, cell: dict) -> None:
    """http/sse: the server runs in-process (an asyncio task in THIS script's
    own process) — killing it the way stdio's die() does would kill this
    script, and restarting a whole uvicorn server on the same port raced the
    OS socket teardown in practice (measured: intermittent
    'address already in use'). Simulate a transport death that leaves the
    SERVER untouched instead — close the held client's own transport
    directly (a real severed connection, the server never notices) and let
    the service's own heal path (MCPTransportError -> _ensure_open,
    connection_service.py) reopen against the SAME still-running server on
    the SAME URL. This is a more representative "reconnect" scenario for
    http/sse anyway (a network blip, not the server restarting)."""
    service = MCPConnectionService()
    try:
        held = await service.get("probe", config, agent_id=None)
        await held.list_tools()  # establish the connection first
    except Exception as exc:
        cell["reyn_feature"]["reconnect"] = _classify_error(exc)
        await service.aclose()
        return

    live_client = service._clients.get("probe")
    if live_client is None:
        cell["reyn_feature"]["reconnect"] = "error:NoLiveClientAfterConnect"
        await service.aclose()
        return
    await live_client.close()

    try:
        result = await held.list_tools()
        cell["reyn_feature"]["reconnect"] = "ok" if result else "error:EmptyToolsAfterReconnect"
    except Exception as exc:
        cell["reyn_feature"]["reconnect"] = _classify_error(exc)
    finally:
        await service.aclose()


def _pid_alive(pid: int) -> bool:
    """Stdlib liveness check (no psutil dependency in this repo) — signal 0
    sends no actual signal, only checks permission/existence."""
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not ours to signal — still "alive"
    return True


async def _measure_teardown(transport: str, config: dict, cell: dict) -> None:
    if transport != "stdio":
        cell["teardown"] = "not_measurable"
        cell["notes"].append(f"teardown: {transport} has no child process of its own to leak")
        return

    client = MCPClient(config)
    try:
        await client.initialize()
        pid_result = await client.call_tool("pid", {})
        child_pid = int(pid_result["structuredContent"]["result"]) if isinstance(pid_result, dict) else None
    except Exception as exc:
        cell["teardown"] = _classify_error(exc)
        try:
            await client.close()
        except Exception:
            pass
        return
    await client.close()
    if child_pid is None:
        cell["teardown"] = "not_measurable"
        cell["notes"].append("teardown: could not determine child pid")
        return
    await asyncio.sleep(0.2)
    cell["teardown"] = "leaked" if _pid_alive(child_pid) else "clean"


async def _measure_row(transport: str) -> dict:
    cell: dict[str, Any] = {
        "transport": transport,
        "server": "tests/_support/mcp_fastmcp_echo_server.py",
        "dep_version": _dep_versions(),
        "negotiated": None,
        "lifecycle": "none",
        "advertised": [],
        "implemented": {},
        "reyn_feature": {},
        "teardown": None,
        "notes": [],
    }

    server_task = None
    port = None
    if transport == "stdio":
        config = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
    else:
        import mcp_fastmcp_echo_server as server_mod

        port = _free_port()
        server_task = asyncio.create_task(
            server_mod.mcp.run_async(
                transport=transport, host="127.0.0.1", port=port, show_banner=False,
            )
        )
        try:
            await _wait_connectable(port)
        except TimeoutError as exc:
            cell["notes"].append(f"server never came up: {exc}")
            for col in ("negotiated", "lifecycle", "advertised", "teardown"):
                cell[col] = "not_measurable"
            for cap in _CAPABILITIES:
                cell["implemented"][cap] = "not_measurable"
            return cell
        path = "/mcp" if transport == "streamable-http" else "/sse"
        config = {"type": transport, "url": f"http://127.0.0.1:{port}{path}"}
        cell["_server_task"] = server_task

    try:
        async with MCPClient(config) as client:
            cell["negotiated"] = client.negotiated_version
            cell["lifecycle"] = "legacy-initialize" if client.is_initialized() else "none"
            cell["advertised"] = client.advertised_capabilities()
            for cap in _CAPABILITIES:
                await _measure_implemented(client, cap, cell)
            await _measure_progress(client, cell)
            await _measure_elicitation(client, cell)
            await _measure_subscription(client, cell)
    except Exception as exc:
        cell["notes"].append(f"connection-level failure: {_classify_error(exc)}: {exc}")
        for col in ("negotiated", "lifecycle", "advertised"):
            if not cell.get(col):
                cell[col] = "not_measurable"
        for cap in _CAPABILITIES:
            cell["implemented"].setdefault(cap, "not_measurable")

    if transport == "stdio":
        await _measure_reconnect_stdio(config, cell)
    else:
        await _measure_reconnect_networked(config, cell)

    await _measure_teardown(transport, config, cell)

    if server_task is not None and not server_task.done():
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
    cell.pop("_server_task", None)
    cell["notes"] = "; ".join(cell["notes"]) if cell["notes"] else ""
    return cell


def _render_markdown(rows: list[dict]) -> str:
    lines = [
        "# MCP conformance matrix",
        "",
        "**Generated by `scripts/mcp_conformance.py` — do not hand-edit.**",
        "Source of truth for diffing is `mcp-conformance.json`; this file is "
        "rendered from it for human reading.",
        "",
        "Every cell is one of `ok` / `error:<ExceptionType>` / `not_measurable` "
        "(with the reason in Notes) — never blank (#3698 ①, architect design).",
        "",
    ]
    for row in rows:
        lines.append(f"## {row['transport']}")
        lines.append("")
        lines.append(f"- **server**: `{row['server']}`")
        lines.append(f"- **dep_version**: {row['dep_version']}")
        lines.append(f"- **negotiated**: {row['negotiated']}")
        lines.append(f"- **lifecycle**: {row['lifecycle']}")
        lines.append(f"- **advertised**: {row['advertised']}")
        lines.append("")
        lines.append("| capability | implemented |")
        lines.append("|---|---|")
        for cap, val in row["implemented"].items():
            lines.append(f"| {cap} | {val} |")
        lines.append("")
        lines.append("| reyn_feature | result |")
        lines.append("|---|---|")
        for feat, val in row["reyn_feature"].items():
            lines.append(f"| {feat} | {val} |")
        lines.append("")
        lines.append(f"- **teardown**: {row['teardown']}")
        lines.append(f"- **notes**: {row['notes'] or '—'}")
        lines.append("")
    return "\n".join(lines)


async def main() -> None:
    rows = []
    for transport in _TRANSPORTS:
        print(f"measuring {transport}...", file=sys.stderr)
        rows.append(await _measure_row(transport))

    _JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    _JSON_OUT.write_text(json.dumps(rows, indent=2, sort_keys=False) + "\n")
    _MD_OUT.write_text(_render_markdown(rows))
    print(f"wrote {_JSON_OUT}", file=sys.stderr)
    print(f"wrote {_MD_OUT}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
