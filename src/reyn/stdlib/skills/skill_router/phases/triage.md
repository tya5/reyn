---
type: phase
name: triage
input: chat_routing_request
role: chat_router
can_finish: false
allowed_ops: []
max_act_turns: 1
permissions:
  python:
    - module: ./preprocessor_steps.py
      function: slice_chat_history
      mode: pure
      timeout: 5
  file.read:
    - path: .reyn/agents
      scope: recursive
    - path: .reyn/chats
      scope: recursive
preprocessor:
  - type: run_op
    op:
      kind: file
      op: read
      path: ""
    args_from:
      path: data.history_path
    into: data.history_raw
    on_error: empty
  - type: python
    module: ./preprocessor_steps.py
    function: slice_chat_history
    into: data.history
    output_schema:
      type: array
      items:
        type: object
        properties:
          role: {type: string}
          text: {type: string}
        required: [role, text]
---

Pick exactly one bucket intent for the user's utterance and transition.
This phase makes a SINGLE decision; it never composes a user-facing
reply (the `reply` phase does that for chitchat / direct_reply, and
`match` does that for task / fresh_lookup).

## Inputs

- `user_message`: the latest utterance
- `history`: recent prior turns (oldest first, preprocessor-injected)
- `available_skills`: catalogue of skills; each entry is
  `{name, description, routing?}` with optional routing metadata.
- `available_agents`: peer agents (`{name, role}`) reachable via the
  topology. Empty list means single-agent mode.
- `memory_index`: merged shared + agent memory index — pass-through
  context for the reply phase. Triage does not read it for routing
  decisions, but forwards it via `routing_intent.memory_index`.

## Bucket intents (closed vocabulary)

Pick the **first** matching bucket using these rules. Stop at the first
match — order encodes specificity.

### B1. `chitchat` — greeting / thanks / ack / casual social ping

Hard cues: greetings (`hi` / `hello` / `こんにちは` / `こんちわ` /
`おはよう` / `안녕` / `你好`), thanks (`ありがとう` / `thanks` / `ty`),
acks (`OK` / `了解` / `はい` / `わかった` / `got it`), single-token
social (`yo` / `sup` / `もしもし`), punctuation-only / single emoji.

When the message is one of those, the answer is **chitchat**, regardless
of how plausibly some skill in the catalogue could be rationalised as a
match. Skills like `article_writer` / `summarizer` are NOT relevant for
"hello" — never let them shadow chitchat.

### B2. `task` — a skill or peer agent could fulfil the request

Pick `task` when there's a clear mapping from the utterance to either:

- A skill in `available_skills` whose `routing.when_to_use` /
  `routing.examples.positive` (or `description` if no `routing` block)
  matches the user's request.
- A peer in `available_agents` whose `role` matches better than any
  skill — `match` will prefer agent delegation in that case.

Examples:
- "ブログ記事を書いて" + an article-writer skill → task
- "このテキストを要約して" + a summarizer skill → task
- "MCP server を探して" + `mcp_search` → task
- "リポジトリのこのファイルを読んで" + `read_local_files` → task

**Negative signals — do NOT pick task on these:**
- The message matched B1 (greeting / thanks / ack). Always B1 wins.
- The user is asking the agent a question about itself or its
  capabilities ("what can you do?", "君は何ができる？"). That's
  direct_reply (B4), not task.
- The message is shorter than ~3 substantive characters AND has no
  imperative verb. Real tasks have a verb.
- Multiple skills look plausible but none is clearly best — still pick
  task; the `match` phase will ask a clarifying question.

### B3. `fresh_lookup` — needs current / time-sensitive web data

Trigger when EITHER:
- The user explicitly asks for current / latest / today's data
  ("今日の…", "最近の…", "最新の…", "current X", "latest X")
- The subject is post-cutoff and information is likely stale
  (recent product releases, current events, market data, fast-moving
  library version numbers)
- The user pasted a URL they want fetched
- The user explicitly asks for an official documentation pointer

Do NOT trigger on form alone. "X とは？" / "X について教えて" is NOT a
freshness signal — most concept questions are stable knowledge (B4).
Trigger only when freshness is genuinely required.

### B4. `direct_reply` — answer the user directly

Catch-all for everything else: memory recall (the user asks about
themselves / the project, and `memory_index` has the answer), stable
knowledge (training-confident answers about established concepts /
libraries / math / code), clarification questions (genuinely ambiguous
input), and meta questions about the agent itself.

The `reply` phase composes the actual `reply_text` from these — triage
just routes there.

## Output

Emit a `routing_intent` artifact with:

```json
{
  "intent": "chitchat" | "task" | "fresh_lookup" | "direct_reply",
  "confidence": 0.0-1.0,
  "rationale": "<one short sentence — name the matching skill for task, the freshness signal for fresh_lookup, or the social cue for chitchat>",
  "user_message": "<verbatim pass-through>",
  "history": [...pass-through...],
  "available_skills": [...pass-through...],
  "memory_index": {...pass-through...}
}
```

Then transition. The OS routes by intent:

| Intent          | Next phase     |
|-----------------|----------------|
| `chitchat`      | `reply`        |
| `task`          | `match`        |
| `fresh_lookup`  | `match`        |
| `direct_reply`  | `reply`        |

## Output language

The `rationale` field should be in the user's language for audit
clarity, but it never reaches the user.
