"""Tier 2: ``reyn run`` CLI input parser auto-wrap contract.

The parser's job is to turn a CLI-supplied input string (JSON or natural
language) into a Reyn artifact dict. When the JSON form is a bare data
dict (= no ``type`` envelope) and the skill loader has identified the
entry phase's wrapped artifact type, the parser auto-wraps it as
``{"type": <name>, "data": <bare>}``. This is the path that lets the
README's RAG demo

    reyn run index_docs '{"source":...,"path":...,"description":...}'

work without forcing readers to know the artifact contract.

The invariant pinned here is the parser contract — pure function, no
filesystem, no LLM. End-to-end CLI behaviour is exercised separately
through manual run smoke (see PR description).
"""
from __future__ import annotations

from reyn.interfaces.cli.commands.run import _parse_cli_input


def test_bare_dict_wraps_with_default_type():
    """Tier 2: bare data dict + default_type → wrapped artifact."""
    out = _parse_cli_input(
        '{"source":"my_docs","path":"docs/**/*.md"}',
        default_type="index_docs_input",
    )
    assert out == {
        "type": "index_docs_input",
        "data": {"source": "my_docs", "path": "docs/**/*.md"},
    }


def test_already_wrapped_passes_through():
    """Tier 2: a dict that already carries ``type`` is not re-wrapped."""
    raw = '{"type":"index_docs_input","data":{"source":"x"}}'
    out = _parse_cli_input(raw, default_type="index_docs_input")
    assert out == {
        "type": "index_docs_input",
        "data": {"source": "x"},
    }


def test_no_default_type_passes_dict_through():
    """Tier 2: with no default_type the parser does not invent one."""
    out = _parse_cli_input('{"foo":"bar"}', default_type=None)
    assert out == {"foo": "bar"}


def test_non_json_text_wraps_as_user_message():
    """Tier 2: non-JSON text becomes a user_message regardless of default_type."""
    out = _parse_cli_input("hello world", default_type="index_docs_input")
    assert out == {"type": "user_message", "data": {"text": "hello world"}}


def test_bare_dict_with_type_key_passes_through():
    """Tier 2: a dict that already carries ``type`` is left alone even
    when the supplied default_type would have wrapped it differently."""
    out = _parse_cli_input(
        '{"type":"other_artifact","data":{"foo":"bar"}}',
        default_type="index_docs_input",
    )
    assert out["type"] == "other_artifact"
