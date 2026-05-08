---
type: reference
topic: runtime
audience: [human, agent]
---

# Control IR

Control IR is the list of side-effect operations the LLM may emit alongside its artifact. The OS dispatches each op and returns the result for the LLM (or the next phase) to consume.

## Op kinds

| Kind | Purpose | Permission required |
|------|---------|---------------------|
| `file` | Read, write, glob, grep, edit, or delete files | `file.<op>` |
| `ask_user` | Pause the phase and ask the user a question | none (always allowed) |
| `run_skill` | Run another skill as a sub-workflow | none (skill-level decision) |
| `lint` | Run the DSL linter on a skill directory | none |
| `shell` | Run a shell command | `shell` (off by default; needs `--allow-shell`) |

## Common envelope

Every op is a JSON object with a `kind` discriminator:

```json
{
  "kind": "file",
  "op": "read",
  "path": "src/foo.py"
}
```

The OS validates the op against its kind's schema, executes it, and returns a result to the calling phase.

## `file`

Sub-operations: `read`, `write`, `edit`, `delete`, `glob`, `grep`.

```json
{"kind": "file", "op": "read", "path": "src/foo.py"}

{"kind": "file", "op": "write", "path": "out.txt", "content": "..."}

{"kind": "file", "op": "edit", "path": "src/foo.py",
 "old_string": "...", "new_string": "..."}

{"kind": "file", "op": "delete", "path": "tmp.txt"}

{"kind": "file", "op": "glob", "pattern": "**/*.py"}

{"kind": "file", "op": "grep", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "output_mode": "content"}
```

Permission scopes are configured per-op kind. See `reference/config/permissions.md`.

## `ask_user`

Pauses the phase and asks the user. The OS prints the question, reads stdin, and re-runs the *same phase* with the answer merged into the input as a `user_message` artifact. Visit count does not increment.

```json
{
  "kind": "ask_user",
  "question": "Which model do you want to target?",
  "suggestions": ["light", "standard", "strong"]
}
```

## `run_skill`

Runs another skill as a sub-workflow. The result is returned as a structured artifact for the calling phase to use.

```json
{
  "kind": "run_skill",
  "skill": "recall_memory",
  "input": {"type": "user_message", "data": {"text": "what did I tell you about my preferences?"}}
}
```

For deterministic invocation from a phase's preprocessor (rather than LLM-driven), use the `run_skill` preprocessor step instead — see `reference/dsl/preprocessor.md`.

## `lint`

Runs the DSL linter on a skill directory. Used by skill-building skills (`skill_builder`, `skill_improver`) to verify their output.

```json
{
  "kind": "lint",
  "skill_path": "reyn/local/my_skill"
}
```

## `shell`

Executes a shell command. **Off by default.** The runtime must be started with `--allow-shell` AND the project must permit `shell` in `reyn.yaml` (or grant per-run via prompt).

```json
{
  "kind": "shell",
  "cmd": "reyn run my_skill 'hello'",
  "timeout": 120
}
```

If shell is denied, the OS emits `shell_not_allowed` and returns a denial result rather than failing the phase.

## Where ops are exposed to the LLM

The OS injects available ops into every context frame as `available_control_ops`. Each entry includes a `kind`, a one-line description, and a worked example. The LLM picks ops by matching its intent to descriptions — phase markdown MUST NOT describe op syntax (P8).

## See also

- [run.md](../cli/run.md) — `--allow-shell`, `--allow-untrusted-python`
- [events.md](events.md) — events emitted per op kind
- [Concepts: principles P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)
