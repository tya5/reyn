"""Child-process entry point for the Python preprocessor step.

Invoked as `python -m reyn._python_harness`. Reads a JSON request from
stdin, executes the user-supplied function under the requested mode
(pure | trusted), and writes a JSON response to stdout.

Wire format
-----------
Request (stdin, single JSON object):
    {
      "module_path": "/abs/path/to/preprocessing.py",
      "function":    "compute_text_stats",
      "mode":        "pure" | "trusted",
      "artifact":    {...},                  # passed as the function's argument
      "allowed_modules": ["numpy", ...]      # extra imports allowed in pure mode
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
import importlib
import json
import sys
import traceback
from typing import Any


# Re-exported to keep parent and child agreeing on the constants.
from reyn._python_allowlist import (
    BANNED_BUILTINS,
    PURE_STDLIB_ALLOWLIST,
    module_is_allowed,
)


# ── AST validation (pure mode) ──────────────────────────────────────────────


_BANNED_NAMES = frozenset(BANNED_BUILTINS) | frozenset({
    # Names whose mere reference enables sandbox escape.
    "__builtins__",
})


class _PureModeViolation(ValueError):
    """Raised when user code does something pure mode disallows."""


def _validate_pure_ast(tree: ast.Module, allowed_modules: frozenset[str]) -> None:
    """Walk `tree` and reject anything outside the pure-mode allowlist.

    What this catches:
      - import / from-import of disallowed modules
      - bare references to banned names (open, eval, __import__, ...)
      - attribute access onto __builtins__ / __class__ / __subclasses__ etc.
        (best-effort — doesn't catch every metaprogramming trick)

    What this does NOT catch:
      - Determined attackers using getattr() chains or string-encoded names
      - Side effects inside *allowed* libraries (pandas.read_csv, etc.)

    Honest limit: this is defense-in-depth, not a real sandbox.
    """
    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not module_is_allowed(alias.name, allowed_modules):
                    raise _PureModeViolation(
                        f"pure mode: import of {alias.name!r} not allowed; "
                        f"allowed stdlib: {sorted(PURE_STDLIB_ALLOWLIST)}, "
                        f"plus user-allowed: {sorted(allowed_modules)}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                raise _PureModeViolation("pure mode: relative imports not allowed")
            if not module_is_allowed(node.module, allowed_modules):
                raise _PureModeViolation(
                    f"pure mode: from-import of {node.module!r} not allowed"
                )
        # Banned name references
        elif isinstance(node, ast.Name):
            if node.id in _BANNED_NAMES:
                raise _PureModeViolation(
                    f"pure mode: reference to {node.id!r} is not allowed"
                )
        # Attribute access patterns commonly used to escape (best-effort).
        elif isinstance(node, ast.Attribute):
            # ().__class__.__bases__[0].__subclasses__() and friends
            if node.attr in {
                "__class__", "__bases__", "__subclasses__", "__mro__",
                "__globals__", "__builtins__", "__import__", "__reduce__",
                "__reduce_ex__", "__init_subclass__", "__subclasshook__",
            }:
                raise _PureModeViolation(
                    f"pure mode: attribute access to {node.attr!r} is not allowed"
                )


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
        top = name.split(".", 1)[0]
        if not module_is_allowed(top, allowed_modules):
            raise ImportError(
                f"pure mode: import of {name!r} not allowed; "
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
    if mode == "pure":
        _validate_pure_ast(tree, allowed_modules)
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
        mode = str(req.get("mode", "pure"))
        artifact = req.get("artifact", {})
        allowed_modules = frozenset(req.get("allowed_modules") or [])

        if mode not in ("pure", "trusted"):
            raise ValueError(f"unknown mode: {mode!r}")

        with open(module_path, encoding="utf-8") as f:
            source = f.read()

        namespace = _exec_user_module(source, module_path, mode, allowed_modules)

        fn = namespace.get(function_name)
        if fn is None or not callable(fn):
            raise AttributeError(
                f"function {function_name!r} not found in {module_path}"
            )

        # Defensive copy so user mutations don't affect the parent's data.
        result = fn(copy.deepcopy(artifact))

        # Round-trip through JSON to fail fast on non-serializable returns.
        _write_response({"ok": True, "result": json.loads(json.dumps(result, default=str))})
        return 0

    except _PureModeViolation as exc:
        _write_response({
            "ok": False, "kind": "PureModeViolation", "error": str(exc),
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
