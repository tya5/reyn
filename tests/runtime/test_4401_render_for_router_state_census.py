"""Tier 2: #4401 ① condition (lead-coder, architect R1's own "tomorrow's
optimization quietly falsifies today's invariant" shape, applied a second
time) — a completeness census over every ``ToolDefinition.render_for_router``
call site that passes ``state=``.

Why this exists: #4401's own empirical trace (issue #4401, the R2 comment)
found `RouterHostAdapter.get_mcp_servers()` has 8 call sites, 5 of which
reach the model — but only a call site that ALSO threads a ``RouterCallerState``
into ``render_for_router(state=...)`` can actually trigger `tools/mcp.py`'s
``_enrich_router_schema`` (the schema-enricher that injects the
`server`/`mcp_tool_name` enums an LLM sees). Today, EVERY such call site sits
inside ``router_tools.py``'s ``build_tools()`` — which #4401's own trace
confirmed is reachable ONLY via ``RouterLoop.present``/``base_tools()``,
themselves reachable ONLY from inside ``RouterLoop.run()``. That is what
makes "await once at the top of `run()`" (#4401 ①) sufficient: every
enum-injecting call site is downstream of it.

That sufficiency is a fact about TODAY'S code, not a structural guarantee —
a future PR could add a NEW ``render_for_router(state=...)`` call site
OUTSIDE `build_tools()` (e.g. one of #4401's own ④/⑤ non-`run()`-gated
paths growing a schema-enricher call it doesn't have today) and the #4401 ①
await would silently stop covering it, exactly the failure architect named
for R1 (the OTHER #4401 invariant, in `mcp_cache_file.py`, that a "later
optimization" could silently break). This test is the mechanical ratchet
that turns that silent break into a red test instead.

★ Architect's own R3 addition: the ratchet is a DETECTOR, not a repair
obligation — "add the new site to the declared set" is exactly the move
that makes a red test green WITHOUT fixing anything (a correctly-looking
operation that leaves the hole open). The failure message below therefore
does not say "add it here" — it says "prove it's downstream of run()'s
await FIRST", because that proof is the actual content of what this test
protects, not the set membership itself.

★ lead-coder BLOCKING (PR #5763): the first cut of this test keyed each
site on ``(file, lineno)``. A line number is NOT a closed target — any
unrelated edit landing ABOVE line 802 in `router_tools.py` shifts every
declared entry at once, so all 9 sites go "undeclared" together though not
one new call site was added. A reviewer who sees that shape once learns
"just fix the numbers, no proof needed" — the exact operation the R3
failure message above forbids — and the SAME reflex then silences a real
new site the next time one appears. The key is now
``(file, enclosing_function_name, receiver_variable_name)`` — e.g.
``("...router_tools.py", "build_tools", "_call_mcp_tool_def")`` — stable
under any line-number churn, and still fully AST-derived (no hand-typed
string beyond what the source itself names the receiver).

AST-based (not a grep), same closed-target reasoning
``test_3595_s4_slash_handler_seam.py``'s own ``_ResidueCollector`` gives —
``state=`` is a keyword argument, syntactically closed, so the walk finds
every spelling a plain string search over source text could miss."""
from __future__ import annotations

import ast

from tests._support.paths import REPO_ROOT

_SRC_ROOT = REPO_ROOT / "src"

class _RenderForRouterStateCallCollector(ast.NodeVisitor):
    """Walks one module, tracking the innermost enclosing function/method
    name, and records every ``<name>.render_for_router(..., state=..., ...)``
    call as ``(enclosing_function, receiver_name)``. Only a plain-Name
    receiver is recorded (every known call site today has this shape,
    ``<tool>_def.render_for_router(...)``) — a call through some other
    expression shape (e.g. a subscript or a call result) would need this
    walk extended, not silently ignored, so such a shape raises rather
    than being dropped."""

    def __init__(self) -> None:
        self._func_stack: "list[str]" = []
        self.found: "set[tuple[str, str]]" = set()

    def _enclosing(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "render_for_router":
            if any(kw.arg == "state" for kw in node.keywords):
                if not isinstance(func.value, ast.Name):
                    raise AssertionError(
                        f"render_for_router(state=...) called through a non-Name "
                        f"receiver ({ast.dump(func.value)!r}) at line {node.lineno} — "
                        "this walk's (function, receiver-name) key assumes every "
                        "call site is `<name>.render_for_router(...)`; extend the "
                        "key shape here rather than silently dropping this call."
                    )
                self.found.add((self._enclosing(), func.value.id))
        self.generic_visit(node)


def _find_state_passing_render_for_router_calls() -> "set[tuple[str, str, str]]":
    """Every ``<name>.render_for_router(..., state=..., ...)`` call in
    ``src/`` — ``(module-relative-path, enclosing_function, receiver_name)``
    triples. The population this test reasons about is derived
    STRUCTURALLY (an AST walk over every call site), never a hand-
    maintained enumeration — only the DECLARED (expected) side below is a
    maintained list; the FOUND side always reflects the actual code."""
    found: "set[tuple[str, str, str]]" = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _RenderForRouterStateCallCollector()
        collector.visit(tree)
        rel = str(path.relative_to(REPO_ROOT))
        for func_name, receiver in collector.found:
            found.add((rel, func_name, receiver))
    return found


#: Every call site KNOWN to pass ``state=`` today — all 9 are the MCP tool
#: definitions (D3/D4 call_mcp_tool/describe_mcp_tool + the 7 resource/
#: prompt verbs) inside ``router_tools.py``'s ``build_tools()``. That
#: function is called only from `RouterLoop.present`/`base_tools()`
#: (#4401's own trace, issue #4401 R2 comment), which are reachable only
#: from inside `RouterLoop.run()` — the boundary #4401 ①'s await sits at
#: the top of.
_DECLARED_SITES: "set[tuple[str, str, str]]" = {
    ("src/reyn/runtime/router_tools.py", "build_tools", "_call_mcp_tool_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_describe_mcp_tool_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_list_mcp_resources_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_list_mcp_resource_templates_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_read_mcp_resource_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_subscribe_mcp_resource_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_unsubscribe_mcp_resource_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_list_mcp_prompts_def"),
    ("src/reyn/runtime/router_tools.py", "build_tools", "_get_mcp_prompt_def"),
}


def test_extraction_is_not_vacuous() -> None:
    """Tier 2: the walk actually finds calls — an extractor that silently
    returns nothing would make every assertion below pass vacuously."""
    found = _find_state_passing_render_for_router_calls()
    assert found, (
        "the walk found NO render_for_router(state=...) call anywhere in "
        "src/ — either the extractor is broken, or every #4401-relevant "
        "call site is genuinely gone (verify before believing it; a broken "
        "walk looks identical to that)."
    )


def test_every_state_passing_call_site_is_declared_and_known() -> None:
    """Tier 2: the ratchet — a NEW ``render_for_router(state=...)`` call
    site outside the declared set is exactly the shape #4401 ①'s await
    stops covering (a partial mcp catalog could then reach an LLM-facing
    enum without ever passing through `RouterLoop.run()`'s own await).
    Keyed on (file, enclosing function, receiver variable name) — NOT
    line number (see this module's own docstring, lead-coder's BLOCKING on
    PR #5763, for why a line-number key makes every unrelated edit above
    line 802 a false "undeclared" alarm for all 9 sites at once)."""
    found = _find_state_passing_render_for_router_calls()
    undeclared = found - _DECLARED_SITES
    assert not undeclared, (
        f"undeclared render_for_router(state=...) call site(s): {sorted(undeclared)!r}. "
        "★ Before touching _DECLARED_SITES: this new call site can bypass "
        "#4401 ①'s single await-at-run()-top guard — PROVE it is reached "
        "only from inside RouterLoop.run() (trace every caller, the way "
        "issue #4401's own R2 comment did for the existing 9), the same "
        "way #4401 ① protects the declared sites today. Adding the line "
        "here WITHOUT that proof only silences this test — it does not "
        "close the hole. If the new site is NOT downstream of run()'s "
        "await, #4401 ①'s design itself needs revisiting (a new "
        "consumption point, or a different seam) before this test may "
        "go green again."
    )


def test_no_declared_site_is_stale() -> None:
    """Tier 2: the standing positive control — every declared site is still
    FOUND. A site that's genuinely gone (code moved/removed/renamed) needs
    its entry deleted (or updated) here, not left stale; more importantly,
    a REGRESSED walk (a lost AST shape) would make declared sites vanish
    from the found set, which would otherwise read as "the census got
    smaller" — the most flattering possible way for this gate to break
    silently."""
    found = _find_state_passing_render_for_router_calls()
    stale = _DECLARED_SITES - found
    assert not stale, (
        f"_DECLARED_SITES declares site(s) the walk no longer finds: {sorted(stale)!r}. "
        "If genuinely gone (or renamed), update the entry and say so in "
        "the PR. If not, the walk regressed and the 'no undeclared site' "
        "result above is understated."
    )
