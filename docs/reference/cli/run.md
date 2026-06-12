---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run]
---

# `reyn run`

Run a skill end-to-end.

## Synopsis

```
reyn run [OPTIONS] [SKILL] [INPUT]
```

## Positional arguments

| Name | Description |
|------|-------------|
| `SKILL` | Skill name. Resolved in order: `reyn/project/<name>` → `reyn/local/<name>` → `src/reyn/stdlib/skills/<name>`. |
| `INPUT` | Initial input. JSON string is used as-is (must be a valid artifact). Natural-language string is auto-wrapped as `{"type": "user_message", "data": {"text": "..."}}`. Reads stdin when omitted. |

## Options

| Flag | Description |
|------|-------------|
| `--skill-path DIR` | Path to a skill directory (overrides name resolution). |
| `--module MODULE` | Python module path exposing a `skill` object. |
| `--skill-root DIR` | Root of the skill tree for shared artifact/phase resolution. Inferred automatically when using `--skill-path`; override when the inferred root is wrong. |
| `--model MODEL` | Model class (`light` / `standard` / `strong`) or LiteLLM model string. Resolved via `reyn.yaml`'s `models` map. |
| `--output-language LANG` | Output language code. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per run. `0` = unlimited. Default `25`. |
| `--events` | Print the full event log after execution. |
| `--strict` | Enforce required fields at every nesting depth (default: top-level only). |
| `--allow-unsafe-python` | Enable unsafe-mode Python preprocessor steps (no AST sandbox). `--allow-untrusted-python` is a legacy alias for backwards compatibility. |

## Examples

Run a stdlib skill with natural-language input:

```bash
reyn run direct_llm "reyn is a workflow OS for LLMs."
```

Run with structured JSON input:

```bash
reyn run my_skill '{"type": "topic_input", "data": {"topic": "ml"}}'
```

Run from stdin:

```bash
echo "summarize this text" | reyn run direct_llm
```

Replay events afterwards:

```bash
reyn run direct_llm "..." --events
```

## See also

- [Reference: skill.md frontmatter](../dsl/skill-md.md)
- `reference/runtime/events.md` — event types (Phase 2)
- [Concepts: architecture](../../concepts/architecture/architecture.md)
