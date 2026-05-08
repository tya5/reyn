# Batch 6 (non-attractor focus) — Findings

> attractor を意図的に避けて非 attractor 系の課題に focus した batch。 fix で
> はなく観測 data 収集が主目的。 結果として:
>
> - **G12 attractor は 4 batch 連続再現** (B2 → B3 → B5 retest 2 → B6) — Wave 3
>   G4 trigger spike の決定的 motivation evidence
> - **B5-M1 (= 並列 invoke 3 件) は決定論的再現** — G3 dedupe (`9798372`) の
>   必要性が定量的裏付け
> - **B2-M2 (= 英語 fallback) と B4-M1 (= eval.md path mismatch) は両方未再現**
>   — それぞれ別 root cause (= LLM が tool 呼ばず直答 / target_skill_path
>   hallucination) が起きていた
> - **新規 HIGH 1 件 + MED 1 件 発見** — B6-S1-H1 (= stdlib skill path 補完
>   bug) + B6-S1-M1 (= validation 結果が LLM context 未到達)

---

## main HEAD と test count

- batch 6 開始時: `0660bb2`、 736 passed
- A3 並走中の Wave 4 fix: G3 `9798372` + G10 `af16228` landed
- A3 完了 (= S1-S5 観測終了): `fd852e5`、 743 passed (+7 = G3 +3 / G10 +4)
- post-S5 wave (eval_builder + B5-M2 + Tier 3 + infra fix 2 件): `f666acb`、 775 passed (+32 = +22 post-wave / 0 regression)

---

## 概要

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [B6-S1-H1](findings/B6-S1-observation.md) | HIGH | `prepare` LLM が stdlib skill (`direct_llm`) の path を `reyn/local/<name>/skill.md` に補完 → stdlib path resolution 欠落 | **resolved** by `e6de782` |
| [B6-S1-M1 仮説 (a) Tier 3](findings/B6-S1-M1-hypothesis-a-tier3-verify.md) | MED | `data.validation.ok` に基づく LLM 分岐を Tier 3 LLMReplay で behavioral pin | **verified (間接的)** `9763ecf` — regression guard 確立 |
| [B6-S1-M1 仮説 (a) retest](findings/B6-S1-M1-hypothesis-a-retest.md) | MED | dogfood retest で preprocessor 先行失敗 → LLM 未呼び出し → 仮説観測不能 | **inconclusive** — 新 infra bug 2 件 (G13/G14) 発見の起点 |
| [B6-INFRA-1](findings/B6-S1-M1-hypothesis-a-retest.md) | HIGH | `reyn chat` が `--allow-untrusted-python` フラグなし → trusted python step が config 設定に関わらず常に失敗 | **resolved** by `07ee851` |
| [B6-INFRA-2](findings/B6-S1-M1-hypothesis-a-retest.md) | HIGH | `Workspace.glob_files()` が stdlib path (absolute) を境界外として拒否 → `file.read: allow` でも bypass 不能 | **resolved** by `f666acb` |
| [B6-S2 G12 retest](findings/B6-S2-observation.md) | (G12 monitoring) | `describe→stop` attractor を 4 batch 連続再現 | G12 active、 Wave 3 G4 spike 動機 |
| [B6-S3 B5-M1 retest](findings/B6-S3-observation.md) | (= G3 evidence) | router 単一 LLM call から `invoke_skill` 3 件 155ms 以内発行 | **G3 fix** (`9798372`) 動機裏付け、 post-fix retest 次 batch |
| [B6-S4 B2-M2 不再現](findings/B6-S4-observation.md) | (= G10 evidence) | 不存在 skill 名で tool_failed 発火せず LLM が直接日本語 reply | G10 fix (`af16228`) は tool_failed path に正しく landing、 effective scope は要確認 |
| [B6-S5 B4-M1 不再現 + 新発見](findings/B6-S5-observation.md) | INFO + 新 root cause | eval.md path search 観測前に target_skill_path hallucination で abort | B4-M1 fix の前提条件として B6-S1-H1 hallucination fix が先 |

**S1-S5 観測 (A3) 時点の新規: HIGH 1 / MED 1** (= B6-S1-H1 / B6-S1-M1)。
**post-S5 wave での新規: HIGH 2** (= B6-INFRA-1 / B6-INFRA-2) — 両方同 session 内で resolved。

---

## ハイライト narrative

### G12 attractor の決定性が確定 — S2 で 4 batch 連続再現

Wave 1 で G12 を giveup tracker 化したのは「prompt rule 路線では完封できない、
真の解は強モデル併用 (G4 trigger)」 の判断。 batch 6 S2 の観測:

```
specialist RouterLoop:
  list_skills("read_local_files")  → ok (= B3-M2 fix で name lookup 機能)
  describe_skill("read_local_files") → ok
  agent_message_sent               ← invoke_skill 呼ばず空 reply
```

**4 batch 連続で `describe→stop` variant が再現** (B2-H1 → B3-H1 → B5R2-H1 →
B6-S2)。 `83bad83` の MUST rule が今も prompt にあるにも関わらず、 weak LLM
(gemini-2.5-flash-lite) が無視するパターンが決定論的に出る。

**Wave 3 G4 trigger spike の優先度を即上げる evidence**: 強モデルでこの attractor が
消えるかどうかが、 production model selection の意思決定材料。

### B5-M1 並列 invoke の決定的再現 — S3

S3 で `skill_improver` を invoke したところ、 router が **単一 LLM call から
3 件の `invoke_skill` を 155ms 以内に発行**。 batch 5 で観測した B5-M1 を
完全再現。 各 instance が独立に暴走 (= ask_user / copy_to_work 進行 / path
補完の 3 種が同時発生)。

**G3 dedupe (`9798372`) の必要性が定量的に裏付け**。 ただし本 batch では fix
**前** の HEAD (= worktree が `0660bb2`) で観測しているので、 G3 post-fix
retest は次 batch で必須。

### B2-M2 (英語 fallback) は別 root cause だった — S4

意図的に不存在 skill 名 (`nonexistent_skill_xyz123`) を投入。 期待は:
1. router が `invoke_skill(name="nonexistent_skill_xyz123")` を試みる
2. dispatch_tool が `tool_failed` event 発火
3. router が fallback reply を生成 → 英語で出る (B2-M2 再現)

実際:
1. router は `invoke_skill` を **呼ばず**、 LLM が直接 text reply で「`nonexistent_
   skill_xyz123` は存在しません」 と日本語で返した
2. tool_failed event 発火せず、 G10 fix (`af16228`) の経路は通らず
3. user に届いたのは日本語 reply、 B2-M2 (英語) 未再現

つまり **B2-M2 の root cause は tool_failed path ではなく LLM の判断ばらつき** で
あった可能性。 G10 fix は tool_failed path を deterministic 化したので正しい
方向の修正だが、 effective scope が想定より狭い。 LLM が直答する経路でも
日本語 reply は出るので、 user impact は B2-M2 ほど深刻ではないかもしれない。

### B4-M1 (eval.md path) の前提が崩れた — S5

S5 で skill_improver chain を回し eval.md path search を観測する目的。 結果:

- LLM が `direct_llm` (= stdlib skill) を **`my_app` という架空 skill に解釈**
- `prepare` phase が `reyn/local/my_app/eval.md` を 1 回試行 → ENOENT で abort
- B4-M1 で観測した「4 回 failed read」 のような path 探索 sequence は出ず

**新 root cause B6-S1-H1 (HIGH)**: stdlib skill path resolution の指示が
`prepare` instructions に欠落。 LLM が「stdlib skill か local skill か」 を
判別する instruction がなく、 すべての target を `reyn/local/<name>/...` で
解釈する。

**B4-M1 fix の前提条件**: B6-S1-H1 hallucination を先に塞がないと B4-M1 を
再現観測できない、 fix を設計できない。 「fix の dependency」 を tracker に
明示する形に variant の depth を表現する必要あり。

### G2 preprocessor 化の動作確認は partial — S1

G2 (`763c86c`) の e2e effectiveness を S1 で verify:

- ✅ `copy_to_work` preprocessor が **8 step 全完走** (= LLM call 0 で完了)
- ✗ glob 結果 0 matches (= target path が hallucinate された `reyn/local/my_app/`
  なので)
- ✗ workspace dir 未作成、 後続 eval cascade `FileNotFoundError`
- ✗ 改善案 user に届かず

**preprocessor 自体は構造的に正しく動いた**、 ただし上流 (= `prepare` phase の
LLM 判断) が誤った target path を渡したため preprocessor の出力も無効化。
これは G2 preprocessor 化の問題でなく、 **B6-S1-H1 hallucination の影響**。

副次 finding **B6-S1-M1 (MED)**: `_validation.ok=false` (= preprocessor の
validate step) でも LLM が「copied」 と判断して run_and_eval に遷移。
**preprocessor validation 結果が LLM context に注入されていない設計問題**。
P3 (OS = runtime engine) が gate すべき箇所で gate していない。

---

---

## B6-S1-M1 系 3 file の関係 — 「どれが結論?」 と迷わないために

B6-S1-M1 に関連する finding doc が 3 つある。 それぞれ別の問いに答えている:

| ファイル | 問い | 結論 |
|---|---|---|
| [B6-S1-M1-hypothesis-a-verify.md](findings/B6-S1-M1-hypothesis-a-verify.md) | 初回 dogfood で仮説 (a) を観測できたか? | **inconclusive** — prepare が copy_to_work に遷移できず LLM 未呼び出し (eval_builder fix 前の状態) |
| [B6-S1-M1-hypothesis-a-tier3-verify.md](findings/B6-S1-M1-hypothesis-a-tier3-verify.md) | `data.validation` rename は LLM judgment に有効か? | **verified (間接的)** — Tier 3 LLMReplay で `validation.ok` に基づく分岐を behavioral pin、 regression guard として確立 (`9763ecf`) |
| [B6-S1-M1-hypothesis-a-retest.md](findings/B6-S1-M1-hypothesis-a-retest.md) | fix landing 後の dogfood e2e で観測できたか? | **inconclusive** — 新 infra bug 2 件 (G13/G14) が先行 fail、 LLM 到達できず。 ただしインフラ gap 発見の起点となった |

**canonical regression guard は Tier 3 (tier3-verify.md)**。 実 LLM での最終確認は
batch 7 以降の retest 課題。 2 つの inconclusive は「仮説 (a) が誤り」ではなく、
「観測路が別の障壁で詰まった」事実を示している。

---

## post-S5 wave narrative (= A4 review 後の events)

### eval_builder D1+D2+D3a fix (Wave 1) — `e6de782`

A4 user review で「LLM に path を扱わせない」 方針が確定し、 eval_builder の
OS path resolution を preprocessor 経由に変更した。 `prepare` phase が
`target_skill_path` を自力で構築するのをやめ、 OS が解決した path を artifact
field として受け取る設計。 B6-S1-H1 の本質的修正。 +8 test。

### B5-M2 fix (Wave 2) — `0fd6d0b`

skill_improver `decide` turn の instructions を strengthen し、 H1/H2/H3 の
initial Control IR invalid 問題を解消。 Wave 1 と並列で landing。 +4 test。

### Tier 3 LLMReplay (Wave 3 前半) — `9763ecf`

`e6de782` / `0fd6d0b` landing 後、 B6-S1-M1 仮説 (a) の behavioral pin を
Tier 3 test として作成。 `copy_to_work` の `validation.ok=True/False` 各ケースの
LLM 分岐を hand-crafted fixture で pin。 2 test、 +2。

### dogfood retest (Wave 3 後半) — `07e16ca` (doc only)

Tier 3 verified の後、 実 LLM で e2e 確認を試みた。 **3 run とも LLM 到達前に
失敗**:

- chat run: `reyn chat` が trusted python を hard-fail (B6-INFRA-1)
- run mode 2 件: `Workspace.glob_files()` が stdlib path を境界外拒否 (B6-INFRA-2)

仮説 (a) の観測は inconclusive だが、 2 つの新規 infra bug を発見した。

### infra fix #1 (G13) — `07ee851`

`reyn chat --allow-untrusted-python` flag 追加。 `reyn run` との symmetry を確保。
`PermissionResolver` に `trusted_python_allowed` フラグが渡されるよう配線。 +4 test。

### infra fix #2 (G14) — `f666acb`

`Workspace.glob_files()` に `PermissionResolver` consultation 追加。 stdlib path
(= `base_dir` 外) への glob を permission 判断で opt-in できる設計に変更。 +4 test。

**この 2 件の landing をもって「chat 経由で skill_improver が動く前提が揃った」**
が batch 6 wave の最終 headline となった。

---

## prediction 精度 (= internal/user metric 分離評価)

| ID | Internal pred | Internal 結果 | User pred | User 結果 | 外れ予測該当 |
|---|---|---|---|---|---|
| S1 | 90% workspace dir 作成 | partial HIT (preprocessor 動作)/ MISS (workspace 未作成) | 70% 改善案届く | MISS (= 上流 hallucination) | (a) eval cascade 別 attractor 該当、 ただし真因は B6-S1-H1 |
| S2 | 30% IR op 発火 | MISS (= 0 件) | 20% prompt 届く | MISS | (c) G12 attractor で skill 起動せず — **完全的中** |
| S3 | 50% 並列再現 | HIT (= 3 並列確実) | n/a | n/a | 保守的すぎ、 100% 再現 |
| S4 | 70% tool_failed 経路 | MISS (= 0 件) | 50% 英語 reply | MISS (= 日本語) | LLM が tool 呼ばず text reply (= G12 family) |
| S5 | 80% 4 回 failed read | MISS (= 1 回で abort) | n/a | n/a | 新 attractor (B6-S1-H1 hallucination) で path search まで届かず |

精度: **方向当たり 1.5/5** (= S2 完全的中 + S3 保守的 HIT 0.5)。 過去の
batch (= 4/5、 3/5、 4/5、 1/2、 0/2) と比較すると低水準。

理由: 「fix できていない領域は再現する」 と仮定したが、 **LLM の判断ばらつき範囲が
予想より広い**。 同じ input でも別 attractor / 別 root cause で fail する
ケースが多発した。 **prediction の精度を上げるには、 LLM judgment の variance を
明示的にモデリング** する必要あり (= 例: 「30% A / 30% B / 40% C のどれか」 と
分布で記述)。

---

## attractor 発生 monitoring (G12 data 蓄積)

batch 6 で観測した G12 attractor:

| Scenario | Attractor variant | LLM model | turn at attractor | context length tokens |
|---|---|---|---|---|
| S1 | (= 起きず、 ただし上流 hallucination で別 fail) | flash-lite | n/a | ~5K |
| S2 | `describe→stop` | flash-lite | turn 3 | ~6K |
| S3 | (= 並列暴走、 attractor とは別軸) | flash-lite | turn 1 | ~3K |
| S4 | LLM tool skip → text reply (= 別 family、 G12 とは別) | flash-lite | turn 1 | ~2K |
| S5 | path hallucination → abort | flash-lite | turn 1 | ~4K |

→ G12 (`describe→stop`) は S2 で 1 件、 純粋 attractor 系の発生率は **5 中 1**。
ただし S1 / S5 の hallucination 系も「LLM 判断ばらつき」 起源で、 broad sense では同 family。

user 提言 (= scenario pattern を増やす) を踏まえ、 batch 7 で attractor mapping
schema を formalize する candidate。

---

## 結論

> batch 6 は fix data 収集 + G12 monitoring を完遂。 G3 / G10 fix は並走で
> landing、 G4 trigger spike の優先度が決定論的 evidence で確定。 ただし batch 6
> 中に **新 HIGH 1 件 (B6-S1-H1) + 新 MED 1 件 (B6-S1-M1)** が露呈し、 B4-M1
> fix の dependency (= hallucination 先行 fix 必要) が判明。

---

## 次のアクション

### 完了済 (= batch 6 wave 内で resolved)

- ✅ **B6-S1-H1 fix** (`e6de782`) — eval_builder OS path resolution
- ✅ **B5-M2 fix** (`0fd6d0b`) — skill_improver decide-turn instructions
- ✅ **B6-S1-M1 Tier 3 guard** (`9763ecf`) — regression guard 確立
- ✅ **B6-INFRA-1 fix** (`07ee851`) — reyn chat trusted python
- ✅ **B6-INFRA-2 fix** (`f666acb`) — workspace glob boundary

### 残件 (= batch 7 以降)

- **B6-S1-M1 dogfood retest** — infra fix 2 件 landing 後の e2e 観測 (仮説 a 最終確認)
- **B4-M1 fix** (= B6-S1-H1 fix landing 済みので前提条件解消): path convention ADR
  + eval_builder write と prepare read の path 一致
- **G3 / G10 post-fix retest** — fix 後の挙動確認 (本 batch は fix 前 HEAD での観測)

### 並走 (= 並列 OK)

- **Wave 3 G4 trigger spike** (= 強モデルで S2 と同 scenario を回す): G12
  attractor が消えるか定量化、 cost 上昇と ROI 評価 (proxy 整備 blocked)

### 次 batch (= batch 7) 設計候補

- attractor mapping schema を giveup tracker G12 に formalize (= scenario 別
  variant tag、 user 提言の方向)
- scenario 多様化軸 (= memory / eval / 3-agent chain / 非日本語 input / mid-session
  state) を意識
- G3 / G10 / B6-S1-H1 / B6-S1-M1 の post-fix retest を含める

---

## 関連 docs

- [scenarios.md](scenarios.md) — A1 + A2 で確定した 5 scenario
- [prelude.md](prelude.md) — batch 6 の前夜
- [retrospective.md](retrospective.md) — batch 6 全体 retrospective (dispatch wave 追記済)
- [findings/B6-S1-M1-hypothesis-a-verify.md](findings/B6-S1-M1-hypothesis-a-verify.md) — 仮説 (a) 初回 verify
- [findings/B6-S1-M1-hypothesis-a-tier3-verify.md](findings/B6-S1-M1-hypothesis-a-tier3-verify.md) — Tier 3 verified (canonical regression guard)
- [findings/B6-S1-M1-hypothesis-a-retest.md](findings/B6-S1-M1-hypothesis-a-retest.md) — dogfood retest (inconclusive + 新 infra bug 2 件)
- [giveup-tracker.md G12](../giveup-tracker.md) — attractor variant family + G13/G14 resolved
- [batch 5 retest 2 retrospective](../2026-05-04-batch-5-fix-verify/retrospective.md) — 直前 batch の教訓 (= G2 / G12 化判断)
- memory `feedback_deterministic_split.md` — G2 + G10 で実証された決定論分離思想
- memory `feedback_prompt_design.md` — bloat / consolidation の警告

---

## A4 review 結果 (= 決定済)

batch 6 wave 完走後の user review で以下が決定された:

1. **B6-S1-H1 fix** → batch 6 内で完了 (`e6de782`) — B6-S1-M1 Tier 3 も同 wave 内
2. **Wave 3 G4 trigger spike** → proxy 整備 blocked のまま。 整備後に即着手
3. **prediction 1.5/5** → 「LLM judgment のばらつき範囲が prediction モデルより広い」
   knowledge 獲得として解釈。 batch 7 で分布形式 prediction を試行候補
4. **attractor mapping schema** → G12 section で variant tag を formalize、 batch 7 で
5. **fix の dependency 明示** → tracker に「blocks / blocked-by」 記述追加の候補 (= B4-M1 → B6-S1-H1 依存解消済)
