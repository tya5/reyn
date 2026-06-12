---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_search]
---

# `skill_search`

Search a public skills registry for skills relevant to a natural-language capability request.

## Entry

`search`

## Final output

`skill_candidate_list` — list of skill candidates with `name`, `source_url` (raw URL to `SKILL.md`), and `description`. Empty list when no relevant skills are found or the registry is unreachable without a cache.

## How it composes

A single `search` phase. A `registry_fetch.py:fetch_registry_results` preprocessor (safe mode, 30 s timeout) queries the skills registry deterministically and injects results into the artifact before the LLM call. The LLM then filters and ranks candidates by relevance to the request — it does not re-fetch anything.

## Caveats

- Requires `http.get` permission for `api.github.com` and `raw.githubusercontent.com` (declared in `skill.md`).
- Registry override is not currently supported — `safe` mode disallows `os` module access, so `REYN_SKILL_REGISTRY_URL` cannot be read. The skill always uses `github.com/anthropics/skills`. To use a non-default registry, declare `mode: unsafe` or wait for the safe-mode env-var support to land.
- Results come from the preprocessor, not LLM knowledge; if the registry is unreachable and no cache exists, the candidate list is empty.

## Usage

```bash
reyn run skill_search "PDF summarization"
reyn run skill_search "GitHub integration"
reyn run skill_search "spreadsheet generation"
```

Pairs with `skill_importer` for the install step: the `source_url` in each candidate feeds directly into `skill_importer`.

## Source

[`src/reyn/stdlib/skills/skill_search/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/skill_search/skill.md)

## See also

- [Reference: skill_importer](skill_importer.md) — install a skill from a registry URL
- [Reference: direct_llm](direct_llm.md) — single-phase skill for general queries
