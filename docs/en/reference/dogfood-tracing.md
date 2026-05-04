# LLM Payload Tracing (Dogfood Debug)

Reyn can optionally dump every LLM call's full payload — messages, tools
schema, sampling params, and response — to a JSONL file on disk. This is
intentionally a debug/dogfood tool, not production audit infrastructure
(events log handles that).

## Why

When a LLM misbehaves during dogfood, you need to see *exactly what the LLM
received*. Without the actual payload, hypotheses like "LLM misread the enum"
or "instruction wasn't forwarded" can't be falsified. The dump makes that a
5-minute verification rather than a guess.

## Enabling the dump

Set the `REYN_LLM_TRACE_DUMP` environment variable to an output path before
starting a dogfood session:

```bash
export REYN_LLM_TRACE_DUMP=.reyn/llm_trace.jsonl
python -m reyn chat
```

When the variable is not set, the feature is completely inactive — no file
opens, no hashing, zero production overhead.

## Inspecting the dump

`scripts/dogfood_trace.py` has three LLM-specific modes.

All three modes accept **one or more trace files** via `--trace`.  When
multiple files are given they are merged chronologically so that `T+` offsets
span a single consistent time axis rooted at the oldest record across all
files.

```bash
# Multiple trace files — specify each with its own --trace flag
python scripts/dogfood_trace.py --mode llm-payloads \
  --trace dump_h1.jsonl --trace dump_h2.jsonl

# Comma-separated form (equivalent)
python scripts/dogfood_trace.py --mode llm-payloads \
  --trace dump_h1.jsonl,dump_h2.jsonl
```

### `llm-payloads` — timeline overview

```bash
python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace.jsonl
```

Prints a time-ordered list of all request/response pairs:

```
[T+12.3s] request_id=abc123...  model=gemini-2.5-flash-lite  caller=router  msgs=3 tools=18
[T+12.4s] response_id=abc123...  finish=tool_calls  tool_calls=2 tokens_in=1700 tokens_out=30
[T+15.1s] request_id=def456...  model=gemini-2.5-flash-lite  caller=phase:copy_to_work  msgs=4 tools=0
...
```

`T+` is seconds since the oldest record across all supplied trace files.

When multiple files are supplied a `[file=<basename>]` annotation is appended
to each request line so you can tell which dump file the record came from:

```
[T+0.0s]  request_id=abc123...  model=...  caller=router  msgs=3 tools=18  [file=dump_h1.jsonl]
[T+0.4s]  response_id=abc123...  finish=tool_calls
[T+12.3s] request_id=def456...  model=...  caller=phase:copy_to_work  msgs=4 tools=0  [file=dump_h2.jsonl]
```

### `llm-detail <request_id>` — full payload

```bash
python scripts/dogfood_trace.py --mode llm-detail abc123... --trace .reyn/llm_trace.jsonl
```

Pretty-prints the full call: model, caller, messages, tool names, sampling
params, and response (finish_reason, usage, content or tool_calls).

System prompts are truncated to head/tail 200 chars by default. Use `--full`
to expand everything:

```bash
python scripts/dogfood_trace.py --mode llm-detail abc123... --trace .reyn/llm_trace.jsonl --full
```

### `llm-tools-schema <request_id>` — tools schema

```bash
python scripts/dogfood_trace.py --mode llm-tools-schema abc123... --trace .reyn/llm_trace.jsonl
```

Outputs the full tools array as pretty-printed JSON. Useful for checking enum
constraints, parameter descriptions, and which skills were in the available
list for that call.

## Dump format

JSONL, one record per line. Two record kinds: `request` and `response`,
paired by `request_id`.

**Request fields:** `kind`, `request_id`, `timestamp`, `model`,
`caller_hint`, `messages`, `tools` (full schema, or `null`),
`tool_choice`, `sampling_params`

**Response fields:** `kind`, `request_id`, `timestamp`, `content`,
`tool_calls`, `finish_reason`, `usage` (`prompt_tokens`,
`completion_tokens`)

## Production hardening

Two opt-out-able safeguards are active by default whenever
`REYN_LLM_TRACE_DUMP` is set.

### Size limit and rotation

| Env var | Default | Effect |
|---------|---------|--------|
| `REYN_LLM_TRACE_DUMP_MAX_SIZE` | `104857600` (100 MB) | Maximum dump file size in bytes |

Before each write, Reyn checks the file size. When it exceeds the limit, the
current file is renamed to `<path>.1` (one generation only — any pre-existing
`.1` is replaced) and a fresh file is started. A message is printed to stderr:

```
[reyn] LLM trace dump rotated: .reyn/llm_trace.jsonl -> .reyn/llm_trace.jsonl.1
(size 104,857,601 > limit 104,857,600)
```

Rotation is intentionally single-generation. For multi-generation archiving,
use an external log-rotation tool (e.g. `logrotate`) on the dump path.

If rotation fails (disk full, permission error, etc.) the write continues on
the original path without error — the dump is never blocked by infrastructure
problems.

To lower the limit for testing:

```bash
export REYN_LLM_TRACE_DUMP_MAX_SIZE=1048576  # 1 MB
```

### Secrets redaction

Reyn scans every string in the dump payload and masks known sensitive patterns
before writing. This is **default ON** — set `REYN_LLM_TRACE_REDACT=off` to
disable.

**Built-in patterns:**

| Pattern name | Regex | Example match |
|--------------|-------|---------------|
| `openai-key` | `sk-[A-Za-z0-9_-]{20,}` | `sk-proj-abc...` |
| `slack-token` | `xoxb-[A-Za-z0-9-]{20,}` | `xoxb-1234567890-abc...` |
| `bearer-token` | `Bearer\s+[A-Za-z0-9._-]{20,}` | `Bearer eyJhbGci...` |
| `private-key` | PEM `-----BEGIN ... KEY-----` block | RSA / EC private keys |

Masked format: `[REDACTED:<pattern_name>]`

**Adding custom patterns** via `REYN_LLM_TRACE_REDACT_PATTERNS` (comma-separated
regex list):

```bash
export REYN_LLM_TRACE_REDACT_PATTERNS='MY_SECRET_[A-Z0-9]+,ghp_[A-Za-z0-9]{36}'
```

Each pattern is assigned a label `custom-0`, `custom-1`, … in order.
Invalid regex strings are silently skipped.

**Disabling redaction:**

```bash
export REYN_LLM_TRACE_REDACT=off
```

**False positive risk:** any sufficiently long alphanumeric string matching a
pattern will be masked. This is intentional — production-safe defaults are
preferred over completeness. If a legitimate field is being masked, add a more
specific regex to distinguish it, or use `REYN_LLM_TRACE_REDACT=off` for local
development only.

## Security and cleanup

- **Secrets redaction is default ON.** Known API key patterns are automatically
  masked before writing. See [Secrets redaction](#secrets-redaction) above.
- **Prompts may contain sensitive content.** System prompts carry project
  context and skill instructions. Treat the file as internal even with redaction
  enabled.
- **Add to `.gitignore`.** The dump path (`.reyn/llm_trace.jsonl` by default)
  should not be committed:
  ```
  .reyn/llm_trace.jsonl
  .reyn/llm_trace.jsonl.1
  ```
- **Delete after a session.** The file is automatically rotated at 100 MB, but
  deleting it before a new dogfood run keeps the trace clean.

---

# LLM Replay (`scripts/llm_replay.py`)

`llm_replay.py` takes a trace file, finds a specific request by `request_id`,
and re-submits that exact payload directly to litellm. No Reyn stack is
started — one LLM call, one cost unit, full isolation.

## Basic usage

```bash
python scripts/llm_replay.py <request_id> --trace <jsonl_path>
```

Get `<request_id>` from `dogfood_trace.py --mode llm-payloads` output.

## Options

| Option | Description |
|--------|-------------|
| `--trace <path>` | Path to JSONL trace file (required) |
| `--model <name>` | Override the model (e.g. `claude-sonnet`, `openai/gpt-4o`) |
| `--temperature <float>` | Override temperature sampling param |
| `--max-tokens <int>` | Override max_tokens sampling param |
| `--n <count>` | Replay N times to observe distribution (default: 1) |
| `--full` | Show full content without head/tail truncation |
| `--output-format pretty\|json` | Output format (default: `pretty`) |
| `--patch EXPR` | Mutate the payload before replay (repeatable; see below) |
| `--diff` | Compare replay response against original recorded response (see below) |
| `--from-attractor` | Detect all attractors in the trace and replay each (see below) |
| `--attractor-heuristics LIST` | Comma-separated heuristic names to filter when using `--from-attractor` |
| `--attractor-first N` | Limit `--from-attractor` to the first N attractors |

## Payload patching (`--patch`)

`--patch` lets you mutate any field in the captured payload before the LLM call
is issued. No trace file is modified — the patch applies only to the in-memory
copy used for this replay run.

### Syntax

```
--patch 'key.path=value'       # replace
--patch 'key.path+=value'      # append (string targets only)
--patch 'key.path?=value'      # set only if the field is absent
--patch 'key.path--'           # delete the field / list element
```

**Key path** uses dot notation and `[N]` for list indices:

```
messages[0].content
tools[0].function.parameters.properties.name.enum
sampling_params.temperature
```

**Value** is parsed as a JSON literal (`"string"`, `123`, `1.5`, `true`,
`false`, `null`, `[…]`, `{…}`). If JSON parsing fails the raw string is used.

Multiple `--patch` options are applied in CLI argument order. When two patches
target the same path the later one wins.

### Error behaviour

| Situation | Result |
|-----------|--------|
| Path not found (for `=`, `+=`) | `error:` message + exit 1, LLM not called |
| `+=` on a non-string target | `error:` message + exit 1 |
| Deletion (`--`) of absent key | `KeyError` / `IndexError`, exit 1 |
| `?=` on absent key | Field is created |
| `?=` on existing key | Field is left unchanged |

> **Note on list deletion:** deleting `tools[1]--` shifts all subsequent
> indices. If you chain multiple list deletions, apply them from the highest
> index to the lowest to avoid index drift.

### Applied patches output

In `--output-format pretty` (the default), a summary section is printed before
the LLM result:

```
=== Applied patches ===
  tools[0].function.parameters.properties.name.enum: replaced → ['skill_a', 'skill_b', 'skill_c']
  messages[0].content: appended ' Available skills (3): skill_a, skill_b, skill_c'
```

## Response diff (`--diff`)

`--diff` compares the original response (recorded in the trace file) with the
response obtained from the replay. It classifies the comparison as:

- **`exact`**: content, tool_calls, and finish_reason all match.
- **`partial`**: tool call *names* match but arguments differ; or content
  matches but finish_reason differs; or similar near-match.
- **`different`**: tool call names differ, or structure changed significantly.

### Single replay diff

```bash
python scripts/llm_replay.py <request_id> --trace .reyn/llm_trace.jsonl --diff
```

Pretty output:

```
=== Diff: original vs replay ===
Match: partial
Content: (no change)
Tool calls:
  ~ changed: invoke_skill
      original: {"name":"skill_improver.review"}
      replay:   {"name":"skill_improver"}
Finish reason: (matches)
```

JSON output (`--output-format json --diff`):

```json
{"match": "partial", "content_diff": null, "tool_calls_diff": {"added": [], "removed": [], "changed": [...]}, "finish_reason_match": true, "summary_line": "match=partial, tool_calls: 1 args changed"}
```

### N-shot diff summary

```bash
python scripts/llm_replay.py <request_id> --trace .reyn/llm_trace.jsonl --n 10 --diff
```

After the per-run distribution table, a diff summary is appended:

```
=== N-shot diff summary (n=10) ===
match=exact      : 3 (30%)
match=partial    : 5 (50%)
match=different  : 2 (20%)

Tool call name distribution (vs original=['invoke_skill']):
  [invoke_skill] (= original): 8 (80%)
  [list_skills]:               2 (20%)

Finish reason matches: 7/10
```

### Fix effect verification (`--patch` + `--diff`)

Combine `--patch` (payload mutation) with `--diff` (comparison against the
original recorded response) to measure a fix in one command:

```bash
python scripts/llm_replay.py <router_request_id> --trace .reyn/llm_trace.jsonl \
  --patch 'tools[0].function.parameters.properties.name.enum=["skill_a","skill_b"]' \
  --diff --n 10
```

This answers "after the fix, how often does the replay match the originally
correct behaviour?" without a full dogfood session.

### Notes

- If the trace file contains no `response` record for the given `request_id`
  (e.g. request-only dumps), `--diff` emits a warning and skips diff output;
  the replay itself continues normally.
- `--diff` with `--model` override compares the override model's response
  against the original model's recorded response — useful for cross-model
  comparison.

## Attractor-driven replay (`--from-attractor`)

`--from-attractor` collapses the manual detect → copy → replay cycle into one
command. It runs `detect_attractor` heuristics on the trace, then replays
every detected attractor request using the same options as a normal replay
(`--n`, `--patch`, `--diff`, `--model`, etc.).

### Syntax

```bash
# Replay all attractors with n=10
python scripts/llm_replay.py --trace .reyn/llm_trace.jsonl \
    --from-attractor --n 10

# Only stop_with_must_rule attractors
python scripts/llm_replay.py --trace .reyn/llm_trace.jsonl \
    --from-attractor --attractor-heuristics stop_with_must_rule --n 5

# First 3 attractors only
python scripts/llm_replay.py --trace .reyn/llm_trace.jsonl \
    --from-attractor --attractor-first 3 --n 10

# Combine with patch to test a fix across all attractors
python scripts/llm_replay.py --trace .reyn/llm_trace.jsonl \
    --from-attractor \
    --patch 'tools[0].function.parameters.properties.name.enum=["skill_a","skill_b"]' \
    --n 10
```

### Output

Each attractor is replayed with a section header, followed by normal replay
output. A summary table is printed at the end:

```
Detected 3 attractor(s) — replaying each with n=10.

=== Attractor 1/3 (heuristic=stop_with_must_rule, rel=T+12.3s) ===
=== LLM Replay ===
  request_id: abc123...
  ...
=== N-shot replay (n=10) ===
  ...

=== Attractor 2/3 (heuristic=stop_with_must_rule, rel=T+24.5s) ===
  ...

=== Multi-attractor replay summary ===
Total attractors replayed: 3
Total LLM calls: 30 (= 3 × 10)
By heuristic:
  stop_with_must_rule: 2 attractors, 20 calls
  enum_violation: 1 attractors, 10 calls
Empty-stop rate by attractor:
  abc123.. (stop_with_must_rule): 5/10 (50%)
  def456.. (stop_with_must_rule): 7/10 (70%)
  ghi789.. (enum_violation): 0/10 (0%)
```

### Primary use cases

**G4 spike — attractor rate measurement across context patches.** After a
prompt change, run `--from-attractor --n 10` on the same trace before and
after to measure attractor rate change without a full dogfood session.

**Systematic fix verification.** Combine `--from-attractor` with `--patch` to
test a schema fix against every attractor in the trace in one command and
inspect the summary for empty-stop rate reduction.

## Use cases

### G4 spike — weak vs strong model comparison

Replay the same payload with a stronger model to check if the hallucination
is model-specific, without running a full dogfood session:

```bash
python scripts/llm_replay.py abc123 --trace .reyn/llm_trace.jsonl \
    --model openai/gpt-4o
```

The output shows both original and override model with token/tool-call diff.

### Attractor probability measurement

Replay the same payload 10 times to measure how often the LLM picks a
particular tool call or decision:

```bash
python scripts/llm_replay.py abc123 --trace .reyn/llm_trace.jsonl --n 10
```

Output is a distribution table: tool call names with frequencies, finish
reason distribution, and token avg/min/max.

### Router enum fix verification

Before landing a fix that adds an `enum` constraint to the router tool schema,
verify it eliminates hallucination with a single replay:

```bash
python scripts/llm_replay.py <router_request_id> --trace .reyn/llm_trace.jsonl \
  --patch 'tools[0].function.parameters.properties.name.enum=["skill_a","skill_b","skill_c"]' \
  --patch 'messages[0].content+=" Available skills (3): skill_a, skill_b, skill_c"'
```

### System prompt MUST rule injection

Add a MUST rule to the system prompt and observe whether the LLM honours it
before the instruction lands in the phase definition:

```bash
python scripts/llm_replay.py <request_id> --trace .reyn/llm_trace.jsonl \
  --patch 'messages[0].content+="\n\nMUST output flat skill names; dot-notation is forbidden."'
```

### Regression observation after prompt change

After modifying a phase's instructions, replay the exact payload that
previously triggered a bug to confirm the fix without a full dogfood run.

## Security and cost notes

- **Dump files contain sensitive prompt content.** System prompts carry project
  context and skill instructions. Do not share trace files externally.
- **Each replay is a real LLM call.** `--n 10` costs 10x a single call.
  Use the cheapest model for initial investigation; switch to stronger models
  only for targeted comparison.
- **No API keys in the dump.** Credentials are read from env vars at replay
  time, not stored in the trace file.

---

# Attractor Detection (`scripts/detect_attractor.py`)

`detect_attractor.py` reads a JSONL trace file and applies heuristic checks to
each request/response pair to flag attractor patterns — cases where the LLM
misbehaves in a structurally repeatable way regardless of prompt changes.

The tool was motivated by RETRO-H4: a MUST rule was injected into the system
prompt, yet the LLM returned `finish_reason=stop` with zero completion tokens.
Automated detection makes attractor rates measurable and comparable across
batches and model variants.

## Basic usage

```bash
python scripts/detect_attractor.py --trace <jsonl_path>
```

## Options

| Option | Description |
|--------|-------------|
| `--trace <path>` | Path to JSONL trace file (required) |
| `--heuristics <list>` | Comma-separated heuristic names to run (default: all) |
| `--output-format pretty\|json` | Output format (default: `pretty`) |
| `--summary-only` | Print aggregate counts only, not per-detection detail |
| `--filter-caller <name>` | Only inspect records where `caller_hint` matches `name` |

## Heuristics

### `stop_with_must_rule` (Heuristic 1)

Fires when:

- `finish_reason = "stop"` **and** the response carries no content (`completion_tokens=0` or empty content + no tool calls)
- **and** the request's system prompt contains MUST-directive language (`MUST`, `must call`, `must use`, `should call`, `必ず`, etc.)

Evidence includes: the finish reason, completion token count, and the matching
MUST-rule line(s) from the system prompt (up to 2 lines).

**False positive risk:** low. The two conditions are combined with AND, so an
ordinary `stop` with real content will not fire, and an empty stop without a
MUST rule will not fire either.

**False negative risk:** if the MUST rule is phrased in a way that does not
match the keyword pattern (e.g. very domain-specific Japanese phrasing), it
will be missed.

### `enum_violation` (Heuristic 2)

Fires when a tool call argument value is not in the `enum` list defined for
that argument in the request tools schema.

Evidence includes: the tool name, the field path, the expected enum list, and
the actual value supplied.

**False positive risk:** near-zero for well-formed traces. Can only fire when
both the enum constraint and a matching argument are present in the trace.

**False negative risk:** if the LLM serialises the argument differently (e.g.
nested JSON or non-string type), the check may not parse the argument correctly.

### `tool_name_hallucinate` (Heuristic 3)

Fires when a tool call uses a `function.name` that is not present in the
request tools list.

Evidence includes: the hallucinated name and the sorted list of available names.

**False positive risk:** none — the check is a strict set membership test.

**False negative risk:** if the LLM calls a real tool but passes it bad
arguments, only `enum_violation` catches it; `tool_name_hallucinate` will not.

### `describe_skill_skip` (Heuristic 4 — not implemented, future work)

Pattern: `list_skills` response appears in message history, followed immediately
by `invoke_skill` without an intervening `describe_skill` call. High false
positive rate expected; deferred to a follow-up wave.

## Output examples

### `--output-format pretty` (default)

```
=== Attractor Detection Report ===
Trace file: .reyn/llm_trace.jsonl
Total LLM calls: 25
Detected attractors: 4 (16%)

By heuristic:
  stop_with_must_rule:           2 (8%)
  enum_violation:                1 (4%)
  tool_name_hallucinate:         1 (4%)

=== Detail ===

[T+12.3s  router] stop_with_must_rule (request_id=abc123...)
  MUST rule found: "After list_skills you MUST call describe_skill or invoke_skill"
  Response: finish=stop, completion_tokens=0

[T+24.5s  router] tool_name_hallucinate (request_id=def456...)
  Hallucinated name: "skill_improver.direct_llm"
  Available names: ["describe_skill", "invoke_skill", "list_skills"]
```

### `--summary-only`

```
=== Attractor Detection Report ===
Trace file: .reyn/llm_trace.jsonl
Total LLM calls: 25
Detected attractors: 4 (16%)

By heuristic:
  stop_with_must_rule:           2 (8%)
  enum_violation:                1 (4%)
  tool_name_hallucinate:         1 (4%)
```

### `--output-format json`

```json
{
  "trace_file": ".reyn/llm_trace.jsonl",
  "total_calls": 25,
  "summary": {
    "stop_with_must_rule": 2,
    "enum_violation": 1,
    "tool_name_hallucinate": 1
  },
  "detections": [
    {
      "request_id": "abc123...",
      "timestamp": "2026-01-01T00:00:12+00:00",
      "rel_time": "T+12.3s",
      "caller": "router",
      "heuristic": "stop_with_must_rule",
      "evidence": {
        "finish_reason": "stop",
        "completion_tokens": 0,
        "must_rule_excerpts": ["After list_skills you MUST call describe_skill or invoke_skill"]
      }
    }
  ]
}
```

## Primary use cases

**G4 spike — weak vs strong model comparison.** Run detection on a weak-model
trace, then on a strong-model trace for the same session. Compare attractor
counts to confirm whether the model change eliminated the pattern.

**Batch-to-batch attractor rate tracking.** After a prompt change or OS fix,
compare `--summary-only --output-format json` output across batch traces to
verify the rate dropped.

**Production skill self-check.** Skill developers can enable `REYN_LLM_TRACE_DUMP`
during local development and run detection to check whether their skill's
prompts trigger attractors before submitting a PR.

**Targeted investigation with `--filter-caller`.** Focus on a single caller
(e.g. `router`) to isolate router-layer attractors from phase-layer noise.
