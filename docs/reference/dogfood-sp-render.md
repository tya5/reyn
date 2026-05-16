# SP Render Inspector (`scripts/dogfood_sp_render.py`)

A CLI for verifying system-prompt output during skill development — replacing the
one-off `python -c "..."` scripts that were previously the only way to preview
what the OS actually injects into a LLM call.

## Why

Before this tool, checking a system prompt required writing an ad-hoc Python
snippet each time: import the SP builder, construct a fake context, call render,
print. The snippet varied by what you wanted to inspect (size? sections?
legacy-literal audit?), and it was never saved. Debugging a routing problem
could cost five variants of the same throwaway script in a single session.

`dogfood_sp_render.py` canonicalises that workflow into six named modes. You
pass the flags that describe your agent's configuration, pick a mode, and get
a consistent, repeatable output.

## Setup

No installation beyond the project's standard dependencies. The script lives in
`scripts/` and uses only modules already in the virtualenv:

```bash
# From the project root
python scripts/dogfood_sp_render.py [flags] [mode]
```

## Usage

### Default mode — full SP preview

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --agent-role "Coding assistant" \
  --skill "skill_builder=Builds and improves skills" \
  --skill "eval=Runs evaluation scenarios"
```

Prints the rendered system prompt to stdout. Pipe to `less` for long prompts.

### `--stats` — character and line counts

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --stats
```

Output:

```
2635 chars / 47 lines
```

Use this to monitor SP size during development. A sudden size increase after a
commit indicates an unexpected injection.

### `--show-sections` — SP structural overview

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --show-sections
```

Output:

```
## Capabilities (routing guide)
## Action categories
## Behaviour
```

Use this to confirm expected sections are present without reading the full SP.

### `--grep-legacy` — legacy literal audit

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --grep-legacy
```

Exits with code 0 if no legacy literals are found. Exits with code 1 and
prints the offending lines if any are found. Designed for pre-commit hooks and
CI checks — see [Integration with workflow](#integration-with-workflow).

### `--compare-legacy` — size delta between wrapper and legacy SP

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --compare-legacy
```

Output:

```
Legacy: 9,029 chars / Wrapper: 2,635 chars / Reduction: 70.8%
```

Use during dogfood prep to confirm the wrapper SP is significantly smaller than
the legacy SP. A regression (wrapper larger than legacy, or reduction below 50%)
is a signal to investigate what is being injected unexpectedly.

### `--legacy-check` — boolean pass/fail for legacy mode

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --legacy-check
```

Prints `PASS` or `FAIL` to stdout and exits with the corresponding code. A
lighter alternative to `--grep-legacy` when only pass/fail status is needed.

## Flag reference

### Agent identity flags

| Flag | Description |
|------|-------------|
| `--agent-name NAME` | Name of the agent (used in SP preamble) |
| `--agent-role TEXT` | Role description injected into the SP |

### Capability flags (repeatable)

| Flag | Description | Format |
|------|-------------|--------|
| `--skill name=desc` | Skill available to this agent | `name=description` |
| `--agent-peer name=role` | Peer agent this agent can delegate to | `name=role` |
| `--mcp-servers name=desc` | MCP server this agent has access to | `name=description` |
| `--indexed-sources name` | Indexed source name (for RAG-aware agents) | plain name |

### Scope and context flags

| Flag | Description |
|------|-------------|
| `--file-scope read=path write=path` | File scope for permission-aware rendering |
| `--output-language LANG` | Output language code (e.g. `ja`, `en`) |
| `--project-context TEXT` | Project context injected into the SP |

### Rendering mode flags

| Flag | Description |
|------|-------------|
| `--hide-legacy-tools` | Render with legacy tool definitions hidden |
| `--universal-wrappers-enabled` | Enable universal wrapper mode |

### Output mode flags (mutually exclusive)

| Flag | Description |
|------|-------------|
| `--stats` | Print char/line count only |
| `--show-sections` | Print section headers only |
| `--grep-legacy` | Audit for legacy literals; exit 1 if found |
| `--compare-legacy` | Print wrapper vs legacy size comparison |
| `--legacy-check` | Print PASS/FAIL and exit accordingly |

## Integration with workflow

### Before commit — leak check

Run `--grep-legacy` to confirm that wrapper-only mode does not contain any
legacy literals that would break routing:

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --universal-wrappers-enabled \
  --skill "skill_builder=Builds and improves skills" \
  --grep-legacy
```

Exit code 1 means a literal leaked through; inspect the printed lines to
identify the source.

### During dogfood prep — size delta observation

Check the reduction ratio before each dogfood batch to establish a baseline.
A ratio below 50% after a code change is worth investigating before running
expensive LLM sessions:

```bash
python scripts/dogfood_sp_render.py \
  --agent-name my_agent \
  --skill "skill_builder=Builds and improves skills" \
  --compare-legacy
```

### When debugging routing problems

Use `--show-sections` to confirm all expected sections are present, then switch
to the default mode to read the full SP for the specific section where the LLM
is misbehaving:

```bash
# 1. Confirm sections
python scripts/dogfood_sp_render.py --agent-name my_agent --skill "..." --show-sections

# 2. Read full SP
python scripts/dogfood_sp_render.py --agent-name my_agent --skill "..." | less
```

### When NOT to use this tool

- **Inspecting what a live session actually sent to the LLM.** This tool renders
  the SP from flags you provide; it does not read a running session's state.
  For that, use `REYN_LLM_TRACE_DUMP` + `dogfood_trace.py`.
- **Auditing LLM behavior.** SP rendering is input-side only. For output-side
  behavior, use `llm_replay.py`.

## See also

- [LLM Payload Tracing](dogfood-tracing.md) — capturing and inspecting live
  LLM payloads (`REYN_LLM_TRACE_DUMP`, `dogfood_trace.py`, `llm_replay.py`)
