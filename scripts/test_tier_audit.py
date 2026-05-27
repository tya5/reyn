#!/usr/bin/env python3
"""Test policy compliance auditor — Reyn testing.ja.md automated linter.

Detects 6 categories of Tier 4 violations and policy warnings in test files:
  1. Missing Tier docstring (ERROR)
  2. Format pinning via len(...) < N (ERROR)
  3. Private state assertion via obj._attr (ERROR)
  4. MagicMock / AsyncMock / patch usage (ERROR)
  5. Bounded-life test outside tests/scaffold/ (WARNING)
  6. Snapshot/golden test outside tests/scaffold/ (ERROR)

Usage:
    python scripts/test_tier_audit.py tests/
    python scripts/test_tier_audit.py tests/test_foo.py --quiet
    python scripts/test_tier_audit.py --strict tests/
    python scripts/test_tier_audit.py --check format-pinning tests/
    python scripts/test_tier_audit.py --json tests/ | jq .
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

TIER_DOCSTRING_RE = re.compile(r"^Tier [123][abc]?:", re.IGNORECASE)

# len(...) < N  /  len(...) > N  /  len(...) == N  /  len(...) >= N  etc.
# Exemption: len(x) > 0  (simple existence check)
FORMAT_PIN_RE = re.compile(r"len\([^)]+\)\s*[<>=!]+\s*(\d+)")

# Private-state detection is AST-based via ``_find_private_attr_access``
# below. The prior implementation used a regex ``r"\.\w+\._\w+"`` which
# required a preceding dot to anchor and silently missed bare
# ``assert obj._x`` — the most common private-state shape — so violations
# accumulated unnoticed across the test corpus (= sub-discipline 6-round
# trap, memory ``feedback_tier4_private_state_repeat_6_round``). Replaced
# with the AST walk 2026-05-27 (Tier C1 dispatch).


def _find_private_attr_access(node: ast.AST) -> list[ast.Attribute]:
    """Walk *node* and return every ``ast.Attribute`` whose ``attr`` is a
    single-underscore-prefixed name (= private state).

    Dunder names (``__init__``, ``__class__``, ``__name__``) are excluded —
    they're language-level surfaces, not private state in the policy sense.
    Module imports (``from mod._private import X``) and docstring text are
    not represented as ``ast.Attribute`` nodes in the assertion's value
    tree, so AST scoping eliminates those false positives automatically.
    """
    results: list[ast.Attribute] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Attribute):
            continue
        attr = inner.attr
        # Single leading underscore, NOT dunder
        if attr.startswith("_") and not attr.startswith("__"):
            results.append(inner)
    return results

BOUNDED_LIFE_KEYWORDS = (
    "byte_identical",
    "byte_equiv",
    "legacy_compatibility",
    "pre_migration",
    "before_refactor",
)

# Heuristic: assert something == path.read() / assert something == path.read_text()
SNAPSHOT_RE = re.compile(
    r"assert\b.*==.*\.(read|read_text)\s*\(",
    re.DOTALL,
)

# Exclusion: both sides are dynamic file reads (= file-copy faithfulness
# invariant, NOT a snapshot pin). Common in tests that verify a preprocessor
# copies source → target without mutation. Example:
#   assert copied.read_text() == source.read_text()
# This is a Tier 2b sub-system invariant (copy correctness), not a snapshot test
# (which would compare a runtime output against a frozen golden file).
INVARIANT_FILE_COPY_RE = re.compile(
    r"assert\b.*\.(read|read_text)\s*\([^)]*\)\s*==.*\.(read|read_text)\s*\(",
    re.DOTALL,
)

# LLM boundary patch detection per testing.ja.md "litellm への unittest.mock
# パッチ" prohibition. Internal code patches (reyn.* paths) inside Tier 2c
# integration tests are permitted. We flag a patch only when its target looks
# like an LLM boundary: litellm, call_llm, acompletion, or a *.llm.* path.
LLM_BOUNDARY_PATCH_RE = re.compile(
    r"\b(litellm|acompletion|call_llm[\w_]*|\w+\.llm\.[\w.]+)\b"
)


def _is_llm_boundary_patch(patch_src: str) -> bool:
    """Return True iff patch target string looks like an LLM boundary."""
    return bool(LLM_BOUNDARY_PATCH_RE.search(patch_src))

RULE_NAMES = {
    "tier-docstring",
    "format-pinning",
    "private-state",
    "mock",
    "bounded-life",
    "snapshot",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

Level = Literal["ERROR", "WARNING"]


@dataclass
class Finding:
    rule: str
    level: Level
    line: int
    message: str
    suggestion: str = ""
    policy_ref: str = ""


@dataclass
class TestResult:
    name: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def has_errors(self) -> bool:
        return any(f.level == "ERROR" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.level == "WARNING" for f in self.findings)


@dataclass
class FileReport:
    path: Path
    results: list[TestResult] = field(default_factory=list)
    parse_error: str = ""

    @property
    def error_count(self) -> int:
        return sum(
            1 for r in self.results for f in r.findings if f.level == "ERROR"
        )

    @property
    def warning_count(self) -> int:
        return sum(
            1 for r in self.results for f in r.findings if f.level == "WARNING"
        )


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class TestAuditor:
    def __init__(self, check_rules: set[str] | None = None) -> None:
        self.check_rules = check_rules  # None = all rules

    def _rule_active(self, rule: str) -> bool:
        return self.check_rules is None or rule in self.check_rules

    def audit_file(self, path: Path) -> FileReport:
        report = FileReport(path=path)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.parse_error = str(exc)
            return report

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.parse_error = f"SyntaxError: {exc}"
            return report

        source_lines = source.splitlines()
        in_scaffold = "tests/scaffold" in str(path).replace("\\", "/")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    result = self._audit_test(
                        path, source, source_lines, node, in_scaffold
                    )
                    report.results.append(result)

        return report

    def _audit_test(
        self,
        path: Path,
        source: str,
        source_lines: list[str],
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        in_scaffold: bool,
    ) -> TestResult:
        result = TestResult(name=node.name)

        # Extract node source lines (1-indexed in AST)
        node_lines = source_lines[node.lineno - 1 : node.end_lineno]
        node_source = "\n".join(node_lines)

        # --- Rule 1: Missing Tier docstring ---
        if self._rule_active("tier-docstring"):
            docstring = ast.get_docstring(node)
            if docstring is None:
                result.findings.append(
                    Finding(
                        rule="tier-docstring",
                        level="ERROR",
                        line=node.lineno,
                        message="no docstring at all — Tier declaration required",
                        suggestion='Add a docstring starting with """Tier N: ..."""',
                        policy_ref="testing.ja.md: 各テストの docstring 一行目に Tier の明記",
                    )
                )
            elif not TIER_DOCSTRING_RE.match(docstring.strip()):
                first_line = docstring.strip().splitlines()[0]
                result.findings.append(
                    Finding(
                        rule="tier-docstring",
                        level="ERROR",
                        line=node.lineno,
                        message=f'docstring does not start with "Tier N:": {first_line!r}',
                        suggestion='Change first docstring line to """Tier 1/2/3a/3b: ..."""',
                        policy_ref="testing.ja.md: 各テストの docstring 一行目に Tier の明記",
                    )
                )

        # --- Rule 2: Format pinning ---
        if self._rule_active("format-pinning"):
            for rel_lineno, line in enumerate(node_lines):
                for m in FORMAT_PIN_RE.finditer(line):
                    # Exempt len(x) > 0  (existence check)
                    n = int(m.group(1))
                    op_start = m.start()
                    after_len = line[op_start:].lstrip()
                    if _is_existence_check_only(line, m):
                        continue
                    abs_lineno = node.lineno + rel_lineno
                    result.findings.append(
                        Finding(
                            rule="format-pinning",
                            level="ERROR",
                            line=abs_lineno,
                            message=f"format pinning: {m.group(0).strip()}",
                            suggestion="Remove this assertion — pin behavior not size/shape",
                            policy_ref='testing.ja.md Tier 4: "見た目のフォーマット固定（空白、句読点、行数、カラーコード）"',
                        )
                    )

        # --- Rule 3: Private state assertion (AST-based, Tier C1 2026-05-27) ---
        # Earlier regex required a preceding dot anchor and missed bare
        # ``assert obj._x`` — the most common form. AST detection walks each
        # ``ast.Assert`` value tree for any ``ast.Attribute`` whose ``attr``
        # starts with a single underscore (excluding dunder), so it catches
        # bare, nested, chained, and subscript forms uniformly.
        if self._rule_active("private-state"):
            seen_lines: set[int] = set()
            for stmt in ast.walk(node):
                if not isinstance(stmt, ast.Assert):
                    continue
                # Walk the assertion's test expression for any private attr.
                # ``ast.walk`` on the whole Assert would re-traverse but
                # confining to ``stmt.test`` keeps msg expressions (= the
                # second arg of assert) out of scope, matching policy intent.
                private_attrs = _find_private_attr_access(stmt.test)
                if not private_attrs:
                    continue
                # One finding per assert (= use the first private access for
                # the message; line is the assert's line so reviewers find it).
                first = private_attrs[0]
                stmt_src = ast.get_source_segment(source, stmt) or ""
                if stmt.lineno in seen_lines:
                    continue
                seen_lines.add(stmt.lineno)
                result.findings.append(
                    Finding(
                        rule="private-state",
                        level="ERROR",
                        line=stmt.lineno,
                        message=(
                            f"private state assertion (.{first.attr}): "
                            f"{stmt_src.strip()[:80]}"
                        ),
                        suggestion="Use snapshot() or public API instead",
                        policy_ref="testing.ja.md Tier 4: private state への直接 assert",
                    )
                )

        # --- Rule 4: MagicMock / AsyncMock / patch ---
        if self._rule_active("mock"):
            # Check imports at module level (only once per file — but we check
            # inside the function body here; module-level checked separately)
            _check_mock_in_func(node, source, result)

        # --- Rule 5: Bounded-life test outside scaffold ---
        if self._rule_active("bounded-life") and not in_scaffold:
            for kw in BOUNDED_LIFE_KEYWORDS:
                if kw in node.name:
                    result.findings.append(
                        Finding(
                            rule="bounded-life",
                            level="WARNING",
                            line=node.lineno,
                            message=f"bounded-life keyword '{kw}' in test name but not in tests/scaffold/",
                            suggestion="Move to tests/scaffold/ with triggered_by/removed_by metadata",
                            policy_ref="testing.ja.md Annex: スキャフォールディングテスト",
                        )
                    )
                    break  # one finding per test

        # --- Rule 6: Snapshot/golden test outside scaffold ---
        if self._rule_active("snapshot") and not in_scaffold:
            for rel_lineno, line in enumerate(node_lines):
                if SNAPSHOT_RE.search(line):
                    # Exclude file-copy invariant pattern (both sides .read_text())
                    if INVARIANT_FILE_COPY_RE.search(line):
                        continue
                    abs_lineno = node.lineno + rel_lineno
                    result.findings.append(
                        Finding(
                            rule="snapshot",
                            level="ERROR",
                            line=abs_lineno,
                            message=f"snapshot/golden file comparison: {line.strip()[:80]}",
                            suggestion=(
                                "Move to tests/scaffold/ with triggered_by/removed_by, "
                                "or replace with direct invariant assertion"
                            ),
                            policy_ref="testing.ja.md Tier 4: スナップショット／ゴールデンファイルテスト",
                        )
                    )
                    break  # one finding per test

        return result


def _is_existence_check_only(line: str, m: re.Match) -> bool:
    """Return True if the len(...) match is a simple > 0 existence check."""
    after = line[m.end():].lstrip()
    # Patterns like "> 0" or ">= 1" — but NOT "< 3000" or "> 10"
    # We consider > 0 and >= 1 as existence checks; anything else is pinning.
    n = int(m.group(1))
    op_part = line[m.start():m.end()]
    # Extract the operator from the full match
    op_m = re.search(r"len\([^)]+\)\s*([<>=!]+)\s*(\d+)", op_part)
    if op_m:
        op = op_m.group(1)
        val = int(op_m.group(2))
        if (op == ">" and val == 0) or (op == ">=" and val == 1):
            return True
    return False


def _check_mock_in_func(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    result: TestResult,
) -> None:
    """Detect @patch decorators and with-patch / MagicMock inside the function.

    Narrow scope per testing.ja.md: prohibition is "litellm への unittest.mock パッチ"
    (= LLM-boundary patches). Internal code patches (= reyn.* paths) inside
    Tier 2c integration tests are permitted — they exercise real production code
    paths through controlled fake dependencies, not LLM contract evasion.
    """
    # Check decorators for @patch
    for dec in node.decorator_list:
        dec_src = ast.get_source_segment(source, dec) or ""
        if "patch" in dec_src and "unittest" in dec_src or dec_src.strip().startswith("patch"):
            if not _is_llm_boundary_patch(dec_src):
                continue
            result.findings.append(
                Finding(
                    rule="mock",
                    level="ERROR",
                    line=dec.lineno,
                    message=f"@patch decorator (LLM boundary): {dec_src.strip()[:80]}",
                    suggestion="Use LLMReplay Fake instead of unittest.mock.patch",
                    policy_ref="testing.ja.md Mock vs Fake: Mock は禁止 — LLMReplay を使う",
                )
            )

    # Walk the function body for MagicMock / AsyncMock / with patch
    for stmt in ast.walk(node):
        stmt_src = ast.get_source_segment(source, stmt) or ""
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            mod = ""
            if isinstance(stmt, ast.ImportFrom) and stmt.module:
                mod = stmt.module
            elif isinstance(stmt, ast.Import):
                mod = " ".join(a.name for a in stmt.names)
            if "unittest.mock" in mod or (
                isinstance(stmt, ast.ImportFrom) and any(
                    n.name in ("MagicMock", "AsyncMock", "patch") for n in stmt.names
                )
            ):
                result.findings.append(
                    Finding(
                        rule="mock",
                        level="ERROR",
                        line=stmt.lineno,
                        message=f"unittest.mock import: {stmt_src.strip()[:80]}",
                        suggestion="Use real instances or LLMReplay Fake",
                        policy_ref="testing.ja.md Mock vs Fake",
                    )
                )
        elif isinstance(stmt, ast.With):
            # with patch(...) context manager
            for item in stmt.items:
                item_src = ast.get_source_segment(source, item.context_expr) or ""
                if re.search(r"\bpatch\s*\(", item_src):
                    if not _is_llm_boundary_patch(item_src):
                        continue
                    result.findings.append(
                        Finding(
                            rule="mock",
                            level="ERROR",
                            line=stmt.lineno,
                            message=f"with patch(...) (LLM boundary): {item_src.strip()[:80]}",
                            suggestion="Use LLMReplay Fake instead of patch",
                            policy_ref="testing.ja.md Mock vs Fake",
                        )
                    )
        elif isinstance(stmt, ast.Call):
            # MagicMock() / AsyncMock() calls
            func_src = ast.get_source_segment(source, stmt.func) or ""
            if re.search(r"\b(MagicMock|AsyncMock)\b", func_src):
                result.findings.append(
                    Finding(
                        rule="mock",
                        level="ERROR",
                        line=stmt.lineno,
                        message=f"MagicMock/AsyncMock usage: {func_src.strip()[:80]}",
                        suggestion="Use real instances or LLMReplay Fake",
                        policy_ref="testing.ja.md Mock vs Fake",
                    )
                )


def _check_module_level_mock_imports(
    path: Path, source: str, tree: ast.Module
) -> list[Finding]:
    """Check module-level unittest.mock imports (outside test functions)."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip; handled per-function
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = " ".join(a.name for a in node.names)
            if "unittest.mock" in mod:
                src = ast.get_source_segment(source, node) or ""
                findings.append(
                    Finding(
                        rule="mock",
                        level="ERROR",
                        line=node.lineno,
                        message=f"module-level unittest.mock import: {src.strip()[:80]}",
                        suggestion="Remove — use real instances or LLMReplay Fake",
                        policy_ref="testing.ja.md Mock vs Fake",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def collect_files(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            result.append(p)
        elif p.is_dir():
            result.extend(sorted(p.rglob("test_*.py")))
    # deduplicate preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for f in result:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

TICK = "✓"
WARN = "⚠️ "
CROSS = "✗"


def _level_icon(level: Level) -> str:
    return WARN if level == "WARNING" else CROSS


def render_text(
    reports: list[FileReport],
    quiet: bool = False,
    strict: bool = False,
) -> None:
    total_tests = 0
    total_ok = 0
    total_warnings = 0
    total_errors = 0

    for report in reports:
        rel = _rel_path(report.path)
        print(f"\n{rel}:")

        if report.parse_error:
            print(f"  [PARSE ERROR] {report.parse_error}")
            continue

        if not report.results:
            print("  (no test functions found)")
            continue

        for res in report.results:
            total_tests += 1
            if res.ok:
                total_ok += 1
                if not quiet:
                    print(f"  {TICK} {res.name}")
            else:
                errors = [f for f in res.findings if f.level == "ERROR"]
                warnings = [f for f in res.findings if f.level == "WARNING"]
                if errors:
                    total_errors += 1
                    print(f"  {CROSS} {res.name}")
                elif warnings:
                    total_warnings += 1
                    print(f"  {WARN}{res.name}")
                for f in res.findings:
                    icon = _level_icon(f.level)
                    print(f"      line {f.line}: {f.message}")
                    if f.policy_ref:
                        print(f"      policy: {f.policy_ref}")
                    if f.suggestion:
                        print(f"      suggestion: {f.suggestion}")

    print(
        f"\nSummary: {total_tests} test{'s' if total_tests != 1 else ''} inspected"
    )
    print(f"  {TICK} OK: {total_ok}")
    print(f"  {WARN}warnings: {total_warnings} (bounded-life candidates)")
    print(f"  {CROSS} errors: {total_errors} (Tier 4 violations)")

    exit_code = 0
    if total_errors > 0:
        exit_code = 1
    elif strict and total_warnings > 0:
        exit_code = 1
    print(f"\nExit code: {exit_code}{' (errors found)' if exit_code else ''}")


def render_json(reports: list[FileReport], strict: bool = False) -> None:
    out: list[dict] = []
    for report in reports:
        file_data: dict = {
            "path": str(report.path),
            "parse_error": report.parse_error,
            "tests": [],
        }
        for res in report.results:
            file_data["tests"].append(
                {
                    "name": res.name,
                    "ok": res.ok,
                    "findings": [
                        {
                            "rule": f.rule,
                            "level": f.level,
                            "line": f.line,
                            "message": f.message,
                            "suggestion": f.suggestion,
                            "policy_ref": f.policy_ref,
                        }
                        for f in res.findings
                    ],
                }
            )
        out.append(file_data)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _rel_path(p: Path) -> str:
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reyn test policy compliance auditor (testing.ja.md)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="FILE_OR_DIR",
        help="Files or directories to audit (default: tests/)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (exit 1 on any warning)",
    )
    parser.add_argument(
        "--check",
        metavar="RULE",
        help=(
            "Only check one rule: "
            "tier-docstring / format-pinning / private-state / mock / bounded-life / snapshot"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress OK tests; show only warnings and errors",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Machine-readable JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate --check
    check_rules: set[str] | None = None
    if args.check:
        if args.check not in RULE_NAMES:
            print(
                f"Unknown rule: {args.check!r}. Valid: {', '.join(sorted(RULE_NAMES))}",
                file=sys.stderr,
            )
            return 2
        check_rules = {args.check}

    # Resolve targets
    target_paths = [Path(t) for t in args.targets] if args.targets else [Path("tests")]
    files = collect_files(target_paths)

    if not files:
        print("No test files found.", file=sys.stderr)
        return 0

    auditor = TestAuditor(check_rules=check_rules)
    reports: list[FileReport] = []

    for f in files:
        report = auditor.audit_file(f)

        # Also check module-level mock imports (injected as a synthetic result)
        if not report.parse_error and check_rules is None or (
            check_rules and "mock" in check_rules
        ):
            try:
                source = f.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(f))
                module_findings = _check_module_level_mock_imports(f, source, tree)
                if module_findings:
                    # Attach to a synthetic result named "<module>"
                    synthetic = TestResult(name="<module-level>")
                    synthetic.findings.extend(module_findings)
                    report.results.insert(0, synthetic)
            except (OSError, SyntaxError):
                pass

        reports.append(report)

    if args.json_output:
        render_json(reports, strict=args.strict)
        # Compute exit code
        has_errors = any(r.error_count > 0 for r in reports)
        has_warnings = any(r.warning_count > 0 for r in reports)
    else:
        render_text(reports, quiet=args.quiet, strict=args.strict)
        has_errors = any(r.error_count > 0 for r in reports)
        has_warnings = any(r.warning_count > 0 for r in reports)

    if has_errors:
        return 1
    if args.strict and has_warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
