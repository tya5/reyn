"""chunkers.py — safe-mode python steps for the index_chat stdlib skill.

Public pure functions (Tier 2 testable — no artifact dict, no global state):
  collect_chat_turn_chunks — walk agents_root/<name>/chat/**/*.jsonl, emit one
                             chunk per user_message_received event
  advance_chat_cursor      — atomic write of new max ts to chat cursor file
  read_chat_cursor         — read chat cursor file; return None if missing

Preprocessor entry point (called by the skill harness before LLM call):
  resolve_chat_scan_context — read chat cursor + summarise chat file inventory

Postprocessor entry points (called by the skill harness with artifact dict):
  run_collect_chat_chunks  — artifact wrapper around collect_chat_turn_chunks
  run_advance_chat_cursor  — artifact wrapper around advance_chat_cursor

Design note: the chat-turn chunker logic was first written in
`index_events/chunkers.py` (#1821 improvement-1).  index_chat is a *separate
skill* with its own dedicated cursor (``.reyn/index/chat_cursor``) and its own
``"chat"`` RAG source.  The chunker code here is intentionally self-contained
because skill .py modules are loaded via spec_from_file_location (not as Python
packages) and cannot import across skill boundaries.

Path manipulation uses plain string operations because pathlib is not on the
safe-mode import allowlist.  The single-character path separator ``/`` is used
throughout — adequate on macOS / Linux, the only supported platforms for stdlib
skills.

P7 note: this module is skill-local and may freely reference chat-domain
concepts (agent, chain_id, user_message_received, etc.).  OS code does NOT
import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from reyn.api.safe import embed_index as _embed_index
from reyn.api.safe import file as _safe_file

# ── Constants ─────────────────────────────────────────────────────────────────

_EPOCH_ISO = "1970-01-01T00:00:00Z"

#: Cursor file for the chat index (separate from events_cursor).
_CURSOR_FILE = ".reyn/index/chat_cursor"

#: Root for chat event JSONL files (relative to workspace; patchable in tests).
_CHAT_EVENTS_DIR = ".reyn/events/agents"

# POSIX stat-mode constants (= stat.S_IFMT / S_IFREG). Hard-coded because
# the stat module is not on the safe-mode import allowlist.
_S_IFMT = 0o170000
_S_IFREG = 0o100000


# ── Preprocessor entry point ──────────────────────────────────────────────────


def resolve_chat_scan_context(artifact: dict) -> dict:
    """Phase preprocessor: resolve chat cursor + summarise chat file inventory.

    Receives the full index_chat_input artifact.  Reads the chat cursor file
    (if present) to determine the effective lower-bound timestamp, then
    discovers all .jsonl files under .reyn/events/agents/<name>/chat/ and
    computes summary statistics WITHOUT exposing the full file list to the LLM.

    The full path list is intentionally excluded from the return value to
    stay under ARTIFACT_REF_THRESHOLD (8KB).  The postprocessor re-globs
    files deterministically at run time.

    Returns:
        {
            "since":              str,           # effective ISO-8601 lower bound
            "chat_files_count":   int,           # number of candidate .jsonl files
            "oldest_timestamp":   str | null,    # oldest file mtime ISO string
            "newest_timestamp":   str | null,    # newest file mtime ISO string
            "mode":               str,           # "append" | "replace"
            "cursor_exists":      bool,
            "cursor_value":       str | null,
        }
    """
    data = artifact.get("data") or {}
    since_input: str | None = data.get("since")
    mode: str = str(data.get("mode") or "append")

    cursor_exists = _path_exists_safe(_CURSOR_FILE)
    cursor_value: str | None = None

    if mode == "replace":
        since = _EPOCH_ISO
    elif since_input:
        since = since_input
    elif cursor_exists:
        try:
            cursor_value = _safe_file.read(_CURSOR_FILE).strip()
            since = cursor_value if cursor_value else _EPOCH_ISO
        except (OSError, PermissionError):
            since = _EPOCH_ISO
    else:
        since = _EPOCH_ISO

    chat_files = _discover_chat_files(_CHAT_EVENTS_DIR)
    chat_files_count = len(chat_files)

    oldest_timestamp: str | None = None
    newest_timestamp: str | None = None
    if chat_files:
        mtimes = []
        for fp in chat_files:
            try:
                mtimes.append(float(_safe_file.stat(fp).get("mtime", 0)))
            except (OSError, PermissionError):
                pass
        if mtimes:
            def _mtime_to_iso(mts: float) -> str:
                return datetime.fromtimestamp(mts, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            oldest_timestamp = _mtime_to_iso(min(mtimes))
            newest_timestamp = _mtime_to_iso(max(mtimes))

    return {
        "since": since,
        "chat_files_count": chat_files_count,
        "oldest_timestamp": oldest_timestamp,
        "newest_timestamp": newest_timestamp,
        "mode": mode,
        "cursor_exists": cursor_exists,
        "cursor_value": cursor_value,
    }


# ── Public pure functions ──────────────────────────────────────────────────────


def collect_chat_turn_chunks(
    agents_root: str,
    since: str | None,
) -> list[dict]:
    """Walk agents_root/<name>/chat/**/*.jsonl, emit one chunk per user turn.

    Each ``user_message_received`` event in the chat JSONL files becomes one
    searchable chunk.  The agent name is extracted from the directory path
    (``agents/<name>/chat/``).  Events from the same ``chain_id`` within the
    same file are grouped so turn-outcome metadata (``routing_decided``,
    ``chat_turn_completed_inline``, ``skill_run_spawned``) can be annotated
    onto the user-turn chunk.

    Chunk shape:
        {
            "id": "chat__<agent>__<chain_id>",
            "text": "<human-readable turn summary>",
            "metadata": {
                "source_path":     str,
                "source_type":     "chat_turn",
                "content_hash":    str,
                "embedding_model": "",
                "chunk_index":     int,     # set by caller
                "size_tokens":     int,
                "parent_context":  None,
                "extra": {
                    "agent":         str,
                    "chain_id":      str,
                    "turn_ts":       str,
                    "turn_outcome":  str,
                    "routed_action": str | None,
                }
            }
        }

    Args:
        agents_root: Path to the ``.reyn/events/agents`` directory.
        since: ISO-8601 lower bound (inclusive by user-turn timestamp).

    Returns:
        List of chunk dicts for user turns with timestamp >= since.
    """
    since_dt: datetime | None = None
    if since:
        since_dt = _parse_iso_safe(since)

    chat_files = _discover_chat_files(agents_root)
    chunks: list[dict] = []
    chunk_index = 0

    for file_path in chat_files:
        agent_name = _extract_agent_name(file_path, agents_root)
        file_chunks = _extract_chat_turn_chunks_from_file(
            file_path, agent_name, since_dt
        )
        for chunk in file_chunks:
            chunk["metadata"]["chunk_index"] = chunk_index
            chunks.append(chunk)
            chunk_index += 1

    return chunks


def advance_chat_cursor(cursor_path: str, new_ts: str) -> None:
    """Atomic write of new max ts to the chat cursor file.

    Creates parent directories as needed.  Uses ``reyn.api.safe.file.write_atomic``
    for crash-safe update.  Raises OSError / PermissionError on write failure.
    """
    parent = _dirname(cursor_path)
    if parent:
        _safe_file.mkdir(parent, parents=True, exist_ok=True)
    _safe_file.write_atomic(cursor_path, new_ts)


def read_chat_cursor(cursor_path: str) -> str | None:
    """Read chat cursor file; return None if missing or empty."""
    if not _path_exists_safe(cursor_path):
        return None
    try:
        value = _safe_file.read(cursor_path).strip()
        return value if value else None
    except (OSError, PermissionError):
        return None


# ── Postprocessor entry points (artifact-dict wrappers) ──────────────────────


def run_collect_chat_chunks(artifact: dict) -> dict:
    """Postprocessor python step: chunk chat turns into the 'chat' index source.

    Discovers chat JSONL files under ``.reyn/events/agents/``, streams
    ``user_message_received`` turns into ``reyn.api.safe.embed_index`` (the
    ``"chat"`` source), and tracks the max turn timestamp for cursor advance.

    Returns summary dict placed at ``data.chat_chunk_stats``:
        {
            "chunk_count":      int,
            "skipped_turns":    int,
            "embedded":         int,
            "skipped_embed":    int,
            "written":          int,
            "skipped_write":    int,
            "max_turn_ts":      str,    # for cursor advance
        }
    """
    data = artifact.get("data") or {}
    since_str: str | None = str(data.get("since") or "") or None

    since_dt: datetime | None = None
    if since_str and since_str != _EPOCH_ISO:
        since_dt = _parse_iso_safe(since_str)

    agents_root = _CHAT_EVENTS_DIR
    chat_files = _discover_chat_files(agents_root)

    acc = {"skipped_turns": 0, "max_dt": None, "max_ts": ""}

    def _gen_chat_chunks():
        chunk_index = 0
        for file_path in chat_files:
            agent_name = _extract_agent_name(file_path, agents_root)
            file_chunks = _extract_chat_turn_chunks_from_file(
                file_path, agent_name, since_dt
            )
            for chunk in file_chunks:
                chunk["metadata"]["chunk_index"] = chunk_index
                turn_ts = str(chunk["metadata"]["extra"].get("turn_ts") or "")
                if turn_ts:
                    dt = _parse_iso_safe(turn_ts)
                    if dt and (acc["max_dt"] is None or dt > acc["max_dt"]):
                        acc["max_dt"] = dt
                        acc["max_ts"] = turn_ts
                yield {"text": chunk["text"], "metadata": chunk["metadata"]}
                chunk_index += 1

    stats = _embed_index.embed_and_index(
        _gen_chat_chunks(),
        "chat",
        "standard",
        mode="append",
        description="Chat turn messages indexed by index_chat skill (#1821)",
    )

    return {
        "chunk_count": stats["embedded"] + stats["skipped_embed"],
        "skipped_turns": acc["skipped_turns"],
        "embedded": stats["embedded"],
        "skipped_embed": stats["skipped_embed"],
        "written": stats["written"],
        "skipped_write": stats["skipped_write"],
        "max_turn_ts": acc["max_ts"],
    }


def run_advance_chat_cursor(artifact: dict) -> dict:
    """Postprocessor python step: advance .reyn/index/chat_cursor.

    Reads the max turn timestamp from ``data.chat_chunk_stats`` (computed
    inline by run_collect_chat_chunks while streaming), then calls
    advance_chat_cursor() to write the new value atomically.

    Returns summary placed at ``data.chat_cursor_result``:
        {
            "indexed_turns":   int,
            "skipped_turns":   int,
            "new_cursor":      str,
            "sources_updated": list[str],
        }
    """
    data = artifact.get("data") or {}
    chat_chunk_stats = data.get("chat_chunk_stats") or {}

    indexed_turns = int(chat_chunk_stats.get("chunk_count") or 0)
    skipped_turns = int(chat_chunk_stats.get("skipped_turns") or 0)

    cursor_path = _CURSOR_FILE
    new_cursor = str(chat_chunk_stats.get("max_turn_ts") or "")
    if not new_cursor:
        existing = read_chat_cursor(cursor_path)
        new_cursor = existing if existing else _EPOCH_ISO

    advance_chat_cursor(cursor_path, new_cursor)

    return {
        "indexed_turns": indexed_turns,
        "skipped_turns": skipped_turns,
        "new_cursor": new_cursor,
        "sources_updated": ["chat"],
    }


# ── Chat-turn internal helpers ────────────────────────────────────────────────


def _discover_chat_files(agents_root: str) -> list[str]:
    """Discover .jsonl files under agents_root/<name>/chat/**/ ."""
    if not _path_exists_safe(agents_root):
        return []
    pattern = f"{agents_root}/*/chat/**/*.jsonl"
    matches = _glob_mod.glob(pattern, recursive=True)
    return sorted(m for m in matches if _is_regular_file(m))


def _extract_agent_name(file_path: str, agents_root: str) -> str:
    """Extract agent name from path ``agents_root/<name>/chat/...``.

    Returns ``"unknown"`` when the path structure doesn't match.
    """
    prefix = agents_root.rstrip("/") + "/"
    remainder = file_path[len(prefix):] if file_path.startswith(prefix) else file_path
    slash_idx = remainder.find("/")
    if slash_idx <= 0:
        return "unknown"
    return remainder[:slash_idx]


def _extract_chat_turn_chunks_from_file(
    file_path: str,
    agent_name: str,
    since_dt: datetime | None,
) -> list[dict]:
    """Parse one chat JSONL file, return one chunk per user turn.

    Groups events by ``chain_id``.  For each ``chain_id`` where a
    ``user_message_received`` event exists, builds a chunk annotated with
    the turn outcome (routing / inline reply / spawned skill).
    """
    if not _path_exists_safe(file_path):
        return []
    try:
        content = _safe_file.read(file_path)
    except (OSError, PermissionError):
        return []

    by_chain: dict[str, list[dict]] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        chain_id = str((event.get("data") or {}).get("chain_id") or "")
        if not chain_id:
            continue
        if chain_id not in by_chain:
            by_chain[chain_id] = []
        by_chain[chain_id].append(event)

    chunks: list[dict] = []
    for chain_id, events in by_chain.items():
        chunk = _build_chat_turn_chunk(
            chain_id, events, file_path, agent_name, since_dt
        )
        if chunk is not None:
            chunks.append(chunk)
    return chunks


def _build_chat_turn_chunk(
    chain_id: str,
    events: list[dict],
    source_file: str,
    agent_name: str,
    since_dt: datetime | None,
) -> dict | None:
    """Build a chat-turn chunk from events sharing a chain_id, or None to skip.

    Returns None when no ``user_message_received`` event exists for this
    chain_id, or when the turn timestamp falls before ``since_dt``.
    """
    user_event: dict | None = None
    routing_action: str | None = None
    turn_outcome = "unknown"

    for event in events:
        etype = str(event.get("type") or "")
        if etype == "user_message_received":
            user_event = event
        elif etype == "routing_decided":
            turn_outcome = "routing"
            routing_action = str(
                (event.get("data") or {}).get("action_name") or ""
            ) or None
        elif etype == "chat_turn_completed_inline":
            if turn_outcome == "unknown":
                turn_outcome = "inline_reply"
        elif etype == "skill_run_spawned":
            if turn_outcome not in ("routing",):
                turn_outcome = "spawned"

    if user_event is None:
        return None

    turn_ts = str(user_event.get("timestamp") or "")
    if since_dt is not None and turn_ts:
        ts_dt = _parse_iso_safe(turn_ts)
        if ts_dt and ts_dt < since_dt:
            return None

    user_data = user_event.get("data") or {}
    user_text = str(user_data.get("text") or "")
    media_count = int(user_data.get("media_block_count") or 0)

    text = _build_chat_turn_text(
        agent=agent_name,
        chain_id=chain_id,
        turn_ts=turn_ts,
        user_text=user_text,
        media_count=media_count,
        turn_outcome=turn_outcome,
        routed_action=routing_action,
    )

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunk_id = f"chat__{agent_name}__{chain_id}"

    return {
        "id": chunk_id,
        "text": text,
        "metadata": {
            "source_path": source_file,
            "source_type": "chat_turn",
            "content_hash": content_hash,
            "embedding_model": "",   # filled in by embed op
            "chunk_index": 0,        # set by caller
            "size_tokens": _approx_tokens(text),
            "parent_context": None,
            "extra": {
                "agent": agent_name,
                "chain_id": chain_id,
                "turn_ts": turn_ts,
                "turn_outcome": turn_outcome,
                "routed_action": routing_action,
            },
        },
    }


def _build_chat_turn_text(
    agent: str,
    chain_id: str,
    turn_ts: str,
    user_text: str,
    media_count: int,
    turn_outcome: str,
    routed_action: str | None,
) -> str:
    """Build human-readable text for a chat-turn chunk.

    Format:
        agent: <name>
        turn_ts: <ISO timestamp>
        chain_id: <id>
        user: <message text>
        media_blocks: <n>     # only when n > 0
        turn_outcome: inline_reply | routing | spawned | unknown
        routed_action: <action_name>  # only when routing
    """
    lines = [
        f"agent: {agent}",
        f"turn_ts: {turn_ts}",
        f"chain_id: {chain_id}",
        f"user: {user_text}",
    ]
    if media_count > 0:
        lines.append(f"media_blocks: {media_count}")
    lines.append(f"turn_outcome: {turn_outcome}")
    if routed_action:
        lines.append(f"routed_action: {routed_action}")
    return "\n".join(lines)


# ── Path helpers (pathlib-free for safe-mode allowlist) ────────────────────


def _dirname(path: str) -> str:
    """Return the parent directory of a POSIX-style path.

    Replacement for Path(p).parent / os.path.dirname.
    Returns ``""`` when the path has no parent.
    """
    idx = path.rfind("/")
    if idx <= 0:
        return ""
    return path[:idx]


def _path_exists_safe(path: str) -> bool:
    """Permission-aware existence check that does not raise."""
    try:
        return _safe_file.exists(path)
    except (OSError, PermissionError):
        return False


def _is_regular_file(path: str) -> bool:
    """Return True iff ``path`` exists and is a regular file."""
    try:
        info = _safe_file.stat(path)
    except (OSError, PermissionError):
        return False
    return (int(info.get("mode", 0)) & _S_IFMT) == _S_IFREG


def _parse_iso_safe(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; return None on parse failure."""
    if not ts:
        return None
    ts = ts.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    return None


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)
