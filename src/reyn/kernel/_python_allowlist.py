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

# mode: safe — output is determined by input artifact + ambient sources only.
# Ambient sources = clock (time, datetime), entropy (random, secrets), and
# bundled static data (zoneinfo). Filesystem, network, subprocess, and
# environment access are syntactically unreachable.
#
# Each allowlisted entry below has been audited against this contract.
#
# Categories used in inline comments:
#   # pure        — no ambient access at all; output is a pure function of inputs
#   # ambient: …  — reads an ambient source (clock / entropy / bundled static data)
#                   but never observes or mutates operator-visible state
#   # restricted  — admitted module but only a subset of operations is safe
#                   (e.g. pure path manipulation only, not filesystem reads)
#
# If a step needs a non-ambient capability (operator files, network, env vars,
# process spawning), use a `run_op` step — that is the proper escape hatch with
# its own permission gate and event log entry.
#
# Full author guide: docs/concepts/python-safe-mode.md
PURE_STDLIB_ALLOWLIST: frozenset[str] = frozenset({
    # --- pure: numeric computation ---
    "math",        # pure: standard math functions over numeric inputs
    "cmath",       # pure: complex-number math
    "statistics",  # pure: descriptive statistics over sequences
    "decimal",     # pure: arbitrary-precision decimal arithmetic
    "fractions",   # pure: rational number arithmetic
    "numbers",     # pure: numeric abstract base classes (ABC only)

    # --- pure: text processing ---
    "string",      # pure: string constants and Template formatting
    "re",          # pure: regular-expression matching
    "textwrap",    # pure: text wrapping and indentation
    "unicodedata", # pure: Unicode character property lookup (bundled table)

    # --- ambient: clock + bundled tz static data ---
    "time",        # ambient: system wall clock + monotonic clock (clock I/O)
    "datetime",    # ambient: system clock via datetime.now(); also pure date arithmetic
    "calendar",    # pure: calendar computations (no clock call at import time)
    "zoneinfo",    # ambient: bundled IANA TZ database shipped with Python (static files)

    # --- pure: data encoding and hashing ---
    "json",        # pure: JSON serialisation / deserialisation
    "base64",      # pure: base-64 / base-32 / base-16 codec
    "binascii",    # pure: binary-to-ASCII conversions
    "hashlib",     # pure: cryptographic hash functions (computation over inputs)
    "hmac",        # pure: keyed-hash message authentication (computation over inputs)

    # --- ambient: entropy ---
    "secrets",     # ambient: /dev/urandom-backed CSPRNG (entropy I/O)
    "random",      # ambient: /dev/urandom-seeded PRNG (entropy I/O)

    # --- pure: collections and functional programming ---
    "collections", # pure: specialised container types (deque, Counter, etc.)
    "itertools",   # pure: iterator combinators
    "functools",   # pure: higher-order functions and decorators
    "operator",    # pure: operators as functions
    "copy",        # pure: shallow and deep copy of objects

    # --- pure: typing and data structures ---
    "enum",        # pure: enumeration classes
    "dataclasses", # pure: data class decorator and helpers
    "typing",      # pure: type annotation support
    "abc",         # pure: abstract base class infrastructure

    # --- pure: compiler directives (no runtime capability) ---
    "__future__",  # pure: compiler directives only (annotations, division, etc.)
                   #       — no I/O, no module loading semantics
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
