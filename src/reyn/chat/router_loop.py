"""RouterLoop — drives the chat router via native LLM tool_use (PR35).

Loop: build tools + prompt → call_llm_tools → if tool_calls, execute in
parallel, append results to messages, repeat → if text reply, emit to host
outbox and stop. Bounded by max_iterations.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from reyn.chat.router_system_prompt import (
    build_system_prompt,
    tier_wants_discovery_mandate,
)
from reyn.chat.router_tools import (
    _DESCRIBE_SKILL_STRIP_FIELDS,
    MAX_DESC_LEN_FOR_LISTING,
    build_tools,
    get_dispatch_kind,
)
from reyn.chat.services.skill_search import BM25Backend
from reyn.chat.session import _TOOL_FAILED_FALLBACK_MSG
from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.index.source_manifest import get_source_manifest
from reyn.llm.llm import call_llm_tools
from reyn.llm.pricing import TokenUsage
from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST
from reyn.services.turn_budget import wrap_up_system_prompt

if TYPE_CHECKING:
    from reyn.config import SkillSearchConfig


# ---------------------------------------------------------------------------
# Empty-response detection (Option F — ADR-0021)
# ---------------------------------------------------------------------------

# Localized user-facing message when the model returns an empty response
# (finish_reason=stop, no content, no tool calls). Deterministic i18n so
# output_language is always honoured.  P7-clean: no skill / tool names.
# "en" is the global-safe default.
_EMPTY_RESPONSE_MSG: dict[str, str] = {
    "ja": (
        "モデルが空の応答を返しました。"
        " 別の表現で再入力するか、設定を確認してください。"
    ),
    "en": (
        "The model returned an empty response."
        " Please try rephrasing your request or check your configuration."
    ),
}


# Localized OS-level acknowledgment emitted when a skill spawns asynchronously
# (= invoke_skill / invoke_action returns ``{status: "spawned", ...}``). The
# H3 ablation exits the router loop before any further LLM call, so without
# this OS-injected message the user sees silence between request and the
# eventual ``[task_completed]`` arrival. The previous (pre-H3) LLM-composed
# spawn-ack was hallucinating skill output that hadn't happened yet (B32
# W3 S1); this deterministic OS message carries the same UX guarantee
# (= "/tasks" hint) without LLM composition, so the race condition does
# not re-emerge.  P7-clean: no skill names, no qualified action names.
_SPAWN_ACK_MSG: dict[str, str] = {
    "ja": (
        "スキルをバックグラウンドで実行しています。"
        " `/tasks` で進行状況を確認できます。"
    ),
    "en": (
        "Skill is running in the background."
        " Use `/tasks` to monitor progress."
    ),
}


# B49 plan-side spawn alignment (= #441 / #445 follow-up): the user-
# friendly trailer that pairs with the `[task_spawned] kind=plan`
# structured header. Plan dispatch is async (= ADR-0023 §2.1.1) so
# behaviour mirrors skill spawn-ack: router exits after dispatch,
# the LLM regains control on the next [task_completed] kind=plan
# injection from session._handle_plan_completed.
_PLAN_SPAWN_ACK_MSG: dict[str, str] = {
    "ja": (
        "プランをバックグラウンドで実行しています。"
        " `/tasks` で進行状況を確認できます。"
    ),
    "en": (
        "Plan is running in the background."
        " Use `/tasks` to monitor progress."
    ),
}


# B55 R-7 (2026-05-25): agent-side spawn alignment — symmetric with
# skill/plan spawn_ack so the LLM sees a structured task lifecycle
# event for delegate_to_agent / other peer-async tools too. Prior
# behaviour pushed a generic `dispatched N async requests; awaiting
# peer reply` status row with no `[task_spawned]` header, leaving
# the SP TASK_SPAWNED rule un-anchored for the agent path. Now mirrors
# the skill / plan format: `[task_spawned] kind=agent ...` header +
# user-facing trailer. Pairs with the `[task_completed] kind=agent
# ...` injection on peer reply receipt (see a2a_handler).
_AGENT_SPAWN_ACK_MSG: dict[str, str] = {
    "ja": (
        "ピアエージェントにリクエストを送信しました。"
        " 返答を待っています — `/tasks` で進行状況を確認できます。"
    ),
    "en": (
        "Request dispatched to the peer agent."
        " Awaiting reply — use `/tasks` to monitor progress."
    ),
}


# NF-W7-B43-2: positive directive injected into the spawn-ack tool_result
# content. Replaces the previous OS-synthetic spawn-ack outbox push +
# early-exit pattern with the standard tool_call → tool_result → LLM
# continuation pattern (= aligned with Claude / GPT competitor designs).
#
# Why directive, not bare status:
#   - Trace-patch-replay N=10 (= 5 variant ablation, 2026-05-20):
#       Variant A (bare status, no directive):      1/10 ACK, 8/10 EMPTY, 0/10 HALLUCINATE
#       Variant B (negative directive "don't"):     0/10 ACK, 10/10 EMPTY
#       Variant D (positive "write short reply"):   7/10 ACK, 3/10 EMPTY, **0/10 HALLUCINATE**
#   - H3 hallucination defense (= B32 W3 S1 race) preserved across all
#     variants thanks to "do not include skill output" framing.
#   - Variant D (= "1-2 sentences confirm" voice) ACK 7/10 baseline;
#     remaining 3/10 EMPTY covered by PR #265 retry mechanism
#     (REYN_EMPTY_STOP_RETRY=1).
#
# The directive is appended to the tool_result body using the same
# ``\n\n---\n`` separator pattern as PR #221's ``_post_text`` (= LLM
# reads it as a textual instruction following the JSON status block).
_SPAWN_ACK_TOOL_DIRECTIVE = (
    "The skill has been spawned and is running in the background. "
    "Write a short reply to the user (1-2 sentences) confirming the "
    "skill has been started. The actual result will arrive in a "
    "subsequent turn as a [task_completed] message — do not include "
    "any skill output here, only the status confirmation."
)


# #187: the SINGLE empty-stop retry continuation directive, shared UNIFORMLY by
# every RouterLoop construction site (chat / plan-step / agent op-loop). owner
# decision (2026-06-07): do NOT build per-site/per-tier directive differentiation
# without evidence — a content-neutral "resume" re-enters the loop and lets the
# model continue (tool-call OR reply) on its own. Real-task: a content-less empty
# stop is 67% premature; "resume" recovers the next action (invoke 11/12). The
# previous per-site directives (chat "write your reply" / plan "step report") were
# unevidenced differentiation — and the chat one's "Do not call another tool"
# was itself anti-invoke. Iterate per-site ONLY if a measured problem appears.
EMPTY_STOP_RETRY_DIRECTIVE = "resume"


def _strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block (``---\\n...\\n---\\n``) from
    a memory file's text and return the body alone.

    Used by :meth:`RouterLoop._read_memory_body` to give the LLM the
    actual remembered text instead of metadata fields it doesn't need
    (= ``name`` / ``description`` / ``type``). When the input doesn't
    start with a frontmatter delimiter the original text is returned
    unchanged — handles legacy memory files written before the frontmatter
    convention existed.
    """
    text = content or ""
    if not text.lstrip().startswith("---"):
        return text
    # Find first non-blank line; require it to be exactly "---".
    lines = text.split("\n")
    # Skip leading blanks.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return text
    # Find the closing "---" after the opening one.
    close = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            close = j
            break
    if close == -1:
        # No closing delimiter — leave content alone rather than truncating.
        return text
    body_lines = lines[close + 1:]
    # Trim a single leading blank line that conventionally follows the
    # closing delimiter; keep subsequent whitespace as authored.
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return "\n".join(body_lines).rstrip("\n") + ("\n" if body_lines else "")


# #272 media axis: per-image token estimate. Single-sourced from the compaction
# engine's ``_IMAGE_FIXED_TOKEN_COST`` (services/compaction/engine.py) so the
# per-turn media bound is unit-consistent with how a turn's image cost is
# measured — one constant, no drift. Name preserved for in-module + test use.
_MEDIA_IMAGE_TOKEN_COST = _IMAGE_FIXED_TOKEN_COST
# #272 media-COUNT cap: conservative per-item token bound for an individual
# overflow ref — boilerplate + a filesystem-bounded path (≤ ~255 chars ≈ 64
# tokens); 128 upper-bounds it so the bounded accounting never under-counts.
_MEDIA_REF_TOKEN_COST = 128
# Reserved for the single tail preview (offload-manifest pointer or no-store
# degrade note) so the WHOLE follow-up stays ≤ budget_tokens.
_MEDIA_TAIL_PREVIEW_RESERVE_TOKENS = 256
# The "Tool `X` returned the following image(s):" intro line.
_MEDIA_INTRO_TOKEN_COST = 24


def _render_context_size_signal_for_host(host: "RouterLoopHost") -> "str | None":
    """#272/#1128: render the OS-injected context-size header from the host's
    live free-window, or None when the host exposes no status (test stubs).
    Best-effort — never breaks a turn.
    """
    status_fn = getattr(host, "context_window_status", None)
    if status_fn is None:
        return None
    try:
        status = status_fn()
        if not status:
            return None
        from reyn.services.compaction.context_signal import render_context_size_signal
        return render_context_size_signal(
            free_window=status["free_window"],
            effective_trigger=status["effective_trigger"],
        )
    except Exception:  # noqa: BLE001 — signal is advisory; absence is harmless
        return None


def _materialise_image_part(block: dict, media_store: Any) -> dict | None:
    """Render one image block into a litellm ``image_url`` part.

    Path-ref blocks (``{"type":"image","path":...}``) are read via the
    MediaStore and base64-embedded; inline blocks (``{"data":"<b64>"}``) embed
    their base64 directly. Returns ``None`` when the block cannot be rendered
    (path-ref without a store, missing/unreadable bytes, or no data).
    """
    mime = block.get("mime_type") or block.get("mimeType") or "image/png"
    path = block.get("path")
    if isinstance(path, str) and path:
        if media_store is None:
            return None
        try:
            data_bytes, found = media_store.read_image(path)
        except PermissionError:
            return None
        if not found:
            return None
        import base64
        data_b64 = base64.b64encode(data_bytes).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data_b64}"}}
    data = block.get("data")
    if isinstance(data, str) and data:
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
    return None


def _as_path_ref(
    block: dict, media_store: Any, *, tool_name: str, seq: int
) -> dict | None:
    """Return a ``{"path","mime_type"}`` lossless handle for an image block.

    A path-ref block is returned as-is (its on-disk path is the handle, valid
    even without a live store object). An inline-base64 block is persisted to
    the MediaStore (``save_image``) so it gains a path — requires a store.
    Returns ``None`` when no path can be obtained (inline + no store, or
    undecodable base64) — the caller then degrades consciously.
    """
    path = block.get("path")
    if isinstance(path, str) and path:
        return {
            "path": path,
            "mime_type": block.get("mime_type") or block.get("mimeType") or "image/png",
        }
    data = block.get("data")
    if isinstance(data, str) and data and media_store is not None:
        import base64
        try:
            raw = base64.b64decode(data)
        except (ValueError, TypeError):
            return None
        mime = block.get("mime_type") or block.get("mimeType") or "image/png"
        saved = media_store.save_image(raw, mime_type=mime, tool=tool_name, seq=seq)
        return {"path": saved["path"], "mime_type": saved.get("mime_type", mime)}
    return None


def _overflow_ref_text(ref: dict) -> str:
    return (
        f"[image not loaded — exceeds the per-turn media budget. "
        f"Stored at {ref['path']} ({ref.get('mime_type', 'image')}); "
        f"load it with read_tool_result when the context has room.]"
    )


def _build_media_tail_preview(
    tail: list[dict], media_store: Any, *, tool_name: str
) -> dict:
    """One bounded text part standing in for ``len(tail)`` over-budget images.

    With a MediaStore: offload a LOSSLESS JSON manifest of the tail images'
    on-disk paths (``save_tool_result``) and point to it (read_tool_result-able)
    — O(1) follow-up cost no matter how many images overflowed.

    Without a store (or if none could be persisted): a least-lossy bounded note
    naming the count. Losslessness requires a store, so this is a conscious
    *environment-bound* degrade, never a silent drop (#272 / the
    no-lossy-truncate principle: the loss is surfaced, not hidden).
    """
    n = len(tail)
    if media_store is not None:
        manifest_images: list[dict] = []
        for i, block in enumerate(tail):
            ref = _as_path_ref(block, media_store, tool_name=tool_name, seq=10_000 + i)
            if ref is not None:
                manifest_images.append(ref)
        if manifest_images:
            import json as _json
            manifest = _json.dumps({"images": manifest_images}, ensure_ascii=False)
            try:
                saved = media_store.save_tool_result(
                    manifest, mime_type="application/json", tool=tool_name,
                )
                return {"type": "text", "text": (
                    f"[{n} more image(s) exceed the per-turn media budget and are "
                    f"not shown here. A lossless manifest of their on-disk paths is "
                    f"stored at {saved['path']}; load it with read_tool_result to "
                    f"access them.]"
                )}
            except Exception:  # noqa: BLE001 — offload best-effort; degrade below
                pass
    return {"type": "text", "text": (
        f"[{n} more image(s) exceed the per-turn media budget and are not shown. "
        f"No media store is configured for lossless offload, so they cannot be "
        f"re-loaded from here — configure a media store to retain them.]"
    )}


def _build_media_followup_message(
    *,
    tool_name: str,
    media_blocks: list[dict],
    media_store: Any = None,
    budget_tokens: int | None = None,
) -> dict | None:
    """Build a multimodal follow-up user message for tool results carrying image
    content (issue #362 → #383 PR-C; bounded by #272 + the media-count cap).

    Strategy (Option A): append a synthetic user message containing the tool's
    images in litellm-normalised shape — provider-agnostic, since user messages
    with content lists are universally supported (Anthropic, Gemini, OpenAI).

    #272 + media-count cap (dead-end-free media axis): when ``budget_tokens`` is
    given, the WHOLE follow-up (materialised images + individual refs + the tail
    preview) is held ≤ ``budget_tokens`` so the result turn stays single-turn
    compactable (the chat retry_loop's shrink can always fold it). Images are
    materialised while they fit; the next become small LOSSLESS path-refs while
    THOSE fit; the remaining tail collapses into ONE offloaded-manifest preview
    (lossless). So neither the image bytes NOR the ref count can grow the
    follow-up without bound — closing the inline-shape bypass (Gap A) and the
    unbounded-ref count (Gap B). ``budget_tokens=None`` preserves the pre-#272
    unbounded behaviour (partial/test hosts).
    """
    images = [
        b for b in media_blocks if isinstance(b, dict) and b.get("type") == "image"
    ]
    if not images:
        return None

    parts: list[dict] = [
        {"type": "text", "text": f"Tool `{tool_name}` returned the following image(s):"},
    ]

    # Unbounded path (pre-#272 / partial-host): materialise all renderable images.
    if budget_tokens is None:
        for block in images:
            part = _materialise_image_part(block, media_store)
            if part is not None:
                parts.append(part)
        return {"role": "user", "content": parts} if len(parts) > 1 else None

    # Bounded path (#272 + media-count cap): keep the whole follow-up ≤ budget.
    spent = _MEDIA_INTRO_TOKEN_COST
    emitted: list[tuple[str, dict]] = []  # (kind, part); kind ∈ {"img", "ref"}
    tail_start = len(images)
    for i, block in enumerate(images):
        # Prefer materialising (usable by the vision model) while it fits.
        if spent + _MEDIA_IMAGE_TOKEN_COST <= budget_tokens:
            part = _materialise_image_part(block, media_store)
            if part is not None:
                parts.append(part)
                emitted.append(("img", part))
                spent += _MEDIA_IMAGE_TOKEN_COST
                continue
        # Otherwise a small LOSSLESS ref, while THAT fits.
        ref = _as_path_ref(block, media_store, tool_name=tool_name, seq=i + 1)
        if ref is not None and spent + _MEDIA_REF_TOKEN_COST <= budget_tokens:
            txt = {"type": "text", "text": _overflow_ref_text(ref)}
            parts.append(txt)
            emitted.append(("ref", txt))
            spent += _MEDIA_REF_TOKEN_COST
            continue
        # Doesn't fit (or no lossless ref obtainable here) → this + rest = tail.
        tail_start = i
        break

    if tail_start < len(images):
        # Reserve room for the single tail preview by popping trailing emitted
        # items until it fits — guarantees the whole follow-up stays ≤ budget.
        while emitted and spent + _MEDIA_TAIL_PREVIEW_RESERVE_TOKENS > budget_tokens:
            kind, part = emitted.pop()
            parts.remove(part)
            spent -= _MEDIA_IMAGE_TOKEN_COST if kind == "img" else _MEDIA_REF_TOKEN_COST
            tail_start -= 1
        tail = images[tail_start:]
        parts.append(_build_media_tail_preview(tail, media_store, tool_name=tool_name))

    return {"role": "user", "content": parts} if len(parts) > 1 else None


def _is_empty_router_response(response: Any) -> bool:
    """OS-side detection: model emitted no text and no tool calls.

    Provider-level glitch (observed with weak models such as
    gemini-2.5-flash-lite at ~50% rate — ADR-0021 / B7-G12).  This is
    NOT recovered by Reyn — surfaced to the user as an explicit failure
    for user-side handling (no retry, no context change, no model switch).

    Trigger: finish_reason=="stop", content empty, tool_calls empty.
    """
    if response is None:
        return True
    finish = getattr(response, "finish_reason", None)
    content = getattr(response, "content", None) or ""
    tool_calls = getattr(response, "tool_calls", None) or []
    return finish == "stop" and not content.strip() and not tool_calls


# Provider context-length errors arrive as litellm exceptions with varied class
# names/messages; the chat session classifies them by keyword on the message
# (session.py:5381-5384). #1092 PR-B reuses the SAME keyword set for the phase
# force-close shrink-retry. NOTE: this list is currently duplicated here +
# twice in session.py — a future cleanup should lift one shared
# ``is_context_overflow_error`` (e.g. next to ``ContextOverflowError`` in
# services/compaction); kept local here to avoid a session.py refactor in PR-B.
_CONTEXT_OVERFLOW_KEYWORDS = (
    "context", "token", "length", "limit", "too long", "too large",
)


def _is_context_overflow_error(exc: BaseException) -> bool:
    """True when *exc* looks like a provider context-length overflow.

    Keyword match on the stringified exception — the same heuristic the chat
    session uses to convert litellm errors into ``ContextOverflowError``.
    """
    msg = str(exc).lower()
    return any(kw in msg for kw in _CONTEXT_OVERFLOW_KEYWORDS)


# ---------------------------------------------------------------------------
# Host protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RouterLoopCore(Protocol):
    """#1092 PR-A (ADR-0036 FD1, decision c): the NARROW core surface the
    RouterLoop act-loop actually depends on — the members RouterLoop's loop
    directly calls for ANY host (chat / plan-step / phase). A phase implements
    ONLY this (via PhaseRouterLoopHost) — no chat-extra stubs. The chat
    ``RouterHostAdapter`` is a superset and satisfies this for free.

    The chat-extras (skills/agents/mcp/memory/web/file/reyn_src/embedding/
    discovery/spawn/send_to_agent/record_plan_*) live on ``RouterLoopHost``
    below; they are reached only via the chat-discovery setup, the chat
    system-prompt build, or chat-dispatch handlers — a phase never reaches them
    (its op catalog REPLACES chat-discovery, and its ops dispatch via the op
    handlers + ``make_router_op_context``). ``get_phase_op_catalog`` is a
    phase-only getattr-hook (not declared here — chat doesn't implement it).
    """

    agent_name: str
    agent_role: str
    output_language: str | None

    @property
    def events(self) -> Any:
        """EventLog (has .emit(type: str, **data)) for tool dispatch events."""
        ...

    def resolve_model(self, name: str) -> str: ...
    def make_router_op_context(self) -> Any: ...
    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None: ...


@runtime_checkable
class RouterLoopHost(RouterLoopCore, Protocol):
    """Abstract surface RouterLoop needs (chat-mode superset of RouterLoopCore).

    Implemented by RouterHostAdapter in
    src/reyn/chat/services/router_host_adapter.py. Extends RouterLoopCore
    (#1092 PR-A) with the chat-only methods (discovery / tool-exec primitives /
    plan-record); the core members are inherited (the redundant re-declarations
    below are harmless Protocol overlap, pending a follow-up cleanup).
    """

    # Static catalogue access
    chat_id: str
    agent_name: str
    agent_role: str
    # BCP-47 code (e.g. "ja", "en") when the user explicitly configured
    # output_language; None when unset, in which case build_system_prompt
    # skips the language directive entirely so the LLM picks based on
    # the user's input language naturally.
    output_language: str | None

    @property
    def events(self) -> Any:
        """EventLog (has .emit(type: str, **data)) for tool dispatch events."""
        ...

    def list_available_skills(self) -> list[dict]:
        """Each entry: {name, description, routing?, category?}"""
        ...

    def list_available_agents(self) -> list[dict]:
        """Each entry: {name, role, cluster?}"""
        ...

    def get_memory_index(self) -> dict:
        """Returns {status: 'ok'|'not_found', content: str}"""
        ...

    def get_file_permissions(self) -> dict | None:
        """{read: [paths], write: [paths]} or None"""
        ...

    def get_mcp_servers(self) -> list[dict]:
        """[{name, description, ...}, ...]"""
        ...

    def get_web_fetch_allowed(self) -> bool:
        """True if `web.fetch: allow` is in the operator's permissions."""
        ...

    def get_project_context(self) -> str:
        """Project context text (= REYN.md / `project_context_path` content),
        or empty string when the operator has not configured one. Threaded
        into the router's system prompt so the chat reply path knows about
        the user's project — without this, only the skill execution path
        sees REYN.md and casual chat queries get answered without
        project-specific context."""
        ...

    def get_universal_wrappers_enabled(self) -> bool:
        """Return whether the FP-0034 universal catalog wrappers should
        appear in tools=. Mirrors ``action_retrieval.universal_wrappers_enabled``
        from reyn.yaml. Default False preserves the prior tools= shape."""
        ...

    def get_action_embedding_index(self) -> Any:
        """Return the session-scoped ActionEmbeddingIndex, or None.

        FP-0034 Phase 2 step 1.  Bound by ChatSession when the operator
        has configured ``action_retrieval.embedding_class``.
        """
        ...

    def get_embedding_provider(self) -> Any:
        """Return the session's EmbeddingProvider instance, or None.

        FP-0034 Phase 2 step 1.  Used together with the
        ActionEmbeddingIndex to power search_actions semantic search.
        """
        ...

    def get_embedding_model_class(self) -> str | None:
        """Return the configured embedding model class name, or None.

        FP-0034 Phase 2 step 1.  Mirror of
        ``action_retrieval.embedding_class`` from reyn.yaml.
        """
        ...

    def get_sandbox_backend(self) -> "str | None":
        """Return the configured sandbox backend name, or None.

        FP-0034 Phase 2.  Mirror of ``sandbox.backend`` from reyn.yaml
        (resolved from ``session._sandbox_config.backend``).  RouterLoop
        forwards this into ``RouterCallerState.sandbox_backend`` so the
        ``exec`` category D14 visibility gate in
        ``universal_catalog._enumerate_category`` can decide whether to
        expose ``exec__sandboxed_exec``.  ``None`` and ``"noop"`` both
        hide the category; any other value (``"seatbelt"`` /
        ``"landlock"`` / ``"auto"``) makes it visible.
        """
        ...

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/search op (DuckDuckGo)."""
        ...

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/fetch op."""
        ...

    async def reyn_src_list(self, *, path: str) -> dict:
        """RouterLoopHost: list entries under ``<reyn_root>/path``.

        ``reyn_root`` resolves to the directory containing
        ``pyproject.toml`` for the running Reyn install (= dev install /
        source clone). For wheel installs without a discoverable
        repo root, returns an error result so the LLM can fall back."""
        ...

    async def reyn_src_read(self, *, path: str) -> dict:
        """RouterLoopHost: read the file at ``<reyn_root>/path`` as text."""
        ...

    # Memory file paths (for list_memory / read_memory_body)
    def memory_path(self, layer: str, slug: str) -> str:
        """Resolve layer ('shared'|'agent') + slug to file path"""
        ...

    def memory_dir(self, layer: str) -> str:
        """Directory for the layer's memory files"""
        ...

    # Action callbacks (async)
    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict: ...

    # FP-0012: non-blocking skill dispatch. Chat-mode hosts return
    # ``{status: "spawned", run_id, chain_id, note}`` immediately and
    # deliver completion via the ``skill_completed`` inbox kind. Hosts
    # that don't support spawn semantics (e.g. plan-mode steps) leave
    # this method un-bound (= duck-typing / hasattr check) so the
    # invoke_skill handler falls back to ``run_skill_awaitable``.
    async def spawn_skill(self, *, skill: str, input: dict,
                          chain_id: str) -> dict: ...

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None: ...

    async def put_outbox(self, *, kind: str, text: str,
                         meta: dict) -> None: ...

    # E-full PR-E (issue #383): persist a single ChatMessage entry
    # without routing through the outbox (= no TUI display side-effect).
    # Used by ``run()`` to record per-iteration assistant tool_call
    # turns and tool response turns so the next ``_build_history_for_router``
    # rebuilds the LLM message list with full fidelity.
    #
    # The host implementation constructs ChatMessage and feeds it to
    # the session's ``_append_history``.  ``meta`` should include
    # ``chain_id`` so the entry can be traced; other meta keys are
    # opaque to the router.
    def append_history_entry(
        self,
        *,
        role: str,
        content: Any,
        meta: dict | None = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None: ...

    # File ops (via op_runtime/file under permission scope)
    async def file_read(self, path: str) -> str: ...

    async def file_write(self, path: str, content: str) -> dict: ...

    async def file_delete(self, path: str) -> dict: ...

    async def file_list_directory(self, path: str) -> list[dict]: ...

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict: ...

    # MCP ops
    async def mcp_list_servers(self) -> list[dict]: ...

    async def mcp_list_tools(self, server: str) -> list[dict]: ...

    async def mcp_call_tool(self, server: str, tool: str,
                             args: dict) -> dict: ...

    # OpContext factory for unified-registry handlers (ADR-0026 Phase 3.5).
    # Builds a permission-aware OpContext with the operator-declared
    # PermissionDecl + Workspace(skill_name="chat_router") + mcp_servers,
    # so handlers in src/reyn/tools/{file,mcp,web*}.py can delegate to
    # op_runtime with the same gating the legacy router branches had.
    def make_router_op_context(self) -> Any: ...

    # Resolve router model (config "router" → real model id)
    def resolve_model(self, name: str) -> str: ...

    # The bound ModelResolver (#1172) — components that build their own LLM
    # callers (e.g. the planner's lazy CompactionEngine) resolve through it.
    @property
    def resolver(self) -> Any: ...

    # Plan-mode lifecycle persistence (ADR-0022 Phase 1).
    # Optional — implementations that don't support plan-mode crash
    # recovery (e.g. test stubs) leave these as no-ops. Real chat
    # session implementation (`session.py`) wires through to
    # SnapshotJournal.record_plan_*.
    async def record_plan_started(
        self, *, plan_id: str, goal: str, n_steps: int,
    ) -> None: ...

    async def record_plan_completed(self, *, plan_id: str) -> None: ...

    async def record_plan_aborted(
        self, *, plan_id: str, reason: str = "",
    ) -> None: ...

    # Plan-mode per-step WAL persistence (ADR-0023 Phase 2 step 6).
    # Optional — test stubs may omit. ChatSession routes these through
    # SnapshotJournal.record_plan_step_*.
    async def record_plan_step_started(
        self, *, plan_id: str, step_id: str, depends_on: list[str],
        n_tools: int,
    ) -> None: ...

    async def record_plan_step_completed(
        self, *, plan_id: str, step_id: str, content_len: int,
        result_text: str | None = None,
    ) -> None: ...

    async def record_plan_step_failed(
        self, *, plan_id: str, step_id: str, error: str,
    ) -> None: ...

    # Decomposition artifact persistence (ADR-0023 §3.5).
    # The artifact is the canonical SSoT for the plan shape on resume.
    # ChatSession threads this through reyn.plan.decomposition with the
    # agent-specific state directory; test stubs may no-op.
    async def write_plan_decomposition(
        self, *, plan_id: str, plan: "Any",
    ) -> str | None:
        """Persist the plan decomposition. Returns the artifact path or None."""
        ...

    async def delete_plan_decomposition(self, *, plan_id: str) -> None:
        """Remove the plan decomposition artifact (= P5 cleanup on success)."""
        ...

    async def spawn_plan_task(
        self, *, plan_id: str, runtime: "Any", chain_id: str,
    ) -> None:
        """Register a PlanRuntime as a background task (ADR-0023 Phase 2.1).

        ChatSession owns the task lifecycle (= ``running_plans`` dict)
        and the wrap-around finally that emits the terminal aggregator
        text via ``put_outbox(kind="agent")`` and cleans up the
        decomposition artifact on clean exit. dispatch_plan_tool hands
        the constructed runtime here and returns immediately.
        """
        ...


# ---------------------------------------------------------------------------
# FP-0034 Phase 2 step 5: hot list alias builder
# ---------------------------------------------------------------------------

# Universal wrapper tool names that are already added by section I of
# build_tools().  Filtering them here prevents duplicate function
# declarations when ActionUsageTracker.get_top_n() returns a wrapper name
# that was recorded as usage (B27-C1).
_UNIVERSAL_WRAPPER_NAMES: frozenset[str] = frozenset({
    "list_actions",
    "search_actions",
    "describe_action",
    "invoke_action",
})


def _filter_ghost_names_by_registry(
    names: "list[str]",
    skill_meta_map: "dict[str, dict] | None",
    mcp_tool_map: "dict[str, dict] | None",
    available_agents: "list[dict] | None",
    *,
    known_skill_names: "frozenset[str] | None" = None,
    known_memory_entries: "frozenset[str]",
    _warned: "set[str] | None" = None,
) -> "list[str]":
    """Filter hot-list names that pass structural check but don't exist in the registry.

    B38 W2 finding: ``_is_valid_qualified_name`` only validates shape
    (= category + separator + entry). A renamed skill like
    ``skill__create_skill`` passes structural check but is a ghost — the
    skill no longer exists under that name. This filter adds the
    existence check at hot-list materialization time, when session
    registry data is available.

    Categories and their existence signals:
    - ``skill__*`` → must be a key in ``skill_meta_map``
      (= resolved skill list from ``host.list_available_skills()``).
    - ``agent.peer__*`` → must match a name in ``available_agents``.
    - ``mcp.tool__*`` / ``mcp.server__*`` → must be a key in ``mcp_tool_map``
      (or any server name prefix for ``mcp.server__*``).
    - ``memory_entry__*`` → must be in ``known_memory_entries`` (=
      qualified names enumerated by ``_enumerate_shared_memory_entries``
      at hot-list build time). Required parameter — caller must supply
      the enumerated set (possibly empty when no entries exist).
    - Operation categories (``file__*``, ``web__*``, ``memory_operation__*``,
      ``reyn_source__*``, ``rag_operation__*``, ``mcp.operation__*``,
      ``exec__*``) → must be in ``KNOWN_STATIC_QUALIFIED_NAMES`` (static
      op registry).
    - ``rag_corpus__*`` is currently routed through the static check; it
      is also a dynamic category and the same fix shape as memory_entry
      applies if its caller starts seeding dynamic corpus aliases — see
      the PR for the memory_entry case for the pattern.

    Ghost names are logged once per unique name per session to stderr.
    ``_warned`` is an optional set for cross-call deduplication.

    P7-clean: check is data-driven from registry enumeration only —
    no hardcoded ghost names.
    """
    import sys

    from reyn.tools.universal_catalog import split_qualified_name
    from reyn.tools.universal_dispatch import KNOWN_STATIC_QUALIFIED_NAMES

    if _warned is None:
        _warned = set()

    # Build existence sets from session state.
    # Skills: use the broader ``known_skill_names`` set when provided
    # (= covers empty-input-schema skills too); fall back to
    # ``skill_meta_map`` keys for backwards-compat.
    if known_skill_names is not None:
        known_skills: frozenset[str] = known_skill_names
    else:
        known_skills = frozenset(skill_meta_map or {})
    known_mcp_tools: frozenset[str] = frozenset(mcp_tool_map or {})
    # Extract MCP server names from mcp_tool_map keys (mcp.tool__<server>.<tool>)
    known_mcp_servers: set[str] = set()
    for qn in known_mcp_tools:
        try:
            _cat, entry = split_qualified_name(qn)
        except ValueError:
            continue
        if "." in entry:
            known_mcp_servers.add(entry.split(".", 1)[0])
    # Phase 1 collapse (2026-05-25): the prior ``known_agents`` set
    # supported the ``agent.peer__<name>`` ghost-filter branch which is
    # now removed — multi_agent__* aliases pass through the static-ops
    # check below since they live in KNOWN_STATIC_QUALIFIED_NAMES.
    static_ops: frozenset[str] = frozenset(KNOWN_STATIC_QUALIFIED_NAMES)

    result: list[str] = []
    for name in names:
        try:
            category, entry_name = split_qualified_name(name)
        except ValueError:
            # Structural rejection (already handled at load; belt+suspenders).
            result.append(name)
            continue

        exists = True
        if category == "skill":
            exists = name in known_skills
        elif category == "mcp.tool":
            exists = name in known_mcp_tools
        elif category == "mcp.server":
            exists = entry_name in known_mcp_servers
        elif category == "memory_entry":
            # Dynamic category enumerated per-session from .reyn/memory/*.md
            # by ``_enumerate_shared_memory_entries``. Static op registry
            # does NOT contain user-saved memory entry slugs; the caller
            # is required to supply ``known_memory_entries`` (= empty
            # frozenset is valid for sessions with zero entries).
            exists = name in known_memory_entries
        else:
            # Operation categories not enumerable from session state:
            # check static op registry. (``rag_corpus__*`` is also a
            # dynamic category but no caller currently seeds it; if/when
            # one does, mirror the memory_entry pattern above.)
            if name in static_ops:
                exists = True
            else:
                # Not in static ops: unknown to the registry.
                exists = False

        if not exists:
            if name not in _warned:
                print(
                    f"[reyn] action_usage: skipping ghost alias "
                    f"{name!r} — not found in current registry",
                    file=sys.stderr,
                )
                _warned.add(name)
            continue

        result.append(name)
    return result


def _build_hot_list_aliases(
    names: list[str],
    short_description_lookup: "dict[str, str] | None" = None,
    *,
    skill_metadata_lookup: "dict[str, dict] | None" = None,
    mcp_tool_lookup: "dict[str, dict] | None" = None,
) -> list[dict]:
    """Build OpenAI-format ToolDefinition dicts for hot list direct aliases.

    Each alias has additionalProperties=True so any args pass through.
    The dispatcher routes these via invoke_action semantics (same path).

    Lever D (B23-PRE-1): when ``short_description_lookup`` is provided,
    embeds the target action's ``short_description`` in the alias
    description with an assertive directive. This surfaces the action's
    purpose directly in the tool listing so the LLM can pick the alias
    without a list_actions / describe_action round-trip.

    When ``short_description_lookup`` is None or the name is absent from
    the map, falls back to the prior generic description so callers that
    don't supply the lookup stay unaffected.

    Universal wrapper names (``list_actions``, ``search_actions``,
    ``describe_action``, ``invoke_action``) are filtered out defensively:
    build_tools() already adds them in section I, so including them here
    would produce duplicate function declarations that Gemini rejects.
    """
    # Defensive filter: drop universal wrapper names before alias construction
    # so that any call site (present or future) cannot introduce duplicates.
    names = [n for n in names if n not in _UNIVERSAL_WRAPPER_NAMES]
    result = []
    lookup = short_description_lookup or {}
    for name in names:
        # D2-min: for operation-category aliases (= passthrough rules in
        # ``_OPERATION_RULES``: web__*, file__*, memory_operation__*,
        # reyn_source__*, rag_operation__*, mcp.operation__*, exec__*),
        # surface the target ToolDefinition's real description + JSON
        # schema directly. Without this the alias arrives at the LLM as
        # `description: "Direct alias for X. Use invoke_action for schema
        # details."` + `parameters: {properties: {}, additionalProperties:
        # true}` — the LLM has neither a use-case hint nor an arg
        # signature, falls back to its training prior ("AI cannot access
        # external sites") and refuses, or hallucinates control-character
        # tool-call text. The FP-0034 D2 "hot list direct alias は full
        # schema 提供" intent is realised here for the passthrough
        # categories; resource categories (skill__X / agent.peer__X /
        # mcp.tool__X.Y / memory_entry__X / rag_corpus__X) need per-
        # resource schema introspection and are out of scope for D2-min.
        rich = _operation_alias_metadata(name) or _resource_alias_metadata(
            name,
            skill_metadata_lookup=skill_metadata_lookup,
            mcp_tool_lookup=mcp_tool_lookup,
        )
        if rich is not None:
            description, parameters = rich
        else:
            short_desc = lookup.get(name, "")
            if short_desc:
                description = (
                    f"{short_desc}. "
                    f"Use this direct alias to invoke {name} without going "
                    "through invoke_action."
                )
            else:
                description = (
                    f"Direct alias for {name}. "
                    "Use invoke_action for schema details."
                )
            parameters = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })
    return result


def _operation_alias_metadata(
    qualified_name: str,
) -> "tuple[str, dict] | None":
    """Return ``(description, parameters)`` for an operation-category alias.

    Scoped to qualified names whose category routes through ``_OPERATION_RULES``
    in ``reyn.tools.universal_dispatch`` (= ``_passthrough_args`` transform —
    the alias's args are forwarded verbatim to the target). For those, the
    target ``ToolDefinition.description`` and ``ToolDefinition.parameters``
    are the correct alias metadata.

    Returns ``None`` for resource-category aliases (skill / agent.peer /
    mcp.tool / memory_entry / rag_corpus) — those route through
    ``_RESOURCE_RULES`` whose target is a generic dispatcher (``invoke_skill``
    etc.) whose parameters do NOT match the resource's actual input schema.
    Those need per-resource schema introspection (D2-full).
    """
    # Late imports to avoid circular dependency at module load time.
    from reyn.tools import get_default_registry
    from reyn.tools.universal_dispatch import (
        KNOWN_STATIC_QUALIFIED_NAMES,
        resolve_describe_action,
    )

    if qualified_name not in KNOWN_STATIC_QUALIFIED_NAMES:
        return None
    try:
        resolved = resolve_describe_action(qualified_name)
    except Exception:
        return None
    tool = get_default_registry().lookup(resolved.target_tool_name)
    if tool is None:
        return None
    return tool.description, dict(tool.parameters)


def _resource_alias_metadata(
    qualified_name: str,
    *,
    skill_metadata_lookup: "dict[str, dict] | None" = None,
    mcp_tool_lookup: "dict[str, dict] | None" = None,
) -> "tuple[str, dict] | None":
    """Return ``(description, parameters)`` for a resource-category alias
    whose schema can be derived from either the routing target (static
    categories) or session-time per-resource metadata (dynamic categories).

    Covered:
      - ``agent.peer__<name>`` (D2-full step 1) — accepts ``{request, ...}``;
        rule curries ``to=<name>``. Source: ``delegate_to_agent`` parameters
        minus ``to``.
      - ``mcp.server__<name>`` (step 1) — accepts ``{}``; rule curries
        ``server=<name>``. The action IS "list this server's tools".
      - ``rag_corpus__<name>`` (step 1) — accepts ``{query, top_k?, ...}``;
        rule curries ``sources=[<name>]``. Source: ``recall`` parameters
        minus ``sources``.
      - ``skill__<name>`` (step 2) — caller supplies ``skill_metadata_lookup``
        keyed by qualified name with ``{description?, input_schema?,
        input_wrapped?}``. The transform ``_invoke_skill_args`` wraps caller
        args under the artifact ``data`` slot, so the alias's parameters
        are the artifact's data schema directly (= ``input_schema``).
      - ``mcp.tool__<server>.<tool>`` (step 3) — caller supplies
        ``mcp_tool_lookup`` keyed by qualified name with ``{description?,
        input_schema?}`` from the MCP server's declared tool schema.

    Returns ``None`` for:
      - any unhandled category, e.g. ``memory_entry__X`` — the current
        ``_read_memory_body_args`` transform sends ``{name: entry}`` but
        the target ``read_memory_body`` expects ``{layer, slug}``;
        pre-existing dispatch shape mismatch, surface separately.
    """
    from reyn.tools import get_default_registry
    from reyn.tools.universal_catalog import split_qualified_name

    try:
        category, entry_name = split_qualified_name(qualified_name)
    except ValueError:
        return None

    registry = get_default_registry()

    # Phase 1 collapse (2026-05-25): ``agent.peer__<name>`` resource alias
    # removed — multi_agent__delegate (operation alias) carries the schema
    # via _operation_alias_metadata and the dynamic ``to`` enum via
    # _enrich_router_schema. No per-peer alias is built anymore.

    if category == "mcp.server":
        params = {"type": "object", "properties": {}, "required": []}
        description = (
            f"List the MCP tools exposed by server {entry_name!r}. "
            f"Returns name + description for each tool."
        )
        return description, params

    if category == "rag_corpus":
        tool = registry.lookup("recall")
        if tool is None:
            return None
        params = _drop_required_field(dict(tool.parameters), "sources")
        first_line = tool.description.splitlines()[0] if tool.description else ""
        description = (
            f"Recall (semantic search) against indexed source {entry_name!r}. "
            f"Single-source variant of: {first_line}"
        )
        return description, params

    if category == "skill":
        meta = (skill_metadata_lookup or {}).get(qualified_name)
        if not meta or "input_schema" not in meta:
            return None
        schema = dict(meta["input_schema"])
        desc_body = meta.get("description") or f"Skill {entry_name!r}"
        description = (
            f"{desc_body}. Hot-list direct alias for skill {entry_name!r} "
            f"— pass the skill's input fields as args; the dispatcher wraps "
            f"them into the input artifact for invoke_skill."
        )
        return description, schema

    if category == "mcp.tool":
        meta = (mcp_tool_lookup or {}).get(qualified_name)
        if not meta or "input_schema" not in meta:
            return None
        schema = dict(meta["input_schema"])
        desc_body = meta.get("description") or f"MCP tool {entry_name!r}"
        description = (
            f"{desc_body}. Hot-list direct alias for MCP tool "
            f"{entry_name!r} — pass the tool's declared inputSchema args; "
            f"the dispatcher routes via call_mcp_tool."
        )
        return description, schema

    if category == "memory_entry":
        # E2e-coder 2026-05-17 N4 probe: memory_entry__<slug> aliases were
        # previously marked unhandled because _read_memory_body_args sent
        # {name: slug} while read_memory_body required {layer, slug} — that
        # transform is now fixed (universal_dispatch.py) to send the
        # canonical {layer: "shared", slug} pair, so the alias can be
        # surfaced with an empty input schema (the qualified name encodes
        # the slug; layer defaults to "shared").
        meta = (skill_metadata_lookup or {}).get(qualified_name) or {}
        desc_body = meta.get("description") or f"shared memory entry {entry_name!r}"
        description = (
            f"Read the body of shared memory entry {entry_name!r}. "
            f"{desc_body}"
        )
        params = {"type": "object", "properties": {}, "required": []}
        return description, params

    return None


def _enumerate_shared_memory_entries(host: Any) -> dict[str, dict]:
    """List shared memory entries as ``memory_entry__<slug>`` → metadata.

    Scans the shared memory layer's directory (= ``<cwd>/.reyn/memory``) for
    ``*.md`` files, ignoring the ``MEMORY.md`` index. The returned mapping is
    keyed by qualified action name so the caller can:

      - extend the hot-list seed (so the alias appears in ``tools=`` without
        the LLM running a discovery ``list_actions(category=['memory_entry'])``
        first), and
      - populate the alias metadata lookup so ``_resource_alias_metadata``
        renders a human description from the entry's frontmatter rather
        than a generic placeholder.

    Returns an empty dict when the layer's directory is absent, a host
    method is missing, or the filesystem read raises — this surface is
    advisory (= a missing entry just means the LLM has to discover it via
    ``list_actions``), so failures are silently absorbed rather than raised.

    Used by 2026-05-17 N4 fix; see project memory entry for context.
    """
    out: dict[str, dict] = {}
    memory_dir_fn = getattr(host, "memory_dir", None)
    if memory_dir_fn is None:
        return out
    try:
        mem_dir = Path(memory_dir_fn("shared"))
    except Exception:
        return out
    if not mem_dir.is_dir():
        return out
    for md_file in sorted(mem_dir.glob("*.md")):
        if md_file.name == "MEMORY.md":
            continue
        slug = md_file.stem
        meta: dict = {"description": f"shared memory entry {slug!r}"}
        # Best-effort frontmatter parse to surface the user-facing
        # "description" field. Files without a parseable frontmatter
        # fall back to the generic placeholder above.
        try:
            text = md_file.read_text(encoding="utf-8")
            stripped = _strip_frontmatter(text)
            if stripped != text:
                # frontmatter present; extract description line
                lines = text.split("\n")
                in_fm = False
                for line in lines:
                    s = line.strip()
                    if s == "---":
                        if in_fm:
                            break
                        in_fm = True
                        continue
                    if in_fm and s.startswith("description:"):
                        desc = s.split(":", 1)[1].strip()
                        if desc:
                            meta["description"] = desc
                        break
        except OSError:
            pass
        out[f"memory_entry__{slug}"] = meta
    return out


def _drop_required_field(params: dict, field_name: str) -> dict:
    """Return a copy of a JSON schema with ``field_name`` removed from
    ``properties`` and ``required``.

    Used by ``_resource_alias_metadata`` to remove curried fields from a
    target tool's parameters before exposing them on the alias.
    """
    out = dict(params)
    props = dict(out.get("properties") or {})
    props.pop(field_name, None)
    out["properties"] = props
    req = [r for r in (out.get("required") or []) if r != field_name]
    out["required"] = req
    return out


def _apply_tool_exclusions(
    tools: list[dict], exclude_tools: "frozenset[str] | set[str]"
) -> list[dict]:
    """Drop tools whose function name is in ``exclude_tools`` from the catalog.

    The post-build filter that hides tools from the LLM-visible catalog (and,
    in lockstep, from the dispatch catalog — both derive from this same
    ``tools`` list). Two callers: the plan sub-loops pass ``{"plan"}`` so plan
    steps cannot recursively self-decompose (planner.py), and the #187
    faithful SWE-eval passes the web tools (``web__search`` / ``web__fetch``)
    so the general agent solves from the repo + issue, not a web lookup of the
    gold solution. ``exclude_tools`` empty → ``tools`` returned unchanged.

    P7-clean: no hardcoded tool names; the exclusion set is data supplied by
    the caller.
    """
    if not exclude_tools:
        return tools
    return [
        t for t in tools
        if t.get("function", {}).get("name") not in exclude_tools
    ]


# ---------------------------------------------------------------------------
# RouterLoop
# ---------------------------------------------------------------------------

class RouterLoop:
    """Drives the chat router via native LLM tool_use.

    Loops: build tools+prompt → call_llm_tools → if tool_calls, execute
    in parallel, append results to messages, repeat → if text, emit to
    outbox and stop. Bounded by max_iterations.
    """

    def __init__(
        self,
        host: RouterLoopHost,
        chain_id: str,
        max_iterations: int = 5,
        router_model: str = "light",  # config tier (light = intent classification)
        budget: Any = None,  # BudgetTracker | None — process-shared cost tracker
        system_prompt_override: str | None = None,
        non_interactive: bool = False,  # #1440 followup: run-once (no TTY) → live router SP proceeds instead of asking a clarifying question (13398). Threaded from ChatSession.
        exclude_tools: set[str] | None = None,
        memo_provider: Any = None,  # SubLoopMemoProvider | None (ADR-0025)
        skill_search_config: "SkillSearchConfig | None" = None,  # FP-0024-A BM25 pre-filter
        empty_stop_retry_directive: str | None = None,  # B42-NF-W6-1 opt-in retry
        empty_stop_retry_auto: bool = False,  # #187: always-on (no env opt-in); all prod sites pass True
        plan_invalid_retries: int = 1,  # B51 NF-W6-3 — safety.loop.plan_invalid_retries
        llm_caller: "Any | None" = None,  # Tier 2 test seam: real-fake injection
    ):
        self.host = host
        self.chain_id = chain_id
        self.max_iterations = max_iterations
        self.router_model = router_model
        self.budget = budget
        # #1092 PR-C-2.5: phase-relative op-dispatch counter for crash-resume WAL
        # memoization. Only advanced when the host opts a dispatch into phase-memo
        # mode (``host.op_dispatch_memo()`` non-None); chat hosts never do, so this
        # stays 0 and the chat dispatch path is byte-identical. The counter +
        # ``phase`` form the ``op_invocation_id`` (``<phase>.<idx>``) that
        # ``dispatch_tool`` memoizes against committed WAL steps on resume.
        self._phase_op_idx = 0
        # When set, RouterLoop skips ``build_system_prompt(host=...)`` and uses
        # this string verbatim as the system message. Plan executor uses this
        # to inject a step-specific narrow prompt (= "you are executing step X
        # of a plan") instead of the full chat router prompt. The host facade
        # still controls the tool catalog narrowing.
        self._system_prompt_override = system_prompt_override
        # #1440 followup: run-once (no interactive user) → the LIVE chat-router SP
        # (built at the build_system_prompt call below) must omit the "ask ONE
        # clarifying question" directive. The original #1440 wired only the
        # session-side _build_router_system_prompt (override/budget path), missing
        # this live path → run-once still dead-stopped (13398). Threaded from
        # ChatSession._non_interactive via the constructor.
        self._non_interactive = bool(non_interactive)
        # Tool names to drop from the catalog (= post-build filter). Used by
        # plan executor to pass ``{"plan"}`` so plan steps cannot recursively
        # call plan (= prevents unbounded nesting). Discovered 2026-05-07:
        # without this, plan-mode dogfood "Read README and CLAUDE.md, then
        # compare" produced 3 plan invocations because step LLMs saw plan
        # in their tool catalog and self-decomposed.
        self._exclude_tools: frozenset[str] = frozenset(exclude_tools or set())
        # FP-0034 Phase 2 step 1: action embedding index background
        # build task handle.  None until the first turn that finds the
        # index configured + not ready, then asyncio.Task while
        # building.  Stays set after completion so we don't re-spawn
        # (the build itself is idempotent via catalog hash, but we
        # avoid the extra Task overhead).
        self._action_index_build_task: "asyncio.Task[None] | None" = None
        # ADR-0025: optional sub-loop LLM call memoization. When set,
        # ``call_llm_tools`` invocations consult the provider before
        # invoking — args_hash hit returns the recorded LLMToolCallResult
        # without paying LLM cost. Used by plan-mode resume so a crashed
        # mid-step sub-loop replays its earlier LLM turns from snapshot
        # rather than re-paying. ``None`` = normal execution (no memo).
        self._memo_provider = memo_provider
        # FP-0024-A: BM25 skill pre-filter config. None = use OS defaults.
        self._skill_search_config = skill_search_config
        # B42-NF-W6-1: directive used as a continuation prompt when an empty
        # stop is detected after a tool-call round. None (= default) preserves
        # the existing chat-router "observe + surface" policy. The plan
        # executor passes a plan-step-appropriate directive ("now report what
        # you found") so the post-tool empty-stop attractor that hits 10/10
        # on Gemini 2.5 Flash Lite (and is documented across providers — see
        # platform.claude.com handling-stop-reasons docs) can be broken with
        # one retry. Even when set, the actual retry behaviour is gated by
        # the ``REYN_EMPTY_STOP_RETRY`` env var so operators opt in per
        # process — the directive plumbing lands in the codebase but no
        # default runtime behaviour change.
        self._empty_stop_retry_directive = empty_stop_retry_directive
        # #187: enable the empty-stop retry WITHOUT requiring the
        # ``REYN_EMPTY_STOP_RETRY`` env var. owner decision (2026-06-07):
        # UNIFORM always-on at every production RouterLoop site (chat /
        # plan-step / agent op-loop all pass ``True``) — the env opt-in is
        # retired. A content-less empty stop is a dead-end the loop must
        # recover from (real-task: 67% premature). The gate below fires when
        # this flag OR the env var is set. The default stays ``False`` only so
        # direct/test construction can exercise the env-gated path; it is NOT a
        # per-site agent-on/chat-off knob (that site-appropriate design was
        # retracted). If a measured problem later motivates per-site
        # divergence, this flag is the switch — but uniform-first by default.
        self._empty_stop_retry_auto = empty_stop_retry_auto
        # B51 NF-W6-3: plan_invalid self-correction retry cap. When the
        # ``plan`` tool returns ``{status:error, error:{kind:
        # plan_invalid}}`` (= the LLM's ``steps_json`` failed JSON
        # validation, typically from unescaped ``"`` inside step
        # description strings), the router loop appends a sanitised
        # directive carrying the parser error and re-enters the LLM
        # loop. This counter caps the per-turn directive injections so
        # a persistently-failing LLM is bounded; the outer caps
        # (``max_router_calls_per_turn`` + ``max_iterations``) still
        # apply on top. ``0`` disables the retry (= pre-fix behaviour).
        self._plan_invalid_max_retries = max(0, int(plan_invalid_retries))
        # Tier 2 test seam: when set, ``run()`` calls this callable instead of
        # the module-level ``call_llm_tools``. Allows real-fake injection
        # (= scripted async callable) without ``unittest.mock.patch`` — per
        # testing.ja.md hard rule that forbids ``MagicMock / AsyncMock /
        # patch``. Production callers leave this as ``None``.
        self._llm_caller = llm_caller
        self._catalog: dict[str, dict] = {}  # populated per run()
        self._tool_names: frozenset[str] = frozenset()  # kept for backward compat
        self._total_usage: TokenUsage = TokenUsage()

    @property
    def total_usage(self) -> TokenUsage:
        """Accumulated token usage across all LLM calls made in this loop."""
        return self._total_usage

    async def run(self, user_text: str, history: list[dict]) -> TokenUsage:
        """Process one user utterance end-to-end. Emits to host.put_outbox.

        Returns the total TokenUsage accumulated across all LLM calls so the
        caller can credit it to the session-level usage counter (F4 Bug 2).
        """
        self._total_usage = TokenUsage()
        host = self.host
        # #1092 PR-A (FD1, ADR-0036): catalog-source REPLACE seam. A phase host
        # supplies its op tool catalog (allowed_ops via _build_phase_tool_catalog),
        # which REPLACES chat-discovery — a phase has no skills/agents/mcp/universal
        # (#1212 PR3 decision A). getattr-fallback so chat / plan-step hosts (no such
        # method) keep the existing chat-discovery tool-build byte-identically.
        _phase_op_catalog_getter = getattr(host, "get_phase_op_catalog", None)
        _phase_op_catalog = _phase_op_catalog_getter() if _phase_op_catalog_getter else None
        all_skills = host.list_available_skills()
        # FP-0024 Component A — BM25 skill pre-filter.
        # Narrow available_skills to top-K BM25 keyword matches when the
        # catalogue exceeds the threshold. Falls through to full enum on
        # 0 BM25 results so no skill can become invisible (Fallback safety).
        skills_for_tools = self._apply_skill_search(all_skills, user_text)
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list recording.
        # Resolved once per run() so recording below can reuse without re-fetching.
        _tracker_getter = getattr(host, "get_action_usage_tracker", None)
        _tracker = _tracker_getter() if _tracker_getter else None
        # FP-0034 PR-3b-iii: read universal wrapper visibility from host.
        # getattr fallback so narrow hosts (= plan-mode sub-host) that
        # don't implement the method default to off (= preserve prior
        # plan-step tools= shape).
        _univ_enabled_getter = getattr(
            host, "get_universal_wrappers_enabled", None,
        )
        _univ_enabled = bool(_univ_enabled_getter()) if _univ_enabled_getter else False
        # Same getattr-fallback pattern: hosts without get_cwd (= FakeRouterHost
        # in LLMReplay tests) skip the Environment section so the SP byte
        # content stays unchanged for cached fixtures.
        _cwd_getter = getattr(host, "get_cwd", None)
        _cwd_str = _cwd_getter() if _cwd_getter else None
        # FP-0034 Phase 2 step 1: D14 visibility gate for search_actions.
        # Only show search_actions when (a) wrappers are on, (b) the
        # operator configured an embedding model class, AND (c) the
        # session has an ActionEmbeddingIndex that is_ready().  Any
        # missing signal degrades to "hide" so the LLM does not see a
        # tool whose query would return empty results.
        _search_visible = False
        if _univ_enabled:
            _idx_getter = getattr(host, "get_action_embedding_index", None)
            _provider_getter = getattr(host, "get_embedding_provider", None)
            _model_getter = getattr(host, "get_embedding_model_class", None)
            _eager_getter = getattr(host, "get_eager_embedding_build", None)
            _idx = _idx_getter() if _idx_getter else None
            _provider = _provider_getter() if _provider_getter else None
            _model_class = _model_getter() if _model_getter else None
            _eager_embedding_build = bool(_eager_getter()) if _eager_getter else False
            # B25-S5-1: when eager flag is set, await the build synchronously
            # before computing _search_visible. This pays the build cost on
            # the first turn (= once per session; subsequent turns see
            # is_ready() True via SQLite cache) but eliminates the cold-start
            # race where search_actions is hidden from the LLM on Turn 1.
            if (
                _eager_embedding_build
                and _idx is not None
                and _provider is not None
                and _model_class
                and not getattr(_idx, "is_ready", lambda: False)()
            ):
                await self._build_action_embedding_index_background(
                    _idx, _provider, _model_class,
                )
            if (
                _idx is not None
                and _model_class
                and getattr(_idx, "is_ready", lambda: False)()
            ):
                _search_visible = True
            # FP-0034 Phase 2 step 1: kick off the background build
            # when the index is configured but not yet ready.  The
            # build is idempotent (= same catalog hash → no-op) and
            # serialised by the index's internal lock.  Only spawned
            # once per RouterLoop (= per chain) via the
            # ``_action_index_build_task`` flag below.
            if (
                _idx is not None
                and _provider is not None
                and _model_class
                and not getattr(_idx, "is_ready", lambda: False)()
                and getattr(self, "_action_index_build_task", None) is None
            ):
                self._action_index_build_task = asyncio.create_task(
                    self._build_action_embedding_index_background(
                        _idx, _provider, _model_class,
                    )
                )
        # D2-wrapper scope expansion (B38): build session-level resource
        # metadata maps when universal wrappers are enabled — regardless of
        # whether a tracker is present. These maps feed both the hot-list
        # alias builder (below) AND _collect_all_session_ars_entries (which
        # needs skill / MCP tool schemas to populate the full ARS block).
        _skill_meta_map: dict[str, dict] = {}
        _mcp_tool_map: dict[str, dict] = {}
        if _univ_enabled:
            # Lever D (B23-PRE-1): build short_description map from
            # available skills so aliases embed the target's purpose.
            # FP-0034 D2-full step 2: also capture per-skill
            # ``input_schema`` (when the skill's entry artifact has
            # a structured shape) so ``skill__<name>`` hot-list
            # aliases expose the actual input parameters instead
            # of the empty ``properties: {}, additionalProperties:
            # True`` stub.
            _short_desc_map: dict[str, str] = {}
            _known_skill_names: set[str] = set()
            for _s in host.list_available_skills():
                if not isinstance(_s, dict) or "name" not in _s:
                    continue
                _qn = f"skill__{_s['name']}"
                _known_skill_names.add(_qn)
                _sd = _s.get("description") or _s.get("short_description") or ""
                if _sd:
                    _short_desc_map[_qn] = str(_sd)
                if "input_schema" in _s:
                    _skill_meta_map[_qn] = {
                        "description": str(_sd) if _sd else "",
                        "input_schema": _s["input_schema"],
                        "input_wrapped": bool(_s.get("input_wrapped", True)),
                    }
            # Issue #879: per-mcp-tool aliases (``mcp.tool__<srv>.<tool>``)
            # were removed when the mcp surface collapsed to six verb
            # actions. LLMs now dispatch tool calls through
            # ``mcp__call_tool(server, mcp_tool_name, args)`` and learn
            # the per-tool args via ``mcp__list_tools`` /
            # ``describe_mcp_tool``. The previous per-tool input-schema
            # lookup is no longer wired here.
        # FP-0034 Phase 2 step 5: hot list aliases for frequent actions.
        # Build only when universal wrappers are on and a tracker is present.
        _hot_list_aliases: list[dict] | None = None
        if _univ_enabled and _tracker is not None:
            from reyn.config import ActionRetrievalConfig as _ARC
            _ar_cfg_getter = getattr(host, "get_action_retrieval_config", None)
            _ar_cfg = _ar_cfg_getter() if _ar_cfg_getter else None
            if _ar_cfg is None:
                _ar_cfg = _ARC()
            _n = _ar_cfg.hot_list_n
            if _n > 0:
                from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
                _seed_cfg = _ar_cfg.hot_list_seed
                _seed: list[str] = (
                    list(DEFAULT_HOT_LIST_SEED)
                    if _seed_cfg == "default"
                    else (list(_seed_cfg) if isinstance(_seed_cfg, list) else [])
                )
                # N4 (2026-05-17): seed shared memory entries dynamically so
                # the LLM can read user-saved memory in a fresh session
                # without first running list_actions(category=['memory_entry']).
                # Without this, cross-session memory retrieval requires a
                # discovery step the weak default model rarely takes.
                # Populates skill_metadata_lookup so the alias gets a
                # human-readable description (= the entry's frontmatter
                # `description`).
                _memory_entries = _enumerate_shared_memory_entries(host)
                for _qn, _meta in _memory_entries.items():
                    if _qn not in _seed:
                        _seed.append(_qn)
                    # _skill_meta_map is reused as a generic
                    # qualified-name → metadata lookup; see
                    # _resource_alias_metadata's memory_entry branch.
                    _skill_meta_map.setdefault(_qn, _meta)
                # FP-0034 refactor: live (= uncompacted) tool-call records
                # are scanned on demand so the hot-list reflects in-session
                # invocations without needing per-call disk writes. Hosts
                # without the accessor (= older mocks, plan-mode sub-host)
                # degrade to compacted-table-only ranking.
                _live_records: list = []
                _live_getter = getattr(
                    host, "get_uncompacted_tool_call_records", None,
                )
                if _live_getter is not None:
                    try:
                        _live_records = list(_live_getter() or [])
                    except Exception:
                        _live_records = []
                _top_names = _tracker.get_top_n(
                    _n, _seed, live_records=_live_records,
                )
                # B38 W2: registry-existence check — filter names that pass
                # structural validation but no longer resolve to a real action
                # in the current session registry. Runs after get_top_n so
                # RouterState (skill / mcp / agent registry) is available.
                _top_names = _filter_ghost_names_by_registry(
                    _top_names,
                    skill_meta_map=_skill_meta_map or None,
                    mcp_tool_map=_mcp_tool_map or None,
                    available_agents=host.list_available_agents() or None,
                    known_skill_names=frozenset(_known_skill_names) or None,
                    # Dynamic memory_entry__<slug> names enumerated above
                    # from .reyn/memory/*.md. Empty set means "no entries
                    # exist this session" — filter rejects any stale
                    # memory_entry name still in the action_usage tracker.
                    known_memory_entries=frozenset(_memory_entries),
                )
                if _top_names:
                    _hot_list_aliases = _build_hot_list_aliases(
                        _top_names,
                        short_description_lookup=_short_desc_map or None,
                        skill_metadata_lookup=_skill_meta_map or None,
                        mcp_tool_lookup=_mcp_tool_map or None,
                    )
        # #272/#1128: compute the OS context-size signal once. It is None when
        # the window is ample (then compact stays hidden + the SP header is
        # omitted); non-None when filling (compact tool + header appear together).
        _ctx_signal = _render_context_size_signal_for_host(host)
        if _phase_op_catalog is not None:
            # #1092 PR-A (FD1): phase op catalog REPLACES chat-discovery. The
            # chat-discovery setup above ran on the phase host's stubs
            # (empty skills/agents/mcp, universal off) — harmless; its build_tools
            # result is discarded here in favor of the op catalog.
            tools = list(_phase_op_catalog)
        else:
            tools = build_tools(
                skills_for_tools,
                host.list_available_agents(),
                file_permissions=host.get_file_permissions(),
                mcp_servers=host.get_mcp_servers(),
                web_fetch_allowed=host.get_web_fetch_allowed(),
                universal_wrappers_enabled=_univ_enabled,
                search_actions_visible=_search_visible,
                hot_list_aliases=_hot_list_aliases,
                compact_visible=_ctx_signal is not None,
            )
        # #187 STEP 1c (owner principle): actions are enumerated ONLY by
        # list_actions, and their schemas ONLY by describe_action. The former
        # ARS block (B37/B38) inlined the whole session action catalog into
        # invoke_action's description — a SECOND enumeration surface that the
        # owner directive disallows — so it is removed here (its two builder
        # functions, _collect_all_session_ars_entries / _enrich_invoke_action_
        # description, are deleted as dead). Sibling-tool cross-ref pointers
        # (e.g. file__write → file__edit, #1420) hand the model the specific
        # action names it needs without re-listing the catalog; for the rest,
        # discovery is list_actions and schema is describe_action.
        tools = _apply_tool_exclusions(tools, self._exclude_tools)
        self._catalog = {t["function"]["name"]: t for t in tools}
        self._tool_names = frozenset(self._catalog.keys())  # backward compat
        if self._system_prompt_override is not None:
            system_prompt = self._system_prompt_override
        else:
            # ADR-0033: pre-fetch indexed sources before building the
            # (sync) system prompt. format_for_prompt() reads the mem
            # cache (fast path when manifest already loaded) and returns
            # the rendered section including the empty-state hint.
            indexed_sources = await get_source_manifest(
                Path.cwd()
            ).format_for_prompt()
            system_prompt = build_system_prompt(
                agent_name=host.agent_name,
                agent_role=host.agent_role,
                available_skills=host.list_available_skills(),
                available_agents=host.list_available_agents(),
                memory_index=host.get_memory_index(),
                file_permissions=host.get_file_permissions(),
                mcp_servers=host.get_mcp_servers(),
                web_fetch_allowed=host.get_web_fetch_allowed(),
                output_language=host.output_language,
                project_context=host.get_project_context(),
                indexed_sources_section=indexed_sources,
                # FP-0034 PR-3b-v: same getattr-fallback pattern as build_tools.
                # Hosts without get_universal_wrappers_enabled (= FakeRouterHost
                # in LLMReplay tests) default to False so SP byte content stays
                # unchanged for cached fixtures.
                universal_wrappers_enabled=_univ_enabled,
                cwd=_cwd_str,
                # FP-0034 §D14: propagate the search_actions D14 visibility gate
                # into the SP so the wrapper enumeration matches tools=.
                # When wrappers are off (_univ_enabled=False), pass True so the
                # SP stays byte-identical to the pre-fix output — those callers
                # are non-wrapper-path tests whose fixtures already include
                # search_actions in the wrapper line and re-recording is not
                # wanted.  When wrappers are on, _search_visible is the runtime
                # truth derived from is_search_available() (= embedding_class
                # configured + index ready); False there means the SP and tools=
                # both exclude search_actions, eliminating the N5 hallucination.
                search_actions_enabled=_search_visible if _univ_enabled else True,
                # #272/#1128: OS-injected context-size signal (header), computed
                # once above. Rendered LAST in the SP (most volatile section →
                # preserves the cached prefix above it); None when ample.
                context_size_signal=_ctx_signal,
                # #187 Stage C: weak-tier mechanical list_actions-first mandate.
                # Gated to weak tiers (light = the flash-lite-backed default
                # intent tier that under-explores the catalog); strong/unknown
                # tiers OFF (strong-flexibility-preserving). Only the chat
                # router path reaches build_system_prompt — the phase op-loop
                # uses system_prompt_override, so this does not touch it.
                discovery_mandate=tier_wants_discovery_mandate(self.router_model),
                non_interactive=self._non_interactive,  # #1440 followup: live-path wiring
            )
        # ChatSession._handle_user_message appends the user turn to history
        # BEFORE invoking _run_router_loop, so by the time we get here the
        # caller's `history` argument already ends with this turn's user
        # message. Appending it again as a trailing user message creates a
        # consecutive-duplicate-user pair that confuses the LLM (= G12-style
        # empty-stop attractor was reproduced via mcp_probe at ~80% rate
        # against gemini-2.5-flash-lite). Use history as-is; only fall back
        # to an explicit append if for some reason the latest history entry
        # is NOT this turn's user text (= defensive — keeps tests that pass
        # an empty / mismatched history alive).
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        # When history's last entry is a user message, trust it — it is
        # either:
        #   - text identical to ``user_text`` (= the normal chat path,
        #     ChatSession already appended it via _append_history); or
        #   - a content-list shape (= issue #366 multimodal turn where
        #     the user attached images via /image; comparing string
        #     ``user_text`` against the list would always fail and
        #     produce a duplicate text-only entry).
        # Only append the fallback text user message when history is
        # empty / mismatched (= defensive for direct-RouterLoop tests).
        if not history or history[-1].get("role") != "user":
            messages.append({"role": "user", "content": user_text})

        return await self.run_loop(messages, tools, _univ_enabled)

    async def run_loop(
        self,
        messages: list[dict],
        tools: list[dict],
        _univ_enabled: bool,
    ) -> "TokenUsage":
        """#1092 PR-B (FD1, ADR-0036): the shared op-execution loop (convergence ii).

        Extracted verbatim from ``run()`` so the chat ``run()`` (after its
        chat-specific pre-loop setup) AND a phase host (via a RouterLoop built with
        PhaseRouterLoopHost) drive the SAME loop = true convergence. The loop body
        is unchanged; chat-specific terminals (put_outbox spawn-acks, text reply)
        are host-polymorphic and go inert for a phase host (no-op put_outbox,
        async_count=0). FD2: the phase transition is a SEPARATE structured-json
        call the phase host post-pends AFTER this loop returns at end_turn — it is
        NOT in this loop (P1/P8 preserved).
        """
        host = self.host
        # #1092 PR-B: keep the DISPATCH catalog (``self._catalog``, consumed by
        # ``_execute_tool`` → ``dispatch_tool``'s ``name in ctx.tool_catalog`` gate)
        # in lockstep with the ADVERTISED ``tools=``. For the chat ``run()`` path
        # this is idempotent (run() already set it from the same post-exclude
        # ``tools``). For a phase host that drives ``run_loop`` directly (bypassing
        # run()'s pre-loop setup), this is the ONLY place it gets set — without it
        # a native tool_call (read_file …) advertised to the model is rejected as
        # ``unknown_tool`` (the native-dispatch catalog gap caught by #1092 dogfood).
        self._catalog = {t["function"]["name"]: t for t in tools}
        self._tool_names = frozenset(self._catalog.keys())
        # B28-Q2 Case A: per-turn counters for chat_turn_completed_inline.
        # _routing_decided_fired: set to True the first time routing_decided
        #   is emitted in this turn (= invoke_action or hot_list_alias path).
        # _tool_calls_attempted: count of tool_call rounds where the LLM
        #   invoked at least one tool (including non-catalog tools).
        _routing_decided_fired: bool = False
        _tool_calls_attempted: int = 0
        # B42-NF-W6-1: empty-stop retry counter. The empty-stop handler
        # consults this before injecting a continuation prompt + looping,
        # so retries are bounded at 1 per turn (= no infinite loops if
        # the LLM keeps returning empty stops even with the continuation
        # prompt; the second empty stop falls through to the standard
        # "observe + surface" path).
        _empty_stop_retries: int = 0
        # B51 NF-W6-3: plan_invalid retry counter. Incremented every time
        # the plan tool returns ``{status:error, kind:plan_invalid}`` and
        # the router injects a self-correction directive back into the
        # message list. Bounded by ``self._plan_invalid_max_retries``
        # (default 1 = one correction attempt per chat turn).
        _plan_invalid_retries: int = 0

        for _iteration in range(self.max_iterations):
            resolved_model = host.resolve_model(self.router_model)
            # #1092 PR-C-5 (2): per-turn phase wall-clock budget enforcement. A phase
            # host implements ``check_phase_budget`` (RAISES PhaseBudgetExceededError
            # when over budget, unless on_limit grants an extension) — the same
            # enforcement json-mode runs before each call_llm. Chat hosts don't
            # implement it (getattr → None) → no-op, chat byte-identical.
            _budget_fn = getattr(self.host, "check_phase_budget", None)
            if _budget_fn is not None:
                await _budget_fn()
            # #1092 PR-C-4b: per-turn in-loop message-history compaction. A phase
            # host implements ``maybe_compact_messages`` to proactively bound the
            # converged op-loop's growing native tool-message history (json-mode
            # parity). Chat hosts don't implement it (getattr → None) → no-op, so
            # the chat loop is byte-identical.
            _compact_fn = getattr(self.host, "maybe_compact_messages", None)
            if _compact_fn is not None:
                messages = await _compact_fn(messages, model=resolved_model)
            # #1092 PR-C: layer-1 force-close trigger — checked AFTER compaction
            # (so it sees the shrunk content). A host implements
            # ``should_force_close`` to decide, from the current accumulated turn
            # content, whether the CUMULATIVE budget is reached; if so this turn is
            # force-closed (a clean wrap-up finish) instead of risking overflow.
            # getattr-guarded → chat/plan hosts that don't implement it → no
            # force-close (byte-identical). LOOP-FREE by construction: the
            # force-close result is a finish (no tool_calls) → the loop's terminal
            # path ends the turn; it is NOT a revert-to-normal that could churn,
            # and the layer-1 threshold sits ``offload_cap`` below the overflow
            # point so it fires gracefully BEFORE the layer-2 floor.
            _force_close_fn = getattr(self.host, "should_force_close", None)
            _force_close_now = bool(
                _force_close_fn is not None
                and await _force_close_fn(messages, model=resolved_model)
            )
            # ADR-0025: memo lookup — a recorded LLMToolCallResult for
            # this exact (model, messages, tools, tool_choice) tuple
            # short-circuits the call. Used by plan-mode resume so a
            # crashed mid-step sub-loop replays earlier LLM turns
            # without re-paying. memo_provider is None for non-resume
            # paths (= chat router main loop, fresh plan runs).
            result = None
            args_hash: str | None = None
            if self._memo_provider is not None:
                # #1092 PR-C-2.6: the memo key is host-delegated when the host
                # supplies ``compute_memo_key`` (the phase host strips volatile frame
                # fields — current_datetime — so a later-time crash-resume HITS instead
                # of MISSING + re-invoking). Chat hosts don't implement it (getattr →
                # None), so the key falls back to the message-based hash, byte-identical.
                # The SAME key is used for lookup AND record (below), so run-1's record
                # and run-2's resume lookup stay consistent.
                _memo_key_fn = getattr(self.host, "compute_memo_key", None)
                if _memo_key_fn is not None:
                    args_hash = _memo_key_fn(
                        model=resolved_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                    )
                else:
                    from reyn.plan.sub_loop_memo import compute_sub_loop_args_hash
                    args_hash = compute_sub_loop_args_hash(
                        model=resolved_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                    )
                memo = self._memo_provider.get_recorded_result(args_hash)
                if memo is not None:
                    host.events.emit(
                        "plan_step_llm_memoized",
                        chain_id=self.chain_id,
                        plan_id=getattr(self._memo_provider, "plan_id", None),
                        step_id=getattr(self._memo_provider, "step_id", None),
                        args_hash=args_hash,
                    )
                    result = memo
            if result is None:
                if _force_close_now:
                    # #1092 PR-C: replace the normal act-turn call with the wrap-up
                    # (force-close) call — swaps the SP for the wrap-up SP +
                    # suppresses tools, so the result is a finish the loop's
                    # terminal path consumes (no continuation). The phase-axis
                    # layer-2 shrink-retry (PR-B) wraps it; chat re-raises to its
                    # outer retry_loop (B′). P6 audit event before the call.
                    host.events.emit(
                        "force_close_triggered",
                        chain_id=self.chain_id,
                        iteration=_iteration,
                    )
                    result = await self._force_close_call_with_retry(
                        messages, resolved_model=resolved_model,
                    )
                    # #1092 PR-D1 (detect): hand the consolidation to the host so
                    # the OS can persist it as a checkpoint + (PR-D2) re-enter.
                    # getattr-guarded → chat hosts don't implement it (their
                    # handoff is the outer retry_loop terminal, PR-F) → no-op,
                    # byte-identical.
                    _record_fc = getattr(host, "record_force_close", None)
                    if _record_fc is not None:
                        _record_fc(result)
                else:
                    # Tier 2 testability: tests inject a real-fake callable via
                    # ``_llm_caller`` (= no unittest.mock.patch needed). None
                    # falls through to the module-level ``call_llm_tools`` so
                    # production callers don't have to know about the seam.
                    _llm = self._llm_caller or call_llm_tools
                    result = await _llm(
                        model=resolved_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        skill_name="router",
                        budget=self.budget,
                        budget_agent=host.agent_name,
                        trace_caller="router",
                    )
                # Record the fresh result for future resume hit. Defensive:
                # never let recording failure break the loop. NOT for a
                # force-close result — it is a terminal wrap-up, not a normal
                # act-turn to replay as-is on resume.
                if not _force_close_now and self._memo_provider is not None and args_hash is not None:
                    try:
                        await self._memo_provider.record(
                            args_hash=args_hash, result=result,
                        )
                    except Exception as exc:  # noqa: BLE001
                        import logging
                        logging.getLogger(__name__).warning(
                            "sub-loop memo record failed: %r", exc,
                        )
            if result.usage:
                self._total_usage += result.usage
            if result.tool_calls:
                # B28-Q2: count non-empty tool_call rounds for chat_turn_completed_inline.
                _tool_calls_attempted += 1
                # F5 fix (dogfood batch 1): dedupe duplicate async
                # tool_calls within the same round. Weak models
                # occasionally emit `delegate_to_agent` twice in one
                # tool_calls list, which would inbox_put the same
                # request twice and double-charge the peer.
                # G3 fix (dogfood batch 5 B5-M1): extend dedupe to
                # invoke_skill — same skill + same args in one round
                # spawns redundant runs (333k tokens / 51 LLM calls
                # observed). invoke_skill is sync but NOT idempotent
                # from a cost perspective; deduping is safe because
                # same args → same deterministic result.
                tool_calls = self._dedupe_tool_calls_round(result.tool_calls)
                # parallel execute all tool calls (deduped)
                tool_results = await asyncio.gather(*[
                    self._execute_tool(tc) for tc in tool_calls
                ])
                # Detect async-deferred dispatches via the canonical
                # registry (router_tools.get_dispatch_kind() →
                # ToolDefinition.dispatch_kind).  Async tools'
                # results arrive via a separate channel (e.g.
                # delegate_to_agent → PR14 pending_chain re-invokes router
                # in a future turn). The current loop can't wait for the
                # result; if we continue, the LLM would see only "dispatched"
                # status and re-dispatch (per dogfood verify_lead repro).
                # Exit after the dispatch; the future invocation resumes.
                async_count = sum(
                    1
                    for tc in tool_calls
                    if get_dispatch_kind(tc["function"]["name"]) == "async"
                )
                if async_count:
                    # B49 plan-side spawn alignment (= #441 / #445
                    # follow-up): when a plan tool dispatch is in the
                    # async batch, push a ``[task_spawned] kind=plan``
                    # structured header instead of the generic
                    # "dispatched N async request" status. This makes
                    # plan spawn-ack symmetric with skill spawn-ack
                    # (= router_loop.py:1881 path), so the LLM history
                    # carries a correlatable spawn record that pairs
                    # with the later ``[task_completed] kind=plan
                    # plan_id=<X>`` injection from
                    # session._handle_plan_completed.
                    # B49 plan-side retest discipline (= retest-before-PR
                    # surfaced this bug): dispatch_tool() wraps all
                    # invoker results as ``{"status": "ok", "data":
                    # <raw_result>}`` (see dispatch/dispatcher.py), so
                    # the spawned status is nested under ``data``. The
                    # skill spawn-ack indices block (above) already
                    # handles both forms; mirror that pattern here so
                    # plan dispatch detection is symmetric.
                    plan_idx = next(
                        (
                            i
                            for i, (tc, r) in enumerate(zip(tool_calls, tool_results))
                            if tc["function"]["name"] == "plan"
                            and isinstance(r, dict)
                            and (
                                r.get("status") == "spawned"
                                or (
                                    isinstance(r.get("data"), dict)
                                    and r["data"].get("status") == "spawned"
                                )
                            )
                        ),
                        None,
                    )
                    if plan_idx is not None:
                        tc_plan = tool_calls[plan_idx]
                        r_plan = tool_results[plan_idx]
                        plan_spawn = (
                            r_plan["data"]
                            if isinstance(r_plan.get("data"), dict)
                            else r_plan
                        )
                        plan_id = plan_spawn.get("plan_id", "")
                        plan_chain_id = plan_spawn.get("chain_id", self.chain_id)
                        n_steps = plan_spawn.get("n_steps", 0)
                        try:
                            plan_args = json.loads(
                                tc_plan["function"].get("arguments") or "{}",
                            )
                        except (json.JSONDecodeError, TypeError):
                            plan_args = {}
                        plan_goal = plan_args.get("goal", "")
                        header = (
                            f"[task_spawned] kind=plan "
                            f"plan_id={plan_id} chain_id={plan_chain_id}\n"
                            f"goal: {plan_goal}  n_steps: {n_steps}"
                        )
                        lang = getattr(host, "output_language", None)
                        trailer = _PLAN_SPAWN_ACK_MSG.get(
                            lang, _PLAN_SPAWN_ACK_MSG["en"],
                        )
                        ack_text = f"{header}\n\n{trailer}"
                        await self.host.put_outbox(
                            kind="agent",
                            text=ack_text,
                            meta={
                                "chain_id": self.chain_id,
                                "source": "plan_spawn_ack",
                            },
                        )
                        return self._total_usage
                    # B55 R-7 (2026-05-25): non-plan async dispatch (=
                    # delegate_to_agent or other peer-async tools). Mirror
                    # skill / plan spawn_ack format: `[task_spawned]
                    # kind=agent ...` header + user-facing trailer so the
                    # SP TASK_SPAWNED rule covers this path too. Prior
                    # behaviour pushed a generic `status` row with no
                    # structured header, leaving the LLM without a task
                    # lifecycle anchor when the corresponding
                    # `[task_completed] kind=agent ...` injection arrives.
                    #
                    # Extract peer / request hint from the first async
                    # tool call (= delegate_to_agent's `to` + `request`
                    # arguments). Fallback to a generic "peer agent"
                    # header when arguments aren't parseable (= defensive
                    # for non-delegate async tools or malformed args).
                    tc_first_async = None
                    for tc_a, r_a in zip(tool_calls, tool_results):
                        if (
                            isinstance(r_a, dict)
                            and r_a.get("status") == "spawned"
                        ):
                            tc_first_async = tc_a
                            break
                    peer = ""
                    request_preview = ""
                    if tc_first_async is not None:
                        try:
                            async_args = json.loads(
                                tc_first_async["function"].get("arguments")
                                or "{}",
                            )
                        except (json.JSONDecodeError, TypeError):
                            async_args = {}
                        peer = str(async_args.get("to", "") or "")
                        request_preview = str(
                            async_args.get("request", "") or "",
                        )[:200]
                    header_lines = [
                        f"[task_spawned] kind=agent "
                        f"chain_id={self.chain_id} count={async_count}",
                    ]
                    if peer:
                        header_lines.append(f"peer: {peer}")
                    if request_preview:
                        header_lines.append(f"request: {request_preview}")
                    header = "\n".join(header_lines)
                    lang = getattr(host, "output_language", None)
                    trailer = _AGENT_SPAWN_ACK_MSG.get(
                        lang, _AGENT_SPAWN_ACK_MSG["en"],
                    )
                    ack_text = f"{header}\n\n{trailer}"
                    await self.host.put_outbox(
                        kind="agent",
                        text=ack_text,
                        meta={
                            "chain_id": self.chain_id,
                            "source": "agent_spawn_ack",
                        },
                    )
                    return self._total_usage

                # ADR-0023 Phase 2.1: plan dispatch is now async — the
                # async tool exit branch above already returned. The
                # legacy synchronous "status=ok with text" special-case
                # is preserved as a safety net for hosts that lack
                # spawn_plan_task (= test stubs running planner sync via
                # the AttributeError fallback in dispatch_plan_tool).
                for tc, r in zip(tool_calls, tool_results):
                    if (
                        tc["function"]["name"] == "plan"
                        and isinstance(r, dict)
                        and r.get("status") == "ok"
                        and isinstance(r.get("text"), str)
                        and r["text"]
                    ):
                        await self.host.put_outbox(
                            kind="agent",
                            text=r["text"],
                            meta={"chain_id": self.chain_id, "source": "plan"},
                        )
                        return self._total_usage
                # G10 / B2-M2 fix: intercept invoke_skill tool_failed results and
                # emit a deterministic i18n message instead of letting the LLM
                # generate an English fallback reply. Checked before accumulating
                # messages so the LLM is never called for this error path.
                for tc, r in zip(tool_calls, tool_results):
                    if (
                        tc["function"]["name"] == "invoke_skill"
                        and isinstance(r, dict)
                        and r.get("status") == "error"
                    ):
                        try:
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        tool_name = args.get("name", "invoke_skill")
                        err_info = r.get("error", {})
                        error_msg = (
                            err_info.get("message", str(r))
                            if isinstance(err_info, dict)
                            else str(err_info)
                        )
                        lang = getattr(host, "output_language", None)
                        tmpl = _TOOL_FAILED_FALLBACK_MSG.get(
                            lang,
                            _TOOL_FAILED_FALLBACK_MSG["en"],
                        )
                        fallback = tmpl.format(
                            tool_name=tool_name, error=error_msg
                        )
                        await host.put_outbox(
                            kind="agent",
                            text=fallback,
                            meta={"chain_id": self.chain_id},
                        )
                        return self._total_usage

                # FP-0034 Phase 3: routing_decided P6 event for catalog dispatch audit.
                # Emitted independently of tracker (tracker=None is valid when
                # hot_list_n=0, but P6 audit must fire whenever catalog routing
                # actually happened). Guard: only when universal wrappers are on,
                # which is the only condition under which catalog routing occurs.
                if _univ_enabled:
                    for tc, r in zip(tool_calls, tool_results):
                        _rd_name = tc.get("function", {}).get("name", "")
                        if _rd_name == "invoke_action":
                            _rd_args = tc.get("function", {}).get("arguments", {})
                            if isinstance(_rd_args, str):
                                try:
                                    import json as _json_rd
                                    _rd_args = _json_rd.loads(_rd_args)
                                except Exception:  # noqa: BLE001
                                    _rd_args = {}
                            _rd_action = (
                                _rd_args.get("action_name", "")
                                if isinstance(_rd_args, dict) else ""
                            )
                            _rd_source = "invoke_action"
                        elif "__" in _rd_name:
                            # Issue #241: distinguish "real hot-list alias the
                            # LLM correctly used" (= name actually surfaced in
                            # tools[]) from "ARS-only direct call the salvage
                            # path covers" (= name appeared only in the
                            # invoke_action.description ARS block, salvaged
                            # by PR #240). Pre-#241, both cases were tagged
                            # ``"hot_list_alias"`` regardless, muddying the
                            # audit chain for downstream readers (B42 W5-S6).
                            _rd_action = _rd_name
                            if _rd_name in self._catalog:
                                _rd_source = "hot_list_alias"
                            else:
                                _rd_source = "ars_direct"
                        else:
                            continue  # non-catalog tool — skip
                        if not _rd_action:
                            continue
                        _rd_outcome = "error" if (
                            isinstance(r, dict)
                            and ("error" in r or r.get("status") == "error")
                        ) else "success"
                        host.events.emit(
                            "routing_decided",
                            action_name=_rd_action,
                            source=_rd_source,
                            outcome=_rd_outcome,
                            chain_id=self.chain_id,
                        )
                        _routing_decided_fired = True  # B28-Q2: track for inline exclusivity
                # H3-ablation race fix: detect invoke_skill / invoke_action
                # spawn-ack and exit the router loop instead of accumulating
                # the spawn-ack into messages for the next iteration.
                #
                # Race condition observed in dogfood batch 32 §4.2 (= W3 S1
                # file_read_via_chat) and confirmed at the OS layer by the
                # H3 ablation (= patch flipped only that single scenario;
                # K/N = 1/22 at batch scale, but the fix is structural and
                # model-agnostic — H1 strong-model ablation reproduced the
                # same "Understood" hallucination under gemini-2.5-flash,
                # confirming this is OS-layer, not LLM-layer).
                #
                # NF-W7-B43-2 (2026-05-20): the OS-synthetic spawn-ack
                # message itself became an in-context-learning attractor —
                # multi-turn conversations accumulated the literal text in
                # the assistant slot, and weak LLMs echoed it in
                # subsequent turns (10/10 deterministic in trace-patch-
                # replay). The env-gated alternative path below switches
                # to the standard role=tool + LLM-composed reply pattern
                # (= Claude / GPT alignment) with H3 hallucination
                # defense via a tool_result directive (= PR #221
                # ``_post_text`` mechanism). Default behaviour is
                # unchanged; ``REYN_SPAWN_ACK_TO_LLM=1`` opts in to the
                # new pattern.
                _spawn_ack_indices: list[int] = [
                    i
                    for i, (tc, r) in enumerate(zip(tool_calls, tool_results))
                    if (
                        tc["function"]["name"] in ("invoke_skill", "invoke_action")
                        and isinstance(r, dict)
                        and (
                            r.get("status") == "spawned"
                            or (
                                isinstance(r.get("data"), dict)
                                and r["data"].get("status") == "spawned"
                            )
                        )
                    )
                ]
                if _spawn_ack_indices:
                    host.events.emit(
                        "invoke_skill_spawn_ack_exit",
                        spawn_ack_count=len(_spawn_ack_indices),
                        chain_id=self.chain_id,
                    )
                    if os.environ.get("REYN_SPAWN_ACK_TO_LLM") == "1":
                        # NF-W7-B43-2 opt-in path: annotate each spawn-ack
                        # tool_result with the directive via ``_post_text``
                        # so the existing PR #221 serialisation layer below
                        # appends it outside the JSON body with the standard
                        # ``\n\n---\n`` separator. The loop continues
                        # normally — the next LLM iteration composes the
                        # user-facing reply (= 7/10 ACK in N=10 trace
                        # replay) with H3 hallucination defense (= 0/10
                        # hallucinate via the directive wording). Residual
                        # 3/10 EMPTY is covered by PR #265 / PR #287's
                        # ``REYN_EMPTY_STOP_RETRY=1`` retry mechanism.
                        for idx in _spawn_ack_indices:
                            r = tool_results[idx]
                            if isinstance(r, dict):
                                r["_post_text"] = _SPAWN_ACK_TOOL_DIRECTIVE
                        # Fall through to the standard
                        # ``messages.append(...)`` block below.
                    else:
                        # Default (= pre-NF-W7-B43-2) behaviour: OS pushes
                        # the deterministic spawn-ack text to the outbox
                        # and exits the loop. Preserves the existing
                        # contract (= ``meta.source="spawn_ack"``
                        # downstream consumers, no LLM composition for
                        # this turn). The in-context-learning attractor
                        # the new path closes is still present here.
                        #
                        # B49 W1-S6 follow-up (2026-05-22): prepend a
                        # structured ``[task_spawned] kind=skill ...``
                        # header so the LLM history carries a machine-
                        # correlatable spawn record that pairs with the
                        # later ``[task_completed] kind=skill run_id=<X>``
                        # injection. The user-friendly trailer
                        # (``_SPAWN_ACK_MSG``) is preserved as the second
                        # paragraph; the user still reads the human
                        # message, the LLM also sees the structured
                        # header for correlation. SP TASK_SPAWNED rule
                        # explains the header's meaning. Plan spawn-side
                        # alignment is deferred to a follow-up (= plan
                        # tool dispatch path currently continues the
                        # router instead of pushing a spawn-ack, which
                        # is a separate behaviour change beyond the
                        # format-only fix here).
                        first_idx = _spawn_ack_indices[0]
                        tc_first = tool_calls[first_idx]
                        r_first = tool_results[first_idx]
                        spawn_data = (
                            r_first["data"]
                            if isinstance(r_first.get("data"), dict)
                            else r_first
                        )
                        spawn_run_id = spawn_data.get("run_id", "")
                        spawn_chain_id = spawn_data.get("chain_id", self.chain_id)
                        try:
                            spawn_args = json.loads(
                                tc_first["function"].get("arguments") or "{}",
                            )
                        except (json.JSONDecodeError, TypeError):
                            spawn_args = {}
                        spawn_skill = (
                            spawn_args.get("name")
                            or spawn_args.get("action_name")
                            or ""
                        )
                        header = (
                            f"[task_spawned] kind=skill "
                            f"run_id={spawn_run_id} chain_id={spawn_chain_id}\n"
                            f"skill: {spawn_skill}"
                        )
                        lang = getattr(host, "output_language", None)
                        trailer = _SPAWN_ACK_MSG.get(lang, _SPAWN_ACK_MSG["en"])
                        ack_text = f"{header}\n\n{trailer}"
                        await host.put_outbox(
                            kind="agent",
                            text=ack_text,
                            meta={"chain_id": self.chain_id, "source": "spawn_ack"},
                        )
                        return self._total_usage
                # No delegation — accumulate messages for next iteration.
                # Use deduped tool_calls so the assistant message and tool
                # result messages stay in sync (matching tool_call_ids).
                assistant_content = result.content or ""
                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                })
                # E-full PR-E (#383): persist this assistant tool-call turn
                # into chat history so the next ``_build_history_for_router``
                # rebuild emits the same message sequence — without this,
                # follow-up user turns lose visibility into what tools were
                # invoked + with what args.
                #
                # ``getattr`` guard: test fakes that pre-date PR-E may not
                # implement ``append_history_entry``. Production hosts
                # (= RouterHostAdapter) always implement it.
                _append_entry = getattr(host, "append_history_entry", None)
                if _append_entry is not None:
                    _append_entry(
                        role="assistant",
                        content=assistant_content,
                        meta={"chain_id": self.chain_id, "source": "router_tool_turn"},
                        tool_calls=tool_calls,
                    )
                for tc, r in zip(tool_calls, tool_results):
                    # B41-NF-W7-1: tool handlers may attach `_post_text` to
                    # the result dict to surface a textual directive after
                    # the JSON-serialised content (= a place the LLM reads
                    # as instruction, not as part of the structured data).
                    # The field is stripped before JSON serialisation and
                    # appended outside the JSON body. P3-clean: OS handles
                    # the serialisation contract, tool handler declares
                    # intent via an optional field.
                    post_text: str | None = None
                    if isinstance(r, dict) and isinstance(r.get("_post_text"), str):
                        post_text = r["_post_text"]
                        r = {k: v for k, v in r.items() if k != "_post_text"}

                    # Issue #362: extract MCP media blocks (= image, etc.)
                    # BEFORE JSON-serialising the tool result. The blocks
                    # would JSON-stringify into the tool message as opaque
                    # base64 (= wasted tokens) — instead, surface them as a
                    # multimodal follow-up user message so vision-capable
                    # models actually see the image. Strip from `r` so the
                    # tool result text stays compact.
                    media_blocks: list[dict] = []
                    if isinstance(r, dict) and isinstance(r.get("media_blocks"), list):
                        media_blocks = list(r["media_blocks"])
                        r = {k: v for k, v in r.items() if k != "media_blocks"}

                    content_str = json.dumps(r, default=str)
                    if post_text:
                        content_str = f"{content_str}\n\n---\n{post_text}"
                    # #1128 size axis (dead-end #1): cap an oversized tool result
                    # ONCE at this chokepoint — the full body is offloaded to the
                    # #385 store (lossless) and content_str becomes a bounded
                    # preview, so BOTH consumers below (the live prompt append +
                    # the persisted-history _append_entry) get the capped form.
                    # This makes every tool turn individually compactable, so the
                    # chat retry_loop's shrink can always fold it. getattr keeps
                    # partial/test hosts a no-op.
                    _cap = getattr(self.host, "cap_tool_result", None)
                    if _cap is not None:
                        content_str = _cap(content_str)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": content_str,
                    })
                    # E-full PR-E (#383): persist this tool response so the
                    # next router turn sees what the tool returned (= image
                    # follow-up / grep result follow-up / multi-step plan
                    # use cases listed in #383). The path-ref shape from
                    # PR-C keeps storage light when results contain media.
                    if _append_entry is not None:
                        _append_entry(
                            role="tool",
                            content=content_str,
                            meta={"chain_id": self.chain_id, "source": "router_tool_turn"},
                            tool_call_id=tc["id"],
                            name=tc.get("function", {}).get("name"),
                        )
                    if media_blocks:
                        # Issue #383 PR-C: pass the host's media_store so
                        # path-ref blocks are materialised to data URLs at
                        # the LLM wire boundary. ``getattr`` keeps tests
                        # that mock the host with bare attributes happy.
                        # #272: bound the media follow-up to the budget left
                        # after the (already-capped) tool text, so the whole
                        # result turn stays ≤ the per-turn cap. Overflow media
                        # stays a small lossless ref. getattr keeps partial/test
                        # hosts unbounded (pre-#272 behaviour).
                        _media_budget = getattr(host, "media_followup_budget", None)
                        _budget_tokens = (
                            _media_budget(content_str) if _media_budget is not None else None
                        )
                        followup = _build_media_followup_message(
                            tool_name=tc.get("function", {}).get("name", "tool"),
                            media_blocks=media_blocks,
                            media_store=getattr(host, "media_store", None),
                            budget_tokens=_budget_tokens,
                        )
                        if followup is not None:
                            messages.append(followup)

                # B51 NF-W6-3: plan_invalid self-correction loop. When the
                # plan tool returned ``{status:error, error:{kind:plan_invalid}}``
                # the most common cause is the LLM forgetting to escape
                # ``"`` inside step description strings (= weak-tier JSON
                # generation failure mode, observed B50/B51 W6-S3 at ~60%
                # rate on user queries containing quoted phrases). Append a
                # sanitised directive carrying the parser error so the LLM
                # gets one corrected attempt before falling through to the
                # generic tool-error path. Bounded by
                # ``self._plan_invalid_max_retries`` (= safety.loop config).
                # Imported lazily so the existing top-level import surface
                # of router_loop stays stable.
                from reyn.chat.planner import _build_plan_invalid_retry_directive
                plan_invalid_idx = next(
                    (
                        i
                        for i, (tc, r) in enumerate(zip(tool_calls, tool_results))
                        if tc["function"]["name"] == "plan"
                        and isinstance(r, dict)
                        and isinstance(r.get("error"), dict)
                        and r["error"].get("kind") == "plan_invalid"
                    ),
                    None,
                )
                if (
                    plan_invalid_idx is not None
                    and _plan_invalid_retries < self._plan_invalid_max_retries
                ):
                    err_payload = tool_results[plan_invalid_idx]["error"]
                    err_msg = str(err_payload.get("message") or "")
                    directive = _build_plan_invalid_retry_directive(err_msg)
                    messages.append({"role": "user", "content": directive})
                    _plan_invalid_retries += 1
                    self.host.events.emit(
                        "router_plan_invalid_retry_injected",
                        directive_length=len(directive),
                        chain_id=self.chain_id,
                        retry_count=_plan_invalid_retries,
                    )
                continue

            # Option F (ADR-0021): detect empty-stop before treating as text reply.
            # Empty-stop = finish_reason="stop", content empty, no tool calls.
            # This is a provider-level glitch (observed at ~50% rate with weak
            # models — B7-G12 measurement).  Reyn does NOT retry, change context,
            # or switch models.  Responsibility: observe + surface to user.
            #
            # B42-NF-W6-1: when a continuation directive is configured AND the
            # ``REYN_EMPTY_STOP_RETRY`` env var opts in, attempt ONE retry per
            # turn with the directive appended as a synthetic user message
            # before re-entering the loop. The retry path matches the
            # Anthropic-recommended "continuation prompts as last resort"
            # pattern (= platform.claude.com handling-stop-reasons docs) and
            # the Hermes-agent #9400 community implementation. Without the
            # env var, behaviour is unchanged from the original "observe +
            # surface" policy.
            if _is_empty_router_response(result):
                # P6: emit audit event — state change must be observable.
                self.host.events.emit(
                    "router_empty_response_detected",
                    finish_reason=result.finish_reason,
                    completion_tokens=getattr(result.usage, "completion_tokens", 0)
                    if result.usage else 0,
                    prompt_tokens=getattr(result.usage, "prompt_tokens", 0)
                    if result.usage else 0,
                    caller_hint="router",
                    model=host.resolve_model(self.router_model),
                )
                # B42-NF-W6-1 detect-and-retry: env-var-gated, directive-gated,
                # max 1 retry per turn. Trace-patch-replay verified 0/10 →
                # 10/10 narration recovery on the W6-S1 plan-step empty stop.
                if (
                    self._empty_stop_retry_directive
                    and _empty_stop_retries < 1
                    and (
                        self._empty_stop_retry_auto
                        or os.environ.get("REYN_EMPTY_STOP_RETRY") == "1"
                    )
                ):
                    _empty_stop_retries += 1
                    messages.append({
                        "role": "user",
                        "content": self._empty_stop_retry_directive,
                    })
                    self.host.events.emit(
                        "router_empty_response_retry_injected",
                        directive_length=len(self._empty_stop_retry_directive),
                        chain_id=self.chain_id,
                    )
                    continue  # re-enter the loop with the directive in messages
                lang = getattr(host, "output_language", None)
                failure_text = _EMPTY_RESPONSE_MSG.get(
                    lang, _EMPTY_RESPONSE_MSG["en"]
                )
                await host.put_outbox(
                    kind="agent",
                    text=failure_text,
                    meta={
                        "chain_id": self.chain_id,
                        "source": "router_empty_response",
                    },
                )
                return self._total_usage  # no retry

            # Text reply — emit and stop
            # B28-Q2 Case A: emit chat_turn_completed_inline when no catalog
            # dispatch happened in this turn (= routing_decided never fired).
            # Mutually exclusive with routing_decided per turn (P6 audit).
            if _univ_enabled and not _routing_decided_fired:
                host.events.emit(
                    "chat_turn_completed_inline",
                    chain_id=self.chain_id,
                    decision="inline_reply",
                    tool_calls_attempted=_tool_calls_attempted,
                )
            await self.host.put_outbox(
                kind="agent",
                text=result.content or "",
                meta={"chain_id": self.chain_id},
            )
            return self._total_usage

        # max_iterations exhausted
        await self.host.put_outbox(
            kind="error",
            text=f"Router loop exceeded max iterations ({self.max_iterations}).",
            meta={"chain_id": self.chain_id},
        )
        return self._total_usage

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    def _dedupe_tool_calls_round(self, tool_calls: list[dict]) -> list[dict]:
        """Dedupe duplicate tool_calls within the same round (F5 + G3).

        Covers two categories of duplicates that weak models emit:

        1. Async tools (e.g. `delegate_to_agent`) — F5 fix (batch 1).
           Duplicates would inbox_put the same request twice, doubling
           peer cost and confusing the chain.

        2. `invoke_skill` — G3 fix (batch 5 B5-M1).
           Three identical invoke_skill calls in one round caused 333k
           tokens / 51 LLM calls. Same skill + same args → same result;
           only the first call is needed.

        Keyed on (tool_name, arguments_json). The original tool_call_id
        is preserved for the kept copy so the assistant/tool message
        alignment downstream stays intact.

        Emits a `tool_call_deduped` audit event per suppressed call.
        """
        # Tools that are dedupe candidates: async tools (by dispatch kind)
        # and invoke_skill (sync but non-idempotent from a cost standpoint).
        # Other sync tools (describe_skill, list_skills, read_file, …) are
        # deliberately excluded — dupes there are wasteful but
        # correctness-preserving and the tool_call_id count must stay
        # consistent with what the LLM emitted.
        _DEDUPE_SYNC_TOOLS: frozenset[str] = frozenset({"invoke_skill"})

        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            name = tc["function"]["name"]
            is_async = get_dispatch_kind(name) == "async"
            is_dedupe_sync = name in _DEDUPE_SYNC_TOOLS
            if is_async or is_dedupe_sync:
                key = (name, tc["function"].get("arguments", ""))
                if key in seen:
                    reason = (
                        "duplicate_async_in_round"
                        if is_async
                        else "duplicate_invoke_skill_in_round"
                    )
                    self.host.events.emit(
                        "tool_call_deduped",
                        name=name,
                        chain_id=self.chain_id,
                        reason=reason,
                    )
                    continue
                seen.add(key)
            deduped.append(tc)
        return deduped

    # Keep backward-compat alias (tests and callers that reference the old name
    # will still work; the alias delegates to the unified implementation).
    _dedupe_async_tool_calls = _dedupe_tool_calls_round

    def _build_force_close_messages(self, messages: list[dict]) -> list[dict]:
        """#1092 PR-B: rebuild ``messages`` for the wrap-up (force-close) call.

        The main system turn is replaced by the axis-independent wrap-up SP
        (``services/turn_budget``); all non-system turns (the working history)
        are kept verbatim so the model consolidates them. Any pre-existing
        system turn(s) are dropped (the wrap-up SP is the only system context
        for this call). Pure — no I/O — so it is unit-testable in isolation.
        """
        non_system = [m for m in messages if m.get("role") != "system"]
        return [
            {"role": "system", "content": wrap_up_system_prompt()},
            *non_system,
        ]

    async def _force_close_call(
        self,
        messages: list[dict],
        *,
        resolved_model: str,
    ) -> "LLMToolCallResult":
        """#1092 PR-B (force-close call): turn the CURRENT turn into a clean
        ``finish`` instead of letting it overflow the cumulative budget.

        Invoked by the per-turn trigger (PR-C, ``maybe_force_close``) when the
        accumulated turn content reaches the headroom threshold. This is NOT a
        truncate (§3): it swaps the main system prompt for the wrap-up SP and
        ADVERTISES NO TOOLS (``tools=[]``), so the model cannot continue the
        task and the small consolidation output makes ``finish_reason=stop`` the
        natural outcome. ``tool_choice`` stays ``"auto"`` (``"none"`` is not
        Gemini-safe) — moot with an empty tools list. The working history is
        preserved; only the system turn is replaced (the wrap-up SP tells the
        model to consolidate that history into a hand-off).

        This method is the single wrap-up call; the layer-2 retry-guarantee
        (overflow → host-delegated compaction shrink → retry, monotonic) wraps
        it in a follow-up commit of this same PR. Additive + unwired here, so the
        chat/phase loops stay byte-identical until PR-C wires the trigger.
        """
        _llm = self._llm_caller or call_llm_tools
        wrap_messages = self._build_force_close_messages(messages)
        # #1092 PR-E (by-construction floor): HARD-CAP the wrap-up output at
        # output_reserve via max_tokens, so the consolidation is ≤ output_reserve
        # by construction (not just by the wrap-up SP's "be concise"). With
        # assert_turn_budget_bounds (output_reserve + offload_cap < threshold), the
        # re-injected checkpoint then provably sits below the threshold → the
        # re-entry makes progress → termination. The cap rides ModelSpec.kwargs →
        # call_llm_tools' spec.kwargs → litellm (llm.py). Host-provided
        # (``wrap_up_output_reserve``); chat hosts return None → no cap (PR-F).
        _model: Any = resolved_model
        _reserve = getattr(self.host, "wrap_up_output_reserve", None)
        if _reserve is not None:
            from reyn.llm.model_resolver import ModelSpec
            _model = ModelSpec(model=resolved_model, kwargs={"max_tokens": int(_reserve)})
        return await _llm(
            model=_model,
            messages=wrap_messages,
            tools=[],            # continuation suppression: no tool to call
            tool_choice="auto",  # "none" is not Gemini-safe; moot with tools=[]
            skill_name="router",
            budget=self.budget,
            budget_agent=self.host.agent_name,
            trace_caller="router_force_close",
        )

    async def _force_close_call_with_retry(
        self,
        messages: list[dict],
        *,
        resolved_model: str,
    ) -> "LLMToolCallResult":
        """#1092 PR-B layer-2 (PHASE axis): the force-close call, made robust to
        its OWN overflow via overflow → host shrink → retry, monotonic to the
        floor (§5 layer-2 retry-guarantee).

        Axis split (B′, lead-coder confirmed): the CHAT axis does NOT use this —
        a chat force-close overflow propagates to the session's existing outer
        ``retry_loop`` (the proven head/middle/tail shrink), so the shrink hook
        is phase-host-only and ``getattr``-guarded; when absent (chat host) the
        overflow is re-raised to that outer loop. The PHASE host drives
        ``run_loop`` directly with no such wrapper, so it shrinks in-loop here via
        ``maybe_compact_messages`` (the SAME hook json-mode parity uses).

        Monotonic termination: each shrink that changes the messages strictly
        reduces them; when the host can shrink no further it returns the messages
        unchanged (identity) = the FLOOR. Until PR-D wires the handoff, reaching
        the floor RAISES (floor-abort) — PR-D replaces that terminal with the
        consolidate+hand-off (the §2 "UnrecoveredError → 区切って継続" swap), and
        PR-E establishes by construction that the floor always fits the wrap-up
        call (floor_content + T_wrap_SP + output_reserve ≤ T_max), so the
        replacement is total.
        """
        shrink = getattr(self.host, "maybe_compact_messages", None)
        cur = messages
        while True:
            try:
                return await self._force_close_call(
                    cur, resolved_model=resolved_model,
                )
            except Exception as exc:  # noqa: BLE001
                if not _is_context_overflow_error(exc):
                    raise
                if shrink is None:
                    # Chat host: no in-loop shrink — propagate to the outer
                    # session retry_loop (B′ axis-inherited path).
                    raise
                shrunk = await shrink(cur, model=resolved_model)
                if shrunk is cur or shrunk == cur:
                    # Floor: the host can shrink no further. Pre-PR-D this is a
                    # genuine terminal → re-raise (floor-abort). PR-D replaces it
                    # with the handoff.
                    raise
                cur = shrunk

    async def _execute_tool(self, tc: dict) -> dict:
        """Dispatch one tool call via dispatch_tool (cross-cutting concerns).

        Returns the tool_result content (will be JSON-serialized into the
        next round's messages).

        Issue #229 fallback (= ARS-only direct call salvage):
        weak LLMs sometimes read a qualified name from the ARS block
        inside ``invoke_action.description`` and emit it as a direct
        ``function_call`` rather than wrapping with
        ``invoke_action(action_name=..., args=...)``.  The name lands
        in ``self._catalog`` only when an actual hot-list alias was
        surfaced; ARS-only entries don't get a top-level tool slot, so
        the dispatcher would otherwise reject with ``unknown_tool``.
        When the missed name resolves through ``universal_dispatch``,
        rewrite the call as ``invoke_action(action_name=name, args=args)``
        and dispatch via the wrapper path so the user-visible behavior
        matches what the LLM intended.
        """
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

        if name not in self._catalog and "__" in name:
            name, args = self._maybe_salvage_qualified_direct_call(name, args)

        # #1406: execution-level exclude enforcement. ``exclude_tools`` is not just
        # an advertisement filter (#1400 ``_apply_tool_exclusions`` hides excluded
        # tools from ``tools[]`` / ``self._catalog``) — the LLM can still call an
        # excluded tool by name, which the #229 salvage rewrites to
        # ``invoke_action(action_name=<excluded>)`` (or it is called as
        # ``invoke_action`` directly), and ``universal_dispatch`` then resolves and
        # EXECUTES it (the #187 N=3 web__search leak). Compute the effective
        # resolved action — unwrap ``invoke_action`` — and reject if excluded.
        # Covers all three bypass paths (native direct / salvaged / direct
        # invoke_action). Catalog filter (#1400) stays as the hide layer
        # (defense-in-depth). A distinct ``tool_excluded`` kind + decision-enabling
        # message lets the model adjust ([[deny-message-decision-enabling]]).
        if self._exclude_tools:
            effective = args.get("action_name") if name == "invoke_action" else name
            if effective in self._exclude_tools:
                return {
                    "status": "error",
                    "error": {
                        "kind": "tool_excluded",
                        "message": (
                            f"tool {effective!r} is excluded this session and not "
                            "available; do not call it (directly or via "
                            "invoke_action)."
                        ),
                    },
                }

        # #1092 PR-C-2.5: phase-mode op-dispatch WAL memoization. A phase host
        # returns the per-phase resume wiring; chat hosts don't implement the hook
        # (getattr → None), so the chat dispatch path below is byte-identical
        # (``caller_kind="router"``, no state_log/skill_run_id → no WAL step).
        memo = getattr(self.host, "op_dispatch_memo", lambda: None)()
        if memo is not None:
            # Phase op: thread state_log + skill_run_id + resume_plan + a
            # phase-relative op_invocation_id into dispatch_tool so the op WAL-step
            # records (and memo-HITS on resume — no re-execution). This restores the
            # json-mode-equal crash-resume HARD GATE (#1225 Decision A) for the
            # converged op-loop, which the registry dispatch otherwise bypassed.
            op_invocation_id = f"{memo['phase'] or 'phase'}.{self._phase_op_idx}"
            self._phase_op_idx += 1
            dctx = DispatchContext(
                caller_kind="skill_phase",
                caller_id=self.host.agent_name,
                chain_id=self.chain_id,
                tool_catalog=self._catalog,
                events=self.host.events,
                state_log=memo["state_log"],
                skill_run_id=memo["skill_run_id"],
                phase=memo["phase"],
                resume_plan=memo["resume_plan"],
            )
            return await dispatch_tool(
                name=name,
                args=args,
                ctx=dctx,
                invoker=functools.partial(self._invoke_router_tool, name),
                op_invocation_id=op_invocation_id,
            )

        dctx = DispatchContext(
            caller_kind="router",
            caller_id=self.host.agent_name,
            chain_id=self.chain_id,
            tool_catalog=self._catalog,
            events=self.host.events,
        )

        return await dispatch_tool(
            name=name,
            args=args,
            ctx=dctx,
            invoker=functools.partial(self._invoke_router_tool, name),
        )

    def _maybe_salvage_qualified_direct_call(
        self, name: str, args: dict,
    ) -> tuple[str, dict]:
        """Issue #229: rewrite an ARS-only direct call into invoke_action.

        Triggered when the LLM emitted ``<category>__<entry>`` as a
        direct function call but the name isn't in ``self._catalog``
        (= it wasn't a hot-list alias, only an ARS schema-hint entry).
        Returns ``(name, args)`` unchanged when ``universal_dispatch``
        cannot resolve the qualified name — the original ``unknown_tool``
        rejection path then surfaces the error normally.

        Audit event ``direct_alias_call_salvaged`` records the rewrite
        so we can count how often this fires in dogfood and inform
        whether the ARS block wording fix (= ``β`` in #229) reduces
        the rate over time.
        """
        try:
            from reyn.tools.universal_dispatch import (
                UnknownActionError,
                resolve_invoke_action,
            )
        except Exception:  # noqa: BLE001
            return name, args
        try:
            resolve_invoke_action(name, args or {})
        except UnknownActionError:
            return name, args
        except Exception:  # noqa: BLE001 — never crash the dispatch on a salvage attempt
            return name, args
        rewritten_args = {"action_name": name, "args": dict(args or {})}
        try:
            self.host.events.emit(
                "direct_alias_call_salvaged",
                original_name=name,
                rewritten_to="invoke_action",
                chain_id=self.chain_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return "invoke_action", rewritten_args

    # Capabilities dispatched via the unified ToolRegistry (ADR-0026 M4 Phase 3
    # step 2). Their handlers in `src/reyn/tools/` delegate via typed
    # RouterCallerState callable fields populated by ``_build_router_caller_state``.
    # Tools NOT in this set fall through to the legacy if/elif tree below; the
    # set expands cluster-by-cluster as Phase 3.5 lands the remaining adapters.
    REGISTRY_DISPATCH_TOOLS: frozenset[str] = frozenset({
        # Phase 3 step 2 (commit 649a426)
        "list_skills", "describe_skill", "list_agents", "describe_agent",
        "delegate_to_agent", "plan",
        # Phase 3.5-D — zero-diff handlers (reyn_src + web).
        # reyn_src handlers are literal copies of RouterHostAdapter helpers;
        # web handlers delegate to op_runtime.web with a synthesized OpContext
        # that the read-only handlers don't consult (= behavior preserved).
        "reyn_src_list", "reyn_src_read",
        "web_search", "web_fetch",
        # B49 Step 2 verify (2026-05-22, post-PR #424): `read_tool_result`
        # was surfaced to the router LLM via `router_tools.build_tools()`
        # E3, but execution dispatch was missing here. The LLM saw the
        # tool, called it (= 2/3 shots in N=3 verify), then hit
        # `{"error": "unhandled tool: read_tool_result"}` → router empty
        # response → empty reply. Dispatch wiring belongs in the same
        # registry-path family as web_fetch / read_file / recall.
        "read_tool_result",
        # Phase 3.5-A+C — file cluster.  Handlers consume
        # RouterCallerState.op_context_factory (= host.make_router_op_context)
        # so op_runtime sees the operator-declared PermissionDecl /
        # Workspace, matching legacy router-branch behavior.
        # _normalise_router_tool_result unwraps read_file / list_directory
        # to the bare-content / bare-list shapes the host adapter returned.
        "read_file", "write_file", "delete_file", "list_directory",
        # #1092 PR-B (FD1): phase-side fine file kinds. edit_file / glob_files /
        # grep_files are registry ToolDefinitions (tools/file.py, registered in
        # tools/__init__.py) that the chat router never exposed as router tools
        # (chat uses list_directory), but they are in the phase default
        # allowed_ops (#1240). When a phase drives RouterLoop via run_loop, its
        # op catalog advertises these, so _invoke_router_tool must route them
        # through the same registry path — closing the dispatch gap that the
        # obviated op-exec seam (ADR-0036) would otherwise have needed a host
        # hook for. Chat is unaffected (chat build_tools never lists them).
        "edit_file", "glob_files", "grep_files",
        # Phase 3.5-B-light — invoke_skill.  Handler delegates via
        # RouterCallerState.run_skill_fn (= chain_id pre-bound) so PR14
        # multi-hop chain semantics propagate into nested run_skill /
        # delegate_to_agent paths.  Defense Layer B (skill-name
        # validation) is applied inside the handler.
        "invoke_skill",
        # Phase 3.5-B-mid — mcp cluster.  Handlers access the
        # RouterHostAdapter via ctx.router_state.host so the session-
        # level MCPClient cache is preserved (= no per-call re-handshake
        # when the LLM repeatedly calls list_mcp_tools / call_mcp_tool).
        # _normalise_router_tool_result unwraps list_mcp_servers and
        # list_mcp_tools dict envelopes back to bare list shape.
        "list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool",
        # Phase 3.5-B-heavy — memory cluster.  Handlers delegate via
        # RouterCallerState.{list_memory_fn, read_memory_body_fn,
        # remember_fn, forget_fn} bound to RouterLoop's private helpers
        # which consume the agent-aware ``host.get_memory_index()`` /
        # ``host.memory_path`` paths.  This preserves per-agent memory
        # privacy that the registry handlers' filesystem-direct fallback
        # cannot guarantee.
        "list_memory", "read_memory_body",
        "remember_shared", "remember_agent", "forget_memory",
        # H1/H2: RAG tools (ADR-0033 Phase 1, B17-S6-1 / B17-S8-2 fix).
        # Handlers in src/reyn/tools/recall.py and src/reyn/tools/drop_source.py
        # delegate to op_runtime.recall / op_runtime.index_drop via execute_op.
        # OpContext is constructed from ctx.router_state.op_context_factory
        # (= host.make_router_op_context) so the permission resolver and
        # intervention_bus are present for the index_drop gate.
        "recall", "drop_source",
        # #272/#1128: voluntary history compaction (handler → execute_op → compact op).
        "compact",
        # FP-0034 Phase 1: universal catalog wrappers.  Handlers in
        # src/reyn/tools/universal_catalog.py — list_actions enumerates
        # via ctx.router_state, describe_action / invoke_action route
        # via universal_dispatch.  search_actions stays included for
        # registry-completeness even though router_tools.build_tools
        # currently excludes it from the LLM-visible tools= list
        # (= Phase 2 wires the §D14 visibility gate + the real handler;
        # listing it here is harmless because the catalog already
        # filters it out before the LLM can call it).
        "list_actions", "search_actions",
        "describe_action", "invoke_action",
    })

    async def _build_action_embedding_index_background(
        self, idx: Any, provider: Any, model_class: str,
    ) -> None:
        """FP-0034 Phase 2 step 1: background ActionEmbeddingIndex build.

        Enumerates the catalog via ``LIST_ACTIONS`` against a fresh
        ``RouterCallerState`` snapshot and feeds the items into
        ``idx.build()``.  The build is idempotent (= same catalog
        hash skipped) and serialised by the index's internal lock,
        so concurrent calls are safe.

        Errors are swallowed and logged via ``host.events`` so a
        misconfigured embedding provider does not crash the chat
        session — the next turn finds ``is_ready()`` False and
        keeps ``search_actions`` hidden.
        """
        from reyn.tools import get_default_registry
        from reyn.tools.types import ToolContext
        try:
            rs = await self._build_router_caller_state()
            tool_ctx = ToolContext(
                events=self.host.events,
                permission_resolver=getattr(self.host, "permission_resolver", None),
                workspace=getattr(self.host, "workspace", None),
                caller_kind="router",
                router_state=rs,
            )
            list_actions_def = get_default_registry().lookup("list_actions")
            if list_actions_def is None:
                return
            result = await list_actions_def.handler({}, tool_ctx)
            items = result.get("items", []) if isinstance(result, dict) else []
            await idx.build(items, provider, model_class)
        except Exception as exc:
            try:
                self.host.events.emit(
                    "action_index_build_failed",
                    error=repr(exc),
                    model_class=model_class,
                )
            except Exception:
                pass

    async def _build_router_caller_state(self) -> Any:
        """Build a RouterCallerState populated with bound callbacks.

        Bindings follow the wiring contract documented in
        ``reyn.tools.types.RouterCallerState``:

        * Catalog ``_fn`` callables wrap RouterLoop's private helpers
          (``_list_skills`` / ``_describe_skill`` / ``_list_agents`` /
          ``_describe_agent``) so the registry handlers stay decoupled from
          RouterLoopHost type.
        * ``send_to_agent`` is bound with ``chain_id`` and ``depth=0`` at
          population time so the delegate handler signature stays pure
          ``(to, request)``.
        * ``dispatch_plan_tool`` is bound with ``parent_host`` / ``chain_id``
          / ``budget`` / ``router_model`` / ``available_tool_names``; the
          plan handler passes only ``args``.

        Forward-looking fields (``available_skills`` / ``available_agents``
        for schema enrichment, identity / cost / model context) are also
        populated so future handler activations have what they need.
        """
        from reyn.tools.types import RouterCallerState

        async def _send_to_agent_bound(*, to: str, request: str) -> None:
            await self.host.send_to_agent(
                to=to, request=request, depth=0, chain_id=self.chain_id,
            )

        async def _run_skill_bound(*, skill: str, input: dict) -> Any:
            return await self.host.run_skill_awaitable(
                skill=skill, input=input, chain_id=self.chain_id,
            )

        # FP-0012: non-blocking spawn binding. Only chat-mode hosts
        # implement ``spawn_skill``; plan-mode ``_PlanStepHost`` does not,
        # so the hasattr check leaves this None and invoke_skill falls
        # back to run_skill_fn (= blocking) inside plan steps.
        _spawn_skill_bound: Any = None
        if hasattr(self.host, "spawn_skill") and callable(
            getattr(self.host, "spawn_skill", None)
        ):
            async def _spawn_skill_bound_impl(*, skill: str, input: dict) -> Any:
                return await self.host.spawn_skill(
                    skill=skill, input=input, chain_id=self.chain_id,
                )
            _spawn_skill_bound = _spawn_skill_bound_impl

        async def _dispatch_plan_bound(*, args: dict) -> Any:
            from reyn.chat.planner import dispatch_plan_tool
            return await dispatch_plan_tool(
                args=args,
                parent_host=self.host,
                chain_id=self.chain_id,
                budget=self.budget,
                router_model=self.router_model,
                available_tool_names=set(self._tool_names) - {"plan"},
            )

        # FP-0034 Phase 2 prep: snapshot indexed RAG corpora for the
        # universal catalog's rag_corpus enumeration. SourceManifest
        # caches the parsed YAML in-process so this is O(1) when the
        # cache is warm (= when the system-prompt path already loaded
        # the manifest earlier in this turn). Failures (= missing file,
        # malformed YAML) degrade to an empty list — the catalog
        # handler then reports zero corpora rather than crashing.
        _rag_sources: list[Mapping[str, Any]] | None = None
        try:
            _manifest = get_source_manifest(Path.cwd())
            _entries = await _manifest.get_all()
            _rag_sources = [
                {
                    "name": e.name,
                    "description": e.description,
                    "backend": e.backend,
                    "chunk_count": e.chunk_count,
                }
                for e in _entries.values()
            ]
        except Exception:
            # Manifest unavailable (= no workspace, no .reyn/index/
            # sources.yaml, transient I/O). Treat as empty catalogue
            # rather than failing the entire tool dispatch.
            _rag_sources = None

        return RouterCallerState(
            # Catalog access (= activated handlers)
            list_skills_fn=self._list_skills,
            describe_skill_fn=self._describe_skill,
            list_agents_fn=self._list_agents,
            describe_agent_fn=self._describe_agent,
            # getattr-guarded (symmetric with ``op_context_factory`` below, #1092
            # PR-C-0): a RouterLoopCore host that is not the chat RouterHostAdapter
            # (e.g. PhaseRouterLoopHost — a phase has no skills/agents catalog) need
            # not implement these chat-discovery methods. Without the guard the
            # eager call AttributeError'd every op dispatch on the converged path.
            available_skills=list(getattr(self.host, "list_available_skills", list)()),
            available_agents=list(getattr(self.host, "list_available_agents", list)()),
            # Async dispatch (= activated handlers)
            send_to_agent=_send_to_agent_bound,
            dispatch_plan_tool=_dispatch_plan_bound,
            # Skill invocation bridge (= for invoke_skill handler;
            # Phase 3.5-B-light) — chain_id pre-bound to preserve PR14
            # multi-hop chain semantics. Plan-mode keeps using this for
            # blocking step execution; chat-mode prefers spawn_skill_fn.
            run_skill_fn=_run_skill_bound,
            # FP-0012: non-blocking spawn binding for chat-mode invoke_skill.
            # None for plan-mode hosts (= no spawn_skill method on host).
            spawn_skill_fn=_spawn_skill_bound,
            # Memory tool bridges (= for memory cluster handlers;
            # Phase 3.5-B-heavy) — bound to RouterLoop's private helpers
            # so registry handlers consume the same agent-aware
            # ``host.get_memory_index()`` / ``host.memory_path`` /
            # ``host.file_*`` paths the legacy router branches used.
            list_memory_fn=self._list_memory,
            read_memory_body_fn=self._read_memory_body,
            remember_fn=self._remember,
            forget_fn=self._forget,
            # Identity + cost + model context (forward-looking; consumed by
            # schema_enricher hooks and future activated handlers)
            chain_id=self.chain_id,
            budget=self.budget,
            router_model=self.router_model,
            available_tool_names=list(self._tool_names),
            # OpContext factory (= for file / mcp / web handlers; Phase 3.5-A+C).
            # ``getattr`` fallback keeps test stubs (= FakeRouterHost without
            # the method) compatible — the handler then uses its minimal
            # synthesis path, which is fine for tests that don't exercise
            # permission gating.
            op_context_factory=getattr(self.host, "make_router_op_context", None),
            # Host duck-type (= for mcp handlers; Phase 3.5-B-mid).  MCP
            # handlers call ``host.mcp_list_servers / mcp_list_tools /
            # mcp_call_tool`` directly to preserve the session-level
            # MCPClient cache (= no per-call re-handshake).
            host=self.host,
            # FP-0034 Phase 2 prep: rag_corpus enumeration snapshot.
            available_rag_sources=_rag_sources,
            # FP-0034 Phase 2 step 1: search_actions wiring.  All
            # three resolve via getattr fallback so narrow hosts
            # (= plan-step host, FakeRouterHost) get None and
            # search_actions degrades gracefully.
            action_embedding_index=(
                getattr(self.host, "get_action_embedding_index", lambda: None)()
            ),
            embedding_provider=(
                getattr(self.host, "get_embedding_provider", lambda: None)()
            ),
            embedding_model_class=(
                getattr(self.host, "get_embedding_model_class", lambda: None)()
            ),
            # FP-0034 Phase 2: sandbox backend name for exec D14 gate.
            # getattr fallback so narrow hosts (= FakeRouterHost, plan-step
            # host) without this method default to None, hiding exec category.
            sandbox_backend=(
                getattr(self.host, "get_sandbox_backend", lambda: None)()
            ),
            # FP-0032 follow-up: mcp_servers must be populated so the
            # universal_catalog ``mcp.server`` / ``mcp.tool`` category
            # enumerations surface the actually-configured servers.
            # Without this the enumeration sees ``rs.mcp_servers is None``
            # and returns [], leaving ``list_actions(category="mcp.server")``
            # silently empty even when ``reyn mcp list`` shows servers.
            # Shape: list[Mapping[name, description, tools?]] from host.
            mcp_servers=self.host.get_mcp_servers() if hasattr(
                self.host, "get_mcp_servers"
            ) else None,
        )

    async def _invoke_via_registry(self, name: str, args: dict) -> Any:
        """Dispatch a tool through the unified ToolRegistry handler.

        Builds a ToolContext with a populated RouterCallerState and calls
        the canonical handler from ``src/reyn/tools/<name>.py``. Wrapped
        externally by ``dispatch_tool`` (= same cross-cutting events /
        validation / error envelope as the legacy invoker path).

        Some handlers return raw op_runtime dict envelopes whereas the
        legacy router branches returned extracted shapes (= the host
        adapter did the extraction).  ``_normalise_router_tool_result``
        replicates that extraction so byte-identity with prior LLM-visible
        output is preserved (= refactor-only migration, no external spec
        change).
        """
        from reyn.tools import get_default_registry
        from reyn.tools.dispatch import invoke_tool
        from reyn.tools.types import ToolContext

        rs = await self._build_router_caller_state()
        tool_ctx = ToolContext(
            events=self.host.events,
            permission_resolver=getattr(self.host, "permission_resolver", None),
            workspace=getattr(self.host, "workspace", None),
            caller_kind="router",
            router_state=rs,
        )
        result = await invoke_tool(get_default_registry(), name, args, tool_ctx)
        return self._normalise_router_tool_result(name, result)

    @staticmethod
    def _normalise_router_tool_result(name: str, result: Any) -> Any:
        """Match registry-handler output to the legacy router-branch shape.

        File handlers in ``src/reyn/tools/file.py`` return raw op_runtime
        dict envelopes (e.g. ``{"kind": "file", "op": "read", "status":
        "ok", "content": "..."}``) but the legacy router path
        (RouterHostAdapter.file_read / file_list_directory) extracted
        ``content`` / ``entries`` before returning so the LLM saw a
        bare string / list. This helper applies the same extraction so
        registry dispatch is LLM-visible-identical to the prior path.
        """
        import json
        if name == "read_file":
            if isinstance(result, dict):
                if "content" in result:
                    return result["content"]
                return json.dumps(result)
            return result
        if name == "list_directory":
            if isinstance(result, dict):
                return result.get("entries", [result])
            return result
        if name == "list_mcp_servers":
            if isinstance(result, dict) and "servers" in result:
                return result["servers"]
            return result
        if name == "list_mcp_tools":
            # FP-0032: key renamed from "tools" to "mcp_tools"; handle both
            # for backward-compat during transition.
            if isinstance(result, dict):
                if "mcp_tools" in result:
                    return result["mcp_tools"]
                if "tools" in result:
                    return result["tools"]
            return result
        if name == "describe_mcp_tool":
            # Return the full dict (= {name, description, input_schema}).
            # No unwrapping needed — the LLM sees it as structured data.
            return result
        return result

    async def _invoke_router_tool(self, name: str, args: dict) -> Any:
        """Execute a validated tool call by name.

        Called by dispatch_tool after name/args validation. Tools in
        ``REGISTRY_DISPATCH_TOOLS`` go through the unified registry path
        (= ADR-0026); the rest fall through to the legacy if/elif tree
        until Phase 3.5 ports their handlers.
        """
        # ADR-0026 M4 Phase 3 step 2 — registry dispatch for activated tools
        if name in self.REGISTRY_DISPATCH_TOOLS:
            return await self._invoke_via_registry(name, args)

        # All router tool clusters are now dispatched via the unified
        # registry — see ``REGISTRY_DISPATCH_TOOLS`` at the top of this
        # method.  Phase 3 step 2 + Phase 3.5-D / A+C / B-light / B-mid /
        # B-heavy migrations land here; the legacy if/elif tree was
        # retained only for clusters whose adapter design needed
        # per-tool review.  When that review surfaces a new cluster /
        # capability not yet in the dispatch set, the new branch lands
        # here as the legacy stop-gap until the adapter migrates.

        # FP-0034 Phase 2 step 5: hot list direct alias dispatch.
        # Qualified names (containing "__") not in the static dispatch set
        # are hot list aliases. Route them as invoke_action so the catalog
        # dispatch handles them correctly.
        if "__" in name:
            return await self._invoke_via_registry(
                "invoke_action",
                {"action_name": name, "args": args or {}},
            )

        # Should not be reached if catalog is correct — dispatch_tool already
        # validated name is in catalog. Return error for safety.
        return {"error": f"unhandled tool: {name}"}

    # -----------------------------------------------------------------------
    # FP-0024-A: BM25 pre-filter
    # -----------------------------------------------------------------------

    def _apply_skill_search(
        self, all_skills: list[dict], query: str
    ) -> list[dict]:
        """Return the skills list to pass to build_tools.

        When the catalogue exceeds ``skill_search_config.threshold``, run
        BM25 keyword search against name + description and keep only the
        top-K candidates.  Emits a ``skill_search_invoked`` event (P6 audit)
        on every BM25 dispatch.

        Fallback safety: if BM25 returns 0 results (no keyword overlap),
        return the full catalogue unchanged so no skill becomes invisible.

        Below threshold: returns all_skills unchanged (no BM25, no event).
        """
        from reyn.config import SkillSearchConfig  # local import to break circular

        cfg: SkillSearchConfig = (
            self._skill_search_config
            if self._skill_search_config is not None
            else SkillSearchConfig()
        )

        if len(all_skills) <= cfg.threshold:
            return all_skills

        backend = BM25Backend(all_skills)
        candidates = backend.search(query, top_k=cfg.top_k)

        # P6: emit audit event for every BM25 dispatch.
        self.host.events.emit(
            "skill_search_invoked",
            query=query,
            candidates_count=len(candidates),
            top_k=cfg.top_k,
        )

        if not candidates:
            # 0 results → full enum fall-through (no skill invisibility).
            return all_skills

        candidate_names = {c.name for c in candidates}
        return [s for s in all_skills if s.get("name") in candidate_names]

    # -----------------------------------------------------------------------
    # Discovery helpers (pure, no async host calls)
    # -----------------------------------------------------------------------

    @staticmethod
    def _skill_item(s: dict) -> dict:
        """Build a list_skills item from a catalogue entry.

        Always includes ``name`` and ``description``. Passes through
        ``input_artifact`` and ``input_fields`` when present so the LLM
        sees the correct input field names before calling ``invoke_skill``
        (RETRO-H2 fix — plan D: pre-call structural context provision).

        Description is truncated to MAX_DESC_LEN_FOR_LISTING chars + "..."
        to mitigate the G12 empty-stop attractor (B7 finding: skill
        description verbosity triggers the attractor — a62a9dad / a947255e).
        describe_skill returns the full description (details on demand).
        """
        raw_desc = s.get("description", "")
        if len(raw_desc) > MAX_DESC_LEN_FOR_LISTING:
            desc = raw_desc[:MAX_DESC_LEN_FOR_LISTING] + "..."
        else:
            desc = raw_desc
        item: dict = {
            "name": s["name"],
            "description": desc,
        }
        if "input_artifact" in s:
            item["input_artifact"] = s["input_artifact"]
        if "input_fields" in s:
            item["input_fields"] = s["input_fields"]
        return item

    def _list_skills(self, path: str) -> list[dict]:
        """Browse skill catalogue hierarchically.

        path == "" → group by category, return [{category, count, sample_names}, ...]
        path == "<category>" → return [{name, description, input_artifact?, input_fields?}, ...]

        The ``sample_names`` preview (up to 5 names per category) was added to
        defuse a G12 empty-stop attractor: with only ``count`` in the
        response, the LLM had nothing concrete to narrate when answering
        "list available skills" and exited with ``finish=stop`` /
        ``content=""``. Surfacing actual skill names gives it material to
        speak with — confirmed via dogfood trace re-run.
        """
        skills = self.host.list_available_skills()

        if not path:
            # Group by category, with a small sample of names per category to
            # defuse the empty-stop attractor on path="".
            categories: dict[str, list[dict]] = {}
            for skill in skills:
                cat = skill.get("category") or "general"
                categories.setdefault(cat, []).append(skill)
            return [
                {
                    "category": cat,
                    "count": len(items),
                    # Up to 5 names per category — concrete enough to narrate,
                    # bounded enough to stay under the verbosity attractor
                    # threshold for projects with hundreds of skills.
                    "sample_names": [s.get("name", "") for s in items[:5]],
                }
                for cat, items in sorted(categories.items())
            ]

        # Return skills in the given category
        by_category = [
            self._skill_item(s)
            for s in skills
            if (s.get("category") or "general") == path
        ]
        if by_category:
            return by_category

        # path didn't match any category — try as a skill name (fallback)
        by_name = [
            self._skill_item(s)
            for s in skills
            if s.get("name") == path
        ]
        return by_name

    def _describe_skill(self, name: str) -> dict:
        """Return router-optimised entry for one skill, or error dict.

        Returns the catalogue entry with internal-only fields stripped
        (``routing``, ``category``) to keep the tool_response concise.
        The ``routing`` block (when_to_use / when_not_to_use / examples)
        averages 800–1400 chars and triggers the G12 P-b verbosity attractor
        when included in the describe_skill tool_response (Pattern D —
        B11-R2 diagnosis: 20% → 0% empty-stop after stripping).

        ``name``, ``description``, ``input_artifact``, and ``input_fields``
        are preserved — they are all the router needs to build a valid
        invoke_skill call.  (P7-clean: filtering uses OS-level field names
        in ``_DESCRIBE_SKILL_STRIP_FIELDS``, not any skill-specific strings.)
        """
        for skill in self.host.list_available_skills():
            if skill.get("name") == name:
                return {k: v for k, v in skill.items() if k not in _DESCRIBE_SKILL_STRIP_FIELDS}
        return {"error": f"skill not found: {name}"}

    def _list_agents(self, path: str) -> list[dict]:
        """Browse agent catalogue hierarchically.

        path == "" → group by cluster, return [{cluster, count}, ...]
        path == "<cluster>" → return [{name, role}, ...] for that cluster
        """
        agents = self.host.list_available_agents()

        if not path:
            clusters: dict[str, list[dict]] = {}
            for agent in agents:
                cluster = agent.get("cluster") or "default"
                clusters.setdefault(cluster, []).append(agent)
            return [
                {"cluster": cluster, "count": len(items)}
                for cluster, items in sorted(clusters.items())
            ]

        return [
            {"name": a["name"], "role": a.get("role", "")}
            for a in agents
            if (a.get("cluster") or "default") == path
        ]

    def _describe_agent(self, name: str) -> dict:
        """Return full entry for one agent, or error dict."""
        for agent in self.host.list_available_agents():
            if agent.get("name") == name:
                return agent
        return {"error": f"agent not found: {name}"}

    def _list_memory(self, path: str) -> list[dict]:
        """Browse memory hierarchically.

        path == "" → [{path: "shared", count: N}, {path: "agent", count: M}]
        path == "shared" or "agent" → sub-type counts
        path == "shared/<type>" or "agent/<type>" → items in that layer+type
        """
        memory_index = self.host.get_memory_index()
        content = memory_index.get("content", "") if memory_index.get("status") == "ok" else ""

        if not path:
            shared_count = self._count_memory_layer(content, "shared")
            agent_count = self._count_memory_layer(content, "agent")
            return [
                {"path": "shared", "count": shared_count},
                {"path": "agent", "count": agent_count},
            ]

        parts = path.split("/", 1)
        layer = parts[0]  # "shared" or "agent"

        if len(parts) == 1:
            # Return sub-categories (types) for this layer
            type_counts = self._count_memory_types(content, layer)
            return [
                {"path": f"{layer}/{mtype}", "count": count}
                for mtype, count in sorted(type_counts.items())
                if count > 0
            ]

        # path == "shared/user" etc. → return items matching layer + type
        mtype = parts[1]
        return self._list_memory_items(content, layer, mtype)

    def _count_memory_layer(self, content: str, layer: str) -> int:
        """Count total entries in the given memory layer from index content."""
        import re
        total = 0
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for _ in slug_re.finditer(line):
                    total += 1
        return total

    def _count_memory_types(self, content: str, layer: str) -> dict[str, int]:
        """Return {type: count} for a given layer."""
        import re
        counts: dict[str, int] = {}
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        slug_re = re.compile(r"\(([^)]+)\.md\)")
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if in_layer:
                for slug_m in slug_re.finditer(line):
                    slug = slug_m.group(1)
                    tm = type_re.match(slug)
                    if tm:
                        mtype = tm.group(1)
                        counts[mtype] = counts.get(mtype, 0) + 1
        return counts

    def _list_memory_items(
        self, content: str, layer: str, mtype: str
    ) -> list[dict]:
        """Return [{slug, name, description}, ...] for layer+type."""
        import re
        items: list[dict] = []
        in_layer = False
        section_re = re.compile(
            r"^#\s+Memory Index\s*\((?P<layer>shared|agent:[^)]*)\)"
        )
        # Match "- [Name](slug.md) — description" or table rows
        entry_re = re.compile(
            r"\[([^\]]+)\]\(([^)]+)\.md\)(?:\s*[—–-]+\s*(.+))?"
        )
        type_re = re.compile(r"^(user|feedback|project|reference)_")

        for line in content.splitlines():
            m = section_re.match(line.strip())
            if m:
                layer_raw = m.group("layer")
                in_layer = (layer_raw == layer) or (
                    layer == "agent" and layer_raw.startswith("agent:")
                )
                continue
            if not in_layer:
                continue
            for em in entry_re.finditer(line):
                name = em.group(1)
                slug = em.group(2)
                desc = (em.group(3) or "").strip()
                tm = type_re.match(slug)
                if tm and tm.group(1) == mtype:
                    items.append({"slug": slug, "name": name, "description": desc})
        return items

    async def _read_memory_body(
        self,
        layer: str,
        slug: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict:
        """Read the full body of a memory entry.

        Memory files are stored as Markdown with a YAML frontmatter (= the
        ``name`` / ``description`` / ``type`` metadata fields written by
        ``_remember``). Returning the full file content with the frontmatter
        intact triggered a G12 empty-stop attractor: when the LLM asked
        ``read_memory_body`` and got back e.g.::

            ---
            name: User Name
            description: User Name
            type: user
            ---

            Yasuda

        it sometimes parsed the frontmatter as the content and exited with
        ``finish=stop`` / ``content=""`` instead of narrating "Yasuda".
        Confirmed via dogfood trace on Q10 ``who am I?`` — the recall
        returned the body with frontmatter and produced an empty reply.

        Stripping the frontmatter before returning gives the LLM clean
        text to narrate. The metadata fields are not LLM-actionable here
        (they were emitted at write time and are surfaced separately via
        ``list_memory``), so dropping them costs nothing.
        """
        path = self.host.memory_path(layer, slug)
        try:
            content = await self.host.file_read(path)
            body = _strip_frontmatter(content)
            if offset is not None or limit is not None:
                lines = body.splitlines(keepends=True)
                start = max(0, offset or 0)
                sliced = (
                    lines[start:start + limit] if limit is not None
                    else lines[start:]
                )
                body = "".join(sliced)
            return {
                "content": body,
                "layer": layer,
                "slug": slug,
            }
        except Exception as exc:
            return {"error": str(exc), "layer": layer, "slug": slug}

    async def _remember(
        self,
        *,
        layer: str,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> dict:
        """Write a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it in slug despite
        # the tool description saying "Filename stem".
        if slug.endswith(".md"):
            slug = slug[:-3]
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {type}\n---\n\n{body}\n"
        )
        # memory_path appends .md itself — pass bare slug.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_write(file_path, frontmatter)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"saved": slug, "layer": layer}

    async def _forget(self, layer: str, slug: str) -> dict:
        """Delete a memory entry and regenerate the index."""
        # Defensive: strip trailing .md if LLM emitted it.
        if slug.endswith(".md"):
            slug = slug[:-3]
        # memory_path appends .md itself.
        file_path = self.host.memory_path(layer, slug)
        await self.host.file_delete(file_path)

        mem_dir = self.host.memory_dir(layer)
        index_path = mem_dir + "/MEMORY.md"
        await self.host.file_regenerate_index(
            mem_dir,
            index_path,
            "- [{name}]({slug}.md) — {description}",
            "# Memory Index\n\n",
        )
        return {"deleted": slug, "layer": layer}
