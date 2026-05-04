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

## Security and cleanup

- **No API keys in the dump.** Auth credentials never appear in messages or
  tools — Reyn reads them from env vars, not the message list.
- **Prompts may contain sensitive content.** System prompts carry project
  context and skill instructions. Treat the file as internal.
- **Add to `.gitignore`.** The dump path (`.reyn/llm_trace.jsonl` by default)
  should not be committed:
  ```
  .reyn/llm_trace.jsonl
  ```
- **Delete after a session.** The file grows unbounded across sessions; delete
  it before starting a new dogfood run.

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
