---
title: shell 解釈の Cons に競合はどう対処しているか — Reyn への提案
last_updated: 2026-09-06
status: stable
sources:
  - url: https://code.claude.com/docs/en/permissions
    accessed: 2026-09-06
  - url: https://learn.chatgpt.com/docs/agent-approvals-security
    accessed: 2026-09-06
  - url: https://github.com/openai/codex/issues/28732
    accessed: 2026-09-06
  - url: https://docs.openclaw.ai/tools/exec
    accessed: 2026-09-06
  - url: https://docs.openclaw.ai/tools/exec-approvals
    accessed: 2026-09-06
  - url: https://hermes-agent.nousresearch.com/docs/user-guide/security
    accessed: 2026-09-06
---

# shell 解釈の Cons に競合はどう対処しているか

owner 依頼 (2026-09-06、#5838): 「各 Cons に対して競合はどう対処してるか調査して。reyn でどうするのが良いか提案して」。9 つの Cons は [#5838](https://github.com/tya5/reyn/issues/5838) に置いたもの。⚠️ **この doc は describing ＋ proposal で、裁定ではない。** 採否は #5838。

## 0. 先に結論 — 4 社が独立に同じ形に到達している

> **入力は shell 文字列、しかしポリシーは「文字列」にではなく「解析した実行計画」に掛ける。解析できないものは通さない（人に回す／拒否）。**

| | 入力 | ポリシーの単位 | 解析できないとき |
|---|---|---|---|
| **Claude Code** | 文字列（Bash tool） | **サブコマンドごと**。`&&` `\|\|` `;` `\|` `\|&` `&` 改行で分割し、**deny/ask は subshell・`$(…)`・`for` 本体の中まで**当てる。wrapper（`timeout`/`nice`…）と既知の安全な env 代入は剥がす。redirect の宛先は **Edit/Read ルール**で検査 | **プロンプト**（「Commands the analysis can't parse … asks for approval」、10,000 字超も） |
| **Codex** | 文字列（`bash -lc`） | **argv[0] の basename** を known-safe 集合と照合（`is_safe_to_call_with_exec`）。`used_complex_parsing` が立てば auto-approve しない | **承認へ**（`untrusted` は v0.149.0 で退役、`on-request` ＋ sandbox に統合） |
| **OpenClaw** | 文字列（sandbox 内は `sh -lc`） | **「enforceable execution plan」** — 各実行体の**解決済みパス**＋`argPattern`。allowlist mode では **chain/redirect は全 top-level segment が通らない限り拒否**。`python -c`/`node -e` は `strictInlineEval` | **人に回す**（heredoc・展開・未対応の wrapper quoting） |
| **Hermes** | 文字列 | **パターン**（curated dangerous patterns ＋ 上書き不能の hardline blocklist ＋ Tirith: pipe-to-interpreter / homograph）＋ `smart` の LLM 判定 | container backend なら検査自体を skip（境界に委ねる） |

**共通の判別子**: *shell を「構文」として受け、「意味」は argv 列に落としてから判断する*。誰も「文字列をそのまま shell に渡して sandbox 任せ」にはしていない（Hermes の container 時だけ例外で、それは境界が受け止めるという別の判断）。

## 1. Cons ごとの対処

| # | Con（#5838） | Claude Code | Codex | OpenClaw | Hermes |
|---|---|---|---|---|---|
| 1 | LLM 文字列 → injection | サブコマンド分割＋subshell/`$(…)` の中まで deny を当てる＋sandbox | known-safe 以外は承認＋sandbox（network 既定 off） | plan 化できないものは人へ＋sandbox | パターン＋blocklist＋container |
| 2 | 監査が binary を失う | サブコマンドごとの rule 保存（承認時に最大 5 rule に分解して記録） | — | **resolved executable path を pin**、承認は argv＋cwd に束縛 | — |
| 3 | threat scan の綴り爆発 | 解析側で wrapper/env 代入を正規化してから照合 | basename 正規化（副作用: `./sed`→`sed` の穴 #28732） | 「rebuild the complete command with those exact paths」＝正規化した plan に照合 | Tirith が pipe-to-interpreter 等の*形*を見る |
| 4 | hooks の first-token allowlist | （hooks は別機構） | — | 同じ resolved-binary＋argPattern を hooks にも | — |
| 5 | 非決定性（replay） | **誰も扱っていない** — replay/rewind を持つのは Reyn だけ | 同左 | 同左 | 同左 |
| 6 | どの shell か | ユーザの shell（Bash tool） | `bash -lc` 固定 | sandbox 内 `sh -lc`、host は `$SHELL`（fish は避けて bash/sh） | — |
| 7 | エラー帰属 | shell の exit code をそのまま | 同左 | 同左 | 同左 — **誰も改善していない** |
| 8 | binary 単位のポリシー | `Bash(git commit *)` 形の**サブコマンド prefix ルール** | known-safe **basename 集合** | **resolved path glob ＋ argPattern** | 危険パターンのみ（allow 側は無い） |
| 9 | LLM の引用ミス | 解析不能→プロンプト（＝黙って通さない） | `used_complex_parsing`→承認へ | plan 化不能→人へ | — |

**読み**: 1・3・8 は「解析して段に落とす」で全社が閉じている。2 は OpenClaw だけが構造的（resolved path を pin）。4 は OpenClaw の「同じ plan 機構を hooks に」が唯一の答え。**5 と 7 は誰も持っていない** — 5 は Reyn 固有の性質（replay）なので Reyn が自分で決めるしかない。

## 2. Reyn への提案 — 「shell は構文、argv は意味」

Reyn は既に **argv-only の実行プリミティブ**（`SandboxedExecIROp(argv)` → `run_sandboxed_exec` → backend、hooks も同じ backend）を持つ。競合が「文字列 → 解析 → 実行計画」で到達した場所に、Reyn は**実行側から**来ている。∴ 足すのは **shell の front-end 1 段**であって、プリミティブの置き換えではない。

### 形

```
"ls -la | grep foo > out.txt"
      │  parse（shlex ＋ 演算子 ＋ redirect）
      ▼
ExecPlan = [ segment(argv=["ls","-la"]), op="|", segment(argv=["grep","foo"]), redirect(">", "out.txt") ]
      │  policy: segment ごとに tool 軸(#5841)・threat scan・resolved argv[0]；redirect は file.write gate
      │  audit : plan を記録（各 segment の resolved argv[0] ＝ Con 2）
      ▼
実行: plan が backend で表現できるなら segment を argv で走らせる（pipe は backend 内で繋ぐ）
      表現できない（heredoc / `$(…)` / 展開 / 未対応 wrapper）→ **拒否**（LLM 経路）／**人へ**（`!` の人間経路）
```

- **解析可能な部分集合だけを受ける。** Codex/OpenClaw と同じく「plan にできないものは通さない」。`$(…)`・バッククォート・heredoc・glob 展開は**最初の版では拒否**（Con 1・3・5 をまとめて閉じる — 非決定性の源は展開なので、展開を受けなければ replay は argv と同じ決定性を保つ）。
- **redirect は file.write の gate を通す**（CC の形）。`> out.txt` は `require_file_write` の対象 — Reyn は file 軸を call-time で既に持っている（#5841 の tool 軸と違い、こちらは本番 8 箇所で効いている）。
- **segment ごとに tool 軸のポリシーと threat scan**（#5841 の call-time restrict を **segment 単位**で当てる）。Con 8 は「segment の resolved argv[0]」で表現できるので、#3227 の binary allowlist も**将来 作れる**まま残る。
- **audit は plan を記録**（Con 2）: `sandboxed_exec_started` に `plan=[...resolved argv0 per segment...]` を足す。`/bin/sh` で潰れない。
- **hooks は触らない**（Con 4）: hooks は operator 静的 config で LLM が author しない（#3226 の表）。plan 機構を hooks に広げるのは別判断。
- **shell を実際に起動しない**のが第一案（segment を backend で直接 pipe）。**どうしても `sh -c` が要る部分集合**（複雑な quoting）は **拒否**して、ユーザには「引用符で囲むか `/exec` の argv 形で」と返す（Con 6・7 を回避）。
- **LLM の引用ミス（Con 9）** は「解析不能→拒否＋理由」で黙って通さない（CC/Codex と同じ）。

### これで Cons はどうなるか

| # | 提案後 |
|---|---|
| 1 | 展開・置換を受けないので injection の主経路が無い。残る pipe/chain は segment ごとに policy |
| 2 | plan に resolved argv[0] が残る（`/bin/sh` にならない） |
| 3 | 正規化した segment に照合、綴りは argv と同じく 1 つ |
| 4 | hooks 不変 |
| 5 | 展開を受けない ∴ argv と同じ決定性。**Reyn 固有の Con を、部分集合の選び方で消す** |
| 6 | shell を起動しない（第一案）∴ 消える |
| 7 | segment ごとに exit code を持てる（pipeline の exit は segment 列で返す） |
| 8 | segment の argv[0] で表現可能 — #3227 は生きたまま |
| 9 | 解析不能は拒否＋理由。LLM の再試行コストは残る（競合と同じ） |

### 費用と順序

- 新規: `ExecPlan` の parser（shlex ベース、演算子 `| && || ;` と redirect `> >> <` のみ）、policy の segment 適用、backend の pipe 実行、audit の `plan` field。**プリミティブ・hooks・pipeline step の schema は不変。**
- **前提**: #5841（tool 軸の call-time restrict）— segment ごとの policy はそれに乗る。**#5825 ①（bounded の境界）は不要になる** — plan が展開を受けない限り、network の既定が open でも injection の主経路が無いので、#5838 A の「bounded と対」という条件は**外れる**。⚠️ これは私の読みで、#5838 で確認する。
- `!`/`!!`（#5837）はこの front-end を通すだけになり、`tokenize_exec_cmdline` は parser に置き換わる（1 関数の差し替え、設計どおり）。

### 採らない形

- **`sh -c` に文字列を渡して sandbox 任せ** — 4 社のどこもやっていない。Con 1〜8 がそのまま残る。
- **Hermes 型のパターン blocklist だけ** — allow 側の表現が無く、Con 8 を閉じない。#3227 を二度と作れなくする。
