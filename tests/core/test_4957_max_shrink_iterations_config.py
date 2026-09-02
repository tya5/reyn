"""Tier 1/2: `chat.compaction.max_shrink_iterations` config parsing (#4957,
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
all (removed — see its own "Bounded termination proof" docstring), so this
field is ORPHANED — nothing reads it. The wiring test this docstring used
to point to (`test_max_shrink_iterations_config_value_bounds_the_real_
driver_call`) was removed for the same reason.

#5623: retirement, kept for ONE version. The field still parses (this
file's parse tests below are unchanged in shape) but its `>= 1` validation
is GONE — a retired key must not reject a config — and setting it now
emits a `DeprecationWarning` once at load, mirroring
`test_stream_repaint_interval_config.py`'s own Tier 1/2 shape for a
sibling `chat.*` rejection warning (unit-level via `_build_chat_config`,
then a real `load_config()` round trip). Removing the field/schema itself,
and registering the key in `check_retired_config_keys_denylist.py`'s
denylist, is a disclosed, separate follow-up.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml

from reyn.config.chat import CompactionConfig, _build_chat_config
from reyn.config.loader import load_config


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_max_shrink_iterations_defaults_to_8() -> None:
    """Tier 1: default is 8 — the retry_loop signature default this knob
    exposes, unchanged behavior for every deployment that never sets it."""
    assert CompactionConfig().max_shrink_iterations == 8


def test_chat_compaction_max_shrink_iterations_parses() -> None:
    """Tier 1: `chat.compaction.max_shrink_iterations: N` in reyn.yaml
    still sets the field — #5623 kept parsing for one version, only the
    validation and the "has an effect" claim are gone."""
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


def test_max_shrink_iterations_below_1_no_longer_raises() -> None:
    """Tier 1: #5623 — falsification pair for the pre-#5623 `>= 1` floor
    this field used to enforce at construction. A retired key must not
    reject a config over a value nothing reads any more, so both values
    that used to raise now construct cleanly, carrying whatever value was
    given."""
    assert CompactionConfig(max_shrink_iterations=0).max_shrink_iterations == 0
    assert CompactionConfig(max_shrink_iterations=-1).max_shrink_iterations == -1


def test_setting_max_shrink_iterations_warns_it_is_retired() -> None:
    """Tier 1: #5623 — explicitly setting the key emits a `DeprecationWarning`
    naming the field and #5531, the real warning/log surface
    `_build_chat_config`'s sibling deprecations already use (`ask_on_exceed`,
    `extension_calls`, the #1128 removed-compaction-keys group) — no
    MagicMock/patch, this is the real `warnings` call the parser makes."""
    with pytest.warns(DeprecationWarning, match="max_shrink_iterations"):
        _build_chat_config({"compaction": {"max_shrink_iterations": 3}})


def test_omitting_max_shrink_iterations_is_silent() -> None:
    """Tier 1: deny sibling — the warning fires on the key's PRESENCE, not
    on every compaction parse. Without this pair, a parser that warned
    unconditionally would also pass the test above."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _build_chat_config({"compaction": {"body_token_cap": 5000}})


def test_max_shrink_iterations_in_reyn_yaml_warns_at_real_load(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5623 — a real `reyn.yaml` on disk, loaded through
    `load_config()` (not just `_build_chat_config` in isolation — the same
    #4899 gap `test_stream_repaint_interval_config.py`'s own Tier 2 pair
    guards), surfaces the retirement warning on the real surface an
    operator's own session would see, and the value still parses through
    (kept for one version, not rejected)."""
    monkeypatch.chdir(tmp_path)
    _write_yaml(
        tmp_path / "reyn.yaml",
        {"chat": {"compaction": {"max_shrink_iterations": 3}}},
    )

    with pytest.warns(DeprecationWarning, match="max_shrink_iterations"):
        cfg = load_config(tmp_path)

    assert cfg.chat.compaction.max_shrink_iterations == 3


def test_absent_from_reyn_yaml_emits_no_retirement_warning_at_real_load(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: deny sibling for the real-load test above — a `reyn.yaml`
    that never sets the retired key loads through the same real
    `load_config()` entrypoint with no retirement warning, and the default
    behaviour (the field's own shipped default) is unchanged."""
    monkeypatch.chdir(tmp_path)
    _write_yaml(
        tmp_path / "reyn.yaml",
        {"chat": {"compaction": {"body_token_cap": 5000}}},
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config(tmp_path)

    assert cfg.chat.compaction.max_shrink_iterations == 8
    assert not any("max_shrink_iterations" in str(w.message) for w in caught)
