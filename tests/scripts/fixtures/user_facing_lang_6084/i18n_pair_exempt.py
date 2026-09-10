"""Fixture for user_facing_lang_gate.py's own test suite (#6084).

Not a real module — never imported, never collected as a test. Exercises
ruling 3's structural i18n-pair exception: a module-level `_FOO_EN` /
`_FOO_JA` pair (matching suffix, same prefix, both module-level) is exempt
— together with everything nested inside its value expression — while a
same-shaped `_BAR_JA` WITHOUT an `_BAR_EN` sibling is NOT exempt and must
still be flagged. The exempt/non-exempt pair below share the exact same
sink shape so the only variable between them is the sibling's presence.
"""
from __future__ import annotations

from reyn.runtime.outbox import OutboxMessage

# Exempt: `_FOO_EN` sibling exists below, same prefix, both module-level.
_FOO_JA = OutboxMessage(kind="system", text="こんにちは")
_FOO_EN = OutboxMessage(kind="system", text="hello")

# NOT exempt: no `_BAR_EN` sibling anywhere in this module — same nesting
# shape as `_FOO_JA` above, must still be flagged.
_BAR_JA = OutboxMessage(kind="system", text="ダメです")
