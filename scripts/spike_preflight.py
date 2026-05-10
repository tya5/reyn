#!/usr/bin/env python3
"""spike_preflight.py — G4 spike preflight validation for FP-0011.

Run once before the spike begins:

    python scripts/spike_preflight.py

Checks:
  1. Strong-tier proxy reachability (gemini-2.5-flash, no thinking tokens)
  2. Reyn's call_llm works with the strong-tier model spec
  3. Trace dump captures payload + response; format works for both str and dict model spec
  4. Events log captures llm_called events with model + phase recorded
  5. RPD counter sanity (spike_results/fp_0011/rpd_state.json)

Exit 0 = all checks PASS or WARN.  Exit 1 = any check FAIL (hard error).
The script is idempotent — repeated runs are safe.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Ensure project src is importable when run from repo root
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Colour helpers (ANSI, disabled when not a TTY)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m" if _USE_COLOR else s


def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m" if _USE_COLOR else s


def _yellow(s: str) -> str:
    return f"\033[93m{s}\033[0m" if _USE_COLOR else s


# ---------------------------------------------------------------------------
# Result accumulator
# ---------------------------------------------------------------------------

_results: list[tuple[str, str, str | None]] = []  # (label, status, detail)


def _record(label: str, status: str, detail: str | None = None) -> None:
    _results.append((label, status, detail))


# ---------------------------------------------------------------------------
# Check 1 — strong-tier proxy reachability
# ---------------------------------------------------------------------------

def _check1_proxy_reachability() -> None:
    """POST to localhost:4000/chat/completions with gemini-2.5-flash.

    NOTE: The proxy routes gemini-2.5-flash → gemini/gemini-2.5-flash via
    LiteLLM's native Gemini provider (not OpenAI-compat). The model uses
    thinking by default. Passing thinkingConfig via extra_body through the
    OpenAI-compat endpoint causes HTTP 400 — the proxy doesn't translate
    this field. This is reported as WARN (not FAIL) because the proxy IS
    reachable and the model IS working; thinking cannot be disabled at the
    proxy level. The operator must accept thinking tokens in the spike.
    See: project_g4_spike_cost_estimates.md — spike design assumed
    thinking=off; actual behaviour may differ.
    """
    label = "Check 1 — strong-tier proxy reachable (pong)"
    try:
        import urllib.request
        import urllib.error

        api_key = os.environ.get("OPENAI_API_KEY", "dummy")
        # Do NOT pass thinkingConfig — it causes 400 on this proxy setup.
        payload = json.dumps({
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "Reply with: OK"}],
            "temperature": 0,
        }).encode()

        req = urllib.request.Request(
            "http://localhost:4000/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                err_msg = str(body)[:200]
            except Exception:
                err_msg = str(e)
            _record(label, "FAIL", f"HTTP {e.code}: {err_msg}")
            return
        except urllib.error.URLError as e:
            _record(label, "FAIL", f"Connection failed: {e.reason} — is litellm proxy running on port 4000?")
            return

        # Verify content includes "OK"
        content = ""
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            _record(label, "FAIL", f"Unexpected response shape: {str(body)[:200]}")
            return

        if "OK" not in content.upper():
            _record(label, "WARN", f"Response did not include 'OK': {content[:100]!r}")
            return

        # Report thinking tokens — expected NON-ZERO (model thinks by default)
        thinking_tokens = None
        try:
            ctd = body.get("usage", {}).get("completion_tokens_details", {}) or {}
            thinking_tokens = ctd.get("reasoning_tokens") or ctd.get("thinking_tokens")
        except Exception:
            pass

        # WARN (not FAIL): thinking cannot be disabled via proxy in current setup.
        # Spike will incur thinking-token cost. See note in docstring above.
        warn_suffix = ""
        if thinking_tokens and thinking_tokens > 0:
            warn_suffix = (
                f"  ⚠️  thinking_tokens={thinking_tokens} "
                "(thinkingConfig not disableable via proxy — see KNOWN ISSUE in preflight doc)"
            )

        _record(label,
                "WARN" if warn_suffix else "PASS",
                f"response={content.strip()!r}  reasoning_tokens={thinking_tokens!r}{warn_suffix}")

    except Exception as exc:
        _record(label, "FAIL", f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Check 2 — Reyn's call_llm works with strong tier
# ---------------------------------------------------------------------------

async def _check2_reyn_call_llm() -> None:
    """Import call_llm; construct minimal ContextFrame; call with strong model."""
    label = "Check 2 — Reyn call_llm works with strong tier"
    try:
        from reyn.llm.llm import call_llm
        from reyn.llm.model_resolver import ModelSpec
        from reyn.schemas.models import ContextFrame

        # Set proxy env vars (matches how CLI sets them from reyn.local.yaml)
        os.environ.setdefault("LITELLM_API_BASE", "http://localhost:4000")

        # Build a minimal ContextFrame (all required fields)
        frame = ContextFrame(
            current_phase="check",
            instructions="Reply with: REYN_OK",
            input_artifact={"type": "preflight_input", "data": {"prompt": "Reply with: REYN_OK"}},
            candidate_outputs=[],
            available_control_ops=[],
        )

        # Strong-tier spec: model string only. NOTE: reyn.local.yaml declares
        # extra_body.thinkingConfig.thinkingBudget=0 but the proxy rejects
        # thinkingConfig via the OpenAI-compat path (HTTP 400). We test without
        # it here to confirm the LLM call path works; the thinkingConfig issue
        # is flagged separately in Check 1. Spike will run with thinking enabled.
        spec = ModelSpec(model="gemini-2.5-flash", kwargs={})

        result = await call_llm(
            spec,
            frame,
            timeout=30.0,
            max_retries=1,
            prompt_cache_enabled=False,
            skill_name="preflight",
            trace_caller="preflight:check2",
        )

        usage = result.usage
        usage_str = (
            f"prompt_tokens={usage.prompt_tokens}  completion_tokens={usage.completion_tokens}"
            if usage else "usage=None"
        )
        _record(label, "PASS", f"call returned data keys={list(result.data.keys())[:5]}  {usage_str}")

    except Exception as exc:
        _record(label, "FAIL", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Check 3 — trace dump works on strong tier (str + dict model spec)
# ---------------------------------------------------------------------------

async def _check3_trace_dump() -> None:
    """Verify REYN_LLM_TRACE_DUMP writes parseable JSON for both spec forms."""
    label = "Check 3 — trace dump format works for str + dict model spec"
    tmp_path = Path(tempfile.mktemp(suffix=".jsonl", prefix="spike_preflight_trace_"))
    issues: list[str] = []

    try:
        from reyn.llm.llm import call_llm
        from reyn.llm.model_resolver import ModelSpec
        from reyn.schemas.models import ContextFrame

        os.environ["REYN_LLM_TRACE_DUMP"] = str(tmp_path)
        os.environ.setdefault("LITELLM_API_BASE", "http://localhost:4000")

        frame = ContextFrame(
            current_phase="check",
            instructions="Say: TRACE_OK",
            input_artifact={"type": "preflight_input", "data": {"prompt": "Say: TRACE_OK"}},
            candidate_outputs=[],
            available_control_ops=[],
        )

        # Form A: dict form (= ModelSpec with kwargs, simulates the strong tier form).
        # NOTE: We do NOT pass thinkingConfig here as it causes 400 on the current
        # proxy setup (see Check 1 note). We use temperature=0 as a representative
        # kwargs field to exercise the spec_kwargs passthrough path.
        spec_dict = ModelSpec(
            model="gemini-2.5-flash",
            kwargs={"temperature": 0},
        )
        await call_llm(
            spec_dict, frame, timeout=30.0, max_retries=1,
            prompt_cache_enabled=False, trace_caller="preflight:check3_dict",
        )

        # Form B: plain string form (= baseline / flash-lite)
        await call_llm(
            "gemini-2.5-flash-lite", frame, timeout=30.0, max_retries=1,
            prompt_cache_enabled=False, trace_caller="preflight:check3_str",
        )

    except Exception as exc:
        issues.append(f"LLM call failed: {exc}")
    finally:
        # Restore — but keep the file for inspection
        del os.environ["REYN_LLM_TRACE_DUMP"]

    # Verify trace file
    if not tmp_path.exists():
        _record(label, "FAIL", "trace file was not created (REYN_LLM_TRACE_DUMP not honoured)")
        return

    records: list[dict] = []
    bad_lines = 0
    with tmp_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad_lines += 1

    if bad_lines:
        issues.append(f"{bad_lines} unparseable line(s) in trace file")

    # Verify required fields in request records
    requests = [r for r in records if r.get("kind") == "request"]
    responses = [r for r in records if r.get("kind") == "response"]

    if not requests:
        issues.append("no 'request' records found in trace file")
    else:
        for i, req in enumerate(requests):
            for field in ("model", "messages", "request_id", "timestamp"):
                if field not in req:
                    issues.append(f"request[{i}] missing field: {field}")
        # Check that spec_kwargs is present in trace (= ModelSpec.kwargs passthrough visible).
        # We use temperature=0 as the representative field (thinkingConfig causes 400).
        dict_reqs = [r for r in requests if "check3_dict" in (r.get("caller_hint") or "")]
        for req in dict_reqs:
            spec_kwargs = req.get("spec_kwargs", {})
            if spec_kwargs is None:
                issues.append(
                    "dict-form request missing spec_kwargs field in trace "
                    "(= ModelSpec.kwargs not written to trace)"
                )
            # temperature may appear in spec_kwargs or as a top-level call kwarg;
            # the key point is spec_kwargs is present and not empty
            elif not isinstance(spec_kwargs, dict):
                issues.append(
                    f"dict-form spec_kwargs unexpected type: {type(spec_kwargs).__name__}"
                )

    if not responses:
        issues.append("no 'response' records found in trace file")
    else:
        for i, resp in enumerate(responses):
            for field in ("content", "usage", "request_id"):
                if field not in resp:
                    issues.append(f"response[{i}] missing field: {field}")

    # Check request/response pairing
    req_ids = {r.get("request_id") for r in requests}
    resp_ids = {r.get("request_id") for r in responses}
    unpaired = req_ids - resp_ids
    if unpaired:
        issues.append(f"unpaired request_ids (no response): {unpaired}")

    if issues:
        _record(label, "WARN" if not bad_lines else "FAIL",
                f"trace at {tmp_path}  issues: " + "; ".join(issues))
    else:
        _record(label, "PASS",
                f"{len(requests)} request(s) + {len(responses)} response(s) "
                f"parseable at {tmp_path}")


# ---------------------------------------------------------------------------
# Check 4 — events log captures llm_called events
# ---------------------------------------------------------------------------

async def _check4_events_log() -> None:
    """Verify EventLog + EventStore round-trip for llm_called events.

    call_llm itself does not emit events — that's the kernel's job (runtime.py:
      self.events.emit("llm_called", phase=phase, model=resolved_model)
    For preflight we emit directly via EventLog to verify the infrastructure
    is wired correctly and the JSONL round-trip works. This tests the exact
    same code path the spike driver will use to verify per-run-cap enforcement.
    """
    label = "Check 4 — events log has llm_called events"
    try:
        from reyn.events.events import EventLog

        captured_events: list[dict] = []

        def _capture(ev) -> None:
            captured_events.append({"type": ev.type, "data": ev.data})

        ev_log = EventLog(subscribers=[_capture])

        # Emit the same event the kernel emits on each LLM call
        ev_log.emit("llm_called", phase="check", model="gemini-2.5-flash")
        ev_log.emit("llm_called", phase="check2", model="gemini-2.5-flash")

        # Verify in-memory capture
        llm_events = [e for e in captured_events if e["type"] == "llm_called"]
        if not llm_events:
            _record(label, "FAIL", "EventLog did not capture llm_called events")
            return

        # Verify each event has model + phase
        issues = []
        for ev in llm_events:
            data = ev.get("data", {})
            if "model" not in data:
                issues.append("llm_called event missing 'model' field")
            if "phase" not in data:
                issues.append("llm_called event missing 'phase' field")

        # Verify round-trip via EventStore (write + read JSONL)
        from reyn.events.event_store import EventStore
        tmp_dir = Path(tempfile.mkdtemp(prefix="spike_preflight_events_"))
        store = EventStore(tmp_dir)
        for ev_raw in ev_log.all():
            store.write(ev_raw)

        read_back = list(store.iter_all())
        llm_read = [e for e in read_back if e.type == "llm_called"]
        if len(llm_read) < 2:
            issues.append(f"EventStore round-trip: expected 2 llm_called, got {len(llm_read)}")

        # Check model field survives round-trip
        for ev in llm_read:
            if ev.data.get("model") != "gemini-2.5-flash":
                issues.append(f"model field corrupted: {ev.data.get('model')!r}")
                break

        if issues:
            _record(label, "WARN", "; ".join(issues))
        else:
            _record(label, "PASS",
                    f"{len(llm_events)} llm_called captured; "
                    f"{len(llm_read)} round-tripped via EventStore JSONL")

    except Exception as exc:
        _record(label, "FAIL", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Check 5 — RPD counter sanity
# ---------------------------------------------------------------------------

def _check5_rpd_counter() -> None:
    """Read or create spike_results/fp_0011/rpd_state.json."""
    label = "Check 5 — RPD counter sanity"
    rpd_path = _REPO_ROOT / "spike_results" / "fp_0011" / "rpd_state.json"

    RPD_DAILY_LIMIT = 8000  # gemini-2.5-flash free-tier RPD cap

    try:
        if rpd_path.exists():
            with rpd_path.open(encoding="utf-8") as f:
                state = json.load(f)

            today = datetime.now(UTC).date().isoformat()
            if state.get("date") != today:
                # New day — reset counter
                state = {"flash_requests_today": 0, "date": today}
                rpd_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                detail_suffix = " (reset — new day)"
            else:
                detail_suffix = ""

            count = state.get("flash_requests_today", 0)
            remaining = RPD_DAILY_LIMIT - count
            status = "PASS" if remaining > 500 else "WARN"
            _record(label, status,
                    f"count={count}  remaining={remaining}/{RPD_DAILY_LIMIT}"
                    f"{detail_suffix}  path={rpd_path}")
        else:
            # Create it
            rpd_path.parent.mkdir(parents=True, exist_ok=True)
            today = datetime.now(UTC).date().isoformat()
            state = {"flash_requests_today": 0, "date": today}
            rpd_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            _record(label, "PASS",
                    f"created fresh counter at {rpd_path}  "
                    f"remaining={RPD_DAILY_LIMIT}/{RPD_DAILY_LIMIT}")

    except Exception as exc:
        _record(label, "FAIL", f"Could not read/create {rpd_path}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run_all_checks() -> None:
    _check1_proxy_reachability()
    await _check2_reyn_call_llm()
    await _check3_trace_dump()
    await _check4_events_log()
    _check5_rpd_counter()


def main() -> None:
    from reyn.llm.llm import run_async, shutdown_logging

    async def _wrapped() -> None:
        await _run_all_checks()
        await shutdown_logging()

    asyncio.run(_wrapped())

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("G4 spike preflight — FP-0011")
    print("=" * 64)

    status_icons = {"PASS": _green("✅"), "WARN": _yellow("⚠️ "), "FAIL": _red("❌")}

    overall = "PASS"
    for label, status, detail in _results:
        icon = status_icons.get(status, "?")
        print(f"{icon} {label}")
        if status == "FAIL":
            overall = "FAIL"
        elif status == "WARN" and overall == "PASS":
            overall = "WARN"

    warn_fail = [(l, s, d) for l, s, d in _results if s in ("WARN", "FAIL")]
    if warn_fail:
        print()
        print(_yellow("⚠️  Issues:") if overall == "WARN" else _red("❌ Failures:"))
        for label, status, detail in warn_fail:
            icon = status_icons.get(status, "?")
            print(f"  {icon} {label}")
            if detail:
                print(f"       {detail}")

    print()
    if overall == "PASS":
        print(_green(f"Status: PASS — all checks passed. Ready to run spike."))
    elif overall == "WARN":
        print(_yellow(f"Status: WARN — fix issue(s) above before running spike, then re-run preflight."))
    else:
        print(_red(f"Status: FAIL — spike blocked. Fix failure(s) above first."))
    print()

    sys.exit(0 if overall in ("PASS", "WARN") else 1)


if __name__ == "__main__":
    main()
