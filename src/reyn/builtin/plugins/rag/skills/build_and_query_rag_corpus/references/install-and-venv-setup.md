# Install + venv setup (register-only, #3209)

`plugin_management__install` **registers** the `rag` plugin's capabilities
(both MCP servers, both pipelines, this skill) into your project's config --
it does **not** install the plugin's Python dependencies for you. That is a
separate, deliberate step: reyn's install op is registration-only, never a
foreign env-provisioning responsibility. Do this BEFORE the servers are
probed at registration time (an unready venv fails the probe the same way a
missing dependency would).

## 1. Install (registers, does not provision deps)

```
plugin_management__install(source={"kind": "builtin", "name": "rag"})
mcp__install_local(name="reyn_markitdown", command="uvx", args=["markitdown-mcp"])
```

No `permissions:` block to add for the mcp servers -- a server in the merged
config is granted when the pipeline runs it.

## 2. Create the rag plugin's own venv (once, in-sandbox)

Both `reyn_chunker` and `reyn_vector_store` are standalone scripts -- they
never import reyn -- so their venv needs ONLY the plugin's own
`requirements.txt`, never reyn itself:

```bash
python3 -m venv ~/.reyn/plugins/rag/.venv
~/.reyn/plugins/rag/.venv/bin/pip install -r ~/.reyn/plugins/rag/requirements.txt
```

**Windows** -- the interpreter path is `Scripts\python.exe`, not `bin/python`:

```powershell
python -m venv %USERPROFILE%\.reyn\plugins\rag\.venv
%USERPROFILE%\.reyn\plugins\rag\.venv\Scripts\pip.exe install -r %USERPROFILE%\.reyn\plugins\rag\requirements.txt
```

## 3. Point the registered servers at that venv

Edit `.reyn/config/mcp.yaml`'s `mcp.servers.reyn_chunker.command` and
`mcp.servers.reyn_vector_store.command` (the two entries
`plugin_management__install` just wrote) to your venv's own interpreter,
absolute path, no exceptions:

```yaml
mcp:
  servers:
    reyn_chunker:
      command: /home/you/.reyn/plugins/rag/.venv/bin/python   # Windows: ...\.venv\Scripts\python.exe
      # ... plugin_management__install's own args/env fields unchanged ...
    reyn_vector_store:
      command: /home/you/.reyn/plugins/rag/.venv/bin/python   # Windows: ...\.venv\Scripts\python.exe
      # ... plugin_management__install's own args/env fields unchanged ...
```

If you skip this (or the venv is incomplete), spawning either server
**fails fast with a clear OS-level error** -- reyn never falls back to
fetching the missing dependency at spawn time (#3060 preserved).

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
give it its **own venv** -- never reyn's -- and point `command` at the
absolute path:

```bash
python3 -m venv ~/.reyn-markitdown && ~/.reyn-markitdown/bin/pip install markitdown-mcp
mcp__install_local(name="reyn_markitdown", args=[],
                   command="/abs/path/.reyn-markitdown/bin/markitdown-mcp")
```
