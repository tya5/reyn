"""LLMReplay — deterministic LLM record / replay for Reyn tests.

Design
------
Monkeypatches ``litellm.acompletion`` AND ``litellm.aembedding`` (#3451 part
1 — the two async boundaries reyn's own source code calls; see
``reyn.dev.testing.network_gate`` for how that pair is kept honest against a
FUTURE litellm surface reyn starts calling) so that all LLM/embedding calls
in a test are intercepted at a single, stable point each.

Fixture format (JSONL, one call per line)
-----------------------------------------
Two shapes share one file, distinguished by ``"kind"`` (absent/``"completion"``
= legacy acompletion entries predating #3451; ``"embedding"`` = aembedding)::

    {"key": "<sha256>", "kind": "completion", "model": "openai/gemini-2.5-flash-lite",
     "prompt_preview": "...", "response": {...}}

    {"key": "<sha256>", "kind": "embedding", "model": "openai/text-embedding-3-small",
     "prompt_preview": "...", "response": {...}}

- ``key`` (completion) SHA-256 hex of ``model + canonical_json(messages)``
  (legacy, no tools) or ``model + canonical_json(messages) +
  canonical_json(tools) + tool_choice`` when tools/tool_choice are present
  (PR35+).
- ``key`` (embedding) SHA-256 hex of ``"embed|" + model + canonical_json(input)``
  — a distinct namespace (the ``"embed|"`` prefix) from the completion key so
  the two kinds can never collide even though they share one fixture file.
- ``model`` / ``prompt_preview``  human-readable grep aids; not used for lookup.
- ``response``  ``litellm.ModelResponse.model_dump()`` (completion) or
  ``litellm.EmbeddingResponse.model_dump()`` (embedding), serialised to dict.
  On replay the dict is reconstructed as the matching litellm response type.

Record mode
-----------
Set ``REYN_LLM_RECORD=1`` before running pytest to call the real LLM/embedding
provider and write fixtures. If a fixture file is absent, record mode is
activated automatically (first-run fixture generation).

Sensitive data note
-------------------
``prompt_preview`` is capped at 200 characters and is purely informational.
No API keys or auth tokens are ever forwarded in the fixture because Reyn
reads those from env-vars (never injects them into the messages list); the
monkeypatch therefore never sees them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass

class MissingFixture(Exception):
    """Raised in replay mode when no fixture entry matches the call."""


class LLMReplay:
    """Record or replay ``litellm.acompletion`` AND ``litellm.aembedding`` calls.

    Usage (via conftest)::

        replay = LLMReplay(fixture_path, mode="replay")
        replay.install()
        try:
            # run test ...
        finally:
            replay.restore()

    Parameters
    ----------
    fixture_path:
        Path to the ``.jsonl`` fixture file.  Created on first ``flush()``
        in record mode.
    mode:
        ``"replay"`` — look up saved responses; raise ``MissingFixture`` on
        a cache miss.
        ``"record"`` — call the real LLM/embedding provider and append to
        the fixture file.
    """

    def __init__(self, fixture_path: Path, mode: Literal["replay", "record"]) -> None:
        self.fixture_path = fixture_path
        self.mode = mode
        # key → serialised ModelResponse dict (kind="completion")
        self._records: dict[str, dict] = {}
        # key → serialised EmbeddingResponse dict (kind="embedding")
        self._embed_records: dict[str, dict] = {}
        # pending writes (record mode only) — completion and embedding entries
        # share one pending list; each entry carries its own "kind".
        self._pending: list[dict] = []
        self._original_acompletion: Any = None
        self._original_aembedding: Any = None
        self._load()

    # ── Key computation ────────────────────────────────────────────────────────

    @staticmethod
    def key(
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> str:
        """Return the SHA-256 cache key for an acompletion call.

        Backward compatibility (Option A):
        - When ``tools`` is None/empty *and* ``tool_choice`` is None/empty,
          the key is byte-identical to the pre-PR35 format
          ``sha256(model_bytes + messages_json_bytes)`` so existing fixtures
          continue to match without re-recording.
        - When tools or tool_choice are non-empty (PR35+ calls), the key uses
          a pipe-delimited format that incorporates tools and tool_choice.

        ``sort_keys=True`` + ``ensure_ascii=False`` gives a stable
        serialisation regardless of insertion order.
        """
        messages_json = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256()
        if tools or tool_choice:
            # PR35+ format: pipe-delimited payload including tools and tool_choice.
            tools_json = json.dumps(tools or [], sort_keys=True, ensure_ascii=False)
            payload = f"{model}|{messages_json}|{tools_json}|{tool_choice or ''}"
            h.update(payload.encode())
        else:
            # Legacy format — preserves all pre-PR35 fixture keys unchanged.
            # The original code concatenated model bytes then messages bytes
            # directly (no separator), so we must replicate that exactly.
            h.update(model.encode())
            h.update(messages_json.encode())
        return h.hexdigest()

    @staticmethod
    def embed_key(model: str, input_texts: list[str]) -> str:
        """Return the SHA-256 cache key for an ``aembedding`` call.

        A distinct ``"embed|"``-prefixed namespace from :meth:`key` — the two
        kinds share one fixture *file* (so a test's replay fixture stays a
        single artifact regardless of which litellm boundary it exercises),
        but must never collide in the lookup dict even for a pathological
        ``model``+``messages`` string that happens to equal some ``model``+
        ``input`` string.
        """
        input_json = json.dumps(input_texts, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256()
        h.update(f"embed|{model}|{input_json}".encode())
        return h.hexdigest()

    @staticmethod
    def _prompt_preview(messages: list[dict]) -> str:
        """First 200 chars of the last message's content (human aid only)."""
        if not messages:
            return ""
        last = messages[-1]
        content = last.get("content", "")
        # content may be a list (multi-block) — flatten to string
        if isinstance(content, list):
            parts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ]
            content = " ".join(parts)
        return str(content)[:200]

    # ── Fixture I/O ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load existing fixture into ``self._records``/``self._embed_records``
        (no-op if absent). ``entry["kind"]`` routes each line; entries predating
        #3451 have no ``"kind"`` key and default to ``"completion"`` (the only
        kind that existed before aembedding coverage was added)."""
        if not self.fixture_path.exists():
            return
        for raw_line in self.fixture_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
                if entry.get("kind", "completion") == "embedding":
                    self._embed_records[entry["key"]] = entry["response"]
                else:
                    self._records[entry["key"]] = entry["response"]
            except Exception:
                # Skip corrupt lines — fixture is a test artifact; silent skip
                # is acceptable (same policy as BudgetLedger).
                pass

    def flush(self) -> None:
        """Write pending record-mode entries to the fixture file.

        Appends new entries; existing entries are not rewritten.  The
        fixture directory is created automatically.
        """
        if not self._pending:
            return
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fixture_path.open("a", encoding="utf-8") as fh:
            for entry in self._pending:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._pending.clear()

    # ── Monkeypatch lifecycle ──────────────────────────────────────────────────

    def install(self) -> None:
        """Replace ``litellm.acompletion`` AND ``litellm.aembedding`` with this
        instance's handlers (#3451 — both boundaries, one Fake)."""
        import litellm

        self._original_acompletion = litellm.acompletion
        litellm.acompletion = self._handle  # type: ignore[attr-defined]
        self._original_aembedding = litellm.aembedding
        litellm.aembedding = self._handle_embedding  # type: ignore[attr-defined]

    def restore(self) -> None:
        """Restore the original ``litellm.acompletion`` / ``litellm.aembedding``."""
        import litellm

        if self._original_acompletion is not None:
            litellm.acompletion = self._original_acompletion  # type: ignore[attr-defined]
            self._original_acompletion = None
        if self._original_aembedding is not None:
            litellm.aembedding = self._original_aembedding  # type: ignore[attr-defined]
            self._original_aembedding = None

    # ── Request handler ────────────────────────────────────────────────────────

    async def _handle(
        self, model: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        """Intercept an ``acompletion`` call.

        Replay mode: look up by key; raise ``MissingFixture`` on miss.
        Record mode: forward to real LLM; save response; return response.

        #3288 ③a: the fixture format is unchanged (always a whole
        ``ModelResponse``) — a ``stream=True`` caller (reyn's
        capability-gated streaming loop in ``recorded_acompletion``) is
        served the SAME recorded/replayed response, wrapped into a small
        synthetic multi-chunk stream (see ``_synthetic_stream``) rather than
        by recording/replaying real provider chunks. This keeps every
        existing fixture valid for both the streaming and non-streaming
        code paths — reconstructing the synthetic stream must yield the
        byte-identical response, which doubles as a live stream≡whole
        equivalence check across the whole fixture-based test suite.
        """
        tools: list[dict] | None = kwargs.get("tools")
        tool_choice: str | None = kwargs.get("tool_choice")
        key = self.key(model, messages, tools=tools, tool_choice=tool_choice)
        stream = bool(kwargs.get("stream"))

        if self.mode == "replay":
            response = self._replay(key, model, messages)
        else:
            # record mode: always fetch/store the WHOLE response — strip
            # ``stream``/``stream_options`` from the forwarded call so the
            # real LLM call + fixture format are unaffected by what THIS
            # call happened to request.
            record_kwargs = {
                k: v for k, v in kwargs.items() if k not in ("stream", "stream_options")
            }
            response = await self._record(key, model, messages, record_kwargs)

        if stream:
            return self._synthetic_stream(response)
        return response

    @staticmethod
    def _synthetic_stream(response: Any):
        """Wrap a whole ``ModelResponse`` into a small async-iterable of
        ``ModelResponseStream`` chunks reconstructing to the SAME response.

        Splits text content and (per tool_call) argument strings across two
        chunks each when long enough — exercising the SAME delta-accumulation
        path (content concatenation, per-index tool_call argument
        accumulation) a real provider stream would, instead of a single
        pass-through chunk. Usage is attached on the terminal chunk so
        ``litellm.stream_chunk_builder``'s reconstruction carries the exact
        recorded/real usage (no token-count re-estimate drift).
        """
        from litellm.types.utils import (
            ChatCompletionDeltaToolCall,
            Delta,
            Function,
            ModelResponseStream,
            StreamingChoices,
        )

        async def _gen():
            message = response.choices[0].message
            model_name = getattr(response, "model", None) or "replay"

            def _chunk(delta: Delta, finish_reason: str | None = None) -> ModelResponseStream:
                return ModelResponseStream(
                    id=getattr(response, "id", "replay-stream"),
                    created=getattr(response, "created", 0) or 0,
                    model=model_name,
                    object="chat.completion.chunk",
                    choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
                )

            content = message.content
            if content:
                mid = len(content) // 2
                if mid > 0:
                    yield _chunk(Delta(role="assistant", content=content[:mid]))
                    yield _chunk(Delta(content=content[mid:]))
                else:
                    yield _chunk(Delta(role="assistant", content=content))
            elif not (message.tool_calls or []):
                yield _chunk(Delta(role="assistant"))

            # #3288 co-vet BLOCK fix: each PARALLEL tool_call gets its OWN
            # ``index`` (enumerate), not a shared ``index=0``.
            # ``stream_chunk_builder`` accumulates tool_call deltas PER
            # INDEX — every chunk claiming index=0 makes it merge N parallel
            # tool calls into ONE (arguments concatenated into invalid JSON,
            # silently — no exception). reyn does emit parallel tool calls,
            # so this must round-trip them distinctly, matching the index a
            # real provider stream assigns per parallel call.
            for tc_index, tc in enumerate(message.tool_calls or []):
                args = tc.function.arguments or ""
                mid = len(args) // 2
                if mid > 0:
                    yield _chunk(Delta(tool_calls=[
                        ChatCompletionDeltaToolCall(
                            id=tc.id, index=tc_index, type="function",
                            function=Function(name=tc.function.name, arguments=args[:mid]),
                        ),
                    ]))
                    yield _chunk(Delta(tool_calls=[
                        ChatCompletionDeltaToolCall(
                            index=tc_index, function=Function(arguments=args[mid:]),
                        ),
                    ]))
                else:
                    yield _chunk(Delta(tool_calls=[
                        ChatCompletionDeltaToolCall(
                            id=tc.id, index=tc_index, type="function",
                            function=Function(name=tc.function.name, arguments=args),
                        ),
                    ]))

            finish_reason = None
            try:
                finish_reason = response.choices[0].finish_reason
            except Exception:
                pass
            last = _chunk(Delta(), finish_reason=finish_reason)
            usage = getattr(response, "usage", None)
            if usage is not None:
                last.usage = usage
            yield last

        return _gen()

    async def _handle_embedding(self, model: str, input: Any, **kwargs: Any) -> Any:  # noqa: A002
        """Intercept an ``aembedding`` call (#3451 — the ``_handle`` sibling for
        the embedding boundary; same record/replay semantics, separate key
        namespace, separate response type).

        ``input`` is litellm's own parameter name for the embedding boundary
        (a str or list[str]) — shadowing the builtin is what the real
        ``litellm.aembedding`` signature does too, so this keeps kwarg
        pass-through byte-identical for callers that pass it positionally
        via ``**kwargs``.
        """
        input_texts = input if isinstance(input, list) else [input]
        key = self.embed_key(model, input_texts)

        if self.mode == "replay":
            return self._replay_embedding(key, model, input_texts)
        return await self._record_embedding(key, model, input_texts, kwargs)

    def _replay_embedding(self, key: str, model: str, input_texts: list[str]) -> Any:
        """Return a reconstructed ``EmbeddingResponse`` from the fixture."""
        if key not in self._embed_records:
            preview = ", ".join(input_texts)[:200]
            raise MissingFixture(
                f"No embedding fixture entry for model={model!r}.\n"
                f"Input preview: {preview!r}\n"
                f"Fixture: {self.fixture_path}\n"
                f"Re-run with REYN_LLM_RECORD=1 to record new fixtures."
            )
        import litellm

        return litellm.EmbeddingResponse(**self._embed_records[key])

    async def _record_embedding(
        self, key: str, model: str, input_texts: list[str], extra_kwargs: dict
    ) -> Any:
        """Call the real embedding provider, save the response, and return it."""
        response = await self._original_aembedding(
            model=model, input=input_texts, **extra_kwargs
        )
        response_dict = response.model_dump()
        preview = ", ".join(input_texts)[:200]
        entry = {
            "key": key,
            "kind": "embedding",
            "model": model,
            "prompt_preview": preview,
            "response": response_dict,
        }
        self._embed_records[key] = response_dict
        self._pending.append(entry)
        return response

    def _replay(self, key: str, model: str, messages: list[dict]) -> Any:
        """Return a reconstructed ``ModelResponse`` from the fixture."""
        if key not in self._records:
            preview = self._prompt_preview(messages)
            raise MissingFixture(
                f"No fixture entry for model={model!r}.\n"
                f"Prompt preview: {preview!r}\n"
                f"Fixture: {self.fixture_path}\n"
                f"Re-run with REYN_LLM_RECORD=1 to record new fixtures."
            )
        import litellm

        return litellm.ModelResponse(**self._records[key])

    async def _record(
        self, key: str, model: str, messages: list[dict], extra_kwargs: dict
    ) -> Any:
        """Call the real LLM, save the response, and return it."""
        response = await self._original_acompletion(
            model=model, messages=messages, **extra_kwargs
        )
        # Serialise to a plain dict for JSONL storage.
        response_dict = response.model_dump()
        preview = self._prompt_preview(messages)
        entry = {
            "key": key,
            "kind": "completion",
            "model": model,
            "prompt_preview": preview,
            "response": response_dict,
        }
        self._records[key] = response_dict
        self._pending.append(entry)
        return response

    # ── Context-manager convenience ────────────────────────────────────────────

    def __enter__(self) -> "LLMReplay":
        self.install()
        return self

    def __exit__(self, *_: Any) -> None:
        self.restore()
        if self.mode == "record":
            self.flush()
