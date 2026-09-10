"""Tier 2: #6061 ⑵ — an operator-facing notice for the tool axis's own
real limit: it restricts each exec segment's resolved `argv[0]` name,
never the binary that actually runs once a wrapper (`env`/`xargs`/
`sh -c`/`timeout`/`nice`) is involved (#6061's own root-cause finding).
NOT a fix that closes the gap -- the doc row this notice quotes states
the SAME limit; this is the runtime-visible half.

Real `AgentRegistry` + real on-disk per-session `config.yaml` throughout
(same idiom as `test_2103_s1a_per_session_config.py`, this file's own
established sibling) -- no mocks. `resolved_profile_for` is driven
directly, never a private-state peek at `_tool_axis_binary_limit_warned`.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.registry import AgentRegistry

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


# ── accept ⑴: no narrowing → no notice ───────────────────────────────────────


def test_no_narrowing_prints_nothing(tmp_path: Path, capsys) -> None:
    """Tier 2: a plain agent with no narrowing at all never reaches the
    notice at all -- the default (unrestricted) operator must never see
    it."""
    reg = _registry(tmp_path)
    reg.resolved_profile_for("plain")
    assert _NOTICE_SNIPPET not in capsys.readouterr().err


# ── accept ⑵: narrowing present, but exec itself is denied → no notice ──────


def test_exec_itself_denied_prints_nothing(tmp_path: Path, capsys) -> None:
    """Tier 2: a narrowing that denies `exec` outright makes this limit
    irrelevant to that operator -- `exec` can't run at all, so nothing
    about ITS OWN argv[0]-only visibility matters."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [exec, ls]\n")
    reg.resolved_profile_for("worker", sid="task1")
    assert _NOTICE_SNIPPET not in capsys.readouterr().err


# ── accept ⑶ (the subject): a binary-looking name in the narrowing → notice ──


def test_narrowing_a_non_registered_name_prints_the_notice(tmp_path: Path, capsys) -> None:
    """Tier 2: #6061's own subject -- a narrowing that denies "ls" (not a
    registered reyn tool name, so it can only be pointing at a BINARY the
    operator meant to restrict via exec) fires the notice.

    Strip-falsify (verified by hand: `_maybe_warn_tool_axis_binary_limit`'s
    own `if narrowed_names <= registered: return` forced to always
    `return` unconditionally): this test goes red -- no notice at all."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [ls]\n")
    reg.resolved_profile_for("worker", sid="task1")
    assert _NOTICE_SNIPPET in capsys.readouterr().err


# ── accept ⑷: narrowing only registered reyn tool names → no notice ─────────


def test_narrowing_only_registered_reyn_tools_prints_nothing(tmp_path: Path, capsys) -> None:
    """Tier 2: an operator who narrowed ONLY real reyn tool names (never a
    binary) must not see this notice -- it would be pure noise for them,
    and (architect's own reasoning) noise here means the ONE time it
    would matter for a DIFFERENT operator gets read past too."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [read_file]\n")
    reg.resolved_profile_for("worker", sid="task1")
    assert _NOTICE_SNIPPET not in capsys.readouterr().err


# ── accept ⑸: keyed on the COMPOSED narrowing, never a process-lived bool ───


def test_the_same_narrowing_composed_twice_notices_once_a_different_one_notices_again(
    tmp_path: Path, capsys,
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

    reg.resolved_profile_for("worker", sid="task1")
    assert _NOTICE_SNIPPET in capsys.readouterr().err, "first composition must notice"

    reg.resolved_profile_for("worker", sid="task1")
    assert _NOTICE_SNIPPET not in capsys.readouterr().err, (
        "the SAME composed narrowing, recomposed, must not notice a second time"
    )

    _write_per_session(reg, "other", "task1", "name: s\ntool_deny: [xargs]\n")
    reg.resolved_profile_for("other", sid="task1")
    assert _NOTICE_SNIPPET in capsys.readouterr().err, (
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
