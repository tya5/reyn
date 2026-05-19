"""Tier 2: RequestBus / UserChannel Protocol split + commitments (issue #254 Phase 2).

Pins the interface separation introduced by Phase 2:

  - ``RequestBus`` ([A] OS↔upper-layer): emit ``request(iv)`` + await
    answer. OS layer knows only this.
  - ``UserChannel`` ([B] Agent↔User): physical delivery path
    (TUI / stdin / a2a webhook). The canonical method is ``deliver(iv)``.
  - ``InterventionBus`` is retained as an alias of ``RequestBus`` for
    backwards-compat — existing callers typed against the old name keep
    working.
  - Concrete buses (``ChatInterventionBus`` / ``StdinInterventionBus`` /
    ``A2AInterventionBus``) satisfy both Protocols in Phase 2: ``request``
    is a thin alias of ``deliver`` so backwards-compat is preserved while
    new code can use the more precise contract.

Plus two cross-session commitments inscribed at file-creation time:

  - **outbox shape stability** (tui-coder Q1 from #254 discussion):
    ``OutboxMessage(kind="intervention")`` carries
    ``meta = {"intervention_id", "prompt", ...}`` — Phase 2 does NOT
    touch the meta key names. TUI's outbox consumer relies on this.
  - **A2AInterventionBus responsibility scope** (dogfood-coder Q1):
    the module MUST NOT import ``_handle_skill_completed`` or any
    other narration-trigger surface. Narration is OS-internal lifecycle
    plumbing, unrelated to intervention routing — confused by the
    pre-Phase-2 single-bus shape, decoupled by the [A]/[B] split.

No mocks. Real Protocol checks via ``runtime_checkable`` + module-level
introspection on the a2a bus.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from reyn.chat.session import ChatInterventionBus, ChatSession
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    InterventionChoice,
    RequestBus,
    StdinInterventionBus,
    UserChannel,
    UserIntervention,
)
from reyn.web.a2a_intervention import A2AInterventionBus

# ── 1. Protocol definitions ────────────────────────────────────────────


def test_request_bus_and_user_channel_are_distinct_protocols() -> None:
    """Tier 2: ``RequestBus`` and ``UserChannel`` are separate Protocol
    classes — Phase 2 splits the previously-conflated single bus.
    """
    assert RequestBus is not UserChannel
    # Both must be runtime_checkable so isinstance() works for tests
    # and dispatch-time guards.
    assert hasattr(RequestBus, "_is_runtime_protocol")
    assert hasattr(UserChannel, "_is_runtime_protocol")


def test_intervention_bus_is_alias_of_request_bus() -> None:
    """Tier 2: legacy ``InterventionBus`` name is preserved as an alias
    of ``RequestBus`` so callers that import it stay valid.
    """
    assert InterventionBus is RequestBus


def test_request_bus_protocol_signature_is_request_iv_to_answer() -> None:
    """Tier 2: ``RequestBus.request`` shape is ``(iv) -> InterventionAnswer``."""
    sig = inspect.signature(RequestBus.request)
    params = list(sig.parameters.keys())
    # self + iv
    assert params == ["self", "iv"]


def test_user_channel_protocol_signature_is_deliver_iv_to_answer() -> None:
    """Tier 2: ``UserChannel.deliver`` shape is ``(iv) -> InterventionAnswer``."""
    sig = inspect.signature(UserChannel.deliver)
    params = list(sig.parameters.keys())
    assert params == ["self", "iv"]


# ── 2. Concrete buses satisfy both Protocols ───────────────────────────


def test_stdin_intervention_bus_satisfies_both_protocols() -> None:
    """Tier 2: ``StdinInterventionBus`` provides ``request`` AND
    ``deliver`` so it can be used in both Phase 2 (= legacy callers
    type as ``RequestBus``) and Phase 3+ (= Agent calls ``deliver``).
    """
    bus = StdinInterventionBus()
    assert isinstance(bus, RequestBus)
    assert isinstance(bus, UserChannel)


def test_chat_intervention_bus_satisfies_both_protocols(tmp_path: Path) -> None:
    """Tier 2: ``ChatInterventionBus`` satisfies both Protocols."""
    session = ChatSession(agent_name="t")
    bus = ChatInterventionBus(session, run_id="r1", skill_name="demo")
    assert isinstance(bus, RequestBus)
    assert isinstance(bus, UserChannel)


def test_a2a_intervention_bus_satisfies_both_protocols() -> None:
    """Tier 2: ``A2AInterventionBus`` satisfies both Protocols. Constructor
    takes a registry handle which we pass as ``None`` here since we
    don't invoke ``deliver`` / ``request`` in this protocol-only test.
    """
    bus = A2AInterventionBus(run_id="r1", registry=None)  # type: ignore[arg-type]
    assert isinstance(bus, RequestBus)
    assert isinstance(bus, UserChannel)


# ── 3. Phase 2 backwards-compat: request delegates to deliver ──────────


def test_stdin_bus_request_delegates_to_deliver() -> None:
    """Tier 2: ``StdinInterventionBus.request`` is a thin alias of
    ``deliver`` in Phase 2 — same answer, same side-effects.

    We swap out ``_read_line`` for a stub so the test does not require
    a real TTY / stdin attached.
    """
    bus = StdinInterventionBus()

    captured: list[str] = []

    async def _stub_read_line(prompt_text: str) -> str:
        captured.append(prompt_text)
        return "hello"

    bus._read_line = _stub_read_line  # type: ignore[method-assign]

    iv = UserIntervention(kind="ask_user", prompt="Q?")

    async def _drive_request() -> InterventionAnswer:
        return await bus.request(iv)

    answer = asyncio.run(_drive_request())
    assert answer.text == "hello"
    assert len(captured) == 1


def test_chat_bus_request_delegates_to_deliver() -> None:
    """Tier 2: ``ChatInterventionBus.request`` delegates to ``deliver``
    so existing callers using ``request`` see identical behaviour.

    Verified via attribute reference rather than running the dispatch
    (= ``deliver`` invokes ``_dispatch_intervention`` which is itself
    covered by ``test_intervention_subscriber_guard.py`` + adjacent
    session tests).
    """
    src = inspect.getsource(ChatInterventionBus.request)
    assert "self.deliver(iv)" in src, (
        "ChatInterventionBus.request must delegate to deliver in Phase 2"
    )


def test_a2a_bus_request_delegates_to_deliver() -> None:
    """Tier 2: ``A2AInterventionBus.request`` delegates to ``deliver``."""
    src = inspect.getsource(A2AInterventionBus.request)
    assert "self.deliver(iv)" in src, (
        "A2AInterventionBus.request must delegate to deliver in Phase 2"
    )


# ── 4. Outbox shape commitment (tui-coder Q1) ──────────────────────────


def test_outbox_intervention_meta_shape_is_stable() -> None:
    """Tier 2: ``OutboxMessage(kind="intervention").meta`` carries the
    canonical key set that TUI relies on.

    Phase 2 commitment to tui-coder (issue #254 comment thread): the
    meta shape must not drift as we re-classify the bus interfaces.
    Concretely the announce path in ``InterventionHandler.announce``
    builds meta via ``_iv_meta``; this test pins:

      - ``intervention_id``: always present
      - ``intervention_kind``: always present
      - ``prompt``: always present
      - ``detail``: present iff the iv carries a non-empty detail
      - ``choices``: present iff the iv carries choices, each a
        ``{"id", "label", "hotkey"}`` dict
      - ``run_id`` / ``run_id_short`` / ``skill_name``: opt-in fields

    If a Phase 2+ refactor renames any of these keys, TUI's outbox
    consumer breaks — this test fails first.
    """
    from reyn.chat.services.intervention_handler import _iv_meta

    iv_minimal = UserIntervention(kind="ask_user", prompt="Q?")
    meta_minimal = _iv_meta(iv_minimal)
    assert meta_minimal["intervention_id"] == iv_minimal.id
    assert meta_minimal["intervention_kind"] == "ask_user"
    assert meta_minimal["prompt"] == "Q?"
    assert "detail" not in meta_minimal
    assert "choices" not in meta_minimal

    iv_rich = UserIntervention(
        kind="permission.shell",
        prompt="Run ls?",
        detail="cmd: ls -la",
        run_id="r-1234",
        skill_name="demo",
        choices=[
            InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
            InterventionChoice(id="no", label="[N]o", hotkey="n"),
        ],
    )
    meta_rich = _iv_meta(iv_rich)
    assert meta_rich["intervention_id"] == iv_rich.id
    assert meta_rich["intervention_kind"] == "permission.shell"
    assert meta_rich["prompt"] == "Run ls?"
    assert meta_rich["detail"] == "cmd: ls -la"
    assert meta_rich["run_id"] == "r-1234"
    assert meta_rich["run_id_short"] == "1234"
    assert meta_rich["skill_name"] == "demo"
    assert meta_rich["choices"] == [
        {"id": "yes", "label": "[Y]es", "hotkey": "y"},
        {"id": "no", "label": "[N]o", "hotkey": "n"},
    ]


# ── 5. A2AInterventionBus responsibility scope (dogfood-coder Q1) ──────


def _names_used_in_module(module) -> set[str]:
    """Return every Name / Attribute / imported identifier referenced
    in ``module``'s source AST, EXCLUDING string literals and docstrings.

    Used to grep-pin that a module does not reference a forbidden
    symbol in actual code (imports, calls, attribute access) without
    false-positives from explanatory comments / docstrings.
    """
    import ast

    src = inspect.getsource(module)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_a2a_intervention_bus_does_not_import_skill_completed_handler() -> None:
    """Tier 2: A2AInterventionBus responsibility is scoped to delivering
    ``ask_user`` prompts to A2A peers — narration triggers (= the
    ``_handle_skill_completed`` lifecycle event chain) are an OS-internal
    layer and must NOT be referenced from this module.

    Phase 2 commitment to dogfood-coder (issue #254 comment thread):
    the [A]/[B] split decouples 'narration trigger' from 'intervention
    routing'. PR #253's auto-escalation path drains narration via
    ``RunRegistry`` separately; this channel only carries ``ask_user``
    prompt deliveries. The grep-pin guards against accidental
    re-conflation in future refactors.

    The check walks the AST so docstring references explaining the
    scope (= "this module MUST NOT import _handle_skill_completed")
    don't false-positive the assertion. Only actual imports / calls /
    attribute access count.
    """
    import reyn.web.a2a_intervention as mod

    names = _names_used_in_module(mod)
    forbidden = {
        "_handle_skill_completed",
        "skill_completion_injected",
    }
    leaks = names & forbidden
    assert not leaks, (
        f"A2AInterventionBus must not import / call / reference "
        f"narration-trigger symbols (issue #254 Phase 2 responsibility "
        f"scope), but found: {sorted(leaks)}"
    )


# ── 6. Phase 2 does not change __all__ exports of legacy names ─────────


def test_user_intervention_all_exports_include_new_and_legacy_names() -> None:
    """Tier 2: ``__all__`` carries ``RequestBus`` and ``UserChannel`` as
    new exports while keeping ``InterventionBus`` for backwards-compat.
    Drop the old name only in Phase 5 cleanup.
    """
    import reyn.user_intervention as mod

    assert "RequestBus" in mod.__all__
    assert "UserChannel" in mod.__all__
    assert "InterventionBus" in mod.__all__, (
        "InterventionBus must stay in __all__ for Phase 2 backwards-compat"
    )
