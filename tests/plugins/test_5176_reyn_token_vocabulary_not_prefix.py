"""Tier 1: #5176 (architect TESTS-READ blocking finding on #5166,
issuecomment-5384269296) — ``find_unresolved_reyn_tokens``'s fail-close
membership test is reyn's own KNOWN token vocabulary
(``_KNOWN_REYN_TOKEN_NAMES``), never a bare ``${REYN_*}``/``${CLAUDE_*}``
PREFIX match.

Root cause: a REYN_/CLAUDE_ prefix is not the same thing as reyn's own
token vocabulary — real environment variables with this prefix exist
that are NOT reyn tokens (``REYN_MCP_REGISTRY_URLS``,
``security/ssrf_guard.py``'s allowlist vars, ``CLAUDE_CODE_*``). #5166's
consolidation of every hooks.yaml-shaped layer onto ONE fail-close check
widened the blast radius of the pre-existing prefix-match bug from "the
1-2 layers #5140/#5164 originally touched" to "every hooks.yaml layer any
operator config can write" — a real, previously-working
``${REYN_MCP_REGISTRY_URLS}`` reference would have silently stopped
working (the whole layer refused) without this fix.

Real ``find_unresolved_reyn_tokens`` — no mocks; a pure function."""
from __future__ import annotations

import ast
import inspect

from reyn.config import loader
from reyn.environment import container_backend
from reyn.hooks import shell_runner
from reyn.plugins.tokens import (
    AGENT_SCOPED_TOKEN_NAMES,
    CONTEXT_TOKEN_NAMES,
    REYN_TOKEN_NAMES,
    PluginTokenContext,
    find_unresolved_reyn_tokens,
    resolve_token_map,
)


def test_a_real_non_reyn_env_var_sharing_the_reyn_prefix_is_not_flagged() -> None:
    """Tier 1: acceptance⑤ — ``${REYN_MCP_REGISTRY_URLS}`` (a real env var
    ``config/loader.py`` itself propagates ``mcp.registries`` into, meant
    for ``expand_env``/``os.environ``, never this module) must not be
    treated as an unresolved reyn token."""
    assert find_unresolved_reyn_tokens({"a": "${REYN_MCP_REGISTRY_URLS}"}) == []


def test_claude_code_harness_env_is_not_flagged() -> None:
    """Tier 1: ``CLAUDE_CODE_*`` (the Claude Code harness's own env,
    unrelated to this module's ``CLAUDE_PLUGIN_ROOT``/``CLAUDE_SKILL_DIR``/
    ``CLAUDE_PROJECT_DIR`` alias spellings) must not be flagged either."""
    assert find_unresolved_reyn_tokens({"a": "${CLAUDE_CODE_SOME_VAR}"}) == []


def test_a_reyn_shaped_typo_is_not_flagged() -> None:
    """Tier 1: acceptance⑥ — a genuine reyn-token TYPO
    (``${REYN_AGNT_NAME}``, missing the E) must pass through untouched,
    not fail-close — "claims to be a reyn token by prefix, misspelled" is
    not reliably distinguishable from "a real env var that happens to
    share reyn's prefix" (architect's own recommendation,
    issuecomment-5384269296; lead-coder concurred)."""
    assert find_unresolved_reyn_tokens({"a": "${REYN_AGNT_NAME}"}) == []


def test_token_vocabularies_are_disjoint_and_complete(tmp_path) -> None:
    """Tier 1: context and agent-scoped token sets form the full vocabulary."""
    context = PluginTokenContext(tmp_path, tmp_path)
    assert not CONTEXT_TOKEN_NAMES & AGENT_SCOPED_TOKEN_NAMES
    assert CONTEXT_TOKEN_NAMES | AGENT_SCOPED_TOKEN_NAMES <= REYN_TOKEN_NAMES
    assert set(resolve_token_map(context)) == CONTEXT_TOKEN_NAMES - {"REYN_SKILL_DIR"}
    trees = [ast.parse(source) for source in (
        inspect.getsource(loader),
        inspect.getsource(container_backend),
        inspect.getsource(shell_runner),
    )]
    supplied_names = {
        node.value
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in AGENT_SCOPED_TOKEN_NAMES
    }
    assert supplied_names == AGENT_SCOPED_TOKEN_NAMES
    assert agent_sources


def test_an_added_token_name_is_checked_automatically(monkeypatch) -> None:
    """Tier 1: a token added to the vocabulary is part of fail-close checks."""
    monkeypatch.setattr(
        "reyn.plugins.tokens.REYN_TOKEN_NAMES",
        frozenset({"REYN_NEW_TOKEN"}),
    )
    assert find_unresolved_reyn_tokens({"a": "${REYN_NEW_TOKEN}"}) == [
        "${REYN_NEW_TOKEN}"
    ]


def test_every_real_reyn_token_name_is_still_flagged_when_unresolved() -> None:
    """Tier 1: the fix must not overcorrect into never flagging anything —
    every name this module ACTUALLY supplies a value for
    (``REYN_PLUGIN_ROOT``/``REYN_PROJECT_DIR``/``REYN_SKILL_DIR`` via
    ``PluginTokenContext``, ``REYN_AGENT_NAME`` via the #5140 hooks.yaml
    map, and the ``CLAUDE_*`` alias spellings) is still caught unresolved."""
    real_names = [
        "REYN_PLUGIN_ROOT",
        "REYN_PROJECT_DIR",
        "REYN_SKILL_DIR",
        "REYN_AGENT_NAME",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_SKILL_DIR",
        "CLAUDE_PROJECT_DIR",
    ]
    for name in real_names:
        found = find_unresolved_reyn_tokens({"a": f"${{{name}}}"})
        assert found == [f"${{{name}}}"], (
            f"{name}: a real reyn token name left unresolved must still be "
            f"caught — got {found!r}"
        )
