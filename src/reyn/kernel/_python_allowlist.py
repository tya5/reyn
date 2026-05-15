"""Allowlist + ambient-sources contract for python step `mode: safe`.

`mode: safe` is a *contract* for the python step's output: it must depend
only on (input artifact + ambient sources). Ambient sources are limited to:

  - **clock** — `time`, `datetime.now()`, `calendar`
  - **entropy** — `random`, `secrets` (= /dev/urandom on POSIX)
  - **bundled static data** — `zoneinfo` (= shipped IANA tz tables),
    `unicodedata` (= Unicode property tables compiled into CPython),
    Python's own `__future__` compiler-directive flags

Everything else in `PURE_STDLIB_ALLOWLIST` is pure (= mathematically derivable
from input alone — `math`, `json`, `re`, `hashlib`, `collections`, ...).

What `mode: safe` rules out by *syntactic unreachability*:

  - filesystem path I/O — `glob`, `os`, `pathlib`, `shutil`, `tempfile`
  - network — `urllib`, `socket`, `http`, `ssl`, `requests`, `httpx`
  - subprocess / external command — `subprocess`, `os.system`, `popen`
  - environment — `os.environ`, `sys.argv` (= operator-injected state)
  - dynamic code — `exec`, `eval`, `compile`, `__import__`,
    `importlib`, file-reading codec / encoding loaders

Authors who need *any* of the above must declare `mode: unsafe` (= stdlib
auto-allowed via `reyn run`, user skills need `--allow-unsafe-python` or
explicit operator approval). The boundary is sharp: if the python step's
output depends on a value the operator can change without editing the
input artifact, the step is non-`safe`.

Honest scope: import allowlist + banned-builtin reference detection at AST
parse time. The real safety boundary is the subprocess execution sandbox +
permission gating — this validator catches honest authoring mistakes, not
motivated attackers.

Each allowlisted module entry below has been audited against this contract.

Categories used in inline comments:
  # pure          — no ambient access at all; output is a pure function of inputs
  # ambient: …    — reads an ambient source (clock / entropy / bundled static data)
                    but never observes or mutates operator-visible state
  # restricted    — admitted module but only a subset of operations is safe
                    (e.g. pure path manipulation only, not filesystem reads)

If a step needs a non-ambient capability (operator files, network, env vars,
process spawning), use a `run_op` step — that is the proper escape hatch with
its own permission gate and event log entry.

Full author guide: docs/concepts/python-safe-mode.md
"""
from __future__ import annotations

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
    "unicodedata", # pure: Unicode property tables compiled into CPython (no file I/O at runtime)

    # --- ambient: clock + bundled tz static data ---
    "time",        # ambient: system wall clock + monotonic clock (clock I/O)
    "datetime",    # ambient: system clock via datetime.now(); also pure date arithmetic
    "calendar",    # pure: calendar arithmetic (grouped here; no system clock read at any call site)
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


# Builtins removed from the user function's exec environment in `mode: safe`.
# These are the remaining built-in escape hatches that would violate the
# "ambient sources only" property even when all imports are restricted:
#   - non-ambient ingress (open, input, __import__)
#   - dynamic code that bypasses the import allowlist (exec, eval, compile)
#   - side-effecting process ops that mutate state beyond the step's scope
# Anything in the standard `builtins` module that isn't listed here remains
# available to safe-mode steps.
BANNED_BUILTINS: frozenset[str] = frozenset({
    "open",         # non-ambient: arbitrary file I/O — violates ambient-sources property
    "exec", "eval", "compile",  # dynamic code — bypasses import allowlist entirely
    "__import__",   # dynamic import — bypasses PURE_STDLIB_ALLOWLIST check
    "breakpoint",   # interactive debugger — non-ambient process I/O
    "input",        # non-ambient: stdin read — operator-injected state
    "exit", "quit", # process termination — non-ambient side effect
    "memoryview",   # raw memory access — non-ambient, may expose interpreter state
    "globals", "vars",  # retained for hygiene; discoverable but not functionally unsafe
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
