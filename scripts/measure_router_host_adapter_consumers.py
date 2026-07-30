#!/usr/bin/env python3
"""Measure, per ``RouterHostAdapter.__init__`` param, WHERE its value is read.

The output is the SSoT for one predicate: *does this constructor param share
an exact consumer set with another param* (= is it half of a real bundle)?
``tests/test_router_host_adapter_param_gate_3482.py`` imports this module and
DERIVES that predicate instead of trusting prose — the #3482 defect was a
registry of 58 hand-written "no shared-consumer partner" reasons whose truth
nothing checked, and 6 of which were measurably false.

Altitude (the part that got #3482 wrong the first time)
-------------------------------------------------------
Measuring "who reads this param" *inside* the adapter answers the wrong
question: the adapter is a RELAY, so nearly every param resolves to its own
same-named forwarding method by construction, and one gets N singletons for N
params no matter how the wiring is actually shaped. A param's consumer is
therefore the DESTINATION its value is carried to, measured as:

  consumers(p) = { "adapter.<m>" for each adapter member m reading p's stored
                   attribute }
               ∪ { "<module>::<func>" for each site OUTSIDE the adapter that
                   reads p's stored attribute, or reads/calls one of those
                   members, off a host/adapter-named expression }

Both halves matter. Without the adapter members, ``append_history`` (carried
to two members) looks identical to ``put_outbox`` (carried to one). Without
the external sites, params with no in-class reader (``journal``,
``output_language``) look dead.

Discriminator: EXACT SET EQUALITY, never overlap
------------------------------------------------
Overlap is not usable: on the measured tree 80 param pairs overlap, and
overlap is not transitive (A∩B≠∅ ∧ B∩C≠∅ with A∩C=∅), so it cannot define a
bundle boundary — its transitive closure melts into one 28-param blob. Exact
equality is what "carried together to the same place" means.

Already-bundled consumers are not counted twice
-----------------------------------------------
A member that reads fields off an ALREADY-bundled attribute (e.g.
``make_router_op_context`` reading ``self._op_ctx.<field>``) is a consumer
whose cluster has already been landed as a bundle. Left in, such a hub
manufactures equality between params that in fact travel to different
dedicated accessors (6 params on the measured tree). It is excluded from the
consumer set — the param itself is NOT excluded, only that one destination,
per the #3482 firm's "exclude the hub by role, not by threshold; ``run`` /
``run_loop`` are not hubs" rule. The hub set is DERIVED from the bundle-typed
attributes, so a newly landed bundle updates it with no edit here.

stdlib-only (``ast``/``pathlib``), no ``reyn`` import: both registries and the
signature are AST-derived from the file on disk, so a hand-maintained list can
never drift from what is measured.
"""
from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ADAPTER_RELPATH = Path("src") / "reyn" / "runtime" / "services" / "router_host_adapter.py"
ADAPTER_CLASS = "RouterHostAdapter"
SCAN_ROOT_RELPATH = Path("src") / "reyn"

# An attribute access is treated as an external read of the adapter's surface
# when its base expression names a host/adapter — `host.x`, `self._router_host.x`,
# `router_host.x`, `adapter.x`. Deliberately conservative: a false NEGATIVE here
# shows up as "consumer unmeasured" (a shelf entry a human must justify), while a
# broader match would invent consumers and manufacture clusters.
_HOST_BASE_SUFFIXES = ("host", "adapter")


@dataclass(frozen=True)
class ParamMeasurement:
    """One ``__init__`` param and the destinations its value reaches."""

    name: str
    annotation: str | None
    bundle_type: str | None
    stored_attrs: tuple[str, ...]
    adapter_members: tuple[str, ...]
    external_sites: tuple[str, ...]

    @property
    def is_bundled(self) -> bool:
        return self.bundle_type is not None

    @property
    def consumers(self) -> frozenset[str]:
        return frozenset(
            [f"adapter.{m}" for m in self.adapter_members] + list(self.external_sites)
        )


@dataclass(frozen=True)
class Measurement:
    """The whole signature, measured."""

    params: tuple[ParamMeasurement, ...]
    bundle_types: tuple[str, ...]
    bundled_consumers: tuple[str, ...]
    unmeasured_registry: dict[str, str]
    blocked_registry: dict[str, str]

    def by_name(self) -> dict[str, ParamMeasurement]:
        return {p.name: p for p in self.params}

    @property
    def bare_params(self) -> tuple[ParamMeasurement, ...]:
        return tuple(p for p in self.params if not p.is_bundled)

    def partners_of(self, name: str) -> tuple[str, ...]:
        """Bare params whose consumer set is EXACTLY equal to ``name``'s.

        Empty for a param with no measurable consumer — "no partner" and "not
        measurable" are different shelves and must not be conflated.
        """
        mine = self.by_name()[name].consumers
        if not mine:
            return ()
        return tuple(
            sorted(p.name for p in self.bare_params if p.name != name and p.consumers == mine)
        )

    def exact_match_clusters(self) -> tuple[tuple[str, ...], ...]:
        groups: dict[frozenset[str], list[str]] = defaultdict(list)
        for p in self.bare_params:
            if p.consumers:
                groups[p.consumers].append(p.name)
        return tuple(
            sorted(
                (tuple(sorted(names)) for names in groups.values() if len(names) > 1),
                key=lambda names: (-len(names), names),
            )
        )

    def unmeasured_params(self) -> tuple[str, ...]:
        return tuple(sorted(p.name for p in self.bare_params if not p.consumers))


def _init_node(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLASS:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return item
    raise RuntimeError(f"{ADAPTER_CLASS}.__init__ not found by AST")


def _class_node(tree: ast.Module) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLASS:
            return node
    raise RuntimeError(f"class {ADAPTER_CLASS} not found by AST")


def _module_level_dict(tree: ast.Module, name: str) -> dict[str, str]:
    """Read a module-level ``name: ... = {"k": "v"}`` literal by AST."""
    for node in tree.body:
        targets = list(getattr(node, "targets", []))
        target = getattr(node, "target", None)
        if target is not None:
            targets.append(target)
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Dict):
            raise RuntimeError(f"{name} is not a dict literal — AST derivation broke")
        out: dict[str, str] = {}
        for k, v in zip(value.keys, value.values):
            if not isinstance(k, ast.Constant):
                raise RuntimeError(f"{name} has a non-literal key — AST derivation broke")
            try:
                out[str(k.value)] = str(ast.literal_eval(v))
            except ValueError:
                # A shared reason constant referenced by Name: keep the source
                # text so a claim check still has something to read.
                out[str(k.value)] = ast.unparse(v)
        return out
    return {}


def _module_level_str_tuple(tree: ast.Module, name: str) -> tuple[str, ...]:
    for node in tree.body:
        targets = list(getattr(node, "targets", []))
        target = getattr(node, "target", None)
        if target is not None:
            targets.append(target)
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, (ast.Tuple, ast.List)):
            raise RuntimeError(f"{name} is not a tuple/list literal — AST derivation broke")
        return tuple(str(ast.literal_eval(e)) for e in value.elts)
    return ()


def _stored_attrs(init: ast.FunctionDef, param_names: set[str]) -> dict[str, set[str]]:
    """param -> the ``self.<attr>`` names its value is stored on.

    Any ``Name`` appearing in the assigned expression counts, so wrapped forms
    (``Path(state_dir) if state_dir is not None else _DEFAULT``) are covered.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(init):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        for t in targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                for p in names & param_names:
                    out[p].add(t.attr)
    return out


def _member_attr_reads(cls: ast.ClassDef) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(attr -> members reading it, member -> attrs it reads), ``__init__`` aside."""
    attr_readers: dict[str, set[str]] = defaultdict(set)
    member_attrs: dict[str, set[str]] = defaultdict(set)
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name == "__init__":
            continue
        for node in ast.walk(item):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "self":
                    attr_readers[node.attr].add(item.name)
                    member_attrs[item.name].add(node.attr)
    return attr_readers, member_attrs


def _bundled_consumers(attr_readers: dict[str, set[str]], bundle_attrs: set[str]) -> set[str]:
    """Members that read an already-bundled attribute (the hubs).

    Touching ``self._op_ctx`` at all makes ``make_router_op_context`` a bundled
    consumer: its cluster is already landed, so counting it again would
    manufacture equality between bare params that in fact travel to different
    dedicated accessors. Keyed on the ATTRIBUTE, not on ``self._op_ctx.<field>``
    subscripting, because a member is free to alias first
    (``op_ctx = self._op_ctx``) — which the real code does.
    """
    hubs: set[str] = set()
    for attr in bundle_attrs:
        hubs |= attr_readers.get(attr, set())
    return hubs


def _external_reads(repo_root: Path, adapter_path: Path) -> dict[str, set[str]]:
    """name -> {"<relpath>::<func>"} for reads off a host/adapter-named base."""
    out: dict[str, set[str]] = defaultdict(set)
    scan_root = repo_root / SCAN_ROOT_RELPATH
    for path in sorted(scan_root.rglob("*.py")):
        if path.resolve() == adapter_path.resolve():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(scan_root).as_posix()
        stack: list[str] = []

        class _V(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
                base = ast.unparse(node.value)
                if any(base.endswith(suffix) for suffix in _HOST_BASE_SUFFIXES):
                    out[node.attr].add(f"{rel}::{stack[-1] if stack else '<module>'}")
                self.generic_visit(node)

        _V().visit(tree)
    return out


def measure(repo_root: Path) -> Measurement:
    """AST-measure every ``__init__`` param's consumer set (see module docstring)."""
    adapter_path = repo_root / ADAPTER_RELPATH
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    cls = _class_node(tree)
    init = _init_node(tree)

    bundle_types = _module_level_str_tuple(tree, "ROUTER_HOST_ADAPTER_BUNDLE_TYPES")
    unmeasured_registry = _module_level_dict(tree, "ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED")
    blocked_registry = _module_level_dict(tree, "ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED")

    annotations: dict[str, str | None] = {}
    args = init.args
    for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if a.arg == "self":
            continue
        annotations[a.arg] = ast.unparse(a.annotation) if a.annotation else None

    def bundle_of(annotation: str | None) -> str | None:
        for name in bundle_types:
            if name in (annotation or ""):
                return name
        return None

    stored = _stored_attrs(init, set(annotations))
    attr_readers, _member_attrs = _member_attr_reads(cls)
    bundle_attrs = {
        attr
        for name, annotation in annotations.items()
        if bundle_of(annotation)
        for attr in stored.get(name, set())
    }
    hubs = _bundled_consumers(attr_readers, bundle_attrs)
    external = _external_reads(repo_root, adapter_path)

    params: list[ParamMeasurement] = []
    for name, annotation in annotations.items():
        attrs = stored.get(name, set())
        members = {m for a in attrs for m in attr_readers.get(a, set())} - hubs
        sites: set[str] = set()
        for a in attrs:
            sites |= external.get(a, set())
        for m in members:
            sites |= external.get(m, set())
        params.append(
            ParamMeasurement(
                name=name,
                annotation=annotation,
                bundle_type=bundle_of(annotation),
                stored_attrs=tuple(sorted(attrs)),
                adapter_members=tuple(sorted(members)),
                external_sites=tuple(sorted(sites)),
            )
        )

    return Measurement(
        params=tuple(params),
        bundle_types=bundle_types,
        bundled_consumers=tuple(sorted(hubs)),
        unmeasured_registry=unmeasured_registry,
        blocked_registry=blocked_registry,
    )


def repo_root_from(start: Path) -> Path:
    for ancestor in [start, *start.parents]:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError(f"repo root not found from {start}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", type=Path, default=None)
    ap.add_argument("--detail", action="store_true", help="print every param's destinations")
    ns = ap.parse_args(argv)
    root = ns.repo_root or repo_root_from(Path(__file__).resolve())
    m = measure(root)

    bundled = [p for p in m.params if p.is_bundled]
    print(f"repo root:            {root}")
    print(f"__init__ params:      {len(m.params)}  (bundled {len(bundled)} / bare {len(m.bare_params)})")
    print(f"bundle types:         {list(m.bundle_types)}")
    print(f"bundled consumers excluded from clustering: {list(m.bundled_consumers)}")

    clusters = m.exact_match_clusters()
    print(f"\nexact-match clusters: {len(clusters)} "
          f"/ {sum(len(c) for c in clusters)} bare params")
    for names in clusters:
        print(f"  {list(names)}")
        print(f"      consumers: {sorted(m.by_name()[names[0]].consumers)}")

    unmeasured = m.unmeasured_params()
    print(f"\nno measurable consumer ({len(unmeasured)}):")
    for name in unmeasured:
        print(f"  {name}: shelf reason = {m.unmeasured_registry.get(name, '<<MISSING>>')!r}")

    if ns.detail:
        print("\nper-param destinations:")
        for p in sorted(m.params, key=lambda p: p.name):
            tag = f"[{p.bundle_type}]" if p.is_bundled else ""
            print(f"  {p.name}{tag}: attrs={list(p.stored_attrs)} "
                  f"members={list(p.adapter_members)} external={list(p.external_sites)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
