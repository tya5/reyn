---
type: skill
name: read_local_files
description: |
  Read one or more local project files via a configured filesystem MCP
  server, then synthesise an answer that references their contents. The
  first stdlib skill that demonstrates the `mcp` Control IR op end-to-end
  — copy this pattern when wiring a new MCP server into a skill.
entry: decide_files
final_output: file_content_response
final_output_description: |
  A natural-language answer that uses the contents of one or more local
  files, plus the list of paths actually read.
finish_criteria:
  - The phase has produced `file_content_response` with a non-empty `response`
    and the list of files that were read in `files_read`
graph:
  decide_files: [read_and_respond]
  read_and_respond: []
permissions:
  mcp: [filesystem]
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
routing:
  intents: [task]
  priority: normal
  when_to_use:
    - User wants to read or analyse local project files (README, source
      code, config, docs)
    - "「プロジェクトの構成を見せて」「READMEを要約して」 — read-and-respond
      style requests over the local checkout"
    - Inspecting code under a specific directory ("what's in src/foo/?")
    - Locating files by content within the project tree
  when_not_to_use:
    - User wants to **write or modify** files — this skill is read-only
    - Multi-step refactoring or code-generation tasks (use a custom skill
      that combines read + plan + write phases)
    - Files outside the configured filesystem MCP server's allowed path
      scope (the server enforces its own root; we cannot read /etc, ~/.ssh, …)
    - Single-shot LLM tasks that don't need any file access (use `direct_llm`)
  examples:
    positive:
      - "Read the README and summarise the philosophy section"
      - "プロジェクトの構成を見せて"
      - "What's in src/reyn/op_runtime/?"
      - "Find all files mentioning 'topology' under docs/"
      - "src/reyn/runtime.py の概要を教えて"
    negative:
      - "Edit the file"                        # write op — out of scope
      - "Create a new file under src/"         # write op — out of scope
      - "Search the web for X"                 # different server / skill
      - "Read /etc/passwd"                     # outside project scope
      - "Improve this skill"                   # → skill_improver
---

## Overview

`read_local_files` is the first stdlib skill that actually **calls** an
MCP server (the existing `mcp_search` only browses the registry via
`web_fetch`). The skill is intentionally small so end users can copy it as
a template when integrating their own MCP servers.

Two phases:

1. **`decide_files`** — pure planning. The LLM reads `user_message.text`
   and produces a `read_plan` listing the paths to fetch and a one-line
   reason. No ops. One act turn.
2. **`read_and_respond`** — execution + synthesis. The LLM emits one
   `mcp` op per path, the OS gates each call through the per-server
   permission, results land in `control_ir_results`, and the LLM composes
   a single natural-language answer plus the list of files actually read.

## Required MCP server configuration

This skill assumes a server **named `filesystem`** is configured in
`reyn.yaml` (or `.reyn/config.yaml`) and points at the user's project
root. A typical entry:

```yaml
mcp:
  servers:
    filesystem:
      url: http://localhost:8765/mcp
      # The server itself enforces which directory tree it will serve;
      # configure the path scope on the server, not here.
```

If no `filesystem` server is configured, `mcp` op execution returns a
clean `status: error` with a helpful message; if the user denies the
permission prompt, the OS raises `PermissionError`. Both surfaces are the
intended failure mode — do not paper over them.

The server is expected to expose at least the canonical filesystem MCP
tool `read_text_file({path})`. Other tools (`list_directory`,
`search_files`, `read_image_file`) are not used by this skill but can be
added by a downstream copy of the skill that needs them.

## Input

`user_message.text` — the user's prompt, exactly as typed. The router
paraphrases naturally-phrased asks ("show me the README") into this
artifact.

## Output

`file_content_response`:
- `response`: natural-language answer that uses the file contents
- `files_read`: list of paths the skill actually read (post-MCP)

## Caveats

- The server name is hard-coded to `filesystem` in the phase frontmatter;
  if the user has named their filesystem server differently, they must
  copy the skill into `reyn/local/` and update `permissions.mcp` and the
  `mcp` op `server` field. A future iteration could surface this through
  routing metadata or a small config artifact.
