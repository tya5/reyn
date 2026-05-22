"""llm_replay.py — replay a single LLM call from a trace dump file.

Reads a JSONL trace file produced by REYN_LLM_TRACE_DUMP, finds the record
matching <request_id>, and re-submits the payload directly to litellm.
No Reyn stack is started — one LLM call, isolated.

Usage:
    python scripts/llm_replay.py <request_id> --trace <jsonl_path>
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --n 5
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --model claude-sonnet
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --full
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> \\
        --patch 'tools[0].function.parameters.properties.name.enum=["a","b"]' \\
        --patch 'messages[0].content+=" MUST output flat skill names"'
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> \\
        --patch 'messages[0].content~=s/skill__code_review/skill__<entry>/g'
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --diff
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> --diff --n 10
    python scripts/llm_replay.py <request_id> --trace <jsonl_path> \\
        --patch 'tools[0].function.parameters.properties.name.enum=["a","b"]' \\
        --diff --n 10
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# detect_attractor import (lazy, from sibling scripts/ directory)
# ---------------------------------------------------------------------------
# We defer the import to _import_detect_attractor() so that the dependency on
# detect_attractor.py only materialises when --from-attractor is used.  The
# script stays usable without detect_attractor on the path.

_SCRIPTS_DIR = Path(__file__).parent


def _import_detect_attractor() -> Any:
    """Import scripts/detect_attractor.py as a module and return it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "detect_attractor", _SCRIPTS_DIR / "detect_attractor.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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
# Patch machinery
# ---------------------------------------------------------------------------

# Token types for the key.path parser
_PATH_SEG = re.compile(r"([^\[\].]+)|\[(\d+)\]")

# Regex matching the full patch expression:
#   <key.path><op>[<value>]
# Supported ops:  =  +=  ?=  ~=  --
#   ~=  sed-style substitution on a string target.
#       Value form: s/<pattern>/<replacement>/[flags]
#       Flags supported: g (global), i (case-insensitive). Default: first match only.
#       Delimiter is fixed as '/'. Escape '/' inside pattern/replacement as '\/'.
_PATCH_RE = re.compile(
    r"^(.+?)"                  # group 1: key.path (non-greedy)
    r"(\?\=|\+\=|\~\=|\-\-|=)" # group 2: operator
    r"(.*)$",                  # group 3: value string (may be empty for --)
    re.DOTALL,
)


_SUBST_RE = re.compile(
    r"^s/((?:[^/\\]|\\.)*)/((?:[^/\\]|\\.)*)/([gi]*)$",
    re.DOTALL,
)


def _parse_subst(raw: str) -> tuple[str, str, int, int]:
    """Parse sed-style 's/pat/repl/flags' into (pattern, replacement, count, re_flags).

    count: 0 = global, 1 = first match (sed-default: first occurrence per line, but
    we apply it across the whole string).
    """
    m = _SUBST_RE.match(raw.strip())
    if not m:
        raise ValueError(
            f"patch: ~= requires 's/pattern/replacement/[gi]' form, got {raw!r}"
        )
    pat = m.group(1).replace(r"\/", "/")
    repl = m.group(2).replace(r"\/", "/")
    flags_str = m.group(3)
    re_flags = re.DOTALL
    if "i" in flags_str:
        re_flags |= re.IGNORECASE
    count = 0 if "g" in flags_str else 1
    return pat, repl, count, re_flags


def _parse_path(path: str) -> list[str | int]:
    """Parse a dotted / bracketed key path into a list of str/int segments.

    'tools[0].function.name' → ['tools', 0, 'function', 'name']
    """
    segments: list[str | int] = []
    for m in _PATH_SEG.finditer(path):
        key, idx = m.group(1), m.group(2)
        if idx is not None:
            segments.append(int(idx))
        else:
            segments.append(key)
    if not segments:
        raise ValueError(f"patch: empty or invalid key path: {path!r}")
    return segments


def _parse_value(raw: str) -> Any:
    """Parse <raw> as a JSON literal, falling back to raw string on failure."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _get_parent(obj: Any, segments: list[str | int]) -> tuple[Any, str | int]:
    """Walk *obj* along *segments[:-1]* and return (parent, last_segment).

    Raises KeyError / IndexError / TypeError when an intermediate node is
    missing (absent-path error for =, +=).
    """
    cur = obj
    for seg in segments[:-1]:
        if isinstance(seg, int):
            cur = cur[seg]
        else:
            cur = cur[seg]
    return cur, segments[-1]


def _apply_patch(payload: dict, expr: str) -> tuple[str, str]:
    """Apply one patch expression to *payload* in-place.

    Returns (path_str, description) for the applied-patches summary.

    Raises ValueError for invalid expressions / incompatible targets.
    """
    m = _PATCH_RE.match(expr)
    if not m:
        raise ValueError(f"patch: cannot parse expression: {expr!r}")

    raw_path, op, raw_value = m.group(1), m.group(2), m.group(3)
    segments = _parse_path(raw_path)

    if op == "--":
        # Delete — parent must exist
        parent, last = _get_parent(payload, segments)
        if isinstance(last, int):
            if not isinstance(parent, list):
                raise ValueError(f"patch: cannot index non-list with [{last}] at {raw_path!r}")
            del parent[last]
        else:
            if not isinstance(parent, dict):
                raise ValueError(f"patch: cannot key non-dict with {last!r} at {raw_path!r}")
            del parent[last]
        return raw_path, "deleted"

    value = _parse_value(raw_value)

    if op == "?=":
        # Optional set — only write if key absent
        try:
            parent, last = _get_parent(payload, segments)
        except (KeyError, IndexError, TypeError):
            # Parent absent — create path
            _force_set(payload, segments, value)
            return raw_path, f"set (was absent): {value!r}"
        # Parent exists — check whether the leaf is present
        try:
            if isinstance(last, int):
                _ = parent[last]
            else:
                _ = parent[last]
            # Key already present — leave unchanged
            return raw_path, "skipped (already set)"
        except (KeyError, IndexError):
            if isinstance(last, int):
                # Can't meaningfully insert into arbitrary list position; error
                raise ValueError(f"patch: ?= on absent list index [{last}] is not supported")
            parent[last] = value
            return raw_path, f"set (was absent): {value!r}"

    if op == "+=":
        # String append — target must be a string
        try:
            parent, last = _get_parent(payload, segments)
            if isinstance(last, int):
                existing = parent[last]
            else:
                existing = parent[last]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"patch: += path not found: {raw_path!r}") from exc
        if not isinstance(existing, str):
            raise ValueError(
                f"patch: += requires a string target; got {type(existing).__name__} at {raw_path!r}"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"patch: += requires a string value; got {type(value).__name__}"
            )
        if isinstance(last, int):
            parent[last] = existing + value
        else:
            parent[last] = existing + value
        return raw_path, f"appended {value!r}"

    if op == "~=":
        # sed-style substitution on a string target
        try:
            parent, last = _get_parent(payload, segments)
            existing = parent[last]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"patch: ~= path not found: {raw_path!r}") from exc
        if not isinstance(existing, str):
            raise ValueError(
                f"patch: ~= requires a string target; got {type(existing).__name__} at {raw_path!r}"
            )
        pat, repl, count, re_flags = _parse_subst(raw_value)
        new_value, n_subs = re.subn(pat, repl, existing, count=count, flags=re_flags)
        if n_subs == 0:
            raise ValueError(
                f"patch: ~= pattern {pat!r} did not match at {raw_path!r}"
            )
        parent[last] = new_value
        return raw_path, f"substituted {n_subs}x: s/{pat}/{repl}/"

    # op == "="  — unconditional replace
    try:
        parent, last = _get_parent(payload, segments)
        if isinstance(last, int):
            parent[last] = value
        else:
            parent[last] = value
        return raw_path, f"replaced → {value!r}"
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"patch: path not found for =: {raw_path!r}") from exc


def _force_set(obj: Any, segments: list[str | int], value: Any) -> None:
    """Walk *segments* and set the leaf, creating intermediate dicts as needed."""
    cur = obj
    for seg in segments[:-1]:
        if isinstance(seg, int):
            cur = cur[seg]
        else:
            if seg not in cur:
                cur[seg] = {}
            cur = cur[seg]
    last = segments[-1]
    if isinstance(last, int):
        cur[last] = value
    else:
        cur[last] = value


def _apply_patches(payload: dict, patch_exprs: list[str]) -> list[tuple[str, str]]:
    """Apply all patch expressions in order; return list of (path, description)."""
    applied: list[tuple[str, str]] = []
    for expr in patch_exprs:
        path_str, desc = _apply_patch(payload, expr)
        applied.append((path_str, desc))
    return applied


def _print_applied_patches(applied: list[tuple[str, str]]) -> None:
    if not applied:
        return
    print("=== Applied patches ===")
    for path_str, desc in applied:
        print(f"  {path_str}: {desc}")
    print()


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
# Diff helpers
# ---------------------------------------------------------------------------


def _tc_name(tc: dict) -> str:
    return tc.get("function", {}).get("name", "?")


def _tc_args(tc: dict) -> str:
    return tc.get("function", {}).get("arguments", "")


def _compute_diff(original: dict, replay: dict) -> dict:
    """Compare original (recorded) response with replay response.

    Returns a dict with keys:
        match: "exact" | "partial" | "different"
        content_diff: unified diff string or None
        tool_calls_diff: {added, removed, changed} or None
        finish_reason_match: bool
        summary_line: 1-line human-readable summary
    """
    orig_content = original.get("content") or ""
    replay_content = replay.get("content") or ""
    orig_tcs = original.get("tool_calls") or []
    replay_tcs = replay.get("tool_calls") or []
    orig_fr = original.get("finish_reason")
    replay_fr = replay.get("finish_reason")

    # --- content diff ---
    content_diff: str | None = None
    content_same = orig_content == replay_content
    if not content_same:
        orig_lines = orig_content.splitlines(keepends=True)
        replay_lines = replay_content.splitlines(keepends=True)
        content_diff = "".join(difflib.unified_diff(
            orig_lines, replay_lines,
            fromfile="original", tofile="replay",
            lineterm="",
        ))
        if not content_diff:
            # edge case: whitespace-only difference not caught by splitlines
            content_diff = f"- {repr(orig_content)}\n+ {repr(replay_content)}"

    # --- tool_calls diff ---
    tc_diff: dict | None = None
    orig_names = [_tc_name(tc) for tc in orig_tcs]
    replay_names = [_tc_name(tc) for tc in replay_tcs]
    orig_name_set = set(orig_names)
    replay_name_set = set(replay_names)

    if orig_tcs or replay_tcs:
        added = [tc for tc in replay_tcs if _tc_name(tc) not in orig_name_set]
        removed = [tc for tc in orig_tcs if _tc_name(tc) not in replay_name_set]
        # "changed" = same name but different arguments
        changed = []
        for orig_tc in orig_tcs:
            name = _tc_name(orig_tc)
            for rep_tc in replay_tcs:
                if _tc_name(rep_tc) == name and _tc_args(rep_tc) != _tc_args(orig_tc):
                    changed.append({
                        "name": name,
                        "original_args": _tc_args(orig_tc),
                        "replay_args": _tc_args(rep_tc),
                    })
        tc_diff = {"added": added, "removed": removed, "changed": changed}

    # --- finish_reason ---
    fr_match = orig_fr == replay_fr

    # --- overall match classification ---
    tc_names_same = orig_names == replay_names
    tc_args_all_same = (tc_diff is None) or (
        not tc_diff["added"] and not tc_diff["removed"] and not tc_diff["changed"]
    )

    if content_same and tc_args_all_same and fr_match:
        match = "exact"
    elif tc_names_same and (not content_same or not tc_args_all_same or not fr_match):
        # Names are the same but something else differs — near-match
        match = "partial"
    else:
        # Names differ or multiple significant changes
        match = "different"

    # Build summary line
    parts: list[str] = [f"match={match}"]
    if not content_same:
        parts.append("content changed")
    if tc_diff and (tc_diff["added"] or tc_diff["removed"]):
        parts.append(f"tool_calls: +{len(tc_diff['added'])} -{len(tc_diff['removed'])}")
    elif tc_diff and tc_diff["changed"]:
        parts.append(f"tool_calls: {len(tc_diff['changed'])} args changed")
    if not fr_match:
        parts.append(f"finish_reason: {orig_fr!r} → {replay_fr!r}")
    summary_line = ", ".join(parts)

    return {
        "match": match,
        "content_diff": content_diff,
        "tool_calls_diff": tc_diff,
        "finish_reason_match": fr_match,
        "summary_line": summary_line,
    }


def _print_diff_result(diff: dict, *, output_format: str) -> None:
    """Print one diff result in pretty or json format."""
    if output_format == "json":
        print(json.dumps(diff, indent=2, ensure_ascii=False))
        return

    print("=== Diff: original vs replay ===")
    print(f"Match: {diff['match']}")

    # content
    cd = diff.get("content_diff")
    if cd is None:
        print("Content: (no change)")
    else:
        print("Content:")
        for line in cd.splitlines():
            print(f"  {line}")

    # tool_calls
    tc_diff = diff.get("tool_calls_diff")
    if tc_diff is None:
        pass  # neither original nor replay had tool_calls
    else:
        added = tc_diff.get("added") or []
        removed = tc_diff.get("removed") or []
        changed = tc_diff.get("changed") or []
        if not added and not removed and not changed:
            if diff.get("match") != "exact":
                print("Tool calls: (same names, args differ — see changed)")
            else:
                print("Tool calls: (no change)")
        else:
            print("Tool calls:")
            for tc in removed:
                print(f"  - removed: {_tc_name(tc)}({_tc_args(tc)[:80]})")
            for tc in added:
                print(f"  + added:   {_tc_name(tc)}({_tc_args(tc)[:80]})")
            for ch in changed:
                print(f"  ~ changed: {ch['name']}")
                print(f"      original: {ch['original_args'][:80]}")
                print(f"      replay:   {ch['replay_args'][:80]}")

    # finish_reason
    if diff.get("finish_reason_match"):
        print("Finish reason: (matches)")
    else:
        print("Finish reason: CHANGED")


def _print_nshot_diff_summary(diffs: list[dict], original: dict) -> None:
    """Print aggregated diff statistics for N-shot runs."""
    n = len(diffs)
    match_counter: Counter = Counter(d["match"] for d in diffs)

    print(f"\n=== N-shot diff summary (n={n}) ===")
    for label in ("exact", "partial", "different"):
        count = match_counter.get(label, 0)
        pct = round(count / n * 100)
        print(f"match={label:<10}: {count} ({pct}%)")

    # Tool call name distribution vs original
    orig_tcs = original.get("tool_calls") or []
    orig_names = [_tc_name(tc) for tc in orig_tcs]
    if orig_names:
        print(f"\nTool call name distribution (vs original={orig_names}):")
        # collect per-run names
        run_name_lists: list[list[str]] = []
        for d in diffs:
            tc_diff = d.get("tool_calls_diff") or {}
            # reconstruct replay names from diff: orig - removed + added
            removed_names = {_tc_name(tc) for tc in tc_diff.get("removed") or []}
            added = [_tc_name(tc) for tc in tc_diff.get("added") or []]
            names = [n for n in orig_names if n not in removed_names] + added
            run_name_lists.append(names)
        name_counter: Counter = Counter(tuple(sorted(nl)) for nl in run_name_lists)
        for name_tuple, count in name_counter.most_common():
            pct = round(count / n * 100)
            label = ", ".join(name_tuple) if name_tuple else "(none)"
            marker = " (= original)" if list(name_tuple) == sorted(orig_names) else ""
            print(f"  [{label}]{marker}: {count} ({pct}%)")

    # finish_reason match rate
    fr_matches = sum(1 for d in diffs if d.get("finish_reason_match"))
    print(f"\nFinish reason matches: {fr_matches}/{n}")


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
        print("  --- cross-model diff ---")
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


# ---------------------------------------------------------------------------
# Multi-turn chain mode (= patch + loop until finish=stop)
# ---------------------------------------------------------------------------
#
# A minimal, self-contained tool executor — supports the subset of router
# tools needed for self-knowledge / discovery chains (list_actions /
# describe_action / invoke_action(reyn.source__*) / file__* / reyn.source__*).
# Tools not in this subset return {"status": "unavailable", ...} so the LLM
# can react without crashing.
#
# Design constraints:
#   - No ToolContext / RouterCallerState wiring (= avoid full Reyn runtime).
#   - Enumerate static catalog directly from universal_dispatch._OPERATION_RULES.
#   - Filesystem reads are bounded to *cwd* (= the repo root usually).

_CHAIN_STATIC_CATEGORIES = (
    "file", "web", "memory.operation", "reyn.source",
    "rag.operation", "mcp.operation", "validation",
)


def _chain_enumerate_static() -> list[dict[str, str]]:
    """Enumerate all static actions from _OPERATION_RULES."""
    try:
        from reyn.tools.universal_dispatch import _OPERATION_RULES
    except Exception:
        return []
    items: list[dict[str, str]] = []
    for qn in sorted(_OPERATION_RULES.keys()):
        items.append({
            "qualified_name": qn,
            "short_description": f"({qn.split('__', 1)[0]} action) {qn.split('__', 1)[-1]}",
        })
    return items


async def _exec_list_actions(args: dict) -> dict:
    """list_actions: enumerate + filter (static categories only)."""
    items = _chain_enumerate_static()
    cat_filter = args.get("category") or []
    if isinstance(cat_filter, str):
        cat_filter = [cat_filter]
    if cat_filter:
        items = [
            it for it in items
            if it["qualified_name"].split("__", 1)[0] in cat_filter
        ]
    text_filter = (args.get("filter") or "").lower()
    if text_filter:
        items = [
            it for it in items
            if text_filter in it["qualified_name"].lower()
            or text_filter in it["short_description"].lower()
        ]
    items.sort(key=lambda it: it["qualified_name"])
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(1, int(args.get("limit", 20) or 20))
    total = len(items)
    page = items[offset:offset + limit]
    return {"items": page, "total": total}


_REYN_SOURCE_READ_DESCRIBE = (
    # Production _REYN_SRC_READ_DESCRIPTION (reyn_src.py) verbatim.
    # No MUST language at the describe boundary — weak LLMs empty-stop
    # on MUST overload here; the SP-level Capabilities routing arrows
    # carry the chain instead.
    "Read a text file from Reyn's own repository by an exact "
    "repo-root-relative path. Use for: (a) reading a specific file the "
    "user named (e.g. README.md, src/reyn/chat/...), or (b) navigating "
    "Reyn's source / docs when NO indexed source covers the topic. "
    "Fallback entry point: reyn_src_read(\"README.md\") for the overview + "
    "curated map of deep-dive paths."
)


async def _exec_describe_action(args: dict) -> dict:
    """describe_action: return action_name + brief description + schema hint."""
    action_name = args.get("action_name", "")
    try:
        from reyn.tools.universal_dispatch import _OPERATION_RULES
    except Exception:
        _OPERATION_RULES = {}
    if action_name not in _OPERATION_RULES:
        return {"status": "error", "error": {"kind": "unknown_action",
                "message": f"action {action_name!r} not in catalog"}}
    op_name, _ = _OPERATION_RULES[action_name]
    # Strengthen description for reyn.source__read with explicit next-step
    # directive; other actions get the stub description.
    if action_name == "reyn.source__read":
        description = _REYN_SOURCE_READ_DESCRIBE
    else:
        description = (
            f"Action {action_name} dispatches to op {op_name}. "
            f"Call via invoke_action(action_name={action_name!r}, args={{...}})."
        )
    return {
        "action_name": action_name,
        "op": op_name,
        "description": description,
        "parameters_hint": "See reyn.op_runtime.registry for per-op schema.",
    }


def _resolve_chain_path(path: str, cwd: Path) -> Path | None:
    """Resolve a repo-relative path against cwd, bounded to cwd."""
    if not path:
        return None
    p = (cwd / path).resolve()
    try:
        p.relative_to(cwd.resolve())
    except ValueError:
        return None
    return p


async def _exec_file_read(args: dict, *, cwd: Path) -> dict:
    """Read a text file by repo-relative path."""
    path = args.get("path", "")
    p = _resolve_chain_path(path, cwd)
    if p is None or not p.is_file():
        return {"status": "error", "error": {"kind": "not_found",
                "message": f"path not found: {path}"}}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"status": "error", "error": {"kind": "read_error",
                "message": str(exc)}}
    offset = int(args.get("offset", 0) or 0)
    limit = args.get("limit")
    if offset or limit:
        lines = text.splitlines(keepends=True)
        end = offset + int(limit) if limit else None
        text = "".join(lines[offset:end])
    return {"status": "ok", "data": {"path": path, "content": text}}


async def _exec_file_list(args: dict, *, cwd: Path) -> dict:
    """List entries under a path."""
    path = args.get("path", "") or ""
    p = _resolve_chain_path(path, cwd) if path else cwd
    if p is None or not p.is_dir():
        return {"status": "error", "error": {"kind": "not_found",
                "message": f"dir not found: {path}"}}
    items = []
    for child in sorted(p.iterdir()):
        items.append({"name": child.name,
                      "type": "dir" if child.is_dir() else "file"})
    return {"status": "ok", "data": {"path": path, "items": items}}


async def _exec_invoke_action(action_name: str, inner_args: dict, *, cwd: Path) -> dict:
    """Route invoke_action to the chain-supported subset."""
    if action_name in ("reyn.source__read", "file__read"):
        return await _exec_file_read(inner_args, cwd=cwd)
    if action_name in ("reyn.source__list", "file__list"):
        return await _exec_file_list(inner_args, cwd=cwd)
    if action_name.startswith("web__"):
        return {"status": "unavailable",
                "message": f"{action_name} not supported in chain replay (live web)"}
    if action_name.startswith("skill__"):
        return {"status": "unavailable",
                "message": f"{action_name}: skill execution not supported in chain replay"}
    if action_name.startswith("agent.peer__"):
        return {"status": "unavailable",
                "message": f"{action_name}: peer dispatch not supported in chain replay"}
    return {"status": "unavailable",
            "message": f"{action_name} not implemented in chain replay"}


_REYN_SELF_KEYWORDS = (
    "a2a", "agent.peer", "agent card", "json-rpc", "message/send",
    "mcp", "reyn web", "reyn run", "reyn chat", "reyn mcp", "reyn auth",
    "reyn source", "cli", "workspace", "skill", "phase", "event log",
    "permission", "runtime", "integration", "how does reyn", "what is",
)


def _query_is_reyn_self(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in _REYN_SELF_KEYWORDS)


async def _exec_search_actions(args: dict) -> dict:
    """search_actions stub — semantic search over actions.

    For chain-replay: when the query is Reyn-self-flavored, surface
    reyn.source__read as the top recommendation; otherwise return empty.
    Production uses embeddings; we approximate by keyword class.
    """
    query = args.get("query", "")
    if _query_is_reyn_self(query):
        return {
            "items": [
                {
                    "qualified_name": "reyn.source__read",
                    "short_description": (
                        "Read a text file from Reyn's own repository — README.md "
                        "for the canonical overview + curated map of deep-dive paths."
                    ),
                    "score": 0.87,
                },
                {
                    "qualified_name": "reyn.source__list",
                    "short_description": "List entries under a path inside Reyn's own repository.",
                    "score": 0.71,
                },
            ],
            "total": 2,
        }
    return {"items": [], "total": 0}


async def _exec_recall(args: dict, *, cwd: Path) -> dict:
    """recall stub — RAG semantic search over indexed sources.

    For chain-replay: when the query looks Reyn-self-flavored, return
    a relevant README chunk so the LLM can synthesize directly.
    Production uses an embedding index; we approximate.
    """
    query = args.get("query", "")
    if _query_is_reyn_self(query):
        readme = cwd / "README.md"
        if readme.is_file():
            text = readme.read_text(encoding="utf-8", errors="replace")
            return {
                "items": [
                    {
                        "source": "README.md",
                        "chunk_id": "readme-overview",
                        "score": 0.84,
                        "content": text[:8000],
                    }
                ],
                "total": 1,
            }
    return {"items": [], "total": 0}


async def _exec_chain_tool(name: str, args: dict, *, cwd: Path) -> dict:
    """Top-level chain tool dispatch."""
    if name == "list_actions":
        return await _exec_list_actions(args)
    if name == "describe_action":
        return await _exec_describe_action(args)
    if name == "search_actions":
        return await _exec_search_actions(args)
    if name == "recall":
        return await _exec_recall(args, cwd=cwd)
    if name == "invoke_action":
        action_name = args.get("action_name", "")
        # Route invoke_action(rag.operation__recall, ...) to recall handler
        if action_name == "rag.operation__recall":
            return await _exec_recall(args.get("args", {}) or {}, cwd=cwd)
        return await _exec_invoke_action(
            action_name, args.get("args", {}) or {}, cwd=cwd,
        )
    if name in ("file__read", "reyn.source__read"):
        return await _exec_file_read(args, cwd=cwd)
    if name in ("file__list", "reyn.source__list"):
        return await _exec_file_list(args, cwd=cwd)
    if name in ("web__search", "web__fetch"):
        return {"status": "unavailable",
                "message": f"{name} not supported in chain replay (live web)"}
    if name in ("plan", "read_tool_result"):
        return {"status": "unavailable",
                "message": f"{name} not supported in chain replay"}
    return {"status": "unavailable",
            "message": f"{name} not implemented in chain replay"}


def _fmt_tool_call_summary(tc: dict) -> str:
    name = tc.get("function", {}).get("name", "?")
    raw = tc.get("function", {}).get("arguments", "")
    if len(raw) > 220:
        raw = raw[:220] + "..."
    return f"{name}({raw})"


async def _run_chain(
    request_id: str,
    trace_path: Path,
    *,
    model_override: str | None,
    temperature_override: float | None,
    max_tokens_override: int | None,
    patch_exprs: list[str] | None,
    max_turns: int,
    cwd: Path,
    full: bool,
    acompletion_fn: Any = None,
) -> None:
    """Multi-turn chain replay: loop until finish=stop or max_turns."""
    records = _load_jsonl(trace_path)
    req, _ = _find_record(records, request_id)
    if req is None:
        print(f"error: request_id not found in trace: {request_id}", file=sys.stderr)
        sys.exit(1)

    original_model = req.get("model", "?")
    model = model_override if model_override else original_model

    payload: dict = {
        "messages": list(req.get("messages") or []),
        "tools": list(req.get("tools") or []) if req.get("tools") else None,
        "tool_choice": req.get("tool_choice"),
        "sampling_params": dict(req.get("sampling_params") or {}),
    }
    if patch_exprs:
        try:
            applied = _apply_patches(payload, patch_exprs)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        if applied:
            _print_applied_patches(applied)

    if temperature_override is not None:
        payload["sampling_params"]["temperature"] = temperature_override
    if max_tokens_override is not None:
        payload["sampling_params"]["max_tokens"] = max_tokens_override

    messages: list[dict] = payload["messages"]
    tools = payload["tools"] or None

    print("=== Multi-turn chain replay ===")
    print(f"  request_id: {request_id}")
    print(f"  model:      {model}")
    print(f"  max_turns:  {max_turns}")
    print(f"  cwd:        {cwd}")
    print(f"  start msgs: {len(messages)}")
    print()

    final_content: str | None = None
    final_finish: str | None = None

    for turn in range(1, max_turns + 1):
        result = await _single_call(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=payload["tool_choice"],
            sampling_params=payload["sampling_params"],
            acompletion_fn=acompletion_fn,
        )
        usage = result.get("usage", {}) or {}
        print(f"--- turn {turn} ---  finish={result['finish_reason']}  "
              f"out={usage.get('completion_tokens', '?')}")

        tool_calls = result.get("tool_calls") or []
        for tc in tool_calls:
            print(f"  → {_fmt_tool_call_summary(tc)}")

        content = result.get("content") or ""
        if content:
            display = content if full or len(content) <= 400 else content[:400] + "...[truncated]"
            print(f"  content: {display}")

        final_content = content or final_content
        final_finish = result["finish_reason"]

        if result["finish_reason"] == "stop":
            print()
            print("=== Chain complete (finish=stop) ===")
            print()
            if final_content:
                print(final_content if full else final_content[:2500])
            return

        if not tool_calls:
            print("  (no tool_calls but finish != stop — breaking)")
            break

        # Append assistant message + tool results
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                tool_args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                tool_result: dict = {"status": "error", "error": {
                    "kind": "invalid_args_json",
                    "message": f"json parse error: {exc}",
                }}
            else:
                tool_result = await _exec_chain_tool(tool_name, tool_args, cwd=cwd)
            preview = json.dumps(tool_result, ensure_ascii=False)
            if len(preview) > 220:
                preview = preview[:220] + "..."
            print(f"  ← {tool_name}: {preview}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, ensure_ascii=False),
            })
        print()

    print(f"=== Chain max_turns ({max_turns}) reached without finish=stop ===")
    print(f"  last finish_reason: {final_finish}")
    if final_content:
        print()
        print("Last content:")
        print(final_content if full else final_content[:2500])


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
    patch_exprs: list[str] | None = None,
    diff: bool = False,
    acompletion_fn: Any = None,
    _collect_results: list[dict] | None = None,
) -> None:
    """Replay *request_id* from *trace_path*.

    *_collect_results*: if provided, each replay result dict is appended to
    this list so the caller (e.g. _run_from_attractor) can compute aggregate
    statistics without re-running.
    """
    records = _load_jsonl(trace_path)
    req, original_resp = _find_record(records, request_id)

    if req is None:
        print(f"error: request_id not found in trace: {request_id}", file=sys.stderr)
        sys.exit(1)

    original_model = req.get("model", "?")
    model = model_override if model_override else original_model

    # Build a mutable payload dict for patch application
    payload: dict = {
        "messages": list(req.get("messages") or []),
        "tools": list(req.get("tools") or []) if req.get("tools") else None,
        "tool_choice": req.get("tool_choice"),
        "sampling_params": dict(req.get("sampling_params") or {}),
    }

    # Apply --patch expressions before any other overrides
    applied_patches: list[tuple[str, str]] = []
    if patch_exprs:
        try:
            applied_patches = _apply_patches(payload, patch_exprs)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

    messages = payload["messages"]
    tools = payload["tools"] or None
    tool_choice = payload["tool_choice"]
    sampling_params = payload["sampling_params"]

    # Apply sampling overrides
    if temperature_override is not None:
        sampling_params["temperature"] = temperature_override
    if max_tokens_override is not None:
        sampling_params["max_tokens"] = max_tokens_override

    # Warn when --diff is requested but trace has no response record
    if diff and original_resp is None:
        print(
            "warning: --diff requested but no response record found in trace for this request_id. "
            "Diff output will be skipped.",
            file=sys.stderr,
        )

    print("=== LLM Replay ===")
    print(f"  request_id: {request_id}")
    print(f"  model:      {model}" + (f"  (original: {original_model})" if model_override else ""))
    print(f"  messages:   {len(messages)}")
    print(f"  tools:      {len(tools) if tools else 0}")
    print(f"  n:          {n}")
    if diff:
        print("  diff:       enabled")
    print()

    # Show applied patches summary in pretty mode
    if applied_patches and output_format == "pretty":
        _print_applied_patches(applied_patches)

    results: list[dict] = []
    diffs: list[dict] = []
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
        if _collect_results is not None:
            _collect_results.append(result)

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

        # Compute and (for n==1) print diff
        if diff and original_resp is not None:
            d = _compute_diff(original_resp, result)
            diffs.append(d)
            if n == 1:
                if output_format == "json":
                    print(json.dumps(d, indent=2, ensure_ascii=False))
                else:
                    _print_diff_result(d, output_format=output_format)

    if n > 1:
        _print_nshot_summary(results)
        if diff and original_resp is not None and diffs:
            _print_nshot_diff_summary(diffs, original_resp)

    await _shutdown_logging()


# ---------------------------------------------------------------------------
# Multi-attractor replay helpers
# ---------------------------------------------------------------------------


def _print_attractor_summary(
    detections: list[dict],
    results_by_rid: dict[str, list[dict]],
    n: int,
) -> None:
    """Print multi-attractor replay summary table."""
    total_attractors = len(detections)
    total_calls = total_attractors * n

    print("\n=== Multi-attractor replay summary ===")
    print(f"Total attractors replayed: {total_attractors}")
    print(f"Total LLM calls: {total_calls} (= {total_attractors} × {n})")

    # By-heuristic counts
    heuristic_counter: Counter = Counter(d["heuristic"] for d in detections)
    if heuristic_counter:
        print("By heuristic:")
        for h_name, count in heuristic_counter.most_common():
            h_calls = count * n
            print(f"  {h_name}: {count} attractors, {h_calls} calls")

    # Per-attractor empty-stop rate
    if results_by_rid:
        print("Empty-stop rate by attractor:")
        for d in detections:
            rid = d.get("request_id", "?")
            h = d.get("heuristic", "?")
            run_results = results_by_rid.get(rid, [])
            if not run_results:
                continue
            empty_stops = sum(
                1 for r in run_results
                if r.get("finish_reason") == "stop"
                and not (r.get("content") or "").strip()
                and not (r.get("tool_calls") or [])
            )
            total_runs = len(run_results)
            pct = round(empty_stops / total_runs * 100) if total_runs else 0
            short_rid = rid[:8] if len(rid) > 8 else rid
            print(f"  {short_rid}... ({h}): {empty_stops}/{total_runs} ({pct}%)")


async def _run_from_attractor(
    trace_path: Path,
    *,
    attractor_heuristics: list[str] | None,
    attractor_first: int | None,
    model_override: str | None,
    temperature_override: float | None,
    max_tokens_override: int | None,
    n: int,
    full: bool,
    output_format: str,
    patch_exprs: list[str] | None = None,
    diff: bool = False,
    acompletion_fn: Any = None,
) -> None:
    """Detect all attractors in *trace_path* and replay each one."""
    da = _import_detect_attractor()

    records = _load_jsonl(trace_path)
    detections = da.detect_all_attractors(
        records,
        heuristics=attractor_heuristics,
    )

    if attractor_first is not None:
        detections = detections[:attractor_first]

    if not detections:
        print("No attractors detected in trace — nothing to replay.")
        return

    print(f"Detected {len(detections)} attractor(s) — replaying each with n={n}.")

    results_by_rid: dict[str, list[dict]] = {}

    for i, detection in enumerate(detections):
        rid = detection["request_id"]
        h = detection["heuristic"]
        rel = detection.get("rel_time", "?")
        print(f"\n=== Attractor {i + 1}/{len(detections)} "
              f"(heuristic={h}, rel={rel}) ===")
        await _run(
            request_id=rid,
            trace_path=trace_path,
            model_override=model_override,
            temperature_override=temperature_override,
            max_tokens_override=max_tokens_override,
            n=n,
            full=full,
            output_format=output_format,
            patch_exprs=patch_exprs or [],
            diff=diff,
            acompletion_fn=acompletion_fn,
            _collect_results=results_by_rid.setdefault(rid, []),
        )

    _print_attractor_summary(detections, results_by_rid, n)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a single LLM call from a REYN_LLM_TRACE_DUMP file."
    )
    parser.add_argument(
        "request_id",
        nargs="?",
        default=None,
        help="request_id to replay (from llm-payloads output). "
             "Omit when using --from-attractor.",
    )
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
    parser.add_argument(
        "--patch",
        action="append",
        default=[],
        metavar="EXPR",
        help=(
            "Patch the payload before replay. Format: 'key.path=value', "
            "'key.path+=value' (string append), 'key.path?=value' (set if absent), "
            "'key.path~=s/pattern/replacement/[gi]' (sed-style substitution), "
            "'key.path--' (delete). Repeatable; applied in CLI order."
        ),
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        default=False,
        help=(
            "Show diff between original (recorded) response and replay response. "
            "For N-shot, summarize match rate across all runs."
        ),
    )
    # --from-attractor group
    parser.add_argument(
        "--from-attractor",
        action="store_true",
        default=False,
        help=(
            "Detect all attractors in the trace (via detect_attractor heuristics) "
            "and replay each one.  Replaces the positional request_id argument."
        ),
    )
    parser.add_argument(
        "--attractor-heuristics",
        default=None,
        metavar="LIST",
        help=(
            "Comma-separated heuristic names to filter when using --from-attractor "
            "(e.g. 'stop_with_must_rule').  Default: all heuristics."
        ),
    )
    parser.add_argument(
        "--attractor-first",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Limit --from-attractor to the first N attractor request_ids (default: all)."
        ),
    )
    # --chain group (multi-turn replay)
    parser.add_argument(
        "--chain",
        action="store_true",
        default=False,
        help=(
            "Multi-turn chain replay — loop until finish=stop or --max-turns. "
            "Tool calls dispatched via a minimal executor (list_actions / "
            "describe_action / invoke_action(reyn.source__*/file__*) / file ops). "
            "Unsupported tools return {status:unavailable} so the LLM can react."
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Max turn limit for --chain (default: 8)",
    )
    parser.add_argument(
        "--cwd",
        type=str,
        default=".",
        help=(
            "Repo / file root for chain-mode file reads (default: '.')."
            " All reyn.source__read / file__read paths resolved against this."
        ),
    )
    args = parser.parse_args()

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    if args.from_attractor:
        # Parse attractor heuristics filter
        attractor_heuristics: list[str] | None = None
        if args.attractor_heuristics:
            da = _import_detect_attractor()
            attractor_heuristics = da._parse_heuristics(args.attractor_heuristics)
            if not attractor_heuristics:
                print("error: no valid heuristics specified for --attractor-heuristics",
                      file=sys.stderr)
                sys.exit(1)

        asyncio.run(_run_from_attractor(
            trace_path=trace_path,
            attractor_heuristics=attractor_heuristics,
            attractor_first=args.attractor_first,
            model_override=args.model_override,
            temperature_override=args.temperature,
            max_tokens_override=args.max_tokens,
            n=args.n,
            full=args.full,
            output_format=args.output_format,
            patch_exprs=args.patch or [],
            diff=args.diff,
        ))
        return

    if args.request_id is None:
        parser.error("request_id is required unless --from-attractor or --chain is used")

    if args.chain:
        asyncio.run(_run_chain(
            request_id=args.request_id,
            trace_path=trace_path,
            model_override=args.model_override,
            temperature_override=args.temperature,
            max_tokens_override=args.max_tokens,
            patch_exprs=args.patch or [],
            max_turns=args.max_turns,
            cwd=Path(args.cwd).resolve(),
            full=args.full,
        ))
        return

    asyncio.run(_run(
        request_id=args.request_id,
        trace_path=trace_path,
        model_override=args.model_override,
        temperature_override=args.temperature,
        max_tokens_override=args.max_tokens,
        n=args.n,
        full=args.full,
        output_format=args.output_format,
        patch_exprs=args.patch or [],
        diff=args.diff,
    ))


if __name__ == "__main__":
    main()
