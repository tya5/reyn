---
type: concept
topic: codeact
audience: [human, agent]
---

# CodeAct: Code-as-Tools

CodeAct is one of Reyn's [tool-use schemes](tool-use-schemes.md) — the way the
LLM is shown its tools and the way its calls are turned into dispatched actions.
Where the default scheme presents tools as JSON function definitions and the LLM
emits a JSON tool-call, **CodeAct presents tools as a code API and the LLM writes
a Python snippet that calls them**. The model's job shifts from "emit a tool
call" to "write code."

This page is the deep dive. For the menu of all schemes and how to pick one, see
[Tool-Use Schemes](tool-use-schemes.md).

## What it is

In CodeAct, the LLM does not return a structured tool-call. It writes a fenced
Python block in its message, and tool use happens as ordinary-looking function
calls **inside** that snippet:

```python
result = tool('file__read', path='README.md')
```

Each `tool(...)` call performs one action and returns its result (or raises if
the action is denied, excluded, or unknown). The model composes its work in code —
loops, intermediate variables, conditionals — and reaches the outside world
**only** through `tool()`.

## How it works

### The code API

Instead of a JSON `tools=` payload, CodeAct builds a **code API**: a reference
list of the actions the model may call, rendered into the system prompt as
`tool(...)` signatures. Each permission-eligible action appears as one line —
its name, parameters, and a short description. This list is *presentation only*;
the model reads it to know what's callable. The JSON tool payload is empty under
CodeAct — the model is not offered JSON tool-calling at all.

### Sandboxed subprocess

The snippet does not run in the agent's process. It executes in a **sandboxed
subprocess** under the platform sandbox backend (Seatbelt on macOS, Landlock on
Linux). Direct filesystem writes, network access, and subprocess spawning from
inside the snippet are blocked by the sandbox — the snippet's *only* sanctioned
channel to the outside world is `tool()`.

### The duplex permission-proxy socket

This is the load-bearing part. The snippet runs sandboxed and holds **no
permission authority** of its own. So how does an in-snippet `tool()` call
actually perform an action?

Each `tool(...)` call marshals its name and arguments over an `AF_UNIX`
socketpair back to the **parent** (the agent process), which services the
request and writes the result back. On the parent side, that request goes
through the **exact same gate every other scheme's tool call goes through**:

```
tool('x', ...)  →  socket  →  parent: exclude-check → permission-check → dispatch_tool  →  result  →  socket  →  snippet
```

The child snippet never touches Reyn internals; it cannot reach a tool the OS
hasn't made eligible, and every call is permission-checked before any side
effect. A CodeAct call is therefore gated **at least as strictly** as the
equivalent JSON call — the same gate, *plus* sandbox containment.

This is the key property: **the scheme changes only the LLM-facing surface — how
the model is asked to express tool use — not what it is permitted to do.** The
exclude → permission → dispatch pipeline (P4/P5) is unchanged. Swapping to
CodeAct does not weaken security or validation.

## The turn contract

A CodeAct turn is **exactly one** of two things, never both:

1. **An action turn** — a single fenced ` ```python ` block and nothing else (no
   prose before or after). The model runs its actions in that block.
2. **A final answer** — plain prose with no code block. This ends the turn.

Inside an action block, the model assigns its final answer to `result` when it's
done computing, and `tool(...)` is the only way to take an action. When the model
has its answer and needs no more actions, it replies in plain prose with no code
block — that is the signal the turn is complete. A response with no fenced block
is read as the plain-prose final answer, **not** as bare code to execute.

## When to use it

CodeAct is opt-in. It's worth choosing when:

- **The model handles code more reliably than JSON tool-calls.** For some models —
  particularly weaker ones — expressing tool use as code is more dependable than
  producing well-formed JSON tool-calls, improving tool-use reliability.
- **A task composes many steps in one turn.** Writing `list → loop-read →
  aggregate` as a short program lets the model do in one turn what would
  otherwise be many sequential round-trip tool-calls.

Use the default `universal-category` scheme otherwise; CodeAct's value is
specific to those two situations.

## How to enable

CodeAct is selected per layer in `reyn.yaml`, like any scheme. The default is
`universal-category`, so CodeAct is opt-in:

```yaml
# reyn.yaml
tool_use:
  chat: codeact     # top-level chat router
  step: universal-category
  phase: universal-category
```

Any of `chat` / `step` / `phase` can independently use `codeact`. See
[`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block).

## Security note

To restate the load-bearing guarantee: **whichever scheme is active, every tool
call passes the same gate** — eligibility (exclude), permission, then dispatch.
CodeAct adds a sandboxed subprocess and routes each in-snippet `tool()` call back
through that same parent-side gate over the permission-proxy socket. The snippet
cannot bypass it. Choosing CodeAct changes how the model expresses tool use; it
does not change what the model is allowed to do, and it does not weaken Reyn's
permission or validation model. See [Permission model](../runtime/permission-model.md).

## See also

- [Tool-Use Schemes](tool-use-schemes.md) — the scheme menu + when to pick which
- [Universal Action Catalog](universal-catalog.md) — the default scheme's internals
- [Permission model](../runtime/permission-model.md) — the gate every scheme dispatches through
