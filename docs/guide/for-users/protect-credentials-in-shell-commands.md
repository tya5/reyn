---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, sandboxed_exec, hooks]
---

# Protect credentials from sandboxed commands

Any command reyn runs through the sandbox — a `sandboxed_exec` op, or a
hook's `exec` / `exec_capture` — is still your machine's process. The
sandbox bounds *where a process can write* and *how it may reach the
network* (see [Configure the sandbox](configure-sandbox.md)); it does not
curate *what the command can see*. **Choose what you let a sandboxed
command run with the same trust you'd give a command typed into your own
shell.**

This is a standing design choice, not a gap waiting to be closed: reyn's
security mechanisms are opt-in, favoring predictability and a small
number of knobs over a machine that silently decides what an operator
did or didn't mean to expose. **Protecting a credential from a sandboxed
command is your responsibility, exercised at the point you write the
command** — not something the sandbox does for you underneath.

## Network and environment variables are both open by default

`network` in `sandbox.policy` defaults to `true` (owner decision,
2026-06-05) — a sandboxed child process can reach the network the same
way your own shell can, unless you set `network: false` for it:

```yaml
sandbox:
  policy:
    network: false
```

A sandboxed command's environment is, by the same posture, your shell's
full environment — not a curated subset by default. Every variable your
shell has set is visible to the command unless you explicitly deny
specific names via `deny_env_names` (`#3901`/`#3823` — the whole
environment passes through by default, same trust level as the launching
shell):

```yaml
sandbox:
  policy:
    deny_env_names:
      - OPENAI_API_KEY
```

If you'd rather opt IN a short list than deny individual names, set
`allow_env_names` to a list — this switches the axis to allow-list
semantics (`#3823`): only the names you list pass through, still
intersected with `deny_env_names` on top:

```yaml
sandbox:
  policy:
    allow_env_names:
      - PATH
      - HOME
```

Both defaults follow the same design choice this page opened with:
reyn's sandbox re-decides nothing the launching shell could already do,
unless you tell it to.

## The command's stdout becomes the agent's context

Whatever a `sandboxed_exec`/`exec_capture` command prints to stdout is
returned as the op's result and read directly by the LLM in the next
turn. Don't run a command that might print a secret to stdout — an
`echo $API_KEY`, a verbose client that logs a bearer token, a tool that
dumps its own config — even if you trust where the command's *output* is
going next, because the very next place it goes is the model's context
window.

## Choosing which commands to run — the method, not a list

There is no list of "safe" commands, because safety depends on what your
own environment currently holds, which reyn cannot see from inside a
command's argv. The questions that matter, before you let a
`sandboxed_exec`/hook command run:

- **Would you run this exact command in your own shell, in this
  environment, right now?** If the answer is no — because you're not sure
  what it reads, or you'd want to check first — that's the answer for the
  sandboxed version too. The sandbox does not make an otherwise-risky
  command safe to run unattended.
- **Does the command need to read the environment at all?** A command
  that has no reason to touch env vars can't leak one it never reads. Keep
  the command's own scope narrow rather than relying on the sandbox to
  narrow it for you.
- **Is a secret reachable from THIS shell's environment, not just from a
  file on disk?** A credential sitting in `~/.aws/credentials` is not
  exposed by a command's env unless something has already loaded it into
  a variable. Check what your shell — and therefore reyn's — actually has
  set before assuming a command is safe because you don't see the
  credential in the config you wrote.

## Credential env vars reyn's own LLM providers read

reyn's own LLM-provider clients read credentials from these environment
variables today: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`AZURE_API_KEY`, `GOOGLE_API_KEY`, `LITELLM_API_KEY`.

**This list is not the boundary of what's risky — it's an example of the
shape.** Every LLM provider, cloud vendor, and SaaS tool you use has its
own credential env var, and reyn has no way to enumerate all of them for
you. Any variable your shell has set that a command could read is worth
the same scrutiny, whether or not it appears on this list.

## See also

- [Configure the sandbox](configure-sandbox.md) — the sandbox's actual
  scope (paths, network, subprocess spawn) and what it does and does not
  bound
- [Concepts: Sandbox and permissions](../../concepts/architecture/sandbox-vs-permission.md) —
  why sandbox and permission are separate layers with different jobs
