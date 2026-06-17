"""kernel — OS runtime engine (P3: context build, LLM call, validation, transitions).

Public names are resolved lazily (PEP 562 ``__getattr__``) so that importing a
kernel *submodule* — notably ``reyn.core.kernel._python_harness``, the python
preprocessor-step child entry point — does not eagerly pull the runtime / llm
chain in through this ``__init__``. See ``reyn/__init__.py`` for the rationale
(FP-0008 C4 lazy-import line extended to the package surface). ``from
reyn.core.kernel import OSRuntime`` / ``reyn.core.kernel.OSRuntime`` still work via the
lazy load on first access; submodule imports (``from reyn.core.kernel.normalizer
import ...``) are unaffected.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import-cost-free hints for type checkers / IDEs
    from .control_ir_executor import ControlIRExecutor
    from .normalizer import (
        ControlIRValidationError,
        NormalizationError,
        NormalizationResult,
        normalize,
    )
    from .preprocessor_executor import PreprocessorExecutor
    from .runtime import LoopLimitExceededError, OSRuntime, RunResult
    from .validation import ValidationError, validate_output

__all__ = [
    "OSRuntime", "RunResult", "LoopLimitExceededError",
    "validate_output", "ValidationError",
    "normalize", "NormalizationError", "NormalizationResult", "ControlIRValidationError",
    "ControlIRExecutor",
    "PreprocessorExecutor",
]

_LAZY_ATTRS = {
    "ControlIRExecutor": ".control_ir_executor",
    "ControlIRValidationError": ".normalizer",
    "NormalizationError": ".normalizer",
    "NormalizationResult": ".normalizer",
    "normalize": ".normalizer",
    "PreprocessorExecutor": ".preprocessor_executor",
    "LoopLimitExceededError": ".runtime",
    "OSRuntime": ".runtime",
    "RunResult": ".runtime",
    "ValidationError": ".validation",
    "validate_output": ".validation",
}


def __getattr__(name: str):
    import importlib

    submodule = _LAZY_ATTRS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(submodule, __name__), name)


def __dir__():
    return sorted(__all__)
