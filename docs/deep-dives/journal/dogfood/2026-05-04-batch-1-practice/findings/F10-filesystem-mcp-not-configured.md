# F10 [HIGH]: filesystem MCP は箱の中で寝ている

> 一行で: `read_local_files` は filesystem MCP を要求するが、 そんな MCP
> server はどこにも設定されていない。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `e59cead` (出来るのは案内のみ — 詳細は下記) |
| Scenario | scenario 3 (read_local_files permission gating) |
| Found | 2026-05-04 |

---

## 観測

scenario 3 のもう一つの finding。 `read_local_files` skill は
`permissions: mcp: [filesystem]` を skill.md frontmatter で declare している。
しかし `reyn.yaml` / `reyn.local.yaml` どちらにも `mcp.servers.filesystem`
の登録がない。

つまり:

- skill 自体は MCP filesystem を使う前提で書かれている
- proj config には filesystem MCP server が登録されていない
- もし skill_router が起動していても、 MCP op 実行時に「server filesystem
  not configured」 エラーで落ちる

scenario 3 が観測したかった「permission prompt UX」 は、 仮に F9 が
解決しても **これ以前の段階で fail** する。

## 影響

- out-of-box experience: `reyn` を入れて `read_local_files` を試そうとしても
  動かない
- 「使い物にならない」 体験の典型例 — どこから手をつければいいか user
  に分からない

## 修正 (commit `e59cead`)

「out-of-box で動く」 ところまで全自動化はしない (= MCP server インストールは
operator の判断、 自動 install は range out)。 **discoverability 強化** で
解決:

- `REYN_YAML_TEMPLATE` (= `reyn init` が生成する yaml) に **commented MCP
  block** を追加。 `# mcp.servers.filesystem: { type: stdio, ... }` のような
  skeleton で、 user が cmt 外せば動く形
- `reyn init` のターミナル出力に **「MCP servers (optional)」 section** を
  追加。 `examples/configs/with-mcp.yaml` への pointer 付き
- `README.md` + `docs/en/reference/stdlib/read_local_files.md` 冒頭に MCP
  setup へのリンク

つまり「動かない」 の root cause である「設定漏れ」 を user が即座に発見
できるようにする方向で対処。 自動化は OSS Lv.1 (Wave C) で再評価。

## 後続

- batch 2 では `examples/configs/with-mcp.yaml` をベースに MCP 経路の
  e2e flow を最初から組んでテスト
- installer が optional に「filesystem MCP server を install する?」 と
  聞く UX は将来の operator-friendly 改善 wave で
