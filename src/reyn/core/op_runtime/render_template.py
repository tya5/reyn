"""render_template kind handler — a sandboxed text-templating producer (FP-0055 PR-2).

``render_template`` renders a Jinja2 template against structured data into a plain
string. It is a **producer**, not a sink: it writes nothing and invokes no surface —
the rendered string is returned as an ordinary op result (canonical ``text``) whose
bulk auto-offloads on the chat path, and the caller routes it to whatever sink it
wants (``present``, a ``write_file`` step, a message, or a pipeline ``ctx``).

Trust + safety model
--------------------
- **Sandbox (producer-side, sink-independent).** The engine is always
  ``jinja2.sandbox.SandboxedEnvironment`` (via the one factory,
  ``reyn.security.template_env.make_sandboxed_env``): templates may be LLM-authored,
  and unsandboxed Jinja2 is arbitrary-code execution (SSTI). A blocked attribute
  traversal (``{{ ().__class__ }}``) raises ``SecurityError`` → a structured error
  result; nothing executes.
- **Producer-neutrality (no output escaping here).** ``autoescape`` is OFF and the
  op returns RAW rendered bytes. Neutralization is the SINK's job — a terminal
  strips control bytes at its guard, a file is inert, a web surface HTML-escapes.
  Escaping in the producer would corrupt file / terminal artifacts.
- **Read-authority equivalence.** ``template_ref`` / ``data_ref`` resolve through
  exactly the ``file.read`` gate (``resolve_ref_text`` / ``resolve_present_source``);
  a denied read → ``status="denied"``. render_template can never read more than the
  agent's ``file.read`` can. An inline-only invocation is pure computation.

Undefined policy
----------------
``strict`` (default) → an undefined variable is a HARD error naming the missing name
(loud-by-default: a file sink must never silently write a broken artifact). ``lenient``
→ undefined renders empty and the referenced-but-unbound names surface in the result
meta as ``undefined_vars`` (a self-correction signal, not a crash).

Resource bounds
---------------
``SandboxedEnvironment`` stops SSTI but NOT resource exhaustion — a bounded loop like
``{% for i in range(10**9) %}`` still floods. The cap is applied **during** generation,
not after: rendering streams through ``template.generate(context)`` (Jinja2's chunk
generator), accumulating against a max-chars budget with a wall-clock backstop; the
moment either is exceeded the loop stops and the result is TRUNCATED with a ``truncated``
meta flag naming which bound fired — a bounded result, never an OOM / hang / crash.
``.render()`` (materialize-then-cap) would be insufficient: the exhaustion happens
during materialization. Bounds default to the safety-spirit constants below and are
overridable per op-context via ``OpContext.render_template_bounds``.

Determinism: a pure function of (template, data) — no clock / random in the render
path; ordinary op-event / memo replay applies. No new event type, no reconstructed
state (recovery-feature gate N/A).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from jinja2 import TemplateError, UndefinedError
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import SecurityError

from reyn.core.present import (
    PresentSourceNotFound,
    resolve_present_source,
    resolve_ref_text,
)
from reyn.security.template_env import make_sandboxed_env

from . import register
from .context import OpContext

_KIND = "render_template"

# The template-context key the resolved data binds under (unambiguous, mirrors the
# pipeline ``ctx`` style — ``{{ data.results[0].title }}``).
_DATA_KEY = "data"


@dataclass(frozen=True)
class RenderTemplateBounds:
    """Resource bounds for a single render, applied DURING generation.

    ``max_output_chars`` — the streaming byte(char) budget; the render truncates the
    moment cumulative output exceeds it. ``wall_clock_seconds`` — the elapsed-time
    backstop (Jinja2 exposes no iteration count, so wall-clock bounds a runaway loop
    that produces little text per step). Safety-spirit defaults: generous enough for
    real reports / configs, tight enough that a runaway generator is stopped quickly.
    """

    max_output_chars: int = 256_000
    wall_clock_seconds: float = 5.0


def _error(error_kind: str, message: str) -> dict:
    """A structured error result — a Jinja render / syntax error, an SSTI-blocked
    access, or a strict-undefined variable. ``status="error"`` is the sole error-path
    driver the canonical mapper reads (→ ``meta.isError``)."""
    return {
        "kind": _KIND,
        "status": "error",
        "ok": False,
        "error_kind": error_kind,
        "error": message,
    }


async def _resolve_template_text(op: Any, ctx: OpContext) -> str:
    """The Jinja2 source text: an inline ``template`` verbatim, or a ``template_ref``
    read as RAW text under ``file.read`` authority (never JSON-rehydrated — a template
    file is source text even when it starts with ``{``)."""
    if op.template_ref is not None:
        text, _ingested = await resolve_ref_text(op.template_ref, ctx)
        return text
    return op.template


async def _resolve_data(op: Any, ctx: OpContext) -> Any:
    """The template-context data: a ``data_ref`` re-hydrated to its full value under
    ``file.read`` authority (the same seam ``present`` uses), or ``data_inline``
    verbatim (already in the LLM's context)."""
    if op.data_ref is not None:
        value, _ingested = await resolve_present_source(op.data_ref, ctx)
        return value
    return op.data_inline


def _undefined_vars(env: Any, source: str, context: dict) -> list[str]:
    """The referenced-but-unbound top-level names in ``source`` (lenient-mode signal).

    A static over-approximation: the names the template references that are neither in
    the bound ``context`` nor in the environment globals (``range``/``dict``/…). Sorted
    for a stable, high-signal self-correction hint; it does not execute the template."""
    referenced = find_undeclared_variables(env.parse(source))
    bound = set(context) | set(env.globals)
    return sorted(referenced - bound)


async def handle(op: Any, ctx: OpContext) -> dict:
    # 1. Resolve template + data under file.read authority. PermissionError
    #    propagates → the dispatch layer returns status="denied" (read-authority
    #    equivalence). A missing ref → not_found (mirrors present).
    try:
        source = await _resolve_template_text(op, ctx)
        data = await _resolve_data(op, ctx)
    except PresentSourceNotFound as exc:
        return {"kind": _KIND, "status": "not_found", "ok": False, "error": str(exc)}

    context = {_DATA_KEY: data}

    # 2. Compile in the sandbox. autoescape OFF (producer-neutral). A syntax error
    #    is a HARD error (never a silent fallback — malformed input must not be masked).
    env = make_sandboxed_env(undefined=op.undefined)
    try:
        template = env.from_string(source)
    except TemplateError as exc:
        return _error("template_error", f"template syntax error: {exc}")

    # 3. Lenient-mode undefined-var signal (static; strict mode raises at render
    #    instead, so this stays empty there).
    undefined_vars: list[str] = []
    if op.undefined == "lenient":
        undefined_vars = _undefined_vars(env, source, context)

    # 4. Stream-render with a during-generation cap (size + wall-clock backstop). The
    #    cap fires DURING generate() so a runaway loop is stopped before it floods
    #    memory — .render() would materialize the whole string first.
    bounds = ctx.render_template_bounds or RenderTemplateBounds()
    chunks: list[str] = []
    total = 0
    truncated = False
    truncate_reason: str | None = None
    started = time.monotonic()
    try:
        for chunk in template.generate(context):
            chunks.append(chunk)
            total += len(chunk)
            if total > bounds.max_output_chars:
                truncated = True
                truncate_reason = "max_output_chars"
                break
            if time.monotonic() - started > bounds.wall_clock_seconds:
                truncated = True
                truncate_reason = "wall_clock_seconds"
                break
    except UndefinedError as exc:
        # strict-undefined → hard error naming the missing variable (Jinja's message
        # names it), so the LLM self-corrects in one turn.
        return _error("undefined", str(exc))
    except SecurityError as exc:
        # SSTI-blocked attribute traversal — nothing executed.
        return _error("security", f"sandbox blocked template access: {exc}")
    except TemplateError as exc:
        return _error("template_error", str(exc))

    rendered = "".join(chunks)
    if truncated:
        rendered = rendered[: bounds.max_output_chars]

    result: dict = {
        "kind": _KIND,
        "status": "ok",
        "ok": True,
        "rendered": rendered,
        "truncated": truncated,
    }
    if truncate_reason is not None:
        result["truncate_reason"] = truncate_reason
    if undefined_vars:
        result["undefined_vars"] = undefined_vars
    return result


from reyn.core.offload.canonical import render_template_to_canonical  # noqa: E402

register(_KIND, handle, canonical=render_template_to_canonical)
