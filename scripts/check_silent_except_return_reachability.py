#!/usr/bin/env python3
"""#5990 stage 2-2 — a gate over the OTHER silent-except axis: not "does the
handler body itself report anything" (`silent_except_ratchet.py`'s own axis,
`src/reyn/interfaces/` only, `Exception`/`BaseException` only), but **"does
the value the handler assigns become this function's own return value"** —
an `except` block (ANY type — a bare `except:`, a narrow `ImportError`, a
tuple, `Exception`/`BaseException` itself; see the algorithm section for why
this axis has no type restriction at all) whose body is ASSIGN-ONLY (every
statement a plain `ast.Assign`, nothing else — no log call, no raise,
nothing) is not necessarily silent in the sense the OTHER gate measures, but
if the name(s) it binds flow into a `return` in the SAME enclosing function,
an unexpected exception silently became **fabricated output data** — the
caller receives a value indistinguishable from a real one. Scope:
`src/reyn/**`, the WHOLE tree (this axis is not `interfaces/`-specific —
backlog-watcher's own #5990 census found it distributed across
`runtime/`/`llm/`/`core/`/`mcp/`/`tools/` etc., heavier OUTSIDE `interfaces/`
than inside it).

## Why this is a DIFFERENT gate, not an extension of `silent_except_ratchet.py`

lead-coder's own correction (#5990, 2026-09-10): the two axes measure
different properties and can each flag a site the other does not — a
handler can report visibly (passing the OTHER gate) while still fabricating
a return value (failing THIS one), or vice versa. Conflating them into one
gate's population would blur which property a given red line is actually
about. `silent_except_ratchet.py` is untouched by this file.

## The algorithm (backlog-watcher's own #5990 census, operationalized)

For each `except` handler — ANY caught type(s), no restriction (backlog-
watcher's own census: narrower than the OTHER gate's own
`Exception`/`BaseException`-only population, e.g. it is what makes the
"expected platform `ImportError`" category below have sites to mark at
all) — whose body is assign-only:

1. Collect the name(s) its `Assign` statement(s) bind (the LHS targets —
   `canonical = repr(payload)` binds ``canonical``; a tuple target like
   `stdout_b, stderr_b, _trunc = b"", b"", False` binds all three).
2. Find the nearest enclosing `def`/`async def` (a MODULE-level handler —
   no enclosing function — is trivially correct-side: there is no `return`
   to reach, per backlog-watcher's own census).
3. Within that function's own body ONLY (never descending into a NESTED
   `def`/`async def`/`lambda` — a bound name shadowed or re-purposed inside
   an inner function is a different scope entirely), collect every
   `Return` node's value expression.
4. If ANY such expression contains a `Name` reference to one of the bound
   names ANYWHERE inside it (not just as the return's direct value — e.g.
   `return SandboxResult(stdout=stdout_b, ...)` counts, matching the #5990
   census's own "drain sites" finding) — VIOLATION-SIDE: the handler's
   fabricated value reaches a caller as this function's own return.

Deliberately narrower than the axis's own EARLIER, broader phrasing
(#5990 §30: "reaches a `return` (or `yield`, or a store to an object that
outlives the function)") — the census that actually produced the 34/20
site counts this gate's population must match used the Return-only
version; widening later would silently change the population every commit
onward, exactly the drift #5990's own memory pin about measurement carrying
its own tree names.

## Marker — the population minus a REASON, not a numeric grandfather

lead-coder's explicit rejection of BOTH a numeric baseline ("N sites
allowed" lets someone fix an old one and add a new one for free — the
count never moves) AND a hand-maintained allowlist (a second table this
repo's own "derive, never maintain" discipline already forbids) for THIS
gate. Population = every violation-side site found by the algorithm above,
MINUS every site carrying a valid marker:

    except Exception:
        # SILENT-EXCEPT-RETURN-OK: <category> -- <specific reason>
        canonical = repr(payload)

The marker comment must appear somewhere between the `except` line and the
handler's own last line (inclusive) — same physical block the violation
was found in, so a marker cannot silently "cover" an unrelated site
elsewhere in the file. `.strip()`-matched (unlike `check_subprocess_reyn_
pin.py`'s own `# EXEMPT:`, which is column-0-anchored) so it reads naturally
at the handler's own indentation.

`<category>` must be one of the FIVE reasons #6057's own PR body already
used, verbatim, when it read #5990's original 34-site population by hand
and judged 20 of them out-of-class (lead-coder's explicit instruction: this
IS the vocabulary, not a fresh invention):

  - `internal-hash-fallback` — a digest/hash computation's own fallback
    when the canonical form can't be produced (e.g. an unhashable value);
    the fallback is itself a valid, if less ideal, hash input.
  - `expected-import-error` — a platform-specific optional backend's
    absence (e.g. Linux-only `landlock`/`seccomp` modules unavailable on
    macOS/Windows) — the exception IS the expected signal, not a failure.
  - `external-content-caller-cannot-act` — parsing untrusted third-party
    content (an HTTP response body, a subprocess's own stdout) where no
    operator-facing action exists for a parse failure beyond "degrade the
    displayed content."
  - `reported-elsewhere` — the failure is already visible through a
    DIFFERENT channel in the same call graph: caught and returned/stored
    as data one level up, an audit-event already emitted at the true
    origin, a retry loop whose own eventual give-up path reports, or an
    adjacent branch in the SAME function already logs it.
  - `low-stakes-display-value` — a documented, explicitly non-authoritative
    display number (a progress percentage, an estimate) where silent
    degradation to a placeholder is the intentionally chosen UX, not an
    oversight.

Both directions are gate failures (lead-coder: "「意図的」で全部通る marker
は gate ではなく署名欄です" — a marker that always passes regardless of its
own content is a signature line, not a gate):

  1. A violation-side site with NO marker at all — a new, unreviewed one.
  2. A violation-side site WITH a marker whose category is not one of the
     five above (a typo, an invented reason) — the marker exists but does
     not actually justify anything a future reader could act on.

## What this gate does NOT attempt

Same disclosed limits as `silent_except_ratchet.py`'s own AST-only
approach: a marker's mere PRESENCE in the handler's line range is not
proof the judgement behind it was correct, only that someone made one and
wrote it down where the next reader (and this gate) can see it. This gate
enforces "reviewed and categorized", not "correctly reviewed" — the same
"suspected, not confirmed" posture every AST-syntax gate in this repo
already carries.

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCOPE = "src/reyn"

#: #6057's own PR body vocabulary (lead-coder's explicit instruction: reuse
#: it verbatim, not invent a new one) — see module docstring for each
#: category's own meaning.
_VOCABULARY = frozenset({
    "internal-hash-fallback",
    "expected-import-error",
    "external-content-caller-cannot-act",
    "reported-elsewhere",
    "low-stakes-display-value",
})

#: `.strip()`-matched against one physical line at a time (never
#: `.search()` over a whole file) — same anti-spoofing anchor
#: `scripts/_markers.py`'s `fixed_comment_marker` establishes for `#
#: EXEMPT:`, adapted to tolerate normal code indentation (this marker lives
#: INSIDE an indented handler body, unlike `# EXEMPT:`'s own column-0 file
#: level placement) and to capture a REASON as a second group, not just a
#: category name.
_MARKER_RE = re.compile(r"^#\s*SILENT-EXCEPT-RETURN-OK:\s*([a-z0-9][a-z0-9-]*)\s*--\s*(\S.*)$")
#: The bare directive, used only to distinguish "no marker attempted at
#: all" from "a marker was attempted but is malformed/mis-categorized" —
#: two different violation messages, not the same one.
_DIRECTIVE_RE = re.compile(r"^#\s*SILENT-EXCEPT-RETURN-OK:")


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `src/reyn/` — `git ls-files`, same
    population source and single-`*` pathspec (git's default glob is NOT
    `FNM_PATHNAME`-scoped, so `*` already crosses `/`) every other
    whole-tree ratchet in this repo uses; no hand-maintained exclusion
    list."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _assign_target_names(target: ast.expr) -> "set[str]":
    """Every `Name` bound by one `Assign` target — a plain name, or the
    elements of a `Tuple`/`List` target (`a, b = ...`)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: "set[str]" = set()
        for elt in target.elts:
            names |= _assign_target_names(elt)
        return names
    return set()


def _handler_bound_names(node: ast.ExceptHandler) -> "frozenset[str] | None":
    """The names this handler's body binds, iff the body is ASSIGN-ONLY —
    every statement a plain `ast.Assign` (an `AugAssign`/`AnnAssign`, a
    `raise`, a call, an `if`, ANYTHING else disqualifies the whole body,
    matching the census's own "assign-only" predicate exactly). Returns
    `None` for a non-assign-only body (not this gate's population at
    all — it either reports visibly or has real control flow, either of
    which is a different question `silent_except_ratchet.py`'s own axis
    already covers, not this one)."""
    names: "set[str]" = set()
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign):
            return None
        for target in stmt.targets:
            names |= _assign_target_names(target)
    return frozenset(names) if names else None


def _collect_returns_within_function(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[ast.Return]":
    """Every `Return` node inside *func*'s own body — NEVER descending
    into a nested `def`/`async def`/`lambda` (a name bound in the outer
    function is a different, unrelated binding inside an inner scope,
    even if it shares the same spelling)."""
    returns: "list[ast.Return]" = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                returns.append(child)
            walk(child)

    walk(func)
    return returns


def _return_references_any(ret: ast.Return, names: "frozenset[str]") -> bool:
    """True iff *ret*'s own value expression contains a `Name` reference
    to any of *names* ANYWHERE inside it — not just as the direct return
    value (`return canonical`) but also nested (`return SandboxResult
    (stdout=stdout_b, ...)`), matching the #5990 census's own "drain
    sites" finding (the bound names flow into a constructor call, not a
    bare return of the name itself)."""
    if ret.value is None:
        return False
    for node in ast.walk(ret.value):
        if isinstance(node, ast.Name) and node.id in names:
            return True
    return False


class _Site:
    """One violation-side `except` handler this gate's axis found —
    line range for marker association, plus enough identity for a
    human-readable report."""

    __slots__ = ("path", "lineno", "end_lineno")

    def __init__(self, path: Path, lineno: int, end_lineno: int) -> None:
        self.path = path
        self.lineno = lineno
        self.end_lineno = end_lineno


def _handler_end_lineno(node: ast.ExceptHandler) -> int:
    """The last line number touched by *node*'s own body — the upper
    bound of where a marker for this SPECIFIC site may live (so a marker
    cannot silently cover an unrelated handler elsewhere in the file)."""
    end = node.lineno
    for child in ast.walk(node):
        child_end = getattr(child, "end_lineno", None)
        if child_end is not None and child_end > end:
            end = child_end
    return end


def find_violation_sites(path: Path) -> "list[_Site]":
    """Every violation-side site in *path*: an assign-only `except
    Exception`/`except BaseException` handler whose bound name(s) reach a
    `return` in the enclosing function. Raises `SyntaxError` (propagated,
    never swallowed) on a parse failure — same fail-CLOSED discipline
    `silent_except_ratchet.py` already established (#5990): a population
    scan that silently under-counts on its OWN failure is the exact defect
    this whole gate family exists to prevent, recurring one layer up."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    sites: "list[_Site]" = []
    func_stack: "list[ast.FunctionDef | ast.AsyncFunctionDef]" = []

    def visit(node: ast.AST) -> None:
        is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_func:
            func_stack.append(node)  # type: ignore[arg-type]
        if isinstance(node, ast.ExceptHandler):
            # #5990 §30's own axis has NO exception-type restriction (any
            # type — `ImportError`, a narrow tuple, even a bare `except:`
            # — counts if the body is assign-only and return-reachable;
            # backlog-watcher's own census explicitly notes this is
            # BROADER than `silent_except_ratchet.py`'s own
            # Exception/BaseException-only population). The "expected
            # platform ImportError" category (sandbox/seccomp backends)
            # only has sites to mark BECAUSE this axis has no such
            # restriction.
            bound = _handler_bound_names(node)
            if bound:
                enclosing = func_stack[-1] if func_stack else None
                if enclosing is not None:
                    returns = _collect_returns_within_function(enclosing)
                    if any(_return_references_any(r, bound) for r in returns):
                        sites.append(_Site(path, node.lineno, _handler_end_lineno(node)))
                # enclosing is None (module-level handler): trivially
                # correct-side, per the census's own finding — no
                # `return` exists to reach.
        for child in ast.iter_child_nodes(node):
            visit(child)
        if is_func:
            func_stack.pop()

    visit(tree)
    return sites


def _site_marker(site: "_Site", lines: "list[str]") -> "tuple[str, str] | Exception | None":
    """The marker covering *site*, read from *lines* (1-indexed via
    `site.lineno`/`site.end_lineno`, so `lines[i-1]`).

    Returns:
      - `(category, reason)` — a valid, vocabulary-matching marker.
      - a `ValueError` instance (not raised) — a `# SILENT-EXCEPT-RETURN-
        OK:` directive is present but malformed or names a category
        outside the vocabulary. Returned rather than raised so the caller
        can report EVERY such site in one pass, not stop at the first.
      - `None` — no marker attempted at all in this site's own line
        range."""
    for lineno in range(site.lineno, site.end_lineno + 1):
        if lineno - 1 >= len(lines):
            break
        stripped = lines[lineno - 1].strip()
        full_match = _MARKER_RE.match(stripped)
        if full_match:
            category, reason = full_match.group(1), full_match.group(2)
            if category not in _VOCABULARY:
                return ValueError(
                    f"line {lineno}: marker category {category!r} is not in "
                    f"the vocabulary ({sorted(_VOCABULARY)})"
                )
            return (category, reason)
        if _DIRECTIVE_RE.match(stripped):
            return ValueError(
                f"line {lineno}: `# SILENT-EXCEPT-RETURN-OK:` marker present "
                f"but malformed — expected `# SILENT-EXCEPT-RETURN-OK: "
                f"<category> -- <reason>`"
            )
    return None


def measured(root: Path = _ROOT) -> "tuple[list[tuple[Path, int, 'Exception | None']], int]":
    """`(unresolved_sites, scanned_file_count)`: `unresolved_sites` is
    `(path, lineno, problem)` for every violation-side site that is either
    unmarked (`problem is None`) or mis-marked (`problem` is the
    `ValueError` explaining why)."""
    files = _iter_scan_files(root)
    unresolved: "list[tuple[Path, int, Exception | None]]" = []
    for path in files:
        sites = find_violation_sites(path)
        if not sites:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for site in sites:
            outcome = _site_marker(site, lines)
            if outcome is None or isinstance(outcome, Exception):
                unresolved.append((path, site.lineno, outcome))
    return (unresolved, len(files))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    return parser


def main(argv: "list[str] | None" = None) -> int:
    build_parser().parse_args(argv)

    try:
        unresolved, scanned = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"silent-except-return-reachability gate FAILED: could not scan "
            f"the population ({exc}). Fails CLOSED on a scan error rather "
            f"than silently under-counting.",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(
            f"silent-except-return-reachability gate FAILED: the scan found "
            f"0 files under {_SCOPE}/ — a scanner failure, not a clean "
            f"population; `git ls-files` returned nothing.",
            file=sys.stderr,
        )
        return 1

    if unresolved:
        print("silent-except-return-reachability gate FAILED:\n", file=sys.stderr)
        for path, lineno, problem in sorted(unresolved, key=lambda t: (str(t[0]), t[1])):
            rel = path.relative_to(_ROOT)
            if problem is None:
                print(f"  {rel}:{lineno}: no marker found", file=sys.stderr)
            else:
                print(f"  {rel}:{lineno}: {problem}", file=sys.stderr)
        print(
            "\nAn `except` block here is "
            "assign-only (no log, no raise, nothing but an assignment) AND "
            "the name(s) it assigns reach this function's own `return` — an "
            "unexpected exception can silently become fabricated output "
            "data. Either fix it (report the failure, or stop returning the "
            "fabricated value), or — if this IS a reviewed, intentional "
            "shape — add a marker in the handler's own line range:\n"
            "  # SILENT-EXCEPT-RETURN-OK: <category> -- <specific reason>\n"
            "<category> must be one of: " + ", ".join(sorted(_VOCABULARY)) + ".\n"
            "See scripts/check_silent_except_return_reachability.py's own "
            "module docstring for what each category means and why no "
            "'intentional' catch-all is accepted.",
            file=sys.stderr,
        )
        return 1

    print(
        f"silent-except-return-reachability gate OK: every violation-side "
        f"site under {_SCOPE}/ carries a valid marker ({scanned} file(s) "
        f"scanned)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
