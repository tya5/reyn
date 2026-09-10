"""#5990 stage 2-2 fixture: correct-side shapes the axis must NOT flag,
even unmarked — each function/module-level site below is a deliberate
NEGATIVE control, one per reason the axis excludes it."""
from __future__ import annotations

try:
    _MODULE_LEVEL_FALLBACK = "x"
except Exception:
    # No enclosing function -- nothing to "return" to. Correct-side by
    # construction, per backlog-watcher's own #5990 census finding.
    _MODULE_LEVEL_FALLBACK = "y"


def not_assign_only() -> str:
    """The handler body is NOT assign-only (a `raise` follows the
    assign) -- outside this axis's population entirely (a different
    question `silent_except_ratchet.py`'s own axis covers)."""
    try:
        value = "x"
    except Exception:
        value = "y"
        raise
    return value


def bound_name_never_returned() -> str:
    """Assign-only handler, but the bound name never reaches a
    `return` in this function -- correct-side: the fabricated value
    stays local."""
    try:
        _unused = "x"  # noqa: F841
    except Exception:
        _unused = "y"  # noqa: F841
    return "a completely different, hardcoded value"


def shadowed_in_nested_function() -> str:
    """The bound name IS returned, but only from a NESTED function's
    own (unrelated) local variable of the same spelling -- the outer
    function's own `return` never references the handler's binding."""
    try:
        canonical = "x"
    except Exception:
        canonical = "y"

    def _inner() -> str:
        canonical = "shadowed, not the outer binding"  # noqa: F841
        return canonical

    _inner()
    return "outer value, unrelated to the except handler above"
