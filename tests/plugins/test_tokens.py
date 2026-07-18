"""Tier 1: Contract — ``${REYN_*}`` / ``${CLAUDE_*}`` token expansion (ADR 0064 §3.4/§3.5/§3.6, #3067).

Pins: each canonical token expands to its real ``PluginTokenContext`` value;
the ``${CLAUDE_*}`` alias only fires when ``alias_claude=True`` (ingestion
boundary, §3.6); ``SKILL_DIR`` and ``PLUGIN_ROOT`` resolve to DIFFERENT
values and neither collapses into the other (§3.4/§3.6's named distinction);
an unrecognised token (owned by a different expansion layer, e.g.
``expand_env``'s ``os.environ`` vars) is left untouched, proving the two
layers are separate and compose rather than collide.
"""
from __future__ import annotations

from pathlib import Path

from reyn.plugins.tokens import PluginTokenContext, expand_reyn_tokens


def _ctx(tmp_path: Path) -> PluginTokenContext:
    return PluginTokenContext(
        plugin_root=tmp_path / "plugins" / "rag",
        project_dir=tmp_path / "my-project",
        skill_dir=tmp_path / "plugins" / "rag" / "skills" / "rag-search",
    )


def test_expands_each_canonical_reyn_token(tmp_path):
    """Tier 1: each of the three canonical ``${REYN_*}`` tokens expands to
    its real ``PluginTokenContext`` field value."""
    ctx = _ctx(tmp_path)
    obj = {
        "root": "${REYN_PLUGIN_ROOT}/server.py",
        "skill": "${REYN_SKILL_DIR}/SKILL.md",
        "project": "${REYN_PROJECT_DIR}/config.yaml",
    }

    expanded = expand_reyn_tokens(obj, ctx)

    assert expanded["root"] == f"{ctx.plugin_root}/server.py"
    assert expanded["skill"] == f"{ctx.skill_dir}/SKILL.md"
    assert expanded["project"] == f"{ctx.project_dir}/config.yaml"


def test_skill_dir_and_plugin_root_are_distinct_values(tmp_path):
    """Tier 1: §3.4/§3.6 'the SKILL_DIR vs PLUGIN_ROOT distinction' must not
    be collapsed — expanding both tokens must not yield the same string."""
    ctx = _ctx(tmp_path)
    obj = "${REYN_PLUGIN_ROOT}|${REYN_SKILL_DIR}"

    expanded = expand_reyn_tokens(obj, ctx)

    root_str, skill_str = expanded.split("|")
    assert root_str == str(ctx.plugin_root)
    assert skill_str == str(ctx.skill_dir)
    assert root_str != skill_str


def test_skill_dir_left_unexpanded_when_absent_from_context(tmp_path):
    """Tier 1: outside a skill-load context (mcp config / pipeline yaml),
    ``skill_dir`` is None — ``${REYN_SKILL_DIR}`` must NOT silently default
    to plugin_root; it is left as a literal token for a later pass."""
    ctx = PluginTokenContext(
        plugin_root=tmp_path / "plugins" / "rag",
        project_dir=tmp_path / "my-project",
    )

    expanded = expand_reyn_tokens("${REYN_SKILL_DIR}/x", ctx)

    assert expanded == "${REYN_SKILL_DIR}/x"


def test_claude_alias_not_expanded_by_default(tmp_path):
    """Tier 1: §3.6 the alias only fires at the ingestion-of-Claude-authored-
    content boundary — never unconditionally."""
    ctx = _ctx(tmp_path)

    expanded = expand_reyn_tokens("${CLAUDE_PLUGIN_ROOT}/server.py", ctx)

    assert expanded == "${CLAUDE_PLUGIN_ROOT}/server.py"


def test_claude_alias_expands_to_same_value_as_reyn_token_when_ingesting(tmp_path):
    """Tier 1: with ``alias_claude=True``, each ``${CLAUDE_*}`` alias expands
    to the SAME value as its canonical ``${REYN_*}`` counterpart."""
    ctx = _ctx(tmp_path)
    obj = {
        "root": "${CLAUDE_PLUGIN_ROOT}",
        "skill": "${CLAUDE_SKILL_DIR}",
        "project": "${CLAUDE_PROJECT_DIR}",
    }

    expanded = expand_reyn_tokens(obj, ctx, alias_claude=True)

    assert expanded["root"] == str(ctx.plugin_root)
    assert expanded["skill"] == str(ctx.skill_dir)
    assert expanded["project"] == str(ctx.project_dir)


def test_unrecognised_token_left_untouched_for_other_expansion_layer(tmp_path):
    """Tier 1: a token this layer doesn't own (e.g. an ``expand_env``
    os.environ var, or a pipeline ``ctx`` param) must be left as-is — proves
    the two expansion layers compose instead of one clobbering the other's
    syntax."""
    ctx = _ctx(tmp_path)

    expanded = expand_reyn_tokens("${SOME_OTHER_ENV_VAR}", ctx)

    assert expanded == "${SOME_OTHER_ENV_VAR}"


def test_recurses_into_nested_dict_and_list(tmp_path):
    """Tier 1: expansion recurses into nested dict values and list items
    (the whole-config-tree shape mcp/pipeline configs actually have)."""
    ctx = _ctx(tmp_path)
    obj = {
        "env": {"PYTHONPATH": "${REYN_PLUGIN_ROOT}/lib"},
        "args": ["--root", "${REYN_PLUGIN_ROOT}"],
    }

    expanded = expand_reyn_tokens(obj, ctx)

    assert expanded["env"]["PYTHONPATH"] == f"{ctx.plugin_root}/lib"
    assert expanded["args"] == ["--root", str(ctx.plugin_root)]


def test_non_string_scalars_returned_unchanged(tmp_path):
    """Tier 1: non-string scalars (int, bool, None) pass through unchanged,
    mirroring ``expand_env``'s shape."""
    ctx = _ctx(tmp_path)
    obj = {"count": 3, "enabled": True, "nothing": None}

    expanded = expand_reyn_tokens(obj, ctx)

    assert expanded == obj
