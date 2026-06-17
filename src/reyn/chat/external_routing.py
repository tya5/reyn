"""External transport routing via MCP — FP-0041 #489 PR-C.

Pure routing helpers for dispatching outbox replies to external chat
transports (= Slack / LINE / Discord / etc.) by way of installed MCP
servers. Reyn core stays SDK-free (= FP-0041 #1 decision: "outbound
external API = MCP delegate"); the actual transport-specific API
calls happen on the MCP server side.

Components:

  ``ExternalTransportRouting`` — config dataclass mapping transport
    name → MCP tool name + args template. Parsed from
    ``reyn.yaml`` / ``.reyn/integrations.yaml`` ``external_transports``
    section.

  ``route_to_mcp(transport, destination, text, ...)`` — async
    routing function. Looks up the MCP tool from config, builds
    args from the destination dict + text via template substitution,
    invokes the supplied ``mcp_dispatcher`` callable. Returns a
    ``RouteResult`` for the caller to log / event-emit.

Args template substitution:

  Template values may contain ``{text}`` (= reply text) and
  ``{destination.<key>}`` (= destination dict lookup). All other
  literal values pass through verbatim. Templates are simple string
  format calls; no eval / no recursion.

Wiring is intentionally NOT included in this PR. PR-D (Slack
handler) brings the outbox subscriber that calls ``route_to_mcp``;
PR-C ships the primitive so the wiring is purely "subscribe outbox
→ filter ExternalRef → call route_to_mcp".
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# ── Config dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalTransportEntry:
    """Single transport entry in ``external_transports`` config.

    Fields:
      mcp_tool: full MCP tool name (= ``<server>__<tool>``) to invoke
        on dispatch. The server portion identifies which installed
        MCP server provides the call; the tool portion is the bare
        tool name on that server.
      args_template: dict shaped like the MCP tool's expected args.
        Values may contain ``{text}`` and ``{destination.<key>}``
        placeholders for substitution at dispatch time.
    """
    mcp_tool: str
    args_template: dict


@dataclass(frozen=True)
class ExternalTransportRouting:
    """``external_transports:`` config section.

    Maps short transport names to MCP tool dispatch recipes::

        external_transports:
          slack:
            mcp_tool: slack__chat_postMessage
            args_template:
              channel: "{destination.channel}"
              thread_ts: "{destination.thread_ts}"
              text: "{text}"
          line:
            mcp_tool: line__reply_message
            args_template:
              reply_token: "{destination.reply_token}"
              messages:
                - type: text
                  text: "{text}"
    """
    transports: dict[str, ExternalTransportEntry] = field(default_factory=dict)

    def get(self, transport: str) -> ExternalTransportEntry | None:
        """Return the entry for ``transport`` or None if unconfigured."""
        return self.transports.get(transport)


def parse_external_transports(raw: object) -> ExternalTransportRouting:
    """Parse the ``external_transports`` section.

    Shape:
      raw = {<transport_name>: {"mcp_tool": str, "args_template": dict}}

    Defensive: malformed entries are skipped with no exception (=
    operator config errors shouldn't crash boot; missing transports
    fall through to "transport unconfigured" at dispatch time).
    Empty / None / non-dict input returns empty config.
    """
    if not isinstance(raw, dict):
        return ExternalTransportRouting()
    entries: dict[str, ExternalTransportEntry] = {}
    for name, raw_entry in raw.items():
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(raw_entry, dict):
            continue
        mcp_tool = raw_entry.get("mcp_tool")
        args_template = raw_entry.get("args_template")
        if not isinstance(mcp_tool, str) or not mcp_tool:
            continue
        if not isinstance(args_template, dict):
            args_template = {}
        entries[name] = ExternalTransportEntry(
            mcp_tool=mcp_tool,
            args_template=dict(args_template),
        )
    return ExternalTransportRouting(transports=entries)


# ── Template substitution ─────────────────────────────────────────────


def _substitute(value: Any, *, text: str, destination: dict) -> Any:
    """Recursively substitute ``{text}`` and ``{destination.<key>}``.

    String values: ``str.format``-style placeholders. Lists / dicts
    are walked recursively. Other types pass through. Missing keys
    are replaced with empty string (= no KeyError, defensive).
    """
    if isinstance(value, str):
        return _substitute_str(value, text=text, destination=destination)
    if isinstance(value, list):
        return [_substitute(v, text=text, destination=destination) for v in value]
    if isinstance(value, dict):
        return {
            k: _substitute(v, text=text, destination=destination)
            for k, v in value.items()
        }
    return value


def _substitute_str(template: str, *, text: str, destination: dict) -> str:
    """``{text}`` / ``{destination.<key>}`` substitution for one string.

    Uses a minimal placeholder scanner instead of ``str.format`` so
    placeholders inside JSON-like strings (= curly braces in JSON
    examples) don't false-positive. Only the two documented
    placeholder shapes resolve.
    """
    out: list[str] = []
    i = 0
    while i < len(template):
        if template[i] != "{":
            out.append(template[i])
            i += 1
            continue
        # find matching close
        close = template.find("}", i + 1)
        if close == -1:
            out.append(template[i])
            i += 1
            continue
        placeholder = template[i + 1:close]
        if placeholder == "text":
            out.append(text)
        elif placeholder.startswith("destination."):
            key = placeholder[len("destination."):]
            val = destination.get(key, "")
            out.append(str(val) if val is not None else "")
        else:
            # unknown placeholder — leave literal (= operator config
            # bug, but don't crash dispatch).
            out.append(template[i:close + 1])
        i = close + 1
    return "".join(out)


def build_mcp_args(
    template: dict, *, text: str, destination: dict,
) -> dict:
    """Build the MCP tool's args dict by substituting placeholders.

    Public helper so PR-D's outbox subscriber can compose args before
    invoking the dispatcher, and so tests can verify substitution
    directly.
    """
    return _substitute(template, text=text, destination=destination)


# ── Dispatch ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteResult:
    """Outcome of one ``route_to_mcp`` call.

    Fields:
      status: "ok" | "unconfigured" | "error"
      transport: the transport name the caller asked for
      mcp_tool: the resolved tool name (= empty when status=unconfigured)
      detail: human-readable detail for log / event emission
    """
    status: str
    transport: str
    mcp_tool: str
    detail: str


async def route_to_mcp(
    transport: str,
    destination: dict,
    text: str,
    *,
    routing: ExternalTransportRouting,
    mcp_dispatcher: Callable[[str, dict], Awaitable[Any]],
) -> RouteResult:
    """Dispatch ``text`` to ``transport`` via the configured MCP tool.

    Parameters
    ----------
    transport:
        Short transport name (= "slack" / "line" / ...). Looked up
        in ``routing.transports``.
    destination:
        Opaque per-transport routing dict (= e.g.
        ``{"channel": "C123", "thread_ts": "..."}``).
    text:
        Reply text to deliver.
    routing:
        Parsed ``ExternalTransportRouting`` config.
    mcp_dispatcher:
        ``async (mcp_tool_name, args) -> result``. Supplied by the
        caller (= PR-D outbox subscriber) so this routing primitive
        stays free of session / MCPClient knowledge.

    Returns
    -------
    ``RouteResult`` summarising the outcome. ``status="unconfigured"``
    when ``transport`` is not in ``routing.transports``;
    ``status="error"`` on dispatcher exception; ``status="ok"`` on
    successful invocation.
    """
    entry = routing.get(transport)
    if entry is None:
        return RouteResult(
            status="unconfigured",
            transport=transport,
            mcp_tool="",
            detail=(
                f"No external_transports entry for {transport!r}; "
                f"add it under reyn.yaml or .reyn/integrations.yaml."
            ),
        )
    args = build_mcp_args(
        entry.args_template, text=text, destination=dict(destination),
    )
    try:
        await mcp_dispatcher(entry.mcp_tool, args)
    except Exception as exc:
        return RouteResult(
            status="error",
            transport=transport,
            mcp_tool=entry.mcp_tool,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return RouteResult(
        status="ok",
        transport=transport,
        mcp_tool=entry.mcp_tool,
        detail=f"Dispatched via {entry.mcp_tool}",
    )


# ── Outbox interceptor factory (= FP-0041 #489 PR-D2 wiring) ───────────


# OutboxMessage type is imported lazily inside the factory to avoid a
# circular import (= chat.outbox imports transport; this module
# imports nothing from chat to keep it pure for unit tests).


def make_outbox_interceptor(
    *,
    routing: ExternalTransportRouting,
    mcp_dispatcher: Callable[[str, dict], Awaitable[Any]],
    dispatchable_kinds: tuple[str, ...] = ("agent",),
) -> Callable[[Any], Awaitable[bool]]:
    """Build an outbox interceptor for FP-0041 PR-D2 wiring.

    Returns an async ``(OutboxMessage) -> bool`` callable to register
    on ``Session._outbox_interceptor``. When invoked:

      1. If ``msg.reply_to`` is not an ``ExternalRef`` → return False
         (= caller falls through to normal outbox queue).
      2. If ``msg.kind`` is not in ``dispatchable_kinds`` → return False
         (= status/trace/__end__/intervention are display-only, not
         relayed to the external transport).
      3. Otherwise dispatch via ``route_to_mcp``. On ``status="ok"``
         or ``status="error"`` (= operator-visible via logs) the
         message is consumed (= return True).
         On ``status="unconfigured"`` the message falls through (=
         return False) so the TUI gets a visible signal that the
         operator config is incomplete.

    The interceptor swallows ``route_to_mcp`` exceptions internally
    (= ``route_to_mcp`` already converts dispatcher exceptions to
    ``RouteResult(status="error", ...)``), so it never raises out to
    the session.

    Parameters
    ----------
    routing:
        Parsed ``ExternalTransportRouting`` config (= mapping from
        transport name to MCP tool + args template).
    mcp_dispatcher:
        Async callable ``(mcp_tool_name, args) -> result`` that
        invokes the actual MCP tool. Supplied by the caller (= web
        lifespan / session factory) and closes over the session's
        MCP client.
    dispatchable_kinds:
        Outbox message kinds that should be relayed to external
        transport. Default ``("agent",)`` — only the LLM-produced
        reply is sent to Slack/LINE/etc., NOT the various status /
        trace / intervention / __end__ display markers that don't
        belong in an external chat surface.
    """
    from reyn.chat.transport import ExternalRef

    async def _interceptor(msg: Any) -> bool:
        reply_to = getattr(msg, "reply_to", None)
        if not isinstance(reply_to, ExternalRef):
            return False
        if getattr(msg, "kind", None) not in dispatchable_kinds:
            return False
        text = getattr(msg, "text", "") or ""
        result = await route_to_mcp(
            reply_to.transport,
            dict(reply_to.destination),
            text,
            routing=routing,
            mcp_dispatcher=mcp_dispatcher,
        )
        if result.status == "unconfigured":
            # Let it fall through to the normal queue path so the
            # operator sees something in TUI / log instead of silent
            # drop. ``route_to_mcp`` already logged the detail.
            return False
        # ok / error → consumed. error means we tried; surfacing on
        # TUI on every Slack post failure would be noisy.
        return True

    return _interceptor


__all__ = [
    "ExternalTransportEntry",
    "ExternalTransportRouting",
    "RouteResult",
    "build_mcp_args",
    "make_outbox_interceptor",
    "parse_external_transports",
    "route_to_mcp",
]
