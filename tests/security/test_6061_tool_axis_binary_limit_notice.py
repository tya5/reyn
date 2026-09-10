"""Tier 2: #6061 ⑵ — an operator-facing notice for the tool axis's own
real limit: it restricts each exec segment's resolved `argv[0]` name,
never the binary that actually runs once a wrapper (`env`/`xargs`/
`sh -c`/`timeout`/`nice`) is involved (#6061's own root-cause finding).
NOT a fix that closes the gap -- the doc row this notice quotes states
the SAME limit; this is the runtime-visible half.

Routed through `logging` at WARNING (lead-coder BLOCKING on PR #6064:
a bare `print(..., file=sys.stderr)` would have been the exact class
#6043 closed -- `resolved_profile_for`'s own caller, `chat._session_
factory`, runs on EVERY session construction, including a spawn/attach
AFTER the inline TUI already owns the terminal). Checked via `caplog`,
never `capsys` -- the whole point of this fix is that nothing here
writes to stderr directly while the TUI is live.

Real `AgentRegistry` + real on-disk per-session `config.yaml` throughout
(same idiom as `test_2103_s1a_per_session_config.py`, this file's own
established sibling) -- no mocks. `resolved_profile_for` is driven
directly, never a private-state peek at `_tool_axis_binary_limit_warned`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from reyn.runtime.registry import AgentRegistry

_LOGGER_NAME = "reyn.runtime.registry"
_NOTICE_SNIPPET = "it can only check each command's own argv[0]"


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: None,
    )


def _write_per_session(reg: AgentRegistry, name: str, sid: str, body: str) -> None:
    d = reg._session_state_dir(name, sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(body, encoding="utf-8")


def _warned(caplog) -> bool:
    return any(
        r.name == _LOGGER_NAME and _NOTICE_SNIPPET in r.message
        for r in caplog.records
    )


# ── accept ⑴: no narrowing → no notice ───────────────────────────────────────


def test_no_narrowing_warns_nothing(tmp_path: Path, caplog) -> None:
    """Tier 2: a plain agent with no narrowing at all never reaches the
    notice at all -- the default (unrestricted) operator must never see
    it."""
    reg = _registry(tmp_path)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reg.resolved_profile_for("plain")
    assert not _warned(caplog)


# ── accept ⑵: narrowing present, but exec itself is denied → no notice ──────


def test_exec_itself_denied_warns_nothing(tmp_path: Path, caplog) -> None:
    """Tier 2: a narrowing that denies `exec` outright makes this limit
    irrelevant to that operator -- `exec` can't run at all, so nothing
    about ITS OWN argv[0]-only visibility matters."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [exec, ls]\n")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reg.resolved_profile_for("worker", sid="task1")
    assert not _warned(caplog)


# ── accept ⑶ (the subject): a binary-looking name in the narrowing → notice ──


def test_narrowing_a_non_registered_name_warns(tmp_path: Path, caplog) -> None:
    """Tier 2: #6061's own subject -- a narrowing that denies "ls" (not a
    registered reyn tool name, so it can only be pointing at a BINARY the
    operator meant to restrict via exec) fires the notice.

    Strip-falsify (verified by hand: `_maybe_warn_tool_axis_binary_limit`'s
    own `if narrowed_names <= registered: return` forced to always
    `return` unconditionally): this test goes red -- no notice at all."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [ls]\n")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reg.resolved_profile_for("worker", sid="task1")
    assert _warned(caplog)


# ── accept ⑷: narrowing only registered reyn tool names → no notice ─────────


def test_narrowing_only_registered_reyn_tools_warns_nothing(tmp_path: Path, caplog) -> None:
    """Tier 2: an operator who narrowed ONLY real reyn tool names (never a
    binary) must not see this notice -- it would be pure noise for them,
    and (architect's own reasoning) noise here means the ONE time it
    would matter for a DIFFERENT operator gets read past too."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [read_file]\n")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reg.resolved_profile_for("worker", sid="task1")
    assert not _warned(caplog)


# ── accept ⑸: keyed on the COMPOSED narrowing, never a process-lived bool ───


def test_the_same_narrowing_composed_twice_notices_once_a_different_one_notices_again(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: #6061 ⑵'s own load-bearing property (architect review,
    #6058/#6059 same night) -- "once" is per DISTINCT composed narrowing,
    never a bare process-lifetime bool. The SAME agent+session composing
    the SAME narrowing twice (a config hot-reload re-reading unchanged
    config, or a second `resolved_profile_for` call for the same
    session) must notice only the first time; a genuinely DIFFERENT
    narrowing (a different agent here, standing in for a hot-reloaded
    config producing a different one) must notice again.

    Strip-falsify (verified by hand: the dedup key changed from
    `(contextual.tool_allow, contextual.tool_deny)` to a bare module- or
    instance-level `bool`): the SECOND half of this test goes red -- the
    second, genuinely different narrowing stays silent too."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [ls]\n")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reg.resolved_profile_for("worker", sid="task1")
        assert _warned(caplog), "first composition must notice"
        caplog.clear()

        reg.resolved_profile_for("worker", sid="task1")
        assert not _warned(caplog), (
            "the SAME composed narrowing, recomposed, must not notice a second time"
        )
        caplog.clear()

        _write_per_session(reg, "other", "task1", "name: s\ntool_deny: [xargs]\n")
        reg.resolved_profile_for("other", sid="task1")
        assert _warned(caplog), (
            "a genuinely DIFFERENT composed narrowing must notice again -- "
            "if the dedup key were a process-wide bool instead of the "
            "narrowing's own content, this would stay silent too"
        )


# ── accept ⑹: the doc row carries the required text ─────────────────────────


def test_permission_model_doc_states_the_tool_axis_limit() -> None:
    """Tier 2: doc-drift guard -- `permission-model.md`'s `tool` axis row
    states the SAME limit this runtime notice does, so the promise and
    the runtime-visible behaviour cannot silently diverge again."""
    from tests._support.paths import REPO_ROOT

    doc = (REPO_ROOT / "docs/concepts/runtime/permission-model.md").read_text(encoding="utf-8")
    assert "a wrapper binary defeats this axis" in doc
    assert "argv[0]" in doc.split("| `tool` |", 1)[1].split("| `shell` |", 1)[0]
