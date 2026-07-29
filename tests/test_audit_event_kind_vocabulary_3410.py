"""Tier 2: the audit-event ``type`` namespace is a CLOSED vocabulary (#3410).

An audit-event's ``type`` is a public interface, not an internal label: reyn is
not the only consumer of ``.reyn/events``. An external subscriber has to be able
to enumerate "every kind I might receive", and an open namespace makes that
impossible in principle. So the vocabulary is declared once, in
``reyn.core.events.event_schema.AUDIT_EVENT_KINDS``, and this module is the gate
that keeps the declaration and the code from drifting apart in EITHER direction:

- **emit ⊆ declaration** — a producer cannot ship a kind the vocabulary does not
  contain. That is #3410's own defect: ``mcp_resources_listed`` /
  ``mcp_prompts_listed`` were emitted for months while no schema, doc or test
  knew they existed.
- **declaration ⊆ emit** — the vocabulary cannot contain a kind no producer
  emits. That is #3357's defect (a subscriber waits forever for a kind nobody
  writes), generalised from the progress fan-out subset
  (``tests/test_progress_lifecycle_fanout_3357.py``) to the whole namespace.
- **doc == declaration** — ``docs/reference/runtime/events.md`` carries the
  enumeration an external consumer actually reads, derived from the same source.

Two supporting gates keep the census itself honest, because a census that
cannot see a producer would make both directions above vacuously green:

- **The seam registry is complete.** The census reads *string-constant* kind
  arguments at calls to a declared set of emit seams. A function that forwards
  its own parameter into a seam IS itself a seam (it can mint any kind), so the
  gate derives those functions from the AST and requires each to be declared.
  Adding an undeclared kind-parameterised forwarder → RED.
- **The blind spots are enumerated.** A call site that passes a kind the AST
  cannot resolve to a constant is, by construction, invisible to the census.
  Those sites are declared in ``DYNAMIC_KIND_EMIT_SITES`` with a classification;
  a new one appearing → RED. The gate cannot close a hole it cannot see, but it
  can refuse to let the set of holes grow silently.

Real instances throughout: the real declaration modules, the real source tree,
the real doc file. Nothing is faked — the census IS the production source.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from reyn.core.events.event_schema import (
    AUDIT_EVENT_KINDS,
    DYNAMIC_KIND_EMIT_SITES,
    EVENT_AUDIT_REQUIREMENTS,
    KIND_EMIT_SEAMS,
    DynamicEmitSite,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "reyn"
_EVENTS_REFERENCE = _REPO / "docs" / "reference" / "runtime" / "events.md"

_DOC_BEGIN = "<!-- BEGIN audit-event-kinds -->"
_DOC_END = "<!-- END audit-event-kinds -->"


# ── The census ────────────────────────────────────────────────────────────


def _source_files() -> list[Path]:
    return [p for p in sorted(_SRC.rglob("*.py")) if "__pycache__" not in p.parts]


def _module_string_constants(trees: dict[Path, ast.Module]) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings across ``src/reyn``.

    Some emit sites pass a named constant rather than an inline literal
    (``events.emit(CANONICAL_FALLBACK_EVENT, ...)``). Resolving those keeps them
    out of the blind-spot registry, where they would be indistinguishable from a
    genuinely unreadable kind. A name bound to two different strings anywhere in
    the tree is left unresolved — an ambiguous name is not evidence.
    """
    seen: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                seen.setdefault(node.targets[0].id, set()).add(node.value.value)
    return {name: next(iter(v)) for name, v in seen.items() if len(v) == 1}


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _kind_argument(call: ast.Call, seam: str, constants: dict[str, str]) -> str | None:
    """The kind this seam call names, or ``None`` when it is not a constant.

    A seam declared with a keyword (``KIND_EMIT_SEAMS[seam]``) is read from that
    keyword only — nothing else in the call can vouch for a kind. A seam
    declared with ``None`` takes the first positional argument that is a string
    constant (or a name bound to one). "First string constant" rather than
    "argument 0" because the positional seams are not uniform: the index
    coordinator's ``_emit(events, kind, **data)`` takes its sink first, and a
    rule keyed on slot 0 would read that sink as an unreadable kind and hide
    five real kinds behind a false blind spot.
    """
    keyword = KIND_EMIT_SEAMS[seam]
    if keyword is not None:
        for kw in call.keywords:
            if kw.arg == keyword and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        return None
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name) and arg.id in constants:
            return constants[arg.id]
    return None


def _seam_calls(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in KIND_EMIT_SEAMS:
            continue
        # A seam whose kind rides a positional slot needs a positional argument
        # to be a kind-carrying call at all. This is what keeps unrelated
        # keyword-only ``emit(...)`` methods (the OTel log exporter's, for one)
        # out of the census instead of showing up as an unreadable kind.
        if KIND_EMIT_SEAMS[name] is None and not node.args:
            continue
        calls.append(node)
    return calls


def _innermost_functions(tree: ast.Module) -> dict[int, str]:
    """Map each AST node id to the name of its innermost enclosing function."""
    owner: dict[int, str] = {}

    def descend(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                descend(child, child.name)
            else:
                if current is not None:
                    owner[id(child)] = current
                descend(child, current)

    descend(tree, None)
    return owner


def _census() -> tuple[dict[str, list[str]], set[tuple[str, str, str]], set[str]]:
    """Walk ``src/reyn`` once and return (kinds→sites, blind spots, forwarders).

    ★ An AST walk, not a text scan, and the distinction is load-bearing rather
    than stylistic (the same reasoning #3357's gate records): every module this
    gate guards discusses kind names in prose, and a ``grep`` for ``emit("x"``
    would let a docstring vouch for a kind no call site produces. Only an
    ``ast.Call`` to a declared seam counts.
    """
    files = _source_files()
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in files}
    constants = _module_string_constants(trees)

    kinds: dict[str, list[str]] = {}
    blind: set[tuple[str, str, str]] = set()
    forwarders: set[str] = set()

    for path, tree in trees.items():
        rel = path.relative_to(_REPO).as_posix()
        owner = _innermost_functions(tree)
        params_of: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                params_of[node.name] = {
                    a.arg for a in args.args + args.posonlyargs + args.kwonlyargs
                }

        for call in _seam_calls(tree):
            seam = _callee_name(call)
            assert seam is not None
            kind = _kind_argument(call, seam, constants)
            enclosing = owner.get(id(call), "<module>")
            if kind is not None:
                kinds.setdefault(kind, []).append(f"{rel}:{call.lineno}")
                continue
            blind.add((rel, enclosing, seam))
            # A function that hands one of its OWN parameters to a seam's kind
            # slot can mint any kind its callers ask for — it is a seam, and the
            # census is blind to every kind flowing through it until it is
            # declared as one.
            keyword = KIND_EMIT_SEAMS[seam]
            slots: list[ast.expr] = (
                list(call.args)
                if keyword is None
                else [k.value for k in call.keywords if k.arg == keyword]
            )
            own = params_of.get(enclosing, set())
            if any(isinstance(sl, ast.Name) and sl.id in own for sl in slots):
                forwarders.add(enclosing)

    return kinds, blind, forwarders


# ── 1. The vocabulary is closed in both directions ────────────────────────


def test_every_emitted_kind_is_declared_in_the_vocabulary() -> None:
    """Tier 2: no producer in ``src/reyn`` emits a kind outside
    ``AUDIT_EVENT_KINDS``.

    This is #3410's own defect as a gate. Emit a new kind without declaring it →
    RED, which is precisely what did not happen when ``mcp_resources_listed`` /
    ``mcp_prompts_listed`` shipped: nothing outside the two call sites knew they
    existed, so an external consumer could receive a kind that appears in no
    schema and no document.
    """
    kinds, _, _ = _census()
    undeclared = {k: sorted(v) for k, v in kinds.items() if k not in AUDIT_EVENT_KINDS}
    assert not undeclared, (
        "audit-event kinds are emitted but absent from AUDIT_EVENT_KINDS — the "
        "kind vocabulary is a public interface, so an undeclared kind reaches "
        "external consumers as a type they cannot enumerate. Declare each in "
        f"reyn.core.events.event_schema: {undeclared}"
    )


def test_every_declared_kind_has_a_live_emit_call_site() -> None:
    """Tier 2: every member of ``AUDIT_EVENT_KINDS`` is emitted by a real
    producer in ``src/reyn``.

    The #3357 direction, generalised from the four progress-fan-out kinds to the
    whole namespace. A declared kind nobody emits is worse than a missing one:
    it is a promise in the public vocabulary that a subscriber can wait on
    forever. Delete a producer without deleting its declaration → RED.
    """
    kinds, _, _ = _census()
    dead = sorted(AUDIT_EVENT_KINDS - set(kinds))
    assert not dead, (
        "AUDIT_EVENT_KINDS declares kinds with no emit call-site in src/reyn "
        "(a subscriber would wait for them forever): "
        f"{dead}"
    )


def test_field_requirements_only_constrain_kinds_in_the_vocabulary() -> None:
    """Tier 2: ``EVENT_AUDIT_REQUIREMENTS`` keys are a subset of
    ``AUDIT_EVENT_KINDS``.

    The two registries answer different questions — *which kinds exist* versus
    *what fields a given kind must carry* — and the field map is a refinement of
    the vocabulary, never a second source of it. Before #3410 nothing said so,
    which is how ``mcp_search_invoked`` / ``mcp_tool_loaded`` sat in the field
    map declaring required fields for a switched-off code path: the only
    registry that mentioned them was the one that never claimed to say what
    exists.
    """
    orphans = sorted(set(EVENT_AUDIT_REQUIREMENTS) - AUDIT_EVENT_KINDS)
    assert not orphans, (
        "EVENT_AUDIT_REQUIREMENTS declares required fields for kinds that are "
        f"not in the audit-event vocabulary: {orphans}"
    )


# ── 2. The census can see what it claims to see ───────────────────────────


def test_every_kind_forwarding_function_is_a_declared_seam() -> None:
    """Tier 2: a function that passes one of its own parameters into a seam's
    kind slot is itself declared in ``KIND_EMIT_SEAMS``.

    ★ This is the arm that keeps the two vocabulary gates from going vacuously
    green. Enumerating seams by hand covers the seams someone remembered; it
    says nothing about a new one. A forwarder can mint ANY kind its callers ask
    for, so an undeclared forwarder is a whole family of emissions the census
    never sees — and both directions above would still pass. Add
    ``def relay(self, kind, **d): self._events.emit(kind, **d)`` without
    declaring ``relay`` → RED.
    """
    _, _, forwarders = _census()
    undeclared = sorted(forwarders - set(KIND_EMIT_SEAMS))
    assert not undeclared, (
        "these functions forward a parameter into an audit-emit seam's kind "
        "slot, so they are kind-emit seams themselves and every kind flowing "
        "through them is invisible to the vocabulary census. Declare each in "
        f"KIND_EMIT_SEAMS: {undeclared}"
    )


def test_dynamic_kind_emit_sites_are_exactly_the_declared_ones() -> None:
    """Tier 2: the set of seam call sites whose kind the AST cannot resolve to a
    constant equals ``DYNAMIC_KIND_EMIT_SITES``.

    A gate cannot close a namespace through a call site it cannot read. What it
    CAN do is refuse to let the unreadable set grow without a decision: each
    blind spot is declared with a classification saying why it is one and what,
    if anything, flows through it. Introduce a new ``emit(f"x_{y}")`` → RED with
    the site named.

    Keyed by (file, enclosing function, seam) rather than line number: a line
    number is not an identifier across a moving ``main``, and a gate that goes
    red on an unrelated edit above it teaches people to re-run and re-pin.
    """
    _, blind, _ = _census()
    declared = {(s.module, s.function, s.seam) for s in DYNAMIC_KIND_EMIT_SITES}
    new = sorted(blind - declared)
    gone = sorted(declared - blind)
    assert not new, (
        "audit-emit call sites pass a kind the vocabulary census cannot read as "
        "a constant — every kind they emit bypasses the closed vocabulary. "
        "Either pass a literal, or declare the site in DYNAMIC_KIND_EMIT_SITES "
        f"with its classification: {new}"
    )
    assert not gone, (
        "DYNAMIC_KIND_EMIT_SITES declares blind spots that no longer exist — "
        f"drop them so the registry stays a census, not a wish list: {gone}"
    )


def test_declared_forwarder_blind_spots_name_a_registered_seam() -> None:
    """Tier 2: every blind spot classified ``FORWARDER`` sits in a function that
    is itself a declared seam.

    The classification is the claim "no new kind is minted here, the kind came
    from a caller the census already reads". That claim is only true if the
    forwarding function is registered — otherwise the kinds flowing through it
    were never censused at all, and calling the site a harmless forwarder would
    be exactly the wrong reading of it.
    """
    unbacked = sorted(
        (s.module, s.function)
        for s in DYNAMIC_KIND_EMIT_SITES
        if s.classification == "FORWARDER" and s.function not in KIND_EMIT_SEAMS
    )
    assert not unbacked, (
        "blind spots classified FORWARDER whose enclosing function is not a "
        f"declared seam (so its callers are not censused either): {unbacked}"
    )


def _kind_selector_collections() -> list[tuple[str, int, list[str], set[str]]]:
    """Every string-literal collection in ``src/reyn`` that is MOSTLY kind names.

    A consumer selection — "which kinds this surface forwards / maps / logs" —
    is a legitimate second list; it answers a different question than the
    vocabulary does. What is never legitimate is a selection naming a kind the
    vocabulary does not contain: nothing can ever match it.

    "Mostly" is the discriminator, and it is what makes this checkable without a
    hand-written registry of selections (which would have the same completeness
    problem as the seam list). A collection where EVERY member is a kind is
    trivially fine; a collection where NONE is has nothing to do with kinds — a
    list of ``op`` values, of config keys, of field names. Only the mixed case
    needs a human: two or more members are kinds, and at least one is not.
    """
    out = []
    for path in _source_files():
        if path == _SRC / "core" / "events" / "event_schema.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.List, ast.Tuple)) or not node.elts:
                continue
            values = [
                e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if len(values) != len(node.elts):
                continue
            known = [v for v in values if v in AUDIT_EVENT_KINDS]
            if len(known) >= 2 and len(known) < len(values):
                rel = path.relative_to(_REPO).as_posix()
                out.append((rel, node.lineno, values, set(values) - AUDIT_EVENT_KINDS))
    return out


def test_no_consumer_selection_names_a_kind_outside_the_vocabulary() -> None:
    """Tier 2: a collection of audit-event kind names in ``src/reyn`` does not
    mix in a name the vocabulary does not contain.

    ★ The third face of the same defect. #3357 was a *declaration* with no
    producer; #3410 was a *producer* with no declaration; this is a *consumer
    selection* naming a kind that does not exist — an arm that can never fire,
    invisible because nothing crashes. It is the failure mode the closed
    vocabulary makes checkable for the first time: before there was a complete
    list, "is this name real?" had no mechanical answer.

    Found one on its first run: the OTel exporter routed ``safety_triggered`` /
    ``safety_limit_reached`` to OTLP log records at WARN, and neither name
    existed anywhere else in the repo — an operator would have waited for
    safety records that could not arrive.

    Add a plausible-but-nonexistent kind to any such collection → RED.

    Deliberately NOT asserted: that a selection covers every kind it *should*.
    Which kinds a surface forwards is a product decision (see
    ``progress_lifecycle.PROGRESS_LIFECYCLE_EVENTS`` — four of 200+, on
    purpose), and there is nothing to derive a "should" from.
    """
    drifted = _kind_selector_collections()
    assert not drifted, (
        "these collections are mostly audit-event kind names but include names "
        "the closed vocabulary does not contain — nothing will ever match "
        "them: "
        + "; ".join(
            f"{rel}:{line} → {sorted(outsiders)}" for rel, line, _v, outsiders in drifted
        )
    )


# ── 3. The doc an external consumer reads is derived, not hand-written ────


def _documented_kinds() -> list[str]:
    text = _EVENTS_REFERENCE.read_text(encoding="utf-8")
    start = text.index(_DOC_BEGIN) + len(_DOC_BEGIN)
    end = text.index(_DOC_END)
    return re.findall(r"^([a-z0-9_]+)$", text[start:end], flags=re.MULTILINE)


def test_events_reference_enumerates_exactly_the_declared_vocabulary() -> None:
    """Tier 2: the enumeration in ``docs/reference/runtime/events.md`` is exactly
    ``AUDIT_EVENT_KINDS``.

    ★ The doc side of the drift, which until #3410 nothing checked anywhere in
    this repo. ``CLAUDE.md`` described ``control-ir.md`` ↔ ``OP_KIND_MODEL_MAP``
    as a CI-checked doc/code pair; the CI-checked pair is actually
    ``OP_KIND_MODEL_MAP`` ↔ the ``Op`` union (code ↔ code, #1983) — every
    test/script that names ``control-ir.md`` only quotes the convention in a
    docstring, and none opens the file. So this is the first gate in the repo
    that reads a doc as a file and asserts it against a code declaration.

    The doc block is written by hand and CHECKED here, rather than generated at
    build time: adding a generation stage to the docs build costs more than it
    saves, and reading-and-comparing is the form ``#3126``'s anchor gate already
    established here. Edit either side alone → RED.
    """
    documented = _documented_kinds()
    assert documented == sorted(documented), (
        "the audit-event kind enumeration is not sorted; keep it in one "
        "deterministic order so a diff shows the kind that changed"
    )
    missing = sorted(AUDIT_EVENT_KINDS - set(documented))
    extra = sorted(set(documented) - AUDIT_EVENT_KINDS)
    assert not missing and not extra, (
        "docs/reference/runtime/events.md's kind enumeration has drifted from "
        "AUDIT_EVENT_KINDS — it is what an external consumer enumerates, so a "
        f"stale entry is a handler that never fires. missing={missing} "
        f"extra={extra}"
    )


def test_dynamic_emit_site_classifications_are_from_the_closed_set() -> None:
    """Tier 2: every declared blind spot carries a classification from
    ``DynamicEmitSite``'s documented set and a non-empty reason.

    A registry of holes is only useful if each entry says which KIND of hole it
    is — a forwarder (no new kind), a kind family the census cannot expand, or a
    name collision that is not an audit emit at all. An entry with a blank
    reason is the "code cannot tell forgotten from decided" failure re-entering
    through the registry meant to prevent it.
    """
    for site in DYNAMIC_KIND_EMIT_SITES:
        assert site.classification in DynamicEmitSite.CLASSIFICATIONS, (
            f"{site.module}:{site.function} has an unknown classification "
            f"{site.classification!r}"
        )
        assert site.reason.strip(), f"{site.module}:{site.function} has no reason"
