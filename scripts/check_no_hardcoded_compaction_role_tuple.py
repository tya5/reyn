#!/usr/bin/env python3
"""#5699 — no file under ``src/`` hand-types the compaction-eligible role
tuple (``("user", "assistant", "tool", "agent")``, with or without
``"summary"`` appended) outside its ONE legitimate definition site.

## Why this exists

The owner's real-machine incident (2026-09-03): ``router_history_buffer.
py``'s two window-building filters gained a ``role="system"``/
``Disclosure.MODEL`` admission (#5678/#5688), but ``compaction_
controller.py``'s own candidate-selection filter and ``session.py``'s own
reporting filter — each a hand-typed COPY of the same base-role tuple —
never gained the same admission. An entry could enter the live window
forever while staying permanently un-foldable by ``/compact``
("Nothing was compacted this pass").

#5699's fix names the condition ONCE — :func:`reyn.runtime.chat_message.
is_compaction_eligible` / :func:`~reyn.runtime.chat_message.
is_compaction_eligible_including_summary` — and every filter now calls
one of those instead of re-typing the tuple. This gate is the "does not
recur" half: a future call site that hand-types the tuple again (instead
of importing the named predicate) silently reopens the exact drift #5699
just closed, with no red anywhere until an operator hits it live.

## Scope: ``src/``, one exempt file

``src/reyn/runtime/chat_message.py`` is the one legitimate definition
site (``COMPACTION_ELIGIBLE_BASE_ROLES``) — every other file must import
the named predicate instead of writing the literal. Scans ``src/`` only:
a test fixture constructing an unrelated 4-tuple of strings for its own
narrow purpose is not this gate's concern (tests are not a production
call site the drift #5699 fixed could recur through), and ``tests/``
already has its own, much larger set of literal role tuples exercising
edge cases deliberately.

## Why a whole-directory static scan, not a diff/base-ref check

Same reasoning as ``check_fastmcp_import_boundary.py``: whether a file
hand-types the tuple is fully determined by its CURRENT content, no move
or diff needed. A pure population scan against a real, verified-zero
baseline (this gate's own starting population, post-#5699, is zero).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _ROOT / "src"
_EXEMPT = {_SRC_DIR / "reyn" / "runtime" / "chat_message.py"}

# Matches the base 4-role sequence as adjacent string literals, in order,
# regardless of quote style or an optional trailing ``, "summary"`` (the
# ``is_compaction_eligible_including_summary`` shape) — a hand-typed
# COPY of ``COMPACTION_ELIGIBLE_BASE_ROLES``, not a reference to it.
_ROLE_TUPLE_PATTERN = re.compile(
    r"""['"]user['"]\s*,\s*['"]assistant['"]\s*,\s*['"]tool['"]\s*,\s*['"]agent['"]"""
)


def _hardcodes_role_tuple(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_ROLE_TUPLE_PATTERN.search(text))


def offending_files(src_dir: Path = _SRC_DIR) -> "list[Path]":
    """Every ``.py`` file under *src_dir* that hand-types the
    compaction-eligible role tuple — the gate's entire decision, isolated
    from CLI/printing so it is directly testable."""
    return [
        path
        for path in sorted(src_dir.rglob("*.py"))
        if path not in _EXEMPT and _hardcodes_role_tuple(path)
    ]


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-directory scan against a baseline of zero
    offenders = offending_files(_SRC_DIR)

    if not offenders:
        print(
            "OK: no file under src/ (outside chat_message.py) hand-types the "
            "compaction-eligible role tuple."
        )
        return 0

    print("no-hardcoded-compaction-role-tuple gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) under src/ hand-type "
        '(\'"user", "assistant", "tool", "agent"\') instead of importing '
        "reyn.runtime.chat_message.is_compaction_eligible[_including_summary] "
        "(#5699):",
        file=sys.stderr,
    )
    for path in offenders:
        print(f"  {path.relative_to(_ROOT)}", file=sys.stderr)
    print(
        "\nA hand-typed copy of this tuple is exactly how the owner's "
        "real-machine incident happened (#5699): one filter's copy was "
        "widened to admit a MODEL-visible system entry, a sibling copy was "
        "not, and the drift produced no signal until an operator hit it "
        "live. Import the named predicate instead of re-typing the roles.\n"
        "\nThis gate's own starting population is zero, so any hit here is "
        "a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
