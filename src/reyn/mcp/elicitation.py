"""MCP elicitation — server->client structured-input requests (#2597 slice ③).

An MCP server can send ``elicitation/create`` mid-session to ask the human a
structured question (a flat, primitive-only JSON-object schema per the
2025-11-25 spec — string / number / integer / boolean / enum; NO nesting).
FastMCP's ``fastmcp.Client(elicitation_handler=...)`` (verified against the
installed fastmcp 3.4.2, ``fastmcp/client/elicitation.py``) is the
capability-declaring seam: passing a handler is what causes FastMCP to
declare the ``elicitation`` client capability during the initialize
handshake. The handler signature (verified against
``fastmcp.client.elicitation.ElicitationHandler``)::

    async def handler(
        message: str,
        response_type: type[T] | None,   # a dataclass FastMCP built from the
                                          # server's JSON schema, or None for a
                                          # no-data / URL-based elicitation
        params: mcp.types.ElicitRequestParams,  # carries the RAW requestedSchema
        context: RequestContext[ClientSession, LifespanContextT],
    ) -> T | dict[str, Any] | ElicitResult[T | dict[str, Any]]

FastMCP auto-converts the JSON schema to ``response_type`` but ERASES the
per-field metadata (enum choices, descriptions) needed to prompt one field at
a time — so this module reads field specs from ``params.requestedSchema``
(the raw JSON Schema dict) directly, and only uses ``response_type`` as the
None-vs-not-None signal for "does this elicitation carry a schema at all".

D2 — receive-loop dispatch (verified by reading the installed mcp SDK):
``mcp.shared.session.BaseSession._receive_loop`` awaits
``ClientSession._received_request`` INLINE for every incoming server request,
and ``_received_request`` awaits the installed ``_elicitation_callback``
INLINE too (``mcp/client/session.py``) — no per-request task is spawned by the
SDK. So awaiting a human answer here blocks THAT server's receive loop for as
long as the wait takes (bounded by ``timeout_seconds`` below) — other
notifications/responses on the SAME connection queue behind it. Since each
held MCP connection (:class:`~reyn.mcp.connection_service.MCPConnectionService`)
owns its own ``fastmcp.Client``/``ClientSession`` with its own receive-loop
task, this block is contained to the ONE server connection that asked the
question — every other held server connection (and the rest of reyn) keeps
running. Cleanly spinning the wait onto a separate asyncio Task would not
avoid the block: the SDK requires this callback's coroutine to complete
(with a ``ClientResult``/``ErrorData``) before it will process the NEXT
message on this connection's read stream at all, so a fire-and-forget task
here would just mean the elicitation response is never sent. Per the design
doc (D2), this bounded per-connection block is ACCEPTABLE — never faked with
an early return.

D3 — timeout semantics: no answer within ``timeout_seconds`` (default 120,
per-server override via server config key ``elicitation_timeout_seconds``) ->
``action="cancel"`` (nobody judged the request). The human explicitly
declining (via the accept/decline gate prompt, or a per-field sensitive-value
confirmation) -> ``action="decline"``.

D4 — headless (no attached intervention listener): auto-decline (never
cancel — ``decline`` signals "don't retry the same way", matching reyn's
no-blocking-AskUser-in-autonomous-mode principle) + emit
``mcp_elicitation_auto_declined``. Per-server override via server config key
``elicitation: "prompt" | "auto_decline"`` (default ``"prompt"`` — i.e. try
the bus when one is available; ``"auto_decline"`` always declines even with a
live listener, for an operator who wants a server's elicitations silenced
outright).

D5 — security (all 5, see :func:`_attributed_message` / :func:`_is_sensitive_field`
/ :func:`build_elicitation_handler`):
  1. every prompt is prefixed with clear server-attribution.
  2. a field whose name/description matches password|token|key|secret|credential
     gets an extra yes/no confirmation + an explicit "sent to server '<name>'"
     warning before the free-text prompt.
  3. answers are human-typed ONLY — this module never reads env vars / secrets
     to prefill a field.
  4. audit events record the server name + the schema's field KEY names, never
     the field VALUES (a user's answer content never appears in an emitted event).
  5. the message is rendered as plain text (``UserIntervention.prompt`` is
     never markdown/HTML-interpreted) and length-capped (:data:`_MAX_MESSAGE_LEN`)
     as a prompt-injection defense — an oversized server message is truncated
     before it ever reaches the human-facing prompt.

D6 — capability: :meth:`build_elicitation_handler` is installed unconditionally
by every HELD connection (:mod:`reyn.mcp.connection_service`) — the "is there a
human" branch lives INSIDE the handler (this module), not at wiring time, so
the ``elicitation`` capability is always declared and every server always gets
an answer of some kind (prompt-and-wait, or a bus-less auto-decline).

D1 — structured response, sequential per-field prompting: a multi-field flat
schema is answered ONE FIELD AT A TIME (never crammed into a single message +
free-text-parsed), preceded by ONE accept/decline gate prompt so the human can
refuse the whole exchange before answering anything. bool -> yes/no choice;
enum -> a choice per enum value; string/number/integer -> free text (best-
effort ``int``/``float`` coercion — a coercion failure falls back to the raw
string; reyn does not re-implement full JSON-Schema validation client-side,
the server is the source of truth for that).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.intervention_choices import ACCEPT, elicitation_gate_choices
from reyn.user_intervention import InterventionChoice, UserIntervention

if TYPE_CHECKING:
    from reyn.user_intervention import RequestBus

logger = logging.getLogger(__name__)

# #2597 slice ③ D3: default per-elicitation deadline (seconds). Per-server
# override: server config key ``elicitation_timeout_seconds``.
DEFAULT_ELICITATION_TIMEOUT_SECONDS = 120.0

# #2597 slice ③ D5.5: prompt-injection defense — an oversized server-authored
# message is truncated before it is ever rendered to the human.
_MAX_MESSAGE_LEN = 1500

# #2597 slice ③ D5.2: substring match (case-insensitive) against a field's
# name OR JSON-schema description. Intentionally broad (over-triggers on e.g.
# "primary_key") — the cost of a false positive is one extra confirmation
# step, not a blocked feature; the cost of a false negative is a credential
# silently typed into an untrusted MCP server's field.
_SENSITIVE_KEYWORDS = ("password", "token", "key", "secret", "credential")

# Bus resolver: called at request time (mirrors the deferred-lambda pattern
# already used for emit_sink/hook_trigger in MCPConnectionService/session.py).
# Returns None when no live intervention listener is attached (headless).
ElicitationBusResolver = Callable[[], "RequestBus | None"]

EmitSink = Callable[..., Any]


def _attributed_message(server_name: str, message: str) -> str:
    """D5.1 — prefix every elicitation prompt with unambiguous server
    attribution so the human never mistakes a server's question for reyn's
    own. D5.5 — length-capped BEFORE the prefix is added (the prefix itself
    is reyn-authored, not attacker-controlled, so it is never truncated)."""
    body = message if len(message) <= _MAX_MESSAGE_LEN else (
        message[:_MAX_MESSAGE_LEN] + "...(truncated)"
    )
    return f"⚠️ MCP server {server_name!r} asks (this is NOT reyn): {body}"


def _field_prefix(server_name: str) -> str:
    """D5.1 — per-field prompts carry a SHORT server-attribution prefix (the
    gate prompt above already carries the full disclaimer; repeating it
    verbatim on every one of N sequential field prompts would be noisy) so a
    human who scrolls directly to a field prompt (e.g. TUI scrollback, past
    the gate) still sees which server is asking BEFORE typing an answer."""
    return f"[MCP server {server_name!r}] "


def _is_sensitive_field(name: str, description: str | None) -> bool:
    haystack = f"{name} {description or ''}".lower()
    return any(kw in haystack for kw in _SENSITIVE_KEYWORDS)


def _schema_fields(requested_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``requestedSchema["properties"]`` (a flat, primitives-only JSON
    object per the MCP spec — verified against the 2025-11-25 spec's
    elicitation section: no nested object/array properties are permitted) into
    an ordered list of per-field specs. Order follows the schema's own
    declaration order (Python dicts + FastMCP's own schema builder both
    preserve insertion order) — this is presentation sequencing, not a pinned
    algorithmic detail a test should assert on."""
    properties = requested_schema.get("properties") or {}
    required = set(requested_schema.get("required") or [])
    out = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        out.append({
            "name": name,
            "type": spec.get("type", "string"),
            "enum": spec.get("enum"),
            "description": spec.get("description"),
            "required": name in required,
        })
    return out


def _coerce(value: str, field_type: str) -> Any:
    """Best-effort scalar coercion for the free-text path (string/number/
    integer). A coercion failure falls back to the raw string — reyn is not
    re-implementing JSON-Schema validation client-side; the server validates
    the final answer and is the source of truth for rejecting a bad value."""
    if field_type == "integer":
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return value
    if field_type == "number":
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return value
    if field_type == "boolean":
        return value.strip().lower() in ("true", "yes", "y", "1")
    return value


def _enum_choices(values: list[Any]) -> list[InterventionChoice]:
    return [
        InterventionChoice(id=str(v), label=f"[{i + 1}] {v}", hotkey=str(i + 1))
        for i, v in enumerate(values)
    ]


def _bool_choices() -> list[InterventionChoice]:
    return [
        InterventionChoice(id="true", label="[y]es", hotkey="y"),
        InterventionChoice(id="false", label="[n]o", hotkey="n"),
    ]


class _Cancelled(Exception):
    """Internal signal: the elicitation deadline elapsed mid-flow."""


class _Declined(Exception):
    """Internal signal: the human explicitly declined mid-flow."""


def build_elicitation_handler(
    *,
    server_name: str,
    bus_resolver: "ElicitationBusResolver",
    emit_sink: "EmitSink | None" = None,
    timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS,
    mode: str = "prompt",
) -> Callable[..., Awaitable[Any]]:
    """Build a FastMCP-shaped ``elicitation_handler`` bound to ``server_name``.

    ``bus_resolver`` is called ONCE per elicitation (at request time, not at
    build time) — mirrors ``MCPConnectionService``'s deferred-lambda pattern
    for ``emit_sink``/``hook_trigger`` — so the "is a human attached right
    now" check (:meth:`InterventionRegistry.has_active_listener`, the SAME
    gate #2095's shell-hook consent uses) is re-evaluated fresh on every call,
    not frozen at connection-open time.

    ``mode``: ``"prompt"`` (default) tries the bus when one is available;
    ``"auto_decline"`` (per-server config override ``elicitation:
    auto_decline``) always auto-declines, even with a live listener attached.
    """
    from fastmcp.client.elicitation import ElicitResult
    from mcp.types import ElicitRequestFormParams

    async def _emit(event: str, **fields: Any) -> None:
        if emit_sink is None:
            return
        try:
            emit_sink(event, server=server_name, **fields)
        except Exception:  # noqa: BLE001 — observability must never break the handler
            logger.warning("mcp elicitation: emit_sink raised for %r", event, exc_info=True)

    async def _ask(bus: "RequestBus", iv: UserIntervention, deadline: float):
        import asyncio

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Cancelled()
        try:
            return await asyncio.wait_for(bus.request(iv), timeout=remaining)
        except TimeoutError:
            raise _Cancelled() from None

    async def handler(
        message: str,
        response_type: "type | None",
        params: Any,
        context: Any,
    ) -> Any:
        field_keys: list[str] = []
        if isinstance(params, ElicitRequestFormParams):
            field_keys = sorted((params.requestedSchema.get("properties") or {}).keys())

        await _emit("mcp_elicitation_requested", field_keys=field_keys)

        if mode == "auto_decline":
            await _emit("mcp_elicitation_auto_declined", field_keys=field_keys, reason="server_configured")
            return ElicitResult(action="decline")

        bus = bus_resolver()
        if bus is None:
            # D4: headless — no live intervention listener attached. Never
            # cancel (cancel means "nobody judged it" and invites a retry
            # loop) — decline, so the server sees a definitive "no".
            await _emit("mcp_elicitation_auto_declined", field_keys=field_keys, reason="headless")
            return ElicitResult(action="decline")

        deadline = time.monotonic() + timeout_seconds
        attributed = _attributed_message(server_name, message)

        try:
            field_specs = (
                _schema_fields(params.requestedSchema)
                if isinstance(params, ElicitRequestFormParams)
                else []
            )

            if not field_specs:
                # No-data elicitation (empty-properties schema, or a
                # non-form/URL request): a single accept/decline gate is the
                # whole exchange.
                gate = UserIntervention(
                    kind="mcp_elicitation",
                    prompt=attributed,
                    choices=elicitation_gate_choices(),
                )
                answer = await _ask(bus, gate, deadline)
                if answer.choice_id != ACCEPT:
                    raise _Declined()
                await _emit("mcp_elicitation_answered", field_keys=field_keys, action="accept")
                return ElicitResult(action="accept", content={})

            field_summary = ", ".join(f["name"] for f in field_specs)
            gate = UserIntervention(
                kind="mcp_elicitation",
                prompt=attributed,
                detail=f"Will ask for: {field_summary}" if len(field_specs) > 1 else "",
                choices=elicitation_gate_choices(),
            )
            gate_answer = await _ask(bus, gate, deadline)
            if gate_answer.choice_id != ACCEPT:
                raise _Declined()

            content: dict[str, Any] = {}
            for spec in field_specs:
                name = spec["name"]
                sensitive = _is_sensitive_field(name, spec["description"])
                if sensitive:
                    warn_iv = UserIntervention(
                        kind="mcp_elicitation_sensitive_field",
                        prompt=(
                            f"{_field_prefix(server_name)}field {name!r} looks like a "
                            "credential (password/token/key/secret/credential). "
                            f"Your answer will be SENT TO server {server_name!r}. "
                            "reyn never autofills this from env vars or stored "
                            "secrets — provide it anyway?"
                        ),
                        choices=elicitation_gate_choices(),
                    )
                    warn_answer = await _ask(bus, warn_iv, deadline)
                    if warn_answer.choice_id != ACCEPT:
                        continue  # skip this field; server sees it absent

                if spec["enum"]:
                    field_iv = UserIntervention(
                        kind="mcp_elicitation_field",
                        prompt=f"{_field_prefix(server_name)}{name}: {spec['description'] or 'choose a value'}",
                        choices=_enum_choices(list(spec["enum"])),
                    )
                    field_answer = await _ask(bus, field_iv, deadline)
                    if field_answer.choice_id is not None:
                        content[name] = field_answer.choice_id
                elif spec["type"] == "boolean":
                    field_iv = UserIntervention(
                        kind="mcp_elicitation_field",
                        prompt=f"{_field_prefix(server_name)}{name}: {spec['description'] or 'yes or no?'}",
                        choices=_bool_choices(),
                    )
                    field_answer = await _ask(bus, field_iv, deadline)
                    if field_answer.choice_id is not None:
                        content[name] = field_answer.choice_id == "true"
                else:
                    field_iv = UserIntervention(
                        kind="mcp_elicitation_field",
                        prompt=f"{_field_prefix(server_name)}{name}: {spec['description'] or 'enter a value'}",
                    )
                    field_answer = await _ask(bus, field_iv, deadline)
                    text = field_answer.text or ""
                    if text or spec["required"]:
                        content[name] = _coerce(text, spec["type"])

            await _emit("mcp_elicitation_answered", field_keys=field_keys, action="accept")
            return ElicitResult(action="accept", content=content)

        except _Cancelled:
            await _emit("mcp_elicitation_timed_out", field_keys=field_keys)
            return ElicitResult(action="cancel")
        except _Declined:
            await _emit("mcp_elicitation_answered", field_keys=field_keys, action="decline")
            return ElicitResult(action="decline")

    return handler


__all__ = [
    "DEFAULT_ELICITATION_TIMEOUT_SECONDS",
    "ElicitationBusResolver",
    "build_elicitation_handler",
]
