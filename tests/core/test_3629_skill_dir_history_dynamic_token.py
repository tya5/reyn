"""Tier 1 / Tier 2: #3629 — a skill body's location tokens
(``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}``) are no longer baked to an
absolute value in what gets PERSISTED to ``history.jsonl``; a wire-serialise
pass re-resolves them fresh, against the CURRENT filesystem, every time.

The reported defect (issue #3629): a skill directory rename (#3588,
underscore -> hyphen) left an ALREADY-EXPANDED absolute path baked into a
persisted tool-result entry; history is immutable, so every later turn
replayed a path to a directory that no longer existed, and the model had no
way to tell a stale absolute path from a live one.

Covered here, bottom-up:

  1. ``load_skill_body`` (pure function, ``reyn.plugins.skill_load``) —
     the persisted variant leaves location tokens literal; the token map
     is metadata only.
  2. ``refresh_location_tokens`` — the "dynamic param" half. THE
     directory-moved witness: change the directory BETWEEN write and
     read, assert the RESOLVED PATH FOLLOWS (self-heal); a genuine
     rename/delete leaves the token literal (never a stale value).
  3. ``load_skill`` op (``reyn.core.op_runtime.load_skill``) — the result
     dict carries ``content_history``/``token_map``/``skill_source_path``
     alongside the unchanged, fully-expanded ``content``.
  4. ``load_skill_to_canonical`` — threads them onto
     ``CanonicalToolResult.history_text``/``history_meta``.
  5. ``RouterLoop.feedback`` — persists the LITERAL-token variant (not the
     wire ``content_str``) via ``append_history_entry``, with
     ``token_map``/``skill_source_path`` on the persisted ``ChatMessage``'s
     meta — the model still reads the fully-resolved path THIS turn (the
     ``out`` wire dict is unaffected).
  6. ``RouterHistoryBuffer._serialise_turn`` — a REAL ``ChatMessage`` built
     the way step 5 persists one, replayed through a REAL
     ``RouterHistoryBuffer.build_history()`` after the directory moves —
     the wire dict content follows the CURRENT filesystem, by value.

No mocks — real ``Workspace``/``OpContext``/``ChatMessage``/``RouterLoop``/
``RouterHistoryBuffer`` throughout (``FakeRouterHost`` is the project's
established Fake, not a mock).
"""
from __future__ import annotations

import asyncio
import json

from reyn.config.chat import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import load_skill_to_canonical
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.load_skill import handle as load_skill_handle
from reyn.data.skills.registry import SkillEntry
from reyn.data.workspace.workspace import Workspace
from reyn.plugins.skill_load import load_skill_body, refresh_location_tokens
from reyn.runtime.chat_message import (
    SKILL_SOURCE_PATH_META_KEY,
    TOKEN_MAP_META_KEY,
    ChatMessage,
)
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.schemas.models import LoadSkillIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools.scheme import ExecutionResult
from tests._support.router_loop import FakeRouterHost


def _run(coro):
    return asyncio.run(coro)


# ── 1. load_skill_body: persisted variant keeps location tokens literal ─────

def test_load_skill_body_persisted_variant_leaves_location_tokens_literal(tmp_path):
    """Tier 1: the CURRENT-turn ``expanded`` body is fully resolved (unchanged
    from before #3629); the PERSISTED body keeps ``${REYN_SKILL_DIR}``/
    ``${REYN_PLUGIN_ROOT}`` literal while ``${REYN_PROJECT_DIR}`` (measured
    safe) is still expanded. The location token map records the SAME values
    that were baked into ``expanded`` — audit-completeness only."""
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    body = "skill=${REYN_SKILL_DIR} project=${REYN_PROJECT_DIR}"

    expanded, persisted, loc_map, _env_exp, _env_den = load_skill_body(
        body, skill_path=skill_path, project_dir=project_dir,
    )

    assert f"skill={skill_dir.resolve()}" in expanded
    assert f"project={project_dir.resolve()}" in expanded
    assert "${REYN_SKILL_DIR}" in persisted, "location token must stay literal in the persisted form"
    assert f"project={project_dir.resolve()}" in persisted, "PROJECT_DIR stays expanded (measured safe)"
    assert loc_map["REYN_SKILL_DIR"] == str(skill_dir.resolve())
    assert "REYN_PROJECT_DIR" not in loc_map, "the map is LOCATION tokens only"


# ── 2. refresh_location_tokens: the directory-moved witness ─────────────────

def test_refresh_location_tokens_self_heals_when_directory_moves(tmp_path):
    """Tier 1: THE directory-moved witness (#3629 brief) — the skill
    directory is renamed BETWEEN persisting the literal-token body and
    replaying it; re-resolution against the SAME (project-relative)
    ``skill_source_path`` follows the CURRENT filesystem and produces the
    NEW directory's absolute path, asserted BY VALUE."""
    old_dir = tmp_path / "skills" / "old-name"
    old_dir.mkdir(parents=True)
    skill_path = old_dir / "SKILL.md"
    skill_path.write_text("x", encoding="utf-8")
    persisted = "See ${REYN_SKILL_DIR}/reference.md"
    source_path = str(skill_path.relative_to(tmp_path))

    # Nothing moved yet: fresh resolution reproduces the SAME dir.
    before = refresh_location_tokens(
        persisted, skill_source_path=source_path, project_dir=tmp_path,
    )
    assert str(old_dir.resolve()) in before

    # The directory moves (a checkout swap / relocation — #3629 names this
    # explicitly, distinct from a rename that changes the LAST path
    # component the model itself referenced).
    new_root = tmp_path / "moved-checkout"
    new_root.mkdir()
    new_dir = new_root / "skills" / "old-name"
    new_dir.parent.mkdir(parents=True)
    old_dir.rename(new_dir)

    after = refresh_location_tokens(
        persisted, skill_source_path="skills/old-name/SKILL.md", project_dir=new_root,
    )
    assert str(new_dir.resolve()) in after, "must follow the CURRENT filesystem, not the write-time value"
    assert str(old_dir) not in after
    assert "${REYN_SKILL_DIR}" not in after


def test_refresh_location_tokens_leaves_token_literal_when_path_is_gone(tmp_path):
    """Tier 1: the reported case — the skill's OWN directory is renamed
    (#3588's underscore->hyphen), so the ORIGINAL ``skill_source_path`` no
    longer resolves to anything. The token is left LITERAL rather than
    substituted with a stale value — unambiguously a placeholder to the
    model, never mistaken for a live path (the exact #3629 defect)."""
    old_dir = tmp_path / "skills" / "reyn_cheat_sheet"
    old_dir.mkdir(parents=True)
    (old_dir / "SKILL.md").write_text("x", encoding="utf-8")
    persisted = "See ${REYN_SKILL_DIR}/reference.md"
    source_path = "skills/reyn_cheat_sheet/SKILL.md"

    old_dir.rename(tmp_path / "skills" / "reyn-cheat-sheet")

    after = refresh_location_tokens(persisted, skill_source_path=source_path, project_dir=tmp_path)

    assert after == persisted, "unresolvable identity -> literal token, never a guessed/stale value"
    assert "${REYN_SKILL_DIR}" in after


# ── 3. the load_skill op result carries the persist-safe fields ─────────────

def _make_ctx(project_root, available_skills):
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(config_permissions={}, project_root=project_root, interactive=False)
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test_3629", available_skills=available_skills,
    )


def _config_registered(tmp_path, body: str):
    project_root = tmp_path / "proj"
    skill_dir = project_root / "skills" / "greeter"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(body, encoding="utf-8")
    rel_path = str(skill_path.relative_to(project_root))
    entry = SkillEntry(name="greeter", description="d", path=rel_path)
    ctx = _make_ctx(project_root, [entry])
    return ctx, rel_path, skill_dir


def test_load_skill_op_result_carries_content_history_and_token_map(tmp_path):
    """Tier 2: a config-registered skill load's result dict has ``content``
    fully expanded (unchanged) AND ``content_history``/``token_map``/
    ``skill_source_path`` for the persist path — never present for the
    unregistered path (#3196 fails-closed, unaffected by #3629)."""
    ctx, rel_path, skill_dir = _config_registered(tmp_path, "loc=${REYN_SKILL_DIR}")

    result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))

    assert result["status"] == "ok"
    assert f"loc={skill_dir.resolve()}" in result["content"]
    assert "${REYN_SKILL_DIR}" in result["content_history"]
    assert result["token_map"]["REYN_SKILL_DIR"] == str(skill_dir.resolve())
    assert result["skill_source_path"] == rel_path


# ── 4. canonical mapper threads history_text/history_meta ────────────────────

def test_load_skill_to_canonical_sets_history_text_only_when_present():
    """Tier 1: ``history_text``/``history_meta`` appear ONLY when the op
    result carries ``content_history`` (a provenance-classified load) — a
    plain (unregistered-path) result's canonical form is unaffected,
    byte-identical to before #3629."""
    with_history = load_skill_to_canonical({
        "path": "s/SKILL.md", "status": "ok", "content": "expanded body",
        "content_history": "${REYN_SKILL_DIR} body",
        "token_map": {"REYN_SKILL_DIR": "/abs/path"},
        "skill_source_path": "s/SKILL.md",
    })
    assert with_history["history_text"] == "${REYN_SKILL_DIR} body"
    assert with_history["history_meta"] == {
        "token_map": {"REYN_SKILL_DIR": "/abs/path"}, "skill_source_path": "s/SKILL.md",
    }

    without_history = load_skill_to_canonical({
        "path": "s/SKILL.md", "status": "ok", "content": "raw body",
    })
    assert "history_text" not in without_history
    assert "history_meta" not in without_history


# ── 5. RouterLoop.feedback persists the literal-token variant ───────────────

def test_router_loop_feedback_persists_literal_token_variant_with_meta(tmp_path):
    """Tier 2: ``RouterLoop.feedback`` persists the ``history_text`` variant
    (location tokens literal) via ``append_history_entry`` — NOT the fully
    resolved wire content — and stamps ``token_map``/``skill_source_path``
    onto the persisted entry's meta. The wire dict returned by ``feedback``
    (what the model reads THIS turn) still shows the fully-resolved path."""
    ctx, rel_path, skill_dir = _config_registered(tmp_path, "loc=${REYN_SKILL_DIR}")
    op_result = _run(load_skill_handle(LoadSkillIROp(kind="load_skill", path=rel_path), ctx))
    assert op_result["status"] == "ok"
    # Tag exactly as the real dispatch chokepoint would (FP-0056 PR-F1).
    tagged_result = {**op_result, "_canonical_source": "load_skill"}

    host = FakeRouterHost()
    loop = RouterLoop(host=host, chain_id="chain-3629", max_iterations=5)

    wire = loop.feedback(ExecutionResult(
        tool_results=[tagged_result],
        tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "load_skill", "arguments": json.dumps({"path": rel_path})},
        }],
        assistant_content="",
    ))

    tool_wire = next(m for m in wire if m.get("role") == "tool")
    assert f"loc={skill_dir.resolve()}" in tool_wire["content"], (
        "the CURRENT turn's wire content is unaffected -- still fully resolved"
    )

    persisted_tool_entries = [e for e in host.history if e["role"] == "tool"]
    assert persisted_tool_entries, "expected the load_skill result to persist a tool entry"
    entry = persisted_tool_entries[0]
    assert "${REYN_SKILL_DIR}" in entry["content"], (
        "the PERSISTED entry must keep the location token literal, not the "
        "absolute value the model read this turn"
    )
    assert entry["meta"][TOKEN_MAP_META_KEY]["REYN_SKILL_DIR"] == str(skill_dir.resolve())
    assert entry["meta"][SKILL_SOURCE_PATH_META_KEY] == rel_path


# ── 6. RouterHistoryBuffer._serialise_turn re-resolves fresh, by value ───────

def _buffer(history: list, project_dir):
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(),
        compaction_controller=None,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,  # #4552 PR-3
        non_interactive=False,
        project_dir_fn=lambda: project_dir,
    )


def test_serialise_turn_refreshes_location_token_after_checkout_moves(tmp_path):
    """Tier 2: THE directory-moved witness over the real
    ``RouterHistoryBuffer`` (#3629 names this explicitly: "the owner runs
    reyn from two working copies") — the SAME persisted ``ChatMessage``
    (shaped exactly as step 5 above persists one) is replayed via a REAL
    ``build_history()`` from a DIFFERENT checkout root than it was written
    under; the wire content follows the CURRENT filesystem root, asserted
    by value, not merely "changed"."""
    source_path = "skills/my-skill/SKILL.md"

    checkout_a = tmp_path / "checkout-a"
    (checkout_a / "skills" / "my-skill").mkdir(parents=True)
    (checkout_a / "skills" / "my-skill" / "SKILL.md").write_text("x", encoding="utf-8")

    checkout_b = tmp_path / "checkout-b"
    (checkout_b / "skills" / "my-skill").mkdir(parents=True)
    (checkout_b / "skills" / "my-skill" / "SKILL.md").write_text("x", encoding="utf-8")

    msg = ChatMessage(
        role="tool",
        content="See ${REYN_SKILL_DIR}/reference.md",
        meta={
            TOKEN_MAP_META_KEY: {"REYN_SKILL_DIR": "/some/stale/write-time/value"},
            SKILL_SOURCE_PATH_META_KEY: source_path,
        },
        tool_call_id="tc1", name="load_skill",
    )

    wire_a = _buffer([msg], checkout_a).build_history()
    tool_msg_a = next(m for m in wire_a if m.get("role") == "tool")
    assert str((checkout_a / "skills" / "my-skill").resolve()) in tool_msg_a["content"]
    assert "/some/stale/write-time/value" not in tool_msg_a["content"], (
        "must NEVER re-expand from the frozen meta value -- only fresh resolution"
    )

    # Same persisted entry, replayed from the OTHER checkout (a rewind /
    # process restart pointed at a different working copy).
    wire_b = _buffer([msg], checkout_b).build_history()
    tool_msg_b = next(m for m in wire_b if m.get("role") == "tool")
    assert str((checkout_b / "skills" / "my-skill").resolve()) in tool_msg_b["content"], (
        "replaying the SAME persisted entry from a DIFFERENT checkout root "
        "must follow the CURRENT filesystem, not repeat checkout-a's value"
    )
    assert str(checkout_a) not in tool_msg_b["content"]


def test_serialise_turn_leaves_token_literal_when_source_path_gone(tmp_path):
    """Tier 2: when the persisted entry's ``skill_source_path`` no longer
    resolves at all (deleted, or the model's own path reference renamed
    away — #3629's actual reported shape), the wire content keeps the
    literal token rather than showing any absolute path — legible
    unresolved, never a guess."""
    msg = ChatMessage(
        role="tool",
        content="See ${REYN_SKILL_DIR}/reference.md",
        meta={
            TOKEN_MAP_META_KEY: {"REYN_SKILL_DIR": "/some/stale/write-time/value"},
            SKILL_SOURCE_PATH_META_KEY: "skills/reyn_cheat_sheet/SKILL.md",  # never existed here
        },
        tool_call_id="tc1", name="load_skill",
    )
    buf = _buffer([msg], tmp_path)

    wire = buf.build_history()

    tool_msg = next(m for m in wire if m.get("role") == "tool")
    assert "${REYN_SKILL_DIR}" in tool_msg["content"]
    assert "/some/stale/write-time/value" not in tool_msg["content"]


def test_serialise_turn_is_noop_for_pre_3629_history(tmp_path):
    """Tier 2: an entry with no ``SKILL_SOURCE_PATH_META_KEY`` at all (every
    pre-#3629 persisted row, and every non-skill tool message) is untouched
    by the refresh pass -- the architect's ruling that already-poisoned
    history is neither rewritten nor annotated."""
    msg = ChatMessage(
        role="tool", content="an ordinary tool result, no location tokens here",
        meta={"chain_id": "abc"}, tool_call_id="tc1", name="read_file",
    )
    buf = _buffer([msg], tmp_path)

    wire = buf.build_history()

    tool_msg = next(m for m in wire if m.get("role") == "tool")
    assert tool_msg["content"] == "an ordinary tool result, no location tokens here"
