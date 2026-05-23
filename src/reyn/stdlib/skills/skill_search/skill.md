---
type: skill
name: skill_search
description: Search a public skills registry for skills relevant to a natural-language capability request
entry: search
final_output: skill_candidate_list
final_output_description: |
  List of skill candidates from a public skills registry (default:
  github.com/anthropics/skills) that match the requested capability.
  Empty list if no relevant skills are found.
finish_criteria:
  - The skills registry has been queried (via preprocessor)
  - Candidates have been filtered by relevance to the user's request
  - Result list is returned (may be empty)
graph:
  search: []
permissions:
  python:
    # FP-0042 Phase 3 drift-fix (2026-05-23): migrated from mode: unsafe
    # to mode: safe via reyn.safe.http (= urllib-backed; no per-call
    # permission gate, see Issue #571 for the deferred gate-design
    # discussion). The skill_search → GitHub Contents API + raw URL
    # fetches stay structurally identical, only the import path moved
    # into the safe-mode-callable namespace.
    - module: ./registry_fetch.py
      function: fetch_registry_results
      mode: safe
      timeout: 30
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
routing:
  intents: [task]
  when_to_use:
    - User wants to find / discover skills for some capability
    - User asks for skill recommendations matching a task or domain
    - User mentions Anthropic skills, agent skills, or wants to import one
  when_not_to_use:
    - User asks conceptually what a skill is (stable_knowledge)
    - User wants to use / invoke a *specific known* skill (route directly)
    - User wants to build a new skill from scratch (use skill_builder)
    - User wants to import a skill from a known URL (use skill_importer directly)
  examples:
    positive:
      - "PDF を要約できる skill を探して"
      - "GitHub の skill が欲しい"
      - "Find skills for translating documents"
      - "code review できる skill ある？"
    negative:
      - "skill って何？"
      - "新しい skill を作って"
      - "code_review skill を実行して"
---

## Overview

Queries a public skills registry (by default ``github.com/anthropics/skills``)
and returns skills relevant to the requested capability. Results come from
a deterministic preprocessor; the LLM only filters and presents them. The
caller (= chat router) is responsible for selecting one when multiple
candidates are returned.

Mirror of ``mcp_search`` for the MCP registry — same shape, different
backing registry. Pairs with ``skill_importer`` for the install step
(= candidate `source_url` feeds directly into the importer).

## Input

Natural language description of the capability needed:

```
reyn run skill_search "PDF を要約"
reyn run skill_search "spreadsheet generation"
reyn run skill_search "code review"
```

## Output

``skill_candidate_list`` with a ``candidates`` array. Each entry has
``name``, ``source_url`` (= directly fetchable raw URL to ``SKILL.md``),
and ``description``. Returns an empty list if no relevant skills are
found or the registry is unreachable and no cache exists.

## Registry override

The default registry is ``github.com/anthropics/skills`` (the canonical
Anthropic-published skills repo). Override at runtime via
``REYN_SKILL_REGISTRY_URL`` — must be a GitHub Contents API URL
pointing at a directory of skill folders, each containing ``SKILL.md``
with YAML frontmatter (``name`` + ``description``).
