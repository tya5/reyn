"""Tier 1: #5685 (architect ruling, lead-coder scope decision) — every
``getattr(<host receiver>, "<literal>", ...)`` call in ``router_loop.py``
must name an attribute that actually has a real definition somewhere in
``src/``.

## Why this exists

``#5684`` renamed ``peek_mid_turn_injection``'s return shape (dict -> list)
but kept the singular name, and ``router_loop.py:2295`` resolves the
callback by STRING (``getattr(host, "peek_mid_turn_injection", None)``). A
rename that touches the literal but not the implementation — or the
implementation but not the literal — degrades to a silent, permanent no-op
(``getattr(..., None)``): no ``AttributeError``, no red anywhere, the host
callback simply stops firing. #5685 filed this hazard; this gate closes the
CLASS (any future literal/implementation drift on this seam), not just the
one instance the rename (landed in this same PR) fixes.

## Why not a ``Protocol`` (architect's own ruling, rejected option ③)

``getattr(host, "x", None)`` is INTENTIONALLY optional — each host
implements a different subset (a phase host implements far fewer members
than the production ``RouterHostAdapter``), and ``None`` IS the "this host
doesn't do that" path. A ``Protocol`` with every member required would
break that premise (a legitimate partial host would fail to type-check); a
``Protocol`` with every member ``Optional`` catches nothing (a renamed-away
member still type-checks). Neither closes the silence #5685 names — a gate
that cross-references the literal against a real definition does.

## Scope: every receiver that POINTS AT ``host`` (lead-coder's own ruling)

Not just a bare ``getattr(host, ...)`` — ``router_loop.py`` also calls
``getattr(self.host, ...)`` throughout (``self.host = host`` at
``__init__``, and several functions do ``host = self.host`` as a local
alias before calling ``getattr(host, ...)`` — that local rebinding is
already covered by the bare-``host``-Name branch below, so ONLY the two
receiver *shapes* need naming: ``host`` the ``Name`` (parameter OR a local
re-bound from ``self.host`` — indistinguishable to a static AST walk, and
correctly so: both ultimately name the SAME object) and ``self.host`` the
``Attribute``. Measured (this file's own gate, not assumed): no OTHER
host-aliasing spelling (``self._host``, a renamed local, etc.) exists in
``router_loop.py`` today — grep confirmed zero hits for those shapes.
Limiting the scope to the bare ``host`` receiver alone (this gate's first
draft) undercounted the real population by MORE than half (26 measured ->
57 actual) — narrowing by receiver *spelling* rather than by the gate's
own stated reason ("literal and definition drift with no red") was the
exact mistake lead-coder's own #5685 comment named and reversed.

## Needle correctness (architect's own warning, hit twice this issue)

``git grep -E`` (POSIX ERE) silently no-matches ``\\b``/``\\s`` — architect's
own first census undercounted by 24 for exactly this reason. A SEPARATE
hazard, found while building this gate: a single-line-anchored pattern
also misses a ``getattr(...)`` call whose arguments are split across
multiple lines (``reasoning_continuity_section``/``commit_mid_turn_
injection``/``get_universal_wrappers_enabled`` were invisible to that
shape). This gate uses ``ast`` throughout for both extraction passes —
neither hazard applies to a real parse tree — and
:func:`test_the_needle_finds_every_real_literal_a_hand_audit_confirms`
below pins that the extractor actually reaches a SPECIFIC verified-by-hand
set of literals, not merely "found something".

## A known, accepted false-negative class (lead-coder co-vet, BLOCKING)

:func:`build_src_definition_index` scans ALL of ``src/`` for a matching
NAME — it cannot tell "the actual host object implements this" from "some
UNRELATED class elsewhere in the tree happens to share this identifier". A
real audit found 6 of the 55 non-allowlisted literals this way: e.g.
``workspace`` is "defined" because ``core/op_runtime/context.py`` has an
unrelated ``workspace: "Workspace"`` field, and ``sandbox_config`` because
``runtime/agent.py``'s ``AgentProfile``-shaped dataclass has its own
unrelated ``sandbox_config`` field — NEITHER has anything to do with
``RouterHostAdapter`` or any other real host. **For those 6 literals, this
gate's direction-2 falsify (rename the IMPLEMENTATION away, literal stays)
does NOT fire** — the unrelated same-named definition keeps the index
entry alive.

This is accepted, not fixed, on purpose: ``host`` is duck-typed by
design (the whole reason option ③ rejected a ``Protocol``), so there is no
static way to ask "does THIS SPECIFIC object implement X" without
re-introducing the tight coupling the design deliberately avoids.
Narrowing the index to a specific class/file would trade this
false-negative for the opposite, worse failure — a false POSITIVE (the
gate blocking a legitimate host that implements the callback somewhere
this narrower scan doesn't look). A conservative, whole-``src/`` index
that occasionally under-flags a name collision is the safer of the two
failure directions for a gate whose entire job is "don't let this seam
break loudly for the OTHER, common case" (direction 1: rename the literal,
forget the impl — still caught for all 57).
:func:`test_a_same_named_unrelated_definition_masks_an_impl_rename`
below fixes this exact boundary as a KNOWN limitation, not a silent gap.
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from tests._support.paths import REPO_ROOT

_ROUTER_LOOP = REPO_ROOT / "src" / "reyn" / "runtime" / "router_loop.py"
_SRC_ROOT = REPO_ROOT / "src" / "reyn"

#: NOT a "defect awaiting disposal" list (lead-coder's own correction,
#: #5685: the first cut of this allowlist misread "no src/ implementation"
#: as "dead branch" — it isn't). This is the list of host seams a doc or
#: docstring EXPLICITLY declares optional/host-provided — each entry's
#: value is WHERE that declaration lives (never an issue number; these
#: entries are permanent, not pending removal). A NEW entry appearing here
#: later is itself the thing to scrutinize: it means an undeclared,
#: unimplemented callback was added with no doc saying so is intentional.
_ALLOWLIST = {
    "record_force_close": (
        "docs/concepts/runtime/safety.ja.md:67 — \"ホストは `record_force_"
        "close` フックを実装すればラップアップを自身のチェックポイントに永続"
        "化できますが、chat ホストは実装していないため、このフックは現状"
        " inert です\" (explicitly documented as an optional hook the "
        "production chat host does not implement)."
    ),
    "compute_memo_key": (
        "src/reyn/core/kernel/sub_loop_memo_key.py's own module docstring — "
        "\"used by the phase memo path (...`PhaseRouterHost.compute_memo_"
        "key`) and as the chat-router fallback when the host provides no "
        "`compute_memo_key`\" (explicitly documented as an optional "
        "phase-host-only seam; this pure-function module IS the "
        "documented fallback for hosts that omit it)."
    ),
}


def _is_host_receiver(node: "ast.expr") -> bool:
    """True for the two receiver SHAPES router_loop.py uses to reach the
    host object — a bare ``host`` Name (a parameter, or a local rebound
    from ``self.host`` — see module docstring) and the ``self.host``
    Attribute directly."""
    if isinstance(node, ast.Name) and node.id == "host":
        return True
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "host"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def find_host_getattr_literals(path: Path) -> "list[str]":
    """AST-walk one file and return every literal name a
    ``getattr(<host receiver>, "<literal>", ...)`` call resolves —
    duplicates included (a caller wanting distinct names should
    ``set()`` the result; kept as a list here so a test can also assert
    on raw occurrence count if ever needed)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    literals: "list[str]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "getattr"):
            continue
        if not node.args or not _is_host_receiver(node.args[0]):
            continue
        if len(node.args) < 2:
            continue
        name_arg = node.args[1]
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            literals.append(name_arg.value)
    return literals


def build_src_definition_index(root: Path) -> "set[str]":
    """One pass over every ``.py`` file under ``root``, collecting every
    identifier DEFINED as a method/property (``def NAME(``, decorator-
    agnostic — ``@property`` doesn't change the ``def`` node's own name),
    a class-body annotated field (``NAME: Type``), or a ``self.NAME =``
    assignment — the three shapes every real attribute in this codebase's
    host classes (``RouterHostAdapter`` et al.) is defined through
    (verified directly against that class: its dataclass-shaped fields use
    ``AnnAssign``, its callback-forwarding methods use ``def``, its
    private-then-property-exposed fields use ``self.NAME =`` for the
    private half and ``def NAME(`` for the public property).

    KNOWN LIMITATION (module docstring's "false-negative class" section
    has the full account): this index is name-only, root-wide — it cannot
    distinguish "the real host implements NAME" from "some unrelated class
    elsewhere under ``root`` happens to define something also called
    NAME". Accepted deliberately (the alternative, narrowing to a specific
    class, trades this for a false-positive risk against ``host``'s
    intentionally duck-typed design) — never silently: see
    :func:`test_a_same_named_unrelated_definition_masks_an_impl_rename`."""
    defined: "set[str]" = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name):
                    defined.add(target.id)
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    defined.add(target.attr)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        defined.add(target.attr)
    return defined


def test_the_needle_finds_every_real_literal_a_hand_audit_confirms():
    """Tier 1: empty-set guard, done as a SPECIFIC-known-set check rather
    than a bare count (#5698's own lesson, cited by lead-coder here too: a
    count pins the CURRENT total for no behavioral reason and is a
    format-pin `test_tier_audit.py` itself rejects). These 4 literals were
    independently hand-verified during this gate's own construction as
    requiring BOTH receiver shapes and BOTH the single-line and
    multi-line call shapes to reach — a walker regressed to bare-``host``-
    only, or to a single-line-anchored extraction, drops at least one of
    them."""
    literals = set(find_host_getattr_literals(_ROUTER_LOOP))
    for needle, why in {
        "peek_mid_turn_injections": "bare host receiver, single-line call",
        "reasoning_continuity_section": "bare host receiver, MULTI-line call",
        "resolver": "self.host receiver, single-line call",
        "compute_memo_key": "self.host receiver, MULTI-line call",
    }.items():
        assert needle in literals, (
            f"the extractor did not find {needle!r} ({why}) — a receiver-"
            f"shape or line-shape regression, not a real removal (that "
            f"literal's own call site was hand-verified present)"
        )


def test_every_literal_has_a_src_definition_or_a_dated_allowlist_entry():
    """Tier 1: the acceptance-item witness (accept side) — every literal
    ``router_loop.py`` resolves against the host, across BOTH receiver
    shapes, has a real ``src/`` definition unless explicitly allowlisted
    with a reason and an issue number."""
    literals = set(find_host_getattr_literals(_ROUTER_LOOP))
    defined = build_src_definition_index(_SRC_ROOT)
    missing = sorted(
        name for name in literals
        if name not in defined and name not in _ALLOWLIST
    )
    assert missing == [], (
        f"getattr(<host receiver>, {missing!r}, ...) in router_loop.py "
        f"names an attribute with NO definition anywhere in src/, and no "
        f"allowlist entry — a rename on one side without the other leaves "
        f"this host callback silently, permanently inert"
    )


def test_allowlist_entries_are_real_gaps_not_stale_exemptions():
    """Tier 1: deny-side companion — an allowlist entry that NO LONGER
    corresponds to a real gap (someone since implemented it) must be
    caught, or the allowlist silently keeps exempting a literal the gate
    could otherwise verify."""
    defined = build_src_definition_index(_SRC_ROOT)
    stale = sorted(name for name in _ALLOWLIST if name in defined)
    assert stale == [], (
        f"{stale!r} now HAS a src/ definition — remove the allowlist "
        f"entry/entries so the gate actually verifies it/them, instead of "
        f"silently exempting a literal that no longer needs it"
    )


def test_renaming_only_the_literal_turns_the_gate_red():
    """Tier 1: falsify pair, direction 1 — a synthetic file whose getattr
    literal names something with NO src definition (the implementation
    side was left on the old name) must be flagged."""
    src = 'x = getattr(host, "renamed_literal_5685_no_impl", None)\n'
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "synthetic.py"
        p.write_text(src, encoding="utf-8")
        literals = set(find_host_getattr_literals(p))
        assert literals == {"renamed_literal_5685_no_impl"}
    defined = build_src_definition_index(_SRC_ROOT)
    assert "renamed_literal_5685_no_impl" not in defined, (
        "sanity: this synthetic name must not collide with anything real"
    )


def test_renaming_only_the_implementation_turns_the_gate_red():
    """Tier 1: falsify pair, direction 2 — the mirror case: the
    IMPLEMENTATION was renamed but the getattr literal (in a synthetic
    ``router_loop.py``-shaped file) still names the OLD attribute, which
    now has no definition either (the real implementation moved to a new
    name the literal never followed)."""
    old_name = "old_before_impl_rename_5685"
    new_name = "new_after_impl_rename_5685"
    src = f'x = getattr(host, "{old_name}", None)\n'
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "caller.py").write_text(src, encoding="utf-8")
        # The "implementation", post-rename, lives under the NEW name only
        # — mirrors a real def-site edit that renamed a method but never
        # touched the getattr string reading its OLD name.
        (tmp_path / "impl.py").write_text(
            f"class Host:\n    def {new_name}(self): ...\n", encoding="utf-8",
        )
        literals = set(find_host_getattr_literals(tmp_path / "caller.py"))
        defined = build_src_definition_index(tmp_path)
        assert literals == {old_name}
        assert new_name in defined
        assert old_name not in defined, (
            "the OLD literal must have no definition once the "
            "implementation renamed away from it — this is the exact "
            "silent-no-op shape #5685 reports"
        )


def test_a_same_named_unrelated_definition_masks_an_impl_rename():
    """Tier 1: fixes the KNOWN false-negative boundary (module docstring's
    own "false-negative class" section, lead-coder co-vet) — direction 2's
    falsify above (:func:`test_renaming_only_the_implementation_turns_
    the_gate_red`) only proves the gate catches a rename when NOTHING else
    in the scanned tree happens to share the old name. It does not.

    This is not a regression to fix — see the module docstring for why a
    conservative, name-only, whole-``src/`` index is the accepted
    trade-off against ``host``'s duck-typed design — but it MUST be a
    documented, tested boundary rather than an unstated weak spot. No
    count is asserted (#5698's own lesson, cited by lead-coder here too):
    this pins the SHAPE of the gap (one synthetic unrelated definition is
    enough to mask a real removal), not how many real literals happen to
    fall in it today — that number is free to drift as the real host
    classes and their unrelated namesakes change, without this test
    needing to track it."""
    shared_name = "totally_unrelated_field_name_5685"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # The "host" seam: a getattr literal with NO real host
        # implementation anywhere (mirrors the real router_loop.py shape).
        (tmp_path / "caller.py").write_text(
            f'x = getattr(host, "{shared_name}", None)\n', encoding="utf-8",
        )
        # An UNRELATED class, in a different file, that happens to declare
        # a field with the exact same name for a completely different
        # purpose — never touched by, or related to, the host seam above
        # (mirrors the real `workspace`/`op_runtime/context.py` and
        # `sandbox_config`/`runtime/agent.py` pairs lead-coder measured).
        (tmp_path / "unrelated.py").write_text(
            f'class SomethingElseEntirely:\n    {shared_name}: str\n',
            encoding="utf-8",
        )
        literals = set(find_host_getattr_literals(tmp_path / "caller.py"))
        defined = build_src_definition_index(tmp_path)
        assert literals == {shared_name}
        assert shared_name in defined, (
            "this IS the known false-negative: an unrelated same-named "
            "field makes the index look satisfied even though no real "
            "host implements this literal — if this ever starts failing, "
            "the index became name+scope-aware and this whole boundary "
            "(and the module docstring section describing it) should be "
            "deleted, not patched around"
        )
