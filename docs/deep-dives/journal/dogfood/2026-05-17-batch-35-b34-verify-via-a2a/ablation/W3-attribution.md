# B35-W3 attribution ablation

## Per-condition routing choice (N=5 each)

| Condition | first-turn tool | count |
|---|---|---|
| A post-B34 | file__glob | 3/5 |
| A post-B34 | file__list | 0/5 |
| A post-B34 | file__grep | 0/5 |
| A post-B34 | other (no-tool-call) | 2/5 |
| B pre-B34  | file__list | 0/5 |
| B pre-B34  | file__glob | 0/5 |
| B pre-B34  | file__grep (invoke_action, UnknownActionError) | 1/5 |
| B pre-B34  | list_actions (catalog discovery) | 1/5 |
| B pre-B34  | other (no-tool-call) | 3/5 |

## Sub-condition A2 (= explicit "pattern" prompt)

- arg name chosen: pattern=3/5, dir=2/5 (wrong — not in schema), path=1/5 (partial)
- count: 5/5 shots completed
- Tool chosen: file__glob = 5/5 (100% consistency with explicit prompt)

Note: `content_regex` (the original hypothesis for the arg-name gap) was NOT observed as a standalone arg. Shot A1 shot 1 sent both `content_regex` and `pattern` simultaneously — handler used `pattern` (correct) and ignored `content_regex`. The actual arg-name gap is `dir` (used 2/5 in A2) — not in `GLOB_FILES` schema.

## Per-shot detail — Condition A (post-B34, 99d8407)

| shot | tool | args_preview | status |
|---|---|---|---|
| 1 | file__glob | `{'content_regex': 'judge_output', 'pattern': 'src/**/*.md'}` | ok |
| 2 | file__glob | `{'keyword': 'judge_output', 'include_content': True, 'pattern': 'src/skill.md'}` | ok |
| 3 | file__glob | `{'pattern': 'src/**/skill.md'}` (then 2x file__grep) | ok |
| 4 | no-tool-call | `{}` | ok |
| 5 | no-tool-call | `{}` | ok |

## Per-shot detail — Condition B (pre-B34, HEAD~1 = 510dd93)

| shot | tool | args_preview | status |
|---|---|---|---|
| 1 | file__grep (invoke_action, failed) | `{'path': 'src/**/skill.md', 'content': 'judge_output'}` | ok (reply: "file__grep not available") |
| 2 | list_actions | `{'category': ['file']}` | ok (reply: "file__grep not available") |
| 3 | no-tool-call | `{}` | ok (reply: "sorry, file__grep not available") |
| 4 | no-tool-call | `{}` | ok (reply: "sorry, file__grep not available") |
| 5 | no-tool-call | `{}` | ok (reply: "sorry, file__grep not available") |

## Per-shot detail — Sub-condition A2 (explicit "pattern" prompt, post-B34)

| shot | tool | arg_name | status |
|---|---|---|---|
| 1 | file__glob | dir (WRONG — not in schema) | ok |
| 2 | file__glob | dir (WRONG — not in schema) | ok |
| 3 | file__glob | path (partial — no pattern arg) | ok |
| 4 | file__glob | pattern (CORRECT) | ok |
| 5 | file__glob | pattern (CORRECT) | ok |

## Attribution

- B34 file__grep routing effect: **attributable**
- Arg-name fix path: **synonym normalization** (`dir` → `path` in `_handle_glob`, same pattern as B34's `text` → `content` fix in `_handle_write`)
- Confidence: **HIGH** with N=5A + N=5B cited

### Reasoning

**Why ATTRIBUTABLE (HIGH):**
- Post-B34 (A): 3/5 shots chose `file__glob` as first-turn action. 0/5 chose `file__list`.
- Pre-B34 (B): 0/5 shots chose `file__glob`. 0/5 chose `file__list`. Instead: 1/5 tried `file__grep` via invoke_action (returned UnknownActionError — not in _OPERATION_RULES pre-B34), then LLM reported "not available" and gave up.
- Clear separation: file__glob appears only when it is in the hot list seed (condition A). When not seeded (condition B), the LLM cannot find it and fails.

**B33 baseline re-interpretation:**
B33's `file__list` observation (with `{filter:...}` → KeyError) was NOT the LLM's true first preference. The LLM prefers `file__grep` (confirmed by B shot 1). B33's `file__list` call was likely a later-turn fallback after file__grep failed, visible only in a multi-turn session trace. The routing shift (B33→B35) from `file__list` to `file__glob` is directly caused by B34 adding both tools to the seed.

**Arg-name fix path (synonym normalization):**
- The `content_regex` gap hypothesis was not the primary issue. LLM sent `content_regex` alongside `pattern` (shot A1-1); handler correctly used `pattern`.
- The real gap: LLM uses `dir` (2/5 in A2) as the directory param name, but schema uses `path`. Add `dir` as accepted synonym in `_handle_glob`:
  ```python
  if "path" not in args and "dir" in args:
      args = {**args, "path": args["dir"]}
  ```
- A2 explicit prompt achieved 5/5 file__glob routing with 3/5 correct `pattern` arg usage — confirming description disambiguation (explicit naming in prompt) boosts correct arg selection but is not required for routing.
