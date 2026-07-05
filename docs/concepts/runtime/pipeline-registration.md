---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [pipeline registration, pipelines directory, register pipeline, run_pipeline, pipeline DSL, PipelineRegistry, scan_dirs, pipeline__run, call step target, load pipeline from disk, add a pipeline]
---

# Pipeline registration

A **pipeline** is a deterministic, multi-step control flow written in the
pipeline DSL (a directory of YAML documents). Once registered, an agent can
launch it by name with `run_pipeline` (or the catalog verb `pipeline__<name>`),
and one pipeline can `call` another by name.

Pipelines are registered from disk: the operator drops DSL files into a scanned
directory and they are loaded, parsed, and registered when a session starts.
This mirrors how skills are registered — pipeline definition files are
operator-owned **source** (like `skills/` and `reyn.yaml`), not runtime state,
so they live at the project root, not under `.reyn/`.

## Adding a pipeline

1. Create a `pipelines/` directory at the project root (the default scan dir).
2. Drop one Appendix-B DSL document per `*.yaml` file into it. Each file
   declares its name with a top-level `pipeline:` key:

   ```yaml
   # pipelines/hello.yaml
   pipeline: hello
   description: Minimal greeting pipeline.
   steps:
     - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
   ```

3. Start (or restart) the session. The pipeline is now registered under its
   declared name and an agent can launch it:

   ```
   run_pipeline(name="hello", input={name: "Reyn"})
   ```

   or via the qualified catalog verb the action catalog surfaces:

   ```
   pipeline__hello({name: "Reyn"})
   ```

No `reyn.yaml` entry is required — dropping a file in `pipelines/` is enough.

## The declared name is authoritative

A pipeline registers under the name in its `pipeline:` key, **not** its file
name. A file `greet.yaml` that declares `pipeline: hello` registers as `hello`.
The declared name is the identity a `call` (or `match`) step's `pipeline:`
target resolves against, so the file name is just a container — name your files
however you like.

## Configuration

The scan directories are configurable. The default is `["pipelines"]`
(project-root-relative):

```yaml
# reyn.yaml
pipelines:
  scan_dirs: ["pipelines", "shared/pipelines"]
```

## Failure behavior — fail loud

Loading is strict, so a broken definition is never silently dropped:

| Condition | Behavior |
|-----------|----------|
| Malformed DSL file | Session start fails, naming the offending file. |
| Two files declaring the same `pipeline:` name | Session start fails, naming the collision. |
| A configured scan dir that does not exist | Skipped (not an error) — add files later. |
| No `pipelines/` dir / no files | No pipelines registered (empty registry). |

## Security — launching a pipeline stays gated

Registering a pipeline does **not** loosen the capability floor. Launching a
pipeline (`run_pipeline` / `run_pipeline_async` / the inline launch verbs, and
their `pipeline__*` catalog forms) is on the same restricted floor as spawning a
sub-session or re-delegating: a pipeline step can itself write, execute, or
delegate, so a pipeline launch is a cost-bound multi-step dispatch.

As a result, a context narrowed by the `_untrusted` floor (untrusted external
content is live) or the `_delegate` floor (an unbound delegate under
`delegation.capability_default=deny`) **cannot launch a pipeline**, whether or
not one is registered. Loading a pipeline definition makes it available to
authorized agents; it never creates a bypass of those floors. See
[Capability profiles](capability-profile.md) and
[Delegation policy](delegation-policy.md).
