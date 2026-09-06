"""``/exec`` and ``/exec-attach`` — run an argv command through the SAME
sandboxed exec op the LLM ``exec`` tool uses (#5837 stage 1).

Usage::

    /exec <cmdline>          — run, show the result on screen only
    /exec-attach <cmdline>   — run, queue argv + result for the NEXT user
                               message (drained by Session._handle_inbox_text,
                               the same ``_pending_user_attachments`` queue
                               ``/attachment``/``/image`` already write to)

owner request (2026-09-06, verbatim): "スラッシュコマンド経由で llm tool の exec
を打てるようにしたい". Owner rulings confirmed in #5837 (quoted there in
full, not repeated here) — the 4 that shape this module:

① **No confirmation prompt; the sandbox stays effective.** Real-machine
   finding (#5837 investigation, reported and re-ruled on before this
   file reached its final shape): the LLM ``exec`` tool call path has NO
   interactive JIT confirmation at all today — ``require_tool``/
   ``CapabilityAxis.TOOL`` (#1199 S3.1b-2c) was built but never wired to
   any tool's dispatch path. So "skip the confirmation" is already true
   of BOTH paths, by absence, not by anything this module does.

   ⚠️ An earlier version of this module added a ``/exec``-only restrict
   check (a new ``is_tool_allowed()`` on ``PermissionResolver``, called
   from here alone). architect's ruling reversed that: (1) it would have
   made the HUMAN path narrower than the LLM path — the wrong direction
   of asymmetry to introduce silently, and (2) it made this ONE fact (is
   ``exec`` permitted for this agent?) checkable from TWO independent
   places that could drift — exactly the class this repo's own review
   discipline closed six times in one night. The tool-axis restrict check
   belongs at ONE seam shared by both the LLM path and this command, not
   built here first — tracked as #5841 (same shape as #5818's "declared,
   documented, never reached the resolver"). **This module does not add
   any exec-specific permission check.** The sandbox (write scope /
   network / subprocess / env) is untouched by any of this — it is
   resolved through the SAME ``op_context_factory()`` seam
   (``op_context_from_tool_context``, ``reyn.tools.exec``) the LLM's own
   ``exec`` calls use, so ``sandbox.mode: strict`` reaches this path
   identically, unrelated to the confirmation question entirely.

② **``caller_kind="operator"``, and a dedicated actor.** ``ToolContext.
   caller_kind`` is ``"operator"`` (never ``"router"`` — this is not an
   LLM tool call). ``op_context_factory()`` (``RouterOpContextSource.
   build``, ``router_op_context.py``) hardcodes ``OpContext.actor=
   "chat_router"`` — reusing that value would mix an operator's own
   ``/exec`` calls into the chat router's approval-ledger record (the
   ledger is actor-scoped: ``<actor>/<op>/<path>``). :data:`_OPERATOR_
   EXEC_ACTOR` is a distinct value this module owns, applied via
   ``dataclasses.replace`` AFTER the factory call (the factory itself has
   no parameter for this — same shape as the resolved-backend override
   ``op_context_from_tool_context`` already applies post-construction).

③ **``/exec`` = screen only; ``/exec-attach`` = queue for the next user
   message.** The queued block carries the ACTUAL argv that ran
   (``shlex.join``, never the raw typed text — quoting/rejected-token
   normalization would otherwise be lost) ahead of the result, so a
   later reader can tell what was executed without re-deriving it.

④ **shlex only — no shell interpretation.** :func:`tokenize_exec_cmdline`
   is the ONE function in this module that touches ``shlex`` — isolated
   so #5838 (the still-undecided "should LLM exec, and this command, move
   to shell interpretation" question) can replace ONLY this function,
   wherever it lands, without touching anything else here. Nothing else
   in this module calls ``shlex`` directly.
"""
from __future__ import annotations

import dataclasses
import shlex
from typing import Any

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash

_USAGE = "usage: /exec <cmdline>  |  /exec-attach <cmdline>"

# #5837 ②: a name distinct from "chat_router" (the LLM-router actor
# build_router_op_context hardcodes) — the approval ledger is actor-scoped
# (<actor>/<op>/<path>), so reusing that name would mix this session's
# operator-originated /exec calls into the chat router's own record.
_OPERATOR_EXEC_ACTOR = "slash_exec_operator"

# #5837 ④ (owner ruling, verbatim "shlex 期待だよ"): shlex interprets
# quoting ONLY. A token containing one of these after shlex has already
# split on whitespace/quotes means the caller wrote something that wants
# shell interpretation (a pipe, a redirect, a subshell, command
# substitution) — reject explicitly rather than silently pass it through
# as a literal argv element nothing will interpret the way the caller
# expects.
_SHELL_METACHAR_TOKENS = ("|", ">", "<", "&&", ";", "$(", "`")

# #5837 ⑦ (architect: "same thinking as #364's size gate", disclosed
# constant — this is an internal display bound, not a config knob, same
# class as `_COMPLETER_MAX`/`_file_size_human`'s own thresholds in
# slash/attachment.py): a queued /exec-attach block becomes part of the
# NEXT completion's prompt, so an unbounded command's full output would
# silently inflate that turn's token cost with no operator-visible signal
# until the bill. Bounded the same shape `_failure_stderr_snippet`
# (hooks/shell_runner.py, #5803) already uses for a comparable "don't
# lose the START (what ran) or the END (the actual error/tail) case —
# only the middle is safe to drop.
_ATTACH_TEXT_MAX_CHARS = 4000
_ATTACH_TEXT_HEAD = 2000
_ATTACH_TEXT_TAIL = 1500


class ExecCmdlineRejected(Exception):
    """Raised by :func:`tokenize_exec_cmdline` when *text* looks like it
    wants shell interpretation (#5837 ④) — never executed as argv."""


def tokenize_exec_cmdline(text: str) -> "list[str]":
    """The ONE place ``/exec``/``/exec-attach`` turn a typed command line
    into argv — see this module's own docstring, point ④, for why this is
    isolated to a single function.

    ``shlex.split`` (quoting only — #5837 owner ruling). A resulting token
    containing a shell metacharacter raises :class:`ExecCmdlineRejected`
    rather than being passed through as a literal argv element — quoting
    a metachar does not rescue it (``/exec 'ls | wc -l'`` shlex-splits to
    ONE token containing ``|``, and is rejected exactly like the
    unquoted form; #5837's own accept criterion names this case).
    """
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise ExecCmdlineRejected(f"could not parse arguments: {exc}") from exc
    if not argv:
        raise ExecCmdlineRejected("no command given")
    for token in argv:
        if any(meta in token for meta in _SHELL_METACHAR_TOKENS):
            raise ExecCmdlineRejected(
                "shell interpretation is not supported here — this runs "
                "argv directly, the same as the LLM's own exec tool. "
                f"Quote the argument if you meant it literally (token: {token!r})."
            )
    return argv


async def _build_exec_tool_context(ctx: "SlashContext") -> Any:
    """Same shape as ``slash/plugin.py``'s/``slash/tasks.py``'s own tool-
    context builders — see this module's docstring point ② for why
    ``caller_kind`` differs from ``plugin.py``'s ``"router"``."""
    from reyn.tools.types import ToolContext, build_resource_caller_state

    host = ctx.session.router_host
    router_state = await build_resource_caller_state(host)
    return ToolContext(
        events=host.events,
        permission_resolver=getattr(host, "permission_resolver", None),
        workspace=getattr(host, "workspace", None),
        caller_kind="operator",
        router_state=router_state,
        resolver=getattr(host, "resolver", None),
        hot_reloader=getattr(host, "hot_reloader", None),
        state_log=getattr(host, "state_log", None),
        agent_name=getattr(host, "agent_name", None),
    )


async def _build_operator_op_context(tool_ctx: Any) -> Any:
    """The SAME ``op_context_factory()`` bridge the LLM's own ``exec`` tool
    call uses (``reyn.tools.exec.op_context_from_tool_context``) — so the
    sandbox policy (``sandbox.mode: strict`` included, #5818/#5827) is
    resolved through the identical seam, never re-derived here. The
    ``actor`` this bridge hardcodes (``"chat_router"``) is replaced
    afterward — see this module's docstring point ②."""
    from reyn.tools.exec import op_context_from_tool_context

    op_ctx = await op_context_from_tool_context(tool_ctx)
    return dataclasses.replace(op_ctx, actor=_OPERATOR_EXEC_ACTOR)


def _format_exec_result(result: "dict[str, Any]") -> str:
    """Screen/attach rendering: exit code, then stdout, then stderr (if
    any) — the same three facts ``renderer.py``'s own short-form
    ``sandboxed_exec`` summary (:725) leads with, expanded to full text
    since this command's whole point is showing the operator everything,
    not a truncated status-row summary."""
    returncode = result.get("returncode")
    stdout = str(result.get("stdout") or "").rstrip("\n")
    stderr = str(result.get("stderr") or "").rstrip("\n")
    lines = [f"exit {returncode}"]
    if stdout:
        lines.append(stdout)
    if stderr:
        lines.append("[stderr]")
        lines.append(stderr)
    return "\n".join(lines)


def _bounded_attach_text(text: str) -> str:
    """Head+tail bound for a queued ``/exec-attach`` block — see this
    module's own ``_ATTACH_TEXT_*`` constants for why. Mirrors
    ``hooks/shell_runner.py``'s ``_failure_stderr_snippet`` shape: keep
    the START (what ran, the exit line) and the END (the part most likely
    to carry the actual error), elide only the middle, and say so
    visibly — never a silent cut."""
    if len(text) <= _ATTACH_TEXT_MAX_CHARS:
        return text
    head = text[:_ATTACH_TEXT_HEAD]
    tail = text[-_ATTACH_TEXT_TAIL:]
    elided = len(text) - _ATTACH_TEXT_HEAD - _ATTACH_TEXT_TAIL
    return f"{head}\n... [{elided} chars elided] ...\n{tail}"


async def _run_exec(ctx: "SlashContext", args: str) -> "tuple[list[str], dict] | None":
    """Shared body for ``/exec``/``/exec-attach``: tokenize, build the
    operator OpContext (② sandbox seam + actor override), then run through
    the SAME op_runtime handler the LLM's exec tool uses — no permission
    check of its own (see this module's docstring, point ①: the tool-axis
    restrict check is #5841's, not built here first). Returns
    ``(argv, result)`` on success, ``None`` after already replying with an
    error (usage/rejected-cmdline/run failure)."""
    cmdline = args.strip()
    if not cmdline:
        await reply_error(ctx, _USAGE)
        return None

    try:
        argv = tokenize_exec_cmdline(cmdline)
    except ExecCmdlineRejected as exc:
        await reply_error(ctx, str(exc))
        return None

    tool_ctx = await _build_exec_tool_context(ctx)
    op_ctx = await _build_operator_op_context(tool_ctx)

    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.schemas.models import SandboxedExecIROp

    op = SandboxedExecIROp(kind="sandboxed_exec", argv=argv)
    try:
        result = await handle_sandboxed_exec(op=op, ctx=op_ctx)
    except PermissionError as exc:
        # The op's own pre-exec threat scan (FP-0050/#1822 S5) denies via
        # this channel — the sandbox-scoped write/network/subprocess
        # boundary applies the same way it does for the LLM's own exec
        # calls (② op_context_factory seam); this is that boundary
        # reporting a real denial, not a new gate this module adds.
        await reply_error(ctx, f"exec denied: {exc}")
        return None
    return argv, result


@slash(
    "exec",
    summary="Run a command through the sandboxed exec tool (screen only)",
    locus="session",
    usage=_USAGE,
    see_also=("exec-attach",),
)
async def exec_cmd(ctx: "SlashContext", args: str) -> None:
    outcome = await _run_exec(ctx, args)
    if outcome is None:
        return
    _argv, result = outcome
    await reply(ctx, _format_exec_result(result))


@slash(
    "exec-attach",
    summary="Run a command and attach argv + result to your next message",
    locus="session",
    usage="usage: /exec-attach <cmdline>",
    see_also=("exec",),
)
async def exec_attach_cmd(ctx: "SlashContext", args: str) -> None:
    outcome = await _run_exec(ctx, args)
    if outcome is None:
        return
    argv, result = outcome

    # #5837 ③: the queued block leads with the ACTUAL argv that ran
    # (shlex.join — never the raw typed cmdline, which may have differed
    # after tokenize_exec_cmdline's own normalization), then the result.
    attach_text = _bounded_attach_text(
        f"$ {shlex.join(argv)}\n{_format_exec_result(result)}"
    )

    queue: "list[dict] | None" = getattr(ctx.session, "_pending_user_attachments", None)
    if queue is None:
        await reply_error(
            ctx,
            "attachment queue is unavailable on this session (=#366 wiring missing).",
        )
        return
    # #5837 ③/architect "実装判断" (this module's docstring): a plain text
    # block, not a path-ref (contrast /attachment's own {"type": "file",
    # "path": ..., ...} shape). Chosen because Session._handle_inbox_text
    # (session.py, drain point) appends every queued block into the SAME
    # content list a leading {"type": "text", "text": ...} block already
    # occupies (verified directly, not inferred) — a bare text block is
    # already a proven-compatible shape at that exact site, no new
    # rendering path needed. NOT chosen: writing exec output to a .reyn/
    # temp file and queuing a path-ref `file` block (mirroring /attachment
    # /image) — rejected because that shape's own rationale (#383 PR-C:
    # "the user's file is the source of truth; reyn does not copy it")
    # does not apply to exec output, which reyn itself produced; a temp
    # file would add a naming/retention-cleanup surface for zero benefit.
    queue.append({"type": "text", "text": attach_text})

    await reply(
        ctx,
        f"attached: exec result ({len(attach_text)} chars). "
        f"queued count: {len(queue)}. Send your next message to include it.",
    )
