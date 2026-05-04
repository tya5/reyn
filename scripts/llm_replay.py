"""llm_replay.py — replay a single LLM call from a trace dump file.

Reads a JSONL trace file produced by REYN_LLM_TRACE_DUMP, finds the record
matching <request_id>, and re-submits the payload directly to litellm.
No Reyn stack is started — one LLM call, isolated.

Usage:
    python scripts/llm_replay.py <request_id> --trace <jsonl_path>
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --n 5
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --model claude-sonnet
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --full
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read trace file: {exc}", file=sys.stderr)
        sys.exit(1)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _find_record(records: list[dict], request_id: str) -> tuple[dict | None, dict | None]:
    """Return (request_record, response_record) for the given request_id."""
    req: dict | None = None
    resp: dict | None = None
    for rec in records:
        if rec.get("request_id") != request_id:
            continue
        if rec.get("kind") == "request":
            req = rec
        elif rec.get("kind") == "response":
            resp = rec
    return req, resp


# ---------------------------------------------------------------------------
# Proxy / litellm kwargs helpers
# ---------------------------------------------------------------------------


def _proxy_kwargs() -> dict:
    """Return extra kwargs for litellm.acompletion when a proxy is configured."""
    api_base = os.environ.get("LITELLM_API_BASE")
    if not api_base:
        return {}
    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    return {"api_base": api_base, "custom_llm_provider": "openai", "api_key": api_key}


def _effective_model(model: str, extra: dict) -> str:
    """Strip provider prefix when routing via local proxy."""
    return model.split("/", 1)[1] if extra and "/" in model else model


# ---------------------------------------------------------------------------
# Single litellm call
# ---------------------------------------------------------------------------


async def _single_call(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: Any,
    sampling_params: dict,
    *,
    acompletion_fn: Any = None,
) -> dict:
    """Issue one acompletion call and return a normalised response dict."""
    if acompletion_fn is None:
        import litellm
        acompletion_fn = litellm.acompletion

    extra = _proxy_kwargs()
    effective = _effective_model(model, extra)

    call_kwargs: dict = {
        "model": effective,
        "messages": messages,
        **extra,
    }
    if tools:
        call_kwargs["tools"] = tools
    if tool_choice is not None:
        call_kwargs["tool_choice"] = tool_choice

    # Apply sampling params — skip reyn-internal keys that litellm doesn't accept
    _skip = {"timeout", "max_retries"}
    for k, v in sampling_params.items():
        if k not in _skip and v is not None:
            call_kwargs[k] = v

    resp = await acompletion_fn(**call_kwargs)

    msg = resp.choices[0].message
    finish_reason = None
    try:
        finish_reason = resp.choices[0].finish_reason
    except Exception:
        pass

    usage: dict = {}
    try:
        u = resp.usage
        if u is not None:
            usage = {
                "prompt_tokens": int(u.prompt_tokens or 0),
                "completion_tokens": int(u.completion_tokens or 0),
            }
    except Exception:
        pass

    # Normalise tool_calls to plain dicts
    tool_calls: list[dict] = []
    try:
        for tc in msg.tool_calls or []:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })
    except Exception:
        pass

    return {
        "model": effective,
        "content": msg.content,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _truncate(text: str | None, full: bool, head: int = 200, tail: int = 200) -> str:
    if text is None:
        return "(null)"
    if full or len(text) <= head + tail:
        return text
    return f"{text[:head]}\n... [{len(text) - head - tail} chars omitted] ...\n{text[-tail:]}"


def _content_hash(result: dict) -> str:
    key = json.dumps(
        {"content": result.get("content"), "tool_calls": result.get("tool_calls")},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _print_single_result(result: dict, *, full: bool, original_resp: dict | None, original_model: str | None) -> None:
    """Print a single replay result in pretty or diff format."""
    print(f"  finish_reason: {result.get('finish_reason', '?')}")
    usage = result.get("usage", {})
    if usage:
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        print(f"  tokens: in={pt} out={ct}")

    tool_calls = result.get("tool_calls") or []
    if tool_calls:
        print(f"  tool_calls ({len(tool_calls)}):")
        for tc in tool_calls:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "")
            # Truncate args display
            display_args = args_str if full else (args_str[:120] + "..." if len(args_str) > 120 else args_str)
            print(f"    - {fn.get('name', '?')}  args={display_args}")
    else:
        content = result.get("content")
        print(f"  content: {_truncate(content, full)}")

    # Diff section when original response available
    if original_resp is not None and original_model is not None:
        print()
        print(f"  --- cross-model diff ---")
        print(f"  Original model: {original_model}")
        print(f"  Override model: {result.get('model', '?')}")

        orig_tcs = original_resp.get("tool_calls") or []
        new_tcs = tool_calls

        orig_names = [tc.get("function", {}).get("name") for tc in orig_tcs]
        new_names = [tc.get("function", {}).get("name") for tc in new_tcs]

        if orig_names == new_names and orig_names:
            print(f"  Tool calls: SAME ({', '.join(orig_names)})")
        elif orig_names and new_names:
            print(f"  Tool calls: CHANGED — original={orig_names}  override={new_names}")
        elif orig_names and not new_names:
            print(f"  Tool calls: REMOVED — original had {orig_names}")
        elif not orig_names and new_names:
            print(f"  Tool calls: ADDED — override has {new_names}")

        orig_usage = original_resp.get("usage") or {}
        new_usage = result.get("usage") or {}
        orig_total = (orig_usage.get("prompt_tokens") or 0) + (orig_usage.get("completion_tokens") or 0)
        new_total = (new_usage.get("prompt_tokens") or 0) + (new_usage.get("completion_tokens") or 0)
        if orig_total or new_total:
            print(f"  Tokens: {new_total} (vs original {orig_total})")


def _print_nshot_summary(results: list[dict]) -> None:
    """Print N-shot distribution table."""
    n = len(results)
    print(f"\n=== N-shot replay (n={n}) ===")

    # Tool call name counts
    name_counter: Counter = Counter()
    args_by_name: dict[str, set] = {}
    for r in results:
        tcs = r.get("tool_calls") or []
        for tc in tcs:
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            name_counter[name] += 1
            args_by_name.setdefault(name, set()).add(fn.get("arguments", ""))

    if name_counter:
        print("Tool calls (by name):")
        for name, count in name_counter.most_common():
            pct = round(count / n * 100)
            variants = len(args_by_name.get(name, set()))
            print(f"  {name}: {count} ({pct}%)")
            if variants > 1:
                print(f"    args variants: {variants} distinct")
    else:
        print("Tool calls: (none in any run)")

    # finish_reason distribution
    finish_counter: Counter = Counter(r.get("finish_reason", "?") for r in results)
    print("Finish reasons:")
    for reason, count in finish_counter.most_common():
        print(f"  {reason}: {count}")

    # Token stats
    totals = [
        (r.get("usage") or {}).get("prompt_tokens", 0) +
        (r.get("usage") or {}).get("completion_tokens", 0)
        for r in results
        if r.get("usage")
    ]
    if totals:
        avg = sum(totals) / len(totals)
        print(f"Tokens (avg / min / max): {avg:.0f} / {min(totals)} / {max(totals)}")

    # Content hash distribution (for non-tool-call responses)
    hash_counter: Counter = Counter(_content_hash(r) for r in results)
    if len(hash_counter) < n:
        print(f"Content hashes: {len(hash_counter)} distinct (out of {n} runs)")


# ---------------------------------------------------------------------------
# Shutdown helper (same pattern as reyn.llm.llm)
# ---------------------------------------------------------------------------


async def _shutdown_logging() -> None:
    try:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
        await GLOBAL_LOGGING_WORKER.clear_queue()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------


async def _run(
    request_id: str,
    trace_path: Path,
    *,
    model_override: str | None,
    temperature_override: float | None,
    max_tokens_override: int | None,
    n: int,
    full: bool,
    output_format: str,
    acompletion_fn: Any = None,
) -> None:
    records = _load_jsonl(trace_path)
    req, original_resp = _find_record(records, request_id)

    if req is None:
        print(f"error: request_id not found in trace: {request_id}", file=sys.stderr)
        sys.exit(1)

    original_model = req.get("model", "?")
    model = model_override if model_override else original_model
    messages = req.get("messages") or []
    tools = req.get("tools") or None
    tool_choice = req.get("tool_choice")
    sampling_params = dict(req.get("sampling_params") or {})

    # Apply sampling overrides
    if temperature_override is not None:
        sampling_params["temperature"] = temperature_override
    if max_tokens_override is not None:
        sampling_params["max_tokens"] = max_tokens_override

    print(f"=== LLM Replay ===")
    print(f"  request_id: {request_id}")
    print(f"  model:      {model}" + (f"  (original: {original_model})" if model_override else ""))
    print(f"  messages:   {len(messages)}")
    print(f"  tools:      {len(tools) if tools else 0}")
    print(f"  n:          {n}")
    print()

    results: list[dict] = []
    for i in range(n):
        if n > 1:
            print(f"--- run {i + 1}/{n} ---")
        result = await _single_call(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            sampling_params=sampling_params,
            acompletion_fn=acompletion_fn,
        )
        results.append(result)

        if n == 1 or output_format == "json":
            if output_format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                _print_single_result(
                    result,
                    full=full,
                    original_resp=original_resp if model_override else None,
                    original_model=original_model if model_override else None,
                )

    if n > 1:
        _print_nshot_summary(results)

    await _shutdown_logging()


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a single LLM call from a REYN_LLM_TRACE_DUMP file."
    )
    parser.add_argument("request_id", help="request_id to replay (from llm-payloads output)")
    parser.add_argument("--trace", required=True, help="Path to JSONL trace file")
    parser.add_argument("--model", default=None, dest="model_override",
                        help="Override model (e.g. claude-sonnet, openai/gpt-4o)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Override temperature sampling param")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Override max_tokens sampling param")
    parser.add_argument("--n", type=int, default=1,
                        help="Number of times to replay (default: 1; >1 prints distribution)")
    parser.add_argument("--full", action="store_true", default=False,
                        help="Show full content without truncation")
    parser.add_argument("--output-format", choices=["pretty", "json"], default="pretty",
                        help="Output format: pretty (default) or json (raw response dict)")
    args = parser.parse_args()

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_run(
        request_id=args.request_id,
        trace_path=trace_path,
        model_override=args.model_override,
        temperature_override=args.temperature,
        max_tokens_override=args.max_tokens,
        n=args.n,
        full=args.full,
        output_format=args.output_format,
    ))


if __name__ == "__main__":
    main()
