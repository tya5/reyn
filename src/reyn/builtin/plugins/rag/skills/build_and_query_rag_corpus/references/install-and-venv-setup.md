# Install + venv setup (register-only, #3209) -- full detail

The SKILL.md body already carries the load-bearing steps (install, create
the venv, point the servers at it). This file is the supplementary detail:
troubleshooting, the markitdown fallback venv, and the reasoning behind the
workspace-relative venv path.

## Why the venv must be INSIDE the project workspace, not `~/.reyn/...`

The sandbox's write scope for `sandboxed_exec` (what an LLM-driven `python3
-m venv ...` / `pip install ...` actually runs under) is tight to the
current project workspace -- it cannot be widened by the LLM. A venv path
under the operator's home directory (`~/.reyn/plugins/rag/.venv`, the
original ADR 0064 §3.11 sketch) is OUTSIDE that scope, so creating it fails
with `Operation not permitted`, and the flow stalls silently (nothing after
the failed `python -m venv` call ever runs). A home-dir path is also GLOBAL
across every project/session on the machine -- two unrelated projects
racing the same `~/.reyn/plugins/rag/.venv` collide.

`./.venv-rag` (a directory at the CURRENT project's root) is inside the
workspace write scope by construction -- no permission wall, no operator
keypress, and it is scoped to just this project.

## 1. Install (registers, does not provision deps)

```
plugin_management__install(source={"kind": "builtin", "name": "rag"})
mcp__install_local(name="reyn_markitdown", command="uvx", args=["markitdown-mcp"])
```

No `permissions:` block to add for the mcp servers -- a server in the merged
config is granted when the pipeline runs it.

## 2. Create the rag plugin's own venv, INSIDE the project (once)

Both `reyn_chunker` and `reyn_vector_store` are standalone scripts -- they
never import reyn -- so their venv needs ONLY the plugin's own
`requirements.txt`, never reyn itself. The `requirements.txt` file itself
still lives under the GLOBAL plugin copy (`~/.reyn/plugins/rag/`, written
once by install) -- only the venv you create from it goes in the project:

```bash
python3 -m venv ./.venv-rag
./.venv-rag/bin/pip install -r ~/.reyn/plugins/rag/requirements.txt
```

**Windows** -- the interpreter path is `Scripts\python.exe`, not `bin/python`:

```powershell
python -m venv .venv-rag
.venv-rag\Scripts\pip.exe install -r %USERPROFILE%\.reyn\plugins\rag\requirements.txt
```

## 3. Point the registered servers at that venv

Edit `.reyn/config/mcp.yaml`'s `mcp.servers.reyn_chunker.command` and
`mcp.servers.reyn_vector_store.command` (the two entries
`plugin_management__install` just wrote) to your venv's own interpreter,
absolute path. **Do not touch `args`** -- it already holds the plugin's own
absolute script path (e.g. `~/.reyn/plugins/rag/scripts/chunker_server.py`),
written correctly by install; there is no `-m <module>` form to rewrite it
into.

```yaml
mcp:
  servers:
    reyn_chunker:
      command: /abs/path/to/this/project/.venv-rag/bin/python   # Windows: ...\.venv-rag\Scripts\python.exe
      # args: unchanged -- already the plugin's own absolute script path
    reyn_vector_store:
      command: /abs/path/to/this/project/.venv-rag/bin/python   # Windows: ...\.venv-rag\Scripts\python.exe
      # args: unchanged -- already the plugin's own absolute script path
```

If you skip this (or the venv is incomplete), spawning either server
**fails fast with a clear OS-level error** -- reyn never falls back to
fetching the missing dependency at spawn time (#3060 preserved).

## Common mistakes (seen in practice)

- **Venv under `~/.reyn/...`** instead of `./.venv-rag` -- fails with a
  permission error the moment `python -m venv` runs; the whole flow then
  silently never reaches the ingest/query step.
- **`args` rewritten** to something like `["-m", "reyn_chunker"]` -- there
  is no such installable module; `args` is a file PATH, and the correct
  value was already written by `plugin_management__install`. Only `command`
  changes.
- **`venv/` instead of `.venv-rag/`** -- the leading dot and the `-rag`
  suffix both matter (a bare `venv/` risks colliding with an unrelated venv
  already in the project).

## If `rag_ingest` reports a server unreachable

`rag_ingest` pre-flights all three servers and returns a **"blocked"**
message before spending on embeddings:

- **Not registered yet**: install above, re-run.
- **Operator refused install**: stop and relay it -- a refusal is an answer,
  not an error to route around. Do not shell out, hand-roll an ingest, or
  re-ask.
- **Venv/deps missing** (the common case post-registration): do steps 2-3
  above -- your sandbox, your call to run pip; do not silently work around
  it.

## markitdown -- separate, third-party, not part of the rag plugin

**Never `pip install markitdown-mcp` beside reyn** -- `uvx` fetches it into
an isolated environment instead. If `uvx` cannot reach PyPI (firewalled),
give it its **own venv, also inside the project workspace** -- never
reyn's, never a home-dir path (same write-scope reasoning as above) -- and
point `command` at the absolute path:

```bash
python3 -m venv ./.venv-markitdown && ./.venv-markitdown/bin/pip install markitdown-mcp
mcp__install_local(name="reyn_markitdown", args=[],
                   command="/abs/path/to/this/project/.venv-markitdown/bin/markitdown-mcp")
```
