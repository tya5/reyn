"""
MCP client (v#2597 S1; #3698 stage 1; #4282 stage 2 — fastmcp retired).

Supports two transports today: ``stdio`` and ``http`` (Streamable HTTP);
``sse`` uses the SSE client.

#4282: EVERY transport — ``stdio``, ``http``/``sse`` with or without OAuth
configured — now goes through the official ``mcp`` SDK DIRECTLY
(``mcp.client.session.ClientSession`` + ``mcp.client.{stdio,
streamable_http,sse}``). fastmcp is no longer constructed anywhere in this
module. #3698 stage 1 had already migrated stdio and non-OAuth http/sse;
#4282 closed the remaining OAuth-configured-http gap by building the
official SDK's own ``mcp.client.auth.OAuthClientProvider`` directly
instead of fastmcp's ``OAuth`` wrapper around it — this needed two things
fastmcp provided internally: a browser-redirect + localhost-callback
implementation (now :mod:`reyn.mcp.oauth_browser_flow`, built from
starlette/uvicorn — zero NEW dependency surface, since fastmcp's own
``fastmcp-slim[server]`` requirement already pulls both in unconditionally
regardless of reyn's own optional ``[web]`` extra — verified by reading
``fastmcp``'s/``fastmcp-slim``'s declared requirements directly, not
assumed) and a token-storage adapter matching the official SDK's simpler
``TokenStorage`` protocol (``get_tokens``/``set_tokens``/
``get_client_info``/``set_client_info``, scoped per ``server_url`` — see
:mod:`reyn.mcp.oauth_token_storage`, rewritten from fastmcp's
``AsyncKeyValue``-shaped adapter, which became dead code once fastmcp's
``OAuth`` was no longer constructed anywhere).

#4282 follow-up (same PR): ``reyn.mcp.elicitation`` used to import ONE
fastmcp type (``fastmcp.client.elicitation.ElicitResult``) via the
now-DELETED ``reyn.mcp._fastmcp_boundary`` module — a strict subclass of
``mcp.types.ElicitResult`` adding zero fields (verified via its MRO), so
it was switched to construct the official SDK's own base type directly.
An AST scan of the whole ``src/reyn/`` tree (excluding
``src/reyn/builtin/plugins/rag/scripts/``, see below) found ZERO
``import fastmcp`` / ``from fastmcp...`` statements anywhere — this part
was actually scanned and is accurate as far as it goes. What it does NOT
cover, and must not be read as covering (per architect/lead-coder's #4302
review of this exact overreach): ``tests/`` was never included in that
scan, and the CONCLUSION drawn from "zero in ``src/reyn/``" — that
fastmcp/``mcp<2.0`` becomes droppable — does not follow, because
``tests/_support/`` (outside the scanned tree) is where fastmcp's real
remaining role lives. See below.

⚠️ **This does NOT mean fastmcp/the ``mcp<2.0`` pin can be dropped from
``pyproject.toml``, and does NOT mean #3698 stage 2 (``mcp>=2.0``
adoption) is unblocked — #4302 (architect, filed after re-checking #4299's
own claim) found the real remaining blocker:**
``src/reyn/builtin/plugins/rag/scripts/chunker_server.py`` /
``vector_store_server.py`` import ``fastmcp.FastMCP()`` to build MCP
SERVERS, but run in an operator-created SEPARATE venv (that directory's
own ``requirements.txt`` header) and do NOT constrain reyn's own venv —
that part of the original finding held. What does NOT hold: ``tests/
_support/`` still builds 5 MCP SERVER test-doubles
(``mcp_fastmcp_echo_server.py``, ``mcp_elicitation_server.py``, and 3
others) on fastmcp's server framework — as long as reyn's own dev/CI venv
installs those, fastmcp (and its own hard ``mcp<2.0`` floor, present in
every fastmcp version) stays present in THAT venv regardless of the
client path being fastmcp-free. Porting those test-doubles to the
official SDK's own server API (``Context``/decorator shape — untested,
own cost) is #4302's scope and the ACTUAL precondition for dropping
fastmcp from ``pyproject.toml`` — not just an unreferenced client-side
import, which is all this PR achieved. "#4282 is #3698 stage 2's
precondition" was true as originally planned, but #4282 landing does not
itself satisfy that precondition — see #4302 for the corrected plan.

Each ``MCPClient`` owns a single connection opened on :meth:`initialize` and
torn down on :meth:`close`, held open via a ``contextlib.AsyncExitStack``
(see :meth:`_initialize_stdio`'s docstring for why — the official SDK
expresses one connection as TWO separate async context managers, not a
single reentrant object) for the object's lifetime (matching the previous
hand-rolled client's caching semantics on ``OpContext.mcp_clients`` / the
pool's subprocess-reuse contract — persistent-subprocess semantics for
stdio either way).

Environment variable expansion:
  ``${VAR_NAME}`` in any string config value is replaced with
  ``os.environ.get("VAR_NAME", "")``. Missing variables expand to empty
  string and a warning is emitted. Apply :func:`expand_env` BEFORE
  handing config to the SDK.

Capability / version gate (#2597 capability slice):
  MCP's ``initialize`` handshake natively negotiates BOTH a protocol version
  and a set of server capabilities (tools/resources/prompts/logging/
  completions) in one round trip — rather than sprinkling version checks
  across reyn, :meth:`initialize` captures both ONCE, right after the
  handshake completes. #3698 stage 1 re-measured this against the official
  SDK directly (live probe, not read from docs): ``ClientSession.
  initialize()`` RETURNS the ``mcp.types.InitializeResult`` directly (a
  plain return value — #4282 removed the last path, fastmcp's OAuth
  transport, that instead read it off a separate ``Client.
  initialize_result`` property populated by ``client.__aenter__()``; every
  path is the plain-return-value shape now). :meth:`supports` answers "did the server
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
  an optional ``elicitation_handler`` (constructor kwarg) — reyn's own
  handler is shaped ``(message, response_type, params, context)``
  (fastmcp's old ``ElicitationHandler`` protocol, kept as reyn's own
  handler shape rather than churning every caller when the transport
  changed), while the official SDK's ``ElicitationFnT`` is ``(context,
  params)`` — 2 args, opposite order, entirely different shape (measured
  by reading both protocols directly, not assumed). ``_adapt_elicitation_
  handler`` wraps it to bridge the two on every transport now (#4282:
  there is no longer a fastmcp path that would take it unadapted). Passing
  ANY non-None handler is itself what declares the ``elicitation`` client
  capability during the initialize handshake. See
  :mod:`reyn.mcp.elicitation` for the handler that routes a server's
  structured question through reyn's consent path
  (:class:`~reyn.mcp.connection_service.MCPConnectionService` builds one
  per held connection); this module only plumbs the constructor kwarg
  through, adapted.

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

  :meth:`_build_oauth_provider` builds the official SDK's
  ``mcp.client.auth.OAuthClientProvider(server_url=url, client_metadata=...,
  storage=MCPOAuthTokenStorage(url), redirect_handler=...,
  callback_handler=...)`` directly (#4282: fastmcp's own ``OAuth`` wrapper
  around this same class is no longer in the path) and passes it as
  ``streamable_http_client(url, headers=..., auth=provider)`` — #4412
  pin-bump PR: the constructor's own ``timeout=`` kwarg is GONE on mcp 2.0
  (confirmed live it was dead even on 1.x — never read anywhere in
  ``mcp.client.auth.oauth2``'s own source — so removing it changes nothing
  behaviorally on either pin) and the transport factory itself is renamed
  (``streamablehttp_client`` → ``streamable_http_client``, both below).
  ``OAuthClientProvider`` IS an ``httpx.Auth``, so it slots into the SDK
  transport's own ``auth=`` kwarg directly, no wrapper object needed.
  ``redirect_handler``/``callback_handler`` (the browser-open + localhost-
  callback round trip fastmcp's ``OAuth`` ran internally) are reyn's own
  implementation now — :mod:`reyn.mcp.oauth_browser_flow`, built from
  starlette/uvicorn (both already core reyn dependencies). A static
  ``client_id`` (skip Dynamic Client Registration) is supported by
  pre-seeding the token storage's ``client_info`` before constructing the
  provider — see :meth:`_build_oauth_provider`'s own docstring for how
  that suppresses DCR (verified by reading the official SDK's
  ``async_auth_flow`` directly).

  Headless graceful failure: before constructing the provider,
  :meth:`_build_oauth_provider` checks :func:`~reyn.mcp.oauth_token_storage.
  has_stored_token` for this URL. If no usable token is cached AND this
  client is running non-interactively (``non_interactive`` constructor kwarg,
  or auto-detected via ``sys.stdin.isatty()`` when not explicitly passed —
  mirrors ``reyn.runtime.session``'s own ``non_interactive`` flag's "no user
  to ask" rationale), it raises :class:`MCPError` immediately with a clear
  message rather than let the OAuth flow open a browser + wait (bounded
  only by the provider's own ``timeout``, default 300s) for a callback
  nobody can complete.
"""
from __future__ import annotations

import asyncio
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


# #2976: substrings that mark a sandbox write denial in a failed server's output.
# Both were OBSERVED in real launches under the real Seatbelt profile, not
# predicted: npm prints ``npm error code EPERM``; uv prints ``Operation not
# permitted (os error 1)``; a Python server prints ``[Errno 1] Operation not
# permitted``. Matching is a diagnostic HINT only — a false positive costs one
# extra sentence in an error that was already failing, so this errs toward
# offering help rather than staying silent.
#
# #3009: these are the SEATBELT (EPERM) shapes, and that is a KNOWN, DELIBERATE
# limit, not an oversight. Landlock denies with EACCES ("Permission denied"),
# which none of these match — so on a Linux host whose Landlock enforces, the
# hints below go silent. That asymmetry is NOT closed here for two reasons:
#   1. It cannot be witnessed from this arc. Landlock is unreachable from
#      production today (#2980: the shim's ``Ruleset`` API raises AttributeError),
#      so a Linux host resolves to NoopBackend and fires no write denial at all —
#      there is nothing to observe, and a marker added on the strength of a man
#      page rather than a measurement is exactly the census this repo keeps
#      finding under its "the mechanism is present" claims.
#   2. Widening to a bare "permission denied" would be WRONG even where it fires:
#      EACCES is also what an ordinary read-only file produces, so the "add it to
#      `write_paths`" advice would be handed to operators whose problem is not the
#      sandbox at all. The correct shape is a per-backend marker set (Seatbelt →
#      EPERM, Landlock → EACCES), which the backend that can actually be TESTED
#      should carry.
# So: #2980's fix — the PR that first makes a Landlock denial observable — owns
# closing this. Until then a Linux operator is no worse off than today (no
# enforcement → no denial → no hint needed).
#
# That handoff is a GATE, not a note: test_3009's end-to-end case skips only while
# no backend on the host enforces. The moment #2980 makes Landlock deny, the test
# starts RUNNING on Linux and goes RED on this marker set — so the asymmetry
# cannot be reintroduced silently by the very PR that makes it reachable.
_WRITE_DENIAL_MARKERS = ("eperm", "operation not permitted")


def _looks_like_write_denial(text: str | None) -> bool:
    """Whether *text* looks like an OS-level permission denial.

    Pure and channel-agnostic on purpose: a denial reaches this module through
    two DIFFERENT transports and the same question is asked of both — a launch
    denial arrives on the subprocess's stderr (:meth:`MCPClient.initialize`),
    while a denial inside a running server's tool handler never touches stderr
    at all and arrives as JSON-RPC tool-error content
    (:meth:`MCPClient.call_tool`). See :data:`_TOOL_CALL_WRITE_DENIAL_HINT`.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _WRITE_DENIAL_MARKERS)


# #3009: the tool-call sibling of initialize()'s #2976 launch hint. Separate text,
# same predicate, because the two denials leave the operator in different places:
# a launch denial means "this server never started" (the fix is about the
# runtime's own cache), while this one means "the server is running fine and the
# path YOU passed is outside its write scope" (the fix is about that path).
#
# MEASURED, not predicted — the real builtin vector-store server under the real
# Seatbelt profile, ``db_path`` outside cwd, returns:
#
#     {"isError": true, "content": [{"type": "text", "text":
#       "Error calling tool 'upsert': [Errno 1] Operation not permitted: '<dir>'"}]}
#
# i.e. FastMCP prefixes the handler's exception but preserves ``str(exc)``
# verbatim, so the errno survives into the payload and _looks_like_write_denial
# matches it. Both concrete remedies are named (the #2932 ``require_mcp`` shape),
# and the zero-config one goes FIRST per #3009's principle: the path that needs no
# declaration is the recommendation, the grant is the documented deviation.
_TOOL_CALL_WRITE_DENIAL_HINT = (
    "\n\nHint (#3009): this looks like reyn's MCP sandbox DENYING the server a "
    "write to a path outside its granted write scope — NOT a bug in the tool "
    "(the error above names the exact path). A sandboxed stdio MCP server may "
    "write only inside its working directory unless its config says otherwise. "
    "Two ways forward:\n"
    "  1. Pass a path INSIDE the server's working directory (a relative path "
    "like \"rag/docs.sqlite\" needs no configuration at all), or\n"
    "  2. Grant the location you want — add it to this server's `write_paths`:\n"
    "         mcp:\n"
    "           servers:\n"
    "             {server}:\n"
    "               write_paths: [\"~/reyn-rag\"]\n"
    "Declaring `write_paths` replaces the built-in per-runtime defaults; the "
    "server's working directory is always granted."
)

# #3698 stage 1 (removed): ``_looks_like_init_timeout`` used to walk an
# exception's ``__cause__`` chain looking for a ``TimeoutError``, because
# fastmcp's OWN ``init_timeout`` mechanism (``anyio.fail_after`` inside
# ``Client.initialize()``) re-raised it wrapped in two layers of opaque
# ``RuntimeError`` — the chain-walk existed only to see through that
# wrapping. Once reyn bounds the stdio handshake itself
# (``asyncio.wait_for(session.initialize(), timeout=...)`` in
# ``_initialize_stdio``), the exception that actually reaches the caller on
# timeout is a plain ``TimeoutError`` with ``__cause__ is None`` — live-
# verified against a stdio server that starts and never speaks (see the
# commit removing this function for the probe output). Detecting it is now
# a direct ``isinstance(exc, TimeoutError)`` at the call site; no separate
# predicate function is needed. If a future reader hits an opaque multi-
# layer wrapping again (e.g. from some other library reyn adopts), THIS is
# the shape that pattern takes and why — re-derive the chain-walk from this
# note rather than from scratch.


# #3028: how long to wait for a server to answer the MCP handshake before giving up.
#
# This bounds ONLY the handshake, never a tool call — see MCPClient.initialize() for
# why that distinction is the whole point of using FastMCP's ``init_timeout`` rather
# than copying the HTTP ``timeout`` (= read_timeout_seconds) knob down onto stdio.
#
# Sized by two MEASURED constraints, a floor and a ceiling.
#
# The floor is a real first-run fetch — the failure the owner actually hit. Cold uv
# cache, fast connection:
#
#     markitdown-mcp   18.1s   (the owner's exact server)
#     mcp-server-time   2.4s   (a small package, for contrast)
#
# So a legitimate launch of a real server burns ~18s before it can speak a word, and
# a slower link or heavier dependency tree goes multiples above. 30s — the HTTP read
# timeout's value — would kill exactly the launch this is meant to protect.
#
# The ceiling is the gateway. MCPGateway._run wraps acquire+op in
# ``resolve_call_timeout`` (default 120s) and every production MCP path goes through
# it (#2421 pins that structurally), so the handshake was ALREADY bounded at 120s
# there — reyn never actually waited forever on any reachable path, contrary to
# #3028's premise. What it did was surface that bound as a bare ``MCPFault:
# TimeoutError:``, naming neither server, cause, nor remedy. So this bound's job is
# less "make it finite" than "reach a finite end that EXPLAINS ITSELF", and it can
# only do that by firing FIRST — MEASURED both ways against a real silent server:
#
#     init_timeout 5 < call_timeout 30  ->  MCPError + the hint below   (operator can act)
#     call_timeout 5 < init_timeout 30  ->  MCPFault: TimeoutError:     (operator has nothing)
#
# 60s sits between: ~3.3x the measured cold fetch, and strictly under the gateway's
# 120s so the explaining error wins deterministically rather than by a race. Nothing
# is lost by landing under that ceiling — a launch slower than 120s was already dead
# at the gateway, just silently; this one dies sooner and says why.
#
# Per the owner's #2964 principle the default is a FLOOR, not a policy: an operator
# with a genuinely slow server raises it, and `init_timeout: 0` restores an unbounded
# handshake (still under the gateway's own bound). Neither is reyn's to decide — but
# note that raising THIS alone cannot buy more than the gateway allows, which is why
# the hint below names both knobs.
_DEFAULT_INIT_TIMEOUT_SECONDS = 60

# #3028: the init-timeout sibling of the #2976 launch hint and the #3009 tool-call
# hint. Same "name the cause, then name the concrete ways out" shape (#2932's
# ``require_mcp`` error), different place to leave the operator: a write denial means
# "the server ran and was refused", while THIS means "the server started and never
# spoke". That silence is diagnostically empty on its own — the process is alive, so
# there is no exit code, and typically nothing on stderr either — which is precisely
# why the hint has to supply the interpretation the operator cannot read off the
# failure. A first-run `uvx`/`npx` fetch is the overwhelmingly likely cause (it is
# the one thing that makes a healthy server sit silent for tens of seconds), so the
# zero-config remedy leads and the config knob follows, per #3009's ordering.
#
# Written to a HARD LENGTH BUDGET, which is why it is terser than its siblings.
# ``pool.describe_fault(limit=600)`` summarises this message on its way to the LLM
# and truncates from the END, and #2976 already paid for that lesson once by losing
# a trailing hint entirely. Ordering the hint before the stderr dump (as #2976 did)
# is necessary but NOT sufficient here: this hint carries two remedies, so a verbose
# version is cut through its own middle and the operator keeps remedy 1 while the
# knob in remedy 2 silently disappears — which is exactly what the gateway-path test
# caught. Every clause below is load-bearing; keep the whole thing inside the budget
# (test_3028 pins it end-to-end through the real seam) rather than trading away the
# second remedy or raising a shared, MCP-agnostic limit for one caller's benefit.
_INIT_TIMEOUT_HINT = (
    "\n\nHint (#3028): server started but never answered the MCP handshake within "
    "{seconds:g}s — usually a launcher (`uvx`/`npx`) fetching its package on first "
    "run. Either pre-install it (`uv tool install <pkg>`, then point `command` at "
    "it) — also needed offline/behind a proxy — or, for a slow launch, raise BOTH "
    "bounds on `{server}`: `init_timeout: 300` AND `call_timeout_seconds: 600` "
    "(the per-op bound, default 120s, covers the launch too — `init_timeout` alone "
    "is not enough)."
)

async def _close_stack_after_init_failure(exc: BaseException, stack: "Any") -> BaseException:
    """Close ``stack`` after an ``initialize()`` failure and return the exception
    the caller should format into :class:`MCPError`'s message — OR re-raise
    ``exc`` UNCHANGED if it is genuine cancellation of the host task (never
    swallow / relabel a real cancel as a connection failure).

    #4282-adjacent CI-red root-cause fix (discovered while #4282 was in
    flight; landed ahead of it since it blocked #4283's own merge):
    live-verified (disposable probes against a real connection-refused
    target and a real hung-server target — not assumed) that
    ``asyncio.wait_for()`` wrapping an anyio-native call tree
    (``stdio_client``/``streamablehttp_client``/``sse_client`` +
    ``ClientSession``, all anyio cancel-scope based) corrupts anyio's
    per-task cancel-scope bookkeeping across ``wait_for``'s internal Task
    boundary: it surfaces as a bare, uninformative ``CancelledError`` at the
    ``session.initialize()`` await point, THEN a
    "Attempted to exit cancel scope in a different task" ``RuntimeError``
    when the stack is later closed — hiding the REAL failure (e.g. a plain
    connection-refused) entirely. Switching the caller to
    ``anyio.fail_after()`` (anyio-native, no such corruption) fixes both
    halves: the real failure now surfaces cleanly from ``stack.aclose()``
    as an ``ExceptionGroup`` wrapping the actual exception (e.g.
    ``httpx.ConnectError``) once the task group's cancel scope unwinds
    normally.

    Discriminating "genuine external cancel of THIS host task" (e.g.
    :func:`reyn.core.cancellable.race_cancellable`'s watcher, or any other
    real ``Task.cancel()`` against us) from "an internal anyio cancel-scope
    propagating a SIBLING task's failure as ``CancelledError`` at OUR await
    point" turned out to NOT be answerable via ``Task.cancelling()`` alone
    — live-measured: it reads 1 in BOTH cases, because anyio's asyncio
    backend implements cross-task cancel-scope cancellation via the exact
    same ``Task.cancel()`` primitive an external caller would use, so the
    counter cannot tell them apart. The reliable signal instead: **whether
    closing the stack reveals a concrete underlying failure.** A genuinely
    externally-cancelled task group has no failed child to report —
    ``stack.aclose()`` completes with NO exception (live-verified: cancel a
    real host task mid-``session.initialize()`` against a hung server,
    ``aclose()`` closes clean). An internally-failed one DOES (the
    ``ExceptionGroup`` above). So: if ``exc`` is ``CancelledError`` AND
    ``stack.aclose()`` closes clean, this was genuine cancellation —
    re-raise ``exc`` untouched so it reaches ``race_cancellable``'s own
    (correct) translation to :class:`~reyn.core.cancellable.Cancelled`
    unmolested, never becoming a misleading ``MCPError``.
    """
    real_exc: BaseException = exc
    try:
        await stack.aclose()
    except BaseException as close_exc:
        real_exc = close_exc
    else:
        if isinstance(exc, asyncio.CancelledError):
            raise exc
    # Unwrap a single-member ExceptionGroup down to its one real leaf so the
    # MCPError message names the actual failure (e.g. "ConnectError: All
    # connection attempts failed") instead of anyio's generic "unhandled
    # errors in a TaskGroup" wrapper text. A multi-member group is left
    # as-is — its own str() is still more informative than picking one
    # arbitrary member.
    while isinstance(real_exc, BaseExceptionGroup) and len(real_exc.exceptions) == 1:
        real_exc = real_exc.exceptions[0]
    return real_exc


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

    #2597 F1 predicate — originally verified by reading the installed
    fastmcp 3.4.2 + mcp SDK source AND by live-probing both branches
    against the real ``tests/_support/mcp_fastmcp_echo_server.py`` test
    double over stdio. **#3698 stage 1 re-measurement**: this predicate's
    own logic needed ZERO code changes — it already reads
    ``mcp.shared.exceptions.McpError``/``mcp.types.CONNECTION_CLOSED``
    directly from the official SDK, never through a fastmcp-shaped
    wrapper. Re-confirmed live against the official SDK path
    (``_initialize_stdio`` + a killed-subprocess probe): the
    ``McpError``/``CONNECTION_CLOSED`` branch fires exactly the same way
    as it did for fastmcp, since both sit on the same underlying
    ``mcp.shared.session.BaseSession`` receive loop. **#4282**: fastmcp is
    no longer constructed anywhere in this module, so the
    ``RuntimeError("Server session was closed unexpectedly")`` branch
    fastmcp's own ``Client._context_manager`` used to wrap a closed session
    in — dead weight since #3698 for the transports it had already
    migrated — is now dead for EVERY transport and has been removed
    outright rather than kept "just in case" (no test pinned it; nothing
    in this codebase can raise that exact message anymore):

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
    try:
        # #4412 pin-bump PR: the SDK's own exception class is renamed on 2.0
        # -- `mcp.shared.exceptions.McpError` (1.x) -> `MCPError` (2.0),
        # confirmed live (`hasattr(mcp.shared.exceptions, "McpError")` is
        # False, `hasattr(..., "MCPError")` is True). The stale 1.x import
        # name silently caught its own ImportError below and made this
        # predicate return False UNCONDITIONALLY under mcp 2.0 -- every
        # transport death went unrecognized, so _heal() never reconnected.
        # Found via a real reconnect-flow repro, not a static grep.
        from mcp.shared.exceptions import MCPError as _SdkMcpError
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


def _extract_stdio_child_pid(stdio_cm: "Any") -> int | None:
    """#2714 best-effort: return the OS pid of the stdio subprocess opened by
    the official SDK's ``stdio_client(...)`` async context manager, or None
    if it can't be located.

    #3698 stage 1: ``stdio_client`` is an ``@asynccontextmanager``-decorated
    generator function; ``contextlib.asynccontextmanager`` wraps it in an
    ``_AsyncGeneratorContextManager`` whose ``.gen`` attribute IS the entered
    generator, and ``anyio.abc.Process`` lives in that generator's OWN local
    variable (``process``) once entered — one hop, since ``MCPClient`` holds
    the entered context manager directly (via its own ``AsyncExitStack`), not
    behind fastmcp's extra task-local indirection the pre-swap
    ``_walk_frames_for_process_pid`` BFS existed to reach. Live-verified: the
    extracted pid matched the server's own ``os.getpid()`` exactly (see the
    commit message introducing this function for the probe output).

    Defensive throughout: any structural drift from an ``mcp`` SDK upgrade
    returns None, and the belt-and-suspenders reap then simply falls back to
    the async graceful teardown — byte-identical to pre-#2714 behaviour.
    Captured ONCE at connect (structure known-good) and only ever used as a
    terminate target, so a stale/None value can never do worse than the
    pre-fix orphan."""
    try:
        frame = getattr(getattr(stdio_cm, "gen", None), "ag_frame", None)
        if frame is None:
            return None
        process = frame.f_locals.get("process")
        pid = getattr(process, "pid", None)
        return pid if isinstance(pid, int) else None
    except Exception:  # noqa: BLE001 — pid capture is best-effort, never fatal
        return None


# ── Client ───────────────────────────────────────────────────────────────────

class MCPClient:
    """Thin async wrapper around the official ``mcp`` SDK's ``ClientSession``
    (#4282: fastmcp is no longer constructed anywhere in this class).

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
        emit_event: Callable[..., Any] | None = None,
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
        # silently ignoring it (only _build_oauth_provider ever reads 'auth').
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
        # #3821: audit-event sink, same optional-injection shape as
        # ``hooks/shell_runner``'s ``emit_event`` (None -> skip). Only the
        # held-connection path (MCPConnectionService) wires one; the ephemeral
        # MCPClientPool path has no sink to give, so a fallback there is still
        # WARNING-only. That asymmetry is stated in _sandbox_wrap_stdio's
        # docstring rather than left for a reader to discover from the wiring.
        self._emit_event: Callable[..., Any] | None = emit_event
        # #2597 S2b: optional async server->client notifications bridge — a
        # ReynMCPMessageHandler (#3698 P3: composes the message-handler
        # contract fastmcp originally defined, rather than subclassing it —
        # see reyn.mcp.message_handler's module docstring) that receives
        # tools/prompts list_changed +
        # progress notifications on this client's held connection and emits them onto
        # reyn's EventLog. None (default) preserves pre-S2b behaviour — no bridge, no
        # behaviour change for callers that don't pass one (e.g. the ephemeral
        # per-call MCPClientPool path never installs a handler).
        self._message_handler: Any = message_handler
        # #2597 slice ③: optional elicitation handler (see
        # ``reyn.mcp.elicitation.build_elicitation_handler``, shaped to
        # fastmcp's original ``ElicitationHandler`` protocol — reyn kept
        # that shape as its own rather than churning callers when the
        # transport changed; ``_adapt_elicitation_handler`` bridges it to
        # the official SDK's own shape) — routes a server->client
        # ``elicitation/create`` request through reyn's consent path.
        # Passing ANY non-None handler is itself what declares the
        # ``elicitation`` client capability during the initialize handshake
        # (D6 — held connections always install one; the ephemeral per-call
        # ``MCPClientPool`` path never does, same None-default no-op
        # pattern as ``message_handler``).
        self._elicitation_handler: Any = elicitation_handler
        # #2597 slice ④: explicit override for the headless-OAuth pre-flight
        # check in _build_oauth_provider (see module docstring's "Headless
        # graceful failure"). None (default) means auto-detect via
        # sys.stdin.isatty() at the point _build_oauth_provider actually
        # needs the answer — see _is_non_interactive().
        self._non_interactive_override: bool | None = non_interactive
        self._client: Any = None  # official mcp.ClientSession when initialized
        # #3698 stage 1 / #4282: holds the entered transport + ClientSession
        # async context managers open for this object's lifetime — the
        # official SDK's connection lifecycle is a pair of async context
        # managers, not fastmcp.Client's old single reentrant
        # __aenter__/close() object, so an AsyncExitStack is what reproduces
        # the same "open once, use across many calls, close later" pattern
        # (live-verified: initialize() then TWO separate call_tool()s
        # against the SAME held session, then a clean stack.aclose() — see
        # the commit message for the probe). None until initialize()
        # actually opens a connection (every transport now).
        self._exit_stack: "Any | None" = None
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
        # #2597 capability/version gate: captured right after the official
        # SDK's ClientSession.initialize() handshake completes (a plain
        # return value — see the module docstring's "Capability / version
        # gate" section). None until then (or if the server's
        # InitializeResult was unavailable — handled defensively, never
        # raises).
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

    async def _paginate_official_sdk(self, list_fn: Any, items_attr: str) -> list[Any]:
        """#3698 stage 1: fastmcp's ``Client.list_tools()`` (and its sibling
        list methods) auto-paginate internally, following the page cursor —
        reyn relied on that. The official SDK's raw ``ClientSession.
        list_tools()`` returns ONE page (a ``ListToolsResult`` object, not a
        bare list) and does NOT paginate on its own — measured directly
        (its own return annotation; ``ClientSession`` has no auto-paginating
        convenience layer fastmcp added on top). This reproduces fastmcp's
        behavior: follow the cursor until it's ``None``, same 250-page guard
        (a malformed/adversarial server cycling cursors forever must not
        hang this call) as the pre-swap comment on the fastmcp path named.

        #4368 (arc #4412): both the result field name (``nextCursor`` 1.x /
        ``next_cursor`` 2.0) AND *list_fn*'s own call shape (1.x: bare
        positional ``cursor``; 2.0: keyword-only ``params:
        PaginatedRequestParams | None``, confirmed live via
        ``inspect.signature`` — the positional shorthand is gone entirely)
        differ between pins. Routed through ``_mcp_client_boundary``'s seam
        for both — see that module's own docstring for the full
        rationale."""
        from reyn.mcp._mcp_client_boundary import call_paginated_list, next_page_cursor

        items: list[Any] = []
        cursor: str | None = None
        for _ in range(250):
            result = await call_paginated_list(list_fn, cursor)
            items.extend(getattr(result, items_attr))
            cursor = next_page_cursor(result)
            if cursor is None:
                break
        return items

    async def initialize(self) -> None:
        """Open the transport and complete the MCP handshake.

        Idempotent: a second call is a no-op.

        #4282: every transport — ``stdio``, ``http``/``sse`` with or
        without OAuth configured — now goes through the official ``mcp``
        SDK directly (:meth:`_initialize_stdio` /
        :meth:`_initialize_http_or_sse`). fastmcp is no longer constructed
        anywhere in this dispatch; see the module docstring's HYBRID
        warning for what that does and does NOT mean yet (``fastmcp``
        stays a declared dependency until this PR also removes it from
        ``pyproject.toml`` — a separate, later step in the same PR, not a
        follow-up).
        """
        if self._initialized:
            return
        if self._type == "stdio":
            await self._initialize_stdio()
        else:
            await self._initialize_http_or_sse()

    async def _initialize_stdio(self) -> None:
        """#3698 stage 1: stdio via the official ``mcp`` SDK, no fastmcp.

        Connection-lifetime model: fastmcp's ``Client`` is a single reentrant
        async context manager an ``MCPClient`` enters once and holds open for
        its own lifetime. The official SDK expresses the SAME connection as
        TWO separate async context managers (``stdio_client`` for the
        transport, ``ClientSession`` for the protocol session) that are
        normally used inside one ``async with`` block. An
        ``contextlib.AsyncExitStack`` reproduces the "open once, use across
        many calls, close later" pattern instead — live-verified against the
        real echo test-double (initialize → two separate call_tool()s against
        the SAME held session → a clean ``stack.aclose()``) before this was
        written, not assumed from reading the API alone.
        """
        from contextlib import AsyncExitStack

        import anyio
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        # Computed BEFORE the try block: the except handler's init-timeout hint
        # needs this value even if the exception fires before the handshake
        # itself starts (a bad command / sandbox-wrap failure), so it cannot
        # live inside the try (unbound-on-early-exception otherwise).
        init_timeout = float(self._config.get("init_timeout", _DEFAULT_INIT_TIMEOUT_SECONDS))
        stack = AsyncExitStack()
        try:
            command = self._config.get("command")
            if not command:
                raise MCPError("stdio MCP server config requires 'command'")
            args = list(self._config.get("args") or [])
            command, args = self._sandbox_wrap_stdio(command, args)
            env = self._config.get("env")
            # Subprocess stderr capture for diagnostic readback on init
            # failure — same tempfile.TemporaryFile pattern _open_stdio used
            # for fastmcp's log_file=; the official SDK's stdio_client takes
            # an errlog= TextIO directly (a real fileno, same requirement).
            try:
                self._stderr_capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            except Exception:  # noqa: BLE001 — temp-file failure is non-fatal
                self._stderr_capture = None
            params = StdioServerParameters(
                command=command,
                args=args,
                env=dict(env) if env else None,
                cwd=self._config.get("cwd"),
            )
            stdio_cm = stdio_client(
                params, **({"errlog": self._stderr_capture} if self._stderr_capture else {}),
            )
            read, write = await stack.enter_async_context(stdio_cm)
            elicitation_callback = None
            if self._elicitation_handler is not None:
                elicitation_callback = _adapt_elicitation_handler(self._elicitation_handler)
            session = await stack.enter_async_context(
                ClientSession(
                    read, write,
                    message_handler=self._message_handler,
                    elicitation_callback=elicitation_callback,
                )
            )
            # #3028/#3698 stage 1: reyn now owns the handshake bound directly
            # instead of relying on fastmcp's opaque init_timeout constructor
            # kwarg (anyio.fail_after wrapped in two layers of RuntimeError —
            # see the removed _looks_like_init_timeout for why that
            # indirection existed and why it no longer does).
            # #4282-adjacent fix: anyio.fail_after(), not asyncio.wait_for() —
            # see _close_stack_after_init_failure's docstring for the full
            # root-cause (asyncio.wait_for corrupts anyio's per-task
            # cancel-scope bookkeeping across an anyio-native call tree like
            # this one). init_timeout == 0 disables THIS bound (#3028's own
            # documented escape hatch) — anyio.fail_after(0), like
            # asyncio.wait_for(timeout=0), means "fire almost immediately"
            # (live-verified), the OPPOSITE of disabled, so 0 must skip the
            # wrapper entirely rather than being passed through.
            if init_timeout > 0:
                with anyio.fail_after(init_timeout):
                    init_result = await session.initialize()
            else:
                init_result = await session.initialize()
        except MCPError:
            self.close_stderr_capture()
            await stack.aclose()
            raise
        except (Exception, asyncio.CancelledError) as exc:
            tail = self.read_stderr_tail()
            self.close_stderr_capture()
            # A fresh name, not a rebind of `exc` — _close_stack_after_init_failure
            # returns `BaseException` (broader than the `Exception | CancelledError`
            # this except clause narrowed `exc` to), so reassigning `exc` itself
            # would widen its statically-known type for every later reference.
            real_exc = await _close_stack_after_init_failure(exc, stack)
            from reyn.security.sandbox.policy import DEFAULT_SANDBOX_NETWORK

            hint = ""
            if not self._config.get("network", DEFAULT_SANDBOX_NETWORK):
                hint = (
                    "\nHint (#1344): this MCP server is sandboxed with network "
                    "DISABLED (`network: false` in its config). If it needs "
                    "network access, set `network: true` (or remove the override)."
                )
            if _looks_like_write_denial(tail):
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
            # #3698 stage 1: no more chain-walking predicate — anyio.fail_after
            # raises a plain TimeoutError (__cause__=None) directly, live-
            # verified against a stdio server that starts and never speaks
            # (see this commit's message for the probe output). The old
            # fastmcp-only branch needed _looks_like_init_timeout because
            # fastmcp implemented its OWN init_timeout via anyio.fail_after,
            # re-wrapped in two layers of RuntimeError before it reached us.
            if isinstance(real_exc, (TimeoutError, asyncio.TimeoutError)):
                hint += _INIT_TIMEOUT_HINT.format(
                    seconds=init_timeout, server=self._server_name or "<server-name>",
                )
            if tail:
                raise MCPError(
                    f"MCP initialize failed: {real_exc}{hint}\n"
                    f"--- subprocess stderr (tail) ---\n{tail}"
                ) from real_exc
            raise MCPError(f"MCP initialize failed: {real_exc}{hint}") from real_exc

        self._client = session
        self._exit_stack = stack
        self._initialized = True
        # #2714: capture the stdio subprocess pid now (structure known-good
        # right after the handshake) for the belt-and-suspenders reap in
        # close(). #3698 stage 1: reads it off the ENTERED stdio_client
        # generator's own frame directly — one hop, not fastmcp's task-local
        # AsyncExitStack indirection _walk_frames_for_process_pid existed
        # for. Live-verified: the extracted pid matched the server's own
        # os.getpid() exactly (see this commit's message for the probe).
        self._child_pid = _extract_stdio_child_pid(stdio_cm)
        # #2597 capability/version gate: read the negotiated version +
        # capabilities right after the handshake. init_result is the plain
        # return value of session.initialize() here (not a property read off
        # a later object, unlike fastmcp's Client.initialize_result) — never
        # None on a successful call, but read defensively anyway in case a
        # future SDK version's contract changes underneath us.
        if init_result is not None:
            # #4368 (arc #4412): protocolVersion (1.x) / protocol_version
            # (2.0) -- routed through _mcp_client_boundary's seam so this
            # site doesn't hardcode either pin's field name (see that
            # module's own docstring for the full rationale).
            from reyn.mcp._mcp_client_boundary import negotiated_protocol_version

            self._negotiated_version = negotiated_protocol_version(init_result)
            self._server_capabilities = init_result.capabilities
        else:
            self._negotiated_version = None
            self._server_capabilities = None

    async def _initialize_http_or_sse(self) -> None:
        """#3698 stage 1: http/sse (no OAuth configured) via the official
        ``mcp`` SDK, no fastmcp. Same ``AsyncExitStack`` lifetime model as
        :meth:`_initialize_stdio` (see its docstring); no stdio-only hints
        (network-disabled / write-denial / stderr tail) since neither
        transport spawns a subprocess.

        ``streamablehttp_client`` yields a 3-tuple (``read, write,
        get_session_id``) — one MORE element than stdio's/sse's 2-tuple
        (measured via its own return-type annotation); ``sse_client``
        matches stdio's 2-tuple shape exactly (measured by reading its
        source's own ``yield read_stream, write_stream``). Only the
        ``read``/``write`` pair is needed here; the session-id callable
        (http-only, used for session resumption) is not currently consumed
        by anything in reyn — same scope as this stage's other transports,
        where only what reyn ALREADY read off the fastmcp wrapper carries
        over, not the full surface a future caller might eventually want.
        """
        from contextlib import AsyncExitStack

        import anyio
        from mcp.client.session import ClientSession

        init_timeout = float(self._config.get("init_timeout", _DEFAULT_INIT_TIMEOUT_SECONDS))
        url = self._config.get("url")
        if not url:
            raise MCPError(f"{self._type} MCP server config requires 'url'")
        headers = {str(k): str(v) for k, v in (self._config.get("headers") or {}).items()}
        if self._agent_id and "X-Reyn-Agent-Id" not in headers:
            headers["X-Reyn-Agent-Id"] = self._agent_id
        read_timeout = self._config.get("timeout", 30)

        stack = AsyncExitStack()
        try:
            if self._type == "http":
                # #4282: OAuth (if configured — __init__ already rejects it
                # for sse) is now built as the official SDK's own
                # OAuthClientProvider (an httpx.Auth) and passed straight
                # into streamablehttp_client's own auth= kwarg, same as any
                # other httpx.Auth — no more fastmcp Client/transport
                # wrapper needed to carry it.
                auth = await self._build_oauth_provider(url)
                # #4412 pin-bump PR: `streamablehttp_client` (the
                # headers/timeout/auth-kwarg form) is GONE on mcp 2.0 —
                # confirmed live, `mcp.client.streamable_http` no longer
                # exports it at all, only its replacement
                # `streamable_http_client(url, http_client=...)`, which
                # takes a pre-built HTTP client instead of individual
                # headers/timeout/auth kwargs. That pre-built client must be
                # an `httpx2.AsyncClient` specifically — mcp 2.0 depends on
                # `httpx2`, a genuinely SEPARATE package from `httpx`
                # (confirmed live: `httpx2.AsyncClient is httpx.AsyncClient`
                # is False), which arrives transitively via `mcp`'s own
                # declared dependency (`pip show mcp` → `Requires: ...
                # httpx2 ...`) — reyn declares nothing extra for it.
                # Deliberately calling the SDK's own `create_mcp_http_client`
                # factory (re-exported from this same module) rather than
                # importing `httpx2` and constructing the client here
                # directly — that would mean reyn's own code speaks the
                # SDK's transport-library vocabulary, which #4368's own
                # ruling (construction is not a seamed axis; reyn's surface
                # must not grow every time the SDK's vocabulary grows)
                # argues against. The factory takes the same
                # headers/timeout/auth reyn already builds and returns an
                # already-`follow_redirects=True`-configured client.
                from mcp.client.streamable_http import (
                    create_mcp_http_client,
                    streamable_http_client,
                )

                http_client = create_mcp_http_client(
                    headers=headers, timeout=read_timeout, auth=auth,
                )
                read, write = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
            else:
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(url, headers=headers, timeout=read_timeout)
                )
            elicitation_callback = None
            if self._elicitation_handler is not None:
                elicitation_callback = _adapt_elicitation_handler(self._elicitation_handler)
            session = await stack.enter_async_context(
                ClientSession(
                    read, write,
                    message_handler=self._message_handler,
                    elicitation_callback=elicitation_callback,
                )
            )
            # #4282-adjacent fix: anyio.fail_after(), not asyncio.wait_for() —
            # see _close_stack_after_init_failure's docstring for the full
            # root-cause (this is the exact bug that made #4283's CI red:
            # asyncio.wait_for corrupts anyio's per-task cancel-scope
            # bookkeeping across an anyio-native call tree like this one,
            # turning a plain connection-refused into an uninformative bare
            # CancelledError instead of a reportable MCPError).
            if init_timeout > 0:
                with anyio.fail_after(init_timeout):
                    init_result = await session.initialize()
            else:
                init_result = await session.initialize()
        except MCPError:
            await stack.aclose()
            raise
        except (Exception, asyncio.CancelledError) as exc:
            # A fresh name, not a rebind of `exc` — see the matching comment in
            # _initialize_stdio's except clause for why.
            real_exc = await _close_stack_after_init_failure(exc, stack)
            hint = ""
            if isinstance(real_exc, (TimeoutError, asyncio.TimeoutError)):
                hint = _INIT_TIMEOUT_HINT.format(
                    seconds=init_timeout, server=self._server_name or "<server-name>",
                )
            raise MCPError(f"MCP initialize failed: {real_exc}{hint}") from real_exc

        self._client = session
        self._exit_stack = stack
        self._initialized = True
        if init_result is not None:
            # #4368 (arc #4412): protocolVersion (1.x) / protocol_version
            # (2.0) -- routed through _mcp_client_boundary's seam so this
            # site doesn't hardcode either pin's field name (see that
            # module's own docstring for the full rationale).
            from reyn.mcp._mcp_client_boundary import negotiated_protocol_version

            self._negotiated_version = negotiated_protocol_version(init_result)
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
        # #4282: fastmcp is gone from every live path (OAuth was the last
        # holdout) — self._client is always the official SDK's
        # ClientSession now, so this only ever builds ONE kwargs shape.
        # progress_callback / read_timeout_seconds are ClientSession.
        # call_tool's own kwarg names (measured by reading its signature).
        kwargs: dict[str, Any] = {}
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        if timeout_seconds is not None:
            # #4412 pin-bump PR: ClientSession.call_tool's read_timeout_seconds
            # is `float | None` on 2.0, confirmed live via its own signature —
            # was `timedelta | None` on 1.x. Passing a timedelta where 2.0
            # expects a float crashes downstream with `unsupported operand
            # type(s) for +: 'float' and 'datetime.timedelta'` (found via a
            # real ephemeral-session repro, not a static read).
            kwargs["read_timeout_seconds"] = timeout_seconds
        try:
            # ClientSession.call_tool() is already the raw/no-raise variant
            # fastmcp needed a call_tool_mcp-suffixed method to reach
            # (measured by reading its source: it returns CallToolResult
            # unconditionally, never raises on isError) — same object shape
            # _result_to_dict already flattens, so op_runtime/mcp.py's
            # consumed shape stays byte-identical to the pre-#3698 one.
            result = await self._client.call_tool(name, args or {}, **kwargs)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP tools/call error: {exc}")
        return self._annotate_write_denial(_result_to_dict(result))

    def _annotate_write_denial(self, result: dict[str, Any]) -> dict[str, Any]:
        """Append the ``write_paths`` hint to *result* when it carries a sandbox
        write denial. Returns *result* unchanged otherwise (#3009).

        Why HERE and not in ``_result_to_dict``: that helper is a pure flattener
        of the SDK's shape, and this is reyn's own diagnosis of a reyn-imposed
        restriction — different altitude. This is also the deepest single seam
        every tool call passes through: ``gateway.call_tool`` and
        ``connection_service.call_tool`` both reach the server via THIS method,
        so wiring it here covers both without either needing to know about it.

        Gated on ``stdio`` for the same reason ``initialize``'s hint is: only a
        spawned subprocess is sandboxed, so only it can be denied by one, and
        only its config has a ``write_paths`` to point the operator at. Advice
        about a knob an http/sse server does not have would be a wrong turn.

        The hint is APPENDED, unlike #2976's, which is deliberately prepended.
        That was forced by ``pool.describe_fault(limit=600)`` truncating an init
        error from the END; nothing truncates this path — a tool-error result is
        returned normally (never raised), and ``op_runtime/mcp.py`` joins every
        text block into the LLM's tool result whole. So the natural order stands:
        what happened, then what to do about it.
        """
        if self._type != "stdio" or not result.get("isError"):
            return result
        content = result.get("content")
        if not isinstance(content, list):
            return result
        text = "\n".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if not _looks_like_write_denial(text):
            return result
        hint = _TOOL_CALL_WRITE_DENIAL_HINT.format(
            server=self._server_name or "<server-name>",
        )
        # A separate block, not an edit of the server's own text: the server's
        # message is data reyn relays, the hint is reyn's. Both reach the reader
        # either way (op_runtime joins them), so keep the provenance clean.
        return {**result, "content": [*content, {"type": "text", "text": hint}]}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the tools advertised by this server as plain dicts.

        The official SDK's raw ``ClientSession.list_tools()`` returns ONE
        ``ListToolsResult`` page, not an auto-paginated flat list (measured
        — see :meth:`_paginate_official_sdk`, which reproduces the
        auto-pagination fastmcp's old convenience wrapper did, following
        ``next_cursor`` up to a 250-page guard) — #2597 S1's free win
        (servers with >1 page of tools no longer silently truncate)
        preserved across #3698/#4282's transport swap.
        """
        await self.initialize()
        # #2597 capability/version gate: same seam as call_tool — see there.
        require_capability(self, "tools")
        try:
            tools = await self._paginate_official_sdk(self._client.list_tools, "tools")
        except Exception as exc:
            _classify_and_raise(exc, f"MCP tools/list error: {exc}")
        return [_tool_to_dict(t) for t in tools]

    # ── resources (#2597 slice ②a — consumption; ②b adds subscribe below) ──────

    async def list_resources(self) -> list[dict[str, Any]]:
        """Return the resources advertised by this server as plain dicts.

        Mirrors :meth:`list_tools`: paginates via :meth:`_paginate_official_sdk`
        (follows ``next_cursor``) and gates on the ``"resources"`` capability
        before issuing the request.
        """
        await self.initialize()
        require_capability(self, "resources")
        try:
            resources = await self._paginate_official_sdk(
                self._client.list_resources, "resources",
            )
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
            # #4412 pin-bump PR: ListResourceTemplatesResult's own field is
            # resource_templates (snake_case) on 2.0 -- confirmed live via
            # model_fields -- was resourceTemplates (camelCase) on 1.x.
            templates = await self._paginate_official_sdk(
                self._client.list_resource_templates, "resource_templates",
            )
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/templates/list error: {exc}")
        return [_resource_to_dict(t) for t in templates]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one resource (or a resolved resource-template URI) and return
        its contents flattened to a dict: ``{"contents": [...]}`` — each
        entry a flattened ``TextResourceContents``/``BlobResourceContents``.

        ``ClientSession.read_resource`` returns the full
        ``ReadResourceResult`` (live-verified: called against the real echo
        server's ``resource://pid``, ``.contents`` reachable directly) —
        the shape-flattening lives in ONE place
        (:func:`_read_resource_result_to_dict`), mirroring how
        :meth:`call_tool` flattens ``CallToolResult`` for the same reason.
        ``uri`` is typed ``AnyUrl`` on that method but accepts a plain
        ``str`` — pydantic coerces it at the request-params model boundary
        (live-verified).
        """
        await self.initialize()
        require_capability(self, "resources")
        try:
            result = await self._client.read_resource(uri)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/read error: {exc}")
        return _read_resource_result_to_dict(result)

    # ── prompts (#2597 slice ②c — consumption) ──────────────────────────────────

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Return the prompts advertised by this server as plain dicts.

        Mirrors :meth:`list_resources`: paginates via
        :meth:`_paginate_official_sdk` (follows ``next_cursor``) and gates
        on the ``"prompts"`` capability before issuing the request.
        """
        await self.initialize()
        require_capability(self, "prompts")
        try:
            prompts = await self._paginate_official_sdk(self._client.list_prompts, "prompts")
        except Exception as exc:
            _classify_and_raise(exc, f"MCP prompts/list error: {exc}")
        return [_prompt_to_dict(p) for p in prompts]

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch one rendered prompt's messages and return them flattened to a
        dict: ``{"description": str | None, "messages": [...]}`` — each entry
        a flattened ``PromptMessage``.

        ``ClientSession.get_prompt`` takes the same ``name``/``arguments``
        shape and returns the full ``GetPromptResult`` — the
        shape-flattening lives in ONE place
        (:func:`_get_prompt_result_to_dict`), mirroring how
        :meth:`read_resource` flattens ``ReadResourceResult`` for the
        same reason.
        """
        await self.initialize()
        require_capability(self, "prompts")
        try:
            result = await self._client.get_prompt(name=name, arguments=arguments)
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

        **#3698/#4282 re-measurement**: this declaration was always reading
        ``mcp.types.ServerCapabilities``/``ResourcesCapability`` directly —
        the official SDK's own types, not a fastmcp-shaped projection.
        ``self._server_capabilities`` is populated from ``ClientSession.
        initialize()``'s ``InitializeResult.capabilities`` on every
        transport now (#4282 closed the last path — OAuth-configured http —
        that instead read it off fastmcp's ``Client.initialize_result.
        capabilities``, same underlying SDK type). No code or behavior
        change; re-confirmed by re-reading the population code.
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

        ``self._client`` IS the ``ClientSession`` directly (#4282: every
        transport, OAuth included), so ``subscribe_resource`` is called on
        it directly — no ``.session`` indirection needed (that was
        fastmcp's own ``Client`` exposing it only via ``Client.session``,
        never on ``Client`` itself). The notification itself carries no
        payload (just ``uri``) — callers re-read the resource to see the
        new content; see :mod:`reyn.mcp.message_handler`'s
        ``on_resource_updated`` for the EventLog bridge.
        """
        await self.initialize()
        require_capability(self, "resources")
        self._require_resources_subscribe_capability()
        try:
            await self._client.subscribe_resource(uri)
        except Exception as exc:
            _classify_and_raise(exc, f"MCP resources/subscribe error: {exc}")

    async def unsubscribe_resource(self, uri: str) -> None:
        """Unsubscribe from server-pushed updates for ``uri``. Same gating as
        :meth:`subscribe_resource`; mirrors it via
        ``mcp.ClientSession.unsubscribe_resource``."""
        await self.initialize()
        require_capability(self, "resources")
        self._require_resources_subscribe_capability()
        try:
            await self._client.unsubscribe_resource(uri)
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
        exit_stack = self._exit_stack
        self._client = None
        self._exit_stack = None
        self._initialized = False
        # #2597 capability/version gate: a closed client re-negotiates on the next
        # initialize() (or duck-typed callers who happen to keep querying supports()
        # on a closed client should see the conservative False, not stale state from
        # the old connection).
        self._negotiated_version = None
        self._server_capabilities = None
        try:
            # #4282: every transport (stdio, http/sse with or without OAuth)
            # opens via self._exit_stack now — the official SDK's transport
            # + ClientSession, entered as two separate async context
            # managers (see _initialize_stdio / _initialize_http_or_sse) —
            # closing means exiting THAT stack. ClientSession itself has no
            # .close() method; only __aexit__ via the stack it was entered
            # through. exit_stack is always set alongside self._client (both
            # init paths set them together), so this is no longer a branch.
            assert exit_stack is not None  # narrows the type; see comment above
            await exit_stack.aclose()
        except (Exception, asyncio.CancelledError):
            # Best-effort graceful cleanup; transport may already be down. The
            # belt-and-suspenders reap in the finally still terminates the OS
            # subprocess even when this graceful path raised (the #2714 guard).
            # #3698 stage 1: asyncio.CancelledError joins Exception here
            # (Python 3.8+ makes it a BaseException, not an Exception
            # subclass) — measured live: the official SDK's stdio_client
            # exits via anyio.create_task_group(), whose __aexit__ can
            # surface a CancelledError from an in-flight subprocess-wait
            # when the CALLER's own task is being torn down concurrently
            # (tests/mcp/test_2597_s2a_mcp_connection_service.py's pool
            # teardown hit this). This is the SAME class of teardown fault
            # the surrounding docstring already documents tolerating for
            # Windows (BrokenResourceError/BaseExceptionGroup) — the
            # finally-reap below is what actually guarantees the child dies,
            # not this try succeeding.
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
        of #2978 the Seatbelt backend emits the deny-list(s) AFTER the write
        grants (SBPL is last-match-wins), so a broad write grant no longer
        nullifies the sensitive-path deny-list — the deny wins and a
        ``sandbox_policy_narrowed`` audit-event is recorded. #3901 PR-B ④ split
        that into two independent fields — ``read_deny_paths`` denies only the
        READ axis, ``write_deny_paths`` only the WRITE axis (previously
        coupled as an undocumented Seatbelt side-effect of ``read_deny_paths``
        alone) — so THIS caller sets BOTH to the same sensitive-path set: an
        MCP server is untrusted third-party code, so an engulfing write grant
        must not leave a credential path writable just because only the read
        axis was protected. The shipped defaults are nonetheless kept
        mechanically disjoint from every path in
        ``DEFAULT_SENSITIVE_READ_DENY`` (pinned by a falsification test) so an
        MCP server never trips that narrowing in the first place.
        """
        from reyn.security.sandbox import SandboxPolicy
        from reyn.security.sandbox.policy import (
            DEFAULT_SANDBOX_NETWORK,
            DEFAULT_SENSITIVE_READ_DENY,
        )

        cwd = self._config.get("cwd") or os.getcwd()
        declared = self._config.get("write_paths")
        extra: tuple[str, ...] | list[str]
        if declared is not None:
            extra = [str(p) for p in declared]
        else:
            extra = _default_runtime_write_paths(self._config.get("command") or "")
        return SandboxPolicy(
            network=bool(self._config.get("network", DEFAULT_SANDBOX_NETWORK)),
            deny_subprocess=not bool(self._config.get("subprocess", True)),
            # ``~`` in an operator-declared or default path is expanded by the
            # backend (expand_policy_path) — NOT here, so every backend applies
            # one shared contract instead of each caller pre-expanding (#2976).
            write_paths=[cwd, *extra],
            # #3901 PR-B ④ (owner ruling B): SandboxPolicy's own dataclass
            # defaults for read_deny_paths/write_deny_paths are now empty
            # (full compat) — but an MCP server is untrusted THIRD-PARTY code,
            # not an operator-typed command, so this builder opts back into
            # the credential-path defense-in-depth explicitly rather than
            # inheriting the compat floor. This is the "read broad + the
            # default sensitive deny-list" this method's own docstring
            # promises; before this line it was true only because the
            # dataclass default carried it for free.
            #
            # BOTH axes, not just read: PR-B ③ split what was a single
            # Seatbelt side-effect (a read_deny_paths entry also denied
            # writes, undocumented) into two independent fields. Setting only
            # read_deny_paths here would leave an engulfing write_paths grant
            # able to WRITE a credential path even with its read denied —
            # exactly the shape #2978 exists to prevent, now requiring both
            # fields since the two axes no longer move together.
            read_deny_paths=list(DEFAULT_SENSITIVE_READ_DENY),
            write_deny_paths=list(DEFAULT_SENSITIVE_READ_DENY),
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
        loud warning AND a ``sandbox_policy_not_applied`` audit-event (#3821),
        so the fallback is legible after the fact and not only to whoever was
        watching stderr at the time.

        The audit-event needs a sink, and only the held-connection path
        (:class:`~reyn.mcp.connection_service.MCPConnectionService`) has one.
        Constructed WITHOUT ``emit_event`` — the ephemeral
        :class:`~reyn.mcp.pool.MCPClientPool` path, and direct callers — the
        fallback is WARNING-only, exactly as it was before #3821. So "never
        silently unsandboxed" is true of the warning on every path, and of the
        audit trail only where a sink was wired.

        #3848: ``wrapped.env`` (the allowlisted env ``wrap_command()``
        computes, #3850) is deliberately NOT carried into the launch here —
        an earlier stage held it for a planned stage 2 that never landed
        (owner ruling, #3848's closing comment). MCP stdio's actual launch
        (``_open_stdio``) passes ``env=None`` when the operator's own
        per-server config doesn't set one, and the OFFICIAL ``mcp`` SDK fills
        that with its own narrow ``DEFAULT_INHERITED_ENV_VARS`` allowlist
        (``HOME``/``LOGNAME``/``PATH``/``SHELL``/``TERM``/``USER``) — this is
        CORRECT and deliberately different from reyn's own sandbox path
        (``resolve_passthrough_env``, which passes everything by default,
        owner ruling B): the trust relationship is not the same. reyn's own
        sandbox path launches a command on the OPERATOR's behalf — the same
        trust level as the operator typing it into their own shell, so
        "pass everything" is correct there. MCP stdio launches a THIRD
        PARTY's server program — the SDK limiting what that program inherits
        is the SDK correctly exercising its OWN responsibility, and reyn
        substituting a wider allowlist (or reyn's own "pass everything")
        would be reyn overriding a decision that isn't reyn's to make. Do
        not apply ruling B here — the two paths differ on WHO is trusted,
        not on what mechanism sandboxes them.
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
            if self._emit_event is not None:
                try:
                    self._emit_event(
                        "sandbox_policy_not_applied",
                        # #3821: the kind's other producer (hooks/shell_runner)
                        # reports ONE refused axis and carries ``policy_field``.
                        # Here the whole policy failed to apply, so ``scope``
                        # tells a subscriber which producer it is holding
                        # instead of leaving it to infer that from a missing key.
                        scope="mcp_stdio",
                        server=self._server_name or "<unknown>",
                        command=command,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                except Exception as emit_exc:  # noqa: BLE001 — telemetry is best-effort
                    logger.debug(
                        "MCP stdio %r: sandbox_policy_not_applied emit failed: %s",
                        command,
                        emit_exc,
                    )
            return command, args

        self._sandbox_cleanup = wrapped.cleanup
        return wrapped.argv[0], list(wrapped.argv[1:])

    def _is_non_interactive(self) -> bool:
        """Resolve the effective headless/non-interactive posture for the
        #2597 slice ④ OAuth pre-flight check (see :meth:`_build_oauth_provider`).

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

    async def _build_oauth_provider(self, url: str) -> "Any":
        """Build the official SDK's ``mcp.client.auth.OAuthClientProvider``
        for ``self._config["auth"]``, or return None if this server config
        carries no ``auth`` key at all (the pre-④ static-bearer-via-
        ``headers`` path, unchanged). #4282: replaces the retired
        ``_build_oauth_auth`` (which built fastmcp's ``OAuth`` object) — the
        validation contract below is unchanged from it.

        See the module docstring's "OAuth 2.1" section for the full
        contract this implements. Raises :class:`MCPError` eagerly (this
        module's existing lazy-validate-at-connect-time posture, same as
        the ``type``/``url`` checks above) for: a non-``oauth`` ``auth``
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
        # this method is only ever reached via _initialize_http_or_sse's
        # http branch, so self._type is guaranteed "http" here.

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

        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
        from pydantic import AnyHttpUrl

        from reyn.mcp.oauth_browser_flow import (
            find_available_port,
            redirect_handler,
            run_callback_server,
        )

        storage = MCPOAuthTokenStorage(url)

        scopes = auth_cfg.get("scopes")
        if isinstance(scopes, list):
            scope_str = " ".join(scopes)
        elif scopes:
            scope_str = str(scopes)
        else:
            scope_str = ""

        port = find_available_port()
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        client_metadata = OAuthClientMetadata(
            client_name="reyn",
            redirect_uris=[AnyHttpUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=scope_str,
        )

        # Static client_id (skip Dynamic Client Registration) — mirrors
        # fastmcp's own OAuth._bind's static-client-info shortcut. Pre-
        # seeding storage.set_client_info() BEFORE constructing the
        # provider works because OAuthClientProvider only runs DCR when
        # ``self.context.client_info`` is still None after ``_initialize()``
        # loads it via ``storage.get_client_info()`` (verified by reading
        # ``mcp/client/auth/oauth2.py``'s async_auth_flow directly: "Step 4:
        # Register client or use URL-based client ID" is gated on
        # ``if not self.context.client_info:``).
        client_id = auth_cfg.get("client_id")
        client_secret = auth_cfg.get("client_secret")
        if client_id:
            metadata_dict = client_metadata.model_dump(exclude_none=True)
            metadata_dict.setdefault(
                "token_endpoint_auth_method",
                "client_secret_post" if client_secret else "none",
            )
            static_info = OAuthClientInformationFull(
                client_id=client_id, client_secret=client_secret, **metadata_dict,
            )
            await storage.set_client_info(static_info)

        callback_timeout = float(auth_cfg.get("callback_timeout", 300.0))

        async def _callback_handler() -> tuple[str, str | None]:
            return await run_callback_server(
                host="127.0.0.1", port=port, timeout=callback_timeout,
            )

        return OAuthClientProvider(
            server_url=url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=_callback_handler,
        )


# ── helpers ──────────────────────────────────────────────────────────────────

def _adapt_elicitation_handler(fastmcp_shaped_handler: Any) -> Any:
    """#3698 stage 1: adapt reyn's own ``(message, response_type, params,
    context) -> ...`` elicitation handler (``reyn.mcp.elicitation.
    build_elicitation_handler``, built to fastmcp's
    ``fastmcp.client.elicitation.ElicitationHandler`` calling convention) to
    the official SDK's ``ElicitationFnT`` — ``(context, params) ->
    ElicitResult | ErrorData``, TWO positional args in the OPPOSITE order.

    Measured before writing this: reyn's own handler body never reads
    ``response_type`` past its signature declaration (grepped — zero uses;
    the module docstring itself says it's "only used as the None-vs-not-None
    signal for does this elicitation carry a schema at all", and the handler
    body already gets that from ``isinstance(params, ElicitRequestFormParams)``
    directly) — so passing ``None`` here loses nothing. ``message`` is
    ``params.message`` on every real ``ElicitRequestParams`` shape (form or
    URL) the official SDK sends.
    """
    async def _adapted(context: Any, params: Any) -> Any:
        return await fastmcp_shaped_handler(
            getattr(params, "message", ""), None, params, context,
        )

    return _adapted


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
    # #4412 pin-bump PR: CallToolResult's own fields are snake_case on 2.0
    # (`is_error`/`structured_content`, confirmed live via model_fields) —
    # was camelCase on 1.x. reyn's OUTPUT dict keys below stay `isError`/
    # `structuredContent` (reyn's own external contract, asserted on by
    # callers/tests) — only the SDK object attribute names being READ change.
    return {
        "content": content_items,
        "isError": bool(getattr(result, "is_error", False)),
        "structuredContent": getattr(result, "structured_content", None),
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
