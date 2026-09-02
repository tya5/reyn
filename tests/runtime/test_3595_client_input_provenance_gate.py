"""#3595 S2/S3 — every site that names ``TurnOrigin.CLIENT_INPUT`` is enumerated.

``CLIENT_INPUT`` is a claim ("a human typed this at a first-party client") that
the OS then acts on: it is the ONLY inbox kind whose text ``Session.
_handle_user_message`` hands to slash dispatch. Four producers made that claim
untruthfully and every registered slash command became reachable from model
output, from a Slack message, from an MCP peer and from a cron job (#3595 steps 1
/ 1b). This file is what keeps the set of claimants a decision rather than an
accident: a new site either appears in ``_CLIENT_INPUT_SITES`` with a reason a
reviewer weighed, or the gate is RED.

★ **Why this gate could not be written before the type existed.** Five separate
censuses of "who claims to be a user?" ran on #3595, and each missed a different
site — because each searched for a *spelling* and the claim had several:
``kind="user"`` (kwarg), ``_put_inbox("user", …)`` (bare positional),
``inbox_kind="user"`` (a third kwarg name), and a value could equally have
travelled through a variable or a dict literal. A pattern-shaped gate freezes the
pattern's blind spot: whatever form it cannot see becomes permanently invisible,
with a green test asserting otherwise. Closing the type is what removes the
degree of freedom — the member has exactly ONE spelling, so "find the claimants"
becomes "find the references to this symbol", which an AST walk answers without
choosing a form. That is the whole reason S1 (the type) had to land before S3
(this gate) and not the other way around.

★ **The standing positive control.** ``test_no_declared_site_is_stale`` requires
every registry entry to still be FOUND by the walk. It is not bookkeeping: it is
the positive control, wired in permanently. The 5th miss on this arc was an AST
scan that filtered by a function-name allowlist and therefore silently returned
zero for the three ``inbox_kind=`` sites; the single question that caught it was
"does ``chat.py`` show up in my own extractor?". With ``chat.py`` in this
registry, an extractor that stops seeing keyword-argument references cannot
report a clean census — it REDs here instead.

⚠️ **What this gate does NOT cover.** It counts references to the SYMBOL. A
producer that regressed to the pre-S1 world by passing the bare wire string
``"user"`` would inject a client-input-kinded message without appearing here.
That leg is closed by the type rather than by a search:
``test_inbox_injection_seams_type_their_kind_parameter`` reads the LIVE
signatures and fails if any inbox seam's kind parameter is widened back to
``str``, which is the only way such a call could stop being a static type error.
Two mechanisms, stated separately, because a single test claiming both would be
claiming more than either measures.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from pathlib import Path

from reyn.mcp.server import send_to_agent_impl
from reyn.runtime.message_bus import MessageBus
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"

#: The member whose claimants this file enumerates. Named through the type, not
#: spelled, so a rename of the member moves this gate with it instead of leaving
#: it silently matching nothing.
_MEMBER = TurnOrigin.CLIENT_INPUT.name


# ── the walk ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Site:
    """One source location that names ``TurnOrigin.CLIENT_INPUT``."""

    module: str          # path relative to src/
    qualname: str        # enclosing class/function chain, "<module>" at top level
    lineno: int

    @property
    def key(self) -> "tuple[str, str]":
        return (self.module, self.qualname)


class _MemberRefCollector(ast.NodeVisitor):
    """Collect every attribute access whose attribute name is the member.

    ★ Deliberately keyword-blind, callee-blind and argument-position-blind. It
    does not ask which function is being called, whether the reference is a
    keyword argument, a positional argument, a dict value, a comparison operand,
    or a plain assignment — every one of those is a site that names the member,
    and every previous census on this arc was wrong because it asked one of those
    questions first. The only thing being matched is the symbol.
    """

    def __init__(self, module: str) -> None:
        self._module = module
        self._scope: list[str] = []
        self.sites: list[_Site] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _MEMBER:
            self.sites.append(_Site(
                module=self._module,
                qualname=".".join(self._scope) or "<module>",
                lineno=node.lineno,
            ))
        self.generic_visit(node)


def _client_input_sites(root: "Path | None" = None) -> "list[_Site]":
    """Every reference to the member in ``src/reyn`` (or ``root``)."""
    base = root if root is not None else _SRC
    out: list[_Site] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _MemberRefCollector(rel)
        collector.visit(tree)
        out.extend(collector.sites)
    return out


# ── the registry ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SiteDeclaration:
    """Why a site is entitled to name ``CLIENT_INPUT``, and how many times.

    ``role`` splits the two things a reference can be doing, because only one of
    them is a trust claim:

    - ``"asserts"`` — the site PUTS a message on the inbox carrying this member.
      It is asserting that a human typed the text, and the OS will hand a
      ``/``-prefixed line to slash dispatch on the strength of that assertion.
      A new one of these is a security decision.
    - ``"reads"`` — the site branches on a kind it received. It asserts nothing;
      it is downstream of somebody else's assertion. Still enumerated, because
      "which code changes behaviour based on this claim" is the other half of
      knowing what the claim buys, and because a reader that quietly widens (a
      new consumer treating the member as permission for something new) is a
      change worth a reviewer's eye.

    ⚠️ ``reason`` is an INTENT record. Nothing here reads the site's
    implementation, so a site whose prose and whose code disagree stays green.
    ``measured_by`` is the behavioural half: the test that drives the site for
    real. An ``asserts`` site with an empty ``measured_by`` is visibly unmeasured
    rather than invisibly so.
    """

    role: str            # "asserts" | "reads"
    reason: str
    occurrences: int = 1
    measured_by: "tuple[str, ...]" = field(default=())


_T3561 = "tests/runtime/test_3561_spawn_session_seam_reachability.py"
_T3595_S1B = "tests/runtime/test_3595_step1b_external_producer_slash_reachability.py"
_T3595_S2 = "tests/runtime/test_3595_s2_pipeline_nudge_origin.py"
_TCLI = "tests/interfaces/test_chat_cli_flags.py"
_T3300 = "tests/interfaces/test_3300_p2a_queue_state_publish.py"

#: Every site in ``src/`` that names the member, with the reason it may.
#:
#: ``occurrences`` is exact rather than a lower bound: two references inside one
#: function collapse to one key, and a gate keyed on presence alone would let a
#: second, unreviewed claim be added to an already-blessed function. The dogfood
#: runner is the live instance of a key with more than one.
_CLIENT_INPUT_SITES: "dict[tuple[str, str], _SiteDeclaration]" = {
    ("reyn/runtime/session.py", "Session.submit_user_text"): _SiteDeclaration(
        role="asserts",
        reason=(
            "THE client seam. Every first-party composer — the TUI, the plain CUI, "
            "the AG-UI/remote client — converges here, so this is the one site whose "
            "claim is true by construction rather than by argument: its caller is a "
            "person at a keyboard. If a future producer needs to make this claim, the "
            "question to ask first is why it is not calling this."
        ),
        measured_by=(
            f"{_T3561}::test_an_operator_submitted_slash_command_still_spawns_a_session",
            f"{_T3595_S1B}::test_an_operator_submitted_slash_command_still_spawns_a_session",
        ),
    ),
    ("reyn/interfaces/cli/commands/chat.py", "_run_once"): _SiteDeclaration(
        role="asserts",
        reason=(
            "`reyn run-once` pipes the operator's own line in on stdin and drives it "
            "through send_to_agent_impl, whose DEFAULT is the external kind because "
            "its other callers are out-of-process peers. The operator did type this "
            "line; only the delivery route is unusual, and `echo \"/model x\" | reyn "
            "run-once` executing the command is behaviour this arc explicitly "
            "preserved rather than a leak it tolerated."
        ),
        measured_by=(f"{_TCLI}::test_no_restore_skips_transcript_load",),
    ),
    (
        "reyn/interfaces/cli/commands/dogfood.py",
        "_build_live_runner.runner_fn",
    ): _SiteDeclaration(
        role="asserts",
        reason=(
            "The dogfood scenario runner stands in for an operator: a human-authored "
            "line replayed AS the operator's own typing, and the whole point of the "
            "instrument is to exercise the path an operator's line takes. Any other "
            "kind would measure a path no operator uses. ★ It is a harness, not a "
            "production route — the entitlement rests on the harness never being "
            "reachable from a request; it ships behind `reyn dogfood`, an operator "
            "CLI subcommand. Two occurrences: the multi-turn and single-turn arms."
        ),
        occurrences=2,
        measured_by=(),
    ),
    ("reyn/runtime/session.py", "Session._run_turn_body"): _SiteDeclaration(
        role="reads",
        reason=(
            "The dispatch itself: the member selects _handle_user_message, which is "
            "the slash-dispatch entry, and every other member routes to "
            "_handle_inbox_text. This branch IS what the claim buys, so it is the one "
            "reader whose behaviour a reviewer must re-derive if the member's meaning "
            "ever moves."
        ),
        measured_by=(
            f"{_T3561}::test_model_output_cannot_reach_slash_dispatch_and_spawns_nothing",
            f"{_T3561}::test_an_operator_submitted_slash_command_still_spawns_a_session",
            f"{_T3595_S2}::test_a_pipeline_nudge_turn_cannot_execute_a_slash_command",
        ),
    ),
    ("reyn/runtime/session.py", "Session._stamp_execution_context"): _SiteDeclaration(
        role="reads",
        reason=(
            "Turn-provenance classification (proposal 0060 A7): the member is the ONLY "
            "kind granting `user_directed`; everything else — known or unknown — falls "
            "to the stricter `auto_improvement`. Reads it as an if/else fail-safe, not "
            "a lookup, so an unmapped kind cannot reach the permissive side."
        ),
        measured_by=(),
    ),
    ("reyn/runtime/session.py", "Session._run_router_loop"): _SiteDeclaration(
        role="reads",
        reason=(
            "#5648: the rewind-timeline anchor's own source selection. "
            "`_current_turn_kind` (the RAW value `_stamp_execution_context` "
            "saw for THIS turn, kept separately from the 2-way "
            "`_current_turn_origin` collapse) compared to CLIENT_INPUT "
            "decides whether the anchor uses this turn's own `user_text` "
            "(a genuine human prompt) or walks history backward for the "
            "last one (a hook/cron/external-message/peer-session turn's "
            "own triggering text is not a prompt anyone typed). Reads it "
            "as a 3-way check (CLIENT_INPUT / a known other kind / never "
            "stamped at all), never grants any permission — the checkpoint "
            "cut this feeds is unconditional either way."
        ),
        measured_by=(
            "tests/runtime/test_5648_rewind_anchor_prefers_human_prompt.py"
            "::test_hook_driven_checkpoints_anchor_on_the_preceding_human_prompt",
            "tests/runtime/test_web_rewind_attach_seam_2d.py"
            "::test_web_path_session_records_anchor_for_picker",
        ),
    ),
    # (#5561, owner ruling, retired the #1800 slice-7 loop valve entirely —
    # this dict previously declared ("reyn/runtime/session.py",
    # "Session.run_one_iteration") here: the valve's reset-on-CLIENT_INPUT
    # step, which is what made this site claim the member. The site is
    # genuinely gone (run_one_iteration's body no longer references
    # TurnOrigin.CLIENT_INPUT at all, verified directly) — this is the
    # correct disposition, a site vanishing because its own reason for
    # existing was removed, not the walk losing sight of a live one.)
    ("reyn/runtime/turn_origin.py", "<module>"): _SiteDeclaration(
        role="reads",
        reason=(
            "#5677: MID_TURN_INJECTABLE's own declaration names CLIENT_INPUT "
            "as one of its 2 members (the founding case #3792 built the whole "
            "mid-turn-injection feature for — a human steering a running tool "
            "loop). This is the ONE place that decides mid-turn INTERRUPT "
            "eligibility, a separate question from slash-dispatch trust (this "
            "gate's own subject) that used to be answered by reusing THIS "
            "member's predicate — see MID_TURN_INJECTABLE's own module "
            "docstring for the full separation."
        ),
        measured_by=(
            "tests/core/test_3792_pr2_session_injection.py::"
            "test_only_mid_turn_injectable_origins_are_peek_eligible",
        ),
    ),
    ("reyn/runtime/session.py", "_render_mid_turn_injection"): _SiteDeclaration(
        role="reads",
        reason=(
            "#5677: dispatches an injected item's WIRE/history rendering on "
            "its own kind — CLIENT_INPUT renders unchanged (role=\"user\", "
            "bare text, the #3792 shape); every OTHER MID_TURN_INJECTABLE "
            "member renders role=\"system\" with an attributed "
            "[<kind>:<name>] prefix, so a non-human producer's injected text "
            "can never again be indistinguishable from the operator's own "
            "(architect's §0 finding on #5677 — widening eligibility without "
            "widening this rendering would reproduce THIS gate's own closed "
            "defect class one layer down, on the mid-turn wire)."
        ),
        measured_by=(
            "tests/runtime/test_5677_mid_turn_injection_wire_rendering.py::"
            "test_client_input_injection_renders_role_user_unchanged",
        ),
    ),
    # #5677: the ("reyn/runtime/inbox_arbiter.py",
    # "InboxArbiter.peek_mid_turn_injection") entry that used to live here
    # is GONE, not forgotten — verified directly (`git grep
    # 'TurnOrigin.CLIENT_INPUT' -- src/reyn/runtime/inbox_arbiter.py`
    # finds nothing). That method no longer names CLIENT_INPUT at all: its
    # eligibility check reads `kind not in MID_TURN_INJECTABLE`
    # (turn_origin.py), a set CLIENT_INPUT is one member of, declared with
    # its own reason next to the member — not this file's job any more,
    # because "may this producer claim CLIENT_INPUT for slash-dispatch
    # trust" (what this gate polices) and "may this producer interrupt an
    # already-running turn" (MID_TURN_INJECTABLE's own question) are
    # answers to two DIFFERENT questions that used to share one predicate
    # by accident (#5677's own finding). test_3595_new_axis_mid_turn_
    # injectable_gate.py is the sibling gate for the second question —
    # same shape, different member, per architect's own co-vet requirement
    # that widening this axis needs its own enumeration test, not silence.
    ("reyn/runtime/session.py", "Session.queued_user_messages"): _SiteDeclaration(
        role="reads",
        reason=(
            "The server-authoritative sent-queue (#3300 P2a) filters the snapshot "
            "inbox to this member. The sent queue renders what THIS operator "
            "submitted from a client; a peer's Slack message, a cron fire and the "
            "attached-pipeline nudge were never that, and each stopped appearing here "
            "as a consequence of its producer being told to say what it is."
        ),
        measured_by=(
            f"{_T3300}::test_queued_user_messages_reflects_undispatched_inbox_queue",
            f"{_T3595_S2}::test_a_pipeline_nudge_is_not_a_queued_operator_submission",
        ),
    ),
}


# ── the gate ──────────────────────────────────────────────────────────────────


def test_extraction_is_not_vacuous() -> None:
    """Tier 2: the walk finds sites at all — a silent zero is the failure mode
    that makes a completeness gate worse than no gate.

    An extractor that returns nothing satisfies "every found site is declared"
    perfectly, so every other assertion in this file is conditional on this one.
    The forms this arc's censuses missed all failed this way: they returned a
    short, clean, wrong list, and nothing in the run said so.
    """
    sites = _client_input_sites()
    assert sites, (
        "the walk found NO reference to TurnOrigin.CLIENT_INPUT anywhere in src/. "
        "Either the member was renamed (update _MEMBER's source, which reads it "
        "off the type) or the walk is broken — in both cases every other "
        "assertion in this file is passing vacuously."
    )


def test_walk_sees_every_syntactic_form_a_claim_can_take() -> None:
    """Tier 2: the walk is blind to argument FORM — kwarg, bare positional, dict
    value and via-variable are all found.

    Each of those four forms is a real miss from this arc's censuses, so this is
    not a hypothetical: a `kind="user"` grep dropped the two bare-positional
    ``_put_inbox("user", …)`` sites, and an AST scan filtered by callee name
    dropped the three ``inbox_kind=`` sites. Run against a fixture rather than
    against production so it measures the walk's capability and not today's
    call-site layout — the walk must keep seeing a form even while no production
    site happens to use it.
    """
    source = (
        "from reyn.runtime.turn_origin import TurnOrigin\n"
        "def kwarg(): put(kind=TurnOrigin.CLIENT_INPUT)\n"
        "def positional(): put(TurnOrigin.CLIENT_INPUT, {})\n"
        "def dict_literal(): put({'kind': TurnOrigin.CLIENT_INPUT})\n"
        "def via_variable():\n"
        "    k = TurnOrigin.CLIENT_INPUT\n"
        "    put(kind=k)\n"
    )
    collector = _MemberRefCollector("fixture.py")
    collector.visit(ast.parse(source))
    found = {s.qualname for s in collector.sites}
    assert found == {"kwarg", "positional", "dict_literal", "via_variable"}, (
        "the walk did not see every syntactic form a provenance claim can take; "
        f"it found {sorted(found)!r}. A form it cannot see becomes a permanently "
        "invisible claimant with a green gate asserting otherwise."
    )


def test_every_client_input_site_is_declared() -> None:
    """Tier 2: no site names the member without a registered reason.

    The completeness half. A new producer claiming to be a first-party client
    input is a security decision — it decides whether text from that producer can
    execute a registered slash command — so it lands in the registry with the
    argument that justified it, or it does not land.
    """
    undeclared = sorted({
        s.key for s in _client_input_sites() if s.key not in _CLIENT_INPUT_SITES
    })
    assert not undeclared, (
        "site(s) name TurnOrigin.CLIENT_INPUT without a declared reason: "
        f"{undeclared!r}. If the site PUTS a message on the inbox, it is claiming "
        "a human typed the text and thereby that a leading '/' may execute a slash "
        "command — say why that is true. If it merely READS the kind, say what it "
        "does differently. Then add the entry to _CLIENT_INPUT_SITES."
    )


def test_no_declared_site_is_stale() -> None:
    """Tier 2: every declared site is still FOUND by the walk, at its declared
    count — the standing positive control.

    Two failures collapse into this one assertion and both matter:

    - a site that moved or was deleted leaves a dead permission in the registry,
      and dead permissions are how a gate stops describing the code;
    - ★ a WALK that regressed — stopped seeing a form, gained a filter — makes
      declared sites vanish from the found set. That is the 5th miss on this arc
      exactly: an AST scan with a function-name allowlist reported zero for the
      three ``inbox_kind=`` sites and looked clean. With those sites declared
      here, the same regression is RED instead.
    """
    counts: dict[tuple[str, str], int] = {}
    for site in _client_input_sites():
        counts[site.key] = counts.get(site.key, 0) + 1
    for key, decl in sorted(_CLIENT_INPUT_SITES.items()):
        assert key in counts, (
            f"_CLIENT_INPUT_SITES declares {key!r}, but the walk does not find it. "
            "Either the site is gone (drop the entry) or the walk stopped seeing "
            "its form — the second is the failure this test exists for, because it "
            "makes an incomplete census look complete."
        )
        assert counts[key] == decl.occurrences, (
            f"{key!r} declares {decl.occurrences} reference(s) but the walk finds "
            f"{counts[key]}. A second claim inside an already-declared function is "
            "still a new claim; re-read the reason and update the count."
        )


def test_asserting_sites_name_a_reason_and_readers_name_theirs() -> None:
    """Tier 2: the registry is an index of arguments, and of the tests that
    measure them.

    Every entry carries prose; every ``asserts`` entry — the ones making the
    trust claim — carries either a behavioural test or, by its absence, an
    admission that it has none. A named test that does not exist fails here, so a
    rename cannot leave a site silently declared-but-unmeasured.
    """
    repo = REPO_ROOT
    for key, decl in sorted(_CLIENT_INPUT_SITES.items()):
        assert decl.role in ("asserts", "reads"), f"{key!r}: unknown role"
        assert decl.reason.strip(), f"{key!r} declares no reason"
        for ref in decl.measured_by:
            path_part, _, test_name = ref.partition("::")
            path = repo / path_part
            assert path.is_file(), f"{key!r} names a missing test file: {ref}"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            defined = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert test_name in defined, (
                f"{key!r} names a behavioural test that does not exist: {ref}"
            )


def test_inbox_injection_seams_type_their_kind_parameter() -> None:
    """Tier 2: every seam a message rides onto the inbox through annotates its
    kind parameter as ``TurnOrigin``, read off the LIVE signature.

    This is the other leg, and it is a different mechanism from the walk above.
    The walk counts references to the symbol; it cannot see a producer that
    regressed to passing the bare wire string ``"user"``, which still works at
    runtime because the member IS that string. What stops such a call from being
    written is that it is a static type error — and that property lives entirely
    in these annotations. Widen any of them back to ``str`` and the pre-#3595
    world returns silently, with this file's other tests still green. Reading the
    signature rather than the source is what makes the check outlive a
    reformatting or a move.
    """
    seams = [
        ("Session._put_inbox", Session._put_inbox, "kind"),
        ("Session._cross_session_hook_put", Session._cross_session_hook_put, "kind"),
        ("MessageBus.request", MessageBus.request, "kind"),
        ("SnapshotJournal.append_inbox", SnapshotJournal.append_inbox, "kind"),
        ("mcp.server.send_to_agent_impl", send_to_agent_impl, "inbox_kind"),
    ]
    for name, func, param in seams:
        hints = inspect.signature(func).parameters
        assert param in hints, f"{name} no longer takes a {param!r} parameter"
        annotation = hints[param].annotation
        rendered = (
            annotation if isinstance(annotation, str) else getattr(
                annotation, "__name__", str(annotation),
            )
        )
        assert "TurnOrigin" in rendered, (
            f"{name}'s {param!r} parameter is annotated {rendered!r}, not "
            "TurnOrigin. That annotation is the ONLY thing making a bare "
            "kind=\"user\" a static error rather than a working call — widening it "
            "reopens #3595's defect class without touching any site this file's "
            "walk can see."
        )
