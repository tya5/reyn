"""Tier 2: #5678 — the ``Disclosure`` axis itself: the ladder ordering, the
required-for-``role="system"``/irrelevant-for-everything-else contract, the
legacy-migration default, and a static completeness scan that every literal
``role="system"`` construction in ``src/reyn`` also declares ``disclosure=``.

``role="system"`` carries two unrelated meanings (Reyn-internal chrome vs
producer-authored content meant for the model) — see ``Disclosure``'s own
docstring in ``chat_message.py`` for the full argument. This file is the
behavioural half of the axis; ``test_5677_mid_turn_injection_wire_rendering.py``
covers the ONE producer #5677/#5684 added (``AGENT_REQUEST`` mid-turn
injection) from the wire-rendering side.
"""
from __future__ import annotations

import ast

import pytest

from reyn.runtime.chat_message import (
    ChatMessage,
    Disclosure,
    _migrate_legacy_chat_message,
)
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# Ladder ordering — a structural decision, not an observation (architect
# ruling on #5678 §3: "MODEL implies operator-visible" must be expressed as
# real comparisons, not a separately-maintained membership set).
# ---------------------------------------------------------------------------


def test_the_three_rungs_are_a_real_total_order():
    """Tier 2: INTERNAL < OPERATOR < MODEL, and every consumer that asks
    "does this reach the operator" can therefore write ``>= OPERATOR``
    (true for OPERATOR AND MODEL) instead of maintaining a second,
    independently-updated set."""
    assert Disclosure.INTERNAL < Disclosure.OPERATOR < Disclosure.MODEL
    assert Disclosure.MODEL > Disclosure.OPERATOR > Disclosure.INTERNAL
    assert Disclosure.MODEL >= Disclosure.OPERATOR
    assert Disclosure.OPERATOR >= Disclosure.OPERATOR
    assert not (Disclosure.INTERNAL >= Disclosure.OPERATOR)
    assert Disclosure.INTERNAL <= Disclosure.MODEL


def test_ordering_only_defined_between_disclosure_members():
    """Tier 2: deny — comparing against a value with no meaningful order
    at all (an ``int``) raises, rather than an accidental comparison
    succeeding. NOTE: a plain ``str`` is NOT covered by this deny — since
    ``Disclosure`` IS a ``str`` subclass (``StrEnum``, matching
    ``Spillability``'s own precedent — see that class's docstring for
    why), ``__lt__``/``__gt__`` returning ``NotImplemented`` for a
    non-``Disclosure`` value lets Python fall back to the plain ``str``'s
    OWN reflected comparison, which succeeds lexicographically. Real
    callers always compare ``Disclosure`` to ``Disclosure`` (both
    call-site declarations and every consumer's own check), so this is a
    disclosed, accepted quirk of the ``StrEnum`` base, not a gap the
    override needs to close."""
    with pytest.raises(TypeError):
        Disclosure.MODEL < 3  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Required for role="system", irrelevant for everything else
# ---------------------------------------------------------------------------


def test_role_system_without_disclosure_raises():
    """Tier 2: #5678 accept — architect ruling, verbatim: an omitted
    declaration must be a LOUD failure, never a silent INTERNAL/MODEL
    guess. This is the fresh-construction half of that ruling (the other
    half, legacy read-back, is proven below)."""
    with pytest.raises(ValueError):
        ChatMessage(role="system", content="x")


def test_role_system_with_disclosure_succeeds_for_every_member():
    """Tier 2: accept — every ``Disclosure`` member is a valid
    declaration for ``role="system"``, round-tripping through
    ``ChatMessage.__init__`` unchanged."""
    for member in Disclosure:
        msg = ChatMessage(role="system", content="x", disclosure=member)
        assert msg.disclosure is member


def test_disclosure_is_none_for_every_non_system_role_even_if_passed():
    """Tier 2: the axis does not apply outside role="system" — a caller
    passing a value anyway (harmless, see #5677/#5684's own
    _render_mid_turn_injection call site, which passes disclosure=MODEL
    unconditionally regardless of whether the rendered role is "user" or
    "system") must not have it silently believed."""
    for role in ("user", "assistant", "tool", "summary"):
        msg = ChatMessage(role=role, content="x", disclosure=Disclosure.MODEL)
        assert msg.disclosure is None, (
            f"role={role!r} must ignore a passed disclosure=, got "
            f"{msg.disclosure!r}"
        )


# ---------------------------------------------------------------------------
# Legacy migration — a history.jsonl line persisted before #5678 must not
# raise on read-back, and must resolve to exactly what the OLD (pre-#5678)
# role/meta-based logic would have shown.
# ---------------------------------------------------------------------------


def test_legacy_turn_cancelled_line_migrates_to_operator():
    """Tier 2: what restore.py's pre-#5678 `meta.get("kind") ==
    "turn_cancelled"` rescue singled out (#3694) must migrate to the SAME
    outcome under the new axis."""
    raw = {
        "role": "system", "content": "Turn interrupted by user.",
        "meta": {"kind": "turn_cancelled", "chain_id": "c1"},
    }
    migrated = _migrate_legacy_chat_message(raw)
    assert migrated["disclosure"] == "operator"
    msg = ChatMessage(**migrated)
    assert msg.disclosure is Disclosure.OPERATOR


@pytest.mark.parametrize("meta", [{}, {"kind": "state_change"}, {"chain_id": "c1"}])
def test_legacy_non_turn_cancelled_system_line_migrates_to_internal(meta):
    """Tier 2: every OTHER pre-#5678 role="system" entry (state_change,
    hook pushes with no meta.kind, ride-alongs, SP chrome) must migrate to
    INTERNAL — exactly what the pre-#5678 blanket _SKIP_ROLES skip and the
    pre-#5678 build_history allowlist already did for all of them."""
    raw = {"role": "system", "content": "x", "meta": meta}
    migrated = _migrate_legacy_chat_message(raw)
    assert migrated["disclosure"] == "internal"
    msg = ChatMessage(**migrated)
    assert msg.disclosure is Disclosure.INTERNAL


def test_a_line_already_carrying_disclosure_is_not_touched():
    """Tier 2: a line written BY #5678-aware code (or already migrated)
    is left alone — the migration only fills a genuinely missing key,
    never overrides a real declaration."""
    raw = {
        "role": "system", "content": "x",
        "meta": {"kind": "turn_cancelled"}, "disclosure": "internal",
    }
    migrated = _migrate_legacy_chat_message(raw)
    assert migrated["disclosure"] == "internal", (
        "an explicit disclosure on the raw line must survive the "
        "migration unchanged, even one that disagrees with what the OLD "
        "meta.kind-based logic would have inferred"
    )


def test_non_system_legacy_line_is_untouched_by_the_disclosure_migration():
    """Tier 2: deny sibling — a legacy line for any OTHER role never
    gains a disclosure key at all (the axis does not apply there)."""
    raw = {"role": "user", "content": "hi"}
    migrated = _migrate_legacy_chat_message(raw)
    assert "disclosure" not in migrated


# ---------------------------------------------------------------------------
# Static completeness — every literal role="system" Call in src/reyn also
# passes disclosure= in the SAME call. Early-warning, structural (mirrors
# #3595/#5677's own AST-walk gates) — the actual hard backstop is
# ChatMessage.__init__'s runtime raise, proven above; this catches the gap
# BEFORE a test happens to exercise the missing site.
# ---------------------------------------------------------------------------


class _RoleSystemCallCollector(ast.NodeVisitor):
    """Every ``Call`` node with a literal ``role="system"`` keyword —
    records whether the SAME call also has a ``disclosure=`` keyword."""

    def __init__(self, module: str) -> None:
        self._module = module
        self.findings: "list[tuple[str, int, bool]]" = []  # (module, lineno, has_disclosure)

    def visit_Call(self, node: ast.Call) -> None:
        role_is_system = any(
            kw.arg == "role"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "system"
            for kw in node.keywords
        )
        if role_is_system:
            has_disclosure = any(kw.arg == "disclosure" for kw in node.keywords)
            self.findings.append((self._module, node.lineno, has_disclosure))
        self.generic_visit(node)


def _role_system_call_findings() -> "list[tuple[str, int, bool]]":
    out: "list[tuple[str, int, bool]]" = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _RoleSystemCallCollector(rel)
        collector.visit(tree)
        out.extend(collector.findings)
    return out


def test_extraction_is_not_vacuous():
    """Tier 2: positive control — if the walk stops finding ANY
    role="system" call (a refactor moves every producer behind a helper,
    a parser regression), the completeness test below passes VACUOUSLY.
    This pins that the walk still finds the module known to hold FOUR of
    the six #5678 producers (notify_state_change, notify_turn_cancelled,
    _handle_hook_message, the C ride-along flush) — a name check, not a
    count, so it survives sites being added or removed elsewhere without
    needing a threshold bump."""
    modules = {module for module, _lineno, _has in _role_system_call_findings()}
    assert "reyn/runtime/session.py" in modules, (
        f"expected to find literal role=\"system\" calls in session.py — "
        f"got modules {sorted(modules)!r}, extraction may have broken"
    )
    assert "reyn/runtime/router_loop.py" in modules, (
        f"expected to find router_loop.py's own #3694 terminal — got "
        f"modules {sorted(modules)!r}, extraction may have broken"
    )


def test_every_literal_role_system_call_also_declares_disclosure():
    """Tier 2: #5678's own structural gate — a literal role="system" Call
    with no disclosure= keyword in the SAME call is exactly the defect
    class this issue exists to close (a new producer silently missing
    the declaration). ChatMessage.__init__ raises at RUNTIME for this —
    this test catches it at COLLECTION time, before any test happens to
    exercise the missing site.

    Strip-falsifier: removing `disclosure=Disclosure.INTERNAL` from
    notify_state_change's ChatMessage(...) call makes this test go RED.
    """
    findings = _role_system_call_findings()
    missing = [
        (module, lineno) for module, lineno, has_disclosure in findings
        if not has_disclosure
    ]
    assert missing == [], (
        f"role=\"system\" constructed without disclosure= at: {missing!r} — "
        f"every such call must declare INTERNAL/OPERATOR/MODEL explicitly "
        f"(#5678)"
    )


# ---------------------------------------------------------------------------
# Acceptance item 3 — the two allowlists are NOT the same filter (lead-coder's
# own corrected measurement): build_history's own turns filter
# (_elide_candidate_turns) never admits role="summary"; decompose_history_
# for_retry's own turns filter does (#5531 invariant — a summary sits at its
# own chronological position). A refactor that "simplifies" the two into one
# shared role tuple would silently drop the summary from build_history's
# input OR admit it (visible only downstream, awkwardly) — this pins the
# ACTUAL difference behaviorally, through the public API, not by re-reading
# the two role tuples as text.
# ---------------------------------------------------------------------------


def _make_disclosure_session(tmp_path):
    from reyn.core.events.state_log import StateLog
    from tests._support.agent_session import make_session
    return make_session(
        agent_name="5678-allowlist-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


def test_summary_role_reaches_retry_decompose_but_not_build_history(tmp_path):
    """Tier 2: #5678 acceptance item 3 — SUMMARY_MESSAGE_ROLE passes
    decompose_history_for_retry's own allowlist but not build_history's,
    UNCHANGED by #5678's own widening (both filters gained the SAME new
    `is_model_visible` admission (relocated to chat_message.py by #5699),
    applied ON TOP of their pre-existing, genuinely different role
    tuples — not collapsed into one).

    Strip-falsifier: replacing `_elide_candidate_turns`'s own role tuple
    with the one `decompose_history_for_retry` uses (i.e. accidentally
    "unifying" the two filters) makes this test's build_history() half go
    RED — a summary would leak into build_history's raw turns.
    """
    from reyn.services.compaction.engine import SUMMARY_MESSAGE_ROLE

    session = _make_disclosure_session(tmp_path)
    session._append_history(ChatMessage(role="user", content="hi", ts="t1"))
    session._append_history(ChatMessage(
        role=SUMMARY_MESSAGE_ROLE, content="summary of earlier turns", ts="t2",
    ))
    session._append_history(ChatMessage(role="assistant", content="ok", ts="t3"))

    history_buffer = session._loop_driver._history_buffer

    built = history_buffer.build_history()
    assert not any(m.get("role") == SUMMARY_MESSAGE_ROLE for m in built), (
        f"build_history() must never admit a raw role={SUMMARY_MESSAGE_ROLE!r} "
        f"turn (it attaches summary content via its own synthetic bridge "
        f"instead) — got {built!r}"
    )

    head, raw_middle, tail, _summary, _seq_by_id = history_buffer.decompose_history_for_retry()
    decomposed = [*head, *raw_middle, *tail]
    assert any(m.get("role") == SUMMARY_MESSAGE_ROLE for m in decomposed), (
        f"decompose_history_for_retry() must admit the raw "
        f"role={SUMMARY_MESSAGE_ROLE!r} turn (#5531 — placed at its own "
        f"chronological position) — got {decomposed!r}"
    )


# ---------------------------------------------------------------------------
# Acceptance item 5 — E's content reaches turn N's prompt EXACTLY ONCE, never
# twice (the double-delivery architect's own §0 read named as the trap a
# naive allowlist-widen-only fix would fall into). A test that ASSERTS the
# count, not a human observing it once (architect's own instruction: "二度に
# なったら赤になる test").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_content_reaches_the_turn_prompt_exactly_once(tmp_path):
    """Tier 2: #5678 acceptance item 5 — a hook-driven (E) turn's own
    content appears in the REAL wire ``messages`` list ``RouterLoop``
    passes to the LLM exactly ONCE, never twice.

    Before #5678: E's history entry (role="system") was excluded from
    build_history's allowlist, so it never appeared in `history` at all
    — the ONLY copy was the turn-seed `_run_router_loop` passed
    separately. #5678 widens the allowlist so the SAME content ALSO
    appears via the projected history entry; #5686's own `user_text=None`
    bridge is what stops the (now redundant) separate seed from ALSO
    being appended — this test is the assertion that the two changes,
    landed together, do not reintroduce a double copy.

    Drives the REAL ``RouterLoopDriver.run_turn`` -> ``RouterLoop.run``
    chain (not a stubbed ``run_turn``, which would bypass the exact
    double-delivery seam under test) — only the LLM boundary itself is
    replaced, via the SAME ``_loop_observer`` Tier-2 test seam
    ``RouterLoop`` already exposes for real-fake ``_llm_caller``
    injection (no unittest.mock/patch).

    Strip-falsifier: reverting `_handle_hook_message`'s
    `_run_router_loop(None, chain_id)` call back to passing `attributed`
    (or `text`) makes this test go RED with a count of 2, not 1.
    """
    from reyn.core.events.state_log import StateLog
    from tests._support.agent_session import make_session
    from tests._support.router_loop import text_result

    state_log = StateLog(tmp_path / "state.wal")
    session = make_session(
        agent_name="5678-e-once-agent", state_log=state_log,
        snapshot_path=tmp_path / "snapshot.json",
    )

    seen_messages: "list[list[dict]]" = []

    async def _recording_llm(**kwargs):
        seen_messages.append(list(kwargs["messages"]))
        return text_result("done")

    def _inject_llm_caller(loop):
        loop._llm_caller = _recording_llm

    session._loop_driver._loop_observer = _inject_llm_caller

    await session._put_inbox(
        "hook", {"name": "on_idle", "text": "status check", "chain_id": "c1"},
    )
    await session.run_one_iteration()

    (turn_messages,) = seen_messages  # exactly one LLM call happened
    # Role-agnostic on purpose: the exact defect this pins is the OLD
    # fallback guard adding a SECOND copy under a DIFFERENT role
    # (role="user", the double-delivery seed) alongside the projected
    # role="system" history entry — a role="system"-filtered count would
    # miss that second copy entirely (caught by strip-falsify: an
    # earlier role-filtered version of this assertion stayed GREEN with
    # the bug reintroduced, because the duplicate landed as role="user").
    occurrences = sum(
        1 for m in turn_messages
        if "status check" in str(m.get("content", ""))
    )
    assert occurrences == 1, (
        f"E's content must appear in the turn's own prompt exactly once, "
        f"got {occurrences} — {turn_messages!r}"
    )
    await state_log.aclose()
