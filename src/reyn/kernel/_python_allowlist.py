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
#
# Pure mode = "ambient sources only". A python step's output is determined
# ONLY by its input artifacts plus ambient sources: the wall clock, an
# entropy stream, and bundled stdlib static data (e.g. the tz database
# shipped with Python). Filesystem, network, subprocess, and process
# environment access are syntactically unreachable — the AST validator
# rejects banned modules / builtins, and the subprocess sandbox provides
# defence in depth.
#
# Author rule of thumb: a module belongs here only if every public call
# is satisfiable from {inputs, clock, entropy, bundled static data}. Modules
# that ingress operator state (`os`, `pathlib`, `glob`, `os.environ`),
# touch the network (`urllib`, `socket`), or spawn processes (`subprocess`)
# are NOT ambient and stay out. If a step needs a non-ambient capability,
# use a `run_op` step instead — that's the proper escape hatch with its
# own permission gate.
#
# `random` / `time` / `datetime` / `secrets` / `zoneinfo` are intentionally
# allowed: they read from ambient sources (clock, entropy, bundled tz data)
# but never observe or mutate operator-visible state. Callers who need
# bit-for-bit determinism should still avoid them.
#
# Full author guide: docs/concepts/python-pure-mode.md
PURE_STDLIB_ALLOWLIST: frozenset[str] = frozenset({
    # numeric — pure computation
    "math", "statistics", "decimal", "fractions", "cmath", "numbers",
    # text — pure computation
    "string", "re", "textwrap", "unicodedata",
    # time / date — ambient clock + bundled tz static data
    "datetime", "calendar", "zoneinfo", "time",
    # data encoding / hashing — pure computation (secrets reads entropy)
    "json", "base64", "binascii", "hashlib", "hmac", "secrets",
    # collections / functional — pure computation
    "collections", "itertools", "functools", "operator", "copy",
    # typing / structure — pure computation
    "enum", "dataclasses", "typing", "abc",
    # randomness — ambient entropy
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
