# `reyn mcp` CLI shape — design positioning

**Status**: Design positioning (2026-05-09 update 1) — `reyn mcp {search,install,list,remove,set-secret,clear-secret}` CLI の design choice。 dogfood Step 0 (= 8 並列 sonnet research) の ground truth 反映済 + ADR-0030 universal secret infra との整合済。 ADR ではなく positioning doc として記録 (= CLI shape は将来 iterate 前提、 ただし storage / loading semantics は ADR-0030 で固定)。
**Track**: MCP UX / OSS launch friction reduction
**Related**: ADR-0029 (`mcp_install` permission)、 ADR-0030 (universal secret handling — `reyn mcp set-secret` の storage layer を提供)、 `docs/concepts/mcp.md` Quick start section

> 元 plan の researcher entry (= `reyn mcp` CLI proposal) を base に、 dogfood findings + industry research で confidence を上げた最終形。 着手 ready の design として残す。

---

## 1. Context

### 解決したい問題

**現状 friction (= dogfood Step 0 で測定した ground truth)**:

1. **Discovery → install の bridge ゼロ**: `mcp_search` skill が候補を JSON で返すが、 `repo_url` を `command` / `args` に変換するのは user 手動 (= 「`https://github.com/mcp/slack` → `npx -y @modelcontextprotocol/server-slack`」 の意味的 gap)。
2. **`reyn config set` が `mcp` キー未対応**: yaml 直書きが必須、 ネスト深く (`mcp.servers.<name>.{type,command,args,env}`) typo risk 高い。
3. **`reyn mcp serve` は inbound のみ**: outbound 管理 (= server 追加・削除・一覧) の専用 CLI が存在しない。
4. **install-time gating ゼロ**: `reyn.yaml` 直編集を gate する layer なし、 enterprise compliance 訴求の主要 lever が不在。

### 既存資産 (= reuse できる infra)

dogfood で確認できた、 wholesale 再利用可能な実装:

- **`${VAR}` env interpolation 部分実装済** (`mcp_client.py::expand_env()`): `mcp.servers.<name>` の全 string field を再帰展開、 op dispatch 時に解決。 ADR-0030 で **全 yaml field に generic 化** + **startup load** に拡張予定
- **gitignore default**: `reyn.local.yaml` / `~/.reyn/config.yaml` / `.env*` がすべて gitignore 済 (= secret は `${VAR}` で参照、 値は env or `~/.reyn/secrets.env` (= ADR-0030) に置く運用が成立)
- **permission system**: `PermissionDecl` / `PermissionResolver._is_config_approved()` / `require_*` pattern が `mcp_install` 追加に整合的に拡張可能 (= ADR-0029 で詳細)
- **MCPClient lifecycle + transport abstraction**: stdio / http の `_open_transport()` 切替が SDK 公式ラッパーで整備済

→ **Reyn は既に MCP credentials 周りの core machinery を持っている**、 ADR-0030 で全 Reyn-wide に generic 化される (= MCP wave に bundle 実装)。 CLI 化の追加 cost は当初想定より低い。

---

## 2. Direction

### subcommand 構成 (= phase 1)

```
reyn mcp search "<query>"            # 候補 discovery (= mcp_search skill wrapper)
reyn mcp install <server_id>         # registry fetch + reyn.yaml 追記 (= 新 mcp_install skill + IR op)
reyn mcp list                        # 設定済み server 一覧 + status
reyn mcp remove <name>               # reyn.yaml から削除
reyn mcp set-secret <server> <KEY>[=<VALUE>]    # secret 設定・rotate (value 省略で prompt)
reyn mcp clear-secret <server> [<KEY>]          # secret 削除 (KEY 省略で全削除)
```

`reyn mcp serve` (= 既存 inbound mode) はそのまま残す。 namespace 共存。 phase 2 candidate: `reyn mcp env set/unset` (= 非 secret env、 demand surface してから追加)。

### 各 subcommand の設計

#### `reyn mcp search "<query>"`

- 既存 `mcp_search` stdlib skill の thin CLI wrapper
- output 改善: 現状 GitHub HTML scraping → **registry.modelcontextprotocol.io API 直叩き** + Reyn 側 cache (`~/.reyn/registry-cache/<server_id>.json`、 TTL 24h) に切替
- registry 未登録 server (= Anthropic 公式 reference servers) は GitHub URL fallback path を併設
- output: `name / description / runtimeHint / install command preview` を table 表示、 user は次 step で `reyn mcp install <name>` を打つ

#### `reyn mcp install <server_id>` ★ 主要 entry

新 `mcp_install` stdlib skill + 新 IR op `{kind: mcp_install, server_id, env_overrides?}`。 architectural split:

- **skill (LLM 担当)**: `discover` phase で registry fetch + 候補 disambiguation + credentials 必要性判断 → Control IR `{kind: mcp_install, server_id: "..."}` emit して finish
- **IR op (OS 担当)**: registry server.json fetch → `runtimeHint` チェック (= npx/uvx/docker/dnx の存在確認) → **`mcp_install` permission gate** (= ADR-0029) → credential 投入 flow (= 下記) → `reyn.yaml` の `mcp.servers.<name>` 追記 → `event: mcp_server_installed` emit

`runtimeHint` mapping (= registry schema で閉じる 4 entries、 将来も stable):
```
npx → Node.js   (https://nodejs.org)
uvx → uv        (https://github.com/astral-sh/uv)
docker → Docker (https://www.docker.com)
dnx → .NET      (https://dotnet.microsoft.com)
```

未インストール時は「`Node.js が必要です: https://nodejs.org`」 で停止 (= subprocess 起動失敗の cryptic error より明確な friction stop)。

#### `reyn mcp list`

設定済み server 一覧を table 表示。 取得は **cheap default** (= cached state、 process 起動なし):

```
NAME         TRANSPORT  STATUS         CREDENTIALS
filesystem   stdio      ready          (none)
github       stdio      ready          GITHUB_TOKEN ✓ (set)
slack        stdio      missing-cred   SLACK_BOT_TOKEN ✗ (not set)
```

`--probe` flag で初めて全 server に handshake を発行 (= API quota 消費 / audit log 副作用あり、 explicit opt-in)。

#### `reyn mcp remove <name>`

`reyn.yaml` から `mcp.servers.<name>` 削除のみ。 **runtime state policy**:

- 「`reyn chat` 実行中の subprocess は terminate しない、 次 session 起動時に config 反映」 を baseline
- 副作用が user 想定外にならないよう、 削除時に「現在 running の subprocess は次 reyn chat session まで継続」 と message 表示
- `--restart` flag で running session への notify を opt-in 化 (= 現在 chat 中の subprocess を recycle、 phase 2 で実装)

---

## 3. Credentials UX (= A+B hybrid + `${VAR}` interpolation reuse)

### Layered approach (= ADR-0030 universal secret infra に乗る)

storage / loading は **ADR-0030 で universal 化**。 ここでは MCP-specific UX layer のみ記述、 underlying machinery は universal:

#### Layer 1: `${VAR}` interpolation (= ADR-0030 で全 yaml に lift、 baseline)

```yaml
# reyn.yaml (= VCS commit 安全、 secret 含まず)
mcp:
  servers:
    github:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}
```

shell env 経由で resolve、 既存実装 reuse。

#### Layer 2: `--env` flag + interactive prompt (= A+B hybrid for `reyn mcp install`)

```sh
# CLI / scripting (= A: --env flag、 Docker 慣例)
$ reyn mcp install github --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx

# Interactive (= B: prompt fallback、 first-time UX)
$ reyn mcp install github
github は GITHUB_PERSONAL_ACCESS_TOKEN を必要とします。
取得方法: https://github.com/settings/personal-access-tokens/new
GITHUB_PERSONAL_ACCESS_TOKEN: ********    ← 入力時 hidden (getpass)
✓ github を追加しました。
  → reyn.yaml に `env: GITHUB_PERSONAL_ACCESS_TOKEN: ${...}` を記録
  → ~/.reyn/secrets.env に値を保存 (chmod 600)
$ reyn chat   # 即座に動く
```

`--non-interactive` flag で prompt 抑制 (= CI 用途)、 token 不足時は exit code 非 0 + post-install guide message 表示。

#### Layer 3: `~/.reyn/secrets.env` dotenv (= ADR-0030 で universal 化、 startup load)

interactive prompt で得た値を `~/.reyn/secrets.env` (chmod 600) に dotenv 形式で保存。 **Reyn process startup 時に load** して `os.environ` に inject (= ADR-0030 Decision)、 全 component (= MCP / LLM / future Web server / etc.) が透過 reuse。 既存 `${VAR}` interpolation の resolve target も自動的に dotenv 値を含む。

```
# ~/.reyn/secrets.env (= user 手で編集 OK、 自動更新は install / set-secret / reyn secret set から)
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxx
SLACK_BOT_TOKEN=xoxb-yyyyyyyy
SLACK_TEAM_ID=Tzzzzzzzz
OPENAI_API_KEY=sk-yyy           # ← MCP 以外でも universal に使える (= ADR-0030)
```

`reyn mcp set-secret` は ADR-0030 `reyn secret set` の **MCP-aware thin wrapper**: server の `mcp.servers.<name>.env` declaration (= 既存) や registry server.json `environmentVariables` を読んで「github MCP server には `GITHUB_PERSONAL_ACCESS_TOKEN` が必要」 と推論、 適切な KEY を prompt。 storage は universal (= `~/.reyn/secrets.env`)、 user が `reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN=...` を直打ちしても等価。

### Phase 2 以降の拡張 path

- **OS keyring 統合** (= Goose 流): macOS Keychain / Windows Credential Manager から secret を取得、 `~/.reyn/secrets.env` を fallback。 phase 2 で実装、 enterprise 訴求点
- **OAuth flow** (= remote / HTTP server 向け): `reyn mcp install github-remote` で browser redirect、 Web UI direction で remote `reyn serve` での OAuth callback も lift。 phase 2
- **install manifest credential 宣言** (= 5ire 流): server.json `environmentVariables[].isSecret` を読んで自動 prompt 生成 (= 既に registry schema にある field、 reuse)

### 不採用案 (= 案 C / D 単独)

- **案 C 単独 (= post-install message のみ)**: 「reyn chat で今すぐ使えます」 messaging が嘘になり OSS first-touch friction が悪化、 主路線として採用しない。 ただし credentials 不足時の **fallback message** として常に出す (= layer 2 で prompt 入力なしで進められた場合)
- **案 D 単独 (= install と set-secret の 2 step 必須)**: cognitive load 高い、 「install したのに動かない」 で user 離脱。 ただし phase 1 で `reyn mcp set-secret` を補助として併設 (= 既存 server の rotate / 後付け)

---

## 4. Registry strategy

dogfood で確認した制約 (= v0.1 freeze、 uptime 保証なし、 Anthropic 公式未登録):

- **registry.modelcontextprotocol.io 直接 fetch + Reyn 側 cache 必須**: `~/.reyn/registry-cache/<server_id>.json` (TTL 24h) で offline / timeout 時 graceful
- **fallback path**: registry 未登録の server は `--source <SOURCE_SPEC>` flag で直接指定 (= Anthropic 公式 servers / 内製 server)。 **実装済 (= commit `b668f4f`)**、 scheme は `npm:<package>` / `pypi:<package>` / `docker:<image>` / `https://github.com/<owner>/<repo>` の 4 種、 GitHub URL は heuristic resolver (= 既知 repo は `@scope/<package>` 推測、 未知 repo は `command` 空欄で graceful degrade)
- **schema version pin**: server.json `$schema` URL の date version (`2025-12-11`) を Reyn 側で記録、 skew 検出時は warning
- **`REYN_MCP_REGISTRY_URL` override**: enterprise 向け private registry (= subregistry spec) を優先 base URL に設定可能、 `mcp.registries:` priority list で複数 registry を順次検索

---

## 5. scope tier (= Claude Code 流 3 tier)

Reyn 既存の config 階層 (`~/.reyn/config.yaml` / `<project>/reyn.yaml` / `<project>/reyn.local.yaml` / `<project>/.reyn/config.yaml`) と mapping:

| scope | flag | 書き込み先 | 用途 |
|---|---|---|---|
| **local** (default) | `--scope local` | `<project>/.reyn/config.yaml` (gitignored) | 個人・project パス限定、 default |
| **project** | `--scope project` | `<project>/reyn.yaml` (commit 対象) | team 共有 (= secret は `${VAR}` 参照のみ) |
| **user** | `--scope user` | `~/.reyn/config.yaml` (gitignored、 全 project 横断) | 全 project で使う server (= filesystem 等) |

`reyn mcp install <id>` の default は **local** (= 「とりあえず試す」 の最小 commit)。 team 共有したくなったら `--scope project` で明示的に再 install。

---

## 6. Tradeoffs

### ✓ 得られるもの

- **OSS first-touch friction の主要因解消**: `mcp_search` 出力 → reyn.yaml 編集の 5-6 step → `reyn mcp install <name>` の 1 command
- **既存 `${VAR}` interpolation reuse**: secret 投入 path に新 abstraction 不要、 dotenv 1 本追加だけで安全運用成立
- **enterprise positioning**: ADR-0029 `mcp_install` permission + private registry override で「approved servers のみ」 policy が実現
- **Claude Code mental model 整合**: scope tier / `--env` flag / OAuth flow が one-to-one で対応、 cross-tool user に親しい

### △ トレードオフ

- **CLI surface 増加**: `reyn mcp` 配下に 6 subcommand (= search / install / list / remove / set-secret / clear-secret) + 既存 serve、 cognitive load 増。 mitigation: docs Quick start で「3 commands で足りる: install / list / remove、 set-secret は token rotate 時のみ」 と明示
- **registry preview API 依存**: GA 前 schema 変更 risk、 cache layer + fallback で吸収するが breaking change が来たら code 修正必要
- **subprocess lifecycle 改善は scope 外**: 既存 `list_mcp_tools` cache なし問題 / phase-side deferred / subprocess close 残留可能性 等は別 wave (= ADR-0026 follow-up or 専用 wave)
- **OAuth は phase 2 deferred**: stdio + PAT が dominant な現状で acceptable trade-off だが、 remote / HTTP server 比率が上がると優先度上昇

---

## 7. 実装着手前の未解決事項

1. **`mcp_install` IR op の P3 / P5 整合確認**: `reyn.yaml` 書き込みは workspace 外 (= `<project>` root)、 OS が直接担う pattern が ADR-0026 ToolDefinition と整合するか detail 確認
2. **既存 `mcp_search` skill の置換 vs 拡張**: GitHub HTML scraping → registry API 切替時、 既存 skill を上書き update するか、 別名 (`mcp_registry_search`) で並走させて段階移行するか
3. **dotenv loader の load timing**: `~/.reyn/secrets.env` を Reyn 起動時に load するか、 op dispatch 時に lazy load するか。 既存 `${VAR}` resolve が op dispatch 時なので latter が整合
4. **非 secret な MCP env (= `LOG_LEVEL` 等の rare case)**: phase 1 では yaml 直編集で対応、 demand surface したら phase 2 で `reyn mcp env set/unset` 追加。 docs に明示が必要
5. **`--scope` default の確定**: local default (= ad-hoc 試行) vs project default (= team 共有想定) の判断、 dogfood 第 2 round で測定推奨

---

## 8. 関連

- ADR-0029 `mcp_install` permission — `docs/deep-dives/decisions/0029-mcp-install-permission.md`
- `docs/concepts/mcp.md` Quick start section (= reyn chat first onboarding)
- `docs/deep-dives/research/positioning/web-ui-direction.md` — `reyn serve` での credentials UX (= server-side dotenv) と直結
- `docs/deep-dives/research/competitive/{openclaw,hermes-agent,pi}.md` — enterprise differentiation の競合 baseline
- 既存 `mcp_search` skill: `src/reyn/stdlib/skills/mcp_search/`
- `MCPClient` + `expand_env()`: `src/reyn/mcp_client.py`
- `PermissionDecl` / `require_mcp()`: `src/reyn/permissions/permissions.py`
