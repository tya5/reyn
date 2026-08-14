"""Tier 1/2: #4756 — ``_dict_detail_lines``'s own pure-function behavior.

Direct, fast unit coverage of the function ``_result_detail_lines`` (the
tool-detail-fold expander, #3508) delegates to for a dict result — the full
app-driven integration coverage (real multi-line unfold + the
``_EXPANDED_MAX_LINES`` cap engaging) lives in
``test_tool_detail_on_highlight_3508.py``, alongside this fix's sibling
mechanisms; this file is the cheaper, more exhaustive complement.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.presenter import _dict_detail_lines


def test_multiline_field_splits_onto_real_lines() -> None:
    """Tier 1: contract — the exact #4756 issue repro (``sandboxed_exec``'s
    ``stdout``). Real newlines, not one JSON-escaped line."""
    result = {
        "kind": "sandboxed_exec", "status": "ok", "backend": "landlock",
        "returncode": 0, "stdout": "line 1\nline 2\nline 3\n", "stderr": "",
        "truncated": False, "denial_class": None, "argv0_resolved": "/bin/bash",
    }
    lines = _dict_detail_lines(result)
    assert "line 1" in lines
    assert "line 2" in lines
    assert "line 3" in lines
    assert not any("\\n" in line for line in lines), (
        "a real newline must not survive as a literal backslash-n in any line"
    )


def test_second_repro_read_file_content_field() -> None:
    """Tier 1: the issue's second confirmed shape (``read_file``'s
    ``content``) — same fix, same code path, independently exercised."""
    result = {
        "op": "read", "path": "foo.py", "status": "ok",
        "content": "def a():\n    pass\n\ndef b():\n    pass\n",
    }
    lines = _dict_detail_lines(result)
    assert "def a():" in lines
    assert "    pass" in lines
    assert "def b():" in lines


def test_non_string_fields_stay_compact_json() -> None:
    """Tier 1: accept-side — a field that is NOT a multi-line string (int,
    bool, None, a short string) keeps the ordinary compact ``"key": value``
    JSON rendering, unaffected by the multi-line special case."""
    result = {"returncode": 1, "truncated": False, "denial_class": None, "path": "a.py"}
    lines = _dict_detail_lines(result)
    joined = "\n".join(lines)
    assert '"returncode": 1' in joined
    assert '"truncated": false' in joined
    assert '"denial_class": null' in joined
    assert '"path": "a.py"' in joined


def test_single_line_string_field_stays_on_one_line() -> None:
    """Tier 1: accept-side — a string field with NO newline is not
    special-cased into the multi-line block form; it stays a normal
    one-line JSON string value."""
    result = {"path": "a.py", "status": "ok"}
    lines = _dict_detail_lines(result)
    assert '  "path": "a.py",' in lines


def test_empty_dict_renders_empty_braces() -> None:
    """Tier 1: accept-side — an empty dict result renders as ``{}``, not an
    empty line list (which would collapse to nothing on screen)."""
    assert _dict_detail_lines({}) == ["{}"]


def test_multiline_field_is_neutralized_before_splitting() -> None:
    """Tier 1: THE mandatory security witness (lead-coder review, #4757) —
    ``json.dumps`` (what this function replaces) incidentally neutralized
    terminal-control bytes in the value (it escapes ``\\x1b`` etc. to
    ``\\u001b``, an accidental side effect, not a designed defense, but a
    real one). ``exec``'s ``stdout`` / ``read_file``'s ``content`` are
    ARBITRARY BYTES FROM THE WORLD, not operator-typed ``reyn.yaml`` text
    — the same FP-0054 rule ``_neutralized_label`` (this module) already
    states. A multi-line field must be neutralized via the SAME
    ``get_neutralizer("terminal")`` seam BEFORE splitting, so an ESC/CSI/
    OSC control sequence embedded in real-world exec output or file
    content cannot reach the terminal raw."""
    malicious = "before\x1b[2Jinjected\nafter"
    lines = _dict_detail_lines({"stdout": malicious})
    joined = "\n".join(lines)
    assert "\x1b" not in joined, "an ESC byte reached the rendered lines unneutralized"
    assert "before" in joined
    assert "injected" in joined
    assert "after" in joined


def test_multiline_field_neutralize_preserves_real_newlines() -> None:
    """Tier 1: accept-side of the same witness — the terminal neutralizer
    explicitly preserves tab/newline/carriage-return (its own control-char
    regex excludes them), so neutralizing BEFORE splitting must not
    collapse the real line breaks the whole #4756 fix exists to
    preserve."""
    result = {"stdout": "line one\nline two\nline three"}
    lines = _dict_detail_lines(result)
    assert "line one" in lines
    assert "line two" in lines
    assert "line three" in lines


def test_result_detail_lines_delegates_to_dict_detail_lines_for_dicts() -> None:
    """Tier 2: THE wiring witness — ``_result_detail_lines`` (what the
    presenter's expand path actually calls) routes a dict result through
    ``_dict_detail_lines``, not the old flat ``json.dumps``. RED without
    the wiring: a multi-line field would collapse to one escaped line."""
    from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_META_KEY
    from reyn.interfaces.inline.textual_chat.presenter import _result_detail_lines
    from reyn.runtime.outbox import OutboxMessage

    msg = OutboxMessage(
        kind="tool_call_started", text="exec",
        meta={RESULT_META_KEY: {"result": {
            "kind": "sandboxed_exec", "status": "ok", "returncode": 0,
            "stdout": "alpha\nbeta\ngamma", "stderr": "",
        }}},
    )
    lines = _result_detail_lines(msg)
    assert "alpha" in lines
    assert "beta" in lines
    assert "gamma" in lines
    assert not any("\\n" in line for line in lines)


def test_bare_string_result_is_also_neutralized() -> None:
    """Tier 1: THE mandatory security witness's sibling leg (lead-coder's
    own follow-up sweep, #4757) — a BARE STRING result (the
    ``isinstance(result, str)`` branch) never went through ``json.dumps``
    at all, so it was open to the SAME unneutralized-control-byte gap
    independently of, and predating, #4756's own dict fix — same
    function, same seam. Closed in the same PR rather than left open one
    branch over."""
    from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_META_KEY
    from reyn.interfaces.inline.textual_chat.presenter import _result_detail_lines
    from reyn.runtime.outbox import OutboxMessage

    msg = OutboxMessage(
        kind="tool_call_started", text="some_tool",
        meta={RESULT_META_KEY: {"result": "before\x1b[2Jinjected\nafter"}},
    )
    lines = _result_detail_lines(msg)
    joined = "\n".join(lines)
    assert "\x1b" not in joined, "an ESC byte reached the rendered lines unneutralized"
    assert "before" in joined
    assert "injected" in joined
    assert "after" in joined
