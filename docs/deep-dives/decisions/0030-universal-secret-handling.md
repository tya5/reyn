# ADR-0030: Universal secret handling — `${VAR}` everywhere + `~/.reyn/secrets.env` + `reyn secret` CLI

**Status**: Proposed (2026-05-09)
**Track**: Architecture — Reyn-wide secret infrastructure (= MCP / LLM / web / audit / future agents で共有)

---

## 1. Context

### MCP UX 検討で surface した観察

ADR-0029 (`mcp_install` permission) + positioning doc (`reyn-mcp-cli-shape.md`) の dogfood Step 0 で確認:

- **`${VAR}` env interpolation は既に部分実装済**: `mcp_client.py::expand_env()` が `mcp.servers.<name>.{env,headers,args,...}` 全 string field を再帰展開、 op dispatch 時に解決
- **ただし MCP 限定**: `llm.py:511` で `os.environ.get("LITELLM_API_BASE")` を直叩き、 `models.<name>.api_key` を yaml で `${VAR}` 書いても解釈されない

→ secret 関連 infra が **MCP 系と LLM 系で 2 重存在**、 加えて以下の future 用途で更に分裂が拡大する見込み:

| 場面 | 必要 secret | 現状 / future |
|---|---|---|
| LLM API keys | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 等 | `os.environ.get()` 直読み (= MCP の `${VAR}` と分離) |
| LiteLLM proxy | `extra_body.headers.Authorization: Bearer xxx` | PR-MODEL-SPEC で導入、 yaml `${VAR}` 未対応 |
| `reyn serve` (Web UI) | TLS cert path / browser auth token / OAuth client secret | Web UI direction doc section 6 (9) で surface 済 |
| AuditSeal anchoring | RFC 3161 TSA endpoint / KMS / vault credentials | ADR-0027 phase 2 candidate |
| external backup | AWS / GCP / Azure cloud creds | future (= `.reyn/events/` を S3 sync 等) |
| custom Python ops | phase preprocessor `python` step が外部 API 呼ぶ時の token | 既存、 `os.environ` 直読み |

### 問題

(a) **secret 投入 path の分散**: MCP / LLM / future 各 layer が独自に env 解決、 user は「どこに何を書けばいいか」 を覚える必要
(b) **declarative visibility の欠如**: `os.environ.get()` 直読みは config を見ても何の env が必要か見えない、 `${VAR}` interpolation で yaml に declarative に書ける範囲が MCP 限定で他 component で使えない
(c) **secret rotation UX なし**: 「token を rotate したい」 が user shell の env 編集 + Reyn 再起動を強制、 CLI で 1 step 完結する path がない
(d) **scope tier との非整合**: `~/.reyn/config.yaml` (user) / `<project>/reyn.yaml` (project) の階層に secret が混入する risk (= 直書きすると VCS 漏洩)

---

## 2. Decision

### 4 Layer 構成で Reyn-wide universal secret infra を確立

#### Layer 1: Universal secret store

```
~/.reyn/secrets.env                    # chmod 600、 dotenv 形式
  GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
  OPENAI_API_KEY=sk-yyy
  ANTHROPIC_API_KEY=sk-ant-zzz
  REYN_SERVE_TLS_KEY_PATH=/etc/reyn/tls.key
  ...
```

- chmod 600 強制 (= world-readable なら warning + 自動修正 prompt)
- gitignore default に追加 (= `.reyn/` 配下と同様、 既存 gitignore 参照済)
- flat namespace、 prefix 慣例 (= `REYN_*` Reyn 自身、 他は外部 service 既定 env 名)

#### Layer 2: Load timing (= startup load + future on-demand reload)

**Phase 1 (= 今回 land する範囲)**: **Reyn process startup 時に load**、 `os.environ` に inject。

- 全 Reyn component (= `LiteLLMClient.__init__` / MCP `expand_env()` / future Web server init / etc.) が透過的に dotenv 値を `os.environ.get()` で参照可能
- 既存 `os.environ.get()` 直読み code は変更不要、 dotenv 経由で値が来るだけ
- MCP `expand_env()` も `os.environ` から resolve するので breaking change なし

**Phase 2 (= future on-demand reload、 demand surface してから)**: secret rotation を process restart なしで反映する path として `reyn secret reload` CLI / `SIGHUP` handler を追加。 phase 1 では process restart で対応 (= startup 時に再 load)。

**load 失敗時の policy**: file 不在は OK (= dotenv 任意)、 file 存在で parse error は warning emit + skip (= run はそのまま継続、 hard error にしない)。

#### Layer 3: `${VAR}` interpolation の universal 化

既存 `expand_env()` (= `mcp_client.py`) と同等の resolver を **全 yaml field 共通**に lift:

```yaml
# reyn.yaml (= phase 1 で対応)
models:
  default-sonnet:
    model: claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}      # ← 拡張対象、 startup load + os.environ resolve
    extra_body:
      headers:
        Authorization: ${LITELLM_PROXY_TOKEN}

litellm:
  api_base: ${LITELLM_API_BASE}        # ← 同上

mcp:
  servers:
    github:
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}  # ← 既存、 動作不変

# future
serve:
  tls:
    key_path: ${REYN_SERVE_TLS_KEY_PATH}
    cert_path: ${REYN_SERVE_TLS_CERT_PATH}
```

- resolver は config load 時に走り、 `${UNDEFINED_VAR}` は warning + 空文字列 fallback (= 既存 `expand_env()` と同 policy)
- `$$` で literal `$` escape (= 既存 yaml + `${VAR}` の慣例)
- 全 string field を再帰走査、 dict / list 入れ子も対応

#### Layer 4: `reyn secret` CLI surface (= phase 1 同時 land)

```bash
reyn secret set <KEY>[=<VALUE>]    # value 省略で interactive prompt (hidden input)
                                   # → ~/.reyn/secrets.env に書き込み (chmod 600 強制)

reyn secret list                   # KEY 一覧、 value は表示しない (= "set" / "unset" のみ表示)
                                   # source も併記 (= secrets.env / shell env / 両方)

reyn secret clear <KEY>            # 単一削除

reyn secret rotate <KEY>           # set の semantic alias (= rotation 意図を audit log に明示)
                                   # 旧値を audit log に記録 (= mask 済)、 新値を set
```

flat namespace、 全 Reyn component が共有。 既存の `reyn config set` は **secrets を扱わない** (= namespace 分離、 `reyn config set` は yaml 構造 edit 専用、 secret は `reyn secret` 専用)。

#### Layer 5: MCP-aware UX (= ADR-0029 の `reyn mcp set-secret` を thin wrapper 化)

```bash
reyn mcp install github           # registry server.json の environmentVariables を読み、
                                  # isSecret=true な KEY を prompt → 内部で `reyn secret set` を呼ぶ
                                  # mcp.servers.github.env に ${KEY} 参照を書く

reyn mcp set-secret github GITHUB_TOKEN[=...]
                                  # github MCP server に必要な secret を prompt
                                  # 内部で `reyn secret set` (= storage は universal)
```

つまり `reyn mcp set-secret` は **「server 側の env 要求 declaration を読んで適切な KEY を prompt する MCP-aware UX」**。 storage は universal、 user が `reyn secret set` 直打ちでも等価。

---

## 3. Consequences

### ✓ 得られるもの

- **secret 投入 path の単一化**: 「Reyn の secret は `~/.reyn/secrets.env` + `${VAR}` 参照」 1 mental model
- **declarative visibility**: yaml で `${VAR}` 書ける範囲が拡大、 「この config は何の env を要求するか」 を grep で確認可能
- **future-proof**: Web UI / AuditSeal / external backup / custom skill 全てで同 infra 再利用、 後付け tech debt が発生しない
- **業界 alignment**: dotenv (= python-dotenv / direnv 慣例)、 `${VAR}` (= Docker / k8s / ansible 慣例)、 `<tool> secret set` CLI (= GitHub Actions / gcloud 慣例) 全てに整合
- **rotation UX**: `reyn secret rotate <KEY>` で audit log + new value、 process restart なしへの拡張 path 確保 (= phase 2)

### △ トレードオフ

- **process startup 時の env 露出**: dotenv 値が `os.environ` に居るため subprocess に inherit される。 Reyn が起動する subprocess (= MCP server / python preprocessor / etc.) は意図的に env を継承するので機能上の問題はないが、 Reyn が起動した別プログラムにも secrets が見える。 mitigation: `_DEFAULT_REDACT_PATTERNS` (= 既存 LLM trace dump 用) で trace 系の漏洩は防御済、 subprocess に明示的に渡す env だけ pass する filtering option を future で追加余地
- **既存 `os.environ.get()` 直読み code への migration**: phase 1 では既存 code 不変 (= dotenv 経由で値が来るだけ)、 ただし 「config で `${VAR}` 宣言」 と 「直 `os.environ.get`」 の混在が残る。 phase 2 で「全 secret 参照を yaml `${VAR}` 経由に migrate」 wave 候補
- **scope tier との interaction**: `secrets.env` は user-global (= `~/.reyn/secrets.env`) のみ、 project-scope `secrets.env` は phase 1 では追加しない。 demand surface したら phase 2 で `<project>/.reyn/secrets.env` (gitignored) を追加 layer に
- **CLI surface 増加**: `reyn secret {set,list,clear,rotate}` 4 subcommand 新設、 ただし `reyn config set` と namespace 分離で confusion 軽減

### 不採用案

- **MCP-only で先行 + 後 wave で universal 化**: tech debt 大きい (= LLM / Web UI / AuditSeal の implementation で再度 secret 投入 path を定義する必要)、 dogfood で「将来 secret 用途 5+ 件確実」 と判明済なので後付けの理由が薄い、 で却下
- **OS keyring を Layer 1 に据える** (= Goose 流): macOS Keychain / Windows Credential Manager / Linux Secret Service を default storage に。 ✓ 最もセキュア、 ✗ headless server / Docker container / WSL 等の keyring 不在環境 fallback で結局 dotenv 必要 + cross-platform 抽象化 cost 高い、 で **phase 2 候補** (= dotenv に keyring layer を追加で被せる)
- **`${VAR}` interpolation の lazy resolve (= 元判断 4-B)**: MCP `expand_env()` の op-dispatch-time resolve を全 yaml に拡張、 ただし `LiteLLMClient.__init__` 等 startup 時 read code が dotenv 値見えない問題を解消できないため不採用、 startup load + 全 component で `os.environ.get()` 透過参照に統一

---

## 4. 実装コスト見積もり

| タスク | コスト |
|---|---|
| `~/.reyn/secrets.env` startup loader (= dotenv parse + `os.environ` inject + chmod 600 enforce) | XS (= 30-50 行、 `python-dotenv` 依存追加 or 自前) |
| `${VAR}` interpolation を全 yaml field に lift (= 既存 `expand_env()` を generic 化、 `config.py::load_config()` 後段で適用) | S (= 既存実装 reuse、 全 yaml field 走査の generic resolver ~50-80 行) |
| `reyn secret {set,list,clear,rotate}` CLI 4 subcommand | S (= file I/O + chmod + interactive prompt、 各 subcommand ~30-50 行) |
| audit event emission (= `secret_set` / `secret_cleared` / `secret_rotated`、 value masked) | XS |
| `reyn mcp set-secret` を `reyn secret set` の thin wrapper に refactor | XS (= 既存 wrapper を universal 呼出に書き換え) |
| Tier 2 test (= load timing / `${VAR}` 全 yaml / CLI subcommand / chmod enforce) | S (= 8-12 件) |
| docs (= `secret-handling.md` 新設 + `reyn.yaml` reference 拡張 + Quick start example 更新) | S |
| **合計** | **MEDIUM** (= 1.5-2 day、 MCP wave に bundle 推奨) |

phase 1 に bundle する理由: secret infra が分散したまま MCP wave を land すると後で再 refactor、 同時 land で「secret は universal」 のmental model を OSS user 初日から提示できる。

---

## 5. 関連

- ADR-0029 `mcp_install` permission — `docs/deep-dives/decisions/0029-mcp-install-permission.md` (= permission gating は orthogonal、 storage は ADR-0030 が provide)
- ADR-0027 AuditSeal — `docs/deep-dives/decisions/0027-audit-seal-separation.md` (= phase 2 RFC 3161 anchoring credentials は ADR-0030 の `${VAR}` 経由で解決)
- positioning doc `reyn-mcp-cli-shape.md` (= `reyn mcp set-secret` を ADR-0030 universal store の MCP-aware wrapper として位置づけ)
- Web UI direction doc section 6 (9) (= server-side secret storage は ADR-0030 dotenv で実現)
- 既存 `expand_env()`: `src/reyn/mcp_client.py::expand_env()` (= phase 1 で generic 化される)
- gitignore default: `.gitignore:21,26` (= `.reyn/` / `.env*` が既に対象、 `~/.reyn/secrets.env` は user-home なので gitignore 範囲外、 file 自体の chmod 600 で defense)

---

## 6. Acceptance criteria (= ADR Accepted 昇格時の確認 list)

- [ ] `~/.reyn/secrets.env` startup loader 実装 + chmod 600 enforce + parse error graceful degradation
- [ ] `${VAR}` interpolation generic resolver 実装、 `mcp.servers.<name>.env` 既存挙動が後方互換
- [ ] `models.<name>.api_key` / `litellm.api_base` を yaml `${VAR}` で書ける (= 既存 `os.environ.get()` 直読み path と coexist)
- [ ] `reyn secret {set,list,clear,rotate}` 4 subcommand 動作 + Tier 2 test
- [ ] `reyn mcp set-secret` が `reyn secret set` を内部 call、 storage が universal
- [ ] Tier 3 e2e (= `~/.reyn/secrets.env` に key を書いて → reyn 起動 → MCP / LLM 両方が透過動作)
- [ ] audit event emission (= `secret_set` / `secret_cleared` / `secret_rotated`、 value 完全 mask)
- [ ] docs (= `docs/concepts/secret-handling.{md,ja.md}` 新設、 `reyn.yaml` reference 拡張)
