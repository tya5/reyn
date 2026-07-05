"""
MCP client — thin wrapper around ``fastmcp.Client`` (v3.4.2; #2597 S1).

Supports two transports today: ``stdio`` and ``http`` (Streamable HTTP);
``sse`` uses FastMCP's ``SSETransport`` (previously ``NotImplementedError`` —
a free win from the swap: no incremental cost to wire once FastMCP's own
transport inference exists).

Each ``MCPClient`` owns a single ``fastmcp.Client`` opened on
:meth:`initialize` and torn down on :meth:`close`. FastMCP's ``Client`` is
itself a reentrant async context manager wrapping the transport + the
underlying ``mcp.ClientSession``; MCPClient enters it once and holds it open
for the object's lifetime (matching the previous hand-rolled client's
caching semantics on ``OpContext.mcp_clients`` / the pool's subprocess-reuse
contract — FastMCP's ``StdioTransport(keep_alive=True)`` is the same
persistent-subprocess semantics).

Environment variable expansion:
  ``${VAR_NAME}`` in any string config value is replaced with
  ``os.environ.get("VAR_NAME", "")``. Missing variables expand to empty
  string and a warning is emitted. Apply :func:`expand_env` BEFORE
  handing config to the SDK.

Capability / version gate (#2597 capability slice):
  MCP's ``initialize`` handshake natively negotiates BOTH a protocol version
  and a set of server capabilities (tools/resources/prompts/logging/
  completions) in one round trip — rather than sprinkling version checks
  across reyn, :meth:`initialize` captures both ONCE, right after FastMCP's
  ``client.__aenter__()`` completes the handshake (verified against fastmcp
  3.4.2: ``Client.initialize_result`` — an ``mcp.types.InitializeResult`` —
  is populated at that point; see ``initialize()``'s inline comment for the
  exact source-file/line trail). :meth:`supports` answers "did the server
  advertise capability X" (conservative False before initialize / on a
  missing result); :func:`require_capability` is the enforcement seam —
  call it before issuing a request for a gated feature so an unsupported
  one fails fast with a reyn-authored error instead of a confusing raw
  protocol error. Today only ``call_tool``/``list_tools`` call it (gated on
  ``"tools"``); a later slice plugs resources/prompts requests into the
  SAME helper before they reach the server. :attr:`negotiated_version`
  exposes the raw protocol version string for callers/later slices to
  branch on — this slice deliberately does not build a version-semantics
  matrix, just makes the version + capabilities readable and gated.
"""
from __future__ import annotations

import os
import tempfile
import warnings
from typing import Any, NoReturn

# ── Env var expansion ─────────────────────────────────────────────────────────
# Shared resolver lives in reyn.security.secrets.interpolation (ADR-0030).
# This re-export keeps the public surface of this module backward-compatible:
# callers that import ``from reyn.mcp.client import expand_env`` continue to
# work without change.
from reyn.security.secrets.interpolation import expand_env as expand_env  # noqa: F401

# ── Errors ───────────────────────────────────────────────────────────────────

class MCPError(RuntimeError):
    """Raised on any MCP transport / protocol / tool error."""


class MCPCapabilityError(MCPError):
    """Raised by :func:`require_capability` when the connected server did not
    advertise the requested capability. This is a REFUSAL, not a transport
    failure — the connection is healthy, reyn is just declining to send a
    request the server never said it supports. :class:`~reyn.mcp.
    connection_service.MCPConnectionService`'s ``_HeldConnection._heal`` must
    NOT treat this as a dead-connection signal (see :class:`MCPTransportError`
    for the one exception type that IS)."""


class MCPTransportError(MCPError):
    """Raised in place of plain :class:`MCPError` when the underlying failure is
    genuine transport-death — a dead subprocess (stdio) or a broken connection
    (http/sse) — as opposed to an application-level protocol error (unknown
    tool/resource, invalid params, a tool that raised) or a capability-gate
    refusal (:class:`MCPCapabilityError`). Raised by :func:`_classify_and_raise`
    at every SDK-call boundary in this module (``call_tool``/``list_tools``/
    ``read_resource``/``list_resources``/``list_resource_templates``) — see that
    function's docstring for the exact predicate, verified against the
    installed fastmcp 3.4.2 + mcp SDK. This is the ONLY exception type
    ``_HeldConnection._heal`` (connection_service.py) treats as a dead-
    connection signal that should discard + reopen the held connection; a
    plain ``MCPError`` (app-level) or ``MCPCapabilityError`` (gate refusal)
    propagates WITHOUT recycling a perfectly healthy connection (#2597 F1 —
    the pre-fix ``except MCPError:`` over-caught both of those)."""


_SUPPORTED_TYPES = {"stdio", "http", "sse"}

# #2597 capability/version gate slice: the ``ServerCapabilities`` fields FastMCP's
# ``mcp.types.InitializeResult.capabilities`` may carry — each is either a capability
# object (server advertises it) or None (server does not). ``experimental`` and
# ``tasks`` are deliberately excluded: they aren't reyn features today (no gate to
# apply), unlike the five below which map 1:1 onto MCP feature surfaces reyn calls
# or will call in a later slice (resources/prompts).
_CAPABILITY_NAMES = frozenset({"tools", "resources", "prompts", "logging", "completions"})


def require_capability(client: "MCPClient", capability: str) -> None:
    """Fail fast with a clear reyn error if ``client``'s connected server did not
    advertise ``capability`` in its initialize handshake — the #2597 enforcement
    seam. Call this BEFORE issuing a request for a gated feature (today: tool
    calls, gated on ``"tools"``; a later slice plugs resources/prompts requests
    into this same helper before they reach the server) so an unsupported feature
    fails with a reyn-authored message instead of a confusing raw protocol error
    from the server.

    Raises :class:`MCPCapabilityError` (an :class:`MCPError` subclass — existing
    ``except MCPError`` callers keep working unchanged) if not supported; no-op
    (returns None) otherwise. #2597 F1: this is a REFUSAL raised before any
    request reaches the server, never a transport failure — a distinct
    subclass from :class:`MCPTransportError` so ``_HeldConnection._heal`` can
    tell "gate refused this call" apart from "the connection died" and leave a
    healthy held connection alone on a gate refusal.
    """
    if client.supports(capability):
        return
    server = client.server_name or "<unknown>"
    version = client.negotiated_version or "<unknown>"
    raise MCPCapabilityError(
        f"MCP server {server!r} does not advertise the {capability!r} capability "
        f"(negotiated protocol version {version}). Refusing to call a "
        f"{capability!r} feature against it."
    )


def _is_transport_death(exc: BaseException) -> bool:
    """Return True iff ``exc`` (caught at an SDK-call boundary in this module)
    signals genuine MCP transport death — as opposed to an application-level
    protocol error the server responded with while alive and connected.

    #2597 F1 predicate — verified by reading the installed fastmcp 3.4.2 +
    mcp SDK source AND by live-probing both branches against the real
    ``tests/_support/mcp_fastmcp_echo_server.py`` test double over stdio:

      - ``mcp.shared.exceptions.McpError`` whose ``.error.code`` equals
        ``mcp.types.CONNECTION_CLOSED`` (``-32000``). ``mcp.shared.session.
        BaseSession``'s receive loop (session.py) catches
        ``anyio.ClosedResourceError`` when the transport's read stream closes
        underneath it and, in the ``finally``, synthesizes exactly this
        ``ErrorData`` for every still-pending in-flight request — this is how
        a dead stdio subprocess actually surfaces to an in-flight
        ``call_tool``/``read_resource``/etc. call. **Live-verified**: killing
        the echo server's subprocess mid-call (the ``die`` tool) raised
        ``MCPError('MCP tools/call error: Connection closed')`` whose
        ``__cause__`` was ``McpError(error=ErrorData(code=-32000,
        message='Connection closed', ...))`` — exactly this branch.
      - ``RuntimeError("Server session was closed unexpectedly")`` — fastmcp's
        ``Client._context_manager`` (client.py) wraps a
        ``anyio.ClosedResourceError`` leaking out of the session's context
        scope in this EXACT message. Not observed directly in the stdio-die
        probe above (that death surfaced via the McpError branch instead),
        but included defensively per fastmcp's own source — a different
        failure timing (e.g. mid-``initialize``, or the read stream closing
        while the caller is inside the ``async with`` scope rather than
        mid-request) could route through this wrapper instead.
      - Raw ``anyio.ClosedResourceError`` / ``anyio.BrokenResourceError`` /
        ``ConnectionError`` — defensive: these are anyio's/stdlib's own
        dead-stream / dead-socket signal types; not observed leaking
        unwrapped to this call site in the probes above, but a conservative
        predicate treats them as transport-death if they ever do.

    Anything else — including OTHER ``McpError`` codes (**live-verified**:
    calling an unknown resource URI raised ``McpError(error=ErrorData(
    code=-32002, message="Resource not found: ..."))``; ``METHOD_NOT_FOUND``/
    ``INVALID_PARAMS`` are the same "server responded, it's just an app-level
    error" shape), a tool-level failure (a tool raising inside its handler
    comes back as a normal ``CallToolResult`` with ``isError: True`` — never
    an exception at all, so it never reaches this predicate), or any other
    exception type — is NOT transport death: the server is alive and
    responded, just with an error. Default is False (= NOT transport) so an
    unrecognized exception propagates as a plain :class:`MCPError` rather
    than triggering an unnecessary reconnect.
    """
    import anyio

    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError)):
        return True
    if isinstance(exc, RuntimeError) and str(exc) == "Server session was closed unexpectedly":
        return True
    try:
        from mcp.shared.exceptions import McpError as _SdkMcpError
        from mcp.types import CONNECTION_CLOSED
    except ImportError:  # pragma: no cover — mcp SDK always installed alongside fastmcp
        return False
    if isinstance(exc, _SdkMcpError):
        error = getattr(exc, "error", None)
        return getattr(error, "code", None) == CONNECTION_CLOSED
    return False


def _classify_and_raise(exc: Exception, message: str) -> NoReturn:
    """Raise :class:`MCPTransportError` if ``exc`` is genuine transport-death
    (see :func:`_is_transport_death`), else plain :class:`MCPError` — either
    way with ``exc`` preserved as ``__cause__``. Shared by every SDK-call
    boundary below (``call_tool``/``list_tools``/``read_resource``/
    ``list_resources``/``list_resource_templates``) so the classification
    logic lives in exactly one place."""
    if _is_transport_death(exc):
        raise MCPTransportError(message) from exc
    raise MCPError(message) from exc


# ── Client ───────────────────────────────────────────────────────────────────

class MCPClient:
    """Thin async wrapper around ``fastmcp.Client``.

    Construct with the *raw* server config dict from ``reyn.yaml`` (the
    caller is responsible for env-var expansion via :func:`expand_env`).

    Lifecycle::

        client = MCPClient(cfg)
        await client.initialize()
        result = await client.call_tool("read_file", {"path": "x"})
        await client.close()
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        agent_id: str | None = None,
        message_handler: Any = None,
        server_name: str | None = None,
    ) -> None:
        if not isinstance(config, dict):
            raise ValueError(f"MCP server config must be a dict, got {type(config).__name__}")
        srv_type = config.get("type")
        if srv_type not in _SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported MCP server type: {srv_type!r}. "
                f"Expected one of {sorted(_SUPPORTED_TYPES)}."
            )
        self._config: dict[str, Any] = dict(config)
        self._type: str = srv_type
        # FP-0016 Component E: agent_id is injected as the
        # ``X-Reyn-Agent-Id`` header on every outgoing HTTP request so
        # downstream MCP servers can attribute calls to a specific Reyn
        # agent. None preserves prior behaviour for direct callers (= the
        # session factory passes ReynConfig.agent.id; tests can omit).
        self._agent_id: str | None = agent_id
        # #2597 capability/version gate: the server name this client connects to, for
        # error messages only (this object never uses it to look itself up — callers
        # that construct MCPClient directly, e.g. tests, may omit it; the fail-fast
        # message then falls back to "<unknown>"). Threaded in by MCPClientPool /
        # MCPConnectionService, both of which already know the server name at
        # construction time.
        self._server_name: str | None = server_name
        # #2597 S2b: optional async server->client notifications bridge — a
        # ReynMCPMessageHandler (fastmcp.client.tasks.TaskNotificationHandler subclass;
        # see reyn.mcp.message_handler) that receives tools/prompts list_changed +
        # progress notifications on this client's held connection and emits them onto
        # reyn's EventLog. None (default) preserves pre-S2b behaviour — no bridge, no
        # behaviour change for callers that don't pass one (e.g. the ephemeral
        # per-call MCPClientPool path never installs a handler).
        self._message_handler: Any = message_handler
        self._client: Any = None  # fastmcp.Client when initialized
        self._initialized = False
        # Captures subprocess stderr for stdio transport so initialize
        # failures (e.g. self-made MCP server exits immediately, writes
        # a traceback to stderr before the MCP handshake completes) can
        # surface the actual error text rather than the opaque "Connection
        # close" wording the SDK produces. FastMCP's ``StdioTransport``
        # takes a ``log_file`` (Path | TextIO) for subprocess stderr —
        # ``io.StringIO`` doesn't work (needs a real fileno for the
        # underlying anyio subprocess), but ``tempfile.TemporaryFile``
        # does. Lazily created in ``_open_stdio``; closed in ``close``.
        self._stderr_capture: Any = None  # tempfile.TemporaryFile | None
        # #1344: path of the temp Seatbelt profile (.sb) used to sandbox a
        # stdio MCP server's subprocess, if one was generated in _open_stdio.
        # Unlinked in close(). None for non-stdio / non-seatbelt / unsandboxed.
        self._sandbox_profile_path: str | None = None
        # #2597 capability/version gate: captured in initialize() right after
        # ``client.__aenter__()`` completes FastMCP's initialize handshake (verified
        # against fastmcp 3.4.2: ``fastmcp.Client.initialize_result`` is populated at
        # that point — see client.py module docstring's fact-check). None until then
        # (or if the server's InitializeResult was unavailable — handled defensively,
        # never raises).
        self._negotiated_version: str | None = None
        self._server_capabilities: Any = None  # mcp.types.ServerCapabilities | None

    @property
    def server_name(self) -> str | None:
        """The configured name of the server this client connects to, or None if
        the caller didn't supply one at construction. Used only for error-message
        context (:func:`require_capability`) — never for lookup."""
        return self._server_name

    @property
    def negotiated_version(self) -> str | None:
        """The MCP protocol version negotiated at connect (e.g. ``"2025-11-25"``),
        or None before :meth:`initialize` runs (or if the server's
        ``InitializeResult`` was unavailable). Read-only — later slices branch on
        this to apply version-specific behaviour; this slice only exposes it."""
        return self._negotiated_version

    def supports(self, capability: str) -> bool:
        """Return True iff the connected server advertised ``capability`` in its
        initialize handshake. ``capability`` must be one of ``"tools"``,
        ``"resources"``, ``"prompts"``, ``"logging"``, ``"completions"``.

        Conservative False before :meth:`initialize` runs (or if the server's
        capabilities were unavailable) — an un-negotiated connection advertises
        nothing rather than everything.
        """
        if capability not in _CAPABILITY_NAMES:
            raise ValueError(
                f"Unknown MCP capability: {capability!r}. "
                f"Expected one of {sorted(_CAPABILITY_NAMES)}."
            )
        if self._server_capabilities is None:
            return False
        return getattr(self._server_capabilities, capability, None) is not None

    def advertised_capabilities(self) -> list[str]:
        """Sorted list of capability names the connected server advertised (subset
        of the five :meth:`supports` recognizes). Empty before :meth:`initialize`
        runs. Used for observability (the ``mcp_initialized`` event) — see
        :mod:`reyn.mcp.connection_service`."""
        return sorted(name for name in _CAPABILITY_NAMES if self.supports(name))

    @property
    def stderr_capture(self) -> "Any":
        """Read-only accessor for the stderr-capture tempfile (or None).

        Tests inspect this to verify the capture lifecycle (= None
        initially, populated after ``_open_stdio``, None again after
        ``close_stderr_capture``). The write side stays internal so the
        lifecycle stays visible at the call sites that own it.
        """
        return self._stderr_capture

    # ── public API ──────────────────────────────────────────────────────────

    def is_initialized(self) -> bool:
        """Return True if the MCP session is currently open.

        Read-only query used by tests to assert lifecycle state without
        accessing private attributes directly.
        """
        return self._initialized

    async def initialize(self) -> None:
        """Open the transport and complete the MCP handshake.

        Idempotent: a second call is a no-op.
        """
        if self._initialized:
            return
        try:
            from fastmcp import Client as FastMCPClient
        except ImportError as exc:
            raise MCPError(
                "The 'fastmcp' package is required for MCP support. "
                "Install with: pip install reyn[mcp]"
            ) from exc

        try:
            transport = self._open_transport()
            client_kwargs: dict[str, Any] = {}
            if self._type in ("http", "sse"):
                # Client-level default read timeout — see _open_http docstring:
                # the pre-swap connect-level ``timeout=`` kwarg on
                # ``streamablehttp_client`` maps to FastMCP's Client-level
                # default ``read_timeout_seconds`` (same knob call_tool's
                # per-call ``timeout_seconds`` overrides).
                client_kwargs["timeout"] = self._config.get("timeout", 30)
            # #2597 S2b: install the notifications bridge, if one was supplied. Passed
            # as a constructor kwarg per FastMCP's own contract (Client(transport,
            # message_handler=...)); ReynMCPMessageHandler's weakref binding to THIS
            # client is completed via bind_client() right below — see
            # reyn/mcp/message_handler.py's module docstring ("two-phase client
            # binding") for why that two-step is necessary.
            if self._message_handler is not None:
                client_kwargs["message_handler"] = self._message_handler
            client = FastMCPClient(transport, **client_kwargs)
            if self._message_handler is not None:
                self._message_handler.bind_client(client)
            await client.__aenter__()
        except MCPError:
            self.close_stderr_capture()
            raise
        except Exception as exc:
            tail = self.read_stderr_tail()
            self.close_stderr_capture()
            # #1344/#1339-D migration hint: a sandboxed stdio server defaults to
            # the single-source network posture (DEFAULT_SANDBOX_NETWORK); the
            # operator isolates a server with `network: false`. If a server was
            # isolated and fails init for a network reason, point the operator at
            # the knob rather than leave an opaque error.
            from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

            hint = ""
            if self._type == "stdio" and not self._config.get(
                "network", DEFAULT_SANDBOX_NETWORK
            ):
                hint = (
                    "\nHint (#1344): this MCP server is sandboxed with network "
                    "DISABLED (`network: false` in its config). If it needs "
                    "network access, set `network: true` (or remove the override)."
                )
            if tail:
                raise MCPError(
                    f"MCP initialize failed: {exc}\n"
                    f"--- subprocess stderr (tail) ---\n{tail}{hint}"
                ) from exc
            raise MCPError(f"MCP initialize failed: {exc}{hint}") from exc

        self._client = client
        self._initialized = True
        # #2597 capability/version gate: read the negotiated version + capabilities
        # right after the handshake completes. ``initialize_result`` is populated by
        # ``client.__aenter__()`` above (fastmcp 3.4.2: ``Client.initialize_result``
        # property backed by ``_session_state.initialize_result``, set inside
        # ``Client.initialize()`` which ``__aenter__`` calls) — but read it
        # defensively: None here would mean FastMCP's own contract changed
        # underneath us, not a reyn bug, so degrade to "unknown" (supports() ->
        # False, negotiated_version -> None) rather than raise.
        init_result = getattr(client, "initialize_result", None)
        if init_result is not None:
            self._negotiated_version = str(init_result.protocolVersion)
            self._server_capabilities = init_result.capabilities
        else:
            self._negotiated_version = None
            self._server_capabilities = None

    async def __aenter__(self) -> "MCPClient":
        """#a359: structured lifecycle. ``initialize()`` here + ``close()`` in ``__aexit__`` run in
        the SAME task/scope — so the transport + session (whose SDK stdio_client / ClientSession hold
        internal anyio task-group scopes that MUST be exited in the task that entered them) open and
        close within one ``async with`` block. Callers use ``async with MCPClient(cfg) as c:`` instead
        of a lazy ``initialize()`` + a deferred ``self._stack`` closed by a later ``close()`` in a
        possibly-different task — that deferral was the root cause of the cross-task 'cancel scope
        crossed task boundary' error (Windows: BrokenResource / BaseExceptionGroup during subprocess
        teardown)."""
        await self.initialize()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        progress_callback: Any = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Call ``name`` on the server with ``args``. Returns a dict
        shaped to match what ``op_runtime/mcp.py`` consumes:
        ``{"content": [...], "isError": bool, "structuredContent": ... | None}``.

        Optional kwargs (issue #264 — wire SDK long-running support):

          - ``progress_callback``: async ``(progress: float, total: float | None,
            message: str | None) -> None`` that the MCP SDK invokes when the
            server emits a ``notifications/progress`` for this call. Default
            ``None`` matches pre-#264 behaviour (= no progress visibility).
            Forwarded to FastMCP's ``call_tool_mcp(progress_handler=...)``,
            which passes it straight through to
            ``mcp.ClientSession.call_tool(progress_callback=...)`` — same SDK
            parameter, same signature.
          - ``timeout_seconds``: float; if set, converts to ``timedelta`` and
            passes as ``read_timeout_seconds`` to the SDK so the call fails
            fast on a stuck server. Default ``None`` keeps the SDK's own
            transport-level default.
        """
        await self.initialize()
        # #2597 capability/version gate: fail fast with a clear reyn error if the
        # server never advertised "tools" rather than let the request reach the
        # server and bounce back as a confusing raw protocol error.
        require_capability(self, "tools")
        kwargs: dict[str, Any] = {}
        if progress_callback is not None:
            kwargs["progress_handler"] = progress_callback
        if timeout_seconds is not None:
            from datetime import timedelta
            kwargs["timeout"] = timedelta(seconds=timeout_seconds)
        try:
            # call_tool_mcp (not FastMCP's raise_on_error-by-default call_tool)
            # returns the RAW mcp.types.CallToolResult unchanged — same object
            # shape _result_to_dict already flattens, so op_runtime/mcp.py's
            # consumed shape stays byte-identical.
            result = await self._client.call_tool_mcp(name, args or {}, **kwargs)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP tools/call error: {exc}")
        return _result_to_dict(result)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the tools advertised by this server as plain dicts.

        Uses FastMCP's auto-paginating ``Client.list_tools()`` (follows
        ``nextCursor`` up to a 250-page guard) instead of a single page-1
        request — #2597 S1 free win: servers with >1 page of tools no
        longer silently truncate.
        """
        await self.initialize()
        # #2597 capability/version gate: same seam as call_tool — see there.
        require_capability(self, "tools")
        try:
            tools = await self._client.list_tools()
        except Exception as exc:
            _classify_and_raise(exc, f"MCP tools/list error: {exc}")
        return [_tool_to_dict(t) for t in tools]

    # ── resources (#2597 slice ②a — consumption; ②b adds subscribe below) ──────

    async def list_resources(self) -> list[dict[str, Any]]:
        """Return the resources advertised by this server as plain dicts.

        Mirrors :meth:`list_tools`: uses FastMCP's auto-paginating
        ``Client.list_resources()`` (follows ``nextCursor``) and gates on the
        ``"resources"`` capability before issuing the request.
        """
        await self.initialize()
        require_capability(self, "resources")
        try:
            resources = await self._client.list_resources()
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/list error: {exc}")
        return [_resource_to_dict(r) for r in resources]

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        """Return the resource templates advertised by this server as plain
        dicts. Mirrors :meth:`list_resources`; empty list is a normal
        (not an error) result for a server that registers no templates."""
        await self.initialize()
        require_capability(self, "resources")
        try:
            templates = await self._client.list_resource_templates()
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/templates/list error: {exc}")
        return [_resource_to_dict(t) for t in templates]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one resource (or a resolved resource-template URI) and return
        its contents flattened to a dict: ``{"contents": [...]}`` — each
        entry a flattened ``TextResourceContents``/``BlobResourceContents``.

        Uses FastMCP's raw ``read_resource_mcp`` (not the convenience
        ``read_resource``, which strips the ``ReadResourceResult`` envelope
        down to just ``.contents``) so the shape-flattening lives in ONE
        place (:func:`_read_resource_result_to_dict`), mirroring how
        :meth:`call_tool` uses ``call_tool_mcp`` for the same reason.
        """
        await self.initialize()
        require_capability(self, "resources")
        try:
            result = await self._client.read_resource_mcp(uri)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/read error: {exc}")
        return _read_resource_result_to_dict(result)

    # ── resource subscriptions (#2597 slice ②b) ─────────────────────────────────

    def _require_resources_subscribe_capability(self) -> None:
        """Fail fast with :class:`MCPCapabilityError` if the connected server
        does not advertise the ``resources.subscribe`` sub-capability.

        Verified against the installed mcp SDK 3.4.2's ``ServerCapabilities``:
        ``resources: ResourcesCapability | None`` where ``ResourcesCapability``
        carries its OWN ``subscribe: bool | None`` field, independent of whether
        the server advertises ``resources`` at all (a server may support reading
        resources but not subscribing to their updates — the base SDK's
        ``mcp.server.lowlevel.server.Server.get_capabilities`` in fact hard-codes
        ``subscribe=False`` for every server that doesn't explicitly override it,
        including every server built with FastMCP's high-level ``FastMCP()``
        class — see ``tests/_support/mcp_subscribable_resources_server.py``'s
        module docstring for the full fact-check). This is a REFUSAL, the same
        shape as :func:`require_capability` — not a transport failure.
        """
        server = self.server_name or "<unknown>"
        version = self.negotiated_version or "<unknown>"
        resources_cap = getattr(self._server_capabilities, "resources", None)
        if resources_cap is None or not getattr(resources_cap, "subscribe", False):
            raise MCPCapabilityError(
                f"MCP server {server!r} does not advertise the resources.subscribe "
                f"sub-capability (negotiated protocol version {version}). Refusing "
                f"to subscribe to a resource on it."
            )

    async def subscribe_resource(self, uri: str) -> None:
        """Subscribe to server-pushed ``notifications/resources/updated`` for
        ``uri``. Gated on BOTH the ``resources`` capability (via
        :func:`require_capability`, same as :meth:`read_resource`) AND the
        resources ``subscribe`` sub-capability (via
        :meth:`_require_resources_subscribe_capability`) — a server may support
        reading resources without supporting subscriptions to them.

        Uses the RAW ``mcp.ClientSession.subscribe_resource`` (verified: FastMCP's
        ``Client`` has no subscribe convenience method of its own — only the
        underlying ``mcp.ClientSession``, reached via ``Client.session``, does).
        The notification itself carries no payload (just ``uri``) — callers
        re-read the resource to see the new content; see
        :mod:`reyn.mcp.message_handler`'s ``on_resource_updated`` for the
        EventLog bridge.
        """
        await self.initialize()
        require_capability(self, "resources")
        self._require_resources_subscribe_capability()
        try:
            await self._client.session.subscribe_resource(uri)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/subscribe error: {exc}")

    async def unsubscribe_resource(self, uri: str) -> None:
        """Unsubscribe from server-pushed updates for ``uri``. Same gating as
        :meth:`subscribe_resource`; mirrors it via the raw
        ``mcp.ClientSession.unsubscribe_resource``."""
        await self.initialize()
        require_capability(self, "resources")
        self._require_resources_subscribe_capability()
        try:
            await self._client.session.unsubscribe_resource(uri)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/unsubscribe error: {exc}")

    async def close(self) -> None:
        """Tear down the transport and session. Safe to call repeatedly."""
        if self._client is None:
            self.close_stderr_capture()
            return
        client = self._client
        self._client = None
        self._initialized = False
        # #2597 capability/version gate: a closed client re-negotiates on the next
        # initialize() (or duck-typed callers who happen to keep querying supports()
        # on a closed client should see the conservative False, not stale state from
        # the old connection).
        self._negotiated_version = None
        self._server_capabilities = None
        try:
            await client.close()
        except Exception:
            # Best-effort cleanup; transport may already be down.
            pass
        self.close_stderr_capture()

    # ── stderr capture (stdio only) ─────────────────────────────────────────

    STDERR_TAIL_BYTES = 2048

    def read_stderr_tail(self) -> str:
        """Return the tail of the subprocess stderr capture, or ''.

        Reads up to ``STDERR_TAIL_BYTES`` from the end of the temp
        file. Returns empty string when no capture is configured (= http
        transport, or stdio capture failed to open) or read raises.
        Failures here are advisory: never propagate beyond the helper
        so the caller's MCPError carries the original exception even
        if the tail can't be retrieved.
        """
        capture = self._stderr_capture
        if capture is None:
            return ""
        try:
            capture.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            capture.seek(0)
            data = capture.read()
        except Exception:  # noqa: BLE001
            return ""
        if not data:
            return ""
        if isinstance(data, bytes):
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        else:
            text = data
        if len(text) > self.STDERR_TAIL_BYTES:
            return "...(truncated)\n" + text[-self.STDERR_TAIL_BYTES :]
        return text

    def close_stderr_capture(self) -> None:
        """Close + delete the stderr temp file + the #1344 Seatbelt profile, if
        any. Idempotent — called at every teardown path."""
        # #1344: unlink the temp Seatbelt profile (.sb) generated for a
        # sandboxed stdio MCP server. Best-effort; a leaked temp file must not
        # break teardown.
        profile_path = self._sandbox_profile_path
        if profile_path is not None:
            self._sandbox_profile_path = None
            try:
                os.unlink(profile_path)
            except OSError:
                pass
        capture = self._stderr_capture
        if capture is None:
            return
        self._stderr_capture = None
        try:
            capture.close()
        except Exception:  # noqa: BLE001
            pass

    # ── transport dispatch ──────────────────────────────────────────────────

    def _open_transport(self) -> "Any":
        """Build the ``fastmcp.client.transports.ClientTransport`` for this server.

        Unlike the pre-swap ``mcp`` SDK version, this returns a constructed
        transport OBJECT (not an entered async context manager / stream
        tuple) — FastMCP's ``Client(transport)`` owns opening it.
        """
        if self._type == "stdio":
            return self._open_stdio()
        if self._type == "http":
            return self._open_http()
        if self._type == "sse":
            return self._open_sse()
        # Unreachable due to __init__ validation, but keep defensive.
        raise ValueError(f"Unsupported MCP server type: {self._type!r}")

    def _build_mcp_sandbox_policy(self):
        """SandboxPolicy for a sandboxed stdio MCP server (#1344).

        read broad (#1323 scoping) + the default sensitive deny-list; write tight
        to the server's working dir; ``network`` is OPERATOR-declared per server
        (``network: false`` in the MCP config to isolate) and defaults to
        :data:`~reyn.security.sandbox.policy.DEFAULT_SANDBOX_NETWORK` (#1339 / sandbox-model
        completion D) — the SAME single-source default as sandboxed_exec, so the
        sandbox network posture is consistent across surfaces. The guarantee is
        operator-ownership (the policy is the operator's, not the LLM's — the LLM
        cannot set it), not default-off; an operator who wants an isolated server
        sets ``network: false`` (see the migration hint surfaced on init failure).
        """
        from reyn.security.sandbox import SandboxPolicy
        from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

        cwd = self._config.get("cwd") or os.getcwd()
        return SandboxPolicy(
            network=bool(self._config.get("network", DEFAULT_SANDBOX_NETWORK)),
            write_paths=[cwd],
        )

    def _sandbox_wrap_stdio(self, command: str, args: list[str]) -> "tuple[str, list[str]]":
        """Wrap ``(command, args)`` so the MCP server subprocess runs sandboxed (#1344).

        Seatbelt (macOS): returns ``("sandbox-exec", ["-f", <profile>, command,
        *args])`` with a generated SBPL profile (a temp ``.sb`` unlinked in
        ``close``). Landlock (Linux, #1344 follow-up E): returns the
        ``reyn.security.sandbox.landlock_exec`` re-exec shim argv (the COMMAND-level
        analog — Landlock has no CLI wrapper). MCP stdio is persistent, so the
        wrap is at the COMMAND level (the backend's one-shot ``run()`` does not
        fit). Other backends (docker) are not yet wrapped here — the server then
        runs UNSANDBOXED with a warning (never silently).
        """
        from reyn.security.sandbox import get_default_backend

        try:
            backend = get_default_backend()
            name = getattr(backend, "name", None)
            available = backend.available()
        except Exception:  # noqa: BLE001 — a backend probe must not block a launch
            name, available = None, False
        if name == "seatbelt" and available:
            from reyn.security.sandbox.backends.seatbelt import _build_sbpl_profile

            profile = _build_sbpl_profile(self._build_mcp_sandbox_policy())
            fh = tempfile.NamedTemporaryFile(
                suffix=".sb", mode="w", delete=False, encoding="utf-8",
            )
            fh.write(profile)
            fh.close()
            self._sandbox_profile_path = fh.name
            return "sandbox-exec", ["-f", fh.name, command, *args]
        if name == "landlock" and available:
            # #1344 follow-up E: the Landlock re-exec shim restricts itself then
            # execs the target (Linux-validation-pending — see landlock_exec).
            from reyn.security.sandbox.landlock_exec import build_landlock_exec_argv

            return build_landlock_exec_argv(
                self._build_mcp_sandbox_policy(), command, args
            )
        warnings.warn(
            f"MCP stdio server {command!r} runs UNSANDBOXED "
            f"(sandbox backend={name or 'none'}); only Seatbelt + Landlock wraps "
            f"are implemented (#1344) — docker wrapping is a follow-up.",
            stacklevel=2,
        )
        return command, args

    def _open_stdio(self) -> "Any":
        from fastmcp.client.transports import StdioTransport

        command = self._config.get("command")
        if not command:
            raise MCPError("stdio MCP server config requires 'command'")
        args = list(self._config.get("args") or [])
        # #1344: wrap the server subprocess in the platform sandbox (Seatbelt)
        # so an LLM-invoked MCP tool cannot escape the sandbox via the server.
        command, args = self._sandbox_wrap_stdio(command, args)
        env = self._config.get("env")
        # Subprocess stderr capture for diagnostic readback on init
        # failure. FastMCP's ``StdioTransport`` accepts ``log_file`` (a
        # Path or TextIO) and passes it straight through to the underlying
        # ``anyio.open_process(stderr=...)``, which requires a real
        # fileno — ``io.StringIO`` doesn't work. ``tempfile.TemporaryFile``
        # auto-deletes on close. Text-mode + utf-8 matches the SDK's
        # default (= sys.stderr). On failure to open the temp file we
        # fall through to no capture (= behavior degrades gracefully
        # to the pre-fix opaque error wording, never blocks the call).
        try:
            self._stderr_capture = tempfile.TemporaryFile(
                mode="w+t", encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — temp-file failure is non-fatal
            self._stderr_capture = None
            log_file = None
        else:
            log_file = self._stderr_capture
        return StdioTransport(
            command=command,
            args=args,
            env=dict(env) if env else None,
            cwd=self._config.get("cwd"),
            # keep_alive=True matches the pre-swap subprocess-reuse contract:
            # MCPClient/pool open once and hold the same transport/subprocess
            # for the object's lifetime (a359 P2 task-affine caching).
            keep_alive=True,
            log_file=log_file,
        )

    def _open_http(self) -> "Any":
        """Open the Streamable HTTP transport.

        Reads from ``self._config``:
          - ``url`` (required) — full MCP endpoint URL.
          - ``headers`` (optional dict[str, str]) — HTTP headers sent on
            every request to the server. Used for ``Authorization: Bearer
            <token>`` and API-key style auth required by hosted MCP servers
            (GitHub MCP, Atlassian MCP, internal enterprise MCPs).  This is
            FP-0016 Component A. Values are passed through verbatim;
            ``${VAR}`` interpolation is the caller's responsibility (the
            standard load_config path applies ``expand_env`` recursively
            across the whole merged config — see ADR-0030).
          - ``timeout`` (optional, default 30) — request timeout in seconds.
            FastMCP's ``StreamableHttpTransport`` has no per-transport
            connect timeout (its ``sse_read_timeout`` ctor kwarg is
            deprecated/unused by the new streamable-http client); the
            equivalent bound is the ``Client``-level default read timeout
            (``fastmcp.Client(transport, timeout=...)``), which flows into
            every request's ``read_timeout_seconds`` exactly like the
            per-call ``timeout_seconds`` kwarg on :meth:`call_tool` — same
            SDK knob, applied as this transport's default instead of the
            old connect-level timeout.
        """
        from fastmcp.client.transports import StreamableHttpTransport

        url = self._config.get("url")
        if not url:
            raise MCPError("http MCP server config requires 'url'")
        headers = {
            str(k): str(v) for k, v in (self._config.get("headers") or {}).items()
        }
        # FP-0016 Component E: inject the agent_id as X-Reyn-Agent-Id so
        # downstream MCP servers can attribute requests to a specific
        # Reyn agent (= RBAC + audit trail requirement per the issue
        # シナリオ 5 Enterprise Agent ID pattern). Explicit operator
        # headers win when they already set the field — operators may
        # need to spoof in tests or proxy in production.
        if self._agent_id and "X-Reyn-Agent-Id" not in headers:
            headers["X-Reyn-Agent-Id"] = self._agent_id
        return StreamableHttpTransport(url, headers=headers)

    def _open_sse(self) -> "Any":
        """Open the SSE transport (#2597 S1 free win — FastMCP ships it, so no
        incremental cost to wire vs. leaving the pre-swap ``NotImplementedError``)."""
        from fastmcp.client.transports import SSETransport

        url = self._config.get("url")
        if not url:
            raise MCPError("sse MCP server config requires 'url'")
        headers = {
            str(k): str(v) for k, v in (self._config.get("headers") or {}).items()
        }
        if self._agent_id and "X-Reyn-Agent-Id" not in headers:
            headers["X-Reyn-Agent-Id"] = self._agent_id
        return SSETransport(url, headers=headers)


# ── helpers ──────────────────────────────────────────────────────────────────

def _result_to_dict(result: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.CallToolResult`` into the shape
    ``op_runtime/mcp.py`` expects (mirrors the JSON-RPC ``result`` field of
    the previous hand-rolled client)."""
    content_items = []
    for item in getattr(result, "content", []) or []:
        # Each item is a TextContent / ImageContent / etc. pydantic model.
        if hasattr(item, "model_dump"):
            content_items.append(item.model_dump())
        elif isinstance(item, dict):
            content_items.append(item)
        else:
            content_items.append({"type": "text", "text": str(item)})
    return {
        "content": content_items,
        "isError": bool(getattr(result, "isError", False)),
        "structuredContent": getattr(result, "structuredContent", None),
    }


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        return tool.model_dump()
    return dict(tool)


def _resource_to_dict(resource: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.Resource`` or ``mcp.types.ResourceTemplate`` into a
    JSON-safe plain dict (mirrors :func:`_tool_to_dict`).

    ``mode="json"`` (not plain ``model_dump()``) — unlike ``Tool``, ``Resource``/
    ``ResourceTemplate`` carry a ``uri: AnyUrl`` field; a plain ``model_dump()``
    leaves that as a live ``pydantic.AnyUrl`` object, which downstream JSON
    encoding (events / tool-result serialization) cannot handle without a
    ``default=str`` escape hatch. ``mode="json"`` serializes it to ``str`` at
    the source instead.
    """
    if hasattr(resource, "model_dump"):
        return resource.model_dump(mode="json")
    return dict(resource)


def _read_resource_result_to_dict(result: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.ReadResourceResult`` into
    ``{"contents": [...]}`` — each entry a flattened
    ``TextResourceContents``/``BlobResourceContents`` (mirrors
    :func:`_result_to_dict`'s content-flattening for tool calls). Uses
    ``mode="json"`` for the same AnyUrl-safety reason as :func:`_resource_to_dict`."""
    contents: list[dict[str, Any]] = []
    for item in getattr(result, "contents", []) or []:
        if hasattr(item, "model_dump"):
            contents.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            contents.append(item)
        else:
            contents.append({"text": str(item)})
    return {"contents": contents}
