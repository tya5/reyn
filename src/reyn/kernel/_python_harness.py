"""Child-process entry point for the Python preprocessor step.

Invoked as `python -m reyn._python_harness`. Reads a JSON request from
stdin, executes the user-supplied function under the requested mode
(safe | unsafe), and writes a JSON response to stdout.

Wire format
-----------
Request (stdin, single JSON object):
    {
      "module_path": "/abs/path/to/preprocessing.py",
      "function":    "compute_text_stats",
      "mode":        "safe" | "unsafe",
      "artifact":    {...},                  # passed as the function's argument
      "allowed_modules": ["numpy", ...]      # extra imports allowed in safe mode
    }

Response (stdout, single JSON object):
    {"ok": true,  "result": <JSON>}                          # success
    {"ok": false, "error": "<message>", "kind": "<class>"}   # failure

Errors are also surfaced via a non-zero exit code so the parent's
subprocess handler catches them even when stdout is malformed.
"""
from __future__ import annotations

import ast
import builtins as _builtins_module
import copy
import json
import sys
import traceback
from typing import Any

# Re-exported to keep parent and child agreeing on the constants.
from reyn.kernel._python_allowlist import (
    BANNED_BUILTINS,
    PURE_STDLIB_ALLOWLIST,
    module_is_allowed,
)

# ── AST validation (safe mode) ──────────────────────────────────────────────


class _SafeModeViolation(ValueError):
    """Raised when user code does something safe mode disallows."""


# Backwards-compatible alias — some external callers / tests reference the
# old name. New code should use _SafeModeViolation.
_PureModeViolation = _SafeModeViolation


def _validate_safe_ast(tree: ast.Module, allowed_modules: frozenset[str]) -> None:
    """Walk `tree` and reject anything outside the safe-mode allowlist.

    What this catches:
      - import / from-import of modules outside the allowlist (including
        explicit reject of `reyn.unsafe.*`)
      - bare references to banned names (open, eval, __import__, ...)

    What this does NOT catch (= NOT a sandbox):
      - Determined attackers using getattr() chains, string-encoded names,
        metaprogramming (`__class__.__bases__[0].__subclasses__()` and
        friends), or any non-syntactic escape technique.
      - Side effects inside *allowed* libraries.

    Honest scope: import allowlist + banned-builtin reference detection.
    The real safety boundary is subprocess isolation + permission gating,
    not this validator. AST-level escape-pattern detection was dropped in
    FP-0014 (ADR-G Phase 1) — it accrued maintenance debt for ~zero
    additional security against motivated attackers, and the subprocess
    boundary is the actual line of defence.
    """
    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not module_is_allowed(alias.name, allowed_modules):
                    raise _SafeModeViolation(
                        f"safe mode: import of {alias.name!r} not allowed; "
                        f"allowed stdlib: {sorted(PURE_STDLIB_ALLOWLIST)}, "
                        f"plus user-allowed: {sorted(allowed_modules)}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                raise _SafeModeViolation("safe mode: relative imports not allowed")
            if not module_is_allowed(node.module, allowed_modules):
                raise _SafeModeViolation(
                    f"safe mode: from-import of {node.module!r} not allowed"
                )
        # Banned name references
        elif isinstance(node, ast.Name):
            if node.id in BANNED_BUILTINS:
                raise _SafeModeViolation(
                    f"safe mode: reference to {node.id!r} is not allowed"
                )


# Backwards-compatible alias — the linter and some tests reference the old
# name. New code should call _validate_safe_ast directly.
_validate_pure_ast = _validate_safe_ast


# ── Restricted execution environment ────────────────────────────────────────


def _build_restricted_builtins(allowed_modules: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Copy the real `builtins` dict minus BANNED_BUILTINS, plus a guarded
    `__import__` so allowlisted module imports can actually execute.

    Without a `__import__` entry the user's `import json` AST-validates but
    fails at exec time with "ImportError: __import__ not found". Restoring
    a wrapped version that consults `module_is_allowed` lets stdlib safe
    modules in PURE_STDLIB_ALLOWLIST + allowed_modules import normally
    while still blocking arbitrary imports.
    """
    safe: dict[str, Any] = {}
    for name in dir(_builtins_module):
        if name.startswith("_") and name not in {"__build_class__", "__name__"}:
            # Drop dunder builtins except the ones Python needs to define classes.
            continue
        if name in BANNED_BUILTINS:
            continue
        safe[name] = getattr(_builtins_module, name)

    real_import = _builtins_module.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not module_is_allowed(name, allowed_modules):
            raise ImportError(
                f"safe mode: import of {name!r} not allowed; "
                f"allowed stdlib: {sorted(PURE_STDLIB_ALLOWLIST)}, "
                f"plus user-allowed: {sorted(allowed_modules)}"
            )
        return real_import(name, globals, locals, fromlist, level)

    safe["__import__"] = guarded_import
    return safe


def _exec_user_module(
    source: str,
    module_path: str,
    mode: str,
    allowed_modules: frozenset[str],
) -> dict[str, Any]:
    """Compile + exec the user file. Returns its module namespace."""
    tree = ast.parse(source, filename=module_path)
    if mode == "safe":
        _validate_safe_ast(tree, allowed_modules)
        builtins_dict = _build_restricted_builtins(allowed_modules)
    else:
        builtins_dict = _builtins_module.__dict__

    code = compile(tree, filename=module_path, mode="exec")
    namespace: dict[str, Any] = {
        "__builtins__": builtins_dict,
        "__name__": "__reyn_python_step__",
        "__file__": module_path,
    }
    exec(code, namespace)
    return namespace


# ── Main entry ──────────────────────────────────────────────────────────────


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw:
        raise ValueError("harness received empty stdin")
    return json.loads(raw)


def _write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    try:
        req = _read_request()
        module_path = str(req["module_path"])
        function_name = str(req["function"])
        mode = str(req.get("mode", "safe"))
        artifact = req.get("artifact", {})
        allowed_modules = frozenset(req.get("allowed_modules") or [])
        # FP-0042: file-permission paths declared by the skill, forwarded
        # by the parent's PreprocessorExecutor / PythonRunner. Either may
        # be empty (= no read / write granted). The values gate every
        # ``reyn.safe.file.*`` call from the user step.
        file_read_paths = list(req.get("file_read_paths") or [])
        file_write_paths = list(req.get("file_write_paths") or [])

        if mode not in ("safe", "unsafe"):
            raise ValueError(f"unknown mode: {mode!r}")

        with open(module_path, encoding="utf-8") as f:
            source = f.read()

        namespace = _exec_user_module(source, module_path, mode, allowed_modules)

        fn = namespace.get(function_name)
        if fn is None or not callable(fn):
            raise AttributeError(
                f"function {function_name!r} not found in {module_path}"
            )

        # FP-0042: initialise reyn.safe.file's permission context before
        # the user step runs. The user code already executed (= module
        # exec at _exec_user_module) but the file-call paths only fire
        # when the function below is invoked. Safe either way: the
        # context is established before any guarded call.
        try:
            from reyn.safe import file as _safe_file
            _safe_file._set_permission_context(
                read_paths=file_read_paths,
                write_paths=file_write_paths,
            )
        except ImportError:
            # reyn.safe.file may not be available in older parent
            # installations (= shouldn't happen post-FP-0042 land but
            # defence-in-depth for parent / harness version skew).
            pass

        # Defensive copy so user mutations don't affect the parent's data.
        result = fn(copy.deepcopy(artifact))

        # Round-trip through JSON to fail fast on non-serializable returns.
        _write_response({"ok": True, "result": json.loads(json.dumps(result, default=str))})
        return 0

    except _SafeModeViolation as exc:
        _write_response({
            "ok": False, "kind": "SafeModeViolation", "error": str(exc),
        })
        return 2
    except Exception as exc:
        _write_response({
            "ok": False, "kind": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=20),
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
