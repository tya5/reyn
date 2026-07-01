"""Tier 2: pure helpers in core/compiler/.

``linter._is_snake``                  — snake_case name validation regex
``shape_renderer._key_to_placeholder``     — UPPER_CASE_FROM_ARTIFACT placeholder
``shape_renderer._replace_string_values``  — recursive leaf-string → placeholder
``shape_renderer._transform_shape_only_block`` — JSON parse + replace, invalid → passthrough
``parser._split_frontmatter``              — split ---frontmatter--- from body
``parser._parse_graph_node``               — @skill_name[workspace] token parse
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.compiler.linter import _is_snake
from reyn.core.compiler.parser import _parse_graph_node, _split_frontmatter
from reyn.core.compiler.shape_renderer import (
    _key_to_placeholder,
    _replace_string_values,
    _transform_shape_only_block,
)

# ---------------------------------------------------------------------------
# linter._is_snake
# ---------------------------------------------------------------------------


def test_is_snake_valid_simple() -> None:
    """Tier 2: simple lowercase word is snake_case."""
    assert _is_snake("foo") is True


def test_is_snake_valid_with_underscores() -> None:
    """Tier 2: lowercase with underscores is snake_case."""
    assert _is_snake("foo_bar_baz") is True


def test_is_snake_valid_with_digits() -> None:
    """Tier 2: lowercase with digits is snake_case."""
    assert _is_snake("foo2bar") is True


def test_is_snake_rejects_uppercase() -> None:
    """Tier 2: any uppercase letter fails snake_case check."""
    assert _is_snake("FooBar") is False


def test_is_snake_rejects_hyphen() -> None:
    """Tier 2: hyphen fails snake_case check."""
    assert _is_snake("foo-bar") is False


def test_is_snake_rejects_leading_digit() -> None:
    """Tier 2: name starting with digit fails."""
    assert _is_snake("1foo") is False


def test_is_snake_rejects_empty() -> None:
    """Tier 2: empty string is not snake_case."""
    assert _is_snake("") is False


# ---------------------------------------------------------------------------
# shape_renderer._key_to_placeholder
# ---------------------------------------------------------------------------


def test_key_to_placeholder_simple() -> None:
    """Tier 2: 'instance_id' → '<INSTANCE_ID_FROM_ARTIFACT>'."""
    assert _key_to_placeholder("instance_id") == "<INSTANCE_ID_FROM_ARTIFACT>"


def test_key_to_placeholder_already_upper() -> None:
    """Tier 2: already uppercase key produces correct form."""
    assert _key_to_placeholder("BASE_COMMIT") == "<BASE_COMMIT_FROM_ARTIFACT>"


def test_key_to_placeholder_single_word() -> None:
    """Tier 2: single lowercase word → uppercase wrapped."""
    assert _key_to_placeholder("title") == "<TITLE_FROM_ARTIFACT>"


# ---------------------------------------------------------------------------
# shape_renderer._replace_string_values
# ---------------------------------------------------------------------------


def test_replace_string_values_flat_dict() -> None:
    """Tier 2: flat dict replaces string values, preserves keys."""
    result = _replace_string_values({"instance_id": "abc", "base_commit": "def"})
    assert result == {
        "instance_id": "<INSTANCE_ID_FROM_ARTIFACT>",
        "base_commit": "<BASE_COMMIT_FROM_ARTIFACT>",
    }


def test_replace_string_values_nested_dict() -> None:
    """Tier 2: nested dict — string leaves replaced, keys preserved at all levels."""
    result = _replace_string_values({"outer": {"inner_key": "val"}})
    assert result == {"outer": {"inner_key": "<INNER_KEY_FROM_ARTIFACT>"}}


def test_replace_string_values_list_items() -> None:
    """Tier 2: list string items get positional placeholders."""
    result = _replace_string_values(["x", "y"])
    assert result == ["<ITEM_0_FROM_ARTIFACT>", "<ITEM_1_FROM_ARTIFACT>"]


def test_replace_string_values_non_string_leaf_unchanged() -> None:
    """Tier 2: int, bool, None leaves are returned as-is."""
    result = _replace_string_values({"count": 42, "flag": True, "nothing": None})
    assert result == {"count": 42, "flag": True, "nothing": None}


def test_replace_string_values_non_container_passthrough() -> None:
    """Tier 2: non-container (int) passes through."""
    assert _replace_string_values(99) == 99


# ---------------------------------------------------------------------------
# shape_renderer._transform_shape_only_block
# ---------------------------------------------------------------------------


def test_transform_shape_only_block_replaces_string_values() -> None:
    """Tier 2: valid JSON block — string values replaced with placeholders."""
    body = '{"problem_statement": "fix the bug"}'
    result = _transform_shape_only_block(body)
    import json

    parsed = json.loads(result)
    assert parsed["problem_statement"] == "<PROBLEM_STATEMENT_FROM_ARTIFACT>"


def test_transform_shape_only_block_invalid_json_passthrough() -> None:
    """Tier 2: invalid JSON block returned unchanged (no crash, no loss)."""
    body = "{not valid json"
    result = _transform_shape_only_block(body)
    assert result == body


def test_transform_shape_only_block_preserves_non_string_fields() -> None:
    """Tier 2: numeric and boolean values in JSON block are left untouched."""
    body = '{"count": 5, "flag": true}'
    result = _transform_shape_only_block(body)
    import json

    parsed = json.loads(result)
    assert parsed["count"] == 5
    assert parsed["flag"] is True


# ---------------------------------------------------------------------------
# parser._split_frontmatter
# ---------------------------------------------------------------------------


def test_split_frontmatter_valid() -> None:
    """Tier 2: standard ---frontmatter--- yields parsed dict and body."""
    text = "---\nname: my_phase\ncan_finish: true\n---\nBody text here."
    fm, body = _split_frontmatter(text)
    assert fm == {"name": "my_phase", "can_finish": True}
    assert body == "Body text here."


def test_split_frontmatter_no_frontmatter() -> None:
    """Tier 2: text without opening '---' → empty dict + full text."""
    text = "Just plain text."
    fm, body = _split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_split_frontmatter_unclosed_returns_empty() -> None:
    """Tier 2: opening '---' with no closing '---' → empty dict + full text."""
    text = "---\nname: foo\nno close"
    fm, body = _split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_split_frontmatter_empty_frontmatter() -> None:
    """Tier 2: '---\\n---' (empty frontmatter) → empty dict."""
    text = "---\n---\nBody."
    fm, body = _split_frontmatter(text)
    assert fm == {}
    assert body == "Body."


def test_split_frontmatter_no_body() -> None:
    """Tier 2: frontmatter with no body → empty string body."""
    text = "---\nname: foo\n---"
    fm, body = _split_frontmatter(text)
    assert fm["name"] == "foo"
    assert body == ""


# ---------------------------------------------------------------------------
# parser._parse_graph_node
# ---------------------------------------------------------------------------


def test_parse_graph_node_plain_phase() -> None:
    """Tier 2: plain phase name → (token, None)."""
    name, node = _parse_graph_node("my_phase")
    assert name == "my_phase"
    assert node is None


def test_parse_graph_node_at_skill_isolated_default() -> None:
    """Tier 2: '@skill_name' without bracket → workspace defaults to isolated."""
    name, node = _parse_graph_node("@my_skill")
    assert name == "@my_skill"
    assert node is not None
    assert node.skill_name == "my_skill"
    assert node.workspace == "isolated"


def test_parse_graph_node_at_skill_explicit_shared() -> None:
    """Tier 2: '@skill_name[shared]' → workspace='shared'."""
    name, node = _parse_graph_node("@my_skill[shared]")
    assert name == "@my_skill"
    assert node is not None
    assert node.workspace == "shared"


def test_parse_graph_node_at_skill_explicit_isolated() -> None:
    """Tier 2: '@skill_name[isolated]' → workspace='isolated'."""
    name, node = _parse_graph_node("@my_skill[isolated]")
    assert node is not None
    assert node.workspace == "isolated"
