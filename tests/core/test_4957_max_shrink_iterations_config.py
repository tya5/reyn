"""Tier 1: `chat.compaction.max_shrink_iterations` config parsing (#4957,
owner: "max iterations は config ノブにしておいた方が良いね").

Before this, `retry_loop`'s `max_iterations` safety cap was a signature
default only (8) — `router_loop_driver.py` never passed it, so production
always ran at 8 with no operator-facing knob at all. Owner's own real
machine hits an HTTP 413 every turn (#4954); this is an ESCAPE VALVE for
that (see `CompactionConfig.max_shrink_iterations`'s own docstring), not a
cure — the field itself does not change #4947's shrink mechanics.

Named `max_shrink_iterations`, not `max_iterations` — `RouterLoop` has its
own unrelated `max_iterations` (the tool-call loop bound,
`router_loop_driver.py`'s `_router_max_iterations`); the same spelling
already caused one same-name-different-meaning confusion this session
(#4942).

#5531 §10 (2026-08-30): `retry_loop` no longer takes `max_iterations` at
all (removed — see its own "Bounded termination proof" docstring), so
this field is now ORPHANED — nothing reads it. The wiring test this
docstring used to point to
(`test_max_shrink_iterations_config_value_bounds_the_real_driver_call`)
was removed for the same reason. Removing the field itself
(schema/validation/docs) is a disclosed, separate follow-up — this file
still parses correctly and is left as-is pending that follow-up.
"""
from __future__ import annotations

import pytest

from reyn.config.chat import CompactionConfig, _build_chat_config


def test_max_shrink_iterations_defaults_to_8() -> None:
    """Tier 1: default is 8 — the retry_loop signature default this knob
    exposes, unchanged behavior for every deployment that never sets it."""
    assert CompactionConfig().max_shrink_iterations == 8


def test_chat_compaction_max_shrink_iterations_parses() -> None:
    """Tier 1: `chat.compaction.max_shrink_iterations: N` in reyn.yaml
    sets the field — the required "config で変更できる" condition."""
    cfg = _build_chat_config({"compaction": {"max_shrink_iterations": 20}})
    assert cfg.compaction.max_shrink_iterations == 20


def test_chat_compaction_max_shrink_iterations_absent_stays_default() -> None:
    """Tier 1: falsification pair — omitting the key keeps the default
    (this field does not accidentally pick up an unrelated value)."""
    cfg = _build_chat_config({"compaction": {"body_token_cap": 5000}})
    assert cfg.compaction.max_shrink_iterations == 8


def test_chat_compaction_max_shrink_iterations_absent_compaction_block_stays_default() -> None:
    """Tier 1: the field parses correctly (falls back to its default) even
    when the whole `chat.compaction:` block is absent, not just when it is
    present but missing this one key."""
    cfg = _build_chat_config({"render_mode": "plain"})
    assert cfg.compaction.max_shrink_iterations == 8


def test_max_shrink_iterations_below_1_raises_at_construction() -> None:
    """Tier 1: `__post_init__` rejects < 1 — 0 would never run the shrink
    loop at all (retry_loop's own `for _iteration in range(max_iterations)`
    body never executes), so the first overflow would raise immediately
    with zero chance to shrink, defeating the entire mechanism silently
    rather than failing fast at config-load time."""
    with pytest.raises(ValueError, match="max_shrink_iterations must be >= 1"):
        CompactionConfig(max_shrink_iterations=0)
    with pytest.raises(ValueError, match="max_shrink_iterations must be >= 1"):
        CompactionConfig(max_shrink_iterations=-1)


def test_max_shrink_iterations_of_1_is_the_valid_floor() -> None:
    """Tier 1: falsification pair for the floor check itself — 1 (the
    boundary) must NOT raise, only values below it."""
    assert CompactionConfig(max_shrink_iterations=1).max_shrink_iterations == 1
