"""OpResult — small helpers for op-handler return values.

Handlers return plain dicts (the shape callers historically appended to
control_ir_results). These exception classes let handlers signal common
non-success outcomes without constructing error dicts inline.
"""
from __future__ import annotations


class OpSkipped(Exception):
    """Raised by a handler when an op is silently skipped (with a reason)."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpDenied(PermissionError):
    """Alias for PermissionError to make handler intent explicit at call site."""


# Type alias for the canonical handler return shape.
# Handlers MAY return additional fields beyond these; this is just the floor.
OpResult = dict
