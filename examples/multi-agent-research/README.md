# multi-agent-research

A 3-agent team — `lead` triages, `researcher` digs, `writer` produces prose.
Uses the `team` topology kind so workers route through the lead.

## What this shows

- `reyn agent new` to provision named agents with role prompts.
- `reyn topology new --kind team` to constrain who can talk to whom.
- The expected delegation chain when the lead receives a request that
  needs both research and drafting.

## Topology

```
       lead (leader)
       /         \
researcher       writer
```

`team` kind permits leader↔member edges only. `researcher` and `writer`
cannot talk directly — they go through `lead`.

## Setup

```bash
reyn agent new lead       --role "team lead. Triage requests; delegate research to researcher and drafting to writer; synthesize a final answer."
reyn agent new researcher --role "deep technical research. Cite primary sources. No prose polish."
reyn agent new writer     --role "concise prose. Strict word budgets. No headings unless asked."

reyn topology new launch --kind team \
    --leader lead \
    --members lead,researcher,writer
```

Inspect:

```bash
reyn topology show launch
```

Expected (4 permitted edges, all leader↔member):

```
permitted edges (4):
  lead → researcher
  lead → writer
  researcher → lead
  writer → lead
```

## Run

```bash
reyn chat lead
> Investigate DuckDB v1's breaking changes and produce a 200-word summary.
```

## Expected delegation chain

1. `lead` receives the request, classifies it as research+writing.
2. `lead` emits `messages_to_agents` → `researcher` (gather facts).
3. `researcher` returns findings to `lead`.
4. `lead` emits `messages_to_agents` → `writer` (draft to 200 words, here are facts).
5. `writer` returns prose to `lead`.
6. `lead` synthesizes and replies to the user.

User sees an interim "(working on it)" then the final 200-word summary.

## Files in this example

- `setup.sh` — the three commands above as a runnable script.
- `topology.expected.txt` — sample `reyn topology show launch` output.
- `transcript.example.txt` — sketch of the chat session.

## See also

- [How-to: build an agent team](../../docs/en/how-to/build-an-agent-team.md)
- [How-to: multi-hop delegation](../../docs/en/how-to/multi-hop-delegation.md)
- [Concepts: topology](../../docs/en/concepts/topology.md)
