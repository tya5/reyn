#!/usr/bin/env python3
"""#6112 — every ``tree-sitter-bash`` node kind a LIVE-derived, real corpus
surfaces must be in EXACTLY ONE of ``exec_plan._ALLOWED_NODE_KINDS`` or this
module's own :data:`_DELIBERATELY_NOT_ALLOWED_NODE_KINDS`.

## Why this exists (split out of #6110, architect's co-vet)

#6110 derived :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` from a
REAL corpus (``.github/workflows/**`` ``run:`` lines, ``docs/**`` fenced
shell code blocks, ``scripts/**`` subprocess calls that build a shell
command line) — 1,143 unique commands, a 29-kind union, 1 kind added
(``number``), 16 kinds flagged "not added" with per-kind reasoning. That
reasoning lived ONLY as prose in
``tests/security/test_6108_exec_plan_allowlist_corpus.py``'s module
docstring, and the derivation itself ran once, by a human, against a
corpus that was then committed as a frozen 25-command accept-side witness
(:data:`tests.security.test_6108_exec_plan_allowlist_corpus._REAL_CORPUS`)
— never re-derived.

architect's co-vet measurement on #6110 (issue #6112, verbatim): of the 16
flagged kinds, only 2 (``variable_assignment``, ``file_descriptor``) have
ever actually been OBSERVED in real execution. The other 14's "reason not
to add" is *read as correct*, not *machine-checked*. Worse: nothing stops
a 17th, still-unclassified kind from entering the corpus tomorrow (a new
doc's shell example, a new workflow step) and going completely unnoticed
until ``exec`` rejects a real command at runtime — the exact #6108
failure mode, rediscovered the same way.

## What this gate does

1. Walks the SAME 3 corpus sources #6110 used, LIVE, at gate-run time —
   never a committed snapshot (see :func:`derive_live_corpus`).
2. Parses every extracted command line through the SAME
   ``tree_sitter.Parser``/``tree_sitter_bash`` machinery
   :mod:`reyn.security.exec_plan`/:mod:`reyn.security.bash_node_kinds`
   already use (:func:`reyn.security.exec_plan._load_bash_parser`,
   :func:`reyn.security.exec_plan._walk_nodes` — imported directly, never
   a second parser instantiation path) and takes the UNION of every named
   node kind that appears in a tree with no parse error (a tree WITH a
   parse error contributes nothing to the union — same exclusion #6110's
   own derivation made for markdown prose / un-substituted ``${{ }}``
   living inside a fenced block, real corpus noise, not evidence about
   what a real command needs).
3. Every kind in that union must be in EXACTLY ONE of
   :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` or this module's
   own :data:`_DELIBERATELY_NOT_ALLOWED_NODE_KINDS`. A kind in NEITHER is
   a RED gate — the fail-closed hole this issue closes.
4. A live derivation that extracts ZERO commands is ALSO a RED gate, not
   a silent "nothing to check" pass — guards against the extraction
   itself silently breaking (a changed doc format, a moved workflow
   directory) reading as "no new kinds" when it is actually "found
   nothing at all."

## What this gate deliberately does NOT do

- Does not change :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` or
  any ``exec_plan.py`` classification behaviour — this issue is about
  making the population VISIBLE to CI, not about widening/narrowing what
  is accepted.
- Does not commit the full corpus (#6110's own 1,143-line set) — this
  gate re-derives it live, every run, from the real repo tree. #6110's
  own ``_REAL_CORPUS`` (a curated 25-command accept-side regression
  witness in ``tests/security/test_6108_exec_plan_allowlist_corpus.py``)
  stays exactly as-is — that file serves a DIFFERENT role and is out of
  scope here.
- Does not judge WHETHER a "not added" reason is still correct — only a
  human reviewing a PR that touches
  :data:`_DELIBERATELY_NOT_ALLOWED_NODE_KINDS` can do that. This gate's
  only mechanical claim: every live-derived kind is AT LEAST accounted
  for, in one of the two sets, with a reason string attached on the
  "not added" side.

Same skeleton as ``scripts/bash_node_kind_count_ratchet.py``/
``scripts/flat_tests_ratchet.py``: a gate script, not a ``tests/`` file —
a single new line in a doc's shell example must not make an unrelated
PR's ``pytest`` run red; it should surface as THIS dedicated CI gate
script failing instead (the same reasoning #6086 used to move the
node-kind COUNT ratchet from ``tests/`` to ``scripts/``).

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# #6110's own per-kind reasoning (PR #6110's merged diff / the module
# docstring of tests/security/test_6108_exec_plan_allowlist_corpus.py),
# moved from PROSE into a machine-readable value per lead-coder's explicit
# instruction (#6112) — the exact wording already established there, not
# re-derived or paraphrased. Each entry is "why NOT added", not "why
# dangerous in the abstract" — a future widen still needs its own
# deliberate review, this just says what it has to overturn first.
_DELIBERATELY_NOT_ALLOWED_NODE_KINDS: "dict[str, str]" = {
    "command_substitution": (
        "one of the three constructs #5987 stage 2's own module docstring "
        "names EXPLICITLY as meant to stay rejected (\"command "
        "substitution... subshell grouping... process substitution... "
        "heredoc\"). Accepting this would reopen the exact parse/execute "
        "semantic gap #5838's whole parser design exists to close (a "
        "nested command this parser would never see)."
    ),
    "subshell": (
        "one of the three constructs #5987 stage 2's own module docstring "
        "names EXPLICITLY as meant to stay rejected (subshell grouping, "
        "\"(...)\" ). Same reopened-gap reasoning as command_substitution."
    ),
    "process_substitution": (
        "one of the three constructs #5987 stage 2's own module docstring "
        "names EXPLICITLY as meant to stay rejected (process substitution, "
        "\"<(...)\"/\">(...)\" ). Same reopened-gap reasoning as "
        "command_substitution."
    ),
    "simple_expansion": (
        "part of the `$`-expansion family (`$VAR`, `${VAR}`, `$?`, `$!`, "
        "`${VAR#pattern}`) -- the STRUCTURAL mechanism #5987 stage 2 "
        "relies on to reject one of its 4 closed bypass shapes (a real "
        "value substituted at execution time this parser can never see "
        "literally -- test_variable_glob_or_home_expansion_is_rejected). "
        "Widening any of this family would silently re-open that bypass."
    ),
    "expansion": (
        "part of the `$`-expansion family -- same structural-bypass "
        "reasoning as simple_expansion."
    ),
    "special_variable_name": (
        "part of the `$`-expansion family -- same structural-bypass "
        "reasoning as simple_expansion."
    ),
    "variable_name": (
        "part of the `$`-expansion family -- same structural-bypass "
        "reasoning as simple_expansion."
    ),
    "regex": (
        "part of the `$`-expansion family -- same structural-bypass "
        "reasoning as simple_expansion."
    ),
    "concatenation": (
        "the STRUCTURAL mechanism catching brace expansion (`{a,b}`) and "
        "non-simple `$`-adjacency -- the second of #5987's 4 closed "
        "bypass shapes (test_brace_expansion_is_rejected)."
    ),
    "variable_assignment": (
        "adjacent to the leading `NAME=value`-assignment bypass class "
        "(#5838 BLOCKING, test_leading_environment_assignment_is_rejected): "
        "a standalone `FOO=bar` currently fails the node-kind gate itself "
        "before the parser's own positional regex re-check even runs -- "
        "allowing this kind would remove that first line of defense, "
        "leaving only the regex-based re-check as a backstop instead of "
        "two independent mechanisms."
    ),
    "declaration_command": (
        "shares variable_assignment's paren/assignment-adjacent shape "
        "(`export FOO=bar`) -- flagged, not added, needs a closer "
        "deliberate look if ever requested, not a corpus-driven "
        "auto-widen."
    ),
    "array": (
        "shares variable_assignment's paren/assignment-adjacent shape "
        "(`FOO=(...)`) -- flagged, not added, same reasoning as "
        "declaration_command."
    ),
    "herestring_redirect": (
        "a redirect SHAPE (`<<<`) not in "
        "exec_plan._REDIRECT_OPS_SINGLE/_REDIRECT_OPS_DOUBLE at all (only "
        "`> >> <` are supported) -- a new data-flow shape this parser has "
        "never reasoned about."
    ),
    "file_descriptor": (
        "fd-numbered redirects (`2>...`) -- ExecPlanRejected's own "
        "docstring already DISCLOSES fd-numbered redirects as an "
        "intentional v1 narrowing, not a bug "
        "(test_redirect_fd_duplication_stays_rejected pins this). "
        "Widening needs a deliberate redirect-handling change, not a "
        "silent node-kind add."
    ),
    "test_command": (
        "traced to a single corpus occurrence (`[--resume]`, doc CLI "
        "usage-syntax text, not a real `[ ... ]` shell test invocation) -- "
        "too thin a signal to trust as \"a real command needs this.\""
    ),
    "unary_expression": (
        "same single-occurrence doc-usage-syntax noise as test_command -- "
        "too thin a signal to trust."
    ),
}

# The 3 extracted-text sources -- lead-coder's explicit list (issue #6112 /
# PR #6110), walked LIVE at gate-run time, never from a committed snapshot.
_WORKFLOWS_DIR = "workflows"
_FENCE_RE = re.compile(r"```(bash|sh|shell)\n(.*?)```", re.DOTALL)
_SH_DASH_C_RE = re.compile(
    r"""\[\s*['"](?:sh|bash)['"]\s*,\s*['"]-c['"]\s*,\s*['"]((?:[^'"\\]|\\.)*)['"]""",
)


def extract_workflow_command_lines(root: Path) -> "list[tuple[str, str]]":
    """Every ``run:`` step body under ``.github/workflows/**``, each
    non-empty, non-comment line treated as its own parse unit (#6110's own
    method: a leading ``$ `` shell-prompt marker is not stripped here
    since workflow ``run:`` bodies do not carry one). Returns
    ``[(command_text, source_path), ...]``; *root* is a real parameter (not
    a hardcoded module constant) so this is testable against a throwaway
    fixture tree, not only the live repo."""
    import yaml

    out: "list[tuple[str, str]]" = []
    workflows_dir = root / ".github" / _WORKFLOWS_DIR
    if not workflows_dir.is_dir():
        return out
    for path in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- a malformed workflow contributes nothing, not a gate crash
            continue
        rel = str(path.relative_to(root))
        for run_body in _walk_run_strings(doc):
            for line in run_body.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                out.append((line, rel))
    return out


def _walk_run_strings(node: object) -> "list[str]":
    """Every string value of a ``run:`` key anywhere in a parsed workflow
    YAML document, at any nesting depth (step bodies live under
    ``jobs.<job>.steps[].run``, but this walks structurally rather than
    assuming that exact shape, so a workflow composed differently is still
    covered)."""
    out: "list[str]" = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_walk_run_strings(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_run_strings(item))
    return out


def extract_doc_command_lines(root: Path) -> "list[tuple[str, str]]":
    """Every fenced ```bash/```sh/```shell code block under ``docs/**``,
    each non-empty, non-comment line, with a leading ``$ `` shell-prompt
    marker stripped if present (#6110's own method)."""
    out: "list[tuple[str, str]]" = []
    docs_dir = root / "docs"
    if not docs_dir.is_dir():
        return out
    for path in sorted(docs_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(root))
        for match in _FENCE_RE.finditer(text):
            block = match.group(2)
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("$ "):
                    line = line[2:].strip()
                if not line or line.startswith("#"):
                    continue
                out.append((line, rel))
    return out


def extract_script_command_lines(root: Path) -> "list[tuple[str, str]]":
    """Every ``scripts/**/*.py`` ``subprocess.run``/``Popen``/``call``/
    ``check_call``/``check_output`` call whose arguments build an actual
    shell command line (#6110's own method) -- a plain string-literal
    first argument, or a ``["sh"/"bash", "-c", <cmd>]``-shaped list whose
    ``<cmd>`` is either a literal or a same-module name assigned a
    literal. PLUS a raw-text regex scan for the same ``["sh"/"bash",
    "-c", ...]`` shape living inside a STRING CONSTANT that itself holds
    literal Python/shell source (the AST only ever sees the OUTER string;
    #6110's own derivation needed this exact fallback to find
    ``scripts/sandbox_seccomp_x86_64_live_smoke.py``'s ``nested_pipe_code``
    constant)."""
    out: "list[tuple[str, str]]" = []
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return out
    for path in sorted(scripts_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(root))
        out.extend((cmd, rel) for cmd in _extract_ast_shell_commands(text))
        out.extend((m.group(1), rel) for m in _SH_DASH_C_RE.finditer(text))
    return out


def _string_const(node: object) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    return name in {"run", "Popen", "call", "check_call", "check_output"}


def _extract_ast_shell_commands(text: str) -> "list[str]":
    """AST-based half of :func:`extract_script_command_lines` — a real
    ``subprocess.*`` call in *text*, either with a literal command-line
    string as its first argument, or a ``["sh"/"bash", "-c", <cmd>]``
    list whose ``<cmd>`` resolves to a literal (inline, or via a
    same-module ``NAME = "<literal>"`` assignment)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    assigns: "dict[str, str]" = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            value = _string_const(node.value)
            if value is not None:
                assigns[node.targets[0].id] = value

    out: "list[str]" = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_subprocess_call(node)):
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        literal = _string_const(arg0)
        if literal is not None:
            out.append(literal)
            continue
        if isinstance(arg0, (ast.List, ast.Tuple)) and len(arg0.elts) >= 3:
            shell_name = _string_const(arg0.elts[0])
            flag = _string_const(arg0.elts[1])
            if shell_name in ("sh", "bash") and flag == "-c":
                cmd = _string_const(arg0.elts[2])
                if cmd is None and isinstance(arg0.elts[2], ast.Name):
                    cmd = assigns.get(arg0.elts[2].id)
                if cmd is not None:
                    out.append(cmd)
    return out


def derive_live_corpus(root: Path) -> "list[tuple[str, str]]":
    """The union of all 3 corpus sources, deduplicated by command text
    (first source wins for a duplicate, matching #6110's own dedup
    description) -- walked LIVE against *root*, never a committed
    snapshot. An empty result is a real, reportable condition
    (:func:`main` treats it as a gate failure), not this function's
    concern to guard against."""
    seen: "dict[str, str]" = {}
    for text, source in (
        extract_workflow_command_lines(root)
        + extract_doc_command_lines(root)
        + extract_script_command_lines(root)
    ):
        seen.setdefault(text, source)
    return list(seen.items())


def derive_node_kind_union(commands: "list[str]") -> "frozenset[str]":
    """Parses every *commands* entry with the SAME
    ``tree_sitter.Parser``/``tree_sitter_bash`` machinery
    :mod:`reyn.security.exec_plan` already uses
    (:func:`reyn.security.exec_plan._load_bash_parser`) and returns the
    UNION of every NAMED node kind across every tree with NO parse error
    -- a tree WITH a parse error contributes nothing (#6110's own
    exclusion: markdown prose / un-substituted ``${{ }}`` inside a fenced
    block is real corpus noise, not evidence about what a real command
    needs). Imported lazily (not at module scope) so ``--help`` and
    argument-parsing errors do not require ``reyn``/``tree-sitter`` to
    already be importable."""
    from reyn.security.exec_plan import _load_bash_parser, _walk_nodes

    union: "set[str]" = set()
    for text in commands:
        parser = _load_bash_parser()
        tree = parser.parse(text.encode("utf-8", errors="surrogateescape"))
        if tree.root_node.has_error:
            continue
        for node in _walk_nodes(tree.root_node):
            if node.is_named:
                union.add(node.type)
    return frozenset(union)


def classify_union(
    union: "frozenset[str]",
) -> "tuple[frozenset[str], frozenset[str], frozenset[str]]":
    """Splits *union* into (allowed-hit, flagged-hit, unclassified) against
    :data:`~reyn.security.exec_plan._ALLOWED_NODE_KINDS` and this module's
    own :data:`_DELIBERATELY_NOT_ALLOWED_NODE_KINDS` -- the third element
    is the gate's actual fail condition: a kind neither set accounts for.
    Imported lazily, same reason as :func:`derive_node_kind_union`."""
    from reyn.security.exec_plan import _ALLOWED_NODE_KINDS

    flagged = frozenset(_DELIBERATELY_NOT_ALLOWED_NODE_KINDS)
    allowed_hit = union & _ALLOWED_NODE_KINDS
    flagged_hit = union & flagged
    unclassified = union - _ALLOWED_NODE_KINDS - flagged
    return allowed_hit, flagged_hit, unclassified


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    return parser


def main(argv: "list[str] | None" = None) -> int:
    build_parser().parse_args(argv)

    corpus = derive_live_corpus(_ROOT)
    if not corpus:
        print("node-kind-coverage ratchet FAILED:\n", file=sys.stderr)
        print(
            "the live corpus derivation (.github/workflows/** run: lines, "
            "docs/** fenced shell code blocks, scripts/** subprocess shell "
            "calls) found ZERO commands -- this is a gate failure, not a "
            "silent \"nothing to check\" pass, because it is "
            "indistinguishable from the extraction itself silently "
            "breaking (a changed doc format, a moved workflow directory) "
            "from the outside. Fix the extraction in scripts/"
            "node_kind_coverage_ratchet.py, never the exit code.",
            file=sys.stderr,
        )
        return 1

    union = derive_node_kind_union([text for text, _source in corpus])
    _allowed_hit, _flagged_hit, unclassified = classify_union(union)

    if unclassified:
        print("node-kind-coverage ratchet FAILED:\n", file=sys.stderr)
        print(
            f"{len(unclassified)} tree-sitter-bash node kind(s) appear in "
            f"the live-derived corpus ({len(corpus)} unique commands) but "
            "are in NEITHER reyn.security.exec_plan._ALLOWED_NODE_KINDS "
            "NOR this script's own _DELIBERATELY_NOT_ALLOWED_NODE_KINDS:",
            file=sys.stderr,
        )
        by_kind: "dict[str, str]" = {}
        for text, source in corpus:
            for kind in derive_node_kind_union([text]):
                if kind in unclassified and kind not in by_kind:
                    by_kind[kind] = f"{source}: {text!r}"
        for kind in sorted(unclassified):
            example = by_kind.get(kind, "(example not recovered)")
            print(f"  {kind!r} -- first corpus hit: {example}", file=sys.stderr)
        print(
            "\nDecide, deliberately, which side this belongs on: add it to "
            "_ALLOWED_NODE_KINDS in src/reyn/security/exec_plan.py (a real "
            "widening, needs its own measured justification -- see that "
            "module's own docstring) or to this script's own "
            "_DELIBERATELY_NOT_ALLOWED_NODE_KINDS with a reason string "
            "naming what closed bypass or disclosed narrowing it touches. "
            "Never delete the corpus entry that surfaced it to make this "
            "gate quiet again.",
            file=sys.stderr,
        )
        return 1

    print(
        f"node-kind-coverage ratchet OK: {len(corpus)} unique live-derived "
        f"command(s), {len(union)} distinct node kind(s), all classified "
        f"({len(_allowed_hit)} allowed, {len(_flagged_hit)} deliberately "
        "not allowed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
