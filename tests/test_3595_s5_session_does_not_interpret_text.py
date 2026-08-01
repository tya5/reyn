"""#3595 S5 — ``Session`` interprets no string, and both clients still run slash.

The arc's completion condition, in the architect's words: ``session.py``'s
``startswith("/")`` dispatch AND the ``/answer`` pre-queue fast path
(``maybe_deliver_answer_command``) both gone, in one change — because deleting
only the first would leave ``/answer`` with a surviving string-sniffing entry,
which is the sibling-site shape this arc hit repeatedly.

Three claims, and each needs its opposite to mean anything:

* **negative** — text on the inbox beginning with ``/`` is READ, not executed.
  On its own this passes if slash is simply dead, so every negative leg here is
  paired with a positive control in the SAME test, asserting the same side
  effect from the client path.
* **positive** — an operator's ``/…`` still runs, in the CUI and in the TUI, and
  is not submitted as a turn.
* **structural** — no slash interpretation is left anywhere in
  ``src/reyn/runtime``. Stated with its blind spots at
  :func:`_prefix_tests_for_slash`, because a gate whose blind spot is
  undocumented is the failure this arc has now hit seven times.

★ On what "no behaviour change" means here. A slash command used to ride the
inbox and was therefore QUEUED behind an in-flight turn; a client-side layer has
no inbox, so every command now runs immediately — the treatment ``/answer``
alone got from #3327. The invariant #3300 actually protects is that a TURN
queues, and that is unchanged and witnessed in
``test_3327_answer_bypasses_sentqueue.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session
from tests._support.slash import RecordingTransport, drain_display, local_transport

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "src" / "reyn" / "runtime"


# ── the structural walk ───────────────────────────────────────────────────────


def _prefix_tests_for_slash(tree: ast.AST) -> "list[tuple[str, int]]":
    """Every syntactic test of "does this string start with ``/``" in ``tree``.

    ★ Written from the VALUE side, not the keyword side: it looks for the
    ``"/"`` literal in the positions that MEAN "is this a command", rather than
    for the names the deleted code happened to use (``_maybe_handle_slash``,
    ``maybe_deliver_answer_command``). A gate keyed on those names would go
    green the moment someone reintroduced the behaviour under a third name,
    which is exactly how this arc's earlier censuses failed.

    Forms it catches, each returned as ``(form, lineno)``:

    * ``x.startswith("/")`` and ``x.startswith(("/", …))`` — the deleted
      ``_handle_user_message`` form;
    * ``x.removeprefix("/")`` — the "strip it and dispatch" variant;
    * ``x[0] == "/"`` / ``"/" == x[0]`` / ``!=`` — the index-compare variant
      that a plain ``startswith`` grep misses, which is the same positional-form
      blind spot that cost this arc two missed producers in S1.

    ⚠️ Forms it does NOT catch, stated rather than implied:

    * a computed prefix (``p = "/"; text.startswith(p)``) or one read from a
      constant elsewhere — the literal is what is matched;
    * a regex (``re.match(r"^/", text)``);
    * a helper OUTSIDE ``src/reyn/runtime`` that the runtime calls — the walk's
      root is the runtime package, so interpretation exported to a new module
      and imported back would pass. ``test_no_runtime_module_imports_the_slash
      _dispatch`` closes the specific version of that hole that matters (the
      runtime reaching for the registry or the client dispatch), not the
      general one.
    """
    out: "list[tuple[str, int]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("startswith", "removeprefix") and node.args:
                arg = node.args[0]
                literals: "list[ast.expr]" = (
                    list(arg.elts) if isinstance(arg, ast.Tuple) else [arg]
                )
                for lit in literals:
                    if isinstance(lit, ast.Constant) and lit.value == "/":
                        out.append((node.func.attr, node.lineno))
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            if not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                continue
            sides = [node.left, node.comparators[0]]
            has_slash = any(
                isinstance(s, ast.Constant) and s.value == "/" for s in sides
            )
            has_index = any(isinstance(s, ast.Subscript) for s in sides)
            if has_slash and has_index:
                out.append(("index-compare", node.lineno))
    return out


def _runtime_sources() -> "dict[str, ast.AST]":
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(_RUNTIME_DIR.rglob("*.py"))
    }


# ── vacuity guards ────────────────────────────────────────────────────────────


def test_the_walk_reads_the_real_runtime_package() -> None:
    """Tier 2: the structural gates below read real runtime modules.

    Every structural assertion in this file is conditional on this one. A wrong
    root — a rename, a moved package — makes an empty walk satisfy "no slash
    interpretation" perfectly, and #3598's author measured that exact shape
    passing on an empty extraction. Named modules say so where a count would
    not: any twenty files satisfy a count.
    """
    sources = _runtime_sources()
    assert {"session.py", "session_api.py", "message_bus.py"} <= set(sources), (
        f"the walk is not reading reyn.runtime — it saw {sorted(sources)!r}"
    )


def test_the_walk_sees_every_form_it_claims_to_see() -> None:
    """Tier 2: the positive control — each declared form is found in a fixture.

    Measured against a fixture rather than today's tree, so it pins the WALK's
    capability rather than the current layout: a walk that silently stopped
    recognising a form would otherwise read as "the runtime got cleaner", the
    most flattering possible way for this gate to break.
    """
    source = (
        "def a(t): return t.startswith('/')\n"
        "def b(t): return t.startswith(('/', ':'))\n"
        "def c(t): return t.removeprefix('/')\n"
        "def d(t): return t[0] == '/'\n"
        "def e(t): return '/' != t[0]\n"
    )
    found = {form for form, _ in _prefix_tests_for_slash(ast.parse(source))}
    assert found == {"startswith", "removeprefix", "index-compare"}, (
        f"the walk missed a form it documents as covered; it found {found!r}. "
        "A form it cannot see becomes an invisible reintroduction with a green gate."
    )


# ── the structural gates ──────────────────────────────────────────────────────


def test_no_runtime_module_tests_a_leading_slash() -> None:
    """Tier 2: nothing in ``reyn.runtime`` asks whether a string is a command.

    The arc's completion condition as a standing property rather than as two
    deletions: it is RED for ``_handle_user_message``'s ``startswith("/")``, for
    ``maybe_deliver_answer_command``'s copy of it, and equally for a third
    entry written tomorrow under any name.
    """
    offenders = {
        module: hits
        for module, tree in _runtime_sources().items()
        if (hits := _prefix_tests_for_slash(tree))
    }
    assert not offenders, (
        f"reyn.runtime interprets text as a slash command: {offenders!r}. "
        "Interpretation is CLIENT work (reyn.interfaces.slash.dispatch) — the "
        "owner's ruling for #3595 is that inbox text is never read as a command "
        "and that a client maps typed text onto published operations."
    )


def test_session_has_no_slash_entry_point() -> None:
    """Tier 2: the two named entry points are gone from the class surface.

    Membership on named things, next to the form gate above rather than instead
    of it: the form gate catches a reintroduction under a new name, this one
    catches a resurrection under the old ones — including a re-add that
    forgot to restore the caller, which the form gate would not see.
    """
    surface = set(dir(Session))
    assert not surface & {"_maybe_handle_slash", "maybe_deliver_answer_command"}, (
        "Session grew a slash entry point back. #3595 S5 deleted both together "
        "precisely so /answer could not keep a second, string-sniffing entry."
    )
    assert {"_handle_inbox_text", "submit_user_text"} <= surface, (
        "the surface enumeration is broken — it does not contain members Session "
        "certainly has, so the absence above says nothing."
    )


def test_no_runtime_module_imports_the_slash_dispatch() -> None:
    """Tier 2: the runtime does not reach for the registry or the client dispatch.

    The form gate above cannot see interpretation that was exported to a helper
    and imported back. This closes the version of that hole that matters: the
    runtime holding the command catalog, or the client's dispatch, at all.
    ``SlashContext`` / ``SessionBoundTransport`` are the declared exception —
    ``Session._slash_context`` builds the context a handler runs SERVER-side
    with, for the remote (``--connect``) case where the client holds no session.
    """
    banned = {
        "REGISTRY", "maybe_dispatch_slash", "suggest_for_unknown",
        "execute_slash_command",
    }
    offenders: "dict[str, list[str]]" = {}
    for module, tree in _runtime_sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "reyn.interfaces.slash"
            ):
                hit = sorted({a.name for a in node.names} & banned)
                if hit:
                    offenders.setdefault(module, []).extend(hit)
    assert not offenders, (
        f"runtime module(s) import the slash catalog / dispatch: {offenders!r}. "
        "Resolving a name against the registry is the interpretation step, and "
        "it belongs to the client."
    )


# ── the behavioural half: negative + positive, always paired ──────────────────


def _one_session_registry(tmp_path, monkeypatch) -> "tuple[AgentRegistry, Session]":
    """A real registry with one attached session — ``/session new``'s side effect
    (a session is born under the agent) is observable from OUTSIDE the session
    that would run it, which is what lets both legs assert on a real effect."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create("operator")
    reg.get_or_load("operator")
    return reg, holder


@pytest.mark.asyncio
async def test_client_input_text_is_read_not_executed(tmp_path, monkeypatch) -> None:
    """Tier 2: a ``/``-prefixed line delivered as ``CLIENT_INPUT`` does not run
    the command — and the same line through the client layer does.

    ``CLIENT_INPUT`` is deliberately the member used here. S1–S3 stopped other
    producers from CLAIMING it; S5's claim is stronger and independent — the
    claim itself carries no command privilege, so even the member an operator's
    own typing arrives under executes nothing.
    """
    reg, holder = _one_session_registry(tmp_path, monkeypatch)
    await reg.attach_session("operator", "main")
    session = reg.get_session("operator", "main")
    assert session is not None
    before = set(reg.session_ids("operator"))

    await session._run_turn_body(
        TurnOrigin.CLIENT_INPUT,
        {"text": "/session new", "chain_id": "c-s5-inbox"},
    )
    assert not set(reg.session_ids("operator")) - before, (
        "a '/'-prefixed line on the inbox EXECUTED — Session is interpreting "
        "text as a command again (owner: inbox につまれたものはスラッシュ"
        "コマンドとして解釈されない)"
    )

    # POSITIVE CONTROL, same command, same session: without it the absence
    # above is satisfied by slash simply being dead.
    transport, _display = local_transport(session)
    consumed = await maybe_dispatch_slash(transport, "/session new")
    assert consumed is True and set(reg.session_ids("operator")) - before, (
        "the operator's own '/session new' did not execute through the client "
        "layer either, so the absence above says nothing about the inbox"
    )


@pytest.mark.asyncio
async def test_the_cui_runs_a_command_without_submitting_it_as_a_turn(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the plain / ``--cui`` client's own routing runs ``/help``.

    Drives ``stream_client.route_input_line`` — the production call site, not
    the shared layer directly — so the wiring is what is measured. The paired
    negative is the second half: bare text on the SAME transport still becomes a
    submission, which is #3300's invariant and the reason "slash left the queue"
    is not "everything left the queue".
    """
    from reyn.interfaces.repl.stream_client import route_input_line

    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="cui-s5",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    session.is_attached = True
    transport, display = local_transport(session)

    await route_input_line(transport, "/help", None)
    shown = " ".join(m.text for m in drain_display(display))
    assert "Slash commands:" in shown, (
        "the CUI did not run /help as a command; route_input_line is not going "
        f"through the shared client-side slash layer. shown={shown!r}"
    )
    assert session.queued_user_messages() == [], (
        "the CUI submitted /help as a turn as well as running it — a command "
        "must not also become a turn"
    )

    await route_input_line(transport, "an ordinary line", None)
    assert [i["text"] for i in session.queued_user_messages()] == [
        "an ordinary line",
    ], (
        "ordinary text stopped reaching the inbox — the assertion above was "
        "passing because nothing was being submitted at all"
    )


@pytest.mark.asyncio
async def test_the_tui_runs_a_command_without_submitting_it_as_a_turn(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the Textual chat app's own submit path runs ``/help``.

    The TUI half of the same claim, driven through ``TextualChatApp._submit``
    (the Composer's production entry) over a real ``ClientTransport``. Paired
    with the same negative: bare text still goes to ``submit_user_text``.
    """
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp

    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="tui-s5",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    session.is_attached = True
    transport = RecordingTransport(session)
    app = TextualChatApp(transport=transport)

    await app._submit("/help")
    assert "Slash commands:" in transport.system_text(), (
        "the TUI did not run /help as a command; _submit is not going through "
        f"the shared client-side slash layer. shown={transport.texts()!r}"
    )
    assert session.queued_user_messages() == [], (
        "the TUI submitted /help as a turn as well as running it"
    )

    await app._submit("an ordinary line")
    assert [i["text"] for i in session.queued_user_messages()] == [
        "an ordinary line",
    ], (
        "ordinary text stopped reaching the inbox — the assertion above was "
        "passing because nothing was being submitted at all"
    )


def test_a_remote_client_can_still_run_a_command(tmp_path, monkeypatch) -> None:
    """Tier 2: the AG-UI ``slash_command`` arm runs a NAME, and refuses an
    unknown one.

    ★ The leg that keeps S5 from being a feature removal. A ``--connect`` client
    holds no ``Session``, so the commands that still read session state (the S4
    residue — ``/model``, ``/cost``, ``/image``, …) cannot run on its side of the
    wire; before S5 they worked because the SERVER interpreted the text. They
    still work, but the server is handed a command name the client already
    resolved rather than a string to sniff, which is the owner's design and not
    a re-introduction of it.

    The refusal leg is the pair: a name this server's registry does not have
    answers ``ran: false`` — a client on a different build, not a crash — and it
    is what proves ``ran: true`` above is reporting a real resolution rather than
    acking everything.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    reg, _holder = _one_session_registry(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    client = TestClient(app)

    resp = client.post(
        "/agui/chat/operator?token=s3cret",
        json={"type": "slash_command", "name": "help", "args": ""},
    )
    assert resp.status_code == 200 and resp.json().get("ran") is True, (
        f"the remote slash arm did not run /help: {resp.status_code} {resp.text}"
    )
    session = reg.attached_session()
    assert session is not None, "the endpoint's attach produced no session"
    # Both display queues, because an attached registry runs a forwarder that
    # moves ``session.outbox`` into ``repl_outbox`` (what the AG-UI stream is fed
    # from) and whether it has run yet is a scheduling detail, not the claim.
    displayed = [
        msg
        for queue in (session.outbox, reg.repl_outbox)
        for msg in iter(
            lambda q=queue: None if q.empty() else q.get_nowait(), None
        )
        if getattr(msg, "text", None) is not None
    ]
    assert any("Slash commands:" in m.text for m in displayed), (
        "the remote /help produced no reply on the stream the connected client "
        f"reads. got kinds={[getattr(m, 'kind', None) for m in displayed]!r}"
    )

    unknown = client.post(
        "/agui/chat/operator?token=s3cret",
        json={"type": "slash_command", "name": "no_such_command", "args": ""},
    )
    assert unknown.status_code == 200 and unknown.json().get("ran") is False, (
        "an unresolvable command name did not answer ran:false — the arm is "
        f"acking without resolving. {unknown.status_code} {unknown.text}"
    )


@pytest.mark.asyncio
async def test_both_clients_run_the_same_layer(tmp_path, monkeypatch) -> None:
    """Tier 2: an unknown command answers identically in both clients.

    "cui / tui はスラッシュコマンド共通実装にすべき" is the owner's requirement,
    and a shared IMPLEMENTATION is falsifiable in a way a shared import is not:
    the suggestion text for a typo is produced in exactly one place, so two
    clients that agree on it are running the same code. A per-client copy would
    drift here first.
    """
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp
    from reyn.interfaces.repl.stream_client import route_input_line

    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="both-s5",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    session.is_attached = True

    cui_transport, cui_display = local_transport(session)
    await route_input_line(cui_transport, "/halp", None)
    cui_text = " ".join(
        m.text for m in drain_display(cui_display) if m.kind == "error"
    )

    tui_transport = RecordingTransport(session)
    await TextualChatApp(transport=tui_transport)._submit("/halp")
    tui_text = tui_transport.error_text()

    assert "unknown command /halp" in cui_text, (
        f"the CUI did not report the typo through the shared layer: {cui_text!r}"
    )
    assert cui_text == tui_text, (
        "the two clients answered a typo differently, so they are not running "
        f"one implementation. cui={cui_text!r} tui={tui_text!r}"
    )
    assert "/help" in cui_text, (
        "the shared layer's suggestion list lost its escape hatch — the "
        "equality above would still hold with both sides empty"
    )
