"""Tier 2: the `_untrusted` narrowing is opt-in, and its deny names itself (#3501).

Two properties, each with its own arms:

**A — opt-in.** The narrowing engages only when the operator sets
``safety.threat_scan.capability_narrowing`` away from ``off``. The default-posture
arms that reach through a real ``Session`` live in
``test_context_auto_1827_s4b.py`` and ``test_1909_intra_turn_opt_in_narrowing.py``
(both have a strip-falsify arm for the gate); this file pins the config contract
itself — the vocabulary, the rejection of a typo, and the fact that the dotted key
the deny message advertises resolves to a real field.

**B — legible deny.** A contextual deny must name WHICH narrowing fired, WHY, and
WHAT lifts it, in the string the model receives. Arms here assert the three parts
are present as *information* (the narrowing's own label, its cause, its lift
condition) — never by pinning the exact sentence, which would be a Tier-4 format
pin and would break on any rewording that kept the message just as useful.

Every collaborator is real: real ``ThreatScanConfig`` / ``SafetyConfig``, the real
``_untrusted`` profile through the real ``load_untrusted_profile`` /
``resolve_profile`` pair, a real ``PermissionResolver``, and the real
``ContextualPermission`` the gate evaluates. Nothing here hand-rolls a stand-in —
per policy, and pointedly so for a permission gate (#3037's invented
``permission_resolver`` field made a dead gate look tested).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from reyn.config.chat import (
    CAPABILITY_NARROWING_MODES,
    SafetyConfig,
    ThreatScanConfig,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.security.permissions.capability_profile import (
    UNTRUSTED_NARROWING_CONFIG_KEY,
    UNTRUSTED_NARROWING_ORIGIN,
    compose_resolved,
    delegate_floor_origin,
    load_untrusted_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualPermission,
    NarrowingOrigin,
    attribute_deny,
    contextual_deny_message,
    narrowing_terms,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)
_REMEMBER_ARGS = {
    "slug": "y", "name": "n", "description": "d", "type": "user", "body": "x",
}


def _tool_call_result(calls: "list[dict]") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None,
        tool_calls=[
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"], "arguments": json.dumps(c.get("args", {})),
                },
            }
            for c in calls
        ],
        finish_reason="tool_calls",
        usage=_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text, tool_calls=[], finish_reason="stop", usage=_USAGE,
    )


def _scripted_llm(rounds: list):
    """A real async callable standing in for ``call_llm_tools`` (policy: a real
    callable, so signature drift raises here as it would in production)."""
    state = {"n": 0}

    async def _call(**kwargs: object) -> LLMToolCallResult:
        r = rounds[state["n"]]
        state["n"] += 1
        return r
    return _call

# One tool from the built-in `_untrusted` deny-set. Which one does not matter —
# `test_2111_floor_alias_completeness.py` is what keeps the whole set denied; this
# is a witness for the MESSAGE, so any member serves.
_FLOORED_TOOL = "remember_shared"


def _untrusted_term() -> ContextualPermission:
    """The real ephemeral term, resolved the way ``Session`` resolves it."""
    return resolve_profile(
        load_untrusted_profile(Path("/nonexistent-project-root")),
        origin=UNTRUSTED_NARROWING_ORIGIN,
    )[0]


# ── A: the opt-in config contract ───────────────────────────────────────────


def test_capability_narrowing_defaults_to_off() -> None:
    """Tier 1: the shipped default is ``off`` — the narrowing is opted INTO."""
    assert ThreatScanConfig().capability_narrowing == "off"
    assert SafetyConfig().threat_scan.capability_narrowing == "off"
    assert ThreatScanConfig().narrowing_engaged() is False
    assert ThreatScanConfig().narrowing_per_iteration() is False


def test_the_ladder_is_ordered_and_iteration_implies_engaged() -> None:
    """Tier 2: one setting, three rungs, strictly increasing.

    ``iteration`` must satisfy ``narrowing_engaged`` too — the whole reason this is
    one ladder rather than two booleans is that "re-narrow every iteration but do
    not narrow" is not a reachable state.
    """
    assert CAPABILITY_NARROWING_MODES == ("off", "turn", "iteration")
    engaged = {
        m: ThreatScanConfig(capability_narrowing=m).narrowing_engaged()
        for m in CAPABILITY_NARROWING_MODES
    }
    per_iteration = {
        m: ThreatScanConfig(capability_narrowing=m).narrowing_per_iteration()
        for m in CAPABILITY_NARROWING_MODES
    }
    assert engaged == {"off": False, "turn": True, "iteration": True}
    assert per_iteration == {"off": False, "turn": False, "iteration": True}


def test_an_unknown_rung_is_rejected_not_silently_downgraded() -> None:
    """Tier 1: a typo raises rather than resolving to some rung.

    Falling back to ``off`` would silently drop hardening the operator asked for;
    falling back to a stricter rung would silently impose one they did not. Both
    are worse than a load-time error naming the legal values.
    """
    with pytest.raises(ValueError) as exc:
        ThreatScanConfig(capability_narrowing="on")
    message = str(exc.value)
    assert "capability_narrowing" in message
    for mode in CAPABILITY_NARROWING_MODES:
        assert mode in message, "the error must name the legal values"


def test_the_config_key_the_deny_advertises_resolves_to_a_real_field() -> None:
    """Tier 2: the dotted key in the deny message is a real config path.

    ``UNTRUSTED_NARROWING_CONFIG_KEY`` is a literal in ``capability_profile.py``
    (security must not import config), so nothing but this arm stops a rename from
    shipping a deny message that points an operator at a key which does not exist.
    Walked attribute-by-attribute from a real ``SafetyConfig``.
    """
    head, *path = UNTRUSTED_NARROWING_CONFIG_KEY.split(".")
    assert head == "safety", UNTRUSTED_NARROWING_CONFIG_KEY
    target: object = SafetyConfig()
    for part in path:
        assert hasattr(target, part), (
            f"{UNTRUSTED_NARROWING_CONFIG_KEY} breaks at {part!r}"
        )
        target = getattr(target, part)
    assert target == "off", "and it must resolve to the default-off value"


# ── B: the deny names which narrowing fired ─────────────────────────────────


def test_the_untrusted_deny_names_itself_its_cause_and_its_lift_conditions() -> None:
    """Tier 2: the message the model receives carries all three parts.

    Asserted as information, not as a sentence: the profile name (which), the
    marker that makes it active (why), and BOTH lift routes — the taint leaving
    the context, and the config key. Naming only the first would leave an operator
    who wants the capability back permanently with nothing to act on.
    """
    message = contextual_deny_message("tool", _FLOORED_TOOL, _untrusted_term())
    assert _FLOORED_TOOL in message
    assert "_untrusted" in message, "which narrowing"
    assert "external_source" in message, "why it is active"
    assert "compacted out" in message, "the condition that lifts it on its own"
    assert UNTRUSTED_NARROWING_CONFIG_KEY in message, "the setting that disables it"


def test_a_composed_narrowing_attributes_the_deny_to_the_term_that_made_it() -> None:
    """Tier 2: composition keeps provenance — the load-bearing property.

    The gate evaluates one flattened ∩ term; without ``composed_from`` the deny
    site could not tell an envelope denial from the untrusted one, which is the
    whole defect. Two terms deny two different tools; each deny must be attributed
    to its own term, and the flat ∩ must still deny both.
    """
    envelope = ContextualPermission(
        tool_deny=frozenset({"web_search"}),
        origin=NarrowingOrigin(
            label="the envelope under test",
            cause="the topology bound it",
            lifts_when="the operator rebinds the member",
        ),
    )
    composed = compose_resolved([
        (envelope, frozenset()), (_untrusted_term(), frozenset()),
    ])[0]

    assert composed.tool_deny >= {"web_search", _FLOORED_TOOL}, "the ∩ still denies both"
    assert len(narrowing_terms(composed)) == 2, "both terms survive composition"

    envelope_origin = attribute_deny(composed, CapabilityAxis.TOOL, "web_search")
    untrusted_origin = attribute_deny(composed, CapabilityAxis.TOOL, _FLOORED_TOOL)
    assert envelope_origin is not None and untrusted_origin is not None
    assert envelope_origin.label == "the envelope under test"
    assert "_untrusted" in untrusted_origin.label
    assert envelope_origin.lifts_when != untrusted_origin.lifts_when, (
        "the two narrowings must not be reported as lifting under the same condition"
    )


def test_composing_a_composed_term_stays_one_level_deep() -> None:
    """Tier 2: re-composition does not lose or nest terms.

    ``RouterLoop._with_exclude_tools`` re-composes an already-composed contextual
    on every intra-turn re-resolve, so the flattening has to be idempotent —
    otherwise attribution would have to recurse, and a term could hide a level
    down where ``attribute_deny`` never looks.
    """
    a = ContextualPermission(
        tool_deny=frozenset({"a_tool"}),
        origin=NarrowingOrigin(label="A", cause="c", lifts_when="l"),
    )
    b = ContextualPermission(
        tool_deny=frozenset({"b_tool"}),
        origin=NarrowingOrigin(label="B", cause="c", lifts_when="l"),
    )
    c = ContextualPermission(
        tool_deny=frozenset({"c_tool"}),
        origin=NarrowingOrigin(label="C", cause="c", lifts_when="l"),
    )
    once = compose_resolved([(a, frozenset()), (b, frozenset())])[0]
    twice = compose_resolved([(once, frozenset()), (c, frozenset())])[0]

    assert [t.origin.label for t in narrowing_terms(twice) if t.origin] == ["A", "B", "C"]
    assert attribute_deny(twice, CapabilityAxis.TOOL, "a_tool").label == "A"
    assert attribute_deny(twice, CapabilityAxis.TOOL, "c_tool").label == "C"


def test_an_undenied_name_is_attributed_to_nothing() -> None:
    """Tier 2: attribution reports a narrowing only when one actually fired."""
    term = _untrusted_term()
    assert attribute_deny(term, CapabilityAxis.TOOL, "web_fetch") is None
    assert attribute_deny(None, CapabilityAxis.TOOL, _FLOORED_TOOL) is None


def test_the_delegate_floor_reports_its_own_cause_per_reason() -> None:
    """Tier 2: the five paths to the `_delegate` floor do not share a remedy.

    A missing profile file is restored, a lost capping parent cannot be — so the
    floor's origin takes its cause per call site while sharing the label and the
    lift condition. A single fixed cause string would misdescribe four of the five.
    """
    missing = delegate_floor_origin("the bound profile file is missing")
    lost_parent = delegate_floor_origin("the capping parent's identity is gone")
    assert missing.label == lost_parent.label
    assert missing.lifts_when == lost_parent.lifts_when
    assert missing.cause != lost_parent.cause
    assert "_delegate" in missing.label


@pytest.mark.asyncio
async def test_the_mcp_gate_uses_the_same_builder_as_the_tool_gate(tmp_path) -> None:
    """Tier 2: the MCP-axis deny is as legible as the TOOL-axis one.

    Same class of decision, so the same three parts must reach the caller. Driven
    through a real ``PermissionResolver.require_mcp`` — the live gate every MCP op
    calls — rather than the message builder alone, so this also witnesses that the
    gate is wired to the builder.
    """
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )
    contextual = ContextualPermission(
        mcp_deny=frozenset({"blocked-srv"}),
        origin=NarrowingOrigin(
            label="the narrowing under test",
            cause="this test applied it",
            lifts_when="the test stops applying it",
        ),
    )
    with pytest.raises(PermissionError) as exc:
        await resolver.require_mcp(
            PermissionDecl(mcp=["blocked-srv"]),
            "blocked-srv",
            _NeverAskedBus(),
            contextual=contextual,
        )
    message = str(exc.value)
    assert "the narrowing under test" in message
    assert "this test applied it" in message
    assert "the test stops applying it" in message


class _NeverAskedBus:
    """A real ``RequestBus``-shaped collaborator for a gate that must reject BEFORE
    asking. Not a stand-in for a data object: it exists to FAIL if the contextual
    layer lets the call reach the approval prompt, which is itself the assertion."""

    async def request(self, iv: object) -> object:  # pragma: no cover - must not run
        raise AssertionError(
            "the contextual layer must deny before the approval prompt is raised"
        )


# ── the string the MODEL actually receives ──────────────────────────────────
#
# The arms above measure the builder and the gates that call it. This one measures
# what a real turn puts in front of the LLM — a different question, and the one the
# issue is about: the operator's agent read the router-loop tool-result, not a
# PermissionError and not the Tool tab. Verified necessary by strip-falsify: with
# the router-loop deny site reverted to its old fixed string, every other arm in
# this file and in test_3380 stayed GREEN.


@pytest.mark.asyncio
async def test_the_tool_result_the_model_reads_carries_the_three_parts(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the model-facing tool-result names the narrowing, its cause and its
    lift conditions.

    A real ``Session`` turn: round 1 dispatches an external-content tool (taint
    lands in history via the real producer seam), round 2 calls a floored tool.
    The assertion is on the tool-result content that becomes the LLM's next
    message — not on the gate's return value, and not on the Tool tab.
    """
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="test_agent", safety=narrowing_on("iteration"),
    )
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _scripted_llm([
            _tool_call_result([
                {"name": "list_memory", "args": {"path": ""}, "id": "tc_ext"},
            ]),
            _tool_call_result([
                {"name": _FLOORED_TOOL, "args": _REMEMBER_ARGS, "id": "tc_denied"},
            ]),
            _text_result("done"),
        ]),
    )
    await session._handle_user_message("look it up then remember it", chain_id="c1")

    (denied,) = [m for m in session.history if m.tool_call_id == "tc_denied"]
    content = str(denied.content)
    assert "tool_excluded" in content, "the deny must still be the tool_excluded kind"
    assert "_untrusted" in content, "which narrowing fired"
    assert "external_source" in content, "why it is active"
    assert "compacted out" in content, "what lifts it on its own"
    assert UNTRUSTED_NARROWING_CONFIG_KEY in content, "what disables it"


# ── the completeness arm ────────────────────────────────────────────────────


def test_every_production_narrowing_term_carries_an_origin() -> None:
    """Tier 2: no production ``resolve_profile`` call omits its provenance.

    Attribution is only as complete as the set of terms that carry an origin, and
    an omission is invisible at runtime — the deny silently degrades to the generic
    "source not recorded" text instead of failing. Counted with the AST over
    ``src/`` rather than a regex, so a keyword spelled across lines still counts
    and a mention inside a docstring does not.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "reyn"
    missing: "list[str]" = []
    seen = 0
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if called != "resolve_profile":
                continue
            seen += 1
            if not any(kw.arg == "origin" for kw in node.keywords):
                missing.append(f"{path.relative_to(src)}:{node.lineno}")
    assert seen >= 5, f"expected the known resolve_profile call sites, found {seen}"
    assert missing == [], f"resolve_profile without origin=: {missing}"
