"""
ConsoleLogger — event subscriber that renders OS events as human-readable console output.

Wire up by passing an instance as a subscriber to EventLog.
"""
from __future__ import annotations

from reyn.schemas.models import Event


class ConsoleLogger:
    """Callable subscriber that prints a concise log line for each relevant event."""

    def __init__(self, conversation: bool = False) -> None:
        self.conversation = conversation

    def __call__(self, event: Event) -> bool:
        """Render *event*. Returns whether anything was actually printed
        (#4383) — ``reyn events`` replay uses this to report how many
        MATCHED events actually RENDERED, not just how many were found.

        Before this: an event type with no dedicated ``on_<type>`` handler
        was silently dropped (only 11 of 211 declared audit-event kinds
        have one — 5.2%), and the replay summary's own count included the
        dropped ones too, so "N events replayed" read as "N shown" when it
        actually meant "N found, possibly 0 shown" — the owner's own
        real-environment report: 21 events matched, 0 lines printed, no
        indication anything had been dropped at all.

        A handler-less type now falls through to :meth:`_render_unhandled`
        (a generic one-line fallback) rather than being dropped — this
        method wraps the handler call in a stdout capture (not a change to
        any of the 11 existing handlers' own bodies) purely to detect
        whether printing genuinely happened, for the caller's own matched-
        vs-rendered accounting; the captured output is still written to
        the real stdout unchanged, byte for byte.
        """
        import io
        from contextlib import redirect_stdout

        handler = getattr(self, f"on_{event.type}", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            if handler is not None:
                handler(event.data)
            else:
                self._render_unhandled(event)
        output = buf.getvalue()
        if output:
            import sys
            sys.stdout.write(output)
        return bool(output)

    def _render_unhandled(self, event: Event) -> None:
        """#4383: generic one-line fallback for a declared audit-event kind
        this module has no dedicated ``on_<type>`` renderer for (~200 of
        211 kinds, before this fix silently dropped by :meth:`__call__`).
        Prints the kind plus a bounded set of its own top-level data
        fields as ``key=value`` — never a full JSON dump (a replay is meant
        to be skimmed line by line, not re-inspected as raw payload; an
        operator who needs the full record already has the source JSONL).
        Bounded to the first 6 fields so one wide event (e.g. a payload
        carrying a large embedded blob) cannot turn a scan of thousands of
        events into an unreadable wall of text."""
        fields = " ".join(f"{k}={v!r}" for k, v in list(event.data.items())[:6])
        print(f"[{event.type}] {fields}".rstrip())

    # ── Workflow ───────────────────────────────────────────────────────────────

    def on_workflow_terminated(self, data: dict) -> None:
        reason = data.get("reason", "limit reached")
        print(f"[os] workflow terminated — {reason} — returning latest artifact")

    def on_workflow_aborted(self, data: dict) -> None:
        print(f"[os] workflow aborted — {data.get('reason', '')}")

    # ── Shell ─────────────────────────────────────────────────────────────────

    def on_shell_started(self, data: dict) -> None:
        cmd = data.get("cmd", "")
        timeout = data.get("timeout", 120)
        print(f"  [shell] {cmd[:120]}  (timeout={timeout}s)")

    def on_shell_completed(self, data: dict) -> None:
        rc = data.get("returncode", "?")
        stdout_len = data.get("stdout_len", 0)
        stderr_len = data.get("stderr_len", 0)
        status = "ok" if rc == 0 else "error"
        print(f"  [shell] [{status}] returncode={rc}  stdout={stdout_len}chars  stderr={stderr_len}chars")

    def on_shell_timeout(self, data: dict) -> None:
        print(f"  [shell] TIMEOUT after {data.get('timeout', '?')}s — {data.get('cmd', '')[:80]}")

    # ── LLM ───────────────────────────────────────────────────────────────────

    def on_llm_called(self, data: dict) -> None:
        print(f"[llm] calling {data.get('model', '?')}...")

    # ``--conversation`` mode (below) filters replay to ``context_built`` +
    # ``llm_response_received``. #2696 finding: **``context_built`` has no
    # producer.** Nothing in ``src/`` emits it — the phase engine that did was
    # deleted (#2434 / #2438). This handler is therefore not "broken but
    # reconnectable": there is no wiring to restore, and making the mode useful
    # again means writing a NEW emitter for whatever the chat router's prompt
    # assembly actually looks like — a feature, not a repair. Tracked separately;
    # see #2696.
    #
    # The body was stripped to bones for exactly that reason. It used to read
    # ``frame.current_phase`` / ``current_phase_role`` / ``execution.current_visit``
    # / ``total_steps`` / ``path`` / ``candidate_outputs[].next_phase`` /
    # ``control_ir_results`` and print a ``[LLM INPUT] phase=… visit=…`` heading —
    # the deleted phase engine's ContextFrame shape. Left intact, it would have
    # taught the next author that phase headings are the target output shape and
    # led them to reintroduce the vocabulary into a phase-less system.
    def on_context_built(self, data: dict) -> None:
        if not self.conversation:
            return
        import json as _json
        print(f"\n{'='*70}")
        print("[LLM INPUT]")
        print(f"{'='*70}")
        print(_json.dumps(data, ensure_ascii=False, indent=2, default=repr))

    # ``llm_response_received`` IS live (``llm.py::_emit_chat_cost_events``), but
    # it carries usage/cost only — never the ``phase``, ``raw`` or
    # ``response_type`` keys this used to read, so ``--conversation`` printed a
    # constant ``[LLM OUTPUT] phase=?  type=?`` + ``{}`` for every call. Now
    # prints what the event actually carries (#2696).
    def on_llm_response_received(self, data: dict) -> None:
        if not self.conversation:
            return
        import json
        print("\n[LLM OUTPUT]")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=repr))

    # ── Present (FP-0054 §8) ────────────────────────────────────────────────────

    def on_presented(self, data: dict) -> None:
        """Re-render a ``presented`` event on replay (§8: presentation is a cache, the
        event is the truth). Best-effort re-render from the still-durable ``data_ref``;
        an expired / inline ref shows a placeholder pointing at this audit event. Never
        raises — a re-render failure degrades to the header line, never a crashed replay.
        Display-only: nothing authoritative is reconstructed from the event."""
        from reyn.core.present import replay_presentation

        try:
            replayed = replay_presentation(data)
        except Exception:  # noqa: BLE001 — replay must never crash on one bad event
            print(f"[present] view={data.get('view', '?')} "
                  f"data_ref={data.get('data_ref', '?')} (re-render unavailable)")
            return
        print(replayed.header)
        for line in replayed.lines:
            print(f"  {line}")

    # ── User intervention ──────────────────────────────────────────────────────

    def on_user_intervention_requested(self, data: dict) -> None:
        print(f"\n[ask_user] {data.get('question', '')}")
        suggestions = data.get("suggestions") or []
        if suggestions:
            suggestions_str = " / ".join(f'"{s}"' for s in suggestions)
            print(f"  Suggestions: {suggestions_str}")
