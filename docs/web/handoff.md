# Claude Code Handoff — Reyn Web UI

> Paste the **Kickoff prompt** below into a fresh Claude Code session running at the project root (`~/Workspace/junk/claude_sandbox/sandbox_2`). It contains everything the session needs.

---

## Context (read this first, then drop the kickoff prompt below)

A separate Cowork session has been doing prep work for adding a Web UI to Reyn. State of the world:

- **Design brief**: drafted and being iterated on in `claude.ai/design`. The full brief lives at
  `~/Library/Application Support/Claude/local-agent-mode-sessions/.../outputs/reyn_design_brief.md`.
  It will move to the project root as `docs/web/design_brief.md` when this session starts.
  Key framing: **two faces — App (OpenClaw-style end-user) and Studio (developer surface)**.
- **Claude Design output**: not yet exported. When it arrives it'll come as either (a) a
  "Send to Claude Code" payload or (b) a standalone HTML/React zip. Either way the destination
  will be the `web/` directory.
- **Branch**: work happens on `feat/web-gateway`, in a worktree under `.claude/worktrees/web-gateway/`.
  Commit style: small, incremental commits.
- **Engine**: do not modify `src/reyn/` modules outside `src/reyn/web/`. The gateway must wrap
  the existing `AgentRegistry`, `ChatSession`, `EventStore`, `Workspace`, `PermissionResolver`,
  `BudgetTracker`, etc. without changing them.
- **Tier-1 rules** in `CLAUDE.md` (P1–P8) still apply. The web layer is OS-adjacent infrastructure
  — it must not contain skill-specific strings (P7).

---

## Kickoff prompt (paste this into Claude Code)

````
You are picking up Reyn web-UI scaffolding from a Cowork session.

## Setup

1. Move `CLAUDE_CODE_HANDOFF.md` → `docs/web/handoff.md`.
2. Move `~/Library/Application Support/Claude/local-agent-mode-sessions/.../outputs/reyn_design_brief.md`
   → `docs/web/design_brief.md`. (Find the exact path by reading the most recent
   `outputs/reyn_design_brief.md` — it's the latest revision.)
3. `git worktree add .claude/worktrees/web-gateway -b feat/web-gateway` and `cd` into it.
4. Read `docs/web/design_brief.md` end-to-end before writing any code. The §0 framing
   (App vs Studio) determines the API surface.

## Goal of this session

Stand up a thin FastAPI + WebSocket gateway in `src/reyn/web/` that wraps the existing
engine. **No engine changes.** Frontend comes later (after Claude Design output arrives).

## Plan (small commits, one bullet per commit)

- [ ] `src/reyn/web/__init__.py` empty package
- [ ] `src/reyn/web/server.py` — FastAPI app, CORS for localhost dev, mount routers
- [ ] `src/reyn/web/deps.py` — shared dependencies: project_root, AgentRegistry singleton,
       PermissionResolver, BudgetTracker (mirror what `cli/commands/chat.py` constructs)
- [ ] `src/reyn/web/routers/agents.py` — REST: GET /api/agents, POST /api/agents,
       GET /api/agents/{name}, DELETE /api/agents/{name}. Wraps AgentProfile + AgentRegistry.
- [ ] `src/reyn/web/routers/skills.py` — REST: GET /api/skills (project/local/stdlib),
       GET /api/skills/{name} (returns skill.md, phases, artifacts, parsed graph)
- [ ] `src/reyn/web/routers/runs.py` — REST: GET /api/runs (list .reyn/runs/*.jsonl),
       GET /api/runs/{run_id}, GET /api/runs/{run_id}/events (SSE stream of events)
- [ ] `src/reyn/web/routers/topologies.py` — REST: list / show / new / rm. Wraps
       reyn.chat.topology.
- [ ] `src/reyn/web/routers/permissions.py` — REST: GET / DELETE /api/permissions.
       Wraps `.reyn/approvals.yaml`.
- [ ] `src/reyn/web/routers/budget.py` — REST: GET /api/budget/usage, PATCH /api/budget/caps.
       Wraps BudgetTracker ledger.
- [ ] `src/reyn/web/ws/chat.py` — WebSocket /ws/chat/{agent_name}. On connect: attach to
       AgentRegistry, drain ChatSession outbox into the WS as JSON messages. On client
       message: forward to `session.submit_user_text`. Handle `ask_user` interventions
       as a typed WS message + return-channel for the user's answer. Renderer-shaped:
       reuse the kind taxonomy from `src/reyn/chat/renderer.py` (agent / status / error /
       intervention / trace / skill_done) so the frontend can switch on `msg.kind`.
- [ ] `src/reyn/cli/commands/web.py` — `reyn web --port 8080 [--host 127.0.0.1] [--reload]`
       command. Register in `cli/commands/__init__.py` ALL list. Launches uvicorn.
- [ ] `pyproject.toml` — add `[project.optional-dependencies] web = ["fastapi>=0.110",
       "uvicorn[standard]>=0.27", "websockets>=12"]`. Document `pip install -e ".[web]"`
       in README.
- [ ] Smoke tests in `tests/web/test_smoke.py`: import the app, `reyn web --help`,
       hit `/api/agents` against a tmp project root.

## Hard constraints

- **P7**: no skill-specific strings in `src/reyn/web/`. Treat artifact types, phase names,
  decision values as opaque from the gateway's POV. Pass them through as JSON.
- **No engine changes**. Only `src/reyn/web/`, `src/reyn/cli/commands/web.py`, the
  `pyproject.toml` extra, `tests/web/`, and docs.
- **Output language**: error messages and logs in English (engine-style); user-visible
  strings (none in the gateway, but in case of CLI help) follow `output_language` from
  reyn.yaml (default ja).
- **Paths**: project_root is discovered via `reyn.config._find_project_root(Path.cwd())`
  exactly like `chat.py` does. Don't hard-code paths.

## When you're done

Don't merge to main. Push the branch (or just leave the commits on `feat/web-gateway`).
Report back to the Cowork session with: (a) the API surface as it actually shipped,
(b) any places where the brief was wrong or under-specified, (c) anything blocked by
the engine that needs an engine PR before the frontend can land.

Cowork will then take Claude Design's output and compose the next handoff (frontend
scaffold + page wiring) for you.
````

---

## How the Cowork ↔ Claude Code loop will work

1. **Cowork (this side)** keeps iterating with Claude Design and revises `docs/web/design_brief.md` as the visual direction firms up. Reviews PRs from the Claude Code session against the brief.
2. **Claude Code** does all code work on `feat/web-gateway` — gateway now, frontend later. Commits incrementally.
3. **You** play conductor: paste design brief revisions into Claude Design; paste handoff updates into Claude Code; merge when both faces are happy.
4. When ready to ship: open a PR from `feat/web-gateway` → `main`, run the full eval suite, merge.
