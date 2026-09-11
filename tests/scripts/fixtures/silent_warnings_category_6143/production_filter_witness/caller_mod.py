"""Middle frame -- see lib_mod.py's own docstring. Also never run as
__main__."""
from __future__ import annotations

import lib_mod


def call_it() -> None:
    lib_mod.warn_something()
