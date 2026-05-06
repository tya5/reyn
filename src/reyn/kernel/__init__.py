"""kernel — OS runtime engine (P3: context build, LLM call, validation, transitions)."""
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
