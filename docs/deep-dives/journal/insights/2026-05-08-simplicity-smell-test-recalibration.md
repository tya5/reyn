---
title: 累積 fix で歪んだ system を、 user の plain-language probe が露呈した話 — simplicity smell test as recalibration mechanism
discovered: 2026-05-08
session-context: batch 13 (= 2026-05-06、 permission system 関連 4 fix landed 後) で user が「permission system を 2-3 文で説明できますか?」 と probe、 assistant の説明が「ただし...」 「特例で...」 を列挙する形になり documented design 違反 fix 2 件 (= `5/5` + `5/6` commit) が露呈、 revert (= `1408f42`、 `b92a22c`) で recalibrate。 真の resolution は dogfood pre-approval pattern (= `reyn.local.yaml` layer 3) で別経路解消
related-commits:
  - 651a053  # G15 fix (later reverted as design-violating)
  - 2219b20  # G21 partial fix (later reverted)
  - 59b57dc  # symmetric resolution attempt (reverted)
  - 96073cb  # startup_guard auto-approve (reverted)
  - 1408f42  # revert wave 1
  - b92a22c  # revert wave 2
related-giveup: [G15, G21]
related-memory: [feedback_minimize_speculation, feedback_verify_reproduce_first, feedback_observe_before_speculate_llm]
status: stable
---

# 累積 fix で歪んだ system を、 user の plain-language probe が露呈した話

> 「permission system を 2-3 文で説明できる?」 という user の単純な問いが、
> 累積 fix で歪んだ system の incoherence を露呈した話 — simplicity smell test
> as recalibration mechanism

## TL;DR

LLM-driven dev workflow で fix が累積すると、 各 fix が局所的に正しくても
**system が globally incoherent** になる pattern (= 「対称性が壊れた」
「documented design に書かれてない exception が増えた」 等)。 Reyn の batch 13
(= 2026-05-06) で permission 関連 4 fix landed 後、 user の **「permission
system を 2-3 文で説明できますか?」** という plain-language probe が、 自動
audit (= test pass / lint pass) が detect 不能だった incoherence を 1 質問で
露呈、 documented design 違反 fix 2 件 revert に至った。 **simplicity test は
誰がいつ trigger するか + user-assistant collaborative dynamic が core**。

## 教訓 headline

1. **automated audit と global coherence の axis 違い**: test pass / lint pass
   / mkdocs strict は **「locally correct」** しか保証しない。 **「globally
   coherent」** は別軸で probe しなければ detect 不能。
2. **non-implementer の plain-language probe が最強の incoherence detector**:
   implementer は fix の意図を覚えているので「説明可能」 と感じる、 fresh
   reader は意図不在で「対称性 / 例外 / 依存」 を素直に観察するため信号差が
   出る。
3. **simplicity probe が automated audit を上回る理由**: implementer bias は
   構造的 (= 自分の書いた fix は意図ある)、 「対称性が壊れた」 「documented
   design 違反」 を test code 化するのは general に困難。 plain-language
   probe は人間 reasoning で同 axis を直接 sample できる。
4. **simplicity test は accumulation 後の re-calibration mechanism**: 設計
   review の場 (= fix dispatch 前) でなく、 **fix 累積後** に system 全体の
   coherence を re-sample するための運用 audit。
5. **TP として記録する value**: 「simplicity test trigger された batch」 を
   後で grep 検索可能にすることで、 framework maturity の signal として活用。

## Background — Reyn permission system fix の累積

Reyn の permission system は `reyn.yaml` で workspace zone (= read / write
allowed paths) を declarative に定義。 dogfood で stdlib 経由の path access
が許可されない gap が連続発覚:

- batch 9 (= G15): eval_builder stdlib path read が default zone 外、 fix:
  `651a053` で「stdlib_root() を default zone に追加」
- batch 11 (= G21): copy_to_work CWD mismatch、 fix: `2219b20` + `59b57dc` で
  default zone 拡張 + symmetric path resolution
- batch 12 (= startup): startup_guard が初回起動時に prompt せず、 fix:
  `96073cb` で auto-approve

各 fix は単独 review で「locally 正しい」 (= 該当 scenario の test green、
mkdocs strict pass、 用例 1 件 demonstrably working)。 だが 4 fix が累積した
batch 13 始動時、 user が **「permission system を 2-3 文で説明できますか?」**
と probe。

## ステップ-by-ステップ narrative

### ステップ 1 — batch 9 G15 fix landing (= 2026-05-04)

eval_builder が stdlib 配下の skill manifest を読もうとして permission denied、
真因「stdlib_root() が default read zone 外」 と推測 → default zone 拡張 fix
として `651a053` landed。 単独 test green、 mkdocs strict pass、 documented
design audit は実施されず。

### ステップ 2 — batch 11 G21 fix landing (= 2026-05-05)

copy_to_work skill が CWD 由来の relative path で read fail、 G15 と同 pattern
として「default zone を CWD 周辺にも拡張」 fix `2219b20` + `59b57dc` landed。
test green、 strict pass。

### ステップ 3 — batch 12 startup auto-approve (= 2026-05-06)

startup_guard が prompt 不在で abort、 fix `96073cb` で「初回起動時 auto-approve」
追加。 ここで permission default zone が「stdlib + CWD 周辺 + auto-approve
exception」 の組合せ状態になる。

### ステップ 4 — batch 13 始動、 user probe 1

user 質問:

> 「permission system 簡潔に説明できますか? 」

assistant が試行:

> 「default zone は workspace + stdlib_root() + CWD 周辺、 ただし stdlib 配下
> は read のみ、 ただし auto-approve 経路では prompt skip、 例: ...」

user 再 probe:

> 「**それ documented design に書いてあったっけ?** 」

### ステップ 5 — documented design audit

`docs/deep-dives/decisions/` の permission ADR と照合:

- ADR 記載: 「default zone = `reyn.yaml` 宣言された path のみ。 stdlib_root()
  は別 layer 経由 (= layer 2: `reyn/local`) で declarable」
- 実装現状: G15 / G21 fix が `reyn.yaml` 不在でも stdlib + CWD 拡張、 ADR
  違反 silent introduction

= **fix 2 件 (= `651a053`、 `59b57dc`) が documented design 違反**。

### ステップ 6 — revert + 真の resolution

batch 13 で revert wave (= `1408f42`、 `b92a22c`)、 真の resolution は
**dogfood pre-approval pattern**:

- `reyn.local.yaml` (= layer 3 mechanism、 ADR 記載済) で dogfood scenario
  毎に必要 path を user explicit declaration
- default zone 拡張ではなく **user explicit decl 経由** で同 access を許可

resolution 後、 simplicity test 再実施で「permission system は `reyn.yaml`
+ `reyn/local` + `reyn.local.yaml` の 3 layer、 default zone は `reyn.yaml`
宣言のみ」 と 2 文で説明可能に recalibrate。

## Universal lessons

### lesson 1 — automated audit と global coherence の axis 違い

test green / lint pass / mkdocs strict はそれぞれ「実装 leaf level の locally
correct」 を verify。 「default zone の semantic 一貫性」 「documented design
との整合」 は **axis が直交**。 automated audit を増やしても「対称性が壊れた」
の detection は実現困難 (= test 化に必要な reference behavior が constantly
shift)。

### lesson 2 — implementer bias の構造

fix を書いた implementer は意図 / 履歴 / 推論経路を抱えており、 「説明可能」
と感じる threshold が低い。 fresh reader は履歴 zero、 「default zone に
stdlib_root() が含まれる」 を見て「なぜ?」 と即 probe する。 この信号差は
**人間 cognition の構造的 bias** であり、 process で対処するしかない。

### lesson 3 — simplicity test の trigger 条件

3 phase で trigger 価値が高い:

- **fix accumulation 後** (= 同 system area に 3+ fix landed): 各 fix の
  局所合理性が global coherence を侵食する rate が経験的に上昇
- **milestone 直前** (= release / branch merge): coherence の baseline
  re-establish タイミング
- **audit wave 開始時** (= 大規模 review / refactor 検討): probe 結果が
  scope 判断の input

### lesson 4 — probe の format

3 phrasing が effective:

- 「**2-3 文で説明して**」: 説明文長で「ただし...」 列挙を抑制、 例外蓄積
  の signal を露呈
- 「**実装読まずに**」: implementer bias を強制的に排除、 documented design
  を唯一の source に
- 「**documented design (= ADR) との整合は?**」: 実装と doc の drift を
  直接 probe

### lesson 5 — TP として記録する value

retrospective の TP (= turning point) section に simplicity test trigger を
verbatim 記録することで:

- 後 session で「simplicity test trigger された batch」 を grep 検索可能
- framework maturity metric (= simplicity test 頻度 / detected incoherence
  rate) として後分析可能
- next contributor が「同 pattern を踏みかけたか」 self-check できる

### lesson 6 — simplicity test の限界

全能でない:

- **probe area 依存**: 質問者が触れた area しか probe されない、 unprobed
  area の incoherence は detect されない
- **質問者の知識依存**: ADR 内容を知らない probe は「説明あった」 で通過
- **probe rate 制約**: user 1 人の attention は有限、 system 全 area の
  rotational probe schedule が必要

## Methodology checklist

operational steps:

- [ ] 大きい batch (= fix wave / milestone) の前後で user / non-implementer
      に「<system area> を 2-3 文で説明して」 probe を依頼
- [ ] 実装内部を見ずに plain language で説明可能か self-test (= implementer
      自身でも一定の test 効果)
- [ ] 「ただし...」 「特例で...」 「例: ...」 の expression 出現で alert (=
      例外蓄積の signal)
- [ ] documented design (= ADR / spec doc) と説明結果の整合性確認、 不整合
      あれば fix が doc 違反の可能性
- [ ] 違反検出時、 「fix を残して doc 更新」 vs 「fix を revert」 を意図的
      に判断 (= revert as first-class fix discipline と接続)
- [ ] retrospective の TP section に simplicity test の internal を記録 (=
      verbatim 質問 + 検出された incoherence + resolution path)

## Reyn-specific tooling reference

- `docs/deep-dives/decisions/` (= ADR) との整合性 audit が simplicity test
  の verification 半身
- `docs/deep-dives/contributing/dogfood-discipline.md` Principle 9 (=
  simplicity smell test) と co-reference、 本 insight が case study 半身
- retrospective の TP1-TPN section format が verbatim 記録の vehicle

## 適用可能性 / 不適用条件

**適用**:

- LLM-driven dev (= 実装速度 vs 設計理解の gap が大きい)
- fix が累積する system (= incremental dev workflow)
- documented design (= ADR / spec) を持つ project (= probe の reference 軸が
  存在)

**不適用**:

- green field (= まだ design 確定していない、 probe しても reference 不在)
- single-implementer toy project (= probe 相手が同 implementer、 bias 排除
  不能)
- documentation-driven 設計でない project (= probe しても「doc がない」 で
  stop)

## 既存 insight / process との差別化

本 insight は [r1-false-alarm-observe-first-saved](2026-05-08-r1-false-alarm-observe-first-saved.md)
(= LLM trace dump 経由で observe-first discipline) の **補完**:

| axis | r1-false-alarm | 本 insight |
|---|---|---|
| detect 対象 | LLM 挙動 vs plumbing artifact | system coherence vs locally-correct fix accumulation |
| 検出経路 | trace dump + dogfood_trace + llm_replay (= 機械観測) | user plain-language probe (= human reasoning) |
| trigger 主体 | implementer (= observe-first discipline 適用) | non-implementer (= user / collaborator) |
| 失敗 mode | speculation stack 暴走 | exception accumulation silent drift |

`docs/deep-dives/contributing/dogfood-discipline.md` Principle 9 (= simplicity
smell test) は **framework 側**、 本 insight は **case study 側**。

## References

### 関連 insight
- [r1-false-alarm-observe-first-saved](2026-05-08-r1-false-alarm-observe-first-saved.md)
  (= observe-first discipline、 機械観測経路、 simplicity probe と補完関係)
- [phased-migration-risk-reduction](2026-05-08-phased-migration-risk-reduction.md)
  (= 直前 commit、 phasing で risk localize、 simplicity test と orthogonal な
  運用 discipline)

### 関連 process docs
- `docs/deep-dives/contributing/dogfood-discipline.md` Principle 9 (= simplicity
  smell test の framework 側、 本 insight が case study 側)
- `docs/deep-dives/decisions/` 配下の permission 関連 ADR (= documented design
  reference)

### 関連 batch retros
- `docs/deep-dives/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`
  (= main case study source、 TP1+TP2 で simplicity test verbatim)
- batch 7 / 14 retro の TP section (= 補助 evidence、 simplicity probe 別 area
  trigger 例)

### 関連 memory
- `feedback_minimize_speculation.md` (= 1 仮説 1 修正 1 検証、 fix 累積で
  bundle 仮説を防ぐ意味で接続)
- `feedback_verify_reproduce_first.md` (= verify-first / reproduce-first、
  documented design 整合 audit が「verify-first」 の half)
- `feedback_observe_before_speculate_llm.md` (= 観測 infra 先、 plain-language
  probe を「人間観測 infra」 として位置付け可能)
