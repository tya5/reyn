"""Tier 2: OS invariant — every RouterHostAdapter.__init__ param's annotation
is either a registered bundle type or a reasoned scalar exception (#3482,
mirroring #3451/#3437's AST-derived SSoT + bidirectional-gate shape).

The #3482 firm (issue thread, architect's re-grounded comment on current
main 890e22d2) measured two real consumer-set clusters in RouterHostAdapter's
77 constructor params — the 16-field op-context cluster
(``RouterOpContextInputs``, sole reader ``make_router_op_context``) and the
3-field mcp-gateway cluster (``McpGatewayInputs``, sole reader
``_mcp_list_via_gateway``) — and explicitly REJECTED forcing 100% bundle
coverage ("consumer 集合が一致するものだけを束ねる... 強制収容は接頭辞
（形）に逃げる圧力になります"). The remaining ~58 params stay bare scalars,
each with a reason recorded in ``ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS``.

This is NOT a param-count pin (Tier-4): the gate never compares against a
fixed N. It asserts a STRUCTURE — every param falls into one of exactly two
buckets, both enumerable and both non-empty — so a new bare param added
tomorrow with neither a bundle annotation nor a registry entry goes RED at
the exact moment it's added, not silently.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.runtime.services.router_host_adapter import (
    ROUTER_HOST_ADAPTER_BUNDLE_TYPES,
    ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS,
    RouterHostAdapter,
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _init_params() -> list[tuple[str, "str | None"]]:
    """AST-derive {param_name: annotation_source} for RouterHostAdapter.__init__
    from the REAL file on disk — never a hand-maintained list (the #3437/#3451
    lesson: a hand-written enumeration silently drifts from the signature)."""
    root = _repo_root()
    path = root / "src" / "reyn" / "runtime" / "services" / "router_host_adapter.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RouterHostAdapter":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = item.args
                    out = []
                    for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                        if a.arg == "self":
                            continue
                        ann = ast.unparse(a.annotation) if a.annotation else None
                        out.append((a.arg, ann))
                    return out
    raise RuntimeError("RouterHostAdapter.__init__ not found via AST")


def test_bundle_type_and_exception_registries_are_non_empty() -> None:
    """Tier 2: vacuity guard — an empty registry on either side would make
    the completeness assertion below pass trivially (#3437's vacuity lesson:
    a gate that can be satisfied by declaring nothing is not a gate)."""
    assert ROUTER_HOST_ADAPTER_BUNDLE_TYPES, (
        "ROUTER_HOST_ADAPTER_BUNDLE_TYPES is empty — the gate would vacuously "
        "pass by finding no bundle-typed params to recognize."
    )
    assert ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS, (
        "ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS is empty — the gate would "
        "vacuously pass by finding no exceptions either."
    )
    for name, reason in ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS.items():
        assert reason and reason.strip(), (
            f"scalar exception {name!r} has an empty reason — a registry "
            "entry with no reason is a bare param wearing a disguise."
        )


def test_every_init_param_is_bundled_or_reasoned() -> None:
    """Tier 2: every RouterHostAdapter.__init__ param (AST-derived from the
    real file, not hand-listed) has an annotation naming a registered bundle
    type, OR is a key in the scalar exception registry with a reason.

    RED the moment a new bare param (e.g. ``foo_fn: Callable``) is added
    without either — the exact N+1 gate the #3482 firm required, without
    pinning a param count (Tier-4)."""
    params = _init_params()
    assert params, "AST found zero __init__ params — parser drifted from the real signature."

    undeclared: list[tuple[str, "str | None"]] = []
    for name, ann in params:
        ann_str = ann or ""
        if any(bundle_name in ann_str for bundle_name in ROUTER_HOST_ADAPTER_BUNDLE_TYPES):
            continue
        if name in ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS:
            continue
        undeclared.append((name, ann))

    assert not undeclared, (
        "RouterHostAdapter.__init__ has params with neither a registered bundle "
        "annotation nor a scalar-exception registry entry — a bare param slipped "
        "in uncovered:\n"
        + "\n".join(f"  {n}: {a}" for n, a in undeclared)
        + "\nEither fold it into an existing/new bundle (if it shares a consumer "
        "with one), or add it to ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS with a "
        "reason (current-form, not a scheduled future fold)."
    )


def test_scalar_exception_keys_match_real_bare_params_exactly() -> None:
    """Tier 2: real ⊆ declared AND declared ⊆ real — the registry names
    exactly the params that are actually bare scalars right now, no more, no
    less. Catches BOTH a stale entry (param renamed/removed/bundled but the
    registry key survives) and a param quietly re-annotated to a bundle type
    while a same-named registry entry lingers unnoticed."""
    params = _init_params()
    bare_now = {
        name
        for name, ann in params
        if not any(bundle_name in (ann or "") for bundle_name in ROUTER_HOST_ADAPTER_BUNDLE_TYPES)
    }
    registered = set(ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS)

    stale = registered - bare_now
    assert not stale, (
        f"ROUTER_HOST_ADAPTER_SCALAR_EXCEPTIONS has stale entries no longer "
        f"matching a bare __init__ param: {sorted(stale)}"
    )
    missing = bare_now - registered
    assert not missing, (
        f"These bare __init__ params have no scalar-exception registry entry: "
        f"{sorted(missing)}"
    )


def test_router_host_adapter_is_the_real_class_under_gate() -> None:
    """Tier 2: sanity — RouterHostAdapter imports cleanly and is the class
    the AST walk above is describing (import-identity check, not a private-
    state assertion)."""
    assert RouterHostAdapter.__name__ == "RouterHostAdapter"
