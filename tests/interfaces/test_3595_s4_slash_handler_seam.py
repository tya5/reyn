"""#3595 S4 — a slash handler depends on the client seam, and the residue shrinks.

Slash is a client-side layer: the owner's design is that a client interprets
``/``-prefixed text and maps it onto published operations, and that ``Session``
never interprets a string. S4 moves what a handler is HANDED from ``Session`` to
:class:`~reyn.interfaces.slash.SlashContext` — a ``ClientTransport`` plus, for
now, the session reads that have not been designed into operations yet.

Two gates, and they pull in opposite directions on purpose:

- ``_SESSION_RESIDUE`` is a **ratchet**: every private ``Session`` member a slash
  module still reaches for is declared with its reason. A member not in the set
  is RED, so the residue cannot grow quietly; and each entry that goes away is
  progress a later PR can delete from here.
- ``_PUBLIC_MEMBER_CEILING`` is the **counterweight**. The obvious way to empty
  the residue set is to publish ``_x`` as ``x`` one-for-one — which would ratify
  the encapsulation break rather than close it, because the members exist
  BECAUSE slash took what it needed in the shape it needed. Shrinking the
  residue while growing the public surface is not progress, so both are measured
  in one file: neither number means anything without the other.

★ **Why the extraction can be complete here.** Unlike #3595 S3's provenance gate
— which could only be written after the value became a symbol — this walk has a
closed target from the start: a private member access is syntactically
``<expr>._name`` or a ``getattr``/``hasattr`` with a literal name, and both forms
are matched. ⚠️ What it does NOT see is an access through an alias
(``s = ctx.session; s._x``) or a computed attribute name
(``getattr(sess, "_" + k)``). Those are not closed by this walk; they are closed
by ``test_no_declared_member_is_stale``'s positive control only in the sense that
a walk regression REDs — a NEW access written in an unseen form would still slip.
Stated rather than implied, because a gate whose blind spot is undocumented is
the failure this arc has now hit six times.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from reyn.interfaces.slash import SlashContext, reply, reply_error
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from tests._support.paths import REPO_ROOT
from tests._support.slash import RecordingTransport, slash_ctx

_SLASH_DIR = REPO_ROOT / "src" / "reyn" / "interfaces" / "slash"


# ── the walk ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Access:
    """One private-``Session`` member access inside the slash package."""

    module: str
    member: str
    lineno: int


def _is_session_expr(node: ast.AST) -> bool:
    """Whether ``node`` denotes the session a handler was handed.

    Both spellings count: ``ctx.session`` (what a handler holds after S4) and a
    bare ``session`` parameter (what the completers still take, because the TUI
    calls them with a session directly — a separate seam S4 did not touch).
    """
    if isinstance(node, ast.Name):
        return node.id in ("session", "sess")
    if isinstance(node, ast.Attribute):
        return node.attr == "session"
    return False


class _ResidueCollector(ast.NodeVisitor):
    """Collect private-member accesses on the session, in both spellings."""

    def __init__(self, module: str) -> None:
        self._module = module
        self.accesses: list[_Access] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") and _is_session_expr(node.value):
            self.accesses.append(
                _Access(self._module, node.attr, node.lineno)
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # ``getattr(session, "_x", None)`` / ``hasattr(session, "_x")`` — the form
        # a plain ``session._x`` grep misses, and the form that hid six members
        # from this arc's earlier censuses.
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id in ("getattr", "hasattr", "setattr")
            and len(node.args) >= 2
            and _is_session_expr(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.startswith("_")
        ):
            self.accesses.append(
                _Access(self._module, node.args[1].value, node.lineno)
            )
        self.generic_visit(node)


def _residue_accesses(root: "Path | None" = None) -> "list[_Access]":
    """Every private-session access in the slash package (or ``root``)."""
    base = root if root is not None else _SLASH_DIR
    out: list[_Access] = []
    for path in sorted(base.glob("*.py")):
        collector = _ResidueCollector(path.name)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        out.extend(collector.accesses)
    return out


# ── the declared residue ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Residue:
    """Why a private ``Session`` member is still reachable from a slash handler.

    ``needed_for`` names the commands, so deleting a command deletes the reason
    with it. ``resolution`` is the shape the eventual fix takes — recorded now,
    while the person who read the call site is the one writing it down, rather
    than left for whoever picks the entry up next.
    """

    needed_for: "tuple[str, ...]"
    resolution: str


#: Every private ``Session`` member the slash package still touches.
#:
#: ★ ``_put_outbox`` is deliberately ABSENT: S4's whole increment was removing it,
#: and its absence here is what makes ``test_no_slash_module_reaches_the_session_outbox``
#: a statement about this registry rather than a second, parallel rule.
_SESSION_RESIDUE: "dict[str, _Residue]" = {
    "_registry": _Residue(
        needed_for=("/agent", "/agents", "/attach", "/reset", "/rewind", "/session"),
        resolution=(
            "The registry bundle. These are agent- and checkpoint-lifecycle "
            "operations, not display: a client asking for them needs a typed "
            "operation on its own seam (the transport, or a registry-facing "
            "surface a client is entitled to), not the session's own field."
        ),
    ),
    "_budget": _Residue(
        needed_for=("/cost", "/budget"),
        resolution=(
            "The budget bundle — a read of the gateway plus one reset. A "
            "cost/budget snapshot read is the natural published shape; "
            "`BudgetGateway` already renders both lines, so what is missing is "
            "the seam, not the rendering."
        ),
    ),
    "_agent": _Residue(
        needed_for=("/model",),
        resolution="Part of the model bundle: the agent-identity default the override is compared against.",
    ),
    "_agent_role": _Residue(
        needed_for=("/agent edit role",),
        resolution=(
            "A WRITE, and the only one in this set: /agent edit role mutates the "
            "in-memory role so the next turn picks it up. Needs a typed operation "
            "precisely because a bare attribute write cannot be audited."
        ),
    ),
    "_model_override": _Residue(
        needed_for=("/model",),
        resolution="Model bundle: the read and the write of the per-session override.",
    ),
    "_resolver": _Residue(
        needed_for=("/model",),
        resolution=(
            "Model bundle: class validation + the available-class list. "
            "``Session.known_model_classes`` already publishes the list half — "
            "the completer uses it — so the missing half is is_known_class."
        ),
    ),
    "_rebuild_derived_model_engines_for_model": _Residue(
        needed_for=("/model",),
        resolution=(
            "Model bundle: the post-switch rebuild. Not a separate operation — "
            "it is part of what 'set the model' MEANS, so a published "
            "set-model operation absorbs it rather than exposing it. #3785: "
            "folds BOTH the turn_budget engine's rebuild and compaction's "
            "(previously missing entirely — compaction never tracked a "
            "/model switch, the bug #3785 fixed) into this ONE accessor "
            "rather than adding a second private-Session entry point for "
            "what is the same kind of residue twice."
        ),
    ),
    "_interventions": _Residue(
        needed_for=("/list", "/answer"),
        resolution=(
            "★ Not the zero-new-API case the S4 spec classified it as: the "
            "transport publishes pending_intervention_head() (the HEAD) and "
            "answer_intervention_*(intervention_id=…), but /list needs "
            "list_active() and /answer needs get(id). Enumerating what is "
            "pending has no transport equivalent today."
        ),
    ),
    "_resolve_intervention_id": _Residue(
        needed_for=("/answer",),
        resolution=(
            "Same gap: /answer takes an id PREFIX and resolves it against the "
            "pending set, reporting ambiguity. Prefix resolution is client-side "
            "work over a published pending list — it disappears with "
            "_interventions, not before it."
        ),
    ),
    "_deliver_answer_to": _Residue(
        needed_for=("/answer",),
        resolution=(
            "The delivery itself, and the one member here with a published "
            "equivalent already: ClientTransport.answer_intervention_text("
            "intervention_id=…). It is not converted in S4 only because it is "
            "reached through the prefix resolution above; converting the "
            "delivery alone would leave the resolution needing the same object."
        ),
    ),
    "_pending_user_images": _Residue(
        needed_for=("/image",),
        resolution=(
            "★ Should not be on Session at all — an image staged for the NEXT "
            "submission is client state, and the client already has a place to "
            "put it (it is what submit_user_text would carry). ⚠️ Session "
            "publishes a read-only `pending_user_images` accessor whose own "
            "docstring says the WRITE side stays private; /image appends, so "
            "routing it through that accessor would falsify the accessor rather "
            "than close anything."
        ),
    ),
    "_multimodal_config": _Residue(
        needed_for=("/image",),
        resolution="The media-size gate's config half — moves with _pending_user_images.",
    ),
    "_perm": _Residue(
        needed_for=("/image",),
        resolution=(
            "The PermissionResolver the media-size gate runs on. A client cannot "
            "hold this; the gate belongs on the session side of a published "
            "attach-image operation, which is where it ends up."
        ),
    ),
    "_intervention_bus": _Residue(
        needed_for=("/image",),
        resolution="The bus the media gate prompts on — moves with _perm, same operation.",
    ),
    "_compact_now_for_op": _Residue(
        needed_for=("/compact",),
        resolution=(
            "The compaction entry point the `compact` OP also uses — so the "
            "operation exists and is typed; only its slash caller reaches it "
            "privately. Publishing this one is nearly free."
        ),
    ),
    "_hot_reloader": _Residue(
        needed_for=("/reload",),
        resolution=(
            "The config hot-reloader. /reload asks for one thing "
            "(request_reload(source='operator')), which is the published "
            "operation's whole signature."
        ),
    ),
}


# ── the gates ─────────────────────────────────────────────────────────────────


def test_extraction_is_not_vacuous() -> None:
    """Tier 2: the walk reads the real slash package and finds accesses at all.

    Every other assertion in this file is conditional on this one. An extractor
    that silently returns nothing satisfies "no undeclared member" perfectly, and
    #3598's author measured that exact shape passing on an empty extraction.
    """
    modules = {p.name for p in _SLASH_DIR.glob("*.py")}
    assert {"__init__.py", "model.py", "agents.py"} <= modules, (
        f"the walk is not reading the slash package — it saw {sorted(modules)!r}. "
        "A wrong root makes every gate below vacuous, and named modules say so "
        "where a count would not (a count is satisfied by any 20 files)."
    )
    accesses = _residue_accesses()
    assert accesses, (
        "the walk found NO private-session access anywhere in the slash package. "
        "If that is genuinely true, S4's successor finished the job and this "
        "file should be deleted along with SlashContext.session — verify that "
        "before believing it, because a broken walk looks identical."
    )


def test_walk_sees_both_forms_a_private_access_takes() -> None:
    """Tier 2: attribute form AND getattr/hasattr form are both found.

    Measured, not hypothetical: the census that produced this arc's "12 private
    members" number counted ``session._x`` only, and missed six members reached
    through ``getattr(session, "_x", None)`` — ``_action_usage_tracker``,
    ``_compact_now_for_op``, ``_multimodal_config``, ``_perm``,
    ``_intervention_bus``, ``_hot_reloader``. Run against a fixture so it
    measures the walk's capability rather than today's call-site layout.
    """
    source = (
        "def attribute(ctx): return ctx.session._alpha\n"
        "def bare_param(session): return session._beta\n"
        "def getattr_form(ctx): return getattr(ctx.session, '_gamma', None)\n"
        "def hasattr_form(ctx): return hasattr(ctx.session, '_delta')\n"
    )
    collector = _ResidueCollector("fixture.py")
    collector.visit(ast.parse(source))
    found = {a.member for a in collector.accesses}
    assert found == {"_alpha", "_beta", "_gamma", "_delta"}, (
        f"the walk missed a form a private access can take; it found {sorted(found)!r}. "
        "A form it cannot see becomes an invisible dependency with a green gate."
    )


def test_every_private_session_access_is_declared() -> None:
    """Tier 2: the ratchet — a slash module may not reach for an undeclared
    private ``Session`` member.

    This is the direction that matters for the arc: the residue is allowed to
    shrink, never to grow. A new command that needs something private is asking
    for an operation to be designed, and this is where that conversation starts.
    """
    undeclared = sorted({
        (a.module, a.member)
        for a in _residue_accesses()
        if a.member not in _SESSION_RESIDUE
    })
    assert not undeclared, (
        f"slash module(s) reach for an undeclared private Session member: "
        f"{undeclared!r}. #3595 S4 handed handlers a ClientTransport precisely so "
        "they would stop doing this — if the operation genuinely belongs on the "
        "session, design it as a published operation and record why here; if it "
        "is display or client state, it belongs on the transport or in the client."
    )


def test_no_declared_member_is_stale() -> None:
    """Tier 2: every declared member is still FOUND — the standing positive control.

    Two failures collapse here. A member that is gone leaves a dead entry, and a
    dead entry is how a registry stops describing the code. More importantly, a
    WALK that regressed (a lost form, an added filter) makes declared members
    vanish from the found set — which would otherwise read as "the residue got
    smaller", the most flattering possible way for this gate to break.
    """
    found = {a.member for a in _residue_accesses()}
    stale = sorted(set(_SESSION_RESIDUE) - found)
    assert not stale, (
        f"_SESSION_RESIDUE declares member(s) the walk no longer finds: {stale!r}. "
        "If the access is genuinely gone, delete the entry and say so in the PR — "
        "that is the arc's progress being recorded. If it is not gone, the walk "
        "regressed and every 'no undeclared member' result above is understated."
    )


def test_no_slash_module_reaches_the_session_outbox() -> None:
    """Tier 2: ``_put_outbox`` is gone from the slash package — S4's own increment.

    One helper, ``reply()``, carried this dependency for every registered
    command; five modules also held it directly for their sentinel messages. It
    is absent from ``_SESSION_RESIDUE``, so the ratchet above already fails on a
    reintroduction — this test exists to say WHY that absence is deliberate
    rather than an oversight, and to fail with the reason attached.
    """
    outbox_accesses = sorted({
        (a.module, a.lineno) for a in _residue_accesses()
        if a.member == "_put_outbox"
    })
    assert not outbox_accesses, (
        f"slash module(s) write to the session outbox directly: {outbox_accesses!r}. "
        "A slash reply is CLIENT-authored display — ClientTransport.put_display's "
        "own docstring named the /copy result as one of its payloads — so it goes "
        "through ctx.transport, which is what lets the dispatch move client-side."
    )


#: ``Session``'s public member count at the time S4 landed.
#:
#: A CEILING, not a pin: removals are the direction this arc wants, and a rename
#: is not a regression. What it catches is the one move that would make the
#: residue registry above look finished while nothing was fixed — publishing a
#: private member so a handler can keep reaching for it under a new name.
#:
#: Raised 104 -> 105 for #4387 Phase B ②'s ``extend_history_backward`` — NOT a
#: slash-handler reach-in (this gate's own failure mode): it is the sanctioned
#: external accessor :mod:`reyn.interfaces.repl.read_model`'s
#: ``RegistryReadModel`` needs for TUI scrollback paging / search, the SAME
#: kind of thin public wrapper ``conversation_history`` already is over
#: ``self.history`` — a read-model seam reads ``Session`` through public
#: members by design, unlike a slash handler reaching into private state.
#:
#: Raised 105 -> 106 for #4206 slice 1: ``output_language`` was ALREADY a
#: public Session field before this change — a plain ``self.output_language
#: = ...`` instance attribute set in ``__init__``. This gate's own
#: enumeration (``dir(Session)``, the CLASS, not an instance) never counted
#: it, because a plain instance attribute assigned in ``__init__`` is
#: invisible to ``dir()`` on the class itself. Converting it to a
#: ``@property`` (so it can live-resolve the new ③ preference-axis
#: session/agent overrides — see ``reyn.runtime.preferences``) makes the
#: SAME external name a class-level descriptor for the first time, which is
#: what this ceiling actually measures. No new slash-reachable capability
#: was added — the name, its meaning, and every external caller's access
#: pattern (``session.output_language``) are unchanged.
#: Raised 106 -> 107 for #4686: ``mcp_subscription_state`` — a NEW public
#: read-only method, the status-bar/MCP-pane's subscription read model
#: (mirrors ``capability_visibility_state``/``hook_state``'s own forwarder
#: shape, both already public before this change). Genuinely unrelated to
#: #3595 S4's slash-handler-encapsulation concern: it is a read accessor a
#: status-readout seam (``status.py``'s ``_session_mcp_subscriptions``) and
#: the LLM-facing ``list_mcp_subscriptions`` tool (via
#: ``RouterHostAdapter``) both call — not a private-state leak a slash
#: handler needed publishing to keep reaching into the session.
#: Raised 107 -> 108 for #4206 slice 2: ``reasoning_display`` — a NEW
#: ``@property``, the second ③ preference-axis key (after slice 1's
#: ``output_language``) to get a live session/agent-override-resolving
#: accessor. Same shape, same reason as the 106->107 entry above: not a
#: private-state leak, a new read surface the ③ axis's own mechanism
#: requires by design (``RouterHostAdapter.reasoning_display_enabled()``
#: consults it via a callback).
#: Raised 108 -> 109 for #4206 Slice B (#4724): ``warn_ratio_overrides`` — a
#: NEW method, the ③ preference-axis resolution for the 7
#: cost.*.warn_ratio keys (Design C: the caller resolves, BudgetTracker
#: never learns a session/agent identity itself). Same shape, same reason
#: as the 107->108 entry above.
#: Raised 109 -> 110 for #4206 ②: ``model_class_ceiling`` — a NEW
#: ``@property``, the ②bounding-axis's live composed ``model`` ceiling
#: (project resolver + agent-layer + session-layer, narrowest wins,
#: restrict-only — see ``reyn.runtime.bounding``). Same "a new read
#: surface the axis's own mechanism requires by design" reason as the
#: 107->108/108->109 entries above (``RouterHostAdapter.model_class_ceiling()``
#: consults it via the same callback shape), not a private-state leak.
#: Raised 110 -> 111 for #4759: ``aclose_background_tasks`` — a NEW method,
#: same public shape as the EXISTING ``aclose_mcp_connections``/
#: ``aclose_event_store`` (already counted in the prior ceiling) —
#: ``AgentRegistry.shutdown()``'s getattr-duck-typed teardown seam for the
#: single background-task funnel (``tracked_tasks.py``). Genuinely unrelated
#: to a slash handler reaching into the session (it is a registry-owned
#: teardown call, not a read a slash command would use) — the gate's own
#: documented carve-out for this case.
#: Raised 111 -> 112 for #4862: ``hot_reloader`` — a NEW ``@property``
#: answering a question nothing else could ("which HotReloader belongs to
#: THIS session", vs. the process-global ``get_active_hot_reloader()``
#: returning only the last-registered session's reloader). Genuinely
#: unrelated to slash (added to close a scaffold-test rescue's private-read
#: gap, #4862/#4864) — not a member published so a slash handler could keep
#: reaching into the session, the gate's own documented carve-out. A sibling
#: ``audit_events`` property was considered and DROPPED (not counted here)
#: — per the gate's other half ("publishing _x as x ratifies the
#: encapsulation break instead of closing it"), it answered no question
#: nothing else could; it was a plain rename of already-private state.
#: Raised 112 -> 113 for #4961 C / #4966: ``aclose_audit_events`` — a NEW
#: method, same public shape as the EXISTING ``aclose_mcp_connections``/
#: ``aclose_event_store``/``aclose_background_tasks`` (already counted in
#: the prior ceiling) — the same getattr-duck-typed teardown chain
#: (``registry.py``'s ``archive_agent``/``remove_session``) gained a 4th
#: instance for closing this session's own ``_audit_events`` EventLog
#: (drain-then-stop) before it's dropped from the in-memory map.
#: Genuinely unrelated to a slash handler reaching into the session (it
#: is a registry-owned teardown call, not a read a slash command would
#: use) — the gate's own documented carve-out for this case.
#: Raised 113 -> 115 for #5012-A PR #5038: ``max_hook_driven_turns`` and
#: ``remaining_hook_driven_turns`` — two READ-ONLY properties reporting the
#: hook-driven-turns loop-valve's effective cap and remaining budget
#: (`_effective_hook_driven_turns_cap`'s SSoT), added so ``describe_session``
#: (a router-callable tool, NOT a slash handler) can surface a value
#: `safety.loop.max_hook_driven_turns` already declares but no reader could
#: previously reach — the OS-internal enforcement site
#: (`_stamp_execution_context`) and this pair are the only two callers.
#: Genuinely unrelated to a slash handler reaching into the session (the
#: gate's own documented carve-out) — no write capability, no state
#: mutation, and the consumer is an LLM-callable tool's OpContext supplier,
#: not `ClientTransport`.
_PUBLIC_MEMBER_CEILING = 115


def test_session_public_surface_does_not_grow() -> None:
    """Tier 2: ``Session``'s public class surface does not grow past S4's baseline.

    The arc's stated success metric. It is measured off the LIVE class rather
    than a source scan so a member added by any route — property, method,
    inherited — counts the same.
    """
    public = sorted(n for n in dir(Session) if not n.startswith("_"))
    assert {"submit_user_text", "shutdown", "run"} <= set(public), (
        f"the public-member enumeration is broken — it produced {public!r}, which "
        "does not contain members Session certainly has. A broken enumeration "
        "passes the ceiling below trivially, so this positive control is what "
        "makes the ceiling mean anything."
    )
    assert len(public) <= _PUBLIC_MEMBER_CEILING, (
        f"Session's public surface grew to {len(public)} (ceiling "
        f"{_PUBLIC_MEMBER_CEILING}). If you added a member so a slash handler "
        "could keep reaching into the session, that is #3595 S4's failure mode: "
        "publishing _x as x ratifies the encapsulation break instead of closing "
        "it. Design the operation, or put the dependency on ClientTransport. If "
        "the member is genuinely unrelated to slash, raise the ceiling in the "
        "same PR and say what it is for.\n"
        f"current: {public!r}"
    )


# ── the behavioural half ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reply_goes_to_the_client_transport() -> None:
    """Tier 1: ``reply()`` / ``reply_error()`` write through ``ClientTransport``.

    The contract the whole increment rests on, driven through a real
    ``ClientTransport`` implementation rather than a stand-in. Both kinds are
    exercised because ``reply_error`` is the branch every failure path takes.
    """
    ctx = slash_ctx()
    await reply(ctx, "ok")
    await reply_error(ctx, "nope")

    assert ctx.transport.kinds() == ["system", "error"], (
        "reply()/reply_error() must display through the transport, in order"
    )
    assert ctx.transport.system_text() == "ok"
    assert ctx.transport.error_text() == "nope"


@pytest.mark.asyncio
async def test_a_handler_needs_no_session_to_reply() -> None:
    """Tier 1: a handler whose only dependency is the reply path runs with
    ``SlashContext.session`` unset.

    The falsifiable form of "the residue is optional": ``/help`` reads the
    registry and displays, nothing else. If ``reply()`` still went through the
    session, this raises instead of rendering — which is exactly what it did
    before S4.
    """
    from reyn.interfaces.slash.help import help_cmd

    ctx = SlashContext(transport=RecordingTransport())
    assert ctx.session is None
    await help_cmd(ctx, "")

    assert "Slash commands:" in ctx.transport.system_text()


@pytest.mark.asyncio
async def test_the_session_built_seam_still_lands_a_reply_on_the_session_outbox(
    tmp_path,
) -> None:
    """Tier 2: the session-built transport routes display to the session's own
    outbox — S4 moved the dependency, not the destination.

    Drives the PRODUCTION construction (``Session._slash_context``) rather than
    the test transport, because the claim is about production routing. ★ After
    #3595 S5 this construction is the REMOTE path specifically: a ``--connect``
    client holds no ``Session``, so the AG-UI endpoint runs the command here and
    its reply reaches that client by riding ``session.outbox`` — the queue it
    reached before. A LOCAL client passes its own transport instead, which the
    S5 file covers.
    """
    from reyn.interfaces.slash.dispatch import execute_slash_command
    from tests._support.agent_session import make_session

    session = make_session(agent_name="default", snapshot_path=tmp_path / "snap.json")
    ran = await execute_slash_command(session._slash_context(), "help", "")

    assert ran, "/help must be executed by the server-side slash executor"
    displayed: list[OutboxMessage] = []
    while not session.outbox.empty():
        displayed.append(session.outbox.get_nowait())
    assert any("Slash commands:" in m.text for m in displayed), (
        "the /help reply did not reach session.outbox; the client seam is "
        f"routing display somewhere else. got kinds={[m.kind for m in displayed]!r}"
    )
