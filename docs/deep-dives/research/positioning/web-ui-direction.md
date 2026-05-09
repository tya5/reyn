# Reyn Web UI direction — `reyn chat` (local + embedded) + `reyn serve` (= browser remote access)

**Status**: Design direction (2026-05-09 update 4) — UX positioning。 architectural commitment ではないため ADR ではなく positioning doc として記録。 **実現性検討は未着手**。
**Track**: Web UI / CLI 統合 UX

> Update 履歴: (1) 元 ADR-0028 「同一 process embedded」、 (2) 一度 `reyn serve / reyn client` の 2 軸案、 (3) 業界慣行寄せで `reyn chat --host` 統合案、 (4) **update 4 で workspace location semantics の懸念から remote TUI client を当面 scope 外**に。 現在 vision は **`reyn chat` (= local + embedded Web UI)** と **`reyn serve` (= explicit long-running server、 browser からアクセス)** の 2 commands のみ。 remote TUI client (= 同 TUI codebase が remote server に接続) は feasibility 検討時に naming + mode boundary 決定とセットで再判断 (= section 8 「Deferred: remote TUI client」 参照)。

## 業界慣行との整合

| Tool | Server start | Client (local default) | Remote browser access |
|---|---|---|---|
| Ollama | `ollama serve` | `ollama run` (auto-start local) | (= 別 web UI app: Open WebUI) |
| Docker | `dockerd` | `docker …` | (= 別 web UI: Portainer / Docker Desktop) |
| LangGraph | `langgraph up` | (= web UI 経由) | (= LangGraph Studio が同梱) |
| vLLM | `vllm serve` | (= no built-in CLI client) | (= OpenAI-compat HTTP API) |
| **Reyn (current direction)** | **`reyn serve`** | **`reyn chat`** (local + embedded Web UI 同梱) | **`reyn serve` の URL を browser で開く** |

`<tool> serve` は modern AI tooling の dominant pattern。 Reyn の差別化は **`reyn chat` 単独で TUI + browser Web UI 両方手に入る (= ADR-0028 thesis)** + **`reyn serve` で multi-user browser access** が同 codebase で実現される点。 remote TUI client は他 tool にもほぼ存在しないため、 当面は browser を remote access path として確定。

---

## 1. Context

### 解決したい問題

(1) **「サーバを起動したが、クリーンアップしていなくて生き続けている」**
開発者ツールの典型的な失敗パターン (= LangGraph Studio / AutoGen Studio の「別サーバー HTTP」 パターン)。

(2) **「Web UI と TUI で全く別の codebase が並走する」**
Web UI を作ると TUI と機能 drift が発生し、 メンテコストが二重になる。

### 想定するユーザー像

- **ライトユーザー**: `reyn chat` だけ知っていれば local で完結する
- **チームユーザー**: 自宅 PC で `reyn serve` 立てて、 別マシン / mobile から `reyn client` で接続したい
- **enterprise**: 中央 server (= compliance / audit / quota 集中管理) に複数開発者が `reyn client` で接続

---

## 2. Direction (現在の vision)

### 2 commands

```
reyn chat                          # local in-process AgentWorker
                                   # + embedded Web UI server (session 生存期間 bind)
                                   # サーバ概念は意識不要、 URL は表示のみ
                                   # → 旧 ADR-0028 embedded thesis をこちらで実現

reyn serve [--bind 0.0.0.0:8765]   # explicit server daemon
                                   # 明示的に長生き、 user-managed
                                   # multi-user / multi-device は browser でアクセス
```

remote TUI client は当面 scope 外 (= section 8 参照)、 remote access は **browser 経由** に統一。

### ライフサイクル原則

```
reyn chat   → process 終了で TUI + embedded Web UI 両方消える
              (= session lifecycle = 全 server lifecycle、 孤児なし)
reyn serve  → user が明示的に start / stop (= 自覚あり)
              browser からアクセス、 client TUI なし
```

「local で完結したい light user」 (= `reyn chat` で TUI + browser から URL 開けば Web UI も使える) と「multi-user / multi-device で運用したい power user」 (= `reyn serve` + browser) が **orthogonal な選択肢**として分離されている。 embedded thesis (= TUI lifecycle = Server lifecycle) は `reyn chat` 内で温存される。

### Multi-user / multi-device の自然な path

`reyn serve` を別軸として置くことで、 embedded だけでは塞がっていた pathway が開く:

- 1 server に複数 browser tab 接続 (= teams)
- mobile で server 状況を確認 (= browser 経由、 client app 不要)
- 中央 server で audit / quota / compliance 集中管理

`reyn chat` の embedded だけでは「自分の TUI を立ち上げないと server が動かない」 ので multi-user / multi-device pathway が塞がる。 `reyn serve` + browser がその限界を解消する別系統。 remote TUI client は当面 scope 外 (= section 8)、 必要性は実装着手時に再判断。

---

## 3. 内部アーキテクチャ (= 概念レベル、 実現性検討前)

### 2 modes, 2 architectures

```
reyn chat (local + embedded Web UI):
  同一 process:
    ├── TUI ──→ AgentWorker (direct coroutine)
    ├── embedded Web UI server ──→ AgentWorker (direct coroutine + SSE)
    └── AgentWorker (asyncio, 共有インスタンス)
         └── Workspace (P5 SSoT — local cwd)

reyn serve:
  独立 process (= remote machine も可):
    ├── AgentWorker (asyncio)
    ├── Workspace (P5 SSoT — server side)
    └── HTTP / SSE / WebSocket endpoints
         └── browser tab(s) が接続
```

remote TUI client は当面 scope 外。 仮に将来追加するなら、 同 codebase で transport だけ切替える pathway は section 8 で 3 案 (Pattern A / B / scope keep-out) のいずれかを選ぶ判断が必要。

---

## 4. 競合との比較

| フレームワーク | 構造 | 孤児リスク | client UX |
|---|---|---|---|
| LangGraph Studio | 別サーバー (HTTP) | あり | Web only |
| AutoGen Studio | 別サーバー (HTTP) | あり | Web only |
| CrewAI+ | クラウド | なし | Web only |
| Claude Code | local subprocess | なし | TUI only (= no remote) |
| **Reyn (current direction)** | **`reyn chat` (local + embedded Web UI) / `reyn serve` (explicit)** | **chat は session bind で孤児なし、 serve は user-managed** | **`reyn chat` 単独で TUI + browser Web UI 両方同梱、 remote は `reyn serve` + browser** |

差別化 point:
- 「ライト user は `reyn chat` 1 コマンドで TUI + Web UI 両方手に入る」 (= 旧 ADR-0028 thesis)
- 「power user は `reyn serve` + browser で multi-user / multi-device」 (= LangGraph Studio が unique に持っていた領域、 Reyn は同 codebase の Web UI を再利用)
- remote TUI client (= 同 TUI codebase が remote server に接続) は当面 scope 外 (= section 8)、 browser remote access で代替

---

## 5. Tradeoffs

### ✓ 得られるもの

- `reyn chat` で light user UX 維持 (= サーバ概念なし、 session bind で孤児なし)
- `reyn chat` 単独で TUI + Web UI 両方使える (= 旧 ADR-0028 embedded thesis を温存)
- `reyn serve` + browser で multi-user / multi-device path
- 業界慣行 (= Ollama / vLLM / LangGraph) の `<tool> serve` pattern に整合
- scope が clean (= 2 commands、 transport flag 等の cognitive overhead なし)
- 「light user で始めて → 必要になったら serve に scale up」 の漸進 path が明確

### △ トレードオフ

- **embedded Web UI server と AgentWorker concurrency**: `reyn chat` 同一 process で TUI input / browser input が並走、 単一 ChatSession に 2 input source が暗黙前提 (= 旧 ADR-0028 から継続する未解決項目、 `reyn serve` の concurrency と統合的に設計したい)
- **`reyn serve` の auth / authorization 設計が必須**: browser exposure に対する token / TLS / origin check baseline は ADR 化前提
- **embedded vs serve の Web UI codebase 同期**: `reyn chat` の embedded Web UI と `reyn serve` の Web UI が drift しないように同 code path で実装したい (= 機能差分が出ると user 混乱)
- **remote TUI client を当面 scope 外にした opportunity cost**: 「TUI で remote server を操作したい」 user は browser に切り替えてもらう必要。 LangGraph Studio 等との UX 差別化を 1 つ放棄

---

## 6. 実現性検討の前 TODO

実装着手前に評価が必要 (= 未着手):

1. **AgentWorker abstraction の design feasibility**: local in-process と remote RPC を 1 つの interface で抽象化できるか。 既存 ChatSession / RouterLoop / dispatch_tool が前提とする invariants (= asyncio event loop / direct method call) を守ったまま remote 化が可能か。
2. **`reyn chat` 内 embedded Web UI server の port / lifecycle**: session 開始時に port allocate (= ephemeral or 8765 fixed?)、 session 終了時に確実に release。 同 host で多重 `reyn chat` が走る場合の port collision policy。
3. **`reyn chat` 内 embedded vs `reyn serve` の同 codebase 化**: embedded Web UI server と `reyn serve` の HTTP / SSE endpoint は同じ code path で良いか (= AgentWorker location だけ違う)、 別実装か。 同 codebase 化が望ましいが session bind なら ephemeral 設定 + auth skip 等の差分管理が必要。
4. **Workspace (P5) の location 設計**: `reyn serve` 側に Workspace、 client 側は viewer のみ? それとも client にも cache layer? events stream で artifact 同期する? P5 SSoT 不変条件をどう保つか。
5. **Events (P6) の transport 設計**: SSE / WebSocket / gRPC streaming のどれが Reyn の event shape に fit するか。 backpressure / retry / out-of-order handling の rule。
6. **AgentWorker concurrency**: `reyn chat` 内 embedded で TUI + browser から並列 input、 もしくは `reyn serve` で複数 client 接続時、 同一 ChatSession に 2+ input source の concurrency model (= 直列化 / per-client session / locking)。
7. **Auth / Authorization**: `reyn serve` の network exposure に対する token / TLS / per-client permission baseline。 `reyn chat` 内 embedded は localhost-only + token 自動発行で auth skip 可能か。 enterprise 視点で必須。
8. **plan-mode async (ADR-0022/0023) との整合**: `reyn chat` (local) で TUI 閉じる前に browser tab で plan 監視中なら? の UX。 `reyn serve` 側で plan 走行中に browser tab disconnect → reconnect で plan resume の semantics。
9. **server-side secret storage** — **resolved by ADR-0030 universal secret handling** (= `~/.reyn/secrets.env` startup load + `${VAR}` 全 yaml 拡張)。 `reyn serve` (= remote 運用) でも同 dotenv pattern を server 側に固定配置 (= chmod 600)、 TLS key path / browser auth token / OAuth client secret 等を `${REYN_SERVE_TLS_KEY_PATH}` 等で参照。 `reyn chat` embedded と `reyn serve` で同 universal infra 透過 reuse 可能 (= phase 1 で ADR-0030 land 済前提)。 KMS / vault 統合は ADR-0030 phase 2 で keyring layer として上に被せる pathway。
10. **single-user assumption for `reyn serve`** (= 2026-05-09 MCP UX 検討で surface): multi-user で同 server に接続した場合の secret isolation (= Alice の `GITHUB_TOKEN` を Bob が見えない)、 session isolation、 audit attribution。 OSS 1.0 では **single-user assumption** (= LangGraph Studio local mode 流) で punt、 multi-tenant は enterprise scope で別 ADR (= ADR-0027 AuditSeal の per-user attribution と統合的に設計)。 ただし「`reyn serve` は当面 single-user 前提」 を docs に明示が必要。
11. **prototype 範囲**: feasibility study の output として、 何を minimum demo として作るか (= echo agent + browser SSE / events stream のみ / `reyn chat` embedded の URL 表示まで / `reyn serve` 単体起動 / etc.)。

これらが解決した時点で **`Web UI 機能スコープ ADR` (= Web frontend は何を見せる / TUI は何を留める)** を defining する ADR を作り、 そこから順次 sub-ADR を切る pathway。

---

## 7. 関連

- ADR-0027: AuditSeal 分離 — `docs/deep-dives/decisions/0027-audit-seal-separation.md` (= `reyn serve` 側で seal、 browser から監査ログ閲覧)
- P5: Workspace SSoT (= `reyn chat` local cwd / `reyn serve` server side、 location が異なる semantics は section 8 の deferred 判断と直結)
- P6: Events (= `reyn serve` → browser への SSE / WebSocket transport の基盤)
- 旧案 (= embedded same-process only): この doc の git history (= rename 前 `embedded-web-server-ux.md`) に保存。 update 2/3/4 で iterate した direction shift の経緯は section 8 末尾 + commit log 参照。

---

## 8. Deferred: remote TUI client (= 当面 scope 外)

### 経緯

Update 2/3 で「同 TUI codebase が remote server に接続する」 方向 (= `reyn serve / reyn client` or `reyn chat --host`) を検討したが、 update 4 で **当面 scope 外** に再判断。

### 判断理由

**workspace location semantics の懸念**: P5 (Workspace SSoT) は Reyn UX で visibly local (= cwd の `.reyn/`)、 events / memory / artifact が filesystem で見える。 同じ `reyn chat` command で transport だけ remote に切り替えると、 light user 視点で「`.reyn/` どこ?」 「server 側? local 側? どっち?」 という mental model 衝突が発生する。

Docker / Ollama / kubectl は 「client は常に client、 state は接続先」 という model が ecosystem 全体で確立している (= Pattern A) ので flag 切替で混乱しない。 一方 SSH / tmux / screen は別 verb (`ssh`, `tmux attach`) で「別 session に join する」 を signal する (= Pattern B)。

Reyn は workspace が visibly local という性格上 **Pattern B 寄り**だが、 そもそも remote TUI client が必要かどうかの判断が feasibility 検討前。 まず browser remote access (= `reyn serve` + browser tab) で multi-user / multi-device path を確保し、 「browser では足りず TUI が remote 操作したい」 という concrete demand が出てから再判断する pathway。

### 将来 revisit 時に選ぶ 3 案

remote TUI client 実装が決まった時点で以下のいずれかを選ぶ:

| 案 | 例 | 業界 analog | trade-off |
|---|---|---|---|
| **A: 同 command + flag** | `reyn chat --host <addr>` (or `REYN_HOST=…`) | Ollama (`OLLAMA_HOST`) / Docker (`DOCKER_HOST`) / kubectl (`--context`) | 業界慣行寄り、 light user の workspace location 混乱 risk あり |
| **B: 別 verb で接続を明示** | `reyn attach <addr>` (or `reyn connect <addr>`) | SSH / tmux attach / screen | workspace 切替を verb で signal、 Reyn 独自 verb 感 |
| **C: scope keep-out** | (= remote TUI client 出さない、 browser only) | LangGraph Studio / Open WebUI | scope clean、 「TUI で remote」 UX 機会喪失 |

update 4 時点では **C を default** に、 A / B は concrete demand が出た時に再評価。

### 再判断の trigger

- `reyn serve` + browser を実装してから、 「browser では UX 不足、 TUI で remote したい」 という concrete user demand が観測された時
- enterprise customer から TUI-based remote operations の specific 要件が出た時
- ADR-0027 AuditSeal 実装時に「監査用 read-only TUI viewer for remote」 が必要と判明した時 (= 監査 viewer は browser で十分の可能性大、 要再判断)

### Update 4 で削った検討項目 (= 将来 remote TUI 案件で復活する)

- AgentWorker abstraction の design feasibility (local in-process / remote RPC を 1 interface で抽象化)
- `reyn chat --host` の disconnect / retry UX
- `--host` flag vs `REYN_HOST` env var 優先順位 + `reyn.yaml` との関係
- naming 決定 (Pattern A / B のどちら、 or 別案)
