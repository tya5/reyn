"""Allowlists used by the Python preprocessor harness.

These define what `safe` mode lets through. The harness consults
PURE_STDLIB_ALLOWLIST when validating import statements in user code,
strips BANNED_BUILTINS from __builtins__ before exec, and (only when
the parent provides them via the JSON protocol) extends the import
allowlist with user-configured `python.allowed_modules` from reyn.yaml.

Lists kept here so they're a single source of truth across parent and
child processes.
"""
from __future__ import annotations

# Stdlib modules considered safe enough for safe-mode preprocessor steps.
#
# Safe mode = "ambient sources only". A python step's output is determined
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
# Full author guide: docs/concepts/python-safe-mode.md
PURE_STDLIB_ALLOWLIST: frozenset[str] = frozenset({
    # numeric — deterministic computation
    "math", "statistics", "decimal", "fractions", "cmath", "numbers",
    # text — deterministic computation
    "string", "re", "textwrap", "unicodedata",
    # time / date — ambient clock + bundled tz static data
    "datetime", "calendar", "zoneinfo", "time",
    # data encoding / hashing — deterministic computation (secrets reads entropy)
    "json", "base64", "binascii", "hashlib", "hmac", "secrets",
    # collections / functional — deterministic computation
    "collections", "itertools", "functools", "operator", "copy",
    # typing / structure — deterministic computation
    "enum", "dataclasses", "typing", "abc",
    # randomness — ambient entropy
    "random",
})


# Builtins removed from the user function's exec environment in safe mode.
# Anything in the standard `builtins` module that isn't here remains available.
BANNED_BUILTINS: frozenset[str] = frozenset({
    "open",         # file I/O
    "exec", "eval", "compile",  # arbitrary code execution
    "__import__",   # bypass import allowlist
    "breakpoint",   # interactive debugger
    "input",        # stdin
    "exit", "quit", # process termination
    "memoryview",   # raw memory access
    "globals", "vars",  # discoverable in safe mode but listed for hygiene
})


def module_is_allowed(module_name: str, extra_allowed: frozenset[str] | set[str]) -> bool:
    """Whether `module_name` may be imported in safe mode.

    Allows:
      - any top-level stdlib module in PURE_STDLIB_ALLOWLIST
      - any module in `extra_allowed` (passed in by the parent)
      - the `reyn.safe` package and its submodules (= Reyn-vetted safe
        helpers; explicit allow even though `reyn` is not in the stdlib
        allowlist)

    Explicitly rejects:
      - `reyn.unsafe` and its submodules (= reserved for unsafe-mode steps;
        explicit defence so the import is rejected at parse time)

    A submodule like `decimal.Decimal` is checked by its top-level package
    name (`decimal`) because that's the import unit. `reyn.safe.X` / `reyn.unsafe.X`
    are matched on the package prefix instead of the top-level so the
    explicit allow / reject pair takes precedence over the implicit
    "`reyn` not in stdlib allowlist" path.
    """
    # Explicit reject of reyn.unsafe.* — defence-in-depth even though `reyn`
    # is not in PURE_STDLIB_ALLOWLIST (= the implicit path would already
    # reject it; this branch makes the intent unambiguous).
    if module_name == "reyn.unsafe" or module_name.startswith("reyn.unsafe."):
        return False
    # Explicit allow of reyn.safe.* — Reyn-vetted helpers callable from
    # safe-mode python steps.
    if module_name == "reyn.safe" or module_name.startswith("reyn.safe."):
        return True
    top = module_name.split(".", 1)[0]
    return top in PURE_STDLIB_ALLOWLIST or top in extra_allowed
