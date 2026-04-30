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
| `SKILL` | Skill name. Resolved in order: `reyn/project/<name>` → `reyn/local/<name>` → `src/stdlib/skills/<name>`. |
| `INPUT` | Initial input. JSON string is used as-is (must be a valid artifact). Natural-language string is auto-wrapped as `{"type": "user_message", "data": {"text": "..."}}`. Reads stdin when omitted. |

## Options

| Flag | Description |
|------|-------------|
| `--skill-path DIR` | Path to a skill directory (overrides name resolution). |
| `--module MODULE` | Python module path exposing a `skill` object. |
| `--dsl-root DIR` | Root of the DSL tree for shared artifact/phase resolution. |
| `--model MODEL` | Model class (`light` / `standard` / `strong`) or LiteLLM model string. Resolved via `reyn.yaml`'s `models` map. |
| `--output-language LANG` | Output language code. Default from `reyn.yaml`. |
| `--max-phase-visits N` | Cap on single-phase revisits per run. `0` = unlimited. Default `25`. |
| `--events` | Print the full event log after execution. |
| `--strict` | Enforce required fields at every nesting depth (default: top-level only). |
| `--rich` | Use Rich-styled console output. |
| `--allow-shell` | Enable the `shell` Control IR op. Off by default. |
| `--allow-untrusted-python` | Enable trusted-mode Python preprocessor steps (no AST sandbox). |

## Examples

Run a stdlib skill with natural-language input:

```bash
reyn run text_summarizer "reyn is a workflow OS for LLMs."
```

Run with structured JSON input:

```bash
reyn run my_skill '{"type": "topic_input", "data": {"topic": "ml"}}'
```

Run from stdin:

```bash
echo "summarize this text" | reyn run text_summarizer
```

Replay events afterwards:

```bash
reyn run text_summarizer "..." --events
```

Run a meta-skill that needs shell access:

```bash
reyn run skill_improver "improve my_skill" --allow-shell
```

## See also

- [Reference: skill.md frontmatter](../dsl/skill-md.md)
- `reference/runtime/events.md` — event types (Phase 2)
- [Concepts: architecture](../../concepts/architecture.md)
