"""LLMReplay — deterministic LLM record / replay for Reyn tests.

Datetime stability
------------------
``ContextFrame.current_datetime`` is populated with ``datetime.now()`` by
default and therefore changes on every run.  Tests that call ``call_llm``
directly must pass a fixed datetime to ``ContextFrame`` so the serialised
user-turn message is identical to the recorded fixture.  Use the module-level
constant ``REPLAY_DATETIME`` for this::

    from reyn.dev.testing.replay import REPLAY_DATETIME
    frame = ContextFrame(..., current_datetime=REPLAY_DATETIME)


Design
------
Monkeypatches ``litellm.acompletion`` (the *async* boundary used by both
``reyn.llm.call_llm`` and ``reyn.skill_node_runner._adapt_artifact``) so
that all LLM calls in a test are intercepted at a single, stable point.

Fixture format (JSONL, one call per line)
-----------------------------------------
::

    {"key": "<sha256>", "model": "openai/gemini-2.5-flash-lite",
     "prompt_preview": "...", "response": {...}}

- ``key``   SHA-256 hex of ``model + canonical_json(messages)`` (legacy, no tools)
  or ``model + canonical_json(messages) + canonical_json(tools) + tool_choice``
  when tools/tool_choice are present (PR35+).
- ``model`` / ``prompt_preview``  human-readable grep aids; not used for lookup.
- ``response``  ``litellm.ModelResponse.model_dump()`` serialised to dict.
  On replay the dict is reconstructed as a ``litellm.ModelResponse``.

Record mode
-----------
Set ``REYN_LLM_RECORD=1`` before running pytest to call the real LLM and
write fixtures. If a fixture file is absent, record mode is activated
automatically (first-run fixture generation).

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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass

# Fixed datetime used in all replay test fixtures so that ContextFrame's
# volatile ``current_datetime`` field does not break the SHA-256 key lookup.
# Tests must pass this constant when constructing ContextFrame objects.
REPLAY_DATETIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class MissingFixture(Exception):
    """Raised in replay mode when no fixture entry matches the call."""


class LLMReplay:
    """Record or replay ``litellm.acompletion`` calls.

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
        ``"record"`` — call the real LLM and append to the fixture file.
    """

    def __init__(self, fixture_path: Path, mode: Literal["replay", "record"]) -> None:
        self.fixture_path = fixture_path
        self.mode = mode
        # key → serialised ModelResponse dict
        self._records: dict[str, dict] = {}
        # pending writes (record mode only)
        self._pending: list[dict] = []
        self._original_acompletion: Any = None
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
        """Load existing fixture into ``self._records`` (no-op if absent)."""
        if not self.fixture_path.exists():
            return
        for raw_line in self.fixture_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
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
        """Replace ``litellm.acompletion`` with this instance's handler."""
        import litellm

        self._original_acompletion = litellm.acompletion
        litellm.acompletion = self._handle  # type: ignore[attr-defined]

    def restore(self) -> None:
        """Restore the original ``litellm.acompletion``."""
        if self._original_acompletion is not None:
            import litellm

            litellm.acompletion = self._original_acompletion  # type: ignore[attr-defined]
            self._original_acompletion = None

    # ── Request handler ────────────────────────────────────────────────────────

    async def _handle(
        self, model: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        """Intercept an ``acompletion`` call.

        Replay mode: look up by key; raise ``MissingFixture`` on miss.
        Record mode: forward to real LLM; save response; return response.
        """
        tools: list[dict] | None = kwargs.get("tools")
        tool_choice: str | None = kwargs.get("tool_choice")
        key = self.key(model, messages, tools=tools, tool_choice=tool_choice)

        if self.mode == "replay":
            return self._replay(key, model, messages)

        # record mode
        return await self._record(key, model, messages, kwargs)

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
