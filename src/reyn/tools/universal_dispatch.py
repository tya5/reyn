"""Catalog action membership — ONE name per operation (#3429).

An **action** is a registered ``ToolDefinition``, addressed by its FLAT
registry name (``read_file``, ``web_search``, ``mcp_call_tool``). A
**category** (``file`` / ``web`` / ``mcp`` / …) is a browsing axis over that
set — ``list_actions(category=["file"])`` — and nothing else. This module owns
the category ↔ action membership table and the "is this a real action" check
the catalog wrappers (``list_actions`` / ``search_actions`` /
``describe_action`` / ``invoke_action``) resolve against.

**#3429 — the second spelling is gone.** Until 2026-07-29 every action also had
a ``<category>__<verb>`` *qualified* spelling — ``file__read`` for
``read_file`` — and this module was the qualified→flat routing table. Two names
for one operation meant every subsystem that keys on a tool name had to decide
whether to handle both, and the ones that forgot broke silently: a census of
the 11 name-keyed subsystems found 4 that had grown explicit two-form
compensation (``capability_profile._expand_tool_forms`` among them — another,
``op_runtime.contextual_gate._OP_KIND_TOOLS``, was itself deleted in #3513 as
caller-less) and 7 that had not
(result normalisation, canonicalization declarations, permission-denied hints,
the advertisement gate, the exclusive-wrapper strip list, the
``routing_decided`` audit-event, action-usage tracking). Fixing the 7 would
have left the twelfth subsystem to flip the same coin. The cause was the second
name, so the second name is what was removed.

The property that keeps it removed is a gate, not this docstring:
``tests/tools/test_no_qualified_tool_names_3429.py`` enumerates the live registry and
this table and fails on any ``__`` in a name — so a *new* qualified name is a
red CI run rather than a silent re-opening. Deletion is a state; the gate is
the property.

**Author-time qualified names are gone too.** ``pipeline__<name>`` (taught in
the user guide) and ``tool: mcp__echo__ping`` (a pipeline DSL step) used to
resolve through a per-category fallback here. They were the same second
spelling wearing an operator-facing hat, and they are removed with it: a
pipeline step names the flat tool and passes the resource id as an ordinary
argument (``run_pipeline{name}`` / ``mcp_call_tool{tool, tool_args}``), which
is what the enumerated verbs already did.

**The enumeration invariant is unchanged (#3026).** ``_CATEGORY_ACTIONS`` is a
closed table of literal names, and it is the ONLY table any enumerator may
read. No category mints an action from operator data, so the number of tools
the LLM is sent does not depend on how many memories / corpora / MCP tools /
pipelines the operator has accumulated. #879 → #1647 is the cautionary tale:
#879 collapsed the mcp resource categories, #1647 re-added one action per MCP
tool citing two motivations that were both already closed. Before enumerating
per-resource actions again, verify the motivating gap is still open — twice now
it was not — and note that ``tests/tools/test_resource_collapse_invariant_3026.py``
will fail if you do.
"""
from __future__ import annotations

from difflib import get_close_matches
from typing import Final

from reyn.tools.universal_catalog import CATEGORIES


class UnknownActionError(ValueError):
    """Raised when a caller names an action that is not in the catalog.

    Carries the offending ``action_name``, the ``reason``, and a list of
    ``suggestions`` from nearby action names (= FP-0034 §D12
    error-with-suggestions). Callers (``universal_catalog``'s wrapper
    handlers) format this into the LLM-facing error response shape so the
    model can recover in one turn.
    """

    def __init__(
        self,
        action_name: str,
        reason: str,
        suggestions: list[str] | None = None,
    ) -> None:
        self.action_name = action_name
        self.reason = reason
        self.suggestions = suggestions or []
        msg = f"Unknown action {action_name!r}: {reason}"
        if self.suggestions:
            msg += f". Suggestions: {self.suggestions}"
        super().__init__(msg)


# ── _CATEGORY_ACTIONS — the closed category ↔ action membership table ──────
#
# category → the FLAT registry tool names that category browses. The names
# here are the ONLY names the catalog knows; each must resolve in
# ``get_default_registry()`` (pinned by
# ``tests/tools/test_universal_catalog.py``).
#
# A category is a browsing axis, not a namespace: nothing here rewrites a
# name, and there is no arg transform layer. An action's advertised schema IS
# its ToolDefinition's schema, on every route, because there is only one name
# to advertise it under.

_CATEGORY_ACTIONS: Final[dict[str, tuple[str, ...]]] = {
    # Phase 1 follow-up (2026-05-25): the old ``agent.peer`` resource category
    # collapsed into three verbs whose args carry the agent name explicitly.
    # #3429: ``list_agents`` used to accept a ``cluster`` arg through this
    # category's arg transformer — it never appeared in the advertised
    # schema, so it was capability only the qualified spelling had. Dropped
    # with the spelling; the schema (``path``) is the contract.
    # ``delegate_to_agent`` retired (proposal 0067 P6, #3978) — see
    # ``send_to_session``/``run_prompt`` for the surviving reach-another-
    # agent's-context verbs.
    # #3896 (owner ruling, option 1): ``spawn_session`` gains a catalog route
    # so exclusive-wrapper mode does not lose the capability entirely — it
    # used to be stripped by router_tools.py's §J with nothing to replace it
    # (unlike ``call_mcp_tool``/``describe_mcp_tool``, whose §J strips DO name
    # a replacement). ``run_prompt``/``send_to_session`` remain catalog-less;
    # this entry is scoped to spawn_session's own finding, not a general fix
    # for the delegation surface.
    "multi_agent": ("list_agents", "describe_agent", "spawn_session"),
    # Issue #879 — a single ``mcp`` category. 2026-05-25 install-surface
    # refactor: ``install_server`` split along the SOURCE axis into three verbs
    # (registry / public package channel / local script), and
    # ``search_server`` → ``search_registry`` so the (search, install) pair is
    # self-evident at list_actions time.
    "mcp": (
        "mcp_search_registry",
        "mcp_install_registry",
        "mcp_install_package",
        "mcp_install_local",
        "list_mcp_servers",
        "list_mcp_tools",
        "mcp_call_tool",
        "mcp_drop_server",
    ),
    # §D20 file surface. FP-0040 (#178) closed the edit gap with a
    # unique-string anchor + replace_all flag (= Claude Code style).
    "file": (
        "read_file",
        "write_file",
        "delete_file",
        "list_directory",
        "grep_files",
        "glob_files",
        "edit_file",
    ),
    "web": ("web_search", "web_fetch"),
    # #3026 added the READ half: the category was write-only (remember /
    # forget) and the only read surface was a per-entry action, i.e. one LLM
    # tool per stored memory. ``read_memory_body`` takes ``layer`` + ``slug``
    # explicitly, so an AGENT-layer memory (everything ``remember_agent``
    # writes) is reachable — the per-entry action hard-coded ``layer="shared"``.
    "memory_operation": (
        "remember_shared",
        "remember_agent",
        "forget_memory",
        "list_memory",
        "read_memory_body",
    ),
    # §D20 reyn_repo surface; FP-0038 closed the glob / grep gap.
    "reyn_repo": (
        "reyn_repo_read",
        "reyn_repo_list",
        "reyn_repo_glob",
        "reyn_repo_grep",
    ),
    # FP-0017 sandboxed_exec, tool renamed ``exec`` in #3226 Phase 3.
    # #4932 (owner ruling, 2026-08-19): the D14-ext visibility gate that
    # used to live in ``_enumerate_category`` (sandbox backend) is
    # retired — ``exec`` always enumerates; the same sandbox_backend
    # value now only composes an isolation-disclosure text suffix
    # (``is_exec_isolated``).
    "exec": ("exec",),
    # #2548 PR-C/PR-D skill directory install verbs; #2971 added ``skill_list``
    # (without it a skill outside the L1 menu had no surface naming it, so it
    # was unreachable rather than merely unadvertised); FP-0066 P0 (#3247)
    # added ``load_skill`` — "load" is the formal activation-verb class,
    # distinct from "read" = content-fetch.
    "skill_management": (
        "skill_install_local",
        "skill_install_source",
        "skill_list",
        "load_skill",
    ),
    # Proposal 0067 P7 (#3978): run_pipeline is the unified launch verb —
    # collect="attached"|"async" (was 4 separate names: run_pipeline /
    # run_pipeline_async / run_pipeline_inline / run_pipeline_inline_async,
    # retired with 0 aliases). #3026 added ``pipeline_list``: before it, the
    # only surface naming a registered pipeline was one action per pipeline.
    "pipeline": (
        "pipeline_list",
        "run_pipeline",
    ),
    # The management plane for pipelines, mirroring ``skill_management``.
    "pipeline_management": ("pipeline_install_local", "pipeline_install_source"),
    # proposal 0067 P4 (#3978): describe/list/cancel a currently-running
    # async task (run_prompt/run_pipeline launch) — reads/acts against the
    # settle-path handle substrate. No read_task_result: a settled task's
    # handle is gone (ADR-0040 D4); results arrive via task_settled, not a poll.
    "task": ("describe_task", "list_tasks", "cancel_task"),
    # proposal 0060 Phase 1 Layer A (A8): register a named presentation
    # template. Single verb — a blueprint is inline declarative data, never
    # file-backed, so there is no source/git-fetch counterpart.
    "presentation_management": ("presentation_install_local",),
    # ADR 0064 P2. #3429 renamed this trio's REGISTRY names — they were
    # ``plugin_management__install`` / ``__uninstall`` / ``__list``, the only
    # registry entries that ever carried the catalog separator inside a flat
    # name. They take R1's ``<verb>_<object>`` default rather than the
    # ``<object>_<verb>`` order of the grandfathered ``skill_*`` / ``pipeline_*``
    # families, for two reasons: there was no pre-existing flat ``plugin_*``
    # tool family for them to be internally consistent WITH (these three names
    # were themselves the anomaly R1 grandfathered), and ``plugin_install`` /
    # ``plugin_uninstall`` are already taken — by the op KINDS these tools
    # dispatch to, which share one canonical-declaration namespace with tool
    # names, so reusing them raises a conflicting-declaration error at import.
    "plugin_management": ("install_plugin", "uninstall_plugin", "list_plugins"),
    # FP-0066 P3c (#3247 firm §3): semantic search across the operator's own
    # skill/memory/repo knowledge. Runtime-gated like ``exec`` — visible only
    # when ``embedding.enabled: true``.
    "knowledge": ("search_knowledge",),
    # #3465: FP-0057 Phase 1's raw embedding primitive. Distinct from
    # ``knowledge`` (search over the operator's OWN skill/memory/repo
    # content) — ``embed`` is the USER-FACING batch text->vector primitive
    # the agent composes into a pipeline against the user's own external MCP
    # vector-DB tools (0066 §9's "two groups, two axes" split). Was
    # registered router=allow with ``router_dispatched=True`` already set,
    # but never gained a membership-table entry — the #3083-class "registered
    # + dispatchable but catalog-invisible" gap, filed as #3465 and closed
    # here rather than in #3464 (which discovered it) to avoid touching this
    # module while #3463's 444-file alias-removal arc was open.
    "embedding": ("embed",),
    # #3465: both tools already share ``ToolDefinition(category="hooks")`` —
    # the natural catalog boundary was already declared, just never wired.
    # ``emit_hook_event`` publishes an LLM-authored hook-event onto this
    # session's own HookBus (Router-only in the sense that it needs the
    # chat-router's live ``ctx.hook_bus``/``ctx.session_id`` — it fails
    # closed, not silently, in a context without one; that is a RUNTIME
    # precondition, not a reason to withhold LLM-reachability). ``hooks_add``
    # lets the agent add its own push hook (the config-hot-reload
    # self-expansion primitive). Same #3083-class wiring gap as ``embed``.
    "hooks": ("emit_hook_event", "hooks_add"),
}


# Every action name the catalog knows, as a flat set. This is the complete
# action set (#3026: no category mints names from operator data), so a
# membership test against it is total — there is no per-category dynamic
# remainder a caller has to consult session state for.
KNOWN_ACTION_NAMES: Final[frozenset[str]] = frozenset(
    name for names in _CATEGORY_ACTIONS.values() for name in names
)

# Sorted tuple form, for suggestion ranking and any caller that needs a stable
# order.
KNOWN_ACTION_NAMES_SORTED: Final[tuple[str, ...]] = tuple(sorted(KNOWN_ACTION_NAMES))

# action name → its category. Built once; each action belongs to exactly one
# category.
_ACTION_CATEGORY: Final[dict[str, str]] = {
    name: category
    for category, names in _CATEGORY_ACTIONS.items()
    for name in names
}


# ── Public API ─────────────────────────────────────────────────────────────


def is_known_action(name: str) -> bool:
    """True iff ``name`` is a catalog action (= a flat registry tool name the
    catalog browses). Total: ``KNOWN_ACTION_NAMES`` is the whole action set."""
    return name in KNOWN_ACTION_NAMES


def require_known_action(name: str) -> str:
    """Return ``name`` when it is a catalog action, else raise
    :class:`UnknownActionError` carrying §D12 suggestions.

    The one place the catalog wrappers turn "the model named something" into
    either a dispatchable name or a recoverable error. It does not rewrite the
    name — there is nothing to rewrite, which is the whole of #3429.
    """
    if name in KNOWN_ACTION_NAMES:
        return name
    raise UnknownActionError(
        name,
        "not a known action name",
        suggestions=suggest_similar_names(name),
    )


def category_of(name: str) -> "str | None":
    """The category ``name`` belongs to, or None when it is not an action."""
    return _ACTION_CATEGORY.get(name)


def action_names_for_category(category: str) -> tuple[str, ...]:
    """The action names ``category`` offers, in declaration order.

    Raises ValueError for a name outside ``CATEGORIES`` — a caller passing an
    unknown category has a bug, not an empty result. (The LLM-facing
    stale-category path is ``universal_catalog._validate_category_filter``,
    which answers with a redirect envelope instead.)
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"unknown category {category!r}; expected one of {list(CATEGORIES)}"
        )
    return _CATEGORY_ACTIONS.get(category, ())


def suggest_similar_names(
    unknown_name: str,
    candidates: list[str] | None = None,
    top_k: int = 3,
    cutoff: float = 0.4,
) -> list[str]:
    """Return up to ``top_k`` action names similar to ``unknown_name``.

    Backs the FP-0034 §D12 error-with-suggestions response. When
    ``candidates`` is None, uses the whole static action set; the catalog
    handlers pass a narrower, availability-aware pool (a category the caller
    excluded, or ``exec`` without a sandbox backend, contributes nothing).

    Uses ``difflib.get_close_matches`` — the same algorithm Python's stdlib
    uses for its own "did you mean?" suggestions, no external dependency.

    Args:
        unknown_name: the name that failed to resolve.
        candidates: names to search; defaults to the static action set.
        top_k: max suggestions (3, matching Python's internal "did you mean").
        cutoff: minimum similarity ratio [0.0, 1.0].

    Returns:
        Up to ``top_k`` names, ranked by descending similarity. Empty when
        nothing scores above ``cutoff``.
    """
    if candidates is None:
        candidates = list(KNOWN_ACTION_NAMES_SORTED)
    if not candidates:
        return []
    return get_close_matches(unknown_name, candidates, n=top_k, cutoff=cutoff)


__all__ = [
    "UnknownActionError",
    "KNOWN_ACTION_NAMES",
    "KNOWN_ACTION_NAMES_SORTED",
    "is_known_action",
    "require_known_action",
    "category_of",
    "action_names_for_category",
    "suggest_similar_names",
]
