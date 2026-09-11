"""#6132's own gate, given something real to catch (dev/test-only).

``tests/conftest.py`` escalates :class:`EncodingWarning` (PEP 597) to an
error for ``reyn.*``'s own call sites, so a NEW text-mode file open in
production code that omits ``encoding=`` fails CI the moment a test path
reaches it (or, more precisely, whenever the interpreter was started with
``PYTHONWARNDEFAULTENCODING=1`` — see that filter's own comment for why the
flag itself cannot be armed from here).

A filter alone is not a witness: nothing proves the filter actually CATCHES
anything unless something genuine, living inside the ``reyn.*`` namespace the
filter is scoped to, calls ``open()`` without ``encoding=``.
:func:`open_without_encoding_for_gate_test` is that something — its ONLY
job. Never call it from production code; it exists so
``tests/dev/test_6132_encoding_default_gate.py`` has a real ``reyn.*``
module (not the test's own ``tests.*`` module, which the filter does not
scope to) to exercise the mechanism against.
"""
from __future__ import annotations

from pathlib import Path


def open_without_encoding_for_gate_test(path: "str | Path") -> None:
    """Open *path* for text writing WITHOUT ``encoding=`` — deliberately,
    the exact #6132 shape. Exists only so the gate has a genuine ``reyn.*``
    call site to prove it catches; see the module docstring."""
    with open(path, "w") as f:  # noqa: PLW1514 -- deliberately no encoding=, see module docstring
        f.write("probe")
