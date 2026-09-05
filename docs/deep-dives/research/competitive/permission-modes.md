---
title: パーミッションモード — CC / Codex / OpenClaw / Hermes と Reyn
last_updated: 2026-09-06
status: stable
sources:
  - url: https://code.claude.com/docs/en/permissions
    accessed: 2026-09-06
  - url: https://learn.chatgpt.com/docs/agent-approvals-security
    accessed: 2026-09-06
  - url: https://developers.openai.com/codex/concepts/sandboxing
    accessed: 2026-09-06
  - url: https://docs.openclaw.ai/tools/permission-modes
    accessed: 2026-09-06
  - url: https://docs.openclaw.ai/tools/exec-approvals
    accessed: 2026-09-06
  - url: https://hermes-agent.nousresearch.com/docs/user-guide/security
    accessed: 2026-09-06
---

# パーミッションモード — 競合 4 種と Reyn の差

owner 依頼 (2026-09-06): 「reyn のパーミッションシステム仕様を業界標準に寄せたい」。**何が業界標準なのか**を一次資料で確定し、Reyn との差を **閉じるべき差 / 意図的に厳しい差 / 他に無い差** に分けて記録する。

⚠️ **この doc は describing doc であって deciding doc ではない。** 「寄せる」かどうか、どこまで寄せるかは未裁定。ここに在るのは観測と選択肢だけで、決定は含まない。

## 1. 各システムのダイヤル (一次資料、2026-09-06 時点)

| | ダイヤルの名前 | 値 | 既定 |
|---|---|---|---|
| **Claude Code** | `defaultMode`(設定) / セッション中のモード | `default`(=Manual) · `acceptEdits` · `plan` · `auto` · `dontAsk` · `bypassPermissions` | プランにより異なる |
| **Codex** | `approval_policy` **×** `sandbox_mode` の 2 本 | 承認: `untrusted` · `on-request` · `never`(＋`granular`) ／ sandbox: `read-only` · `workspace-write` · `danger-full-access` | `Auto` プリセット = `workspace-write` + `on-request` |
| **OpenClaw** | `tools.exec.mode` | `deny` · `allowlist` · `ask` · `auto` · `full` | `auto` を推奨 |
| **Hermes** | `approvals.mode` ＋ sandbox backend | `smart` · `manual` · `off` ／ backend: local · docker · ssh · singularity · modal · daytona (・vercel) | `smart` |
| **Reyn** | **無し** | — | — |

## 2. 収束している形 = 「業界標準」の中身

4 種が独立に同じ形に到達している点。**これが「寄せる」対象の実体**である。

1. **名前の付いた姿勢のダイヤルが 1 本ある。** 値は 3〜6 個、厳→緩に順序が付き、実行中に切り替えられる。4 種すべてが持つ。
2. **姿勢の宣言と、強制の境界は別軸。** Codex は 2 キー＋プリセットで明示。CC は「permissions と sandboxing は補完的な層」と明言。Hermes は backend 選択と `approvals.mode` が独立。OpenClaw は自ら「sandbox / approvals / tool policy の 3 つの別の安全層」と説明する。
3. **「毎回訊く」と「訊かない」の**間**に、自動レビュアの段がある。** CC `auto`(classifier) / OpenClaw `auto`(native auto-reviewer) / Hermes `smart`(補助 LLM)。**4 種中 3 種**。Codex だけは代わりに `granular`(カテゴリ別) を持つ。
4. **上書きできない床がある。** CC: どの層の deny も他の層で覆せない・managed settings は CLI 引数でも覆せない・`disableBypassPermissionsMode`。Hermes: hardline blocklist は mode / YOLO / 明示承認のいずれでも覆らない。OpenClaw: 実効ポリシーは「`tools.exec.*` と approvals の**厳しい方**」でセッション上書きはホストの床を越えられない。Codex: `workspace-write` でも `.git` / `.codex` は再帰的に読み取り専用。
5. **deny > allow、実効権限は積集合。** CC と OpenClaw が明文化。
6. **無人時は fail-closed。** Hermes は文脈ごとに既定を持つ (`cron_mode` / `single_query_mode` / `unattended_mode` すべて `deny`)。CC の `dontAsk` は未承認を拒否する。

## 3. 永続の仕方 — ここは 4 種でバラバラ (＝標準が無い)

| | 承認はどこまで残るか | 何に紐づくか |
|---|---|---|
| Codex | **セッションを跨がない**(毎回ポリシーを適用し直す) | — |
| Hermes | once / session / **always** → `command_allowlist` | **パターンのみ。パス別・ツール別の scope 無し** |
| Claude Code | 「リポジトリとコマンド単位で恒久的」、設定ファイルに残る | repo + コマンド |
| OpenClaw | SQLite の承認 DB に恒久化、失効可 | **argv と承認時の作業ディレクトリ**に束縛 |
| **Reyn** | 追記専用台帳 (`.reyn/approvals.jsonl`)、最後の記録が勝つ | **actor / op / path** ＋ **agent scope** ＋ **inode 同一性** |

## 4. Reyn との差

### (a) 閉じるべき差 — **姿勢のダイヤルが無い**

Reyn には 3 層の許可階層 (既定 → 宣言 → 事前承認) と、実行時の**連言的な restrict 層**が在る。しかし **「今どの姿勢か」をユーザが名指して切り替えられる 1 本のつまみが無い。** 4 種すべてが持ち、しかもそれが各システムの主要な UX 面になっている。

⚠️ **これは機能の欠落ではなく、面の欠落である。** Reyn の機構は既に厳しい側に十分揃っており、足りないのは**それらを 3〜5 個の名前に畳んだ操作面**。

### (b) 意図的に厳しい差 — **粒度**

Reyn は per-path で、しかも承認したパスの **inode 同一性を初回に束縛して毎回照合**する (#5042 — 同名で作り直された別オブジェクトは再プロンプト)。**4 種のどれもこれを持たない。** 最も近い OpenClaw でも argv + cwd 束縛まで。Hermes に至ってはパス別 scope が無い。

**寄せる際にここを捨ててはいけない。** 粒度を落とすと、Reyn が主張している「監査可能・回復可能」の根拠が減る。

### (c) 他に無い差 — **actor / agent 次元**

承認キーが actor scope で、決定ごとに **agent scope** を持つ (#5052: agent 識別があれば `agent:<name>`、無ければ workspace 全体)。**4 種のどれにも無い。** 理由は単純で、他はマルチエージェント OS ではないため。**業界標準に無い＝落とす理由にはならない。**

### (d) 業界標準から外れている差 — **使う前の「宣言」**

Reyn の層 2 は `reyn.yaml` の `permissions:` ブロックで、**宣言していない能力はそもそも要求できない**。これは iOS / Android のマニフェスト方式であって、**CC / Codex / OpenClaw / Hermes のどれにも無い**。4 種はいずれも「エージェントは何でも試みてよく、使う瞬間に門で止める」。

**これが「Reyn は重い」と感じる主因**であり、寄せるとしたら最初に議論すべき点。ただし宣言には実利がある — **使う前に権限の目録が読める**（他 4 種では実行してみるまで分からない）。

### (e) 強制の差 — safe-mode python

Reyn の `sandboxed_exec` は Docker ＋ Landlock / Seatbelt / Seccomp を持つ一方、**safe-mode python は AST 検証＋`reyn.api.safe.*` の honor-system**。Codex/CC は OS sandbox、Hermes/OpenClaw はコンテナを境界にしている。**ここは Reyn が弱い側**。

## 5. 未裁定の論点 (owner 判断)

⚠️ **以下は選択肢であって推奨ではない。** それぞれに私の見立てを併記する。

1. **姿勢のダイヤルを足すか。** 足す場合、既存の連言的 restrict 層の**上に**名前を被せるだけで済むか、層自体を作り直す必要があるかは未測定。
2. **層 2 の宣言を必須のままにするか。** 「必須をやめて、厳しい姿勢のときだけ要求する」形なら、標準に寄りつつ目録の実利を残せる。**ただしこれは挙動変更であり UX 可視。**
3. **自動レビュアの段を持つか。** 4 種中 3 種が持つが、**門の経路に LLM が入る**ため、コスト軸と「レビュアを信頼する根拠は何か」という新しい問いが増える。**他の 2 点とは独立に決められる。**
4. **safe-mode python の honor-system をどうするか。** 標準に寄せる＝カーネル境界に寄せる、だが実装コストは 1〜3 と桁が違う。

## 6. 参照

- Reyn 側の仕様: [permission-model.md](../../../concepts/runtime/permission-model.md)
- 個別分析: [openclaw.md](openclaw.md) / [hermes-agent.md](hermes-agent.md) / [comparison.md](comparison.md)
