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

Elicitation (#2597 slice ③ — server->client ``elicitation/create``):
  an optional ``elicitation_handler`` (constructor kwarg, same shape as
  ``message_handler``) is forwarded verbatim to ``fastmcp.Client(...,
  elicitation_handler=...)`` — passing ANY non-None handler is itself what
  makes FastMCP declare the ``elicitation`` client capability during the
  initialize handshake. See :mod:`reyn.mcp.elicitation` for the handler
  that routes a server's structured question through reyn's consent path
  (:class:`~reyn.mcp.connection_service.MCPConnectionService` builds one per
  held connection); this module only plumbs the constructor kwarg through.

OAuth 2.1 (#2597 slice ④ — the umbrella's LAST slice, hosted MCP servers like
GitHub MCP / Atlassian that require browser-based OAuth rather than a static
bearer token):

  A server config's ``auth`` key selects the auth mode for the ``http``
  transport (``sse``/``stdio`` reject a non-empty ``auth`` — OAuth only makes
  sense over Streamable HTTP). Static bearer/API-key auth is UNCHANGED —
  still expressed via ``headers: {Authorization: "Bearer ..."}`` and carries
  no ``auth`` key at all (the pre-#2597-④ path, still exercised by
  ``test_http_transport_round_trip``). ``auth`` is new and, when present,
  MUST resolve to ``{"type": "oauth", ...}`` (a bare string ``"oauth"`` is
  shorthand for ``{"type": "oauth"}``) — any other ``type`` is a config
  error raised eagerly at transport-open time, matching this module's
  existing lazy-validate-at-connect-time posture (``type``/``url`` are
  validated the same way).

  :meth:`_open_http` builds a ``fastmcp.client.auth.OAuth(mcp_url=url,
  scopes=..., client_id=..., client_secret=..., token_storage=
  MCPOAuthTokenStorage())`` (see :mod:`reyn.mcp.oauth_token_storage` for the
  exact verified FastMCP contract — NOT the ``mcp.client.auth.TokenStorage``
  ABC the umbrella issue originally assumed; FastMCP 3.4.2 instead wants a
  generic ``key_value.aio`` ``AsyncKeyValue`` store) and passes it as
  ``StreamableHttpTransport(url, headers=..., auth=oauth)`` — FastMCP's own
  ``OAuth`` object IS an ``httpx.Auth`` (via ``OAuthClientProvider``), so it
  slots into the same ``auth=`` parameter ``StreamableHttpTransport`` already
  exposed (unused pre-④). FastMCP's ``OAuth`` runs the full Authorization
  Code Grant + PKCE + browser-open + localhost-callback dance internally on
  first use — reyn does NOT reimplement any of that; it only supplies the
  token_storage backend + the pre-flight headless check below.

  Headless graceful failure: before constructing ``OAuth``,
  :meth:`_open_http` checks :func:`~reyn.mcp.oauth_token_storage.
  has_stored_token` for this URL. If no usable token is cached AND this
  client is running non-interactively (``non_interactive`` constructor kwarg,
  or auto-detected via ``sys.stdin.isatty()`` when not explicitly passed —
  mirrors ``reyn.runtime.session``'s own ``non_interactive`` flag's "no user
  to ask" rationale), :meth:`_open_http` raises :class:`MCPError` immediately
  with a clear message rather than let FastMCP open a browser + wait (bounded
  only by ``OAuth``'s own ``callback_timeout``, default 300s) for a callback
  nobody can complete.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import warnings
from collections.abc import Callable
from typing import Any, NoReturn

# ── Env var expansion ─────────────────────────────────────────────────────────
# Shared resolver lives in reyn.security.secrets.interpolation (ADR-0030).
# This re-export keeps the public surface of this module backward-compatible:
# callers that import ``from reyn.mcp.client import expand_env`` continue to
# work without change.
from reyn.security.secrets.interpolation import expand_env as expand_env  # noqa: F401

logger = logging.getLogger(__name__)

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

# #2976: per-runtime DEFAULT write grants, keyed on the basename of the server's
# ``command``. A package-manager launcher bootstraps itself into a per-user cache
# outside the workspace, so a workspace-only write grant denies the very launch
# the sandbox is wrapping (opaque EPERM, server never starts).
#
# THIS MAP IS A CENSUS, AND A CENSUS CANNOT BE COMPLETE. It is a convenience
# default, NEVER the correctness mechanism — that is the operator-declared
# ``write_paths`` key (see _build_mcp_sandbox_policy). Two independent reasons
# this map is wrong-by-construction, both MEASURED, not predicted:
#
#   1. Across runtimes — bun / deno / pip / dnx each have their own locations.
#      Entries here cover only what was measured (npx, uvx); adding a runtime
#      needs NO code change, only a `write_paths` line in the server's config.
#   2. WITHIN a runtime we already list — these paths are the DEFAULTS, and every
#      one of them is relocatable by the user's own environment:
#          XDG_CACHE_HOME=/tmp/xdg  → `uv cache dir`         → /tmp/xdg/uv
#          npm_config_cache=/tmp/x  → `npm config get cache` → /tmp/x
#      An operator who relocates their cache MUST use `write_paths`; this map is
#      simply wrong for them, and no larger map would fix that.
#
# So the failure mode of an incomplete census here is "the operator writes one
# config line", never "the product is broken". Do NOT add unmeasured runtimes to
# make this look complete — an entry that was never run against a real server is
# a guess wearing the costume of a default.
_RUNTIME_DEFAULT_WRITE_PATHS: dict[str, tuple[str, ...]] = {
    # measured: npx bootstraps into the npm cache; ~/.npm alone is sufficient
    # (a writable /tmp is NOT required — verified by running the real server).
    "npx": ("~/.npm",),
    "npm": ("~/.npm",),
    # measured: uv needs BOTH its cache root AND its tool/data root — granting
    # only ~/.cache/uv still fails on ~/.local/share/uv/tools. Two roots, not one.
    "uvx": ("~/.cache/uv", "~/.local/share/uv"),
    "uv": ("~/.cache/uv", "~/.local/share/uv"),
}


def _default_runtime_write_paths(command: str) -> tuple[str, ...]:
    """DEFAULT write grants for *command*'s runtime, or ``()`` if unknown.

    Unknown is a FIRST-CLASS outcome, not a failure: an unrecognised runtime
    gets no guessed grant and, if it needs one, the operator declares it (and
    the init-failure hint names that knob). See _RUNTIME_DEFAULT_WRITE_PATHS.
    """
    return _RUNTIME_DEFAULT_WRITE_PATHS.get(os.path.basename(command).lower(), ())


# #2976: substrings that mark a sandbox write denial in a failed server's stderr.
# Both were OBSERVED in real launches under the real Seatbelt profile, not
# predicted: npm prints ``npm error code EPERM``; uv prints ``Operation not
# permitted (os error 1)``; a Python server prints ``[Errno 1] Operation not
# permitted``. Matching is a diagnostic HINT only — a false positive costs one
# extra sentence in an error that was already failing, so this errs toward
# offering help rather than staying silent.
_WRITE_DENIAL_MARKERS = ("eperm", "operation not permitted")


def _looks_like_write_denial(stderr_tail: str | None) -> bool:
    """Whether *stderr_tail* looks like an OS-level permission denial."""
    if not stderr_tail:
        return False
    lowered = stderr_tail.lower()
    return any(marker in lowered for marker in _WRITE_DENIAL_MARKERS)

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


def _extract_stdio_child_pid(fastmcp_client: "Any") -> int | None:
    """#2714 best-effort: return the OS pid of the stdio subprocess backing
    ``fastmcp_client``, or None if it can't be located.

    fastmcp 3.4.2's ``StdioTransport`` deliberately keeps the spawned subprocess
    handle OFF the transport object (its connect-task holds it in a task-local
    ``AsyncExitStack`` so the task owns no back-reference), and neither fastmcp nor
    the mcp SDK exposes the pid on any public surface. So we walk the connect-task's
    coroutine / async-generator frame chain (through the mcp ``stdio_client``
    generator's ``AsyncExitStack``) to find the ``anyio.abc.Process`` and read its
    ``pid``.

    Every step is defensive: any structural drift from a fastmcp/mcp upgrade returns
    None, and the belt-and-suspenders reap then simply falls back to the async
    graceful teardown — byte-identical to pre-#2714 behaviour. Captured ONCE at
    connect (structure known-good) and only ever used as a terminate target, so a
    stale/None value can never do worse than the pre-fix orphan."""
    try:
        from anyio.abc import Process as _AnyioProcess
    except Exception:  # pragma: no cover — anyio ships with fastmcp
        return None
    transport = getattr(fastmcp_client, "transport", None)
    connect_task = getattr(transport, "_connect_task", None)
    get_coro = getattr(connect_task, "get_coro", None)
    if get_coro is None:
        return None
    try:
        return _walk_frames_for_process_pid(get_coro(), _AnyioProcess)
    except Exception:  # noqa: BLE001 — pid capture is best-effort, never fatal
        return None


def _walk_frames_for_process_pid(root_coro: "Any", process_type: type) -> int | None:
    """Breadth-first walk of a coroutine/async-generator/AsyncExitStack graph rooted
    at ``root_coro``, returning the ``pid`` of the first ``process_type`` instance
    found in any frame's locals. Bounded (``id``-deduped, step-capped) so a cyclic
    or pathological graph can never loop forever. Helper for
    :func:`_extract_stdio_child_pid` — see there for why the walk is necessary."""
    pending: list[Any] = [root_coro]
    seen: set[int] = set()
    steps = 0
    while pending and steps < 500:
        steps += 1
        node = pending.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        # An AsyncExitStack node: descend into the context managers it will exit
        # (the mcp stdio_client generator + the ClientSession live here).
        callbacks = getattr(node, "_exit_callbacks", None)
        if callbacks is not None:
            for cb in callbacks:
                callback = cb[1] if isinstance(cb, tuple) and len(cb) == 2 else None
                target = getattr(callback, "__self__", None)
                gen = getattr(target, "gen", None)  # _AsyncGeneratorContextManager.gen
                if gen is not None:
                    pending.append(gen)
                inner_stack = getattr(target, "_exit_stack", None)  # ClientSession
                if inner_stack is not None:
                    pending.append(inner_stack)
            continue
        frame = getattr(node, "cr_frame", None) or getattr(node, "ag_frame", None)
        if frame is not None:
            for value in frame.f_locals.values():
                if isinstance(value, process_type):
                    pid = getattr(value, "pid", None)
                    if isinstance(pid, int):
                        return pid
                if getattr(value, "_exit_callbacks", None) is not None:
                    pending.append(value)
        awaited = getattr(node, "cr_await", None) or getattr(node, "ag_await", None)
        if awaited is not None:
            pending.append(awaited)
    return None


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
        elicitation_handler: Any = None,
        server_name: str | None = None,
        non_interactive: bool | None = None,
    ) -> None:
        if not isinstance(config, dict):
            raise ValueError(f"MCP server config must be a dict, got {type(config).__name__}")
        srv_type = config.get("type")
        if srv_type not in _SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported MCP server type: {srv_type!r}. "
                f"Expected one of {sorted(_SUPPORTED_TYPES)}."
            )
        # #2597 slice ④: 'auth' (OAuth) only makes sense over Streamable HTTP —
        # reject it eagerly at construction time for stdio/sse rather than
        # silently ignoring it (only _open_http ever reads 'auth').
        if config.get("auth") and srv_type != "http":
            raise ValueError(
                f"MCP server 'auth' config is only supported for 'http' "
                f"(Streamable HTTP) servers, not {srv_type!r}."
            )
        # #2976: same eager-rejection model as 'auth' above — 'write_paths' is a
        # sandbox grant for a spawned subprocess, so only 'stdio' has one. A
        # silently-ignored security field on an http/sse server would read as an
        # applied restriction that was never applied.
        write_paths = config.get("write_paths")
        if write_paths is not None:
            if srv_type != "stdio":
                raise ValueError(
                    f"MCP server 'write_paths' is only supported for 'stdio' "
                    f"servers (it scopes the sandboxed subprocess), not {srv_type!r}."
                )
            if not isinstance(write_paths, list) or not all(
                isinstance(p, str) for p in write_paths
            ):
                raise ValueError(
                    "MCP server 'write_paths' must be a list of strings, got "
                    f"{write_paths!r}."
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
        # #2597 slice ③: optional FastMCP ``ElicitationHandler`` (see
        # ``reyn.mcp.elicitation.build_elicitation_handler``) — routes a
        # server->client ``elicitation/create`` request through reyn's
        # consent path. Passing ANY non-None handler to ``fastmcp.Client``
        # is itself what causes FastMCP to declare the ``elicitation`` client
        # capability during the initialize handshake (D6 — held connections
        # always install one; the ephemeral per-call ``MCPClientPool`` path
        # never does, same None-default no-op pattern as ``message_handler``).
        self._elicitation_handler: Any = elicitation_handler
        # #2597 slice ④: explicit override for the headless-OAuth pre-flight check
        # in _open_http (see module docstring's "Headless graceful failure").
        # None (default) means auto-detect via sys.stdin.isatty() at the point
        # _open_http actually needs the answer — see _is_non_interactive().
        self._non_interactive_override: bool | None = non_interactive
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
        # #1344 / #2620: cleanup callable for whatever resource the sandbox
        # backend's ``wrap_command()`` allocated for a stdio MCP server's
        # subprocess wrap (e.g. Seatbelt's temp ``.sb`` profile file), if any.
        # Invoked in close_stderr_capture(). None when the backend's wrap owns
        # no such resource (Noop / Landlock).
        self._sandbox_cleanup: Callable[[], None] | None = None
        # #2597 capability/version gate: captured in initialize() right after
        # ``client.__aenter__()`` completes FastMCP's initialize handshake (verified
        # against fastmcp 3.4.2: ``fastmcp.Client.initialize_result`` is populated at
        # that point — see client.py module docstring's fact-check). None until then
        # (or if the server's InitializeResult was unavailable — handled defensively,
        # never raises).
        self._negotiated_version: str | None = None
        self._server_capabilities: Any = None  # mcp.types.ServerCapabilities | None
        # #2714 belt-and-suspenders: the OS pid of the stdio subprocess this client
        # spawned (stdio transport only; None for http/sse and until initialize()
        # succeeds). Captured best-effort right after the connect handshake and used
        # as the explicit-terminate target in ``_reap_child_process`` so a normal-exit
        # teardown that is cut short by a swallowed Windows teardown fault (or a loop
        # torn down before the async graceful close drains) still reaps the child
        # rather than orphaning it in Task Manager. See ``_extract_stdio_child_pid``.
        self._child_pid: int | None = None

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
                "It is a core reyn dependency, so this usually means a broken "
                "install — reinstall reyn (e.g. pip install -e .)."
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
            # #2597 slice ③: same constructor-kwarg contract as message_handler —
            # FastMCP's ``Client(transport, elicitation_handler=...)``.
            if self._elicitation_handler is not None:
                client_kwargs["elicitation_handler"] = self._elicitation_handler
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
            # #2976: same shape as the network hint — a sandbox write denial is
            # the one failure the operator can always fix, but ONLY if the error
            # names the knob. This is what makes the per-runtime default map
            # (_RUNTIME_DEFAULT_WRITE_PATHS) safe to leave incomplete: an unknown
            # runtime, or a relocated cache (XDG_CACHE_HOME / npm_config_cache),
            # surfaces as "add this path" rather than an opaque EPERM.
            if self._type == "stdio" and _looks_like_write_denial(tail):
                hint += (
                    "\nHint (#2976): the sandbox DENIED a write to a path outside "
                    "this server's granted write scope (the stderr below names "
                    "the exact path). A launcher that bootstraps into a per-user "
                    "cache needs that cache granted. Add the path to this "
                    "server's `write_paths` in its MCP config, e.g.\n"
                    "    write_paths: [\"~/.npm\"]\n"
                    "Declaring `write_paths` replaces the built-in per-runtime "
                    "defaults; the server's working directory is always granted."
                )
            if tail:
                # #2976: the hint goes BEFORE the stderr dump, not after it. The
                # message is later summarised by pool.describe_fault(limit=600),
                # which truncates from the END — a trailing hint is therefore the
                # FIRST thing dropped, and precisely on the verbose failures that
                # need it most (npm's cache EPERM alone exceeds the limit, which
                # is how this was found: the hint reached uvx's short error and
                # was silently cut from npx's long one). The actionable knob
                # outranks the tail of a log the operator can re-read.
                raise MCPError(
                    f"MCP initialize failed: {exc}{hint}\n"
                    f"--- subprocess stderr (tail) ---\n{tail}"
                ) from exc
            raise MCPError(f"MCP initialize failed: {exc}{hint}") from exc

        self._client = client
        self._initialized = True
        # #2714: capture the stdio subprocess pid now (structure known-good right
        # after the handshake) for the belt-and-suspenders reap in close(). Best-
        # effort and stdio-only — non-stdio transports own no subprocess, and any
        # structural drift in a future fastmcp/mcp upgrade simply yields None (the
        # reap then no-ops, falling back to the async graceful teardown).
        if self._type == "stdio":
            self._child_pid = _extract_stdio_child_pid(client)
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

    # ── prompts (#2597 slice ②c — consumption) ──────────────────────────────────

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Return the prompts advertised by this server as plain dicts.

        Mirrors :meth:`list_resources`: uses FastMCP's auto-paginating
        ``Client.list_prompts()`` (follows ``nextCursor``) and gates on the
        ``"prompts"`` capability before issuing the request.
        """
        await self.initialize()
        require_capability(self, "prompts")
        try:
            prompts = await self._client.list_prompts()
        except Exception as exc:
            _classify_and_raise(exc, f"MCP prompts/list error: {exc}")
        return [_prompt_to_dict(p) for p in prompts]

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch one rendered prompt's messages and return them flattened to a
        dict: ``{"description": str | None, "messages": [...]}`` — each entry
        a flattened ``PromptMessage``.

        Uses FastMCP's raw ``get_prompt_mcp`` (not the convenience
        ``get_prompt``, which additionally supports background-task /
        version kwargs this slice does not need) so the shape-flattening
        lives in ONE place (:func:`_get_prompt_result_to_dict`), mirroring
        how :meth:`read_resource` uses ``read_resource_mcp`` for the same
        reason.
        """
        await self.initialize()
        require_capability(self, "prompts")
        try:
            result = await self._client.get_prompt_mcp(name=name, arguments=arguments)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP prompts/get error: {exc}")
        return _get_prompt_result_to_dict(result)

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
        """Tear down the transport and session. Safe to call repeatedly.

        #2714: the graceful ``client.close()`` (fastmcp → mcp ``stdio_client``'s
        SIGTERM→SIGKILL / Windows Job-Object tree-terminate) is the PRIMARY reaper.
        But that teardown runs inside anyio cancel scopes that, on Windows, can raise
        a ``BrokenResourceError`` / ``BaseExceptionGroup`` mid-teardown (the fault the
        existing seams contain, see connection_service.py / pool.py) — and if that
        fault (or the event loop tearing down before the async teardown drains) cuts
        the terminate short, the stdio subprocess survives (Unix reaps orphans; Windows
        does not). So after the graceful close — whether it succeeds OR raises — a
        ``finally`` explicitly reaps the captured child pid, guaranteeing the OS
        subprocess is terminated rather than trusting that a swallowed fault left it
        dead. On a clean close the child is already gone and the reap is a no-op."""
        if self._client is None:
            self.close_stderr_capture()
            self._reap_child_process()  # nothing opened, or already closed once — still idempotent
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
            # Best-effort graceful cleanup; transport may already be down. The
            # belt-and-suspenders reap in the finally still terminates the OS
            # subprocess even when this graceful path raised (the #2714 guard).
            pass
        finally:
            # #2714: explicit terminate runs on BOTH the success and the fault path
            # (finally, not just after — a BaseExceptionGroup from the anyio teardown
            # would otherwise skip it), so a Windows teardown fault can never leave the
            # child alive.
            self._reap_child_process()
            self.close_stderr_capture()

    def _reap_child_process(self) -> None:
        """#2714 belt-and-suspenders: synchronously terminate the captured stdio child
        subprocess if it is still alive. Idempotent + best-effort — never raises.

        The async graceful teardown normally leaves the child already dead, so the
        common case is ``ProcessLookupError`` (already gone) → a no-op. This exists for
        the path where the graceful teardown did NOT complete (a swallowed Windows
        teardown fault, or a loop torn down before it drained): a plain synchronous
        ``os.kill`` reaps the child without needing a live event loop.

        Scope note (honest bound): this reaps the DIRECT child pid via stdlib
        ``os.kill`` only (psutil is not a dependency), which is exactly the reported
        leak (``python -m <server>`` / an ``execvp``-preserving sandbox wrapper, whose
        direct child IS the server — verified). Full process-TREE termination (a server
        that itself forks grandchildren) stays owned by the graceful path's
        SIGTERM→SIGKILL / Windows Job-Object teardown."""
        pid = self._child_pid
        if pid is None:
            return
        self._child_pid = None
        # SIGKILL is absent on Windows; os.kill(pid, SIGTERM) there maps to
        # TerminateProcess — either way an unconditional, immediate terminate.
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, ChildProcessError):
            return  # already gone — the graceful path reaped it (the common no-op case)
        except OSError as exc:  # e.g. EPERM — never fail teardown on the reap
            logger.warning("MCP subprocess reap (pid=%s) failed: %r", pid, exc)
            return
        # POSIX: the child was spawned in-process (anyio.open_process) so it is OUR
        # child — waitpid it so the freshly SIGKILL'd process doesn't linger as a
        # zombie (itself a leftover process). Blocks only until the just-killed child
        # is reaped (immediate); a concurrent reap by asyncio's child watcher surfaces
        # as ChildProcessError, which is fine. Windows has no zombies and no waitpid
        # for a non-os-spawned pid, so skip it there.
        if os.name == "posix":
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass

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
        """Close + delete the stderr temp file + the #1344/#2620 sandbox wrap's
        cleanup resource (e.g. Seatbelt's temp ``.sb`` profile), if any.
        Idempotent — called at every teardown path."""
        # #1344/#2620: invoke the sandbox backend's wrap_command() cleanup
        # (Seatbelt: unlink the temp .sb profile; Noop/Landlock: no-op).
        # Best-effort; a leaked temp file must not break teardown.
        cleanup = self._sandbox_cleanup
        if cleanup is not None:
            self._sandbox_cleanup = None
            try:
                cleanup()
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

        ``subprocess`` is likewise OPERATOR-declared per server (``subprocess:
        false`` to harden) and defaults to ``True`` (#2820 part C). A stdio MCP
        server is, in the overwhelming common case, launched via a fork-based
        launcher (``npx`` → node, ``uvx`` → the tool, a ``python`` wrapper) — it
        forks to exist. The pre-#2820 default of ``False`` (SandboxPolicy's own
        default, since this builder never set the field) emitted ``(deny
        process-fork)`` and so silently killed the very launch it was wrapping,
        with an opaque ``fork: Operation not permitted`` — the same launcher-fork
        denial class as #2820. ``False`` here hardened nothing (the server never
        started); it only hid the knob behind an unexplained failure. Default
        ``True`` is the honest default per the operator-customizability posture;
        the remaining boundaries (network gate, write scoping, read deny-list)
        still bound the server and its children. An operator who runs a
        genuinely fork-free server sets ``subprocess: false`` to harden it —
        same operator-ownership model as ``network``.

        ``write_paths`` (#2976) is the THIRD field on that same operator-owned
        model, and it exists to close an ASYMMETRY rather than to add a concept:
        ``sandboxed_exec`` already lets an operator declare write targets (via
        ``reyn.yaml sandbox.policy``, which wins over the op's own fields —
        #1326/#1339); a sandboxed stdio MCP server had NO way to express one.
        The grant was hardcoded to ``[cwd]``, so a launcher that bootstraps into
        a per-user cache (``npx`` → ``~/.npm``, ``uvx`` → ``~/.cache/uv`` +
        ``~/.local/share/uv``) was denied and the server never started.

        Resolution order, most-specific first:

        1. the server's own ``write_paths`` (operator KNOWLEDGE) — replaces the
           per-runtime defaults entirely, so an operator can NARROW as well as
           widen (narrowing is a security control: a hardened server may want
           less than the default);
        2. otherwise :data:`_RUNTIME_DEFAULT_WRITE_PATHS` for the runtime — a
           convenience GUESS, honestly a census, never load-bearing;
        3. an unknown runtime gets nothing extra and degrades to ONE config
           line, never to a broken product (the init-failure hint names the
           knob).

        ``cwd`` is always granted: it is the server's own working directory, a
        structural requirement rather than a per-runtime guess, so declaring
        ``write_paths`` narrows the EXTRA grants without silently dropping the
        workspace the caller computed.

        Scoping note (why the defaults stay tight): these grants are per-runtime
        cache/state directories, and a write grant is also a READ re-allow. As
        of #2978 the Seatbelt backend emits ``read_deny_paths`` AFTER the write
        grants (SBPL is last-match-wins), so a broad write grant no longer
        nullifies the sensitive-read deny-list — the deny wins for both read and
        write and a ``sandbox_policy_narrowed`` audit-event is recorded. The
        shipped defaults are nonetheless kept mechanically disjoint from every
        path in ``DEFAULT_SENSITIVE_READ_DENY`` (pinned by a falsification test)
        so an MCP server never trips that narrowing in the first place.
        """
        from reyn.security.sandbox import SandboxPolicy
        from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

        cwd = self._config.get("cwd") or os.getcwd()
        declared = self._config.get("write_paths")
        extra: tuple[str, ...] | list[str]
        if declared is not None:
            extra = [str(p) for p in declared]
        else:
            extra = _default_runtime_write_paths(self._config.get("command") or "")
        return SandboxPolicy(
            network=bool(self._config.get("network", DEFAULT_SANDBOX_NETWORK)),
            allow_subprocess=bool(self._config.get("subprocess", True)),
            # ``~`` in an operator-declared or default path is expanded by the
            # backend (expand_policy_path) — NOT here, so every backend applies
            # one shared contract instead of each caller pre-expanding (#2976).
            write_paths=[cwd, *extra],
        )

    def _sandbox_wrap_stdio(self, command: str, args: list[str]) -> "tuple[str, list[str]]":
        """Wrap ``(command, args)`` so the MCP server subprocess runs sandboxed
        (#1344, uniformly rerouted through the abstraction #2620).

        Routes through ``get_default_backend().wrap_command()`` UNIFORMLY — no
        per-backend-name branching here. Every backend implements
        ``wrap_command`` (Seatbelt: ``sandbox-exec -f <profile>``; Landlock: the
        ``landlock_exec`` re-exec shim; NoopBackend: argv unchanged), so there is
        no agent-reachable code path here that skips the abstraction — a
        NoopBackend passthrough still went THROUGH ``wrap_command``, it just
        enforces nothing (the owner-acceptable no-isolation case). MCP stdio is
        a persistent subprocess, so the wrap is at the COMMAND level (the
        backend's one-shot ``run()`` does not fit).

        A failure while resolving/probing the backend itself (not a normal
        outcome — defensive only) falls back to an unwrapped launch WITH a
        loud warning, so a launch is never silently unsandboxed.
        """
        from reyn.security.sandbox import get_default_backend

        argv = [command, *args]
        try:
            backend = get_default_backend()
            wrapped = backend.wrap_command(argv, self._build_mcp_sandbox_policy())
        except Exception as exc:  # noqa: BLE001 — a backend probe/wrap must not block a launch
            warnings.warn(
                f"MCP stdio server {command!r} runs UNSANDBOXED "
                f"(sandbox backend probe/wrap failed: {exc}).",
                stacklevel=2,
            )
            return command, args

        self._sandbox_cleanup = wrapped.cleanup
        return wrapped.argv[0], list(wrapped.argv[1:])

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

    def _is_non_interactive(self) -> bool:
        """Resolve the effective headless/non-interactive posture for the
        #2597 slice ④ OAuth pre-flight check (see :meth:`_build_oauth_auth`).

        The explicit constructor kwarg wins when given; otherwise auto-detect
        via ``sys.stdin.isatty()`` — no attached TTY means there is no human
        to complete a browser OAuth round-trip. Defensive: any failure while
        probing stdin (closed / non-file-backed stdin, seen in some
        subprocess / CI harnesses) is treated as non-interactive — the
        conservative choice, since raising a clear error beats hanging.
        """
        if self._non_interactive_override is not None:
            return self._non_interactive_override
        try:
            return not sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            return True

    def _build_oauth_auth(self, url: str) -> "Any":
        """Build the ``fastmcp.client.auth.OAuth`` object for ``self._config
        ["auth"]``, or return None if this server config carries no ``auth``
        key at all (the pre-④ static-bearer-via-``headers`` path, unchanged).

        See the module docstring's "OAuth 2.1 (#2597 slice ④)" section for
        the full contract this implements. Raises :class:`MCPError` eagerly
        (this module's existing lazy-validate-at-connect-time posture, same
        as the ``type``/``url`` checks above) for: a non-``oauth`` ``auth``
        type, an ``auth`` key on a non-``http`` transport, or a headless
        caller with no cached token yet.
        """
        auth_cfg = self._config.get("auth")
        if not auth_cfg:
            return None
        if isinstance(auth_cfg, str):
            if auth_cfg != "oauth":
                raise MCPError(
                    f"Unsupported MCP 'auth' shorthand: {auth_cfg!r}. "
                    "The only supported string shorthand is 'oauth'."
                )
            auth_cfg = {"type": "oauth"}
        if not isinstance(auth_cfg, dict):
            raise MCPError(
                "MCP server 'auth' config must be the string 'oauth' or a "
                f"dict, got {type(auth_cfg).__name__}."
            )
        auth_type = auth_cfg.get("type")
        if auth_type != "oauth":
            raise MCPError(
                f"Unsupported MCP 'auth.type': {auth_type!r}. Only 'oauth' is "
                "supported today — static bearer/API-key auth uses the "
                "'headers' key instead (e.g. headers: {Authorization: "
                "'Bearer ${TOKEN}'})."
            )
        # Note: the http-only restriction is already enforced eagerly in
        # __init__ (config.get("auth") + srv_type != "http" raises there) —
        # this method is only ever reached via _open_http, so self._type is
        # guaranteed "http" here.

        from reyn.mcp.oauth_token_storage import (
            MCPOAuthTokenStorage,
            has_stored_token,
        )

        server = self.server_name or url
        if self._is_non_interactive() and not has_stored_token(url):
            raise MCPError(
                f"MCP server {server!r} requires OAuth authentication and no "
                "cached token was found at ~/.reyn/oauth_tokens.json. Run "
                "reyn interactively once against this server to complete the "
                "browser-based OAuth flow — the token is then cached for "
                "subsequent headless/non-interactive runs."
            )

        from fastmcp.client.auth import OAuth

        scopes = auth_cfg.get("scopes")
        return OAuth(
            mcp_url=url,
            scopes=scopes,
            client_id=auth_cfg.get("client_id"),
            client_secret=auth_cfg.get("client_secret"),
            token_storage=MCPOAuthTokenStorage(),
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
          - ``auth`` (optional — #2597 slice ④) — ``"oauth"`` or
            ``{"type": "oauth", "scopes": [...], "client_id": ..., "client_secret":
            ...}``. Mutually additive with ``headers`` (both can be set; a
            server that also needs a static header alongside OAuth is
            supported). See :meth:`_build_oauth_auth`.
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
        auth = self._build_oauth_auth(url)
        return StreamableHttpTransport(url, headers=headers, auth=auth)

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


def _prompt_to_dict(prompt: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.Prompt`` into a JSON-safe plain dict (mirrors
    :func:`_resource_to_dict`). ``mode="json"`` for the same reason: a
    ``Prompt`` has no ``AnyUrl`` field today, but ``mode="json"`` is the
    uniform, future-proof choice across this module's model-dump helpers."""
    if hasattr(prompt, "model_dump"):
        return prompt.model_dump(mode="json")
    return dict(prompt)


def _get_prompt_result_to_dict(result: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.GetPromptResult`` into
    ``{"description": str | None, "messages": [...]}`` — each entry a
    flattened ``PromptMessage`` (mirrors :func:`_read_resource_result_to_dict`'s
    content-flattening for resource reads). Uses ``mode="json"`` for the same
    AnyUrl-safety reason as :func:`_resource_to_dict`."""
    messages: list[dict[str, Any]] = []
    for item in getattr(result, "messages", []) or []:
        if hasattr(item, "model_dump"):
            messages.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            messages.append(item)
        else:
            messages.append({"role": "user", "content": {"type": "text", "text": str(item)}})
    return {
        "description": getattr(result, "description", None),
        "messages": messages,
    }


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
