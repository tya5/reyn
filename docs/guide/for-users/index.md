---
type: landing
topic: using-reyn
audience: [human]
---

# For users

Everything you need to use Reyn day-to-day — no skill authoring, no server management.

If you haven't installed Reyn yet, start with [Getting started: Installation](../getting-started/01-installation.md).

---

## The one command you need

```bash
reyn chat
```

This opens a TUI session. Type a request, get a response. That's it.
Reyn routes your request to the right built-in skill automatically — you don't choose which one.

---

## What you can do in chat

| Task | Example input |
|---|---|
| Ask questions | `"What's the capital of France?"` |
| Summarize files | `"Summarize README.md"` |
| Work on local files | `"What functions are in src/reyn/skill_runtime.py?"` |
| Search the web | `"What's the latest release of Python?"` |
| Run multi-step tasks | `"Research X and write a report"` |

No configuration required for any of these. Reyn has the skills for them out of the box.

---

## How-tos

### Interface

- **[Chat and Web UI](chat-and-web-ui.md)** — start the web interface, use it alongside the TUI.

### Files and tools

- **[Work with local files](work-with-files.md)** — reference files and directories in your requests.
- **[Use an MCP server](../for-skill-authors/operations/use-an-mcp-server.md)** — add GitHub, Slack, a database, or any MCP-compatible tool.
- **[Enable semantic search](enable-semantic-search.md)** — index your own docs so Reyn can search them by meaning.

### Control and safety

- **[Manage permissions](manage-permissions.md)** — approve or deny what Reyn is allowed to do.
- **[Respond mid-task](ask-user-mid-phase.md)** — answer questions Reyn asks while a skill is running.
- **[Rewind a session](time-travel.md)** — jump back to an earlier point with `/rewind` and branch from there.
- **[Cap your spending](cap-spending.md)** — set token / dollar limits so a run can't overspend.
- **[Run a skill on a schedule](schedule-skills.md)** — fire a skill on a cron schedule with `reyn cron`.

---

## Things Reyn handles for you

**Memory** — Reyn remembers facts across sessions automatically. No setup needed.

**Crash recovery** — if a long task is interrupted, re-run `reyn chat` and it resumes from where it left off.

**Cleanup** — when you close the TUI, the session ends cleanly. No background processes left behind.

---

## Where to go from here

Once you're comfortable with chat mode:

- **[Getting started: Your first skill](../getting-started/03-your-first-skill.md)** — build a custom automation tailored to your workflow.
- **[Reference: CLI / chat](../../reference/cli/chat.md)** — full list of slash commands and flags.
