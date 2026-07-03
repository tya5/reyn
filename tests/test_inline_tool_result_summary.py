"""Tier 2: tool-result summary — CC-style one-liners + graceful fallback.

`summarize_tool_result` turns a raw op result into a human line (``Read 42
lines``) per tool/shape, and ALWAYS degrades gracefully on an unknown / empty /
oversized / malformed result (never raises, never dumps raw for a known shape).
"""
from __future__ import annotations

from reyn.interfaces.repl.renderer import summarize_tool_result


def test_file_read_reports_line_count() -> None:
    """Tier 2: a file read summarises to 'Read N lines'."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "ok", "content": "a\nb\nc"}
    )
    assert out == "Read 3 lines"


def test_read_with_none_content_no_status_is_clean_not_raw_repr() -> None:
    """Tier 2: a read result whose content is None and carries no status gets a
    clean note, not a raw-dict dump."""
    out = summarize_tool_result("file__read", {"op": "read", "content": None})
    assert out == "Read (no content)"
    assert "{" not in out and "None" not in out


def test_read_with_none_content_but_status_shows_status() -> None:
    """Tier 2: a read that errored (content None) still surfaces its status."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "content": None, "status": "error"}
    )
    assert out == "error"


def test_file_read_singular_line() -> None:
    """Tier 2: a single-line read says 'Read 1 line', not 'Read 1 lines'."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "ok", "content": "only one line"}
    )
    assert out == "Read 1 line"


def test_file_read_truncated_is_flagged() -> None:
    """Tier 2: a truncated read says so."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "truncated", "content": "x\ny"}
    )
    assert "truncated" in out


def test_file_write_and_edit_name_the_path() -> None:
    """Tier 2: write / edit name the path."""
    assert summarize_tool_result("file__write", {"op": "write", "path": "f.py"}) == "Wrote f.py"
    assert summarize_tool_result("file__edit", {"op": "edit", "path": "g.py"}) == "Edited g.py"


def test_file_create_treated_same_as_write() -> None:
    """Tier 2: op='create' uses the same 'Wrote …' branch as 'write'.

    A create op arriving from an MCP tool (or a future OS create op) must
    not fall through to the raw-repr fallback — it shares the 'write|create'
    branch. Pinning this prevents the branch being silently narrowed to
    write-only.
    """
    assert summarize_tool_result("file__create", {"op": "create", "path": "new.py"}) == "Wrote new.py"


def test_write_without_path_degrades_cleanly() -> None:
    """Tier 2: a write result with no path field → 'Wrote file', not a crash."""
    assert summarize_tool_result("file__write", {"op": "write"}) == "Wrote file"


def test_edit_without_path_degrades_cleanly() -> None:
    """Tier 2: an edit result with no path field → 'Edited file', not a crash."""
    assert summarize_tool_result("file__edit", {"op": "edit"}) == "Edited file"


def test_web_search_counts_results() -> None:
    """Tier 2: a search list summarises to 'N results'."""
    assert summarize_tool_result("web__search", ["a", "b", "c"]) == "3 results"


def test_generic_list_counts_items_with_pluralisation() -> None:
    """Tier 2: a generic list → 'N items', singular for one."""
    assert summarize_tool_result("anything", [1, 2]) == "2 items"
    assert summarize_tool_result("anything", ["only"]) == "1 item"


def test_task_list_result_shows_count() -> None:
    """Tier 2: task__list result counts tasks rather than falling through to 'ok'."""
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": []}
    ) == "0 tasks"
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": [{"id": "t1"}]}
    ) == "1 task"
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": [{"id": "t1"}, {"id": "t2"}]}
    ) == "2 tasks"


def test_dict_with_error_key_shows_error_not_raw_repr() -> None:
    """Tier 2: a dict with an 'error' key shows '✗ <error message>', not raw repr.

    `file__list` outside the project returns `{'error': 'glob not permitted…'}`.
    The summarizer must surface the error string with a '✗' failure prefix so the
    ⎿ row renders in red (not dim grey), distinguishing failure from success.
    """
    out = summarize_tool_result(
        "file__list",
        {"error": "glob not permitted: '/tmp/*' (outside project, no read permission)"},
    )
    assert out.startswith("✗"), "error result must carry '✗' failure prefix"
    assert "glob not permitted" in out
    assert "{" not in out, "raw dict repr must not leak into ⎿ row"


def test_file_read_not_found_shows_error_not_zero_lines() -> None:
    """Tier 2: file__read for a missing file shows '✗ <error>', not 'Read 0 lines'.

    file__read returns {"op": "read", "content": "", "error": "file not found: …"}
    for a non-existent path.  Without the error-first guard the read branch fires
    on op=="read" and returns "Read 0 lines" (empty content → 0 lines), which looks
    identical to an empty file and gives the user no signal that the file is absent.
    The '✗' prefix ensures the ⎿ row renders in red (tool_call_completed + ✗ → _CC_ERR).
    """
    out = summarize_tool_result(
        "file__read",
        {"op": "read", "content": "", "error": "file not found: README.md"},
    )
    assert out.startswith("✗"), "error result must carry '✗' failure prefix"
    assert "0 lines" not in out, "missing file must not look like empty file"
    assert "not found" in out or "README" in out, "must surface the error"
    assert "{" not in out, "raw dict repr must not leak"


def test_file_delete_shows_deleted_path() -> None:
    """Tier 2: file__delete result (op='delete', path=...) shows 'Deleted {path}'.

    file__delete returns {"kind": "file", "op": "delete", "path": ..., "status": "ok"}.
    Without this branch op='delete' misses all op checks, falls through to
    status='ok' → shows 'ok' with no path, giving the user no signal of what was
    deleted.
    """
    assert summarize_tool_result(
        "file__delete",
        {"kind": "file", "op": "delete", "path": "src/old.py", "status": "ok", "deleted": True},
    ) == "Deleted src/old.py"
    assert summarize_tool_result(
        "file__delete", {"op": "delete"}
    ) == "Deleted file"


def test_memory_remember_shows_saved_slug() -> None:
    """Tier 2: memory_operation__remember_* result ({saved, layer, path}) shows 'Saved {slug}'.

    Without this branch the raw dict repr leaked into the ⎿ row; the result
    has no 'op', 'status', 'tasks', 'entries', or 'matches' key.
    """
    assert summarize_tool_result(
        "memory_operation__remember_agent",
        {"saved": "my-memory-slug", "layer": "agent", "path": "/.reyn/memory/agent/my-memory-slug.md"},
    ) == "Saved my-memory-slug"


def test_memory_forget_shows_forgot_slug() -> None:
    """Tier 2: memory_operation__forget result ({deleted, layer}) shows 'Forgot {slug}'.

    Without this branch the raw dict repr leaked into the ⎿ row; same missing-key
    condition as the remember case (no 'op', 'status', or list keys).
    """
    assert summarize_tool_result(
        "memory_operation__forget",
        {"deleted": "old-memory-slug", "layer": "shared"},
    ) == "Forgot old-memory-slug"


def test_recall_success_shows_chunk_count() -> None:
    """Tier 2: rag_operation__recall success ({chunks, mode}) shows 'N chunks'.

    recall returns {"chunks": [...], "mode": "recall"} with no op/status/matches/
    entries key, so without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "rag_operation__recall",
        {"chunks": [{"text": "a"}, {"text": "b"}, {"text": "c"}], "mode": "recall"},
    ) == "3 chunks"
    assert summarize_tool_result(
        "rag_operation__recall",
        {"chunks": [{"text": "only"}], "mode": "recall"},
    ) == "1 chunk"
    assert summarize_tool_result(
        "rag_operation__recall",
        {"chunks": [], "mode": "fallback"},
    ) == "0 chunks"


def test_error_message_field_shown_not_raw_dict() -> None:
    """Tier 2: dicts with 'error_message' (no 'error' key) show '✗ <message>'.

    recall validation errors return {"ok": False, "error_kind": ..., "error_message": ...}
    and task_ops returns the same shape for invalid-args/no-context errors.
    Without the error_message fallback both fell through to raw dict repr.
    The '✗' prefix ensures the ⎿ row renders in red (tool_call_completed + ✗ → _CC_ERR).
    """
    out = summarize_tool_result(
        "rag_operation__recall",
        {
            "ok": False,
            "error_kind": "missing_required_arg",
            "error_message": "recall requires ['query']. Available sources are listed …",
            "missing": ["query"],
        },
    )
    assert out.startswith("✗"), "error_message result must carry '✗' failure prefix"
    assert "recall requires" in out
    assert "{" not in out, "raw dict repr must not leak"
    assert "ok" not in out.lower() or "False" not in out, "must not dump raw repr"


def test_mcp_result_shows_content_first_line() -> None:
    """Tier 2: MCP tool result (kind='mcp', content='...') shows first content line.

    MCP op results carry {"kind": "mcp", "status": "ok", "content": <joined text>}.
    Without this branch the status fallback shows the generic 'ok' with no hint of
    what the MCP tool returned.  The first line of content is the minimal useful
    preview — it degrades to 'ok' when content is absent or empty.
    """
    assert summarize_tool_result(
        "mcp__github__get_issue",
        {"kind": "mcp", "status": "ok", "server": "github", "tool": "get_issue",
         "content": "Issue #42: Fix the thing\nBody: more detail here", "media_blocks": []},
    ) == "Issue #42: Fix the thing"
    assert summarize_tool_result(
        "mcp__slack__get_message",
        {"kind": "mcp", "status": "ok", "server": "slack", "tool": "get_message",
         "content": "", "media_blocks": []},
    ) == "ok"


def test_file_list_shows_entry_count() -> None:
    """Tier 2: file__list result ({path, entries}) shows 'Listed N entries'.

    Without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "file__list", {"path": "src/", "entries": ["a.py", "b.py", "c.py"]}
    ) == "Listed 3 entries"
    assert summarize_tool_result(
        "file__list", {"path": "src/", "entries": ["only.py"]}
    ) == "Listed 1 entry"


def test_file_grep_shows_match_count() -> None:
    """Tier 2: file__grep result (op='grep', count=N) shows 'N matches'.

    Without this branch grep fell through to status='ok' which is uninformative.
    """
    assert summarize_tool_result(
        "file__grep",
        {"op": "grep", "status": "ok", "count": 7, "matches": []},
    ) == "7 matches"
    assert summarize_tool_result(
        "file__grep",
        {"op": "grep", "status": "ok", "count": 1, "matches": []},
    ) == "1 match"


def test_file_glob_shows_match_count() -> None:
    """Tier 2: file__glob result ({pattern, matches, count}) shows 'N matches'.

    Without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "file__glob",
        {"pattern": "src/**/*.py", "matches": ["a.py", "b.py"], "count": 2},
    ) == "2 matches"
    assert summarize_tool_result(
        "file__glob",
        {"pattern": "*.md", "matches": ["README.md"], "count": 1},
    ) == "1 match"


def test_list_mcp_servers_shows_server_count() -> None:
    """Tier 2: list_mcp_servers result ({servers: [...]}) shows 'N servers'.

    list_mcp_servers returns {"servers": [...]} with no op/status/error key;
    without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "list_mcp_servers", {"servers": ["github", "slack", "linear"]}
    ) == "3 servers"
    assert summarize_tool_result(
        "list_mcp_servers", {"servers": ["only"]}
    ) == "1 server"
    assert summarize_tool_result(
        "list_mcp_servers", {"servers": []}
    ) == "0 servers"


def test_list_mcp_tools_shows_tool_count() -> None:
    """Tier 2: list_mcp_tools result ({mcp_tools: [...]}) shows 'N tools'.

    list_mcp_tools returns {"mcp_tools": [...]} with no op/status/error key;
    without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "list_mcp_tools",
        {"mcp_tools": [{"name": "github__get_issue"}, {"name": "github__list_prs"}]},
    ) == "2 tools"
    assert summarize_tool_result(
        "list_mcp_tools", {"mcp_tools": [{"name": "slack__send_message"}]}
    ) == "1 tool"


def test_search_actions_shows_item_count() -> None:
    """Tier 2: search_actions/list_actions result ({items: [...], total: N}) shows 'N items'.

    Both search_actions and list_actions return {"items": [...], "total": N} with no
    op/status/error key; without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "search_actions", {"items": [{"qualified_name": "file__read"}, {"qualified_name": "file__write"}], "total": 2}
    ) == "2 items"
    assert summarize_tool_result(
        "list_actions", {"items": [], "total": 0}
    ) == "0 items"
    assert summarize_tool_result(
        "search_actions", {"items": [{"qualified_name": "recall"}], "total": 1}
    ) == "1 item"


def test_index_drop_shows_chunks_dropped() -> None:
    """Tier 2: index_drop result ({removed: bool, chunks_dropped: int}) shows 'Dropped N chunks'.

    index_drop returns {"removed": bool, "chunks_dropped": int} with no op/status/error key;
    without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "index_drop", {"removed": True, "chunks_dropped": 7}
    ) == "Dropped 7 chunks"
    assert summarize_tool_result(
        "index_drop", {"removed": True, "chunks_dropped": 1}
    ) == "Dropped 1 chunk"
    assert summarize_tool_result(
        "index_drop", {"removed": False, "chunks_dropped": 0}
    ) == "Dropped 0 chunks"


def test_describe_tool_shows_name_not_raw_dict() -> None:
    """Tier 2: describe_action/describe_mcp_tool result ({input_schema, name/description}) shows name.

    Both tools return a dict with an 'input_schema' key (dict) and a 'name' or 'description'
    key.  Without this branch the raw schema dict leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "describe_mcp_tool",
        {"name": "get_issue", "description": "Get a GitHub issue", "input_schema": {"type": "object"}},
    ) == "get_issue"
    out = summarize_tool_result(
        "describe_action",
        {"description": "Lists actions by category", "input_schema": {"properties": {}}},
    )
    assert "Lists actions" in out
    assert "{" not in out, "raw dict repr must not leak"


def test_dict_with_status_shows_status() -> None:
    """Tier 2: an opaque dict with a status field shows the status."""
    assert summarize_tool_result("mcp__call", {"status": "ok", "x": 1}) == "ok"


def test_empty_or_none_reports_done() -> None:
    """Tier 2: empty / None result → 'done', not a blank line."""
    assert summarize_tool_result("x", None) == "done"
    assert summarize_tool_result("x", "") == "done"


def test_oversized_result_is_truncated_one_line() -> None:
    """Tier 2: a huge / multiline result collapses to a truncated single line."""
    out = summarize_tool_result("x", "z" * 500 + "\ntail")
    assert "\n" not in out
    assert "…" in out
    assert "tail" not in out


def test_unknown_shape_degrades_without_raising() -> None:
    """Tier 2: a malformed/unknown result returns a string, never raises."""
    weird = {"op": object(), "nested": [object()]}
    out = summarize_tool_result("x", weird)
    assert isinstance(out, str) and out  # some non-empty summary, no crash


def test_judge_output_passed_shows_score() -> None:
    """Tier 2: judge_output pass result shows 'Passed (score)' not raw dict.

    judge_output returns {kind, score, passed, reason, threshold, on_fail} with no
    status key; without this branch the raw dict repr leaked into the ⎿ row.
    """
    out = summarize_tool_result(
        "judge_output",
        {"kind": "judge_output", "score": 0.85, "passed": True,
         "reason": "looks good", "threshold": 0.7, "on_fail": "retry"},
    )
    assert out == "Passed (0.85)"
    assert "{" not in out


def test_judge_output_failed_shows_score() -> None:
    """Tier 2: judge_output fail result shows 'Failed (score)' not raw dict."""
    out = summarize_tool_result(
        "judge_output",
        {"kind": "judge_output", "score": 0.23, "passed": False,
         "reason": "incomplete", "threshold": 0.7, "on_fail": "retry"},
    )
    assert out == "Failed (0.23)"
    assert "{" not in out


def test_web_search_results_shows_count() -> None:
    """Tier 2: web_search result ({kind, query, status, results: [...]}) shows 'N results'.

    web_search returns {"kind": "web_search", "results": [...], "status": "ok"};
    adding the 'results' list key means N results is shown rather than the generic 'ok'.
    """
    out = summarize_tool_result(
        "web_search",
        {"kind": "web_search", "query": "python asyncio", "backend": "brave",
         "status": "ok", "results": [{"title": "A"}, {"title": "B"}, {"title": "C"}]},
    )
    assert out == "3 results"
    out1 = summarize_tool_result(
        "web_search",
        {"kind": "web_search", "query": "x", "status": "ok", "results": [{"title": "X"}]},
    )
    assert out1 == "1 result"


def test_file_mkdir_shows_created_path() -> None:
    """Tier 2: file mkdir result (op='mkdir', path=...) shows 'Created {path}'.

    file__mkdir returns {kind, op='mkdir', path, status='ok', created: bool} with
    no matching op branch; without this fix it falls to status → shows 'ok'.
    """
    assert summarize_tool_result(
        "file__mkdir",
        {"kind": "file", "op": "mkdir", "path": "src/new/", "status": "ok", "created": True},
    ) == "Created src/new/"
    assert summarize_tool_result(
        "file__mkdir", {"op": "mkdir"}
    ) == "Created directory"


def test_file_move_shows_destination() -> None:
    """Tier 2: file move result (op='move', dest_path=...) shows 'Moved to {dest}'.

    file__move returns {kind, op='move', path, dest_path, status='ok', moved: True}
    with no matching op branch; without this fix it falls to status → shows 'ok'.
    """
    assert summarize_tool_result(
        "file__move",
        {"kind": "file", "op": "move", "path": "old.py", "dest_path": "new.py",
         "status": "ok", "moved": True},
    ) == "Moved to new.py"
    assert summarize_tool_result(
        "file__move", {"op": "move"}
    ) == "Moved"


def test_cron_list_shows_job_count() -> None:
    """Tier 2: cron_list result ({status, source, jobs: [...]}) shows 'N jobs'.

    cron_list returns {"status": "ok", "source": "...", "jobs": [...]} — 'jobs' is
    not in the covered list-key set so without this branch it falls to status → 'ok'.
    """
    assert summarize_tool_result(
        "cron_list",
        {"status": "ok", "source": "live_scheduler",
         "jobs": [{"name": "nightly"}, {"name": "weekly"}]},
    ) == "2 jobs"
    assert summarize_tool_result(
        "cron_list", {"status": "ok", "source": "config_file", "jobs": [{"name": "daily"}]}
    ) == "1 job"
    assert summarize_tool_result(
        "cron_list", {"status": "ok", "source": "live_scheduler", "jobs": []}
    ) == "0 jobs"


def test_sandboxed_exec_ok_shows_exit_code() -> None:
    """Tier 2: sandboxed_exec success (returncode int, status='ok') shows 'exit N'.

    sandboxed_exec returns {kind, status, returncode, stdout, stderr, ...}; for
    status='ok' the generic 'ok' is unhelpful. 'timeout'/'cancelled' get a '✗' prefix
    with the exit code so failure is visually distinct from a successful result row.
    """
    assert summarize_tool_result(
        "sandboxed_exec",
        {"kind": "sandboxed_exec", "status": "ok", "backend": "subprocess",
         "returncode": 0, "stdout": "hello", "stderr": "", "truncated": False},
    ) == "exit 0"


def test_task_op_shows_task_name() -> None:
    """Tier 2: task op result ({kind, status, task: {name, ...}}) shows the task name.

    task.create/get/update_status/abort/assign/add_dependency/... all return a
    nested 'task' dict; without this branch they fall to status → 'ok', losing
    the identity of which task was acted on.
    """
    assert summarize_tool_result(
        "task__create",
        {"kind": "task.create", "status": "ok",
         "task": {"task_id": "t1", "name": "Add user auth", "status": "open"}},
    ) == "Add user auth"
    assert summarize_tool_result(
        "task__update_status",
        {"kind": "task.update_status", "status": "ok",
         "task": {"task_id": "t2", "name": "Fix login bug", "status": "done"}},
    ) == "Fix login bug"
    assert summarize_tool_result(
        "task__get",
        {"kind": "task.get", "status": "ok",
         "task": {"task_id": "abc-123", "name": "", "status": "open"}},
    ) == "abc-123"


def test_compact_shows_freed_tokens() -> None:
    """Tier 2: compact success ({kind, status, freed_tokens, ...}) shows 'Freed N tokens'.

    compact returns {"kind": "compact", "status": "ok", "freed_tokens": int, ...};
    without this branch status fires showing 'ok' with no context of how much was freed.
    """
    assert summarize_tool_result(
        "compact",
        {"kind": "compact", "status": "ok", "freed_tokens": 8200,
         "free_window_after": 50000, "summarized_turns": 12},
    ) == "Freed 8200 tokens"
    assert summarize_tool_result(
        "compact",
        {"kind": "compact", "status": "ok", "freed_tokens": 1,
         "free_window_after": 10000},
    ) == "Freed 1 token"
    assert summarize_tool_result(
        "compact",
        {"kind": "compact", "status": "ok", "freed_tokens": 0},
    ) == "Freed 0 tokens"


def test_ask_user_shows_answer_text() -> None:
    """Tier 2: ask_user result ({kind, question, answer, status}) shows the answer.

    ask_user returns {"kind": "ask_user", "question": "...", "answer": "...", "status": "ok"};
    showing the answer text is more informative than the generic 'ok'.
    """
    assert summarize_tool_result(
        "ask_user",
        {"kind": "ask_user", "question": "Which environment?",
         "answer": "production", "status": "ok"},
    ) == "production"
    long_answer = "yes " * 30
    out = summarize_tool_result(
        "ask_user",
        {"kind": "ask_user", "question": "Are you sure?",
         "answer": long_answer, "status": "ok"},
    )
    assert "…" in out and "\n" not in out  # long answer is truncated to a single line


def test_mcp_install_shows_server_name() -> None:
    """Tier 2: mcp_install success ({status:"ok", server_name, ...}) shows 'Installed {name}'.

    mcp_install returns {kind:"mcp_install", status:"ok", server_id, server_name, ...};
    without this branch status fires showing 'ok', losing which server was installed.
    """
    assert summarize_tool_result(
        "mcp_install",
        {"kind": "mcp_install", "status": "ok", "server_id": "github",
         "server_name": "GitHub MCP", "scope": "project", "installed_path": "/path"},
    ) == "Installed GitHub MCP"
    assert summarize_tool_result(
        "mcp_install",
        {"kind": "mcp_install", "status": "ok", "server_id": "s",
         "server_name": "slack", "scope": "user"},
    ) == "Installed slack"


def test_web_fetch_shows_url() -> None:
    """Tier 2: web_fetch success ({kind:"web_fetch", url, status:"ok", content, ...}) shows URL.

    web_fetch returns {kind, url, status:"ok", content, ...}; without this branch
    status fires showing 'ok'. Error paths (blocked/timeout/error) all carry 'error'
    str which is caught by the error-first guard and are unaffected.
    """
    out = summarize_tool_result(
        "web_fetch",
        {"kind": "web_fetch", "url": "https://example.com/api/v1",
         "status": "ok", "status_code": 200, "content": "body text"},
    )
    assert "example.com" in out and "{" not in out
    long_url = "https://example.com/" + "a" * 80
    out2 = summarize_tool_result(
        "web_fetch",
        {"kind": "web_fetch", "url": long_url, "status": "ok", "content": ""},
    )
    assert "…" in out2 and "\n" not in out2


def test_task_heartbeat_shows_state() -> None:
    """Tier 2: task.heartbeat result ({state, task_id, status:"ok"}) shows the state.

    task.heartbeat returns {kind:"task.heartbeat", status:"ok", task_id, state, unblocked};
    showing the state ("running"/"awaiting"/"completed") is more informative than 'ok'.
    """
    assert summarize_tool_result(
        "task__heartbeat",
        {"kind": "task.heartbeat", "status": "ok",
         "task_id": "t-abc", "state": "running", "unblocked": False},
    ) == "running"
    assert summarize_tool_result(
        "task__heartbeat",
        {"kind": "task.heartbeat", "status": "ok",
         "task_id": "t-def", "state": "awaiting", "unblocked": False},
    ) == "awaiting"


def test_file_regenerate_index_shows_indexed_path() -> None:
    """Tier 2: file regenerate_index result shows 'Indexed <path>' not raw dict.

    file op='regenerate_index' returns {kind:"file", op:"regenerate_index", path, status:"ok",
    entries:int} — 'entries' is an int (not a list) so no list branch fires; without the
    op branch this falls through to the status branch and shows 'ok'.
    """
    assert summarize_tool_result(
        "file__regenerate_index",
        {"kind": "file", "op": "regenerate_index", "path": "/repo/.reyn/index",
         "status": "ok", "entries": 42},
    ) == "Indexed /repo/.reyn/index"
    assert summarize_tool_result(
        "file__regenerate_index",
        {"kind": "file", "op": "regenerate_index", "path": None, "status": "ok"},
    ) == "Indexed"


def test_mcp_drop_server_shows_removed_name() -> None:
    """Tier 2: mcp_drop_server success shows 'Removed <server>' not 'ok'.

    mcp_drop_server returns {kind:"mcp_drop_server", status:"ok", server:str, ...};
    without a branch this falls through to status='ok'. not_found result has status
    'not_found' and should NOT match (stays as 'not_found' via the status fallback).
    """
    assert summarize_tool_result(
        "mcp__drop_server",
        {"kind": "mcp_drop_server", "status": "ok", "server": "my-mcp"},
    ) == "Removed my-mcp"
    assert summarize_tool_result(
        "mcp__drop_server",
        {"kind": "mcp_drop_server", "status": "not_found", "server": "missing-mcp"},
    ) == "not_found"


def test_cron_enable_disable_shows_verb_and_name() -> None:
    """Tier 2: cron_enable/disable shows 'Enabled/Disabled <name>' not 'ok'.

    cron_enable returns {status:"ok", name:str, enabled:True};
    cron_disable returns {status:"ok", name:str, enabled:False}.
    Both currently fall through to the status branch showing 'ok'.
    """
    assert summarize_tool_result(
        "cron__enable",
        {"status": "ok", "name": "daily-sync", "enabled": True},
    ) == "Enabled daily-sync"
    assert summarize_tool_result(
        "cron__disable",
        {"status": "ok", "name": "daily-sync", "enabled": False},
    ) == "Disabled daily-sync"


def test_sandboxed_exec_cancelled_shows_failure_text() -> None:
    """Tier 2: sandboxed_exec cancelled result shows '✗ cancelled (exit N)' not plain 'cancelled'.

    sandboxed_exec returns {kind:"sandboxed_exec", status:"cancelled", returncode:int, ...}
    when the subprocess is killed. Without a branch, the status fallback returns the bare
    string 'cancelled' with no visual failure signal.
    """
    assert summarize_tool_result(
        "sandboxed_exec",
        {"kind": "sandboxed_exec", "status": "cancelled",
         "backend": "local", "returncode": -9, "stdout": "", "stderr": ""},
    ) == "✗ cancelled (exit -9)"


def test_sandboxed_exec_timeout_shows_failure_text() -> None:
    """Tier 2: sandboxed_exec timeout result shows '✗ timeout (exit N)' not plain 'timeout'.

    sandboxed_exec returns {kind:"sandboxed_exec", status:"timeout", returncode:-1, ...}
    when the subprocess exceeds the deadline. The bare 'timeout' string is indistinguishable
    from a successful result in the ⎿ row without an explicit failure prefix.
    """
    assert summarize_tool_result(
        "sandboxed_exec",
        {"kind": "sandboxed_exec", "status": "timeout",
         "backend": "local", "returncode": -1, "stdout": "partial", "stderr": ""},
    ) == "✗ timeout (exit -1)"
