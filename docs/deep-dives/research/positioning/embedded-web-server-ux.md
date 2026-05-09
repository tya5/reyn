# Embedded Web Server UX — TUI ライフサイクルに連動する

**Status**: Design direction (2026-05-09) — UX positioning。 architectural commitment ではないため ADR ではなく positioning doc として記録。
**Track**: Web UI / CLI 統合 UX

> 元 ADR-0028 として提案されたが、 (a) P1-P8 のような不変条件に触れず 1 つの implementation choice (= 同一 process embedded) であり後で daemon-default に倒すことも可能、 (b) 「 Web UI 自体の存在意義 / 機能スコープ」 が未 defending、 の 2 点で ADR ではなく design proposal として記録する。 architectural decision 化するなら先に「 Web UI で何を達成するか」 の scope ADR が前提。

---

## 1. Context

### 解決したい問題

「サーバを起動したが、クリーンアップしていなくて生き続けている」問題。

開発者ツールの典型的な失敗パターン：

```
$ langchain serve &   # バックグラウンドで起動
$ ... (忘れる)
$ ps aux | grep lang  # 数日後に孤児プロセスを発見
```

Web UI を提供するために別サーバーを立てるアーキテクチャは、
ライトユーザーに「サーバー管理」という概念を押し付ける。

### 想定するライトユーザー像

- 「サーバって何？」という状態でも使えてほしい
- `reyn chat` と打てばチャットができることだけ知っていればいい
- Web UI があることは URL を見て初めて知ってもよい

---

## 2. Direction

### Web Server を TUI プロセスに embedded する

```
reyn chat
  → TUI 起動（プロセス開始）
  → Web Server も同一プロセス内で起動（ユーザーは意識しない）
  → TUI に URL が表示される
  → ブラウザで開くとリッチな Web UI で会話できる
  → TUI を閉じる（プロセス終了）
  → Web Server も消える（孤児プロセスなし）
```

### ユーザー体験フロー

```
$ reyn chat

╭─────────────────────────────────────╮
│  Reyn  ·  http://localhost:8765     │  ← URL は表示するが説明しない
╰─────────────────────────────────────╯
> _
```

1. ユーザーは TUI でチャットする（URL を無視してもよい）
2. URL をブラウザで開くと Web UI が使える
3. CLI と Web UI は同一の Agent Worker を共有 → 同じセッションが見える
4. TUI を閉じる → プロセス終了 → サーバ消滅 → 孤児なし

### ライフサイクル原則

```
TUI のライフサイクル = Web Server のライフサイクル（デフォルト）
```

- TUI 起動 → Web Server 起動（自動）
- TUI 終了 → Web Server 終了（自動）
- ユーザーが「サーバを管理する」必要はない

### デーモンモードは明示的オプトイン

```bash
reyn serve --daemon   # 上級者が明示的にデーモン化する場合のみ
```

デフォルトを「閉じたら消える」にすることで、
`--daemon` を使う人は「自分がサーバを管理する」と自覚している。

---

## 3. 内部アーキテクチャ

### Agent Worker の共有

CLI と Web Server は同一プロセス内で同じ Agent Worker（asyncio）を使う。

```
同一プロセス:
  ├── TUI (CLI interface)  ──→ (direct coroutine)
  ├── Web Server           ──→ (coroutine + SSE)
  └── Agent Worker (asyncio, 共有インスタンス)
       └── Workspace
```

CLI は localhost 経由不要（同一 event loop 内で直接呼び出し）。
Web Server の HTTP は外部ブラウザ向けのみ。

### イベントストリーム

TUI と Web UI は同じ Agent Worker のイベントを購読する。

```
Agent Worker
  └── Events
        ├── TUI が subscribe → リアルタイム描画
        └── Web Server が subscribe → SSE でブラウザに push
```

---

## 4. 競合との比較

| フレームワーク | 構造 | 孤児リスク |
|---|---|---|
| LangGraph Studio | 別サーバー (HTTP) | あり |
| AutoGen Studio | 別サーバー (HTTP) | あり |
| CrewAI+ | クラウド | なし（でも自前 UI なし） |
| **Reyn** | **embedded (同一プロセス)** | **なし** |

---

## 5. Tradeoffs

### ✓ 得られるもの

- ライトユーザーにサーバー管理の概念を押し付けない
- 孤児プロセスが原理的に発生しない
- CLI と Web UI が同一セッションを共有できる
- `reyn chat` だけ知っていれば Web UI も使える

### △ トレードオフ

- TUI を閉じると Web UI も切れる（意図的設計）
- 長時間実行タスクを Web UI だけで監視したい場合は `--daemon` が必要
- Web Server のポート競合時の UX を考慮する必要がある（ポート自動選択で対応可）

---

## 6. 関連

- ADR-0027: AuditSeal 分離（Web UI での監査ログ表示に関連）— `docs/deep-dives/decisions/0027-audit-seal-separation.md`
- P5: Workspace SSoT（CLI / Web UI 間のセッション共有の基盤）
- P6: Events（TUI と Web UI への共通イベントストリームの基盤）

## 7. ADR 化に向けた未解決事項

ADR に格上げする前に decide すべき項目 (= ADR review で出た指摘):

1. **`localhost:8765` の auth model**: 同 host 別 user / docker / sidecar からの leak risk。 token query string / Unix socket / origin check のいずれかは production 必須。
2. **port collision policy**: ephemeral port (= URL 毎回変化、 bookmark 不能) vs fixed port fail-fast vs sequential fallback の trade-off は ADR-level。
3. **plan-mode async (ADR-0022) との整合**: long plan 走行中に TUI 閉じると plan abort? detach? ADR-0023 resume との UX flow。
4. **同一 AgentWorker の concurrency model**: TUI 入力処理中に Web 入力来た時の serialize 戦略。 単一 ChatSession に 2 input source が暗黙前提。
5. **Web UI の機能スコープ ADR**: 「 リッチな Web UI で会話できる」 だけでは Web UI の差別化価値が defending できていない。 「 何を Web UI に出して、 何を TUI に留めるか」 の scope ADR が前提条件。
