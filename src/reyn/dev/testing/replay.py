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
Three shapes share one file, distinguished by ``"kind"`` (absent/``"completion"``
= legacy acompletion entries predating #3451; ``"embedding"`` = aembedding;
``"environment"`` = a captured environment snapshot, #3473)::

    {"key": "<sha256>", "kind": "completion", "model": "openai/gemini-2.5-flash-lite",
     "prompt_preview": "...", "preconditions": {...}, "key_components": {...},
     "response": {...}}

    {"key": "<sha256>", "kind": "embedding", "model": "openai/text-embedding-3-small",
     "prompt_preview": "...", "response": {...}}

    {"kind": "environment", "name": "mcp_catalog", "value": {...}}

- ``key`` (completion) SHA-256 hex of ``model + canonical_json(messages)``
  (legacy, no tools) or ``model + canonical_json(messages) +
  canonical_json(tools) + tool_choice`` when tools/tool_choice are present
  (PR35+) — with every registered environment precondition's imprint SCRUBBED
  out of ``tools`` first, so the key is the SCENARIO (see below).
- ``key`` (embedding) SHA-256 hex of ``"embed|" + model + canonical_json(input)``
  — a distinct namespace (the ``"embed|"`` prefix) from the completion key so
  the two kinds can never collide even though they share one fixture file.
- ``model`` / ``prompt_preview``  human-readable grep aids; not used for lookup.
- ``preconditions`` (completion, #3473) ``{precondition name: observed value}``
  — the environment imprints scrubbed out of that call's key, recorded so
  they can be CHECKED instead of hashed.
- ``key_components`` (completion, #3473) the per-component fingerprint
  (``model`` / per-message digests / per-tool-name digests / ``tool_choice``)
  a later cache miss is attributed against — see
  ``reyn.dev.testing.replay_key_diff``. One-way digests: enough to say WHICH
  component moved, never enough to reconstruct the payload.
- ``response``  ``litellm.ModelResponse.model_dump()`` (completion) or
  ``litellm.EmbeddingResponse.model_dump()`` (embedding), serialised to dict.
  On replay the dict is reconstructed as the matching litellm response type.

Environment preconditions (#3473)
---------------------------------
A replay key contains the SCENARIO; the ENVIRONMENT is a checked PRECONDITION,
not a key component. ``reyn.dev.testing.replay_preconditions`` owns that
framework and its module docstring owns the rationale; the three touchpoints
here are:

1. :meth:`LLMReplay.key` scrubs each precondition's imprint out of ``tools``
   before hashing (a no-op, hence key-preserving, on a payload where the
   imprint is absent).
2. Record mode stores the imprint per entry plus one ``"environment"`` line
   per precondition that captured a snapshot; replay mode compares the
   imprints and raises :class:`PreconditionMismatch` NAMING the difference.
3. :meth:`install` injects the captured snapshots, so replay runs against the
   environment the fixture was captured under rather than whatever this
   machine happens to produce under load.

A pre-#3473 entry carries no ``preconditions`` field. It is served only when
this run's imprint is empty — i.e. only when the key it was recorded under is
byte-identical to the key computed now. Serving it under a non-empty
environment would be replaying a response recorded against different tooling,
which is the failure this whole mechanism exists to make impossible.

Record mode
-----------
Set ``REYN_LLM_RECORD=1`` before running pytest to call the real LLM/embedding
provider and write fixtures — including first-run fixture generation. #3662:
a missing fixture file no longer activates record mode on its own (a fixture
absent for an unrelated reason — an accidental delete, a bad rebase — used to
fall back to a real, unauthorized network call instead of a loud failure);
the caller (``tests/conftest.py::_llm_replay``) now requires the explicit
env var either way.

#3634: regenerating in place REPLACES a call's stale entry, not appends
alongside it. Before #3634, :meth:`flush` only ever appended, so re-recording
a call whose TOOL SCHEMA changed (including a description-only edit — the
schema hash covers the whole tool payload) produced a new key while the old
entry stayed on disk; the fixture then matched both the old and the new
schema and never went RED again, regardless of which one the code actually
implements. See ``reyn.dev.testing.replay_stacking`` for the grouping rule
that lets :meth:`flush` tell "this session re-recorded that exact call under
a new key" (drop the old one) from "a different call recorded by a sibling
test sharing this same fixture file" (keep it) — and
``tests/dev/test_replay_fixture_no_stacking_3634.py`` for the CI gate that fails
if any committed fixture holds a stacked group anyway.

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
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from reyn.dev.testing.replay_key_diff import explain_miss, fingerprint
from reyn.dev.testing.replay_preconditions import (
    EnvironmentPrecondition,
    ReplayRequest,
    default_preconditions,
)
from reyn.dev.testing.replay_stacking import group_signature

if TYPE_CHECKING:
    pass

class MissingFixture(Exception):
    """Raised in replay mode when no fixture entry matches the call."""


class PreconditionMismatch(MissingFixture):
    """A fixture entry EXISTS for this scenario, but the environment differs.

    #3473: the two are genuinely different diagnoses — "this conversation was
    never recorded" versus "this conversation was recorded, under a different
    machine state" — and collapsing them into one message is what made #3473
    take three sessions to attribute. Subclassing keeps existing
    ``except MissingFixture`` handlers (``reyn.dev.dogfood.replay``) working
    and, because the message names the difference, makes their logs strictly
    more informative than before.
    """


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
    preconditions:
        Environment preconditions (#3473) — the values kept OUT of the key
        and checked instead. Defaults to
        ``replay_preconditions.default_preconditions()``. Pass an explicit
        tuple to point one at a non-default location (e.g. a session whose
        state dir is not CWD-relative), or ``()`` to disable checking
        entirely — which also disables scrubbing, so ``()`` reproduces the
        pre-#3473 key exactly.
    """

    def __init__(
        self,
        fixture_path: Path,
        mode: Literal["replay", "record"],
        preconditions: "Sequence[EnvironmentPrecondition] | None" = None,
    ) -> None:
        self.fixture_path = fixture_path
        self.mode = mode
        self._preconditions: tuple[EnvironmentPrecondition, ...] = tuple(
            default_preconditions() if preconditions is None else preconditions
        )
        # precondition name → captured environment snapshot (kind="environment")
        self._environment: dict[str, Any] = {}
        # key → {precondition name: observed imprint} (completion entries).
        # A key absent from this map was recorded before #3473.
        self._entry_preconditions: dict[str, dict] = {}
        # Per-component key fingerprints of the recorded completion entries —
        # what a miss is attributed against (#3473).
        self._fingerprints: list[dict] = []
        # key → serialised ModelResponse dict (kind="completion")
        self._records: dict[str, dict] = {}
        # key → serialised EmbeddingResponse dict (kind="embedding")
        self._embed_records: dict[str, dict] = {}
        # #5283: keys actually served by a REPLAY hit this instance's
        # lifetime (added by ``_replay``/``_replay_embedding``, never by
        # ``_record``/``_record_embedding`` — record mode has its own
        # in-place-replace dedup via ``replay_stacking.group_signature``
        # and this set answers a different question). Read by
        # ``reyn.dev.testing.replay_unconsumed`` after this instance's
        # ``restore()`` to report entries that exist in ``self.fixture_path``
        # but were never consumed anywhere this session — see that module's
        # docstring for why observing consumption (not enumerating what
        # COULD change a key) is what closes #5283's class of stacking.
        self._consumed_keys: set[str] = set()
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
        preconditions: "Sequence[EnvironmentPrecondition] | None" = None,
    ) -> str:
        """Return the SHA-256 cache key for an acompletion call.

        #3473: the key is the SCENARIO. Every environment precondition's
        imprint is scrubbed out of ``tools`` before hashing, so a value that
        varies with the machine rather than the conversation cannot turn a
        replay into a `MissingFixture`. What is scrubbed is not discarded —
        it is recorded and checked (see the module docstring). Scrubbing is a
        no-op on a payload carrying no imprint, so a fixture recorded on a
        machine where the environment never materialised keeps its key
        byte-identical.

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
        request = ReplayRequest(
            model=model, messages=messages, tools=tools, tool_choice=tool_choice,
        )
        for precondition in (
            default_preconditions() if preconditions is None else preconditions
        ):
            request = precondition.scrub(request)
        model, messages = request.model, request.messages
        tools, tool_choice = request.tools, request.tool_choice
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
        kind that existed before aembedding coverage was added). #3473 adds the
        ``"environment"`` kind (a captured snapshot, keyed by precondition name
        rather than by a call) and the per-entry ``"preconditions"`` field."""
        if not self.fixture_path.exists():
            return
        for raw_line in self.fixture_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
                kind = entry.get("kind", "completion")
                if kind == "embedding":
                    self._embed_records[entry["key"]] = entry["response"]
                elif kind == "environment":
                    self._environment[str(entry["name"])] = entry["value"]
                else:
                    self._records[entry["key"]] = entry["response"]
                    recorded = entry.get("preconditions")
                    if isinstance(recorded, dict):
                        self._entry_preconditions[entry["key"]] = recorded
                    components = entry.get("key_components")
                    if isinstance(components, dict):
                        self._fingerprints.append(components)
            except Exception:
                # Skip corrupt lines — fixture is a test artifact; silent skip
                # is acceptable (same policy as BudgetLedger).
                pass

    def flush(self) -> None:
        """Write pending record-mode entries to the fixture file.

        #3634: REPLACES, not appends. A naive append made regenerating a
        fixture in place — the documented procedure — stack schema
        generations: re-recording a call whose tool schema changed produced
        a NEW key while the OLD entry (recorded against the old schema)
        stayed on disk, so the fixture then matched both generations and
        stopped measuring anything (a stale fixture goes RED and gets
        noticed; a stacked one stays GREEN). The fix drops three kinds of
        on-disk entry before writing:

        1. Any entry whose ``key`` matches a key THIS session (re-)recorded —
           an unchanged call re-recorded verbatim would otherwise leave a
           byte-identical duplicate line every time (harmless to a replay
           lookup, which collapses same-key entries into one dict slot, but
           still fixture bloat / git-diff noise for no reason).
        2. Any completion entry whose :func:`replay_stacking.group_signature`
           (model + tool_choice + per-message digests, i.e. everything the
           key hashes over EXCEPT ``tools`` — the one component a schema
           change is expected to move) matches an entry this session
           recorded — that is an EARLIER generation of a call this run
           superseded, the #3634 stacking case proper.
        3. Any "environment" entry whose ``name`` matches a precondition
           THIS session captured (#3969) — an environment entry carries no
           ``key`` at all (it's keyed by precondition ``name``, one snapshot
           per precondition), so rules 1/2 structurally cannot see it; #3634's
           own fix never covered this kind, so every record-mode flush
           appended a fresh "environment" line without ever dropping the
           stale one, unboundedly, across every re-recording.

        Every other on-disk entry (a genuinely different call, e.g. one
        recorded by a SIBLING test sharing this same fixture file —
        ``llm_tools/text_only.jsonl`` is read by four separate tests) is
        preserved untouched: this is a surgical replace of only what this
        session (re-)recorded, not a wholesale truncate, so cross-test
        accumulation into one shared fixture file still works. An on-disk
        entry predating #3473 carries no ``key_components`` fingerprint and
        so cannot be grouped by rule 2 — it is preserved unconditionally
        unless rule 1 (exact key match) applies, degrading to the old
        append-only safety rather than silently deleting un-groupable data.

        #3473: in RECORD mode each precondition is asked to :meth:`capture` the
        live environment here — at the END of recording, when whatever
        populated it (an MCP probe, a catalog refresh) has had the whole
        recorded run to do so. The snapshots are written as ``"environment"``
        lines and are what a later replay injects. A replay-mode flush
        captures nothing: it would append this machine's environment to a
        committed fixture, which is the opposite of the guarantee. (In
        practice a replay-mode flush is always a no-op: only record-mode
        calls ever populate ``self._pending``.)
        """
        captured = [
            {"kind": "environment", "name": precondition.name, "value": snapshot}
            for precondition, snapshot in (
                (p, p.capture()) for p in self._preconditions
            )
            if snapshot is not None
        ] if self.mode == "record" else []
        if not self._pending and not captured:
            return
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)

        if self.mode != "record":
            # Unreachable in practice (see docstring) — kept as the safe
            # append fallback rather than silently rewriting a fixture no
            # replay-mode caller asked to change.
            with self.fixture_path.open("a", encoding="utf-8") as fh:
                for entry in [*captured, *self._pending]:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._pending.clear()
            return

        session_groups = {
            group_signature(entry["key_components"])
            for entry in self._pending
            if entry.get("kind", "completion") == "completion"
            and isinstance(entry.get("key_components"), dict)
        }
        # Every key this session (re-)recorded, completion AND embedding
        # alike -- an on-disk entry sharing one of these keys is about to be
        # rewritten verbatim by the fresh pending copy, so keeping the old
        # line too would be a byte-identical duplicate (harmless to replay,
        # since a dict lookup collapses it, but still a fixture that grows a
        # line every time an UNCHANGED call is re-recorded).
        session_keys = {entry["key"] for entry in self._pending}
        # #3969: "environment" entries carry no ``key`` at all — they're
        # keyed by ``name`` (one snapshot per registered precondition), so
        # the key/group_signature machinery above structurally cannot see
        # them. Without this, every record-mode flush() appended a fresh
        # "environment" line without ever dropping the stale one (the same
        # failure #3634 fixed for completion entries, just on a kind #3634's
        # own fix never looked at — found by tui-coder re-recording #3967's
        # fixture). ``captured`` is this call's freshly-captured snapshots
        # (always present in record mode, one per registered precondition —
        # see above); an on-disk "environment" entry whose name matches one
        # of them is the stale prior generation.
        captured_names = {entry["name"] for entry in captured}

        kept_existing: list[dict[str, Any]] = []
        if self.fixture_path.exists():
            for raw_line in self.fixture_path.read_text(encoding="utf-8").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except Exception:
                    # Corrupt line — same silent-skip policy as `_load`.
                    continue
                kind = entry.get("kind", "completion")
                if kind == "environment":
                    if entry.get("name") in captured_names:
                        continue  # this session re-captured this precondition — drop the old copy
                    kept_existing.append(entry)
                    continue
                key = entry.get("key")
                if key in session_keys:
                    continue  # this session re-recorded this exact call — drop the old copy
                if kind == "completion":
                    components = entry.get("key_components")
                    if (
                        isinstance(components, dict)
                        and group_signature(components) in session_groups
                    ):
                        continue  # stale earlier-generation duplicate — drop
                kept_existing.append(entry)

        with self.fixture_path.open("w", encoding="utf-8") as fh:
            for entry in [*kept_existing, *captured, *self._pending]:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._pending.clear()

    # ── Monkeypatch lifecycle ──────────────────────────────────────────────────

    def install(self) -> None:
        """Replace ``litellm.acompletion`` AND ``litellm.aembedding`` with this
        instance's handlers (#3451 — both boundaries, one Fake).

        #3473: in replay mode this first INJECTS every captured environment
        snapshot, so the run's environment is the fixture's environment by
        construction rather than by luck. Injection is a direct write of the
        recorded value — never a wait, a retry or a widened deadline, all of
        which only make "in time today" more likely instead of removing the
        dependence on timing.
        """
        import litellm

        if self.mode == "replay":
            for precondition in self._preconditions:
                snapshot = self._environment.get(precondition.name)
                if snapshot is not None:
                    precondition.inject(snapshot)

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

    def loaded_keys(self) -> "set[str]":
        """Every completion/embedding key this instance loaded from
        ``self.fixture_path`` at construction (#5283) — the universe
        ``reyn.dev.testing.replay_unconsumed`` diffs ``self._consumed_keys``
        against. Not affected by anything recorded THIS session (record
        mode's new entries are never what #5283 is asking about)."""
        return set(self._records) | set(self._embed_records)

    def consumed_keys(self) -> "set[str]":
        """Keys actually served by a replay hit this instance's lifetime
        (#5283) — see ``self._consumed_keys``'s own comment."""
        return set(self._consumed_keys)

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
        key = self.key(
            model, messages, tools=tools, tool_choice=tool_choice,
            preconditions=self._preconditions,
        )
        request = ReplayRequest(
            model=model, messages=messages, tools=tools, tool_choice=tool_choice,
        )
        observed = {p.name: p.observe(request) for p in self._preconditions}
        stream = bool(kwargs.get("stream"))

        if self.mode == "replay":
            response = self._replay(key, model, messages, observed, request)
        else:
            # record mode: always fetch/store the WHOLE response — strip
            # ``stream``/``stream_options`` from the forwarded call so the
            # real LLM call + fixture format are unaffected by what THIS
            # call happened to request.
            record_kwargs = {
                k: v for k, v in kwargs.items() if k not in ("stream", "stream_options")
            }
            response = await self._record(
                key, model, messages, record_kwargs, observed, request,
            )

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
        self._consumed_keys.add(key)
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

    def _replay(
        self,
        key: str,
        model: str,
        messages: list[dict],
        observed: dict[str, Any],
        request: ReplayRequest,
    ) -> Any:
        """Return a reconstructed ``ModelResponse`` from the fixture.

        #3473: three distinct diagnoses, reported distinctly. A key miss is
        "this conversation was never recorded" — and it now carries a
        per-component attribution (``replay_key_diff``) naming WHICH of the
        key's four components moved, because a report scoped to the cause
        someone already suspects can confirm that cause but never reject it.
        A key hit whose recorded environment differs from ``observed`` is
        "this conversation WAS recorded, on a differently-equipped machine" —
        that one raises :class:`PreconditionMismatch`, because a fixture
        replayed under different tooling would answer as if the model had
        been offered capabilities it was not. A hit with a matching
        environment is served.
        """
        if key not in self._records:
            preview = self._prompt_preview(messages)
            attribution = explain_miss(
                fingerprint(
                    request.model, request.messages, request.tools, request.tool_choice,
                ),
                self._fingerprints,
            )
            raise MissingFixture(
                f"No fixture entry for model={model!r}.\n"
                f"Prompt preview: {preview!r}\n"
                f"Fixture: {self.fixture_path}\n"
                f"{attribution}\n"
                f"This run's environment preconditions: {observed!r}\n"
                f"Re-run with REYN_LLM_RECORD=1 to record new fixtures."
            )
        self._check_preconditions(key, model, messages, observed)
        self._consumed_keys.add(key)
        import litellm

        return litellm.ModelResponse(**self._records[key])

    def _check_preconditions(
        self, key: str, model: str, messages: list[dict], observed: dict[str, Any],
    ) -> None:
        """Raise :class:`PreconditionMismatch` if this run's environment differs."""
        recorded = self._entry_preconditions.get(key)
        for precondition in self._preconditions:
            actual = observed.get(precondition.name)
            if recorded is None:
                # A pre-#3473 entry records no imprint, so there is nothing to
                # compare against. An empty imprint is still safe to serve —
                # scrubbing was a no-op, so this key IS the key it was recorded
                # under. A non-empty one is not: the response would be replayed
                # against tooling the recording never saw. Reported as
                # "not recorded", never as "captured empty" — the two are
                # different claims and only one of them was observed.
                if actual == precondition.absent_value():
                    continue
                raise PreconditionMismatch(
                    f"Fixture precondition unverifiable: {precondition.name!r} — "
                    f"this entry predates environment-precondition recording "
                    f"(#3473) and this run's environment is not empty, so there "
                    f"is nothing to check it against.\n"
                    f"Expected (captured): NOT RECORDED\n"
                    f"Actual   (this run): {actual!r}\n"
                    f"Model: {model!r}\n"
                    f"Prompt preview: {self._prompt_preview(messages)!r}\n"
                    f"Fixture: {self.fixture_path}\n"
                    f"Serving it would replay a response recorded against "
                    f"unknown tooling. Re-record with REYN_LLM_RECORD=1."
                )
            expected = recorded.get(precondition.name, precondition.absent_value())
            if actual == expected:
                continue
            raise PreconditionMismatch(
                f"Fixture precondition mismatch: {precondition.name!r} — this "
                f"fixture entry was captured under a different environment.\n"
                f"{precondition.describe_mismatch(expected, actual)}\n"
                f"Expected (captured): {expected!r}\n"
                f"Actual   (this run): {actual!r}\n"
                f"Model: {model!r}\n"
                f"Prompt preview: {self._prompt_preview(messages)!r}\n"
                f"Fixture: {self.fixture_path}\n"
                f"The conversation matched — only the environment did not, so "
                f"this is NOT a missing recording. Either restore the recorded "
                f"environment (an injected snapshot does this automatically when "
                f"the fixture carries one) or re-record with REYN_LLM_RECORD=1."
            )

    async def _record(
        self,
        key: str,
        model: str,
        messages: list[dict],
        extra_kwargs: dict,
        observed: dict[str, Any],
        request: ReplayRequest,
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
            # #3473: what was scrubbed out of the key, kept so replay can check
            # it. Recorded unconditionally (including the empty imprint) — an
            # absent field is what marks a pre-#3473 entry, so writing one only
            # when non-empty would make new empty entries indistinguishable
            # from old unchecked ones.
            "preconditions": observed,
            # #3473: the per-component fingerprint a later miss is attributed
            # against. Taken over the RAW request, not the scrubbed key input —
            # on a miss the reader wants everything that moved, including what
            # the key deliberately ignores.
            "key_components": fingerprint(
                request.model, request.messages, request.tools, request.tool_choice,
            ),
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
