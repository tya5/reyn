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

`T+` is seconds since the first request in the trace.

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
