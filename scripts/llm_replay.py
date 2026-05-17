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
        parser.error("request_id is required unless --from-attractor is used")

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
