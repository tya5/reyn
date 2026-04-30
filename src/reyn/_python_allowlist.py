"""Allowlists used by the Python preprocessor harness.

These define what `pure` mode lets through. The harness consults
PURE_STDLIB_ALLOWLIST when validating import statements in user code,
strips BANNED_BUILTINS from __builtins__ before exec, and (only when
the parent provides them via the JSON protocol) extends the import
allowlist with user-configured `python.allowed_modules` from reyn.yaml.

Lists kept here so they're a single source of truth across parent and
child processes.
"""
from __future__ import annotations


# Stdlib modules considered safe enough for pure-mode preprocessor steps.
# Curated for "no I/O, no subprocess, no dynamic code execution".
# random / time are included because they're CPU-only — non-deterministic
# but not side-effecting from the OS-level perspective. callers who care
# about determinism should avoid them in their function.
PURE_STDLIB_ALLOWLIST: frozenset[str] = frozenset({
    # numeric
    "math", "statistics", "decimal", "fractions", "cmath", "numbers",
    # text
    "string", "re", "textwrap", "unicodedata",
    # time / date — non-deterministic but no I/O
    "datetime", "calendar", "zoneinfo", "time",
    # data encoding / hashing
    "json", "base64", "binascii", "hashlib", "hmac", "secrets",
    # collections / functional
    "collections", "itertools", "functools", "operator", "copy",
    # typing / structure
    "enum", "dataclasses", "typing", "abc",
    # randomness — non-deterministic but no I/O
    "random",
})


# Builtins removed from the user function's exec environment in pure mode.
# Anything in the standard `builtins` module that isn't here remains available.
BANNED_BUILTINS: frozenset[str] = frozenset({
    "open",         # file I/O
    "exec", "eval", "compile",  # arbitrary code execution
    "__import__",   # bypass import allowlist
    "breakpoint",   # interactive debugger
    "input",        # stdin
    "exit", "quit", # process termination
    "memoryview",   # raw memory access
    "globals", "vars",  # discoverable in pure mode but listed for hygiene
})


def module_is_allowed(module_name: str, extra_allowed: frozenset[str] | set[str]) -> bool:
    """Whether `module_name` (top-level) may be imported in pure mode.

    Allows the stdlib allowlist plus any extras the parent passes in.
    A submodule like `decimal.Decimal` is checked by its top-level package
    name (`decimal`) because that's the import unit.
    """
    top = module_name.split(".", 1)[0]
    return top in PURE_STDLIB_ALLOWLIST or top in extra_allowed
