"""Deepest frame in the production-filter witness (see
test_silent_warnings_category_gate_6143.py). Never run as __main__ --
always imported. `stacklevel=2` blames the CALLER's frame (`caller_mod`),
never this module and never `__main__`, matching how a real
`src/reyn/**` warnings.warn call site is several frames below any process
entry point."""
from __future__ import annotations

import warnings


def warn_something() -> None:
    warnings.warn(
        "silent under the stock default filter -- caller is not __main__",
        DeprecationWarning,
        stacklevel=2,
    )
