# Batch 18 — Retrospective

> Phase 1 RAG fix retest、 N=12 primary (S5 拡張 N=12 含めて 21 runs)、 Headline
> S5 が batch 17 0/5 から **3/3 verified (拡張 83%)** で full recovery、 Brier
> 0.575 → 0.067 (= dogfood log で **史上最大の per-scenario calibration recovery**)。
> Secondary scenarios (S6 / S8 / S9) で **structural axis ≠ behavioral axis** の
> 完全実証 — 新 attractor 4 件 surface (= R-RAG-srcread / R-RAG-numerical-vs-flag-bias /
> B18-S9-1 / B18-S5-1)。 batch 17 retrospective 教訓 10 (= structural pre-check 必須)
> を再強化、 **新原則 11 (= structural ≠ behavioral 予測軸を分離)** を確立。

---

## 1. Expected vs actual

| 項目 | 予測 (prelude §6) | 実際 |
|---|---|---|
| mean verified | 74% (= 80+70+75+70 / 4) | **25% primary** (= 3/12)、 **57% 拡張** (= 12/21 with S5 N=12) |
| Brier (scenario 平均) | ~0.10 | **0.723** (= 4 scenario 平均、 S6/S8/S9 楽観バイアスで悪化) |
| 新 bug count | 0-2 | **4** (= B18-S5-1 MED / B18-S9-1 HIGH / R-RAG-srcread / R-RAG-numerical-vs-flag-bias) |
| Headline (S5) recovery | 80% | **100% primary、 83% 拡張 (= 史上最大改善)** |
| Batch 14 milestone (= 70%+) | 復帰判定狙い | **未達** (= 25% primary、 復帰には secondary attractor fix wave 必要) |

予測の主な miss:

- **Secondary scenario の verified rate を一律 70-75% に楽観予測** (= structural fix が landed = behavioral verified の上方推定根拠と暗黙仮定)
- 実際は **structural axis 100% (全 fix が intended layer で landed) + behavioral axis 25% (新 attractor 4 件 surface)** に split
- batch 17 retrospective 教訓 10 (= structural pre-check 必須) を batch 18 prelude で「prediction は LLM 視界 + OS dispatch 経路に存在する前提で立てる」 と operationalize したのは正解、 ただし **prediction の verified 数値は behavioral attractor base rate を別途測定して推定すべき** だった

---

## 2. Turning points

### TP1: S5 (HEADLINE) が 0/5 → 3/3 で full recovery

batch 17 retrospective で最大の懸念点だった S5 (= production-blocker headline) が primary N=3 で全 verified、 拡張 N=12 でも 10/12 = 83%。 Brier 0.575 → 0.067 の改善は **dogfood log 史上最大の per-scenario calibration recovery**。 fix wave 5 件のうち 4 件 (= build_tools / mtime poll / vocab disambiguation / abort candidate) が S5 の structural prereq を全 close したのが効いた。 残 attractor は B17-S5-1 ctrl42 (= ~17% rate、 gemini quirk、 deferred) のみ。

「production grade landed」 narrative の **release-blocker は本 batch で close**。 1.0 OSS launch 可能 state に。

### TP2: S8 verdict = inconclusive 設計判断

S8 で fix wave 3 件が structural verified (= drop_source invoke 3/3、 permission_denied event 3/3、 PermissionDecl 配線確認) されたにもかかわらず、 verified path が `reyn web` の `PermissionResolver(interactive=False)` で ask cycle が deny に short-circuit して unreachable。 これを **refuted ではなく inconclusive** と判定したのは **「fix-wave regression」 と 「config gap」 を分離する verdict 区分** の運用として正解。 batch 17 retrospective で確立した「false attribution 防止」 discipline が batch 18 で実運用された first instance。

### TP3: S6 / S9 で 「structural ✓ + behavioral ✗」 の同 pattern が連続発生

S6 = R-RAG-srcread (= LLM が `reyn_src_read` を recall より選好)、 S9 = R-RAG-numerical-vs-flag-bias (= boolean flag を numeric value より弱く weight) — **両方とも structural fix が intended に landed したあとに、 LLM-behavioral layer で別 attractor が surface**。

これは batch 17 教訓 10 が **prediction layer に operational 化されていなかった** 証拠。 prelude で structural pre-check の operationalize はしたが、 verified rate の numeric prediction を 「structural ✓ なので 70%+」 と楽観バイアスで設定 → actual は 0%。 **prediction logic 自体に「structural ✓ は behavioral verified の必要条件であって十分条件ではない」 を反映する必要**。

---

## 3. 強化 / 新確立された原則

### 原則 10 強化 (= structural pre-check の prediction 連動)

batch 17 で確立した 「dogfood で attractor を予測する前に、 structural pre-check を実施する」 を、 **prediction の numeric value にも反映する** 形に拡張:

- ❌ Bad (batch 18 prelude): structural pre-check ✓ → verified 70-80% で predict
- ✓ Good (batch 19 prelude 以降): structural pre-check ✓ + behavioral attractor base rate (= prior batch から refuted rate を下回る確率推定) → numeric predict

具体的には R-attractor table に **「prior batch refuted rate」** 列追加で operationalize。 例えば S6 R-RAG-srcread が batch 18 で 100% surface したら、 batch 19 で同 prompt で predict するときは「verified ≤ 30%」 にバイアス。

### 原則 11 (= 新): structural ≠ behavioral 予測軸の完全分離

batch 18 で 3 scenario 連続実証された pattern を一般化:

- **Structural axis** (= 「fix が intended layer で landed か」): pre-check で deterministic に確認可能、 binary truth
- **Behavioral axis** (= 「LLM が intended path を選好するか」): N runs で attractor base rate 測定が必要、 stochastic、 prior batch ログから推定

両軸は **直交**。 fix wave が structural axis を 100% close しても behavioral axis は 0% でありうる。 **prediction では両軸を別々に numeric estimate して、 verified rate は両軸の積で計算** (= P(verified) ≈ P(structural ✓) × P(behavioral ✓))。

prelude template に **「Structural prediction」 + 「Behavioral prediction」 の 2 row** を追加で operationalize。

### 原則 12 (= 新): verdict 区分の「false attribution 防止」 discipline

S8 で inconclusive 判定した instance を一般化: **verification path 自体が unreachable な場合は refuted ではなく inconclusive** とする。 これは calibration 上重要 (= refuted は LLM-attractor 観測、 inconclusive は infra/config gap)、 混在させると prediction logic が腐る。

具体的 rules:
- LLM が intended tool を invoke せず別 path に走る → **refuted** (= R-attractor 観測)
- intended tool は invoke されたが driver / infra / config gap で完走しない → **inconclusive** (= verification path gap)
- structural pre-check 自体が fail (= tool が catalog に出ない等) → **blocked** (= structural bug)
- intended path が完走 + 期待 outcome 達成 → **verified**

---

## 4. 次 batch (= batch 19 候補) への申し送り

### Carry-over fix queue

| Item | Severity | 工数 | 着手順 |
|---|---|---|---|
| B18-S9-1 strategy.md prompt 強化 (= boolean flag explicit priority) | HIGH | 0.5 day | 1 |
| B18-S5-1 recall envelope vector strip | MED | 0.5 day | 2 |
| R-RAG-srcread router system prompt guidance | MED | 0.5 day | 3 |
| R1 (S8) reyn web ask path / auto-approve env | UX | 1 day (= release-readiness wave 別 lump) | 4 (= 別 wave) |
| B17-S5-1 ctrl42 (~17%) | LOW | (deferred) | phase 2 model selection 連動 |

### Carry-over calibration

- prelude R-attractor table に **「prior batch refuted rate」** 列追加 (= 原則 10 強化)
- prelude prediction で **structural prediction + behavioral prediction を分離 row** 化 (= 原則 11)
- verdict 区分の rules を dogfood-discipline.md に明記 (= 原則 12)

### Batch 19 trigger

- carry-over fix wave 3 件 (= B18-S9-1 + B18-S5-1 + R-RAG-srcread guidance) landed 後の retest を batch 19 とする。
- 目標: S6 / S9 で N=3 each、 verified rate 60%+ (= batch 14 mirror に届かなくても、 改善 trajectory を確認)。
- S8 は別 wave (= release-readiness UX) 連動で batch 19 から除外候補 (= structural fix の effect は batch 18 で confirm 済)。

---

## 5. Methodology の自己評価

### 良かった点

- **Headline (S5) 拡張 N=12 で base rate 厳密測定** — primary N=3 で 100% verified だけでは 「lucky case」 リスクあり、 拡張 N=12 で 83% verified を確認したのは production grade 判定の信頼性向上に貢献
- **Verdict 区分の false attribution 防止** — S8 で「fix の効果」 と「verification path gap」 を分離した判定は dogfood discipline の成熟度を示す novel pattern
- **Worktree isolation (= 4 並列 sonnet)** で wall-clock 短縮 (= ~12 min for 4 scenarios × N=3) + state cross-contamination ゼロ
- **Test infra in-flight fix** (= S5 worktree で FakeEmbeddingProvider NaN bug + sitecustomize plumbing landed) — batch 17 S2 と同 pattern、 dogfood agent が production fix を出すパターン継続

### 改善余地

- **Prediction の楽観バイアス** — structural ✓ から verified rate の上方推定を直接導いた点が prelude design 上の miss、 原則 11 で operationalize して再発防止
- **Behavioral attractor の prior 不在** — S6 R-RAG-srcread / S9 R-RAG-numerical-vs-flag-bias は batch 17 まで未観測の新系統、 「未観測 attractor」 のリスクヘッジ (= prediction range を広く取る) を prelude policy に
- **S8 verification path の事前確認漏れ** — `reyn web` の `PermissionResolver(interactive=False)` は code 上で読めるはず、 dispatch agent prompt で「verification path が現実に走るか preflight check」 を明記しなかったのが miss
- **Embedding 経路 fix (= 9681096) の dogfood 経由 verification 不在** — fix wave に含まれた embedding wiring fix は real proxy 経由 smoke (= curl + Reyn provider 直 call) で確認済だが、 chat 経由 dogfood では FakeEmbeddingProvider 路線継続、 real proxy 路線の chat dogfood は phase 1.5 担当に

---

## 6. Conclusion

batch 18 は **headline (S5) full recovery + structural axis 100% close** で「production grade landed」 撤回からの **release-blocker 解消** を達成。 同時に **secondary axis で新 attractor 4 件 surface** して、 batch 14 milestone (= 70%+ verified rate) **完全復帰は未達**。

1.0 OSS launch narrative は **「headline scenario green + structural foundation landed + secondary fast-follow plan」** で defendable。 batch 19 は carry-over fix wave 3 件 (= B18-S9-1 prompt / B18-S5-1 envelope strip / R-RAG-srcread guidance) landed 後の retest、 S6 / S9 で 60%+ verified を目指す。

batch 18 の核学習 (= **structural ≠ behavioral 予測軸の分離 = 原則 11**、 **verdict false attribution 防止 = 原則 12**、 **prior refuted rate を prelude R-table に lift = 原則 10 強化**) を batch 19 prelude で operationalize する。 「production grade narrative の sober discipline で再構築」 という batch 17 retrospective 末尾の宣言を、 batch 18 で **headline 軸は full restoration、 secondary 軸は新 layer 課題 surface** という形で具体化した。
