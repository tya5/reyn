# B2-H3 [HIGH]: MCP permission grant が with-mcp.yaml に無い

> 一行で: `with-mcp.yaml` に `mcp.filesystem: allow` がなく、非インタラクティブ環境では
> `read_local_files` の全 MCP 呼び出しが `permission_denied` になる。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `a75587d` |
| Scenario | S2 (Agent B — `read_local_files` headless run) |
| Found | 2026-05-04 |

---

## 観測 (Agent B raw report)

```
mcp.filesystem: allow がデフォルトで未設定のため、非インタラクティブ（TTY なし）で必ず
permission_denied になる。TTY 環境では prompt が出るが、スキルの skill.md 冒頭に
`permissions: mcp: [filesystem]` を宣言しているにもかかわらず、プロジェクト config に
`mcp.filesystem: allow` が存在しないと実行不可。
```

## 影響

- headless / CI / piped-stdin 環境 (= batch dogfood の典型的な実行形態) では
  skill が空で終了する
- TTY 環境でも、 user が config を見ずに `with-mcp.yaml` をそのままコピーすると
  起動毎に interactive prompt が出て驚く
- F10 (e59cead) が `with-mcp.yaml` への pointer を張ったにもかかわらず、
  そのファイル自体に permission grant が欠けていたため F10 の修正が不完全だった

## 原因

`examples/configs/with-mcp.yaml` に `mcp.servers.filesystem` の設定は存在したが、
対応する `permissions: mcp.filesystem: allow` が無かった。

`PermissionResolver._is_config_approved` は `mcp.filesystem` キーを config dict から
引くため、 config になければ `_interactive=False` 時に即 `False` を返し
`permission_denied` になる (permissions.py L280)。

## 修正

1. **`examples/configs/with-mcp.yaml`** — `permissions:` ブロックに
   `mcp.filesystem: allow` / `mcp.git: allow` / `mcp.fetch: allow` を追加。
   headless モードでの必要性と、 interactive prompt に戻す方法をコメントで説明。
2. **`docs/en/reference/stdlib/read_local_files.md`** — "Required setup" 冒頭に
   "Setup checklist" を追加。 MCP サーバー設定・permission pre-approval・
   `with-mcp.yaml` への pointer を 3 点で明示。 YAML snippet にも permission
   行を追加。
3. **`README.md`** — MCP skills の one-liner 案内に headless 環境での
   `mcp.filesystem: allow` 必須を一文追記。

## 後続

- batch 3 で out-of-box experience を再 dogfood し、 `with-mcp.yaml` コピーだけで
  headless 実行が通ることを確認する
