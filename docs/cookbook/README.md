# Reyn cookbook

End-to-end workflows. Each subdirectory is one self-contained recipe: a
`README.md` walking through the commands, any profile / topology / skill
files the recipe needs, and a `transcript.example.txt` showing roughly what
a successful run prints.

## Format

Every example is driven by **`reyn run <skill> "<input>"`** and/or
**`reyn chat <agent>`** — no new tooling. Recipes that need an agent or a
topology call the existing `reyn agent new` / `reyn topology new` commands.

## Status legend

- ✅ **Works today** — every dependency exists in this repo; runnable as-is.
- ℹ️ **Works today (custom skill)** — runnable, but uses a skill bundled in
  the example dir rather than a stdlib skill. Copy it into `reyn/local/`
  before running.
- 🔮 **Roadmap** — depends on a capability that is not yet implemented (a
  missing stdlib skill, MCP server config, scheduling, etc.). Documents the
  intended shape; not runnable as-is.

## Index

| Recipe | Status | What it shows |
|--------|--------|---------------|
| [eval-a-skill](eval-a-skill/README.md) | ✅ Works today | Score a skill against a test case with `eval` |
| [multi-agent-research](multi-agent-research/README.md) | ✅ Works today | 3-agent team via `reyn agent new` + `reyn topology new --kind team` |
| [improve-a-skill](improve-a-skill/README.md) | ℹ️ Works today (custom skill) | Iteratively raise eval score with `skill_improver`; uses a deliberately weak bundled `sample_skill` |
| [translate-doc](translate-doc/README.md) | ℹ️ Works today (custom skill) | en → ja document translation; uses a bundled `translate_doc` skill (no stdlib equivalent) |
| [research-topic](research-topic/README.md) | 🔮 Roadmap | Generic web research; `mcp_search` stdlib only finds MCP **servers** in the GitHub registry — generic web search needs an external search MCP server you've configured |
| [write-readme](write-readme/README.md) | 🔮 Roadmap | Generate a Reyn-style README; `skill_builder` builds **new skills**, not arbitrary docs — needs a dedicated doc-writer skill |
| [weekly-summary](weekly-summary/README.md) | 🔮 Roadmap | Cron-style recurring summarizer; needs scheduling / persistent state |

## Prerequisites (all recipes)

- `reyn` CLI on PATH (`pip install -e .` from repo root, or your install method).
- `OPENAI_API_KEY` exported in shell. (Local LiteLLM at `localhost:4000`
  works too — set `OPENAI_BASE_URL` if you use it.)
- A working directory with a `reyn.yaml` (or run from this repo root, which
  has one).

## Conventions

- Inputs shown as `"..."` are natural language; Reyn auto-wraps them as a
  `user_message` artifact.
- Inputs shown as `'{"type": ...}'` are explicit JSON artifacts.
- Transcripts are sketches — exact wording from the LLM will vary run to run.
