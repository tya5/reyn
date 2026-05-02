"""kernel — OS runtime engine (P3: context build, LLM call, validation, transitions)."""
from .runtime import OSRuntime, RunResult, LoopLimitExceededError
from .validation import validate_output, ValidationError
from .normalizer import (
    normalize,
    NormalizationError,
    NormalizationResult,
    ControlIRValidationError,
)
from .control_ir_executor import ControlIRExecutor
from .preprocessor_executor import PreprocessorExecutor

__all__ = [
    "OSRuntime", "RunResult", "LoopLimitExceededError",
    "validate_output", "ValidationError",
    "normalize", "NormalizationError", "NormalizationResult", "ControlIRValidationError",
    "ControlIRExecutor",
    "PreprocessorExecutor",
]
