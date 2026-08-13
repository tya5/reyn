"""Tier 2b: every string reaching the LLM (tool schemas + assembled system
prompts) is CJK-free.

Owner rule: LLM-facing text (tool ``description=``, JSON-schema per-parameter
``description``, system-prompt literals) must be English. Non-LLM-facing text
(code comments/docstrings, user-visible UI strings such as
``UserIntervention.prompt`` or the localized peer-dispatch outbox messages)
is explicitly out of scope and may legitimately contain Japanese — this test
therefore scans *rendered/assembled* LLM payloads, never source files, so it
cannot false-positive on that legitimate non-LLM-facing Japanese.

This is a PERMANENT structural gate (not `tests/scaffold/`): it prevents any
future regression where Japanese (or other non-English/CJK text) leaks into
a tool description, a parameter description, or an assembled system prompt.
"""
from __future__ import annotations

import re

import pytest

from reyn.prompt.dogfood import DOGFOOD_INTERPRETATION_SYSTEM_PROMPT, dogfood_judge_system_prompt
from reyn.prompt.loop_control import (
    EMPTY_STOP_RETRY_DIRECTIVE,
    G12_SIGNAL_ERROR_TEXT,
    tool_call_cap_notice,
)
from reyn.runtime.reasoning_continuity import render_reasoning_section
from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools import get_default_registry
from reyn.tools.encoders import build_actions_map, render_code_api
from reyn.tools.schemes._content_fence_cell import _format_codeact_observation
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.schemes.retrieval import _search_sp

# Hiragana, Katakana, and CJK Unified Ideographs (incl. extension A) — the
# same three ranges the owner named for the audit.
_CJK_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿]"
)

# A backtick-quoted qualified action name (`<category>__<entry>`, possibly with
# more `__` segments) as it appears in the assembled SP prose.
_QUALIFIED_TOKEN_RE = re.compile(r"`([a-z_]+(?:__[a-z_]+)+)`")
# The universal-wrapper vocabulary the SP prose references by bare tool name.
_WRAPPER_TOKEN_RE = re.compile(
    r"`(list_actions|search_actions|invoke_action|describe_action)`"
)


def _walk_strings(obj, path="root"):
    """Yield (path, string) for every string leaf in a nested dict/list/tuple."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _all_tool_render_strings() -> list[tuple[str, str]]:
    """Every string in every registered tool's router-rendered schema
    (name / description / full JSON-schema parameters, including nested
    per-parameter ``description`` fields)."""
    registry = get_default_registry()
    out: list[tuple[str, str]] = []
    for tool in registry:
        rendered = tool.render_for_router()
        out.extend(_walk_strings(rendered, f"tool:{tool.name}"))
    return out


def _representative_sp_flag_combos() -> list[dict]:
    """A representative (not exhaustive-huge) set of the scheme-layer flag
    combinations that feed ``build_universal_tool_use_slots`` — covers both
    wrapper-on (universal-category / retrieval) and wrapper-off
    (enumerate-all) paths, with discovery-mandate / non-interactive each
    toggled at least once."""
    return [
        dict(universal_wrappers_enabled=True, search_actions_enabled=True,
             discovery_mandate=True, non_interactive=False),
        dict(universal_wrappers_enabled=True, search_actions_enabled=False,
             discovery_mandate=False, non_interactive=True),
        dict(universal_wrappers_enabled=False, search_actions_enabled=True,
             discovery_mandate=True, non_interactive=False),
        dict(universal_wrappers_enabled=False, search_actions_enabled=False,
             discovery_mandate=False, non_interactive=True),
    ]


def _all_assembled_system_prompts() -> list[tuple[str, str]]:
    """Every assembled system prompt across the representative scheme/flag
    fixture set (the OS frame + whichever scheme slot-map is injected)."""
    out: list[tuple[str, str]] = []
    empty_memory = {"status": "not_found", "content": ""}
    for combo in _representative_sp_flag_combos():
        slots = build_universal_tool_use_slots(**combo, available_skills=None)
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="general assistant",
            available_agents=[{"name": "peer1", "role": "peer role"}],
            memory_index=empty_memory,
            tool_use_sp=slots,
            non_interactive=combo["non_interactive"],
            cwd="/tmp/project",
        )
        out.append((f"sp:{combo}", prompt))

    # Retrieval scheme's own search guidance (both terminal states).
    for terminal in (True, False):
        out.append((f"retrieval._search_sp(terminal={terminal})", _search_sp(terminal=terminal)))

    # CodeAct scheme's code-API render.
    sample_entries = [
        {"action_name": "read_file", "name": "read_file",
         "description": "Read a file", "parameters": {"properties": {"path": {}}}},
        {"action_name": "exec", "name": "exec",
         "description": "Run a shell command", "parameters": {"properties": {"argv": {}}}},
    ]
    ident_by_qn = build_actions_map([e["action_name"] for e in sample_entries])
    out.append(("encoders.render_code_api", render_code_api(sample_entries, ident_by_qn)))

    return out


class TestToolSchemasAreCJKFree:
    def test_every_registered_tool_render_is_cjk_free(self):
        """Tier 2b: no registered tool's rendered name/description/parameter-
        description contains CJK — this is the exact shape sent to the LLM
        via ``render_for_router()``."""
        hits = [
            (path, s) for path, s in _all_tool_render_strings()
            if _CJK_RE.search(s)
        ]
        assert hits == [], (
            "CJK found in LLM-facing tool schema text (must be English): "
            f"{hits!r}"
        )

    def test_strip_falsify_tool_description_cjk_is_detected(self):
        """Tier 2b: injecting one CJK character into an LLM-facing tool
        description must make the scan detect it (falsification) — proves
        the regex/walk actually inspects reachable text, not a vacuous pass."""
        poisoned = {
            "type": "function",
            "function": {
                "name": "fake_tool",
                "description": "A normal English description with a stray 日 char.",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string", "description": "fine"}},
                },
            },
        }
        hits = [
            (path, s) for path, s in _walk_strings(poisoned, "tool:fake_tool")
            if _CJK_RE.search(s)
        ]
        assert hits, "strip-falsify: injected CJK char was not detected — gate is not live"


def _all_request_stream_nudges() -> list[tuple[str, str]]:
    """Every §I-M mid-request-stream nudge string (SP Phase 3, loop-control +
    dogfood + CodeAct observation labels) — these reach an LLM request as a
    synthetic message / embedded tool-result text, but are NOT part of the
    assembled system prompt, so ``_all_assembled_system_prompts`` above never
    exercises them. Extends the CJK/liveness corpus so a future Japanese or
    stale-name regression in one of these is caught the same way."""
    out: list[tuple[str, str]] = [
        ("loop_control.EMPTY_STOP_RETRY_DIRECTIVE", EMPTY_STOP_RETRY_DIRECTIVE),
        ("loop_control.G12_SIGNAL_ERROR_TEXT", G12_SIGNAL_ERROR_TEXT),
        ("loop_control.tool_call_cap_notice", tool_call_cap_notice(attempted=7, kept=3)["content"]),
        ("reasoning_continuity.render_reasoning_section", render_reasoning_section(["a prior entry"])),
        ("dogfood.DOGFOOD_INTERPRETATION_SYSTEM_PROMPT", DOGFOOD_INTERPRETATION_SYSTEM_PROMPT),
        ("dogfood.dogfood_judge_system_prompt", dogfood_judge_system_prompt("- on-topic\n- polite")),
        ("codeact._format_codeact_observation[result]", _format_codeact_observation(
            {"ok": True, "result": {"x": 1}, "stdout": "", "stderr": ""}
        )),
        ("codeact._format_codeact_observation[stdout]", _format_codeact_observation(
            {"ok": True, "result": None, "stdout": "printed text", "stderr": ""}
        )),
        ("codeact._format_codeact_observation[stderr]", _format_codeact_observation(
            {"ok": True, "result": {"x": 1}, "stdout": "", "stderr": "warning text"}
        )),
    ]
    return out


class TestRequestStreamNudgesAreCJKFree:
    """Tier 2b: SP Phase 3 — the mid-request-stream nudges (§I-M) are CJK-free.
    These inject as synthetic messages / embedded tool-result text, not via
    ``build_system_prompt``, so they need their own corpus (the assembled-
    system-prompt corpus above never renders them)."""

    def test_every_request_stream_nudge_is_cjk_free(self):
        """Tier 2b: no §I-M nudge contains CJK."""
        hits = [
            (path, s) for path, s in _all_request_stream_nudges()
            if _CJK_RE.search(s)
        ]
        assert hits == [], (
            "CJK found in a request-stream nudge (must be English): "
            f"{hits!r}"
        )

    def test_strip_falsify_request_stream_nudge_cjk_is_detected(self):
        """Tier 2b: a nudge string containing one CJK character must be
        flagged by the same scan (falsification) — proves the corpus/regex
        combination is live, not vacuously passing."""
        poisoned = "A normal nudge string with a stray 通 character."
        assert _CJK_RE.search(poisoned), (
            "strip-falsify: injected CJK char in a nudge string was not "
            "detected — gate is not live"
        )


class TestAssembledSystemPromptsAreCJKFree:
    def test_every_assembled_system_prompt_variant_is_cjk_free(self):
        """Tier 2b: across a representative sweep of scheme/flag combinations
        (universal wrappers on/off, search on/off, discovery mandate on/off,
        hot-list on/off, non-interactive on/off) plus the retrieval and
        codeact scheme-owned SP fragments, the assembled system-prompt text
        the LLM actually receives contains no CJK."""
        hits = [
            (path, s[max(0, m.start() - 30):m.end() + 30])
            for path, s in _all_assembled_system_prompts()
            for m in [_CJK_RE.search(s)]
            if m
        ]
        assert hits == [], (
            "CJK found in an assembled system prompt (must be English): "
            f"{hits!r}"
        )

    def test_strip_falsify_system_prompt_cjk_is_detected(self):
        """Tier 2b: a system-prompt string containing one CJK character must
        be flagged by the same scan used above (falsification)."""
        poisoned_prompt = "Some assembled system prompt text with a stray 探 character."
        assert _CJK_RE.search(poisoned_prompt), (
            "strip-falsify: injected CJK char in a prompt string was not "
            "detected — gate is not live"
        )


class TestSPToolNamesResolveToLiveTools:
    """Tier 2b: every tool name referenced in assembled system-prompt prose
    resolves to a LIVE registered tool. Structurally prevents a stale
    tool-name reference (e.g. a rename like recall→semantic_search that misses
    the SP text) from silently shipping — the SP would instruct the LLM to call
    a name the OS no longer dispatches."""

    def test_sp_prose_teaches_no_double_underscore_tool_name(self):
        """Tier 2b: #3429 — no backtick token in the assembled SP names a tool
        with a ``__`` in it.

        The catalog's ``<category>__<verb>`` spelling was abolished, so a
        ``__``-bearing name in SP prose is either a resurrected alias or a
        stale line teaching one. The single exception is the MCP tool
        IDENTIFIER (``<server>__<tool>``), which is an ARGUMENT VALUE for
        ``mcp_call_tool`` in a namespace reyn does not own — allowed only when
        the surrounding line marks it as such."""
        offenders: list[tuple[str, str]] = []
        for path, text in _all_assembled_system_prompts():
            for m in _QUALIFIED_TOKEN_RE.finditer(text):
                token = m.group(1)
                if token.startswith("<server>__") or token == "<server>__<tool>":
                    continue
                offenders.append((path, token))
        assert offenders == [], (
            "SP prose contains a `<a>__<b>` tool name — the qualified spelling "
            f"was abolished in #3429: {sorted(set(offenders))!r}"
        )

    def test_every_wrapper_tool_name_in_sp_is_registered(self):
        """Tier 2b: the universal-wrapper vocabulary the SP names by bare tool
        name (list_actions / search_actions / invoke_action / describe_action)
        each resolves to a live registered tool."""
        registry = get_default_registry()
        stale: list[tuple[str, str]] = []
        for path, text in _all_assembled_system_prompts():
            for m in _WRAPPER_TOKEN_RE.finditer(text):
                token = m.group(1)
                if token not in registry:
                    stale.append((path, token))
        assert stale == [], (
            "SP prose names wrapper tool(s) not in the registry: "
            f"{sorted(set(stale))!r}"
        )

    def test_strip_falsify_double_underscore_scan_is_live(self):
        """Tier 2b: the scan above fires on a planted ``__`` token — proves it
        is not vacuously passing because the regex never matches anything."""
        planted = "call `file__read` to read a file"
        found = [m.group(1) for m in _QUALIFIED_TOKEN_RE.finditer(planted)]
        assert found == ["file__read"], (
            "strip-falsify: the qualified-token regex did not match a planted "
            f"`file__read`, so the SP scan proves nothing: {found!r}"
        )


# Backtick tokens in SP prose that are deliberately NOT tool names — argument
# keys and schema field names the prose has to name. Everything else that looks
# like a tool name (snake_case with an underscore) must BE one, live in the
# registry; see the class below.
_NON_TOOL_BACKTICK_TOKENS = frozenset({
    # ``read_memory_body``'s / ``mcp_call_tool``'s argument keys, named inline.
    "kind", "layer", "slug", "source",
    # #3465: ``emit_hook_event``'s argument key, named inline in the
    # ``hooks`` category bullet.
    "event_name",
})
_BARE_BACKTICK_TOKEN_RE = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_.]*)`")


class TestSPToolTokensAreLiveRegistryNames:
    """Tier 2b: every backtick token in the assembled SP that has a tool name's
    SHAPE resolves to a live registered tool.

    #3429 inverted this guard's predecessor. That one treated a backticked
    ``read_file`` as a SMELL — "the LLM cannot call it, dispatch is keyed by the
    qualified name" — and kept a curated watch-list of the few bare names the SP
    was allowed to say. The qualified spelling is gone: the flat name IS the
    dispatch name, so every tool the SP names it names correctly, and a
    watch-list of exceptions has nothing left to except.

    What survives is the failure the class was created for: a rename that
    updates the registry and misses the SP prose, leaving the model instructed
    to call a name the OS no longer dispatches. That is now checkable directly
    and totally — no curated subset — because a token with a tool name's shape
    is a tool name."""

    def test_every_tool_shaped_backtick_token_in_sp_is_registered(self):
        """Tier 2b: each backtick snake_case token in the assembled SP is a live
        registered tool name (or a declared non-tool argument key)."""
        registry_names = {tool.name for tool in get_default_registry()}
        stale: list[tuple[str, str]] = []
        for path, text in _all_assembled_system_prompts():
            for m in _BARE_BACKTICK_TOKEN_RE.finditer(text):
                token = m.group(1)
                if "_" not in token or token in _NON_TOOL_BACKTICK_TOKENS:
                    continue
                if token not in registry_names:
                    stale.append((path, token))
        assert stale == [], (
            "SP prose names tool-shaped token(s) that are NOT live registered "
            "tools (stale rename, or a new non-tool term that belongs in "
            f"_NON_TOOL_BACKTICK_TOKENS): {sorted(set(stale))!r}"
        )

    def test_strip_falsify_stale_tool_token_is_detected(self):
        """Tier 2b: a planted tool-shaped token that is NOT registered must be
        flagged — proves the scan is live, not vacuously passing."""
        registry_names = {tool.name for tool in get_default_registry()}
        poisoned = "Some SP prose still says `read_file_RENAMED_AWAY`."
        hits = [
            m.group(1) for m in _BARE_BACKTICK_TOKEN_RE.finditer(poisoned)
            if "_" in m.group(1)
            and m.group(1) not in _NON_TOOL_BACKTICK_TOKENS
            and m.group(1) not in registry_names
        ]
        assert hits == ["read_file_RENAMED_AWAY"], (
            "strip-falsify: a stale tool-shaped token was not detected — the "
            f"liveness guard is not live: {hits!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
