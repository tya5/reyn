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
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.schemes.codeact import (
    _build_actions_map,
    _format_codeact_observation,
    _render_code_api,
)
from reyn.tools.schemes.retrieval import _search_sp
from reyn.tools.universal_dispatch import _OPERATION_RULES

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
    (enumerate-all) paths, with discovery-mandate / hot-list / non-interactive
    each toggled at least once."""
    return [
        dict(universal_wrappers_enabled=True, search_actions_enabled=True,
             discovery_mandate=True, has_hot_list_aliases=True, non_interactive=False),
        dict(universal_wrappers_enabled=True, search_actions_enabled=False,
             discovery_mandate=False, has_hot_list_aliases=False, non_interactive=True),
        dict(universal_wrappers_enabled=False, search_actions_enabled=True,
             discovery_mandate=True, has_hot_list_aliases=False, non_interactive=False),
        dict(universal_wrappers_enabled=False, search_actions_enabled=False,
             discovery_mandate=False, has_hot_list_aliases=True, non_interactive=True),
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

    # Retrieval scheme's own sp_fragment (both terminal states).
    for terminal in (True, False):
        out.append((f"retrieval._search_sp(terminal={terminal})", _search_sp(terminal=terminal)))

    # CodeAct scheme's code-API render.
    sample_entries = [
        {"qualified_name": "file__read", "name": "file__read",
         "description": "Read a file", "parameters": {"properties": {"path": {}}}},
        {"qualified_name": "exec__run", "name": "exec__run",
         "description": "Run a shell command", "parameters": {"properties": {"argv": {}}}},
    ]
    ident_by_qn = _build_actions_map([e["qualified_name"] for e in sample_entries])
    out.append(("codeact._render_code_api", _render_code_api(sample_entries, ident_by_qn)))

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


def _is_illustrative_mcp_tool_example(token: str) -> bool:
    """A dynamic MCP tool reference of the form ``mcp__<server>__<tool>`` (3+
    ``__``-segments under the ``mcp`` prefix) is a per-server example, not a
    statically registered verb — the SP uses ``mcp__brave__search`` to teach the
    ``mcp__<server>__<tool>`` shape. The static ``mcp`` management verbs
    (``mcp__call_tool`` etc.) are 2-segment and DO resolve via _OPERATION_RULES,
    so they are NOT excluded here."""
    return token.startswith("mcp__") and token.count("__") >= 2


def _resolve_qualified_action(token: str) -> "str | None":
    """Resolve a qualified action name from SP prose to a live registry tool
    name, or return None if it does not resolve. Mirrors the real dispatch
    lookup: ``_OPERATION_RULES[token]`` → target ToolDefinition name → registry.
    """
    rule = _OPERATION_RULES.get(token)
    if rule is None:
        return None
    target_name = rule[0]
    if target_name in get_default_registry():
        return target_name
    return None


class TestSPToolNamesResolveToLiveTools:
    """Tier 2b: every tool name referenced in assembled system-prompt prose
    resolves to a LIVE registered tool. Structurally prevents a stale
    tool-name reference (e.g. a rename like recall→semantic_search that misses
    the SP text) from silently shipping — the SP would instruct the LLM to call
    a name the OS no longer dispatches."""

    def test_every_qualified_action_name_in_sp_resolves(self):
        """Tier 2b: each backtick `<category>__<entry>` token in the assembled
        SP resolves via the real dispatch table (_OPERATION_RULES → registry),
        except the documented dynamic ``mcp__<server>__<tool>`` example shape."""
        stale: list[tuple[str, str]] = []
        for path, text in _all_assembled_system_prompts():
            for m in _QUALIFIED_TOKEN_RE.finditer(text):
                token = m.group(1)
                if _is_illustrative_mcp_tool_example(token):
                    continue
                if _resolve_qualified_action(token) is None:
                    stale.append((path, token))
        assert stale == [], (
            "SP prose references qualified action name(s) that do NOT resolve "
            f"to a live registered tool (stale rename?): {sorted(set(stale))!r}"
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

    def test_strip_falsify_stale_qualified_name_is_detected(self):
        """Tier 2b: a qualified action name whose dispatch target is NOT
        registered must be flagged as stale (falsification) — proves the
        resolver actually checks liveness against the registry."""
        # `file__edit` → _OPERATION_RULES → "edit_file". Simulate the registry
        # tool being renamed/removed by resolving a bogus target that cannot be
        # in the registry — the resolver must return None (= stale).
        assert _resolve_qualified_action("file__edit") is not None, (
            "precondition: file__edit should resolve to a live tool"
        )
        # A token not in _OPERATION_RULES resolves to None (the stale signal).
        assert _resolve_qualified_action("file__edit_NONEXISTENT_XYZ") is None, (
            "strip-falsify: an unresolvable qualified name was not flagged — "
            "liveness gate is not live"
        )


# The bare (non-qualified, no "__") tool-registry-internal names the SP prose
# is DELIBERATELY allowed to reference by bare backtick — the universal-
# wrapper vocabulary itself. Any OTHER bare backtick token that happens to
# match a live registry tool's internal name (e.g. a stray `read_file`
# instead of the qualified `file__read` the dispatch table expects) is a
# smell: either a copy-paste of the internal implementation name into SP
# prose (which the LLM cannot call — dispatch is keyed by qualified name,
# not this internal name), or a new wrapper-style verb that needs this
# watch-list extended deliberately (closes the curated-subset trap: adding a
# 5th SP-referenced bare name must be a conscious edit here, not silent).
_CURATED_BARE_NAME_WATCHLIST = frozenset(
    {"list_actions", "search_actions", "invoke_action", "describe_action"}
)
_BARE_BACKTICK_TOKEN_RE = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_.]*)`")


class TestBareBacktickTokensAreWatchlisted:
    """Tier 2b: bare-name meta-guard (SP Phase 1 prompt-package co-vet). Any
    backtick-bare token in the assembled SP that matches a LIVE registered
    tool's internal name, but is NOT one of the 4 curated universal-wrapper
    verbs, fails — forcing the watch-list to be extended deliberately as the
    SP's bare-name vocabulary evolves, instead of silently growing unchecked."""

    def test_bare_backtick_tokens_matching_registered_tools_are_watchlisted(self):
        """Tier 2b: every bare backtick token in the assembled SP that
        matches a live registered tool name is on the curated watch-list."""
        registry = get_default_registry()
        registry_names = {tool.name for tool in registry}
        unexpected: list[tuple[str, str]] = []
        for path, text in _all_assembled_system_prompts():
            for m in _BARE_BACKTICK_TOKEN_RE.finditer(text):
                token = m.group(1)
                if token in registry_names and token not in _CURATED_BARE_NAME_WATCHLIST:
                    unexpected.append((path, token))
        assert unexpected == [], (
            "SP prose contains bare backtick token(s) matching a live "
            "registered tool name that are NOT on the curated watch-list "
            "(extend _CURATED_BARE_NAME_WATCHLIST deliberately if this is "
            f"intentional new SP vocabulary): {sorted(set(unexpected))!r}"
        )

    def test_strip_falsify_unwatchlisted_bare_name_is_detected(self):
        """Tier 2b: a bare backtick token matching a live registry tool name
        that is NOT on the watch-list must be flagged (falsification) —
        proves the scan is live, not vacuously passing."""
        registry = get_default_registry()
        registry_names = {tool.name for tool in registry}
        # `read_file` is a real internal registry name (per get_default_registry())
        # but is NOT one of the 4 curated wrapper verbs — a bare reference to
        # it in SP prose must be flagged.
        assert "read_file" in registry_names, (
            "precondition: read_file should be a live registered tool name"
        )
        assert "read_file" not in _CURATED_BARE_NAME_WATCHLIST, (
            "precondition: read_file must not already be on the watch-list"
        )
        poisoned_text = "Some SP prose accidentally names `read_file` bare."
        hits = [
            m.group(1) for m in _BARE_BACKTICK_TOKEN_RE.finditer(poisoned_text)
            if m.group(1) in registry_names and m.group(1) not in _CURATED_BARE_NAME_WATCHLIST
        ]
        assert hits == ["read_file"], (
            "strip-falsify: an unwatchlisted bare registry-tool-name token "
            "was not detected — the meta-guard is not live"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
