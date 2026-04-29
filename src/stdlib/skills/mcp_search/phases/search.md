---
type: phase
name: search
input: user_message
role: mcp_researcher
can_finish: true
---

Search the GitHub MCP Registry for servers relevant to the user's capability request.
You MUST complete this entire phase with exactly ONE web_fetch call — no more.

## Step 1 — Extract search keyword

From the user's request, extract the most concise English keyword(s) that describe the capability
needed (1–3 words). Examples:
- "GitHub リポジトリの操作" → `github`
- "web 検索がしたい" → `search`
- "PostgreSQL データベースに接続したい" → `database`
- "Slack にメッセージを送りたい" → `slack`

## Step 2 — ONE web_fetch (no more)

Perform exactly one web_fetch:

```
web_fetch: https://github.com/mcp?search={keyword}
prompt: Find all <a> tags whose href attribute starts with "/mcp/". For each, return the exact
        href string as it appears in the HTML (e.g. "/mcp/github/github-mcp-server").
        Do NOT modify, abbreviate, or infer the owner or repo name — copy it verbatim.
        Exclude hrefs where the first path segment after "/mcp/" is any of:
        mcp-clients, servers, server, blob, tree, graphs, issues, pulls, actions,
        wiki, security, dist, bridges, main, ga, docs, assets.
        Return a plain newline-separated list of exact href strings only.
```

After this fetch, go directly to Step 3. Do not call web_fetch again for any reason.

## Step 3 — Return result immediately

Using only the href paths from Step 2, finish with `mcp_candidate_list`.
Do NOT call web_fetch again. Do NOT fetch individual repository pages for descriptions.

For each href `/mcp/{owner}/{repo}` (use the exact owner and repo strings — never guess):
- `name`: `{owner}/{repo}`
- `repo_url`: `https://github.com/{owner}/{repo}`
- `description`: a one-line description inferred from the repo name alone

If Step 2 returned no valid paths, set `candidates: []`.
