"""
ConsoleLogger — event subscriber that renders OS events as human-readable console output.

Wire up by passing an instance as a subscriber to EventLog or Agent.
"""
from __future__ import annotations
from reyn.models import Event


class ConsoleLogger:
    """Callable subscriber that prints a concise log line for each relevant event."""

    def __init__(self, conversation: bool = False) -> None:
        self.conversation = conversation

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    # ── Workflow ───────────────────────────────────────────────────────────────

    def on_workflow_terminated(self, data: dict) -> None:
        print("[os] loop limit reached — returning latest artifact")

    def on_workflow_aborted(self, data: dict) -> None:
        print(f"[os] workflow aborted — {data.get('reason', '')}")

    # ── Phase lifecycle ────────────────────────────────────────────────────────

    def on_phase_started(self, data: dict) -> None:
        print(f"[phase:{data['phase']}] started (visit #{data['visit_count']})")

    def on_phase_retry(self, data: dict) -> None:
        error = data.get("error", "")
        print(
            f"[phase:{data['phase']}] retry {data['attempt']}/{data['max_retries']}"
            f" — {error[:120]}"
        )

    def on_phase_completed(self, data: dict) -> None:
        phase = data["phase"]
        next_phase = data.get("next", "?")
        confidence = data.get("confidence", 0.0)
        was_normalized = data.get("was_normalized", False)
        was_inferred = data.get("was_inferred", False)
        retries = data.get("retries", 0)
        artifact_path = data.get("artifact_path", "")

        norm = (
            " (inferred)" if was_inferred
            else f" (normalized from '{data.get('original_raw_type')}')" if was_normalized
            else ""
        )
        retry_str = (
            f" [{retries} retr{'y' if retries == 1 else 'ies'}]" if retries else ""
        )
        path_str = f"  → {artifact_path}" if artifact_path else ""
        print(f"[phase:{phase}] → {next_phase}{norm}{retry_str}  (confidence={confidence}){path_str}")

    def on_artifact_created(self, data: dict) -> None:
        phase = data.get("phase", "?")
        artifact_type = data.get("artifact_type", "?")
        path = data.get("path", "")
        keys = data.get("keys", [])
        keys_str = f"  keys={keys}" if keys else ""
        print(f"[artifact:{artifact_type}] phase={phase}{keys_str}  saved → {path}")

    # ── LLM ───────────────────────────────────────────────────────────────────

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
        print(f"[phase:{data['phase']}] calling LLM ({data.get('model', '?')})...")

    def on_context_built(self, data: dict) -> None:
        if not self.conversation:
            return
        import json as _json
        frame = data.get("frame", {})
        phase = frame.get("current_phase", data.get("phase", "?"))
        role = frame.get("current_phase_role") or ""
        execution = frame.get("execution", {})
        visit = execution.get("current_visit", 1)
        total = execution.get("total_steps", 0)
        path = execution.get("path", [])

        print(f"\n{'='*70}")
        role_str = f"  role={role}" if role else ""
        print(f"[LLM INPUT]  phase={phase}{role_str}  visit={visit}  total_steps={total}")
        print(f"{'='*70}")

        if path:
            print("  path: " + " → ".join(path))

        instructions = frame.get("instructions", "")
        if instructions:
            print("  --- instructions ---")
            for line in instructions.splitlines():
                print(f"  {line}")

        artifact = frame.get("input_artifact", {})
        if artifact:
            print("  --- input_artifact ---")
            print(_json.dumps(artifact, ensure_ascii=False, indent=4)
                  .replace("\n", "\n  "))

        candidates = frame.get("candidate_outputs", [])
        if candidates:
            print("  --- candidates ---")
            for c in candidates:
                desc = f"  {c.get('description', '')}" if c.get("description") else ""
                print(f"    next={c.get('next_phase')}  schema={c.get('schema_name')}{desc}")

        finish_criteria = frame.get("finish_criteria", [])
        if finish_criteria:
            print("  --- finish_criteria ---")
            for fc in finish_criteria:
                print(f"    {fc}")

        ir_results = frame.get("control_ir_results", [])
        if ir_results:
            print("  --- control_ir_results (act re-call) ---")
            print(_json.dumps(ir_results, ensure_ascii=False, indent=4)
                  .replace("\n", "\n  "))

    def on_llm_response_received(self, data: dict) -> None:
        if not self.conversation:
            return
        phase = data.get("phase", "?")
        raw = data.get("raw", {})
        print(f"\n[LLM OUTPUT] phase={phase}  type={data.get('response_type', '?')}")
        import json
        print(json.dumps(raw, ensure_ascii=False, indent=2))

    # ── Act turn ──────────────────────────────────────────────────────────────

    def on_act_executed(self, data: dict) -> None:
        phase = data["phase"]
        turn = data.get("act_turn", "?")
        print(f"[phase:{phase}] act turn #{turn}")
        for op in data.get("ops", []):
            kind = op.get("kind")
            if kind == "file":
                print(f"  op: file {op.get('op')} → {op.get('path')}")
            elif kind == "ask_user":
                q = (op.get("question") or "")[:80]
                print(f"  op: ask_user → {q}")
            else:
                print(f"  op: {kind}")
        for r in data.get("results", []):
            kind = r.get("kind")
            status = r.get("status", "?")
            if kind == "file" and r.get("op") == "read":
                content_len = len(r.get("content", ""))
                print(f"  result: file read {r.get('path')} [{status}] ({content_len} chars)")
            elif kind == "file" and r.get("op") == "glob":
                print(f"  result: file glob {r.get('pattern')} [{status}] ({r.get('count', 0)} matches)")
            elif kind == "file":
                print(f"  result: file write {r.get('path')} [{status}]")
            elif kind == "ask_user":
                answer = r.get("answer", "")
                print(f"  result: ask_user [{status}] answer={answer!r}")
            elif kind == "lint":
                passed = "passed" if r.get("passed") else f"{r.get('error_count', 0)} errors"
                print(f"  result: lint {r.get('app_path')} [{status}] {passed}, {r.get('warning_count', 0)} warnings")
            elif kind == "eval":
                score = r.get("overall_score", 0.0)
                pc = r.get("passed_criteria", 0)
                tc = r.get("total_criteria", 0)
                print(f"  result: eval {r.get('spec_path')} [{status}] score={score:.2f} ({pc}/{tc})")
            else:
                print(f"  result: {kind} [{status}]")

    # ── User intervention ──────────────────────────────────────────────────────

    def on_user_intervention_requested(self, data: dict) -> None:
        print(f"\n[ask_user] {data.get('question', '')}")
        suggestions = data.get("suggestions") or []
        if suggestions:
            suggestions_str = " / ".join(f'"{s}"' for s in suggestions)
            print(f"  Suggestions: {suggestions_str}")
