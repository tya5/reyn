"""RouterLoop — drives the chat router via native LLM tool_use (PR35).

Loop: build tools + prompt → call_llm_tools → if tool_calls, execute in
parallel, append results to messages, repeat → if text reply, emit to host
outbox and stop. Bounded by max_iterations.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.llm.llm import call_llm_tools
from reyn.llm.pricing import TokenUsage
from reyn.prompt.loop_control import (
    EMPTY_STOP_RETRY_DIRECTIVE,  # noqa: F401 -- deliberate re-export, see #4097:
    # every external consumer imports this name FROM router_loop (not from
    # loop_control directly) -- router_loop_driver.py and
    # test_chat_router_empty_stop_directive_wired.py both do
    # `from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE`.
)
from reyn.prompt.loop_control import tool_call_cap_notice as _tool_call_cap_notice_text
from reyn.runtime.router_system_prompt import (
    build_system_prompt,
)
from reyn.runtime.router_tools import (
    build_tools,
    get_dispatch_kind,
)
from reyn.services.compaction.engine import (
    _IMAGE_FIXED_TOKEN_COST,
    is_context_overflow_error,
)
from reyn.services.turn_budget import wrap_up_system_prompt

if TYPE_CHECKING:
    from reyn.llm.llm import LLMToolCallResult
    from reyn.tools.scheme import ExecutionResult

logger = logging.getLogger(__name__)


def _resolve_tool_use_scheme(name: "str | None" = None):
    """Return the active ``ToolUseScheme`` (#1593), resolving by name and defaulting
    to universal-category. Per-layer config (``tool_use``) passes
    the selected name here.

    #1608 ④: the built-ins **self-register at import time** — this function names NO
    scheme class (P7 cleanliness). Importing the ``schemes`` package runs every
    built-in module's import-time ``register_scheme`` (the package ``__init__``
    imports them all), so the full set is present without the OS resolve knowing any
    concrete scheme."""
    # Importing the package self-registers all built-in schemes (the bundle
    # self-describes; no scheme-literal in OS code). Idempotent — modules import once.
    import reyn.tools.schemes  # noqa: F401  (register_scheme import-time side effect)
    from reyn.tools.scheme import DEFAULT_SCHEME_NAME, get_scheme

    # Unknown / unconfigured name → default (universal-category) — byte-identical.
    return get_scheme(name or DEFAULT_SCHEME_NAME) or get_scheme(DEFAULT_SCHEME_NAME)


# ---------------------------------------------------------------------------
# Empty-response detection (Option F — ADR-0021)
# ---------------------------------------------------------------------------

# Localized user-facing message when the model returns an empty response
# (finish_reason=stop, no content, no tool calls). Deterministic i18n so
# output_language is always honoured.  P7-clean: no tool names.
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


# Localized OS-level acknowledgment emitted when a request is dispatched
# asynchronously (= a tool call returns ``{status: "spawned", ...}``). The
# H3 ablation exits the router loop before any further LLM call, so without
# this OS-injected message the user sees silence between request and the
# eventual ``[task_completed]`` arrival. The previous (pre-H3) LLM-composed
# ack was hallucinating output that hadn't happened yet (B32
# W3 S1); this deterministic OS message carries the same UX guarantee
# (= "/agents" hint) without LLM composition, so the race condition does
# not re-emerge.  P7-clean: no tool names, no qualified action names.
# #1593 PR-4: the OS-generic synthetic tool-response the RePresent arm appends for
# an intercepted re-present tool_call (a retrieval search). The scheme's interpret
# turned the tool_call into a RePresent (it is never dispatched), but the message
# history still needs every tool_call answered — this is that answer. P7-clean:
# OS-level vocabulary, no "search" / scheme concept; the new tools= payload + the
# scheme's tool-use SP carry the actual re-presentation meaning to the LLM.
_REPRESENT_ACK = (
    "The available tools have been updated based on your request. "
    "The tools you can now call are listed above."
)
# Defensive backstop for the RePresent loop (#1593 PR-4). The REAL bound is
# convergence-by-construction (the scheme's monotonic ``presented`` on a finite
# catalog + its terminal-search-drop forcing Execute); this valve only fires for a
# misbehaving scheme that never terminates its re-present loop, well above any
# realistic search-refine sequence. The outer max_iterations is the ultimate cap.
_MAX_REPRESENT_ROUNDS = 64

# B55 R-7 (2026-05-25): agent-side spawn alignment — the LLM sees a
# structured task lifecycle event for delegate_to_agent / other
# peer-async tools too. Prior behaviour pushed a generic `dispatched N
# async requests; awaiting peer reply` status row with no
# `[task_spawned]` header, leaving the SP TASK_SPAWNED rule
# un-anchored for the agent path. Now emits the uniform
# `[task_spawned] kind=prompt ...` header + user-facing trailer. Pairs
# with the `[task_completed] kind=prompt ...` injection on peer reply
# receipt (see inter_agent_messaging) — proposal 0067 P4 (#3978),
# architect ruling 2026-08-10: `kind=agent` collapsed to `kind=prompt`
# (D2's kind axis names WHAT ran, not WHO triggered it; "who" still
# rides chain_id/peer, unaffected by this rename).
_AGENT_SPAWN_ACK_MSG: dict[str, str] = {
    "ja": (
        "ピアエージェントにリクエストを送信しました。"
        " 返答を待っています — `/agents` で進行状況を確認できます。"
    ),
    "en": (
        "Request dispatched to the peer agent."
        " Awaiting reply — use `/agents` to monitor progress."
    ),
}


# #187: the SINGLE empty-stop retry continuation directive, shared UNIFORMLY by
# every RouterLoop construction site (chat / plan-step / agent op-loop). owner
# decision (2026-06-07): do NOT build per-site/per-tier directive differentiation
# without evidence — a content-neutral "resume" re-enters the loop and lets the
# model continue (tool-call OR reply) on its own. Real-task: a content-less empty
# stop is 67% premature; "resume" recovers the next action (invoke 11/12). The
# previous per-site directives (chat "write your reply" / plan "step report") were
# unevidenced differentiation — and the chat one's "Do not call another tool"
# was itself anti-invoke. Iterate per-site ONLY if a measured problem appears.
# reyn.prompt.loop_control (SP prompt-package, Phase 3 §I) — imported above,
# re-bound to the original public name so every consumer
# (``from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE``) is
# unchanged.


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
        f"load it with read_file(path={ref['path']!r}) when the context has room.]"
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
                    f"stored at {saved['path']}; load it with "
                    f"read_file(path={saved['path']!r}) to access them.]"
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
    """Is THIS ONE response empty — no text, no tool calls?

    Provider-level glitch (observed with weak models such as
    gemini-2.5-flash-lite at ~50% rate — ADR-0021 / B7-G12).

    Trigger: finish_reason=="stop", content empty, tool_calls empty.

    **This predicate's subject is deliberately narrow — one response, no
    turn context** (its only argument is ``response``, structurally unable
    to ask "did the LLM already produce something earlier THIS turn?").
    The call site (`run()`, near the "Option F" block) is what answers the
    real question ADR-0021 needs — "did this TURN produce nothing?" — by
    additionally gating on `_tool_calls_attempted` (#4486: a tool call
    already dispatched this turn means the model may correctly have
    nothing further to add — e.g. `present`'s entire contract is that the
    tool call itself is the answer — and that is not the glitch this
    function detects). Do not fold that gating in here: a caller that only
    wants "is this single response empty" (there are none today, but the
    predicate's name promises exactly that) would silently gain turn-scoped
    behavior it never asked for.

    **Retry note** (#4486 drift fix — this docstring previously claimed
    Reyn does not retry at all, which stopped matching production the
    moment B42-NF-W6-1 landed): the OS-level retry decision is NOT made
    here either — see the "Option F" block at the `_is_empty_router_response`
    call site, and `empty_stop_retry_auto` (#4677: config-driven via
    `chat.empty_stop_retry`, owner default `False` since 2026-08-14 —
    was hardcoded `True`, `router_loop_driver.py`) for the actual current
    behavior.
    """
    if response is None:
        return True
    finish = getattr(response, "finish_reason", None)
    content = getattr(response, "content", None) or ""
    tool_calls = getattr(response, "tool_calls", None) or []
    return finish == "stop" and not content.strip() and not tool_calls


# #3783 stage 1: the TODO this comment used to carry ("a future cleanup
# should lift one shared is_context_overflow_error, e.g. next to
# ContextOverflowError in services/compaction") is done — this local
# duplicate + the 3 in router_loop_driver.py + the divergent 4-keyword copy
# in compaction/engine.py all replaced by
# ``reyn.services.compaction.engine.is_context_overflow_error``.


def _is_unsupported_param_error(exc: BaseException) -> bool:
    """#1616: True when *exc* is the embedding provider rejecting a param.

    Detects the gemini-via-LiteLLM-proxy case where the proxy adds
    ``encoding_format`` and the provider rejects it (``UnsupportedParamsError``),
    which fails the action embedding index build. Keyword/typename match — the
    same stringified-exception heuristic family as
    ``reyn.services.compaction.engine.is_context_overflow_error``.
    """
    return (
        "UnsupportedParams" in type(exc).__name__
        or "does not support parameter" in str(exc)
        or "encoding_format" in str(exc)
    )


def _action_index_build_failure_warning(exc: BaseException, model_class: Any) -> str:
    """#1616: cause-aware operator guidance for a failed action-index build.

    Two distinct failure modes need two distinct operator actions, so branch
    on the cause instead of emitting one misleading message:

    * **Unsupported-param** (``UnsupportedParamsError`` — the embedding provider
      rejected a param, typically ``encoding_format`` on a gemini-routed embedding
      behind a LiteLLM proxy): point to the recommended *proxy-side* fix
      (``litellm_settings: drop_params: true``), since reyn cannot suppress a
      param the proxy injects.
    * **Otherwise** (e.g. network error, bad credentials, unreachable API):
      generic embedding-provider-failure guidance pointing at reyn.yaml
      config, opt-out, or an alternate class.

    Returned as a fully-formatted string so the cause-selection is unit-testable
    without driving the whole index build.
    """
    if _is_unsupported_param_error(exc):
        return (
            "Semantic search_actions disabled for this session: the embedding "
            f"provider REJECTED a parameter ({type(exc).__name__}: {exc}). This is "
            "typically `encoding_format` on a gemini-routed embedding behind a "
            "LiteLLM proxy (the proxy adds it; the provider rejects it). Fix: set "
            "`litellm_settings:\n  drop_params: true` on your LiteLLM PROXY (the "
            "client-side flag does NOT apply on the proxy route — known litellm "
            "behaviour), OR use an OpenAI-compatible embedding class. Options to "
            "opt out: set `embedding.enabled: false`."
        )
    return (
        "Semantic search_actions disabled for this session: action embedding "
        f"index build failed for model class {model_class!r} "
        f"({type(exc).__name__}: {exc}). Options: (1) check the embedding "
        "provider config (`embedding.classes` / API credentials / LiteLLM "
        "proxy reachability) in reyn.yaml, (2) set "
        "`embedding.enabled: false` to opt out, (3) use a "
        "different API-backed class via `embedding.default_class` (e.g. `standard`)."
    )


# ---------------------------------------------------------------------------
# Host protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RouterLoopCore(Protocol):
    """#1092 PR-A (ADR-0036 FD1, decision c): the NARROW core surface the
    RouterLoop act-loop actually depends on — the members RouterLoop's loop
    directly calls for any host. The chat ``RouterHostAdapter`` is a superset and
    satisfies this for free. (The narrow/superset split dates to the phase-graph
    era; the phase host — ``PhaseRouterLoopHost`` — was deleted in #2438, so chat
    is the only production implementor today.)

    The chat-extras (agents/mcp/memory/web/file/reyn_repo/embedding/
    discovery/spawn) live on ``RouterLoopHost`` below; they are
    reached only via the chat-discovery setup, the chat system-prompt build, or
    chat-dispatch handlers.
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
    # #3633: ``persist`` makes the kind=="agent" → history-append coupling an
    # EXPLICIT per-call-site choice instead of an implicit blanket rule the
    # host applies unconditionally. Defaults True (= the pre-#3633 behavior,
    # unchanged for every existing caller); a call site whose text is already
    # persisted by another path (e.g. router_loop's tool-turn display bubble,
    # duplicated by ``feedback()``'s ``append_history_entry``) passes False.
    async def put_outbox(
        self, *, kind: str, text: str, meta: dict, persist: bool = True,
    ) -> None: ...


@runtime_checkable
class RouterLoopHost(RouterLoopCore, Protocol):
    """Abstract surface RouterLoop needs (chat-mode superset of RouterLoopCore).

    Implemented by RouterHostAdapter in
    src/reyn/runtime/services/router_host_adapter.py. Extends RouterLoopCore
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

    @property
    def state_log(self) -> Any:
        """The process-shared WAL (StateLog) or None — #2259 PR-1: threaded into the
        ToolContext so a recovery-core config tool records a config generation."""
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
        the user's project — without this, only the phase-execution path
        sees REYN.md and casual chat queries get answered without
        project-specific context."""
        ...

    def get_universal_wrappers_enabled(self) -> bool:
        """Return whether the FP-0034 universal catalog wrappers should
        appear in tools=. Mirrors ``tool_use.universal_wrappers_enabled``
        from reyn.yaml (#4552 PR-3: moved from
        ``action_retrieval.universal_wrappers_enabled``). Default False
        preserves the prior tools= shape."""
        ...

    def get_action_embedding_index(self) -> Any:
        """Return the session-scoped ActionEmbeddingIndex, or None.

        FP-0034 Phase 2 step 1.  Bound by Session when the operator
        has set ``embedding.enabled: true`` (FP-0066 §7).
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
        ``embedding.default_class`` from reyn.yaml (bound only when
        ``embedding.enabled: true`` — FP-0066 §7).
        """
        ...

    def get_sandbox_backend(self) -> "str | None":
        """Return the configured sandbox backend name, or None.

        FP-0034 Phase 2.  Mirror of ``sandbox.backend`` from reyn.yaml
        (resolved from ``session._sandbox_config.backend``).  RouterLoop
        forwards this into ``RouterCallerState.sandbox_backend`` so the
        ``exec`` category D14 visibility gate in
        ``universal_catalog._enumerate_category`` can decide whether to
        expose ``exec``.  ``None`` and ``"noop"`` both
        hide the category; any other value (``"seatbelt"`` /
        ``"landlock"`` / ``"auto"``) makes it visible.
        """
        ...

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """RouterLoopHost: invoke the OS-native web/search op (DuckDuckGo)."""
        ...

    async def web_fetch(self, *, url: str) -> dict:
        """RouterLoopHost: invoke the OS-native web/fetch op."""
        ...

    async def reyn_repo_list(self, *, path: str) -> dict:
        """RouterLoopHost: list entries under ``<reyn_root>/path``.

        ``reyn_root`` resolves to the directory containing
        ``pyproject.toml`` for the running Reyn install (= dev install /
        source clone). For wheel installs without a discoverable
        repo root, returns an error result so the LLM can fall back."""
        ...

    async def reyn_repo_read(self, *, path: str) -> dict:
        """RouterLoopHost: read the file at ``<reyn_root>/path`` as text."""
        ...

    # Proposal 0067 P1' (#3978): mark the session's current_task as
    # outstanding — called from the async-dispatch block below, right before
    # the turn exits to let the delegate work. Sync (a plain attribute set,
    # not an awaited call) — see RouterHostAdapter's implementation.
    def mark_task_pending(self) -> None: ...

    # #2103 S1bc: spawn a fresh-context session under THIS agent for a task.
    # Multi-session hosts (the chat RouterHostAdapter) implement it; others leave
    # it unbound (= hasattr-guarded at caller-state build).
    async def spawn_session(self, *, request: str, mode: str,
                            narrowing: "dict | None", chain_id: str,
                            base_dir: "str | None" = None,
                            agent: "str | None" = None,
                            session: "str | None" = None) -> dict: ...

    # Proposal 0067 P5 (#3978): fire-and-forget delivery to a peer (agent,
    # session) via TurnOrigin.PEER_SESSION. Multi-session hosts implement it;
    # others leave it unbound (= hasattr-guarded at caller-state build, same
    # pattern as spawn_session above).
    async def send_to_session(self, *, agent: str, session: str,
                              text: str, wake: bool) -> dict: ...

    # #3633: see RouterLoopCore.put_outbox above — ``persist`` is the same
    # explicit per-call-site opt-out, inherited here (Protocol overlap).
    async def put_outbox(
        self, *, kind: str, text: str, meta: dict, persist: bool = True,
    ) -> None: ...

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

    # The memory-store capability (``remember`` / ``forget`` / ``read_body``).
    # #3607: the host used to expose four file primitives + two memory-path
    # helpers instead, and this loop assembled the memory operations out of
    # them. It exposes the operations themselves now; the file callbacks
    # underneath belong to the memory layer, not to the router's host surface.
    @property
    def memory(self) -> Any: ...

    # MCP ops
    async def mcp_list_servers(self) -> list[dict]: ...

    # #4686: per-connection resource-subscription state (never aggregated).
    async def mcp_list_subscriptions(self) -> list[dict]: ...

    async def mcp_list_tools(self, server: str) -> list[dict]: ...

    async def mcp_call_tool(self, server: str, tool: str,
                             args: dict) -> dict: ...

    # #2597 slice ②a: resources consumption (list / read / templates).
    async def mcp_list_resources(self, server: str) -> list[dict]: ...

    async def mcp_list_resource_templates(self, server: str) -> list[dict]: ...

    async def mcp_read_resource(self, server: str, uri: str) -> dict: ...

    # #2597 slice ②b: resource subscriptions (subscribe/unsubscribe — the async
    # push event-source; the resulting notifications land on the EventLog as
    # mcp_resource_updated, not through this Protocol).
    async def mcp_subscribe_resource(self, server: str, uri: str) -> dict: ...

    async def mcp_unsubscribe_resource(self, server: str, uri: str) -> dict: ...

    # #2597 slice ②c: prompts consumption (list / get). Prompts have no
    # subscribe concept.
    async def mcp_list_prompts(self, server: str) -> list[dict]: ...

    async def mcp_get_prompt(self, server: str, name: str,
                              arguments: dict | None = None) -> dict: ...

    # OpContext factory for unified-registry handlers (ADR-0026 Phase 3.5).
    # Builds a permission-aware OpContext with the operator-declared
    # PermissionDecl + Workspace(actor="chat_router") + mcp_servers,
    # so handlers in src/reyn/tools/{file,mcp,web*}.py can delegate to
    # op_runtime with the same gating the legacy router branches had.
    def make_router_op_context(self) -> Any: ...

    # Safety-limit intervention bus factory (FP-0005 extension).
    # Returns the current RequestBus for handle_limit_exceeded interactive
    # mode, or None when no bus is wired (headless / test stubs).
    # getattr-guarded in run_loop — hosts that don't implement it degrade
    # to unattended (no ask, decision-enabling message instead).
    def make_intervention_bus(self) -> "Any | None": ...

    # Resolve router model (config "router" → real model id)
    def resolve_model(self, name: str) -> str: ...

    # The bound ModelResolver (#1172) — components that build their own LLM
    # callers (e.g. the planner's lazy CompactionEngine) resolve through it.
    @property
    def resolver(self) -> Any: ...


# ---------------------------------------------------------------------------
# Catalog action-name membership (#4552: this used to also host the hot-list
# alias builder — _filter_ghost_names_by_registry / _build_hot_list_aliases /
# _operation_alias_metadata / _UNIVERSAL_WRAPPER_NAMES — removed with the
# hot-list feature, owner directive: discarded, superseded by list_actions
# as the canonical discovery path. _known_action_names()/_KNOWN_ACTION_NAMES
# survive: _emit_routing_decided's ars_direct classification still needs
# catalog-action membership independent of hot-list.)
# ---------------------------------------------------------------------------

def _known_action_names() -> "frozenset[str]":
    """The catalog action set, imported lazily to keep module import order free
    of a ``reyn.tools`` cycle at load time."""
    from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES

    return KNOWN_ACTION_NAMES


# The catalog's action set, bound once at import. Read by the ``routing_decided``
# audit arm to tell a direct call on a catalog action from a direct call on a
# base tool that the catalog does not carry.
_KNOWN_ACTION_NAMES: frozenset[str] = _known_action_names()


def gate_effective_tool_name(name: str, args: "dict | None") -> "str | None":
    """The TOOL-axis gate's name resolution — the ONE place the wrapper unwrap lives.

    Both halves of the #3378 advertise ⇔ enforce agreement key on this:
    :func:`apply_contextual_visibility` (advertisement, ``args=None``) and
    :meth:`RouterLoop._excluded_result` (enforcement, the live ``args``). A
    ``invoke_action`` call carries its real target in ``action_name``, so the
    effective name is knowable only at CALL time — ``args=None`` therefore
    returns ``None`` ("undeterminable"), which the advertisement half reads as
    "cannot pre-filter this row". That asymmetry is deliberate and load-bearing:
    pre-filtering the wrapper itself under an allow-list contextual would hide
    the ONLY route to every allowed action, i.e. advertise MORE narrowly than
    enforcement denies — the mirror image of the #3378 defect.

    ``None`` is also returned for an ``invoke_action`` whose ``action_name`` is
    absent (a malformed call): nothing to gate on, and dispatch rejects it.
    """
    if name == "invoke_action":
        return (args or {}).get("action_name")
    return name


def apply_contextual_visibility(
    tools: list[dict], contextual: "object | None"
) -> list[dict]:
    """Drop from the LLM-visible catalog every tool the CONTEXTUAL would deny at call time.

    #3378: the advertisement half of the advertise ⇔ enforce agreement. It reads
    the SAME source as the live gate — ``RouterLoop._contextual_permission``, the
    composed effective narrowing (topology ∩ delegate floor ∩ per-session config ∩
    ⊆-parent cap ∩ ``/visibility`` override ∩ the ephemeral ``_untrusted`` profile ∩
    the ``exclude_tools`` bridge) — and the same
    :func:`gate_effective_tool_name` unwrap, so a tool that would be rejected with
    ``tool_excluded`` is never offered in the first place. The prior filter keyed on
    ``exclude_tools`` ALONE, so any contextual that did not come from
    ``exclude_tools`` (topology / delegate / ephemeral) left the tool advertised and
    denied it only at call time — the owner-reported ``exec`` symptom.

    **Not a substitute for enforcement (#187).** Hiding a row does not stop the LLM
    from naming it anyway (native direct call, the #229 salvage, or a direct
    ``invoke_action(action_name=…)``), which is exactly how the #187 ``web_search``
    leak executed. ``_excluded_result`` stays the boundary; this is the presentation
    half that keeps the model from wasting a turn on a tool it cannot have.

    ``contextual is None`` (no narrowing anywhere) → ``tools`` returned unchanged.
    P7-clean: no hardcoded tool names; the narrowing is data resolved per session.
    """
    if contextual is None:
        return tools
    from reyn.security.permissions.effective import tool_contextually_denied

    kept: list[dict] = []
    for t in tools:
        name = t.get("function", {}).get("name")
        effective = (
            gate_effective_tool_name(name, None) if isinstance(name, str) else None
        )
        if effective is not None and tool_contextually_denied(contextual, effective):
            continue
        kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# RouterLoop
# ---------------------------------------------------------------------------

def _derive_registry_dispatch_tools() -> "frozenset[str]":
    """#2123: the dispatch set DERIVED from the per-tool ``router_dispatched`` flag —
    the single SoT replacing the old hand-maintained frozenset. Computed once at class
    definition from the default registry (no cycle: ``reyn.tools`` does not import
    ``router_loop``)."""
    from reyn.tools import get_default_registry
    reg = get_default_registry()
    return frozenset(
        d.name for d in (reg.lookup(n) for n in reg.names())
        if d is not None and d.router_dispatched
    )


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
        router_model: "str | None" = None,  # #1672: None → config (model_class_for("router")); was hardcoded "light"
        budget: Any = None,  # BudgetTracker | None — process-shared cost tracker
        system_prompt_override: str | None = None,
        non_interactive: bool = False,  # #1440 followup: run-once (no TTY) → live router SP proceeds instead of asking a clarifying question (13398). Threaded from Session.
        exclude_tools: set[str] | None = None,
        excluded_categories: set[str] | None = None,  # #1667 catalog categories skipped at source
        contextual_permission: "object | None" = None,  # #1827 S1: per-session ContextualPermission (TOOL-axis enforcement); None → bridged from exclude_tools
        memo_provider: Any = None,  # SubLoopMemoProvider | None (ADR-0025)
        empty_stop_retry_directive: str | None = None,  # B42-NF-W6-1 opt-in retry
        empty_stop_retry_auto: bool = False,  # #187/#4677: config-driven (chat.empty_stop_retry, owner default False since 2026-08-14) — the one prod site (router_loop_driver.py) reads it from config, not a hardcoded True
        max_tool_calls_per_turn: int = 50,  # #1666 — safety.loop.max_tool_calls_per_turn (0 = unlimited)
        on_limit: "Any | None" = None,  # OnLimitConfig | None — FP-0005 max_iterations checkpoint
        llm_caller: "Any | None" = None,  # Tier 2 test seam: real-fake injection
        scheme_name: "str | None" = None,  # #1593 PR-2: chat-layer tool-use scheme (None → universal default; the construction site resolves config.tool_use.scheme x .transport via P4a's registry, FP-0066 P4b #3247)
        response_format: "dict | None" = None,  # 0062: schema-constrained answer turn; None = byte-identical (no other caller sets this)
        schema_validate_fn: "Any | None" = None,  # 0062: Callable[[Any], list[str]] — parsed-value -> validation-error strings ([] = conforming)
        max_schema_reprompt_attempts: int = 2,  # 0062 §2.1 failure-mode-(c): bounded re-prompt budget (extra attempts beyond the first)
        intra_turn_contextual_for_turn_fn: "Any | None" = None,  # #1909 OPT-IN: () -> ContextualPermission|None, re-invoked every run() iteration. None (default) = off — self._contextual_permission stays turn-frozen. RouterLoopDriver only threads this when safety.threat_scan.capability_narrowing is "iteration" (#3501 — the top rung of the 3-value ladder off/turn/iteration; at "turn" the narrowing is resolved at the turn boundary only, so this stays None there too).
        contextual_static_baseline: "object | None" = None,  # #1909: the UN-narrowed static contextual (identity anchor) — a per-iteration resolve equal (by identity) to this means "not tainted"; anything else means the untrusted-composed profile engaged. Only consulted when intra_turn_contextual_for_turn_fn is not None.
    ):
        self.host = host
        self.chain_id = chain_id
        # Bumped per LLM round in ``run_loop``; initialised here so a delta
        # emitted from any other entry point carries 0 rather than raising —
        # narration must never break the turn it describes.
        self._delta_round_index = 0
        self.max_iterations = max_iterations
        # #1672: an UNSET router_model follows the configured model (no hidden
        # "light" tier) — resolve the "router" purpose class via the host's
        # config-aware ModelResolver. Explicit router_model still wins, so the
        # chat router follows config from this single resolution point.
        from reyn.llm.model_resolver import resolve_purpose_class
        _resolver = getattr(host, "resolver", None)
        self.router_model = resolve_purpose_class(
            router_model, _resolver, "router",
        )
        # #4206 T1 (②bounding): the project-declared ceiling, if any — read
        # once here (not re-fetched per call) since the resolver is
        # process-wide and its ceiling doesn't change mid-session. No
        # ``hasattr`` fallback here (unlike ``resolve_purpose_class`` above):
        # that fallback's failure direction is a harmless value default
        # ("standard"); this one's is a WIDENING one — a resolver that can't
        # answer ``class_ceiling()`` would silently make the ceiling
        # disappear. A host whose ``resolver`` is not a real ``ModelResolver``
        # should raise (fail loud), not go unbounded (lead-coder review, #4318).
        self._model_class_ceiling = (
            _resolver.class_ceiling() if _resolver is not None else None
        )
        self.budget = budget
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
        # Session._non_interactive via the constructor.
        self._non_interactive = bool(non_interactive)
        # Tool names to drop from the catalog (= post-build filter). E.g. the
        # #187 faithful SWE-eval drops the web tools so the general agent solves
        # from the repo + issue, not a web lookup of the gold solution.
        self._exclude_tools: frozenset[str] = frozenset(exclude_tools or set())
        # #1667: catalog categories skipped at the source (_enumerate_category),
        # threaded onto RouterCallerState so the universal catalog drops them.
        self._excluded_categories: frozenset[str] = frozenset(excluded_categories or set())
        # #1827 S1 (live-gate): the TOOL-axis ENFORCEMENT flows through the unified
        # ∩-model (effective.py ContextualLayer) at the single live gate
        # ``_excluded_result``. #3378: ``_contextual_permission`` is now the SINGLE
        # EFFECTIVE SOURCE for BOTH halves — enforcement (``_excluded_result``) and
        # advertisement (``apply_contextual_visibility``) — so ``exclude_tools`` is
        # COMPOSED IN as one more restrict-only ∩ conjunct rather than living on as a
        # parallel, advertisement-only axis.
        #
        # Composition (not either/or) closes the defect in BOTH directions:
        #   - a contextual from topology / delegate / ephemeral used to leave
        #     ``exclude_tools``-driven advertisement untouched → the tool stayed
        #     advertised and was denied only at call time (the owner's ``exec``);
        #   - the old ``if contextual is not None`` branch DISCARDED ``exclude_tools``
        #     from enforcement whenever any contextual was present (every Session goes
        #     through CapabilityVisibility, which yields a non-None contextual once
        #     ``reapply_visibility_override`` has run) → a ``--exclude-tools web_search``
        #     was hidden but NOT execution-blocked, i.e. the #187 leak in reverse.
        self._contextual_permission: "object | None" = self._with_exclude_tools(
            contextual_permission
        )
        # FP-0034 Phase 2 step 1 / FP-0066 P2b (#3247): the action embedding
        # index background-build once-per-chain dedup used to live on a
        # RouterLoop instance attr (``_action_index_build_task``). That
        # dedup is now owned by ``IndexCoordinator._bg_tasks`` (per-
        # workspace singleton, see ``_get_index_coordinator`` below) —
        # absorbed per the #3247 firm §1/§7, so there is no
        # RouterLoop-instance-scoped task handle to declare here anymore.
        # ADR-0025: optional sub-loop LLM call memoization. When set,
        # ``call_llm_tools`` invocations consult the provider before
        # invoking — args_hash hit returns the recorded LLMToolCallResult
        # without paying LLM cost. Used by phase-step resume so a crashed
        # mid-step sub-loop replays its earlier LLM turns from snapshot
        # rather than re-paying. ``None`` = normal execution (no memo).
        self._memo_provider = memo_provider
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
        # #1666: per-turn tool_call count cap (cost-bound). 0 = unlimited.
        self._max_tool_calls_per_turn = max(0, int(max_tool_calls_per_turn))
        # Tier 2 test seam: when set, ``run()`` calls this callable instead of
        # the module-level ``call_llm_tools``. Allows real-fake injection
        # (= scripted async callable) without ``unittest.mock.patch`` — per
        # testing.ja.md hard rule that forbids ``MagicMock / AsyncMock /
        # patch``. Production callers leave this as ``None``.
        self._llm_caller = llm_caller
        self._on_limit = on_limit  # FP-0005 max_iterations checkpoint config
        # 0062: when set, the loop's terminal PlainText resolution routes through
        # a SEPARATE no-tools response_format-constrained call (ADR-0035 D2
        # separate-decide, reapplied to this loop — see ``_run_structured_answer_turn``)
        # instead of emitting the tool-turn's own free-form content. None (every
        # caller except a schema-bearing ``run_agent_step``) is byte-identical to
        # today's behaviour.
        self._response_format = response_format
        self._schema_validate_fn = schema_validate_fn
        self._max_schema_reprompt_attempts = max(0, int(max_schema_reprompt_attempts))
        self._catalog: dict[str, dict] = {}  # populated per run() — the ADVERTISED mirror
        # #1618 root-1: the DISPATCHABLE membership map (None ⇒ = self._catalog,
        # byte-identical for schemes whose advertised = dispatchable). Set per run()
        # from ``Presentation.dispatchable_catalog`` when a scheme decouples them.
        self._dispatch_catalog: "dict[str, dict] | None" = None
        self._tool_names: frozenset[str] = frozenset()  # kept for backward compat
        self._total_usage: TokenUsage = TokenUsage()
        # Status-bar ctx chip's "current size" figure needs a SINGLE LLM call's
        # prompt_tokens, not the turn-summed _total_usage above — summing every
        # call in a multi-tool-iteration turn double(triple/...)-counts nearly
        # the same growing context each iteration re-sends, wildly overstating
        # "how much of the window is currently occupied".
        self._last_call_usage: TokenUsage = TokenUsage()
        # #1593: the active tool-use scheme. PR-1 = universal-category (the shipped
        # behaviour, behind the protocol) for every layer → byte-identical. Per-layer
        # config selection (tool_use) plugs in here; with all
        # layers defaulting to universal-category it is byte-identical today.
        self._scheme = _resolve_tool_use_scheme(scheme_name)
        # #1909 OPT-IN (default off): intra-turn untrusted-content re-narrowing.
        # None (the default, every caller except the opt-in path) means run()'s
        # per-iteration re-resolve block below is skipped entirely — no new
        # code path executes, so the default posture is structurally
        # byte-identical to pre-#1909 (turn-frozen ``_contextual_permission``).
        self._intra_turn_contextual_for_turn_fn = intra_turn_contextual_for_turn_fn
        self._contextual_static_baseline = contextual_static_baseline
        # Turn-scoped monotonic latch (only meaningful when the above is set):
        # once tainted this turn, stays tainted through turn end regardless of
        # a later compaction evicting the ``external_source`` marker.
        self._untrusted_latched: bool = False
        self._untrusted_latched_permission: "object | None" = None

    def _with_exclude_tools(self, contextual: "object | None") -> "object | None":
        """Compose ``exclude_tools`` into ``contextual`` as one more restrict-only ∩ term.

        #3378: the single place ``exclude_tools`` enters the effective narrowing, so
        every reader of ``self._contextual_permission`` — the live enforcement gate AND
        the advertisement filter — sees the same set. Called at construction AND on the
        #1909 intra-turn re-resolve (which replaces the whole contextual with a
        freshly-composed untrusted-narrowed one; without re-composing here, an
        ``exclude_tools`` session would silently LOSE its exclusion mid-turn — the
        composition is a meet, so re-applying it is idempotent and can only narrow).

        No ``exclude_tools`` → ``contextual`` returned unchanged (identity), which keeps
        the #1909 ``contextual_static_baseline`` identity comparison intact for the
        overwhelmingly common no-exclude case.
        """
        if not self._exclude_tools:
            return contextual
        from reyn.security.permissions.effective import (
            ContextualPermission,
            NarrowingOrigin,
        )
        bridged = ContextualPermission(
            tool_deny=self._exclude_tools,
            origin=NarrowingOrigin(
                label="this run's explicit tool exclusion list",
                cause=(
                    "the caller that started this run listed this tool as excluded "
                    "(the `exclude_tools` argument — a CLI flag, a phase's `gates`, or "
                    "a pipeline step's declaration)"
                ),
                lifts_when=(
                    "the run is started without that exclusion. It is fixed for the "
                    "lifetime of this run"
                ),
            ),
        )
        if contextual is None:
            return bridged
        from typing import cast

        from reyn.security.permissions.capability_profile import compose_resolved
        return compose_resolved([
            (cast("ContextualPermission", contextual), frozenset()),
            (bridged, frozenset()),
        ])[0]

    @property
    def total_usage(self) -> TokenUsage:
        """Accumulated token usage across all LLM calls made in this loop."""
        return self._total_usage

    @property
    def last_call_usage(self) -> TokenUsage:
        """TokenUsage of the single MOST RECENT LLM call made in this loop —
        distinct from ``total_usage`` (the turn-summed figure). Reset at the
        start of each ``run()`` like ``total_usage``."""
        return self._last_call_usage

    def _emit_agent_delta(self, text: str) -> None:
        """#3288 ③b: forward one streamed content-delta chunk as an audit-event
        — the owner-ratified L4 replacement (issue #3288 comment thread): a
        partial rides ``host.events`` (the SAME audit-event channel
        ``user_submitted`` / ``router_represent_round`` already use), NEVER
        ``host.put_outbox`` — ``OutboxMessage.__post_init__`` validates
        ``kind`` against the closed display vocabulary, so an
        ``agent_delta`` OutboxMessage would either raise (unregistered) or
        require registering it there, which is exactly the outbox-kind
        design the owner's decision replaces. Routing through ``host.events``
        instead means a surface with no ``agent_delta`` handler consumes-but-
        drops it (EVENT-frame semantics, ``frames.py``) rather than the
        default generic-row rendering an unknown DISPLAY kind gets — the
        "no visible-garbage window" invariant.

        Never touches history or the terminal ``kind="agent"`` OutboxMessage
        — those are unchanged, emitted exactly as before this turn's call
        returns (L9 whole-persist: the completed full text is what gets
        appended to history and put on the outbox, exactly once).

        ``round_index`` is which LLM round of this turn produced the chunk,
        and it is the reason this method is a bound method rather than a free
        function: it runs INSIDE the round (``on_content_delta=self.
        _emit_agent_delta``), so the round is a fact it already holds. A
        consumer that wants to separate "what the model said before calling a
        tool" from "what it said after reading the result" would otherwise have
        to re-derive that boundary from the ARRIVAL ORDER of unrelated frames —
        reconstructing a fact the producer had, which is the coupling this
        field exists to remove.

        A monotonic index rather than a boundary flag, deliberately: a dropped
        flag is undetectable (nothing changes until the next one), while a
        dropped index shows as a gap. It is not ``stop_reason`` either —
        stop_reason says why a round ended, and the boundary is its
        CONSEQUENCE; naming the consequence keeps a consumer from having to
        learn every cause that can produce one.

        Best-effort: a failing audit-event emit must never abort the
        in-flight LLM call it is merely narrating.
        """
        try:
            self.host.events.emit(
                "agent_delta",
                text=text,
                chain_id=self.chain_id,
                round_index=self._delta_round_index,
            )
        except Exception:  # noqa: BLE001 — narration must never break the turn
            logger.exception("router: agent_delta audit-event emit failed")

    async def run(self, user_text: str, history: list[dict]) -> TokenUsage:
        """Process one user utterance end-to-end. Emits to host.put_outbox.

        Returns the total TokenUsage accumulated across all LLM calls so the
        caller can credit it to the session-level usage counter (F4 Bug 2).
        """
        self._total_usage = TokenUsage()
        self._last_call_usage = TokenUsage()
        host = self.host
        # FP-0034 PR-3b-iii: read universal wrapper visibility from host.
        # getattr fallback so narrow hosts (= test FakeRouterHost) that don't
        # implement the method default to off (= the prior flat tools= shape).
        _univ_enabled_getter = getattr(
            host, "get_universal_wrappers_enabled", None,
        )
        _univ_enabled = bool(_univ_enabled_getter()) if _univ_enabled_getter else False
        # Same getattr-fallback pattern: hosts without get_cwd (= FakeRouterHost
        # in LLMReplay tests) skip the Environment section so the SP byte
        # content stays unchanged for cached fixtures.
        _cwd_getter = getattr(host, "get_cwd", None)
        _cwd_str = _cwd_getter() if _cwd_getter else None
        # #1479: system info (date/platform/shell/git). Same getattr-fallback:
        # FakeRouterHost has no get_environment_info → None → fields absent →
        # fixture SP keys unaffected.
        _env_info_getter = getattr(host, "get_environment_info", None)
        _environment_info = _env_info_getter() if callable(_env_info_getter) else None
        # FP-0034 Phase 2 step 1 / FP-0066 P2b (#3247): D14 visibility gate
        # for search_actions. Only show search_actions when (a) the operator
        # configured an embedding model class (= embedding.enabled, reflected
        # by ``_idx``/``_provider``/``_model_class`` being non-None below)
        # AND (b) the session has an ActionEmbeddingIndex that is_ready().
        # Any missing signal degrades to "hide" so the LLM does not see a
        # tool whose query would return empty results.
        #
        # #4564: this block used to be gated on ``_univ_enabled`` too — an
        # UNDECLARED second gate. ``embedding.py``'s own docstring already
        # declares "search_actions is gated separately via
        # embedding.enabled" — no mention of the wrapper flag. Every
        # scheme's ``present()`` call reads this same ``_search_visible``
        # (via ``layer_ctx["search_visible"]``), not just
        # ``universal-category``'s, so the extra gate silently hid
        # search_actions under enumerate-all/retrieval whenever an operator
        # set ``universal_wrappers_enabled: false`` — a flag whose NAME
        # reads as scoped to a scheme they may not even be using. Removed
        # (architect ruling, #4552): the None-checks below are now the
        # ONLY gate, matching the declared contract exactly. Pre-release
        # (#4552: 未リリース) — the eager-build cost this newly enables for
        # a "wrappers off + embedding on" operator requires BOTH
        # ``embedding.enabled: true`` AND the separately opt-in
        # ``eager_embedding_build: true`` (both default False), so exposure
        # is a narrow, deliberate triple opt-in, not a default-path cost.
        #
        # P2b: the eager-vs-background DECISION + once-per-chain spawn dedup
        # + failure-memo are now routed through ``IndexCoordinator`` (see
        # ``_ensure_action_index_built`` / ``ensure_built``). P2-convergence
        # PR2 (#3270 §3): the failure-memo is now SOLELY
        # ``IndexCoordinator.build_failed(source_id)`` — the RouterLoop's own
        # ``_action_index_build_failed`` flag (a twin, in-sync-by-convention
        # signal) and the production-dead
        # ``_build_action_embedding_index_background`` primitive (the flag's
        # only setter) are both removed; production failure-tracking lives
        # only in the Coordinator's ``_failure_memo``.
        _search_visible = False
        _idx_getter = getattr(host, "get_action_embedding_index", None)
        _provider_getter = getattr(host, "get_embedding_provider", None)
        _model_getter = getattr(host, "get_embedding_model_class", None)
        _eager_getter = getattr(host, "get_eager_embedding_build", None)
        _idx = _idx_getter() if _idx_getter else None
        _provider = _provider_getter() if _provider_getter else None
        _model_class = _model_getter() if _model_getter else None
        _eager_embedding_build = bool(_eager_getter()) if _eager_getter else False
        # #1458: a prior build failure in this session must not spawn a
        # retry. P2-convergence PR2 (#3270 §3, REVISED after a co-vet-
        # caught regression): the suppression is enforced inside
        # ``_ensure_action_index_built`` itself (checked at ITS entry,
        # reading ``IndexCoordinator.build_failed(source_id)`` — the
        # single STATE owner), NOT here and NOT inside
        # ``IndexCoordinator.ensure_built`` (an earlier revision put it
        # there, which silently broke the §G2 heal contract:
        # ``search_await`` calls ``ensure_built`` directly, for every
        # OTHER registered source too, to heal a dirty/failed entry
        # once its provider recovers — a blanket suppression inside
        # ``ensure_built`` made that path permanently stuck). Both
        # calls below are therefore UNCONDITIONAL here (no caller-side
        # ``build_failed`` mirror) — the callee is the single AUTO-
        # rebuild chokepoint that decides whether to actually attempt a
        # build, so the calls resolve as a cheap no-op after a failure
        # without this call site needing to know that.
        #
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
            await self._ensure_action_index_built(
                _idx, _provider, _model_class, await_completion=True,
            )
        if (
            _idx is not None
            and _model_class
            and getattr(_idx, "is_ready", lambda: False)()
        ):
            _search_visible = True
        # FP-0034 Phase 2 step 1 / FP-0066 P2b: kick off the background
        # build when the index is configured but not yet ready.  The
        # build is idempotent (= same catalog hash → no-op) and
        # serialised by the index's internal lock; once-per-source
        # in-flight dedup is now IndexCoordinator's ``ensure_built``
        # (``_bg_tasks``, P2-convergence PR1 eliminated the two-path
        # ``ensure_built_self_contained`` this comment used to name),
        # not a RouterLoop instance flag.
        if (
            _idx is not None
            and _provider is not None
            and _model_class
            and not getattr(_idx, "is_ready", lambda: False)()
        ):
            await self._ensure_action_index_built(
                _idx, _provider, _model_class, await_completion=False,
            )
        if _univ_enabled:
            # FP-0066 P3b (#3247 firm §3): repo_doc/repo_src are "static"
            # sources too — same background-schedule shape as the action-
            # catalog block just above, but via the material-producing
            # ``IndexCoordinator.ensure_built`` (not
            # ``ensure_built_self_contained`` — the repo builders have no
            # pre-existing self-contained build primitive to preserve, so
            # they use the Coordinator's own cross-process lock + embed-
            # verify-write directly, same as memory/skill). ``await``ing
            # ``sync_repo_ingest_background`` here does NOT block this turn
            # on the embedding build itself — the ``await_completion=False``
            # branch inside it only schedules ``asyncio.create_task`` and
            # returns; §8's "never a foreground surprise" holds because the
            # ACTUAL embed/write work runs on the background task, not on
            # this awaited call. Best-effort (never raises) + no-ops
            # entirely when ``embedding.enabled`` is false, so this call is
            # safe on every turn regardless of embedding configuration.
            #
            # #4564 note: unlike the action-index block above, this repo-doc/
            # repo-src ingest is a SEPARATE feature (FP-0066 P3b) with its
            # own declared contract that has never claimed to be
            # ``embedding.enabled``-only — left gated on ``_univ_enabled``
            # here, out of #4564's scope (which is specifically about
            # search_actions's declared gate).
            try:
                from reyn.data.index.knowledge_ingest import sync_repo_ingest_background

                await sync_repo_ingest_background(
                    self._get_index_coordinator(),
                    self.host.make_router_op_context(),
                    events=self.host.events,
                )
            except Exception:
                pass
        # #272/#1128: compute the OS context-size signal once. It is None when
        # the window is ample (then compact stays hidden + the SP header is
        # omitted); non-None when filling (compact tool + header appear together).
        _ctx_signal = _render_context_size_signal_for_host(host)
        # #1593: build the presentation via the active scheme (tools= payload +
        # the tool-use SP). Universal delegates to the router's `present` op →
        # byte-identical (build_tools with the catalog wrappers). PR-2/3 schemes
        # shape tools= differently. The OS still projects _catalog from the
        # payload and injects the scheme-owned SP slots below.
        # #1593 PR-4: capture the build_presentation inputs so the OS RePresent arm
        # (run_loop) can re-call build_presentation with a refinement + the
        # accumulated `presented` set. Stashed on self (RouterLoop is per-run state,
        # like self._catalog) — NOT the scheme (a registered singleton).
        _scheme_available = {
            # #1593 PR-3 / #3378: the session's EFFECTIVE contextual narrowing, so a
            # scheme presenting actions outside tools= (CodeAct's code-API) omits the
            # same rows the JSON path drops — presentation parity with
            # ``apply_contextual_visibility`` below, and with the live gate. Was
            # ``exclude_tools`` (a frozenset), which could not express the
            # topology/delegate/ephemeral narrowing NOR an allow-list. Stashed too, so
            # a RePresent re-present keeps it.
            "contextual_permission": self._contextual_permission,
        }
        # #1791 A2: resolve the router model class → coarse family (raw FACT; the
        # scheme derives the non-Claude operational-steering policy from it, P7-clean).
        # Single classifier (model_resolver.model_family); resolved string carries the
        # family regardless of proxy prefix.
        _rm_family = "other"
        try:
            from reyn.llm.model_resolver import model_family as _model_family
            _rsv = getattr(self.host, "resolver", None)
            _resolved = (
                _rsv.resolve(self.router_model).model
                if _rsv is not None and hasattr(_rsv, "resolve")
                else self.router_model
            )
            _rm_family = _model_family(_resolved)
        except Exception:
            _rm_family = "other"
        _scheme_layer_ctx = {
            "univ_enabled": _univ_enabled,
            "search_visible": _search_visible,
            "ctx_signal_present": _ctx_signal is not None,
            # #1627 Stage 1: raw FACTS the scheme turns into policy. The scheme
            # (not the OS) derives discovery_mandate + non_interactive idiom from
            # these. OS does NOT pre-compute discovery_mandate here (P7-clean).
            "router_model": self.router_model,
            "router_model_family": _rm_family,  # #1791 A2: raw family fact; scheme gates non-Claude
            "non_interactive": self._non_interactive,
            # #2548 PR-A: skill registry snapshot (enabled skills only). The
            # scheme layer renders the ## Skills block from this into the
            # dedicated slot_post_skills. getattr fallback keeps narrow hosts
            # (plan-step host / FakeRouterHost without the accessor) at None →
            # no Skills section (byte-identical to no-skills configs).
            "available_skills": (
                getattr(self.host, "get_available_skills", lambda: None)()
            ),
        }
        self._scheme_available = _scheme_available
        self._scheme_layer_ctx = _scheme_layer_ctx
        _pres = await self._scheme.build_presentation(
            _scheme_available, _scheme_layer_ctx, ops=self,
        )
        from reyn.tools.scheme import advertised_entries  # noqa: PLC0415

        # #3421: the WIRE view of the scheme's ``tools=`` channel. Both arms
        # collapse to a list here and that is correct at this boundary — a
        # transport with no channel (``content_fence``) and a channel with no
        # eligible tool both send ``tools=[]``. What must NOT collapse is the
        # dispatch gate, and it does not: a no-channel cell is required to carry
        # ``dispatchable_catalog``, read below.
        tools = advertised_entries(_pres.tools_channel)
        # #187 STEP 1c (owner principle): actions are enumerated ONLY by
        # list_actions, and their schemas ONLY by describe_action. The former
        # ARS block (B37/B38) inlined the whole session action catalog into
        # invoke_action's description — a SECOND enumeration surface that the
        # owner directive disallows — so it is removed here (its two builder
        # functions, _collect_all_session_ars_entries / _enrich_invoke_action_
        # description, are deleted as dead). Sibling-tool cross-ref pointers
        # (e.g. write_file → edit_file, #1420) hand the model the specific
        # action names it needs without re-listing the catalog; for the rest,
        # discovery is list_actions and schema is describe_action.
        # #3378: advertisement is derived from the SAME effective contextual the live
        # gate enforces (was: ``exclude_tools`` alone → a topology/delegate/ephemeral
        # contextual left the tool advertised and denied it only at call time).
        tools = apply_contextual_visibility(tools, self._contextual_permission)
        self._catalog = {t["function"]["name"]: t for t in tools}
        self._tool_names = frozenset(self._catalog.keys())  # backward compat
        # #1618 root-1: source the dispatch membership map from the scheme's
        # DISPATCHABLE set when it decouples it from the advertised payload (CodeAct:
        # advertises ∅, dispatches the full catalog). None ⇒ dispatch gate keys on
        # self._catalog (byte-identical for universal / enumerate / retrieval).
        if _pres.dispatchable_catalog is not None:
            from reyn.tools.scheme import dispatch_catalog_map  # noqa: PLC0415
            self._dispatch_catalog = dispatch_catalog_map(_pres.dispatchable_catalog)
        else:
            self._dispatch_catalog = None
        if self._system_prompt_override is not None:
            system_prompt = self._system_prompt_override
        else:
            # #3025: the router no longer pre-fetches SourceManifest.
            # format_for_prompt() here. The "## Indexed sources" SP section it
            # rendered was accepted by build_system_prompt as
            # ``indexed_sources_section`` and then discarded — the wrapper-only
            # SP has not injected it since B23-PRE-1, so every turn paid a
            # SourceManifest.get_all() (sources.yaml read + parse) to build a
            # string nothing read. Corpus discovery is the ``list_rag_sources``
            # verb (#3026), not the SP, so the prefetch + dead parameter are
            # removed rather than revived (reviving it would re-introduce the
            # per-corpus, operator-scaling SP cost #3026 removed).
            system_prompt = build_system_prompt(
                agent_name=host.agent_name,
                agent_role=host.agent_role,
                available_agents=host.list_available_agents(),
                memory_index=host.get_memory_index(),
                file_permissions=host.get_file_permissions(),
                mcp_servers=host.get_mcp_servers(),
                web_fetch_allowed=host.get_web_fetch_allowed(),
                output_language=host.output_language,
                project_context=host.get_project_context(),
                cwd=_cwd_str,
                # #1627 Stage 4: scheme-owned slot-map (all 4 schemes populate
                # tool_use_sp via build_universal_tool_use_slots or their own
                # renderer). The OS is a pure injector — no tool-use vocab here.
                tool_use_sp=_pres.tool_use_sp,
                # #272/#1128: OS-injected context-size signal (header), computed
                # once above. Rendered LAST in the SP (most volatile section →
                # preserves the cached prefix above it); None when ample.
                context_size_signal=_ctx_signal,
                # #1479: system info (date/platform/shell/git).
                environment_info=_environment_info,
                # #1652: prior-turns' reasoning text section (continuity). Host
                # returns "" when continuity is off / no prior reasoning →
                # omit-when-empty (byte-identical SP). The model's own thoughts
                # are stripped from the wire assistant messages (built explicitly
                # from content+tool_calls), so this text-section is the single
                # replay vehicle on gemini (no native double-inject).
                reasoning_continuity_section=getattr(
                    host, "reasoning_continuity_section", lambda: ""
                )(),
                non_interactive=self._non_interactive,
            )
        # Session._handle_inbox_text appends the user turn to history
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
        #     Session already appended it via _append_history); or
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
        chat-specific pre-loop setup) drives it as the single shared op-execution
        loop. (The extraction originally also served a phase host driving the SAME
        loop for true chat/phase convergence; that host — ``PhaseRouterLoopHost`` —
        was deleted in #2438, leaving chat as the only driver. The chat-specific
        terminals below remain host-polymorphic from that design.)
        """
        host = self.host
        # 0062 §2.1 failure-mode-(a): the model-support pre-check runs BEFORE this
        # turn's very first LLM call (tool-decision calls included) — a schema
        # the resolved model cannot honor at all is rejected here so NO completion
        # is ever wasted on it, whether this turn is a no-capability agent's sole
        # call or a tool-using agent's multi-round turn.
        if self._response_format is not None:
            from reyn.llm.litellm_bootstrap import (
                LitellmUnavailableError,
                ensure_litellm_ready,
            )
            from reyn.llm.llm import proxy_kwargs, routing_for_spec
            from reyn.runtime.errors import StructuredOutputUnsupportedModelError
            _spec_fn = getattr(host, "resolve_model_spec", None)
            if _spec_fn is not None:
                _precheck_spec = _spec_fn(self.router_model)
            else:
                from reyn.llm.model_resolver import ModelSpec
                _precheck_spec = ModelSpec(model=host.resolve_model(self.router_model), kwargs={})
            # Strip the proxy provider-prefix EXACTLY like ``recorded_acompletion``
            # does (llm.py) before checking — an operator-configured
            # ``"openai/gemini-2.5-flash-lite"`` proxy alias must be checked as
            # the underlying gemini model, not misclassified as an (unsupported)
            # literal OpenAI model name.
            _routing = routing_for_spec(_precheck_spec)
            _extra = _routing if _routing is not None else proxy_kwargs()
            _precheck_model = (
                _precheck_spec.model.split("/", 1)[1]
                if _extra.get("api_base") and "/" in _precheck_spec.model
                else _precheck_spec.model
            )
            # #4395: this precheck genuinely needs a real answer (no fallback
            # exists — structured-output support must actually be known
            # before the turn's first LLM call) and PREVIOUSLY did its own
            # unconditional `import litellm` right after `ensure_litellm_
            # ready()` without checking its return value — a #4395-shaped
            # double-attempt-on-failure site missed by every census so far
            # (this call site was in neither PR-1's Set A/B nor #4415's
            # in-progress recount). `run_loop` is `async def`, so a
            # synchronous call here blocked the WHOLE event loop for as
            # long as the underlying import took — via `asyncio.to_thread`
            # instead, matching `llm.py`'s own `recorded_acompletion` fix
            # (#4413): only the coroutine waits, not the loop.
            # `ignore_cooldown=True`: this precheck has no fallback — the
            # axis② cooldown protects fallback-having callers, not this
            # one; see `ensure_litellm_ready()`'s own docstring.
            litellm = await asyncio.to_thread(ensure_litellm_ready, ignore_cooldown=True)
            if litellm is None:
                raise LitellmUnavailableError(
                    "import litellm failed — see the reyn.llm.litellm_bootstrap "
                    "warn-once log line for the underlying cause",
                )
            if not litellm.supports_response_schema(_precheck_model):
                raise StructuredOutputUnsupportedModelError(
                    f"model {_precheck_model!r} does not support structured output "
                    "(litellm.supports_response_schema returned False) — schema-"
                    "constrained generation (response_format) requires provider "
                    "support. Choose a different model class for this agent step, "
                    "or drop its `schema`."
                )
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
        # #3455: routing_decided is now emitted from ``_dispatch_resolved`` —
        # the single chokepoint every catalog dispatch funnels through
        # (invoke_action wrapper, ARS-salvaged direct call, AND the
        # flat/default bare-name dispatch that runs when universal wrappers
        # are OFF, which the prior ``_univ_enabled``-gated emit never
        # covered). An instance attribute (not a run_loop-local)
        # because the emit site moved to a method called from multiple
        # places; reset here so it reads "did routing happen THIS turn".
        # _tool_calls_attempted: count of tool_call rounds where the LLM
        #   invoked at least one tool (including non-catalog tools).
        self._routing_decided_this_turn: bool = False
        _tool_calls_attempted: int = 0
        # B42-NF-W6-1: empty-stop retry counter. The empty-stop handler
        # consults this before injecting a continuation prompt + looping,
        # so retries are bounded at 1 per turn (= no infinite loops if
        # the LLM keeps returning empty stops even with the continuation
        # prompt; the second empty stop falls through to the standard
        # "observe + surface" path).
        _empty_stop_retries: int = 0
        # #1593 PR-4: OS RePresent convergence state (per-turn loop-locals — NOT
        # scheme self-state; schemes are registered singletons). ``_represented``
        # is the monotonic accumulator of every candidate the scheme has presented
        # this turn, threaded into build_presentation so the scheme self-determines
        # convergence. ``_represent_rounds`` feeds the defensive backstop.
        _represented: set = set()
        _represent_rounds: int = 0
        # Which LLM round of this turn the deltas below belong to. Monotonic
        # across the WHOLE turn (never reset by the outer re-entry loop), so a
        # consumer can tell rounds apart and see a gap if one is lost.
        self._delta_round_index = 0

        # FP-0005 max_iterations checkpoint: outer while allows re-entry after
        # an approved extension. _loop_cancelled tracks cancel-break vs exhaustion.
        _loop_cancelled = False
        while True:
         for _iteration in range(self.max_iterations):
            self._delta_round_index += 1
            # #1468: cooperative turn-cancel checkpoint. Checked BEFORE the LLM
            # call so a cancel_inflight() fired between tool iterations stops the
            # chain at the next boundary (= after the current tool completes, not
            # mid-call). getattr-guarded → phase hosts that don't implement
            # _is_turn_cancel_requested are no-ops (byte-identical).
            _cancel_fn = getattr(host, "_is_turn_cancel_requested", None)
            if callable(_cancel_fn) and _cancel_fn():
                host.events.emit("turn_cancelled", chain_id=self.chain_id)
                _loop_cancelled = True
                break
            # #1909 / #3501 (OPT-IN, default off): intra-turn untrusted-content
            # re-narrowing. ``self._intra_turn_contextual_for_turn_fn`` is
            # None unless ``safety.threat_scan.capability_narrowing`` is
            # ``"iteration"`` — the top rung of the three-value ladder
            # (off/turn/iteration); RouterLoopDriver threads it on that rung
            # only, so both ``off`` and ``turn`` take NEITHER branch here and
            # ``self._contextual_permission`` stays the turn-frozen value set
            # at __init__.
            #
            # On the opt-in path: re-invoke the live history tag-scan every
            # iteration so external content spliced in round N narrows
            # dispatch in round N+1 of the SAME turn (closes the same-turn
            # injection window). ★ Monotonic latch: once ANY iteration this
            # turn observes the ``external_source`` taint, stay narrowed
            # through the rest of the turn even if a later compaction
            # evicts the tainted history entry — ``_effective_contextual_
            # for_turn`` self-clears on compaction (until-compaction
            # scope), so a naive re-scan would let capability RECOVER
            # mid-turn after compaction (a taint-laundering hole). The
            # latch closes it; it clears only at the turn boundary (a
            # fresh RouterLoop per user turn).
            if self._intra_turn_contextual_for_turn_fn is not None:
                _resolved_contextual = self._intra_turn_contextual_for_turn_fn()
                _live_tainted = (
                    _resolved_contextual is not self._contextual_static_baseline
                )
                if _live_tainted:
                    if not self._untrusted_latched:
                        self._untrusted_latched = True
                        host.events.emit(
                            "untrusted_narrowing_engaged",
                            chain_id=self.chain_id,
                            iteration=_iteration,
                            provenance="external_source",
                        )
                    self._untrusted_latched_permission = _resolved_contextual
                    self._contextual_permission = self._with_exclude_tools(
                        _resolved_contextual
                    )
                elif self._untrusted_latched:
                    # Compaction evicted the marker mid-turn — latch holds.
                    self._contextual_permission = self._with_exclude_tools(
                        self._untrusted_latched_permission
                    )
                # #3378: the agreement is per-CALL, not per-turn. The re-resolve above
                # can only NARROW, so re-run the advertisement filter on the live
                # payload — otherwise round N+1 would be offered tools round N+1's gate
                # now rejects (the same advertise/enforce split, at finer grain).
                tools = apply_contextual_visibility(
                    tools, self._contextual_permission
                )
                self._catalog = {t["function"]["name"]: t for t in tools}
                self._tool_names = frozenset(self._catalog.keys())
            resolved_model = host.resolve_model(self.router_model)
            # #1654: the FULL ModelSpec (model + operator kwargs) for the LLM
            # call below, so per-model kwargs (reasoning_effort #1650/#1652,
            # temperature, extra_body, …) reach litellm. resolve_model returns
            # the bare string (kwargs dropped) — fine for the model-NAME params
            # (compaction / force-close / memo-key / events) which keep
            # resolved_model, but the actual call_llm_tools must get the spec.
            # Host-polymorphic: test hosts without resolve_model_spec fall
            # back to a kwargs-less spec = byte-identical to the prior behaviour.
            _spec_fn = getattr(host, "resolve_model_spec", None)
            if _spec_fn is not None:
                resolved_spec = _spec_fn(self.router_model)
            else:
                from reyn.llm.model_resolver import ModelSpec
                resolved_spec = ModelSpec(model=resolved_model, kwargs={})
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
            # #3792: mid-turn injection seam — the ONE design decision the
            # whole feature hinges on (architect, #3792; PR1 #3802 locked in
            # this position). Position: after the per-iteration guards above
            # (the cancel checkpoint at the top of this loop — a cancelled
            # turn ``break``s before ever reaching here), and immediately
            # before whichever send this iteration makes, so an injected
            # message can only land between tool-call/tool-result rounds,
            # never mid-send (wire_format.py's adjacency requirement forbids
            # splitting an assistant(tool_calls) / role=tool group — the
            # assert below is the load-bearing check for that). Skipped
            # during a force-close send (``_force_close_now``): that call is
            # a special wrap-up/summary turn, not an ordinary conversation
            # round, and is out of this feature's scope (issue #3792 scope
            # note: injection only, the force-close/cancel axes are
            # untouched). getattr-guarded → hosts that don't implement the
            # hook (phase hosts; production chat host implements it via
            # ``RouterHostAdapter``, PR2 #3792) are a no-op.
            _peek_injection_fn = getattr(host, "peek_mid_turn_injection", None)
            if _peek_injection_fn is not None and not _force_close_now:
                _injection = await _peek_injection_fn()
                if _injection is not None:
                    _injected_payload = _injection["payload"]
                    _injected_msg = {
                        "role": "user",
                        "content": _injected_payload.get("text") or "",
                    }
                    messages = [*messages, _injected_msg]
                    # #3792: wire-position assert (architect's flagged
                    # pitfall) — a naive splice between an
                    # assistant(tool_calls) message and its role=tool
                    # results does NOT 400; wire_format.py's
                    # ``repair_tool_call_pairing`` silently RE-ADJACENTS it
                    # instead, and the resulting warning names the
                    # tool_result as the anomaly, not the injection. By
                    # construction this seam only ever fires between
                    # completed rounds (never mid-pair), so the injected
                    # message must survive repair at the TAIL, unmoved —
                    # this assert is the fail-fast witness of that
                    # invariant, checked BEFORE the send, not discovered
                    # downstream as a misattributed warning.
                    from reyn.llm.wire_format import repair_tool_call_pairing
                    _repaired = repair_tool_call_pairing(messages)
                    assert _repaired[-1] is _injected_msg, (
                        "#3792: mid-turn injection did not land at the tail "
                        "after wire repair — the seam's never-mid-pair "
                        "position invariant was violated"
                    )
                    _commit_injection_fn = getattr(
                        host, "commit_mid_turn_injection", None,
                    )
                    if _commit_injection_fn is not None:
                        await _commit_injection_fn(_injection["msg_id"])
            # ADR-0025: memo lookup — a recorded LLMToolCallResult for
            # this exact (model, messages, tools, tool_choice) tuple
            # short-circuits the call. Used by phase-step resume so a
            # crashed mid-step sub-loop replays earlier LLM turns
            # without re-paying. memo_provider is None for non-resume
            # paths (= chat router main loop, fresh phase-step runs).
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
                    from reyn.core.kernel.sub_loop_memo_key import compute_sub_loop_args_hash
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
                        # #1654: pass the FULL ModelSpec (not the bare string) so
                        # per-model kwargs (reasoning_effort/temperature/…) reach
                        # litellm; call_llm_tools accepts Union[str, ModelSpec].
                        model=resolved_spec,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        budget=self.budget,
                        budget_agent=host.agent_name,
                        trace_caller="router",
                        # #1683: chat path emits llm_called + llm_response_received
                        # so the TUI cost tab updates (kernel emits via LLMCallRecorder).
                        emit_cost_events=True,
                        # #3288 ③b: forward streamed content-deltas as audit-events
                        # (③a's capability gate decides whether this ever fires —
                        # a non-streaming call never invokes it).
                        on_content_delta=self._emit_agent_delta,
                        # #4206 T1 (②bounding): this call resolved its model
                        # class via ``class_for_purpose`` (self.router_model)
                        # — subject to the ceiling.
                        model_class=self.router_model,
                        model_class_ceiling=self._model_class_ceiling,
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
                self._last_call_usage = result.usage
            # #1593 loop-unify (Issue-1): interpret-driven routing — the active
            # scheme classifies EVERY result, instead of the OS sniffing
            # ``result.tool_calls``. universal-category returns Execute when there
            # are tool calls and PlainText when there are none, so the Execute gate
            # below + the PlainText fall-through to the text-reply path are
            # byte-identical to the former ``if result.tool_calls:`` gate. CodeBlock
            # (PR-3) / RePresent (PR-4) stay S1 stubs in this seam PR — no scheme on
            # main emits them, so they are unreached (the seam PR proves the routing
            # change byte-identically, isolated from CodeAct/retrieval).
            from reyn.tools.scheme import (  # noqa: PLC0415
                CodeBlock,
                Execute,
                ExecutionResult,
                RePresent,
                advertised_entries,
            )
            # #1666: bound the per-turn tool_call count BEFORE interpret, so all
            # branches + the assistant↔tool-result alignment inherit the cap from a
            # single choke point. ``_cap_info`` carries (attempted, kept) when it
            # fired → the round appends a re-grounding notice after its results.
            _cap_info = self._enforce_tool_call_cap(result)
            interp = self._scheme.interpret(
                result, tool_catalog=self._catalog, ops=self,
            )
            if isinstance(interp, CodeBlock):
                # #1593 PR-3 CodeAct (design (a)): run the snippet in the sandboxed
                # CodeActRunner — each in-code tool() call re-enters the SAME exclude
                # + dispatch_tool + permission gate per call (so a CodeAct call is
                # gated >= a JSON call) — then append the [assistant: code] turn + the
                # scheme's format_feedback message(s) and loop. A CodeBlock turn has
                # no tool_calls, so the scheme returns observation message(s) and the
                # OS only appends (no synthetic tool_call = provider-safe). #1608
                # (#1611) UNIFIED format_feedback to a SINGLE-SHAPE appendable-messages
                # contract across BOTH paths: the Execute arm delegates to ops.feedback
                # (the assistant + {role:tool, tool_call_id} zip relocated there, so the
                # OS no longer knows the JSON correlation shape — P7), this CodeBlock arm
                # returns observation messages; the OS *appends* in either case. The arms
                # stay separate because the INTERPRETATION differs (snippet vs tool_calls),
                # not because the feedback shape differs.
                cb_feedback = await self._run_codeblock_round(interp)
                _cb_content = result.content or ""
                messages.append({"role": "assistant", "content": _cb_content})
                _cb_append = getattr(host, "append_history_entry", None)
                if _cb_append is not None:
                    _cb_append(
                        role="assistant", content=_cb_content,
                        meta={"chain_id": self.chain_id, "source": "router_codeblock_turn"},
                    )
                for _cb_msg in cb_feedback:
                    messages.append(_cb_msg)
                    if _cb_append is not None:
                        _cb_append(
                            role=_cb_msg.get("role", "user"),
                            content=_cb_msg.get("content", ""),
                            meta={"chain_id": self.chain_id, "source": "router_codeblock_turn"},
                        )
                continue
            if isinstance(interp, RePresent):
                # #1593 PR-4: the generic OS RePresent mechanism (ratified seam). The
                # scheme classified the LLM output as a re-present request (e.g. a
                # retrieval search). The OS, generically (no scheme concepts — P7):
                #   1. records the assistant turn + a synthetic tool-response for each
                #      intercepted tool_call (OpenAI requires every tool_call answered);
                #   2. re-calls build_presentation with the refinement + the
                #      accumulated ``presented`` set (an OS loop-local, NOT scheme
                #      self-state — schemes are registered singletons);
                #   3. swaps the advertised tools + the dispatch ``_catalog`` mirror;
                #   4. accumulates ``presented`` and re-enters the main iteration via
                #      ``continue`` (budget / compaction / force-close / memo all keep
                #      applying — no inner LLM loop).
                # Convergence is the SCHEME's: it self-determines terminal from
                # ``presented`` + drops the search tool → the next turn can only
                # Execute. Bounded by construction (monotonic ``presented`` on a
                # finite catalog). The round counter is a defensive valve for a
                # non-converging scheme, never the bound; max_iterations is the cap.
                _represent_rounds += 1
                if _represent_rounds > _MAX_REPRESENT_ROUNDS:
                    raise RuntimeError(
                        "RePresent did not converge within the defensive backstop "
                        f"({_MAX_REPRESENT_ROUNDS} rounds) — the active scheme is not "
                        "terminating its re-present loop"
                    )
                # ``or []`` guard (sandbox_2 co-vet): retrieval always RePresents off
                # a tool_call (the search), but the arm is GENERIC — a future scheme
                # could re-present off assistant text (no tool_call), in which case
                # there is simply nothing to synthetic-answer.
                _re_tool_calls = result.tool_calls or []
                messages.append({
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": _re_tool_calls,
                })
                for _tc in _re_tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": _tc["id"],
                        "content": _REPRESENT_ACK,
                    })
                # #1666: if the cap fired on this re-present turn, append the
                # re-grounding notice after the synthetic acks (cost-bound applies
                # to every branch, not only Execute).
                if _cap_info is not None:
                    messages.append(self._tool_call_cap_notice(*_cap_info))
                _represent_layer_ctx = {
                    **(getattr(self, "_scheme_layer_ctx", None) or {}),
                    "refinement": interp.refinement,
                    "presented": tuple(_represented),
                }
                _re_pres = await self._scheme.build_presentation(
                    getattr(self, "_scheme_available", None) or {},
                    _represent_layer_ctx,
                    ops=self,
                )
                tools = apply_contextual_visibility(
                    advertised_entries(_re_pres.tools_channel),
                    self._contextual_permission,
                )
                self._catalog = {t["function"]["name"]: t for t in tools}
                self._tool_names = frozenset(self._catalog.keys())
                _represented |= set(_re_pres.candidates)
                host.events.emit(
                    "router_represent_round",
                    chain_id=self.chain_id,
                    round=_represent_rounds,
                    presented_count=len(_represented),
                )
                continue
            if isinstance(interp, Execute):
                # B28-Q2: count non-empty tool_call rounds for chat_turn_completed_inline.
                _tool_calls_attempted += 1
                # #1642: surface the assistant's TEXT content that accompanies tool_calls.
                # The terminal text-reply path (below, ~line 2638) only fires for a
                # no-tool_calls turn, so on a tool-turn the explanatory text was dropped
                # from the conversation. Emit it as an ``agent`` bubble BEFORE
                # _run_execute_round so the text renders ahead of the tool rows
                # (lifecycle_forwarder queues tool_call_started during the round). Skip
                # empty (no empty bubble).
                #
                # #3633: this call is DISPLAY-ONLY — ``persist=False``. The prior
                # comment here ("no double-emit ... history persistence is unchanged")
                # was wrong: it only ruled out collision with the *different*
                # no-tool_calls terminal path further down this file and missed that
                # ``self._scheme.feedback()`` (called a few lines below, via
                # ``format_feedback`` → ``append_history_entry``) independently
                # persists this SAME text moments later as the canonical
                # ``source="router_tool_turn"`` record — the one that also carries
                # ``tool_calls``, so it is the complete turn. Without ``persist=False``
                # RouterHostAdapter's unconditional ``kind=="agent"`` → append-to-
                # history side effect wrote the identical string to history.jsonl
                # twice (measured: 24/283 adjacent-duplicate assistant records in a
                # real session, #3633).
                _tool_turn_text = result.content or ""
                if _tool_turn_text.strip():
                    await self.host.put_outbox(
                        kind="agent",
                        text=_tool_turn_text,
                        persist=False,
                        # #1652: supply this turn's reasoning (host gates display/
                        # continuity + emits the discrete kind="reasoning" signal).
                        meta={
                            "chain_id": self.chain_id,
                            "source": "router_tool_turn_text",
                            "reasoning": result.reasoning,
                            # #4691: this row IS the call that just returned
                            # ``result`` — its own real per-call tokens, not
                            # the turn total (see gutter.py's ReynTurnUsageGutter
                            # docstring for why this is the boundary fix).
                            "prompt_tokens": getattr(result.usage, "prompt_tokens", None),
                            "completion_tokens": getattr(result.usage, "completion_tokens", None),
                        },
                    )
                # F5 fix (dogfood batch 1): dedupe duplicate async
                # tool_calls within the same round. Weak models
                # occasionally emit `delegate_to_agent` twice in one
                # tool_calls list, which would inbox_put the same
                # request twice and double-charge the peer.
                # G3 fix (dogfood batch 5 B5-M1): extend dedupe to
                # sync tool calls too — same tool + same args in one
                # round spawns redundant runs (333k tokens / 51 LLM
                # calls observed). A sync call is NOT idempotent from a
                # cost perspective; deduping is safe because same args →
                # same deterministic result.
                # #1593: execute the Execute round (interpret already ran at the
                # loop level above → ``interp`` carries the resolved actions). The OS
                # exclude-gates pre-dispatch inside _run_execute_round → dispatch →
                # format_feedback; returns (tool_calls, tool_results) in deduped order.
                tool_calls, tool_results = await self._run_execute_round(interp)
                # Detect async-deferred dispatches via the canonical
                # registry (router_tools.get_dispatch_kind() →
                # ToolDefinition.dispatch_kind).  Async tools'
                # results arrive via a separate channel (e.g.
                # spawn_session → PR14 pending_chain re-invokes router
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
                    # Proposal 0067 P1' (#3978): mark the session's own task
                    # as still outstanding BEFORE exiting the turn — this is
                    # exactly the turn-boundary MessageBus.request's
                    # quiescence check needs to see, so a delegating
                    # top-level requester keeps waiting for the peer's real
                    # answer instead of returning this ack as if it were
                    # final (the bug P1' exists to close; see
                    # message_bus.py's _is_quiescent).
                    self.host.mark_task_pending()
                    # B55 R-7 (2026-05-25): non-plan async dispatch (=
                    # spawn_session or other peer-async tools). Mirror
                    # task / plan spawn_ack format: `[task_spawned]
                    # kind=prompt ...` header + user-facing trailer so the
                    # SP TASK_SPAWNED rule covers this path too. Prior
                    # behaviour pushed a generic `status` row with no
                    # structured header, leaving the LLM without a task
                    # lifecycle anchor when the corresponding
                    # `[task_completed] kind=prompt ...` injection arrives.
                    #
                    # Extract peer / request hint from the first async
                    # tool call (= spawn_session's `request` argument, or
                    # delegate_to_agent's retired `to` + `request` shape
                    # for any still-async peer tool). Fallback to a
                    # generic "peer agent" header when arguments aren't
                    # parseable (= defensive for malformed args).
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
                        f"[task_spawned] kind=prompt "
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
                            # #4691: same call as the tool-turn text row above.
                            "prompt_tokens": getattr(result.usage, "prompt_tokens", None),
                            "completion_tokens": getattr(result.usage, "completion_tokens", None),
                        },
                    )
                    return self._total_usage

                # #3455: routing_decided is no longer emitted here. It used to
                # live in this loop, gated on ``if _univ_enabled:`` — which
                # meant the opt-out configuration (an operator setting
                # ``action_retrieval.universal_wrappers_enabled: false`` in
                # reyn.yaml → flat bare-name ``tools=``, the pre-PR-3b-iv
                # shape) never emitted it at all, even though catalog routing
                # was happening on every dispatched call. The
                # emit is now inside ``_dispatch_resolved`` (the #3429-census
                # chokepoint every dispatch funnels through — invoke_action,
                # ARS-salvaged direct call, and the flat bare-name path
                # alike), so coverage is structural rather than a property of
                # which entry surface the model happened to use.
                # ``self._routing_decided_this_turn`` (set there) is the
                # B28-Q2 inline-exclusivity flag consumed below.
                # #1608: the active scheme builds the appendable message sequence
                # (assistant tool-call turn + per-result {role:tool, tool_call_id}
                # messages + media follow-ups) AND persists each to history; the OS
                # only *appends*. universal / enumerate-all / retrieval delegate to
                # ops.feedback (the relocated construction) → the OS loop no longer
                # inlines the JSON tool_call/result zip (P7). tool_calls[i] aligns
                # with tool_results[i] (the #1406/#187 excluded-in-place row keeps its
                # index). CodeBlock/RePresent never reach here (separate arms).
                for _fb_msg in self._scheme.format_feedback(
                    ExecutionResult(
                        tool_results=tool_results,
                        tool_calls=tool_calls,
                        assistant_content=result.content or "",
                    ),
                    ops=self,
                ):
                    messages.append(_fb_msg)

                # #1666: after a capped round's results, append the single
                # re-grounding notice (placed AFTER all tool results so the
                # assistant.tool_calls ↔ tool_result pairing stays intact).
                if _cap_info is not None:
                    _cap_msg = self._tool_call_cap_notice(*_cap_info)
                    messages.append(_cap_msg)
                    _cap_append = getattr(host, "append_history_entry", None)
                    if _cap_append is not None:
                        _cap_append(
                            role=_cap_msg["role"], content=_cap_msg["content"],
                            meta={"chain_id": self.chain_id, "source": "tool_call_cap_notice"},
                        )

                continue

            # Option F (ADR-0021): detect empty-stop before treating as text reply.
            # Empty-stop = finish_reason="stop", content empty, no tool calls.
            # This is a provider-level glitch (observed at ~50% rate with weak
            # models — B7-G12 measurement). Reyn does not change context or
            # switch models when it fires.  #4486 drift fix: this comment
            # previously also said "does NOT retry" — that stopped matching
            # production the moment B42-NF-W6-1 shipped `empty_stop_retry_auto`
            # (see below; the real driver, `router_loop_driver.py`, reads it
            # from `chat.empty_stop_retry` — #4677, owner default `False`
            # since 2026-08-14, was hardcoded `True`; an operator on a weak
            # model that needs the retry sets the config key back on).
            #
            # #4486: this is turn-scoped, not response-scoped — a tool call
            # dispatched EARLIER this turn (`_tool_calls_attempted > 0`) means
            # an otherwise-"empty" response may be the model correctly having
            # nothing further to add (e.g. `present`'s entire contract is that
            # the tool call itself is the answer), not ADR-0021's glitch
            # (calibrated against a turn's very FIRST response, no preceding
            # tool call). `_is_empty_router_response` itself can't tell these
            # apart — its only argument is the one `response` — so the turn
            # context is supplied HERE, at the call site, not folded into that
            # narrower predicate. architect ruling (#4486): the asymmetry
            # decides it — a false positive here ASSERTS a failure that never
            # happened (a completed, successful turn reads as broken to the
            # user); a false negative only drops a trailing summary line, on
            # work already done. Deliberately `_tool_calls_attempted` (a
            # per-turn monotonic count, reset once at the top of `run()`, only
            # ever incremented — never "did the immediately preceding response
            # have tool_calls", which would miss the tool -> empty -> resume ->
            # empty shape: the resume round's own response has no tool_calls).
            # Not merged with #4453's taint latch despite the same per-turn/
            # monotonic SHAPE — different STATE, different reset trigger
            # (compaction there, turn boundary here).
            if _is_empty_router_response(result):
                if _tool_calls_attempted > 0:
                    return self._total_usage
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
                # B42-NF-W6-1 detect-and-retry: directive-gated, auto- or
                # env-var-gated, max 1 retry per turn (`empty_stop_retry_auto`
                # is the real production switch — see the module-docstring
                # note above; the env var is a secondary opt-in for callers
                # that construct RouterLoop without it). Trace-patch-replay
                # verified 0/10 → 10/10 narration recovery on the W6-S1
                # plan-step empty stop.
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
                        # #4691: a genuine (if content-empty) call's own usage.
                        "prompt_tokens": getattr(result.usage, "prompt_tokens", None),
                        "completion_tokens": getattr(result.usage, "completion_tokens", None),
                    },
                )
                return self._total_usage  # no retry

            # Text reply — emit and stop
            # B28-Q2 Case A: emit chat_turn_completed_inline when no catalog
            # dispatch happened in this turn (= routing_decided never fired).
            # Mutually exclusive with routing_decided per turn (P6 audit).
            if _univ_enabled and not self._routing_decided_this_turn:
                host.events.emit(
                    "chat_turn_completed_inline",
                    chain_id=self.chain_id,
                    decision="inline_reply",
                    tool_calls_attempted=_tool_calls_attempted,
                )
            # 0062 §2.1/§2.2: this resolution point IS the "answer turn" — for a
            # no-capability agent it is the sole call above; for a tool-using agent
            # it is reached only once the model stops requesting tools. When a
            # ``response_format`` is configured, do NOT emit the tool-turn's own
            # free-form ``result.content`` — issue the SEPARATE no-tools
            # constrained call instead (ADR-0035 D2 separate-decide: the tool
            # round(s) above stayed tools-only/unconstrained the whole time).
            if self._response_format is not None:
                _structured_text = await self._run_structured_answer_turn(
                    messages, resolved_spec,
                )
                await self.host.put_outbox(
                    kind="agent",
                    text=_structured_text,
                    meta={"chain_id": self.chain_id, "reasoning": result.reasoning},
                )
                return self._total_usage
            await self.host.put_outbox(
                kind="agent",
                text=result.content or "",
                # #1652: supply the turn's reasoning; the host applies the
                # display/continuity gates + the discrete kind="reasoning" emit.
                meta={"chain_id": self.chain_id, "reasoning": result.reasoning},
            )
            return self._total_usage

         # end of inner for loop
         if _loop_cancelled:
            break  # cancelled: exit outer while, emit error below

         # max_iterations exhausted — FP-0005 checkpoint
         if self._on_limit is not None:
            from reyn.runtime.limits.limit_handler import handle_limit_exceeded as _hle
            _bus = getattr(self.host, "make_intervention_bus", lambda: None)()
            _dec = await _hle(
                bus=_bus,
                on_limit=self._on_limit,
                kind="max_iterations",
                run_id=self.chain_id,
                prompt=(
                    f"Router hit the iteration limit ({self.max_iterations}). "
                    "Allow more iterations?"
                ),
                detail=f"chain_id={self.chain_id}",
                extension_amount=float(self.max_iterations),
                # #1649: non-TTY/run-once → handle_limit_exceeded falls back to
                # bounded auto-extend instead of a silent refuse (the agent
                # completes instead of an empty exit-0 stop).
                non_interactive=self._non_interactive,
            )
            if _dec.allow_continue:
                self.max_iterations += int(_dec.extension)
                continue  # re-enter inner for loop with extended limit
         break  # no extension granted or no on_limit — exit outer while

        # Cancelled path (user-initiated esc / cancel_inflight): an acknowledgement,
        # not an error. This branch previously emitted the max_iterations error
        # text by copy-paste — a bug every cancel consumer hit (inline esc + web
        # ws cancel). max_iterations exhaustion takes the limit-deny / on_limit
        # path below (where _loop_cancelled is False), never this branch, so this
        # message is cancel-only.
        if _loop_cancelled:
            await self.host.put_outbox(
                kind="system",
                text="✗ turn interrupted",
                meta={"chain_id": self.chain_id},
            )
            # #3694: durable cancelled-turn outcome — the cooperative-cancel
            # terminal (this branch fires when the loop-head check caught
            # the request before a hard Task.cancel() landed). Mirrors
            # Session.notify_turn_cancelled's shape exactly (same
            # role="system" + meta.kind precedent as notify_state_change);
            # inlined here rather than added as a new RouterLoopHost method
            # because host.append_history_entry already IS the generic
            # "persist a ChatMessage, no outbox side effect" primitive this
            # needs. The hard-cancel case (the more common mid-LLM-call
            # Ctrl+C) never reaches this branch at all — CancelledError
            # unwinds straight past it — so Session.run_one_iteration's own
            # catch is the receiver for that case, not a duplicate of this.
            self.host.append_history_entry(
                role="system",
                content="Turn interrupted by user.",
                meta={"kind": "turn_cancelled", "chain_id": self.chain_id},
            )
            return self._total_usage

        # Limit-deny path (#1496): give the LLM one final tool-less turn to
        # summarize what was accomplished before the turn ends. The cause
        # is injected as a context message (SP stays cause-neutral). A
        # structured marker on the outbox message signals forced-stop to
        # the UI without a competing prose block. Degrades to canned error
        # if the wrap-up call itself fails.
        host.events.emit(
            "limit_denied",
            kind="max_iterations",
            limit=self.max_iterations,
            chain_id=self.chain_id,
        )
        # Cause embedded in wrap-up SP (not as a trailing user message):
        # Gemini rejects a user turn immediately after a tool_result.
        _reason = f"router reached its iteration limit ({self.max_iterations} iterations)"
        try:
            _wrapup = await self._force_close_call_with_retry(
                messages,
                resolved_model=host.resolve_model(self.router_model),
                reason=_reason,
            )
            if _wrapup.content:
                # Mirror cumulative-axis: hand result to host for checkpoint
                # persistence (phase) and step-result collection (plan).
                # Only when LLM produced real text — an empty consolidation
                # would trigger a spurious phase re-entry via _last_force_close_checkpoint.
                # getattr-guarded → chat hosts don't implement it → no-op.
                _record_fc = getattr(host, "record_force_close", None)
                if _record_fc is not None:
                    _record_fc(_wrapup)
                await self.host.put_outbox(
                    kind="agent",
                    text=_wrapup.content,
                    meta={
                        "chain_id": self.chain_id,
                        "limit_stopped": True,
                        "limit_kind": "max_iterations",
                    },
                )
                return self._total_usage
        except Exception:  # noqa: BLE001 — wrap-up failed; degrade to decision-enabling error
            pass
        await self.host.put_outbox(
            kind="error",
            text=(
                f"Router loop exceeded max iterations ({self.max_iterations}). "
                f"Configure safety.on_limit.mode=interactive or auto_extend to "
                f"extend, or increase safety.loop.max_router_iterations."
            ),
            # #1649 PART B: the limit_stopped marker lets the run-once / A2A
            # caller detect a limit-abort (vs a normal reply) → surface the
            # message + exit non-zero, so a non-TTY wrapper never sees a silent
            # exit-0 stop. Mirrors the wrap-up agent message's marker above.
            meta={
                "chain_id": self.chain_id,
                "limit_stopped": True,
                "limit_kind": "max_iterations",
            },
        )
        return self._total_usage

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    def _dedupe_tool_calls_round(self, tool_calls: list[dict]) -> list[dict]:
        """Dedupe duplicate async tool_calls within the same round (F5).

        Async tools (e.g. `spawn_session`) — F5 fix (batch 1).
        Duplicates would inbox_put the same request twice, doubling peer
        cost and confusing the chain.

        Keyed on (tool_name, arguments_json). The original tool_call_id
        is preserved for the kept copy so the assistant/tool message
        alignment downstream stays intact. Sync tools are deliberately
        excluded — dupes there are wasteful but correctness-preserving and
        the tool_call_id count must stay consistent with what the LLM emitted.

        Emits a `tool_call_deduped` audit event per suppressed call.
        """
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            name = tc["function"]["name"]
            if get_dispatch_kind(name) == "async":
                key = (name, tc["function"].get("arguments", ""))
                if key in seen:
                    self.host.events.emit(
                        "tool_call_deduped",
                        name=name,
                        chain_id=self.chain_id,
                        reason="duplicate_async_in_round",
                    )
                    continue
                seen.add(key)
            deduped.append(tc)
        return deduped

    # Keep backward-compat alias (tests and callers that reference the old name
    # will still work; the alias delegates to the unified implementation).
    _dedupe_async_tool_calls = _dedupe_tool_calls_round

    def _build_force_close_messages(
        self,
        messages: list[dict],
        *,
        reason: "str | None" = None,
    ) -> list[dict]:
        """#1092 PR-B: rebuild ``messages`` for the wrap-up (force-close) call.

        The main system turn is replaced by the axis-independent wrap-up SP
        (``services/turn_budget``); all non-system turns (the working history)
        are kept verbatim so the model consolidates them. Any pre-existing
        system turn(s) are dropped (the wrap-up SP is the only system context
        for this call). Pure — no I/O — so it is unit-testable in isolation.

        Args:
            reason: Cause for the wrap-up (e.g. "router reached iteration
                limit"). Forwarded to ``wrap_up_system_prompt`` and embedded
                in the SP — NOT as a trailing user message, because providers
                with strict function-call pairing (Gemini) reject a user turn
                immediately after a tool_result. ``None`` = cumulative-axis
                (cause-neutral; replay fixtures unaffected).
        """
        non_system = [m for m in messages if m.get("role") != "system"]
        return [
            {"role": "system", "content": wrap_up_system_prompt(reason=reason)},
            *non_system,
        ]

    async def _run_structured_answer_turn(
        self, messages: list[dict], resolved_spec: Any,
    ) -> str:
        """0062 §2.1: the separate no-tools ``response_format``-constrained
        answer turn (ADR-0035 D2 separate-decide, reapplied here — the
        preceding tool-decision round(s) stayed tools-only/unconstrained the
        whole time; this is the ONE additional call that produces the actual
        structured reply). Returns the raw JSON text — the caller
        (``run_agent_step``) still ``json.loads`` + ``core.pipeline.schema.
        validate``s it as belt-and-suspenders (0062 impl-focus 3: reyn-side
        re-validate is kept even though the provider already constrained
        generation).

        Failure-mode separation (0062 §2.1, never conflated):
          - mode (a) model-unsupported: already excluded by ``run_loop``'s
            pre-check before this method is ever reached.
          - mode (b) provider rejects the SCHEMA itself: since (a) already
            passed, any exception on the FIRST attempt here can only be the
            provider's json_schema-subset validation rejecting the schema —
            raised immediately as :class:`StructuredOutputSchemaRejectedError`,
            never entered into the re-prompt loop below (re-prompting cannot
            fix an incompatible schema).
          - mode (c) generation-side non-conformance: a syntactically-fine
            call whose JSON fails ``json.loads`` or the caller-supplied
            ``schema_validate_fn`` — bounded re-prompt (feed the error back,
            ``self._max_schema_reprompt_attempts`` extra attempts), then
            :class:`StructuredOutputNonConformingError` on exhaustion.
        """
        from reyn.runtime.errors import (
            StructuredOutputNonConformingError,
            StructuredOutputSchemaRejectedError,
        )

        attempt_messages = list(messages)
        last_errors: list[str] = []
        total_attempts = 1 + self._max_schema_reprompt_attempts
        for attempt in range(total_attempts):
            call_messages = attempt_messages
            if last_errors:
                call_messages = [
                    *attempt_messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply did not conform to the required "
                            "schema:\n- " + "\n- ".join(last_errors) +
                            "\n\nReply again with ONLY the corrected JSON, "
                            "conforming exactly to the schema."
                        ),
                    },
                ]
            try:
                # Tier 2 testability (matches the tool-turn call above): honour
                # ``self._llm_caller`` when a test injected one — the constrained
                # answer-turn call must go through the SAME real-fake seam, not
                # bypass it to the real ``call_llm_tools``/litellm.
                _llm = self._llm_caller or call_llm_tools
                result = await _llm(
                    model=resolved_spec, messages=call_messages, tools=[],
                    budget=self.budget, budget_agent=self.host.agent_name,
                    trace_caller="router_structured_output",
                    emit_cost_events=True,
                    response_format=self._response_format,
                    # #4206 T1 (②bounding): same router-purpose class as the
                    # main tool-turn call.
                    model_class=self.router_model,
                    model_class_ceiling=self._model_class_ceiling,
                )
            except Exception as exc:
                if attempt == 0:
                    raise StructuredOutputSchemaRejectedError(
                        "the model/provider rejected the response_format "
                        f"schema (fail-fast, no re-prompt): {exc}"
                    ) from exc
                raise
            if result.usage:
                self._total_usage += result.usage
                self._last_call_usage = result.usage
            text = result.content or ""
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                last_errors = [f"output is not valid JSON: {exc}"]
                attempt_messages = call_messages
                continue
            errors = (
                self._schema_validate_fn(parsed)
                if self._schema_validate_fn is not None else []
            )
            if not errors:
                return text
            last_errors = list(errors)
            attempt_messages = call_messages
        raise StructuredOutputNonConformingError(
            "structured-output generation did not converge to a schema-"
            f"conforming value after {total_attempts} attempt(s); last "
            f"validation errors: {last_errors}"
        )

    async def _force_close_call(
        self,
        messages: list[dict],
        *,
        resolved_model: str,
        reason: "str | None" = None,
    ) -> "LLMToolCallResult":
        """#1092 PR-B (force-close call): turn the CURRENT turn into a clean
        ``finish`` instead of letting it overflow the cumulative budget.

        Invoked by the per-turn trigger (PR-C, ``maybe_force_close``) when the
        accumulated turn content reaches the headroom threshold. This is NOT a
        truncate (§3): it swaps the main system prompt for the wrap-up SP and
        ADVERTISES NO TOOLS (``tools=[]``), so the model cannot continue the
        task and the small consolidation output makes ``finish_reason=stop`` the
        natural outcome. ``tool_choice`` stays ``"auto"`` (``"none"`` is not
        Gemini-safe) — omitted by call_llm_tools when tools=[] (Gemini rejects
        tool_choice without function_declarations). The working history is
        preserved; only the system turn is replaced (the wrap-up SP tells the
        model to consolidate that history into a hand-off).

        This method is the single wrap-up call; the layer-2 retry-guarantee
        (overflow → host-delegated compaction shrink → retry, monotonic) wraps
        it in a follow-up commit of this same PR. Additive + unwired here, so the
        chat/phase loops stay byte-identical until PR-C wires the trigger.
        """
        _llm = self._llm_caller or call_llm_tools
        wrap_messages = self._build_force_close_messages(messages, reason=reason)
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
            tool_choice="auto",  # omitted by call_llm_tools when tools=[] (Gemini fix)
            budget=self.budget,
            budget_agent=self.host.agent_name,
            trace_caller="router_force_close",
            # #1683: chat path emits cost events for the TUI cost tab.
            emit_cost_events=True,
            # #4206 T1 (②bounding): the wrap-up call still resolves the
            # router-purpose class — same ceiling applies.
            model_class=self.router_model,
            model_class_ceiling=self._model_class_ceiling,
        )

    async def _force_close_call_with_retry(
        self,
        messages: list[dict],
        *,
        resolved_model: str,
        reason: "str | None" = None,
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
        reduces them; when the host can shrink no further it returns the
        messages unchanged (identity) = the FLOOR, which RAISES (floor-abort).
        This used to be a placeholder — "PR-D"/"PR-E", a planned
        consolidate+hand-off that would replace the raise — but #4381 PR-4
        (owner ruling: "２の force close 廃止して spill にしよう") undid that
        plan permanently: the floor-abort is the real terminal now, not a
        pre-handoff stopgap. The #4381 family (tool-result spill) is what
        keeps an oversized single result from reaching this floor at all.
        """
        shrink = getattr(self.host, "maybe_compact_messages", None)
        cur = messages
        while True:
            try:
                return await self._force_close_call(
                    cur, resolved_model=resolved_model, reason=reason,
                )
            except Exception as exc:  # noqa: BLE001
                if not is_context_overflow_error(exc):
                    raise
                if shrink is None:
                    # Chat host: no in-loop shrink — propagate to the outer
                    # session retry_loop (B′ axis-inherited path).
                    raise
                shrunk = await shrink(cur, model=resolved_model)
                if shrunk is cur or shrunk == cur:
                    # Floor: the host can shrink no further — genuine
                    # terminal, re-raise (floor-abort). #4381 PR-4: no
                    # handoff-and-retry replaces this anymore.
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
        ``invoke_action(action_name=..., args=...)``. #4552: the name
        never lands in ``self._catalog`` (the hot-list mechanism that used
        to put catalog-action names there directly was discarded); every
        bare catalog-action call reaches here via this same #229/#3429
        salvage. ARS-only entries don't get a top-level tool slot, so
        the dispatcher would otherwise reject with ``unknown_tool``.
        When the missed name resolves through ``universal_dispatch``,
        rewrite the call as ``invoke_action(action_name=name, args=args)``
        and dispatch via the wrapper path so the user-visible behavior
        matches what the LLM intended.
        """
        name, args, raw_name = self._resolve_tool_call(tc)

        excluded = self._excluded_result(name, args)
        if excluded is not None:
            # #3455: a pre-dispatch exclude IS a routing decision (the
            # decision was "deny") — it never reaches ``_dispatch_resolved``,
            # so its own emit call there would silently drop this outcome.
            # Matches the pre-#3455 behavior (the old run_loop-local emit
            # iterated ALL tool_results, excluded ones included).
            self._emit_routing_decided(name, args, excluded, raw_name=raw_name)
            return excluded
        return await self._dispatch_resolved(name, args, raw_name=raw_name)

    def _resolve_tool_call(self, tc: dict) -> "tuple[str, dict, str]":
        """#1593: name + args + #229 salvage → the effective ``(name, args)``,
        plus (#3455) the ``raw_name`` the model actually called BEFORE any
        salvage rewrite.

        **Resolution only** (no dispatch), so the scheme's ``interpret`` runs it and
        the OS exclude-gates the result BEFORE ``execute`` — preserving the #1406/#187
        pre-dispatch order across the scheme split (byte-identical).

        #3455: ``raw_name`` is threaded through to ``_dispatch_resolved`` so
        ``routing_decided``'s ``source`` classification keeps reading "which
        surface did the MODEL use" (invoke_action / a bare name) rather than
        "what name did dispatch end up running" — the #229 salvage rewrites
        an un-advertised bare call's EFFECTIVE name/args to ``invoke_action``
        internally, but that rewrite is an OS routing mechanic, not a change
        in what the model did. Losing this distinction would silently
        reclassify every existing ``"ars_direct"``-labeled call as
        ``"invoke_action"`` the moment the emit moved to the resolved
        chokepoint — the #241 discriminator would break value, not shape."""
        from reyn.tools.universal_catalog import strip_provider_tool_namespace  # noqa: PLC0415

        # #1989: a weak model (Gemini) may echo its function-calling namespace
        # onto the call name (``default_api.invoke_action`` /
        # ``default_api.web_search``). Strip it FIRST so the catalog membership +
        # the salvage below see the plain name. Safe: reyn names are dot-free.
        raw_name = strip_provider_tool_namespace(tc["function"]["name"])
        name = raw_name
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

        if name not in self._catalog:
            name, args = self._maybe_salvage_action_direct_call(name, args)
        return name, args, raw_name

    def _excluded_result(self, name: str, args: dict) -> "dict | None":
        """#1406/#187: the **pre-dispatch** exclude gate. Returns the
        ``tool_excluded`` error result when the effective op is excluded, else None.

        The narrowing is not just an advertisement filter (#1400 / #3378
        ``apply_contextual_visibility`` hides denied tools from ``tools[]`` /
        ``self._catalog``) — the LLM can still call an excluded tool by name, which
        the #229 salvage rewrites to ``invoke_action(action_name=<excluded>)`` (or it
        is called as ``invoke_action`` directly), and ``universal_dispatch`` then
        resolves and EXECUTES it (the #187 N=3 web_search leak). Compute the
        effective resolved action — unwrap ``invoke_action`` — and reject if excluded.
        Covers all three bypass paths (native direct / salvaged / direct
        invoke_action). The ``tool_excluded`` kind + decision-enabling message lets
        the model adjust ([[deny-message-decision-enabling]]).

        #1827 S1 (live-gate): the decision now flows through the unified ∩-model
        (effective.py ``ContextualLayer``) — the single live TOOL-axis enforcement
        gate. ``never-elevate`` is the structural ``all()`` in
        ``EffectivePermission`` (a contextual deny can't be re-granted). The
        standalone ``exclude_tools`` *enforcement* membership is retired; #3378
        COMPOSES ``exclude_tools`` into ``_contextual_permission`` (see
        ``_with_exclude_tools``) so this gate and the advertisement filter read one
        source, and S2+ feed a real contextual from topology / delegate / ephemeral
        narrowing."""
        if self._contextual_permission is not None:
            # #1912: the shared contextual gate — the same check the
            # advertisement filter runs, so what is hidden and what is denied
            # cannot disagree. (This said "chat RouterLoop + op dispatch" until
            # #3513; the op-dispatch leg was an orphaned wrapper with no caller
            # and is gone. Op-dispatch coverage is #3546, unmeasured.)
            from reyn.security.permissions.effective import (
                contextual_deny_message,
                tool_contextually_denied,
            )
            # #3378: the SAME unwrap the advertisement half uses (one seam).
            effective = gate_effective_tool_name(name, args)
            if effective is not None and tool_contextually_denied(
                self._contextual_permission, effective
            ):
                # #3501: the message the MODEL reads. It used to say only "excluded
                # this session", which is why an agent that lost a capability
                # mid-session could not say what had happened or what would restore
                # it — the reason and the lift condition were nowhere on this path,
                # only in the Tool tab the operator had to open by hand. The shared
                # builder names the narrowing that actually fired.
                return {
                    "status": "error",
                    "error": {
                        "kind": "tool_excluded",
                        "message": (
                            contextual_deny_message(
                                "tool", effective, self._contextual_permission,
                            )
                            + " Do not call it again this turn (directly or via "
                            "invoke_action)."
                        ),
                    },
                }
        return None

    async def _dispatch_resolved(
        self, name: str, args: dict, *, raw_name: "str | None" = None,
    ) -> dict:
        """#1593: dispatch a resolved, exclude-cleared tool call via the OS substrate
        (DispatchContext / ``dispatch_tool`` — P5). The pure-OS dispatch
        half of the former ``_execute_tool``; the scheme's ``execute`` orchestrates
        calls to it (it never sees the DispatchContext).

        #1618 root-1: the membership/resolution gate (``tool_catalog``) is sourced
        from the scheme's DISPATCHABLE set (``self._dispatch_catalog``) — decoupled
        from the ADVERTISED ``tools=`` mirror (``self._catalog``). Default ``None`` ⇒
        ``self._catalog`` (universal / enumerate / retrieval, where dispatchable =
        advertised — byte-identical). CodeAct advertises ∅ but dispatches the full
        catalog, so its gate must key on the dispatchable set, not the empty mirror
        (the #7 "not in catalog" root).

        #3455: ``raw_name`` (optional) is the name the MODEL literally called,
        before the #229 salvage rewrite — see ``_resolve_tool_call``. This is
        the single chokepoint EVERY dispatch funnels through (native
        Execute-round calls via ``resolve()``/``dispatch()``, the direct
        ``_execute_tool`` test seam, and the CodeAct in-snippet ``tool()``
        call via ``_run_codeblock_round``'s ``_os_gate`` — the latter has no
        separate "raw" surface, so it omits ``raw_name`` and
        ``_emit_routing_decided`` falls back to ``name``), which is what
        makes it the right place for ``routing_decided`` (#3455)."""
        catalog = (
            self._dispatch_catalog
            if self._dispatch_catalog is not None
            else self._catalog
        )
        dctx = DispatchContext(
            caller_kind="router",
            caller_id=self.host.agent_name,
            chain_id=self.chain_id,
            tool_catalog=catalog,
            events=self.host.events,
        )

        result = await dispatch_tool(
            name=name,
            args=args,
            ctx=dctx,
            invoker=functools.partial(self._invoke_router_tool, name),
        )
        # FP-0056 PR-F1: tag the INVOKED IDENTITY at the common router-dispatch funnel so the
        # feedback() chokepoint canonicalizes by what was called, not result["kind"] (data a producer
        # may not set — the reyn_repo incident class). ``setdefault``: a wrapper handler (invoke_action)
        # already tagged the DEEPER resolved target inside ``data`` — that wins (extract_canonical_
        # source takes the deepest); a direct call has only this outer tag = the named tool.
        if isinstance(result, dict):
            result.setdefault("_canonical_source", name)
        self._emit_routing_decided(name, args, result, raw_name=raw_name)
        return result

    def _emit_routing_decided(
        self, name: str, args: dict, result: Any, *, raw_name: "str | None" = None,
    ) -> None:
        """#3455: emit ``routing_decided`` at the chokepoint EVERY catalog
        dispatch funnels through (``_dispatch_resolved`` — the #3429 census
        finding that ``dispatch_tool`` is called from exactly this one
        call site plus the CodeAct ``tool()`` unwrap, both of which route
        here). Structural fix, not a widened gate: this replaces the prior
        ``run_loop``-local emit that lived inside ``if _univ_enabled:`` —
        a guard keyed on which ENTRY SURFACE the model used (the
        ``invoke_action`` wrapper), not on whether routing actually
        happened. The opt-out configuration (an operator setting
        ``action_retrieval.universal_wrappers_enabled: false`` in
        reyn.yaml → flat bare-name ``tools=``, the pre-PR-3b-iv shape)
        never advertises ``invoke_action`` at all, so that guard silently
        zeroed out ``routing_decided`` for that path even though every one
        of its tool calls dispatches a catalog action right here.

        ``surface`` (``raw_name`` if given, else ``name``) is what the model
        actually called, BEFORE the #229 salvage rewrite. This matters
        because the salvage rewrites an un-advertised bare call's EFFECTIVE
        ``(name, args)`` to ``invoke_action`` internally — a routing
        mechanic, not something the model did — so classifying off the
        POST-salvage ``name`` would relabel every existing
        ``"ars_direct"`` call as ``"invoke_action"``, silently changing the
        #241 discriminator's meaning the moment the emit moved here.

        Source classification:
          - ``"invoke_action"``: the model called the universal wrapper;
            the real action name is nested in ``args["action_name"]``.
          - ``"ars_direct"``: a bare catalog-action name — always reached
            via the #229/#3429 salvage. #4552: this used to split into
            ``"hot_list_alias"`` vs ``"ars_direct"`` depending on whether
            ``surface`` was in ``self._catalog`` (= actually advertised in
            ``tools=`` by the hot-list mechanism, #241); with hot-list
            discarded nothing ever puts a bare catalog-action name in
            ``self._catalog``, so that branch is now unreachable by
            construction and the ternary collapses to this one constant.
          - anything else (e.g. an async peer tool): not a catalog
            action — no routing decision to record.
        """
        surface = raw_name if raw_name is not None else name
        if surface == "invoke_action":
            action_name = (
                args.get("action_name", "") if isinstance(args, dict) else ""
            )
            source = "invoke_action"
        elif surface in _KNOWN_ACTION_NAMES:
            action_name = surface
            source = "ars_direct"
        else:
            return  # non-catalog tool — no routing decision to record
        if not action_name:
            return
        # #3450: derive from the SAME source as the LLM-visible envelope —
        # dispatch_tool's own outer ``status`` field, which now promotes any
        # handler-declared error before ``result`` is ever built (see
        # dispatcher.py), so this check is trustworthy for every catalog
        # dispatch outcome.
        outcome = "error" if (
            isinstance(result, dict) and result.get("status") == "error"
        ) else "success"
        self.host.events.emit(
            "routing_decided",
            action_name=action_name,
            source=source,
            outcome=outcome,
            chain_id=self.chain_id,
        )
        self._routing_decided_this_turn = True

    # ── #1593 SchemeOps adapter ─────────────────────────────────────────────
    # The router IS the ``SchemeOps`` a *delegating* scheme calls. PR-1's
    # UniversalCategoryScheme delegates here, so the seam is byte-identical (no
    # universal-category logic is physically relocated). PR-2/3 schemes implement
    # their own logic instead of delegating.

    def present(self, available, layer_ctx):
        """SchemeOps.present: today's universal-category presentation —
        ``build_tools`` with the catalog wrappers. Carries the ``tools=`` payload
        only; the scheme layer owns the tool-use SP (the universal-category
        scheme turns this payload into an ``Exposure`` and lets the ``tool_calls``
        encoder produce both channels)."""
        from reyn.tools.scheme import AdvertisedTools, Presentation

        univ = layer_ctx["univ_enabled"]
        search_visible = layer_ctx["search_visible"]
        tools = build_tools(
            self.host.list_available_agents(),
            file_permissions=self.host.get_file_permissions(),
            mcp_servers=self.host.get_mcp_servers(),
            web_fetch_allowed=self.host.get_web_fetch_allowed(),
            universal_wrappers_enabled=univ,
            search_actions_visible=search_visible,
            compact_visible=layer_ctx["ctx_signal_present"],
        )
        return Presentation(
            # The router's own presentation is always a ``tool_calls`` one, so the
            # channel EXISTS here even when ``build_tools`` returns nothing (#3421).
            tools_channel=AdvertisedTools(entries=tools),
        )

    def base_tools(self, available, layer_ctx) -> list[dict]:
        """SchemeOps.base_tools (#1593 PR-2): the prior-shape base tools —
        ``build_tools`` with the universal wrappers OFF. The common base a
        self-contained scheme starts from (enumerate-all adds ``catalog_entries``
        on top instead of the wrappers)."""
        return build_tools(
            self.host.list_available_agents(),
            file_permissions=self.host.get_file_permissions(),
            mcp_servers=self.host.get_mcp_servers(),
            web_fetch_allowed=self.host.get_web_fetch_allowed(),
            universal_wrappers_enabled=False,
            search_actions_visible=False,
            compact_visible=layer_ctx["ctx_signal_present"],
        )

    async def catalog_entries(self) -> list[dict]:
        """SchemeOps.catalog_entries (#1593 PR-2): every usable catalog action as
        a flat callable tool schema (qualified ``<category>__<entry>`` name).

        Consumes sandbox_2's ``universal_catalog.catalog_entries(ctx)`` substrate
        (#1598: the all-entries→schemas projection, single-source with
        list/describe via ``_enumerate_category`` + ``_describe_one``, #1455
        invariant; name-sorted; ``parameters`` never None). Async because the
        complete caller-state is built async (caveat-1, #3026: a ``ToolContext``
        WITHOUT ``router_state`` no longer drops whole categories — none are
        resource-backed — but it does lose the AVAILABILITY gates
        (``excluded_categories``, ``exec``'s sandbox backend), so the result is
        not the "usable this session" set; the rag manifest fetch is the genuine
        await). Maps each generic entry →
        the OpenAI ``{type: function, function: {name, description, parameters}}``
        shape so the flat tools= is uniform with ``base_tools``."""
        from reyn.tools import universal_catalog
        from reyn.tools.types import ToolContext

        rs = await self._build_router_caller_state()  # caveat-1: populated router_state
        tool_ctx = ToolContext(
            events=self.host.events,
            permission_resolver=getattr(self.host, "permission_resolver", None),
            workspace=getattr(self.host, "workspace", None),
            caller_kind="router",
            router_state=rs,
            # #1673: thread the config-aware resolver so a tool handler that spawns
            # a sub-run hands the spawned OpContext a real resolver
            # instead of resolver=None (→ literal "standard" → litellm BadRequestError).
            resolver=getattr(self.host, "resolver", None),
            hot_reloader=getattr(self.host, "hot_reloader", None),  # #2073 S3
            state_log=getattr(self.host, "state_log", None),  # #2248 PR-A2 (config emit)
            agent_name=getattr(self.host, "agent_name", None),  # #2088: scope-aware hooks_add
            session_state_dir=getattr(self.host, "session_state_dir", None),  # #4215①
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "description": entry["description"],
                    "parameters": entry["parameters"],
                },
            }
            for entry in universal_catalog.catalog_entries(tool_ctx)
        ]

    async def search_actions(self, query: str, *, top_k: int = 10) -> list[str]:
        """SchemeOps.search_actions (#1593 PR-4): rank usable actions by semantic
        match to ``query`` → matched qualified names. Reuses the FP-0034
        ``ActionEmbeddingIndex`` (the same substrate the ``search_actions`` tool uses);
        ``query`` awaits the embedding of the dynamic query (the reason presentation
        is async). Returns ``[]`` when the index / provider is unavailable (degrade).

        FP-0066 P2d (#3247 firm §5/§6): before serving, awaits
        ``IndexCoordinator.search_await`` — a cheap manifest-read no-op in
        the steady state (source already ``clean``), or a heal-await if a
        prior sync-in-op build left the source ``dirty``/mid-``building``
        (the "best-effort search is a bug" completeness guarantee). Wraps
        the search in ``semantic_search_started``/``_complete`` audit-events
        (results count) via the shared ``emit_wrapped_semantic_search``
        helper (P3-helper, #3247 firm §6) — the unification of this wrap
        with the ``universal_catalog._handle_search_actions`` copy.
        """
        from reyn.data.index.coordinator import emit_wrapped_semantic_search

        index = self.host.get_action_embedding_index()
        provider = self.host.get_embedding_provider()
        model_class = self.host.get_embedding_model_class()
        if index is None or provider is None:
            return []
        source_id = getattr(index, "source_name", None) or "actions"
        results: list[dict[str, Any]] = []
        try:
            coordinator = self._get_index_coordinator()
            # FP-0057 #2856 Part A: idx.query() routes through the shared
            # `embed` op (execute_op) instead of calling ``provider`` directly
            # — needs an OpContext, not the provider instance itself.
            op_ctx = self.host.make_router_op_context()
            results = await emit_wrapped_semantic_search(
                events=self.host.events,
                coordinator=coordinator,
                source_id=source_id,
                index=index,
                query=query,
                op_ctx=op_ctx,
                model_class=model_class,
                top_k=top_k,
            )
        except Exception as e:  # noqa: BLE001 — search is best-effort presentation aid
            import logging
            logging.getLogger(__name__).warning("search_actions failed: %s", e)
        return [r["action_name"] for r in results if r.get("action_name")]

    def resolve(self, llm_response, tool_catalog: dict) -> list[dict]:
        """SchemeOps.resolve: dedupe + #229 salvage → actions carrying the original
        ``tc`` + the resolved effective ``name``/``args``. The OS exclude-gates these
        (on the effective name) before dispatch."""
        deduped = self._dedupe_tool_calls_round(llm_response.tool_calls)
        actions: list[dict] = []
        for tc in deduped:
            name, args, raw_name = self._resolve_tool_call(tc)
            actions.append({"tc": tc, "name": name, "args": args, "raw_name": raw_name})
        return actions

    async def dispatch(self, actions: list[dict]) -> list[dict]:
        """SchemeOps.dispatch: run the resolved (exclude-cleared) actions SERIALLY in
        declaration order via the OS dispatch substrate.

        #2344 (owner design decision): the chat axis must NOT unilaterally parallelize
        stacked tool_calls. The LLM API returns tool_calls as an ordered list but does
        not guarantee they are independent/parallel-safe, and there is no
        workspace-write lock — a concurrent gather races on order-dependent calls (e.g.
        write-a-file then read-it-back). The faithful default is serial in declaration
        order, which also matches the phase axis (``control_ir_executor`` is already a
        serial ``for op in ops: await``). Ordering (``tool_calls[i]`` ↔
        ``tool_results[i]``) is unchanged — the append preserves declaration order the
        same way ``gather`` did. Error semantics are unchanged: ``dispatch_tool``
        normalizes every exception to ``{status: error}`` and never raises, so serial
        does not short-circuit (every call still runs). The only thing given up is
        parallel latency for genuinely-independent calls."""
        results: list[dict] = []
        for a in actions:
            results.append(await self._dispatch_resolved(
                a["name"], a["args"], raw_name=a.get("raw_name"),
            ))
        # FP-0050/#1822 S2: tag untrusted-source results by the EFFECTIVE resolved
        # name (``a["name"]``). feedback() iterates the raw tool_calls whose name
        # may be the ``invoke_action`` wrapper, so classifying there would miss
        # wrapped MCP/web/memory calls; tagging here (post-resolve) is
        # scheme-agnostic. The fence at feedback() gates on this tag; scan-all
        # runs regardless of the tag.
        from reyn.tools import get_default_registry
        _reg = get_default_registry()
        for a, r in zip(actions, results):
            if isinstance(r, dict):
                _td = _reg.lookup(a["name"])
                if _td is not None and getattr(_td, "returns_external_content", False):
                    r["_external_source"] = True
        return results

    def feedback(self, result: "ExecutionResult") -> list[dict]:
        """SchemeOps.feedback (#1608): build the **appendable message sequence** for
        an Execute round — the assistant tool-call turn + the per-result
        ``{role:tool, tool_call_id, content}`` messages (+ media follow-ups) — and
        persist each to chat history. This is the OS loop's former inline zip,
        relocated **byte-identically**: the delegating schemes (universal /
        enumerate-all / retrieval) return this, and the OS loop only *appends*, so it
        no longer knows the JSON tool_call/result correlation shape (P7). Relies on
        ``result.tool_calls[i]`` aligning with ``result.tool_results[i]`` (un-reordered
        — the #1406/#187 excluded-in-place result keeps its index)."""
        host = self.host
        _append_entry = getattr(host, "append_history_entry", None)
        out: list[dict] = []
        assistant_content = result.assistant_content
        out.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": result.tool_calls,
        })
        if _append_entry is not None:
            _append_entry(
                role="assistant",
                content=assistant_content,
                meta={"chain_id": self.chain_id, "source": "router_tool_turn"},
                tool_calls=result.tool_calls,
            )
        # #2425 案B: every tool result is normalised at this chokepoint into the canonical
        # {text, attachments, meta} shape and rendered as the frontmatter+text LLM-visible format
        # (no JSON envelope). FP-0056 PR-F1: ``to_canonical`` dispatches on the INVOKED IDENTITY
        # (``source=`` — the effective tool name dispatch() tagged), resolving the declaration born at
        # the op/tool registration seam; a genuinely unregistered source falls back to a whole-dict
        # ``structured`` attachment. The format is INDEPENDENT of the media store — it applies even
        # when ``media_store is None``; only the size-gated offloading needs a store.
        from reyn.core.offload.canonical import (
            CANONICAL_DEGRADED_EVENT,
            CANONICAL_FALLBACK_EVENT,
            canonical_degraded_reason,
            canonical_fallback_reason,
            extract_canonical_source,
            to_canonical,
            unwrap_dispatch_envelope,
        )
        from reyn.core.offload.seam import build_offload_body, render_tool_result
        from reyn.runtime.chat_message import (
            SKILL_SOURCE_PATH_META_KEY,
            TOKEN_MAP_META_KEY,
            TOOL_ERROR_KIND_META_KEY,
            TOOL_ERROR_MESSAGE_META_KEY,
            TOOL_STATUS_ERROR,
            TOOL_STATUS_META_KEY,
        )
        for tc, r in zip(result.tool_calls, result.tool_results):
            # B41-NF-W7-1: _post_text → appended outside the body.
            post_text: str | None = None
            if isinstance(r, dict) and isinstance(r.get("_post_text"), str):
                post_text = r["_post_text"]
                r = {k: v for k, v in r.items() if k != "_post_text"}
            # Issue #362: extract media blocks BEFORE rendering (surfaced as a
            # multimodal follow-up user message, not base64 in the tool text).
            media_blocks: list[dict] = []
            if isinstance(r, dict) and isinstance(r.get("media_blocks"), list):
                media_blocks = list(r["media_blocks"])
                r = {k: v for k, v in r.items() if k != "media_blocks"}
            # FP-0050/#1822 S2: untrusted-source tag set by dispatch() (effective
            # name). Pop before rendering so it never reaches the LLM body.
            external_source = False
            if isinstance(r, dict) and r.get("_external_source"):
                external_source = True
                r = {k: v for k, v in r.items() if k != "_external_source"}
            # FP-0056 PR-F1: split the invoked-identity tag(s) off the (possibly envelope-wrapped)
            # result — the deepest tag (a wrapper handler's resolved target) wins over the outer
            # dispatch-loop tag. ``source`` feeds to_canonical so canonicalization resolves by what was
            # called, not result["kind"]; the cleaned result carries no tag into rendering.
            canonical_source: str | None = None
            if isinstance(r, dict):
                canonical_source, r = extract_canonical_source(r)

            _media_store = getattr(host, "media_store", None)
            _save_fn = _media_store.save_tool_result if _media_store is not None else None
            _cap = getattr(host, "cap_tool_result", None)

            # Error path (plain string, never JSON): a dispatch-envelope error carries
            # ``error.kind``/``.message`` (``permission_denied`` vs ``not_found`` imply different
            # recovery); an MCP ``isError`` carries its description in the content text.
            # #73 co-vet (Option C): the success/failure classification is KNOWN right here —
            # ``r``/``canonical`` already discriminate error from success — so it is captured into
            # typed ``_tool_error_*`` locals and stamped onto the persisted ``_tool_meta`` below
            # (NEVER re-derived downstream by sniffing the rendered string; restore.py reads this
            # typed field directly, matching reyn's typed-over-form-sniffed convention).
            #
            # #2649: the dispatch-error shape check runs on the UNWRAPPED envelope, not the raw
            # ``r`` — a tool-registry HANDLER that itself returns the standard
            # ``{status:error, error:{kind,message}}`` shape (e.g. ``run_pipeline`` failed/cancelled)
            # is a normal (non-exception) return, so ``dispatch_tool`` wraps it one layer deeper
            # (``{status:ok, data:<handler's own envelope>}``) before it ever reaches here — the
            # raw-``r`` check only ever matched dispatch_tool's OWN top-level errors (permission_denied
            # / unknown_tool / invalid_args / exception) and pre-dispatch synthetic ones
            # (tool_excluded), never a handler's nested one. ``unwrap_dispatch_envelope`` is a no-op
            # on those bare shapes (no ``data`` key to peel), so moving the check here is
            # behavior-preserving for them and additionally recognizes the wrapped case.
            scan_target: str
            _tool_error_kind: "str | None" = None
            _tool_error_message: "str | None" = None
            # #3629: an ALTERNATE persisted-content string + its meta, set ONLY
            # when the canonical mapper (`load_skill_to_canonical`) supplied
            # ``history_text``/``history_meta`` — every other mapper leaves this
            # ``None``, so persistence is byte-identical to ``content_str`` for
            # them, unchanged. See ``CanonicalToolResult.history_text``'s
            # docstring (canonical.py) for why this exists.
            _persist_content_str: "str | None" = None
            _history_meta_extra: dict = {}
            # Unwrap dispatch-envelope layers ({status, data} with nothing else) until the op
            # result (the dict carrying ``kind``) is reached. One layer for op_runtime ops
            # (bare {kind:…} wrapped once by dispatch_tool); two for tool-registry handlers whose
            # own return is already an envelope (e.g. run_pipeline → wrapped again). Shared with
            # the pipeline `tool:` step ctx path (#2425 PR-2, executor.py::_run_tool_step).
            _inner = unwrap_dispatch_envelope(r)
            if (isinstance(_inner, dict) and _inner.get("status") == "error"
                    and isinstance(_inner.get("error"), dict)):
                _err = _inner["error"]
                _tool_error_kind = str(_err.get("kind") or "error")
                _tool_error_message = str(_err.get("message") or "")
                content_str = f"Error ({_tool_error_kind}): {_tool_error_message}"
                scan_target = content_str
            elif not isinstance(_inner, dict):
                # Non-dict result (rare) — render its value as plain text, losslessly.
                content_str = _inner if isinstance(_inner, str) else json.dumps(_inner, default=str)
                scan_target = content_str
            else:
                canonical = to_canonical(_inner, source=canonical_source)
                if (canonical.get("meta") or {}).get("isError"):
                    text_full = canonical.get("text", "") or ""
                    scan_target = f"Error: {text_full}"
                    text = _cap(text_full) if _cap is not None else text_full
                    content_str = f"Error: {text}"
                    _tool_error_message = text_full
                else:
                    # #3580: the two structured sizes are operator-tunable. A host
                    # without them (legacy/test double) yields ``None``, and the
                    # callee keeps its shipped default — same fall-closed idiom as
                    # the flag below.
                    _si = getattr(host, "offload_structured_inline_max_chars", None)
                    _sp = getattr(host, "offload_structured_preview_chars", None)
                    _structured_kw = {}
                    if _si is not None:
                        _structured_kw["structured_inline_max_chars"] = _si
                    if _sp is not None:
                        _structured_kw["structured_preview_chars"] = _sp
                    frontmatter, text, built_media, content_type = build_offload_body(
                        canonical, save_fn=_save_fn,
                        # opt-in flip: a host with no ``offload_enabled`` attribute
                        # (legacy/test double) falls closed — offload stays off.
                        enabled=getattr(host, "offload_enabled", False),
                        **_structured_kw,
                    )
                    # FP-0056 PR-F2: a VISIBLE fallback (a #2681 CANONICAL_TODO producer, a
                    # genuinely-unregistered source, or a STRUCTURED_PASSTHROUGH whose whole-dict
                    # blob exceeded the offload gate) emits a P6 audit event naming the source —
                    # degrade-with-audit, never silently (the dogfood incident was one silent
                    # fallback). Source id only; NEVER the result body.
                    # FP-0056 v2 piece #3: passing ``canonical`` lets the classifier also fire on a
                    # mapped producer whose inner discriminator missed (reason
                    # ``"discriminator_miss"`` — M3, #2695), riding the same fallback event.
                    _fallback_reason = canonical_fallback_reason(
                        canonical_source,
                        structured_offloaded=frontmatter.get("structured") == "offloaded",
                        canonical=canonical,
                    )
                    if _fallback_reason is not None:
                        _events = getattr(host, "events", None)
                        if _events is not None:
                            _events.emit(
                                CANONICAL_FALLBACK_EVENT,
                                source=canonical_source,
                                reason=_fallback_reason,
                            )
                    # FP-0056 v2 piece #2: a MAPPED producer that canonicalized to an empty view
                    # (no text + no attachments) on a non-error result silently lost its content
                    # (mode M2) — fire ``canonical_degraded`` (audit event + warn log,
                    # degrade-with-audit). A legit-empty success renders an explicit marker in its
                    # mapper and does not reach here. Source id only; NEVER the result body.
                    _degraded_reason = canonical_degraded_reason(_inner, canonical)
                    if _degraded_reason is not None:
                        import logging
                        logging.getLogger(__name__).warning(
                            "canonical_degraded: source=%s reason=%s (a non-error tool result "
                            "canonicalized to an empty view — no text, no attachments)",
                            canonical_source, _degraded_reason,
                        )
                        _events = getattr(host, "events", None)
                        if _events is not None:
                            _events.emit(
                                CANONICAL_DEGRADED_EVENT,
                                source=canonical_source,
                                reason=_degraded_reason,
                            )
                    if built_media:
                        # media lifted from INSIDE data (the top-level strip missed it)
                        media_blocks.extend(built_media)
                    # FP-0050/#1822 S2: scan the FULL body BEFORE the cap truncates it, so
                    # injection cannot hide past the size cap.
                    scan_target = render_tool_result(frontmatter, text)
                    if _cap is not None:
                        # #2663: thread the canonical's renderer-only content_type through to the
                        # text offload store as its mime_type — NEVER into frontmatter (built
                        # above, already sealed) — so a later present(data_ref=<this ref>) can
                        # recover it from the stored ref's file extension.
                        text = _cap(text, content_type=content_type)
                    content_str = render_tool_result(frontmatter, text)
                    # #3629: `load_skill_to_canonical` is the one mapper that sets
                    # `history_text` — the SAME frontmatter, un-capped (a skill body is
                    # already read-bounded upstream by `load_skill`'s own
                    # `control_ir_inline_cap`, so a second offload-store cap pass here
                    # would risk a second, independent offload ref for content that
                    # never needs one in practice). `history_meta` carries no LLM-visible
                    # text — it becomes ChatMessage.meta keys only, never frontmatter.
                    _history_text = canonical.get("history_text")
                    if _history_text is not None:
                        _persist_content_str = render_tool_result(frontmatter, _history_text)
                        _history_meta_extra = dict(canonical.get("history_meta") or {})
            if post_text:
                content_str = f"{content_str}\n\n---\n{post_text}"
                if _persist_content_str is not None:
                    _persist_content_str = f"{_persist_content_str}\n\n---\n{post_text}"
            # FP-0050/#1822 S2: scan-all on the FULL content BEFORE cap truncates
            # (so injection can't hide past the size cap). Detection completeness.
            _scan = getattr(host, "scan_tool_result", None)
            if _scan is not None:
                _scan(scan_target)
            # FP-0050/#1822 S2: fence untrusted-source content AFTER cap (so
            # truncation can't sever the end marker). Trusted-internal = scan-only.
            if external_source:
                _fence = getattr(host, "fence_tool_result", None)
                if _fence is not None:
                    content_str = _fence(content_str)
                    if _persist_content_str is not None:
                        _persist_content_str = _fence(_persist_content_str)
            out.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": content_str,
            })
            # E-full PR-E (#383): persist the tool response (capped form).
            # #1909: carry the ``external_source`` taint (already extracted above,
            # FP-0050/#1822 S2) into the persisted history-entry meta — the SAME
            # convention the S4 external-peer-answer seam uses
            # (intervention_handler.py, ``UNTRUSTED_META_KEY`` in
            # capability_profile.py). ``_effective_contextual_for_turn`` live-scans
            # ``self.history`` meta every turn (not cached), so once this lands the
            # NEXT dispatch is already capability-narrowed — no separate re-narrowing
            # step is needed intra-turn.
            _tool_meta: "dict[str, object]" = {
                "chain_id": self.chain_id, "source": "router_tool_turn",
            }
            if external_source:
                _tool_meta["external_source"] = True
                # #4381 PR-2 stage ③: the SAME update point as the meta stamp
                # above — not a second, independently-set signal. The meta
                # stamp only becomes visible to `_effective_contextual_for_turn`
                # once THIS history entry is persisted (`_append_entry` below);
                # this in-flight flag closes the gap for the CURRENT turn's
                # own remaining iterations, which see it immediately. Getattr-
                # guarded: a host that never wired the callback (most test
                # construction, phase hosts) behaves as a no-op, same
                # convention as every other optional host method.
                _mark_untrusted = getattr(host, "mark_untrusted_in_flight", None)
                if _mark_untrusted is not None:
                    _mark_untrusted()
            # #73 (Option C, co-vet-directed): stamp the ALREADY-KNOWN success/failure
            # classification as a typed field — never re-derived downstream from the
            # rendered ``content_str`` (a display string is not a stable data contract;
            # a legitimate success payload can itself start with the word "Error").
            if _tool_error_message is not None:
                _tool_meta[TOOL_STATUS_META_KEY] = TOOL_STATUS_ERROR
                _tool_meta[TOOL_ERROR_MESSAGE_META_KEY] = _tool_error_message
                if _tool_error_kind is not None:
                    _tool_meta[TOOL_ERROR_KIND_META_KEY] = _tool_error_kind
            # #3629: persist the location-tokens-literal variant + its token
            # map/source-path (never the wire ``content_str`` shown above) when
            # the canonical mapper supplied one — see ``_persist_content_str``'s
            # comment above and ``CanonicalToolResult.history_text``'s docstring.
            if "token_map" in _history_meta_extra:
                _tool_meta[TOKEN_MAP_META_KEY] = _history_meta_extra["token_map"]
            if "skill_source_path" in _history_meta_extra:
                _tool_meta[SKILL_SOURCE_PATH_META_KEY] = _history_meta_extra["skill_source_path"]
            if _append_entry is not None:
                _append_entry(
                    role="tool",
                    content=(
                        _persist_content_str if _persist_content_str is not None
                        else content_str
                    ),
                    meta=_tool_meta,
                    tool_call_id=tc["id"],
                    name=tc.get("function", {}).get("name"),
                )
            if media_blocks:
                # Issue #383 PR-C / #272: bounded media follow-up message.
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
                    out.append(followup)
        return out

    def _enforce_tool_call_cap(self, result) -> "tuple[int, int] | None":
        """#1666: bound the per-turn ``tool_calls`` count (cost-bound, OS-level).

        A degenerate (weak-model, long-context) completion can emit thousands of
        ``tool_calls`` — observed 3451 in one SWE-bench completion — each costing a
        tool-result message + token inflation. This caps the count at
        ``self._max_tool_calls_per_turn`` (``0`` = unlimited): the overflow calls are
        TRUNCATED off ``result.tool_calls`` **in place, before ``interpret``**, so
        every downstream branch (Execute / RePresent) and the assistant-message ↔
        tool-result alignment inherit the bound from a single choke point (only the
        kept calls are interpreted, executed, and appended).

        Scheme-agnostic (operates on the generic ``result.tool_calls`` shape, no
        domain-specific strings — P7-clean). Emits the P6 ``tool_call_cap_exceeded``
        event recording the **original attempted count** so history is bounded to
        ``kept`` while the true magnitude survives in the audit log.

        Returns ``(attempted, kept)`` when the cap fired (so the caller appends the
        re-grounding notice), else ``None``.
        """
        cap = self._max_tool_calls_per_turn
        if cap <= 0:
            return None
        tcs = getattr(result, "tool_calls", None) or []
        attempted = len(tcs)
        if attempted <= cap:
            return None
        # Truncate in place — the assistant message + interpret + execute all read
        # this same list, so 50 calls ↔ 50 results stays aligned by construction.
        result.tool_calls = tcs[:cap]
        try:
            self.host.events.emit(
                "tool_call_cap_exceeded",
                chain_id=self.chain_id,
                attempted=attempted,
                kept=cap,
            )
        except Exception:  # noqa: BLE001 — never let an audit emit break the loop
            pass
        return attempted, cap

    def _tool_call_cap_notice(self, attempted: int, kept: int) -> dict:
        """#1666: the single decision-enabling notice appended after a capped
        round's results so the model re-grounds (deny-message-is-decision-enabling:
        states what happened + what to do, with the true attempted count).

        reyn.prompt.loop_control (SP prompt-package, Phase 3 §K) owns the
        literal template text; this method is now a thin call-through kept
        for source/behaviour compatibility with existing callers."""
        return _tool_call_cap_notice_text(attempted, kept)

    async def _run_execute_round(self, interp) -> "tuple[list[dict], list[dict]]":
        """The ``Execute`` arm — **byte-identical** to the former
        ``_run_scheme_tool_round`` body. The OS exclude-gate produces the same
        ``tool_excluded`` error result **in place** (not a drop), so order +
        ``tool_call_id`` alignment are preserved across the dispatch."""
        from reyn.tools.scheme import ExecContext, Execute

        actions = interp.actions
        tool_calls = [a["tc"] for a in actions]

        results: list = [None] * len(actions)
        to_dispatch: list[tuple[int, dict]] = []
        for i, a in enumerate(actions):
            ex = self._excluded_result(a["name"], a["args"])
            if ex is not None:
                # #3455: the exclude gate IS a routing decision (outcome
                # "error") — this action never reaches ``dispatch()`` /
                # ``_dispatch_resolved``, so its emit call there would never
                # see it. Mirrors the pre-#3455 behavior (the old run_loop
                # emit iterated every tool_result, excluded ones included).
                self._emit_routing_decided(
                    a["name"], a["args"], ex, raw_name=a.get("raw_name"),
                )
                results[i] = ex
            else:
                to_dispatch.append((i, a))

        exec_res = await self._scheme.execute(
            Execute(actions=[a for _, a in to_dispatch]), ExecContext(), ops=self,
        )
        for (i, _), r in zip(to_dispatch, exec_res.tool_results):
            results[i] = r
        # #1608: return the RAW (tool_calls, results) — the 8 lifecycle/audit sites
        # (async / spawn-ack / plan / error / routing) consume the pair, and the OS
        # Execute branch hands it to ``format_feedback`` for the message build. (The
        # former format_feedback call here was a no-op passthrough.)
        return tool_calls, results

    async def _run_codeblock_round(self, interp) -> "list[dict]":
        """The ``CodeBlock`` arm body — run the CodeAct snippet via the scheme's
        ``execute`` under the OS per-call gate + sandbox, and return the scheme's
        ``format_feedback`` message(s) for the loop to append (design (a)).

        ``_os_gate`` is the SAME gate the Execute path uses, per in-code ``tool()``
        call: ``_excluded_result`` (exclude, pre-dispatch, resolved effective name) →
        ``_dispatch_resolved`` (``dispatch_tool`` → permission_resolver, P5). The
        snippet runs in the sandboxed subprocess (``CodeActRunner`` via
        ``exec_ctx.sandbox``, fail-closed); the scheme orchestrates, the OS gates."""
        from reyn.security.sandbox import get_default_backend  # noqa: PLC0415
        from reyn.tools.scheme import ExecContext  # noqa: PLC0415

        async def _os_gate(name: str, args: dict) -> dict:
            excluded = self._excluded_result(name, args)
            if excluded is not None:
                # #3455: CodeAct's in-snippet ``tool()`` calls previously had
                # NO routing_decided coverage at all (the old emit only ever
                # iterated the Execute arm's tool_calls/tool_results). Now
                # covered symmetrically with the exclude branches above:
                # the exclude gate is itself a routing decision.
                self._emit_routing_decided(name, args, excluded)
                return excluded
            return await self._dispatch_resolved(name, args)

        # CodeAct-safe default policy (operator-overridable in S4 via the host's
        # configured sandbox policy); the backend auto-selects per platform and the
        # runner is fail-closed when no real sandbox is available.
        sandbox = get_default_backend(getattr(self.host, "sandbox_config", None))
        exec_ctx = ExecContext(
            tool_catalog=self._catalog,
            sandbox=sandbox,
            extra={
                "dispatch": _os_gate,
                # #1658: the DISPATCHABLE catalog (the gate's membership map — the same
                # names `_os_gate` accepts). CodeAct builds the {identifier:
                # action_name} direct-function stub map from this; self._catalog (the
                # ADVERTISED payload) is empty for CodeAct, so the dispatchable map is
                # the right source. Generic catalog (action names), not scheme vocab (P7).
                "dispatchable_catalog": self._dispatch_catalog or self._catalog,
                "sandbox_policy": getattr(self.host, "default_sandbox_policy", None)
                # #3901 PR-B ④: deny_subprocess=True (renamed, inverted sense)
                # is the same "no spawning" floor as the prior
                # allow_subprocess=False. env_deny_names is omitted (empty
                # default, owner ruling B) — PATH passes by default now,
                # where the old allow-list had to name it explicitly.
                or {"network": False, "deny_subprocess": True},
                # #4166: the SAME per-turn cancel_event the non-CodeAct
                # sandboxed_exec op already races via ctx.cancel_event
                # (make_router_op_context() is the public factory that
                # builds that same OpContext — reusing it here rather than
                # adding a second cancel_event holder on the adapter).
                # getattr-guarded like sandbox_config above: a phase/test
                # host that doesn't implement it degrades to cancel_event=
                # None (byte-identical — the pre-#4166 behaviour).
                "cancel_event": (
                    self.host.make_router_op_context().cancel_event
                    if callable(getattr(self.host, "make_router_op_context", None))
                    else None
                ),
            },
        )
        exec_res = await self._scheme.execute(interp, exec_ctx, ops=self)
        return self._scheme.format_feedback(exec_res, ops=self)

    def _maybe_salvage_action_direct_call(
        self, name: str, args: dict,
    ) -> tuple[str, dict]:
        """Issue #229: rewrite an ARS-only direct call into ``invoke_action``.

        Triggered when the LLM called a catalog action by name as a direct
        function but the name is not in ``self._catalog`` — i.e. it was never
        advertised as a row, only named in the ``invoke_action`` description's
        ARS block. Returns ``(name, args)`` unchanged when there is nothing to
        salvage; the ordinary ``unknown_tool`` path then surfaces the error.

        **Why it survives #3429.** Its input is not the qualified spelling. The
        TRIGGER used to be ``"__" in name``, which was a proxy for "this looks
        like a catalog action", and with one name per action the proxy and the
        question became the same test — asked directly here, against the
        membership table. The case itself is untouched: a scheme that advertises
        only the wrappers still teaches action names through the ARS block, and
        a model still calls them directly instead of wrapping them. Removing
        this would reopen #229 for every such name.

        **What #3429 DID remove is #3461's first arm.** #3461 added "if the
        alias's bare target is itself dispatchable, salvage to THAT instead of
        to ``invoke_action``" — because a qualified call whose bare equivalent
        was advertised would otherwise dead-end as ``unknown_tool:
        invoke_action`` under a scheme that does not advertise the wrapper. With
        one name, "the alias" and "its bare target" are the same string, so that
        arm degenerates into "return the name unchanged". It is kept in that
        honest form — an early return, not a rewrite — because the CONDITION it
        tested is still load-bearing: a name absent from the advertised catalog
        but present in the DISPATCHABLE one (CodeAct advertises nothing and
        dispatches everything) must reach the OS gate under its own name rather
        than be wrapped.

        Audit event ``direct_alias_call_salvaged`` records the rewrite
        so we can count how often this fires in dogfood and inform
        whether the ARS block wording fix (= ``β`` in #229) reduces
        the rate over time.
        """
        try:
            from reyn.tools.universal_dispatch import is_known_action
        except Exception:  # noqa: BLE001
            return name, args
        try:
            if not is_known_action(name):
                return name, args
        except Exception:  # noqa: BLE001 — never crash the dispatch on a salvage attempt
            return name, args
        # #3458/#3461: an action the executor CAN dispatch needs no wrapper hop.
        # ``invoke_action`` is advertised by only some presentations, so
        # rewriting to it unconditionally dead-ends as ``unknown_tool`` under a
        # scheme without the wrapper.
        _dispatchable = self._dispatch_catalog or self._catalog
        if name in _dispatchable:
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
    # #2123: DERIVED from the per-tool ``router_dispatched`` flag (single SoT), not a
    # hand-maintained frozenset. A tool routing through the unified registry dispatch
    # path (``_invoke_via_registry``) sets ``router_dispatched=True`` on its
    # ToolDefinition; this set is computed from those flags at class-definition time.
    # Kills the 3-place-wiring drift (#2120 advertise-miss / #2122 dispatch-miss /
    # read_tool_result advertised-but-unhandled): a new router-only tool is dispatch-
    # wired by one flag, and the cross-seam guard asserts every ADVERTISED bare router
    # tool carries it. Per-tool rationale now lives on each ToolDefinition.
    REGISTRY_DISPATCH_TOOLS: "frozenset[str]" = _derive_registry_dispatch_tools()

    def _get_index_coordinator(self) -> Any:
        """Return the per-workspace ``IndexCoordinator`` singleton (FP-0066
        P2b, #3247) used to orchestrate the action-catalog build.

        Prefers a host-provided coordinator (``host.get_index_coordinator``,
        a seam for tests/hosts that want to inject one — mirrors the
        ``get_action_embedding_index`` getattr-fallback pattern used
        throughout this method); falls back to the module-level
        ``get_index_coordinator(workspace_root)`` singleton, keyed by
        ``host.workspace_root`` (or ``Path.cwd()`` when the host does not
        expose one — mirrors ``ActionEmbeddingIndex.__init__``'s own
        default).
        """
        from pathlib import Path as _Path

        _coord_getter = getattr(self.host, "get_index_coordinator", None)
        if _coord_getter is not None:
            _coord = _coord_getter()
            if _coord is not None:
                return _coord
        from reyn.data.index.coordinator import get_index_coordinator

        workspace_root = getattr(self.host, "workspace_root", None) or _Path.cwd()
        return get_index_coordinator(workspace_root)

    async def _fetch_action_catalog_items(self) -> list[dict] | None:
        """Fetch the current action catalog via ``list_actions`` against a
        fresh ``RouterCallerState`` snapshot.

        Extracted (behavior-preserving) from
        ``_build_action_embedding_index_background`` (P2-convergence PR1,
        #3270 §2) so BOTH that method (kept for direct/standalone callers
        — see its docstring) and the production ``ensure_built`` build_fn
        (``_ensure_action_index_built``) fetch the SAME catalog the SAME
        way — a single source so the two shapes cannot silently diverge on
        WHAT gets embedded. Returns ``None`` when ``list_actions`` is not
        registered (defensive; should not happen in production).
        """
        from reyn.tools import get_default_registry
        from reyn.tools.types import ToolContext

        rs = await self._build_router_caller_state()
        tool_ctx = ToolContext(
            events=self.host.events,
            permission_resolver=getattr(self.host, "permission_resolver", None),
            workspace=getattr(self.host, "workspace", None),
            caller_kind="router",
            router_state=rs,
            # #1673: thread the config-aware resolver (see the sibling sites).
            resolver=getattr(self.host, "resolver", None),
            hot_reloader=getattr(self.host, "hot_reloader", None),  # #2073 S3
            state_log=getattr(self.host, "state_log", None),  # #2248 PR-A2 (config emit)
            agent_name=getattr(self.host, "agent_name", None),  # #2088: scope-aware hooks_add
            session_state_dir=getattr(self.host, "session_state_dir", None),  # #4215①
        )
        list_actions_def = get_default_registry().lookup("list_actions")
        if list_actions_def is None:
            return None
        result = await list_actions_def.handler({}, tool_ctx)
        return result.get("items", []) if isinstance(result, dict) else []

    async def _ensure_action_index_built(
        self, idx: Any, provider: Any, model_class: str, *, await_completion: bool,
    ) -> None:
        """P2-convergence PR1 (#3270 §2, design firm on #3270): route the
        action-catalog build's eager-vs-background DECISION + once-per-
        source spawn dedup + failure-memo through the single
        ``IndexCoordinator.ensure_built`` + ``register_builder`` — the
        parallel ``ensure_built_self_contained`` two-path entry point this
        method used to call is ELIMINATED (see ``coordinator.py``'s module
        docstring).

        ``ActionEmbeddingIndex.prepare_material`` (P2-convergence PR1) is
        registered as the ``BuildFn``: lock-free, write-free material
        generation that keeps the action-catalog's own disk-adopt +
        dual-axis-invalidation POLICY (the Coordinator never owns domain
        policy) — it returns ``BuildMaterial`` when a real rebuild is
        needed (the Coordinator's ``_run_build`` then owns
        ``embed_verify_write``) or ``None`` when its own policy already
        determined nothing needs writing (a disk-adopt cache hit, which
        ``prepare_material`` adopts directly onto ``idx``).

        Two Coordinator hooks close the gaps a material-only ``BuildFn``
        opens for THIS specific adapter (both fire from inside
        ``IndexCoordinator._run_build``, uniformly for eager AND
        background, since that is the one coroutine body either awaited
        inline or scheduled via ``asyncio.create_task``):

        * ``on_error`` — P2-convergence PR2 (#3270 §3): #1458's
          decision-enabling warning log ONLY. The failure-memoization
          itself is no longer duplicated on the RouterLoop instance — it
          lives solely in the Coordinator's ``_failure_memo``
          (``build_failed()``), set one frame up in ``_run_build``'s own
          except-block BEFORE this callback runs, so by the time
          ``on_error`` fires the memo is already the single source of
          truth.
        * ``on_success`` — syncs ``idx``'s in-memory
          ``is_ready()``/``size()``/``catalog_hash()`` gate
          (``ActionEmbeddingIndex.adopt_build_result``) after a REAL
          rebuild, since the Coordinator (not ``idx``) now performs the
          write. The disk-adopt ``None`` case needs no sync here —
          ``prepare_material`` already mutated ``idx`` directly.

        Before any of that: if ``idx`` is already ready, this is a cheap
        no-op (mirrors the pre-PR1 ``ensure_built_self_contained``'s
        combined "manifest clean AND is_ready_probe()" gate). If ``idx``
        is NOT ready but the Coordinator's PERSISTED manifest already says
        "clean" (a fresh process/instance re-reading a prior process's
        completed build — see ``ActionEmbeddingIndex``'s module docstring
        "process-restart cache hit"), ``ensure_built``'s own top-level
        clean-shortcut would otherwise skip calling ``prepare_material``
        entirely, permanently stranding THIS ``idx`` instance not-ready —
        so a targeted ``mark_dirty`` forces ``ensure_built`` to actually
        invoke ``prepare_material`` (whose OWN cheap disk-adopt check then
        typically resolves in one file-stat, no embed call).

        #1458 same-session AUTO-rebuild suppression (P2-convergence PR2,
        #3270 §3, REVISED after a co-vet-caught regression): a co-vet round
        found that enforcing this inside ``IndexCoordinator.ensure_built``
        itself silently broke the §G2 heal contract for every OTHER
        registered source — ``search_await`` calls ``ensure_built``
        directly (not through this method) to heal a dirty/failed entry
        once its provider recovers, and a blanket suppression there made a
        dirty+failed source unhealable forever. ``ensure_built`` is
        TRIGGER-AGNOSTIC; the suppression is enforced HERE instead — the
        ONE chokepoint both the eager (``await_completion=True``) and
        background (``await_completion=False``) AUTO-rebuild paths funnel
        through (``RouterLoop.run()`` calls only this method for both).
        Reads STATE from the Coordinator's ``build_failed(source_id)`` (the
        single owner of that state — see ``coordinator.py``); this method
        owns the POLICY decision to skip a further AUTO attempt once that
        state is True, which is why the check lives here and not on the
        Coordinator.
        """
        if getattr(idx, "is_ready", lambda: False)():
            return

        coordinator = self._get_index_coordinator()
        source_id = getattr(idx, "source_name", None) or "actions"

        if coordinator.build_failed(source_id):
            # #1458: a prior AUTO-rebuild attempt in this session already
            # failed for this source — do not spawn another one. (This is
            # the suppression POLICY seam; ``search_await``'s heal path
            # does NOT go through this method, so it is unaffected.)
            return

        if await coordinator.is_ready(source_id):
            await coordinator.mark_dirty(
                source_id, reason="action_index_instance_not_ready",
            )

        _fetch_state: dict[str, Any] = {}

        async def _build_fn() -> Any:
            items = await self._fetch_action_catalog_items()
            if items is None:
                items = []
            _fetch_state["items"] = items
            op_ctx = self.host.make_router_op_context()
            return await idx.prepare_material(items, op_ctx, model_class)

        def _on_error(exc: BaseException) -> None:
            # #1458: decision-enabling warning log. Failure memoization
            # itself is the Coordinator's ``_failure_memo`` (set already,
            # one frame up in ``_run_build``'s except-block) — see the
            # docstring above.
            import logging

            logging.getLogger(__name__).warning(
                "%s", _action_index_build_failure_warning(exc, model_class)
            )

        def _on_success(outcome: Any) -> None:
            if getattr(idx, "is_ready", lambda: False)():
                return  # prepare_material's disk-adopt branch already synced idx
            items = _fetch_state.get("items")
            adopt = getattr(idx, "adopt_build_result", None)
            if items is None or adopt is None:
                return
            from reyn.tools.action_index import compute_catalog_hash

            chunk_count = outcome.chunk_count if outcome.chunk_count is not None else 0
            adopt(compute_catalog_hash(items), model_class, chunk_count)

        coordinator.register_builder(source_id, _build_fn, kind="static")
        await coordinator.ensure_built(
            source_id,
            await_completion=await_completion,
            events=self.host.events,
            on_error=_on_error,
            on_success=_on_success,
        )

    async def _build_router_caller_state(self) -> Any:
        """Build a RouterCallerState populated with bound callbacks.

        Bindings follow the wiring contract documented in
        ``reyn.tools.types.RouterCallerState``:

        * Catalog ``_fn`` callables wrap RouterLoop's private helpers
          (``_list_agents`` / ``_describe_agent``) so the registry handlers
          stay decoupled from RouterLoopHost type.

        Forward-looking fields (``available_agents`` for schema enrichment,
        identity / cost / model context) are also populated so future handler
        activations have what they need.

        #2567: the host-derived (resource) fields — ``available_agents``,
        ``op_context_factory``, ``host``, ``available_rag_sources``,
        ``action_embedding_index``/``embedding_provider``/
        ``embedding_model_class``, ``sandbox_backend``, ``mcp_servers``,
        ``available_skills``, ``agent_registry``, ``pipeline_registry`` — are
        built by the shared ``build_resource_caller_state(host)`` factory (a
        byte-identical extraction of what this method used to inline) so any
        caller with just a host reference (e.g. the async pipeline driver's
        tool-step dispatch) gets the SAME resource wiring a router turn gets.
        This method overlays the remaining loop-local fields (chain_id,
        budget, dispatch callbacks, catalog/memory bindings, ...) that only
        exist mid-RouterLoop-turn.
        """
        import dataclasses

        from reyn.tools.types import build_resource_caller_state

        resource_state = await build_resource_caller_state(self.host)

        # #2103 S1bc: session-spawn binding. Only multi-session hosts (the chat
        # RouterHostAdapter) implement ``spawn_session``; a host without it leaves
        # this None (= duck-typed / hasattr-guarded). chain_id pre-bound.
        _spawn_session_bound: Any = None
        if hasattr(self.host, "spawn_session") and callable(
            getattr(self.host, "spawn_session", None)
        ):
            async def _spawn_session_bound_impl(
                *, request: str, mode: str, narrowing: "dict | None" = None,
                base_dir: "str | None" = None,
                agent: "str | None" = None, session: "str | None" = None,
            ) -> dict:
                return await self.host.spawn_session(
                    request=request, mode=mode, narrowing=narrowing,
                    base_dir=base_dir, chain_id=self.chain_id,
                    agent=agent, session=session,
                )
            _spawn_session_bound = _spawn_session_bound_impl

        # #2103 B-tool: agent-spawn binding (mirror session-spawn). Only multi-agent
        # hosts implement ``spawn_agent``; a host without it leaves this None.
        _spawn_agent_bound: Any = None
        if hasattr(self.host, "spawn_agent") and callable(
            getattr(self.host, "spawn_agent", None)
        ):
            async def _spawn_agent_bound_impl(*, name: str, role: str = "") -> dict:
                return await self.host.spawn_agent(name=name, role=role)
            _spawn_agent_bound = _spawn_agent_bound_impl

        # Proposal 0067 P5 (#3978): send_to_session binding (mirror
        # session-spawn). Only multi-session hosts implement it; a host
        # without it leaves this None. No pre-bound identity — target
        # agent/session are per-call args.
        _send_to_session_bound: Any = None
        if hasattr(self.host, "send_to_session") and callable(
            getattr(self.host, "send_to_session", None)
        ):
            async def _send_to_session_bound_impl(
                *, agent: str, session: str, text: str, wake: bool = False,
            ) -> dict:
                return await self.host.send_to_session(
                    agent=agent, session=session, text=text, wake=wake,
                )
            _send_to_session_bound = _send_to_session_bound_impl

        # Proposal 0067 P4d (#3978): run_prompt(collect="attached") binding
        # (mirror send_to_session above). Only multi-session hosts implement
        # it; a host without it leaves this None.
        _run_prompt_result_bound: Any = None
        if hasattr(self.host, "run_prompt_result") and callable(
            getattr(self.host, "run_prompt_result", None)
        ):
            async def _run_prompt_result_bound_impl(
                *, agent: str, session: str, prompt: str,
                timeout: "float | None" = None,
            ) -> dict:
                return await self.host.run_prompt_result(
                    agent=agent, session=session, prompt=prompt, timeout=timeout,
                )
            _run_prompt_result_bound = _run_prompt_result_bound_impl

        # Proposal 0067 P4e (#3978): run_prompt(collect="async") binding
        # (mirror run_prompt(collect="attached") above). Only multi-session
        # hosts implement it; a host without it leaves this None.
        _run_prompt_async_bound: Any = None
        if hasattr(self.host, "run_prompt_async") and callable(
            getattr(self.host, "run_prompt_async", None)
        ):
            async def _run_prompt_async_bound_impl(
                *, agent: str, session: str, prompt: str,
            ) -> dict:
                return await self.host.run_prompt_async(
                    agent=agent, session=session, prompt=prompt,
                )
            _run_prompt_async_bound = _run_prompt_async_bound_impl

        # #2103 C1: topology-create binding (mirror agent-spawn). Only multi-agent hosts
        # implement ``create_topology``; a host without it leaves this None.
        _topology_create_bound: Any = None
        if hasattr(self.host, "create_topology") and callable(
            getattr(self.host, "create_topology", None)
        ):
            async def _topology_create_bound_impl(
                *,
                name: str,
                kind: str,
                members: "list[str]",
                leader: "str | None" = None,
                profiles: "dict[str, str] | None" = None,
            ) -> dict:
                return await self.host.create_topology(
                    name=name, kind=kind, members=members,
                    leader=leader, profiles=profiles,
                )
            _topology_create_bound = _topology_create_bound_impl

        # #2567: overlay the loop-local ((b)/(c)) fields onto the host-derived
        # ``resource_state`` built above — the resource (a)-fields
        # (available_agents / op_context_factory / host / available_rag_sources
        # / action_embedding_index / embedding_provider / embedding_model_class
        # / sandbox_backend / mcp_servers / available_skills / agent_registry /
        # pipeline_registry) are UNTOUCHED here; only fields this method alone
        # can populate (chain_id, budget, catalog/memory bindings, dispatch
        # callbacks, ...) are set.
        return dataclasses.replace(
            resource_state,
            # Catalog access (= activated handlers)
            list_agents_fn=self._list_agents,
            describe_agent_fn=self._describe_agent,
            # Proposal 0067 P4d (#3978): run_prompt(collect="attached") dispatch
            # (None for non-multi-session hosts).
            run_prompt_result_fn=_run_prompt_result_bound,
            # Proposal 0067 P4e (#3978): run_prompt(collect="async") dispatch
            # (None for non-multi-session hosts).
            run_prompt_async_fn=_run_prompt_async_bound,
            # #2103 S1bc: session-spawn dispatch (None for non-multi-session hosts).
            spawn_session_fn=_spawn_session_bound,
            # Proposal 0067 P5 (#3978): send_to_session dispatch (None for
            # non-multi-session hosts).
            send_to_session_fn=_send_to_session_bound,
            # #2103 B-tool: agent-spawn dispatch (None for non-multi-agent hosts).
            spawn_agent_fn=_spawn_agent_bound,
            # #2103 C1: topology-create dispatch (None for non-multi-agent hosts).
            topology_create_fn=_topology_create_bound,
            # Memory tool bridges (= for memory cluster handlers).
            # ``list_memory`` still parses the host's rendered
            # ``get_memory_index()`` listing, so it stays a loop helper;
            # remember / forget / read_body are memory-STORE operations and
            # ride the capability itself (#3607) — the loop no longer holds
            # a re-implementation of them over file primitives.
            list_memory_fn=self._list_memory,
            memory_service=getattr(self.host, "memory", None),
            # Identity + cost + model context (forward-looking; consumed by
            # schema_enricher hooks and future activated handlers)
            chain_id=self.chain_id,
            budget=self.budget,
            router_model=self.router_model,
            available_tool_names=list(self._tool_names),
            # #1667: catalog categories the universal catalog skips at source.
            excluded_categories=self._excluded_categories,
        )

    async def _invoke_via_registry(self, name: str, args: dict) -> Any:
        """Dispatch a tool through the unified ToolRegistry handler.

        Builds a ToolContext with a populated RouterCallerState and calls
        the canonical handler from ``src/reyn/tools/<name>.py``. Wrapped
        externally by ``dispatch_tool`` (= same cross-cutting events /
        validation / error envelope as the legacy invoker path).

        The handler's result is returned VERBATIM. #3429 deleted the
        ``_normalise_router_tool_result`` post-step that used to sit here: it
        re-extracted ``read_file``'s ``content`` to a bare string and
        ``list_directory``'s ``entries`` to a bare list, so that registry
        dispatch stayed byte-identical to the pre-ADR-0026 router branches the
        migration replaced. Its own docstring gave that — "byte-identity with
        prior LLM-visible output" — as the whole reason it existed, i.e. it was
        back-compat with a code path that no longer exists, and back-compat is
        not a reason.

        It was also the sharpest instance of the #3429 defect: it keyed on the
        name it was DISPATCHED with, so the qualified spelling of the same read
        (dispatched as ``invoke_action``) skipped it and the model got a
        different value back for the same operation.

        Deleting it lets both results reach ``file_to_canonical`` — the mapper
        that already renders a read's ``content`` as the body and a listing's
        paths as a ``structured`` attachment, and that additionally surfaces an
        image read's ``media_blocks`` (which the flattening dropped outright)
        and the ``truncated`` / ``total_count`` / ``returned_count`` /
        ``note`` signals as frontmatter the model reads. Nothing is
        re-implemented here; the mapper was always the designed destination —
        ``_file_signal_meta``'s docstring named this bypass as a known one.
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
            # #1673: thread the config-aware resolver so a tool handler that spawns
            # a sub-run hands the spawned OpContext a real resolver
            # instead of resolver=None (→ literal "standard" → litellm BadRequestError).
            resolver=getattr(self.host, "resolver", None),
            hot_reloader=getattr(self.host, "hot_reloader", None),  # #2073 S3
            state_log=getattr(self.host, "state_log", None),  # #2248 PR-A2 (config emit)
            agent_name=getattr(self.host, "agent_name", None),  # #2088: scope-aware hooks_add
            session_state_dir=getattr(self.host, "session_state_dir", None),  # #4215①
        )
        return await invoke_tool(get_default_registry(), name, args, tool_ctx)

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

        # #3429: there is no second arm. A catalog action used to be reachable
        # under a ``<category>__<verb>`` spelling that was NOT in
        # ``REGISTRY_DISPATCH_TOOLS``, so it needed re-routing through
        # ``invoke_action`` — the wrapper hop that made the two spellings of one
        # operation return different things to the model (skipped result
        # normalisation, a generic canonical renderer, a wrapper-named
        # ``routing_decided``). The spelling is gone, so every action arrives
        # here under the one name the registry dispatches directly.
        #
        # Should not be reached if catalog is correct — dispatch_tool already
        # validated name is in catalog. Return error for safety.
        return {"error": f"unhandled tool: {name}"}

    # -----------------------------------------------------------------------
    # Discovery helpers (pure, no async host calls)
    # -----------------------------------------------------------------------

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

