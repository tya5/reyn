"""Tier 1: a tool's rendered schema never depends on what ran earlier in the process.

#3383. ``ToolDefinition.render_for_router`` used to return ``dict(self.parameters)``
— a SHALLOW copy, leaving every nested sub-schema (``properties``, each ``oneOf``
variant) aliased to the module-level constant the definition was built from.
litellm's provider transforms rewrite the ``tools[]`` payload they are handed IN
PLACE (``_build_vertex_schema`` is documented as returning "the input parameters,
modified in place"; ``add_object_type`` injects ``type: object`` into any node
without one, ``_remove_additional_properties`` deletes
``additionalProperties: false``). So ONE Gemini/Vertex call permanently rewrote
reyn's canonical schema, and every later render — for ANY provider — served the
corrupted shape: ``install_plugin``'s ``source`` variants lost
``additionalProperties`` and grew ``"type": "object"`` inside their ``kind``
const, and ``presentation_install_local``'s untyped ``blueprint`` became
``type: object``. Observed as a schedule-dependent baseline-fixture failure,
but the defect is in the product: the schema the LLM receives was a function of
process history.

These tests assert the invariant directly, at each of the THREE seams where a
canonical schema leaves its ToolDefinition: the router payload
(``render_for_router``), the ``build_tools`` payload (``ToolSpec``), and the
``describe_action`` tool result. Each drives the defect by CONSTRUCTING the
precondition — mutating the handed-out payload in place, exactly as a
provider transform does — rather than depending on a provider being reachable
or on a particular test order.

Note: a fourth seam, the hot-list direct aliases
(``router_loop._operation_alias_metadata`` → ``_build_hot_list_aliases``),
used to be covered here too — it was removed along with the hot-list feature
(#4552 PR-1), and its dedicated arm below went with it.

★ Coverage has TWO axes, and enumerating the registry only closes one of them.
The per-seam arms are total in the *tool* axis (registry-enumerated, so a future
tool is covered the day it is registered) and were **partial in the *seam* axis**
— the hot-list alias path (now removed) rendered through none of the other
three, so a live LLM-payload seam sat outside a gate that looked exhaustive.
The seam axis is closed from both ends here, and the two claims are NOT
interchangeable:

  - the per-seam arms  = the escape points that EXIST route through the
    projection helper (``parameters_for_export``);
  - ``test_no_shallow_parameters_copy_outside_the_projection_helper``  = a NEW
    escape point CANNOT avoid it. Needed because the absence of an arm is
    silent: a shallow copy is asymptomatic until something mutates it, which is
    how #3383 survived from a99bcf64 (2026-07-12) to 2026-07-28;
  - ``test_parameters_for_export_shares_no_mutable_substructure`` = the helper's
    own contract, independent of any caller.
"""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from reyn.runtime.router_tools import build_tools
from reyn.tools import get_default_registry
from reyn.tools.types import ToolContext, parameters_for_export
from reyn.tools.universal_catalog import _handle_describe_action

_SENTINEL_KEY = "__injected_by_a_provider_transform__"


def _mutate_in_place(node: Any) -> int:
    """Rewrite every dict in ``node`` the way a provider transform does.

    Injects a key into each dict and deletes one existing key — the two shapes
    litellm's Gemini/Vertex path applies (``add_object_type`` /
    ``_remove_additional_properties``). Returns the number of dicts touched so a
    caller can reject a vacuous run.
    """
    touched = 0
    if isinstance(node, dict):
        touched += 1
        for value in list(node.values()):
            touched += _mutate_in_place(value)
        existing = [k for k in node if k != _SENTINEL_KEY]
        node[_SENTINEL_KEY] = "object"
        if existing:
            del node[existing[0]]
    elif isinstance(node, list):
        for item in node:
            touched += _mutate_in_place(item)
    return touched


def _shared_nested_objects(exported: Any, canonical: Any) -> list[str]:
    """Paths at which ``exported`` still holds the SAME object as ``canonical``."""
    shared: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            if a is b:
                shared.append(path or "<root>")
                return
            for key in a:
                if key in b:
                    walk(a[key], b[key], f"{path}.{key}")
        elif isinstance(a, list) and isinstance(b, list):
            if a is b:
                shared.append(path or "<root>")
                return
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")

    walk(exported, canonical, "")
    return shared


def test_parameters_for_export_shares_no_mutable_substructure() -> None:
    """Tier 1: the projection helper's contract — nothing nested stays aliased."""
    registry = get_default_registry()
    nested_seen = 0
    for tool in registry:
        exported = parameters_for_export(tool.parameters)
        assert exported == dict(tool.parameters), (
            f"{tool.name}: parameters_for_export changed the schema's CONTENT; "
            "it must copy, not normalize"
        )
        shared = _shared_nested_objects(exported, tool.parameters)
        assert not shared, (
            f"{tool.name}: parameters_for_export left substructure aliased to the "
            f"canonical schema at {shared!r} — a consumer's in-place edit would "
            "reach the ToolDefinition (#3383)"
        )
        nested_seen += len(dict(tool.parameters).get("properties") or {})

    assert nested_seen > 0, (
        "no registered tool had nested properties — this arm would prove nothing"
    )


def test_render_for_router_survives_in_place_mutation_of_a_handed_out_payload() -> None:
    """Tier 1: mutating a render_for_router() payload cannot alter the next render."""
    registry = get_default_registry()
    pristine = {t.name: deepcopy(t.render_for_router()) for t in registry}

    total_touched = 0
    for tool in registry:
        payload = tool.render_for_router()
        total_touched += _mutate_in_place(payload)
        assert payload != pristine[tool.name], (
            f"{tool.name}: the mutation helper did not change the payload — "
            "this run would prove nothing"
        )

    assert total_touched > len(pristine), (
        "expected the mutation to reach nested sub-schemas, not just the top "
        f"level (touched={total_touched}, tools={len(pristine)})"
    )

    for tool in get_default_registry():
        assert tool.render_for_router() == pristine[tool.name], (
            f"{tool.name}: render_for_router() changed after an EARLIER payload "
            "was mutated in place — the canonical schema escaped by reference, "
            "so the schema the LLM receives depends on process history (#3383)"
        )


def test_build_tools_survives_in_place_mutation_of_a_handed_out_payload() -> None:
    """Tier 1: mutating a build_tools() payload cannot alter the next build_tools()."""
    agents = [{"name": "researcher", "role": "Research agent"}]
    pristine = deepcopy(build_tools(agents))

    payload = build_tools(agents)
    touched = _mutate_in_place(payload)
    assert touched > len(pristine), (
        f"mutation did not reach nested sub-schemas (touched={touched})"
    )
    assert payload != pristine, "the mutation helper did not change the payload"

    assert build_tools(agents) == pristine, (
        "build_tools() changed after an EARLIER payload was mutated in place — "
        "a ToolSpec / ToolDefinition schema escaped by reference (#3383)"
    )


def test_describe_action_survives_in_place_mutation_of_its_input_schema() -> None:
    """Tier 1: mutating describe_action's input_schema cannot alter the definition."""
    # events=None: describe_action is a pure registry read and emits nothing, so
    # no event sink is needed (and none is faked).
    ctx = ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )
    action = "install_plugin"
    pristine = deepcopy(asyncio.run(_handle_describe_action({"action_name": action}, ctx)))
    assert pristine["input_schema"], f"{action}: describe_action returned no input_schema"

    handed_out = asyncio.run(_handle_describe_action({"action_name": action}, ctx))
    touched = _mutate_in_place(handed_out["input_schema"])
    assert touched > 1, f"mutation did not reach nested sub-schemas (touched={touched})"

    again = asyncio.run(_handle_describe_action({"action_name": action}, ctx))
    assert again["input_schema"] == pristine["input_schema"], (
        "describe_action's input_schema changed after an EARLIER result was "
        "mutated in place — the canonical schema escaped by reference (#3383)"
    )
    canonical = get_default_registry().lookup(action)
    assert canonical is not None
    assert _SENTINEL_KEY not in str(canonical.parameters), (
        "the ToolDefinition's own parameters were rewritten by a mutation of a "
        "describe_action result (#3383)"
    )


# ── the syntactic half of the seam axis ──────────────────────────────────────


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _is_shallow_parameters_copy(node: ast.AST) -> bool:
    """True for a ``dict(<expr>.parameters)`` CALL — the #3383 defect's shape."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "parameters"
    )


def _projection_helper_span(types_py: Path) -> tuple[int, int]:
    """(start, end) line span of ``parameters_for_export`` in tools/types.py."""
    tree = ast.parse(types_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "parameters_for_export":
            return node.lineno, (node.end_lineno or node.lineno)
    raise AssertionError(
        "parameters_for_export not found in tools/types.py — the projection "
        "chokepoint moved or was renamed (#3383)"
    )


def test_no_shallow_parameters_copy_outside_the_projection_helper() -> None:
    """Tier 2: no new seam can shallow-copy a schema past the projection helper.

    Walks ``src/reyn`` and REDs on any ``dict(<expr>.parameters)`` call outside
    ``parameters_for_export`` itself. The per-seam arms above witness that the
    escape points that EXIST route through the helper; this one is what stops a
    seam written next month from re-introducing the defect, because the absence
    of a per-seam arm is silent (a shallow copy is asymptomatic until something
    mutates it).

    **What this gate does NOT catch — and why it need not, in the same place, so
    the reason is available exactly when someone is deciding whether the
    exemption still holds.**

    It matches one syntactic shape, ``dict(<expr>.parameters)``. It will not
    catch a bare ``<expr>.parameters`` handed out with NO copy at all. One such
    site exists and is deliberately not flagged: ``runtime/router_tools.py``'s
    ``ToolSpec.to_openai_dict`` returns ``self.parameters`` uncopied.

    ★ That site's safety is **derived, not intrinsic**. It is safe only because
    every ``ToolSpec`` construction in ``build_tools`` has the shape
    ``ToolSpec(parameters=_X_rendered["function"]["parameters"], ...)`` — i.e.
    ``ToolSpec.parameters`` is always a ``render_for_router()`` product, and
    therefore already a per-call deep copy produced by ``parameters_for_export``.
    **If ``render_for_router`` is ever relaxed back toward a shallow copy, that
    exempt site silently becomes an ungated escape point.** The exemption is
    contingent on this PR's fix holding; it is not a standing property of
    ``ToolSpec``.

    Widening the gate to flag bare attribute reads would put a legitimate site in
    its false-positive set, and a gate that cries wolf gets suppressed — then it
    protects nothing. The bounded version is honest and survivable, and the
    ``ToolSpec`` seam is covered behaviourally instead, by
    ``test_build_tools_survives_in_place_mutation_of_a_handed_out_payload``
    (which does go RED if ``render_for_router`` regresses — verified by
    strip-falsify).
    """
    root = _repo_root()
    src = root / "src" / "reyn"
    types_py = (src / "tools" / "types.py").resolve()
    start, end = _projection_helper_span(types_py)

    offenders: list[str] = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_shallow_parameters_copy(node):
                continue
            inside_helper = py.resolve() == types_py and start <= node.lineno <= end
            if not inside_helper:
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "dict(<expr>.parameters) outside parameters_for_export — a SHALLOW copy "
        "leaves every nested sub-schema aliased to the canonical definition, and "
        "litellm's provider transforms rewrite the payload they are handed in "
        "place, so one Gemini/Vertex turn rewrites the schema for the rest of the "
        "process (#3383). Call reyn.tools.types.parameters_for_export instead. "
        f"Offending sites: {offenders}"
    )


def test_projection_helper_actually_performs_a_deep_copy() -> None:
    """Tier 2: positive guard — the allow-listed helper really deep-copies.

    Without this, renaming or gutting ``parameters_for_export`` would leave the
    AST gate above scanning for offenders against a chokepoint that no longer
    does anything — asserting-empty and vacuously green. (This arm is why the
    gate's allow-list is anchored to a function that is checked to still exist:
    ``_projection_helper_span`` raises rather than returning an empty span.)
    """
    root = _repo_root()
    types_py = (root / "src" / "reyn" / "tools" / "types.py").resolve()
    start, end = _projection_helper_span(types_py)
    tree = ast.parse(types_py.read_text(encoding="utf-8"))
    deep_copies = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "deepcopy"
        and start <= node.lineno <= end
    ]
    assert deep_copies, (
        "parameters_for_export no longer calls deepcopy — the projection that "
        "every seam is routed through has stopped owning its one obligation, and "
        "the AST gate above is now guarding nothing (#3383)"
    )
