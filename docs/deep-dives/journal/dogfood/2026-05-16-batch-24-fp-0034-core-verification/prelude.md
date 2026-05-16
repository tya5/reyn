# Batch 24 — FP-0034 wrapper-only e2e core path verification (= N=3)

> Core path verification batch (= 原則 11 N=3 functional confirm)。 calibration framework 稼働後の Brier target 0.3-0.5。 fix dispatch は attractor surface 時のみ (= 原則 16 multi-agent context analysis 前提)、 surface なしなら B25 skip → B26 直接の judgment あり。

---

## 0. Carry-over from Batch 23

**Batch 23 outcomes (= practice / calibration baseline)**:
- S1 catalog discovery: **inconclusive** (= dual-intent prompt 構造で deterministic parallel dispatch、 真因 evidence-isolation via `llm_replay --patch` 7 hypotheses)
- S2 routing_decided emit: **verified** (= Phase 3 P6 audit trail 完全)
- S3 exec visibility: **inconclusive** (= 原則 12 補正、 prelude 想定 `sandbox.backend=noop` ≠ actual `auto`)
- Batch Brier **0.948** (= practice baseline 帯)

**Methodology 確立**:
- per-cwd + per-reyn-agent isolation (= dogfood-discipline §6.6 multi-shot pattern)
- `llm_replay.py --patch` を active debug loop に昇格 (= behavioral anomaly 観察時の standard)
- 原則 12 verdict 2-layer (= driver mechanism check / analyst intent check)

**carry-over for B24**:
- driver `dogfood_b24_driver.py` を S1A/S1B/S3-noop/S3-auto に拡張 (= pre-batch fix B2)
- prelude template に `Config defaults verified` row 追加 (= 原則 10 extension、 pre-batch fix B3)
- prompt-structure 軸を prediction model に統合 (= 本 prelude で実装)

---

## 1. Goal hierarchy

**Primary**:
- Core wrapper-only path を **N=3 で functional confirm** (= S1B / S2 / S3 mainline)
- **Brier 0.3-0.5** で calibration framework 稼働確認
- **Attractor base rate** measurement (= dual-intent / sequential / natural query での比較)

**Secondary**:
- S4 (= hot list cold start direct alias rate) baseline
- S5 (= search_actions semantic via natural query) trigger 条件確認

**Gates → B25 (= attractor fix wave) or B26 (= N=5 stability)**:
- attractor surface (= verified < 65%) → B25 fix wave 必要
- attractor 0% + verified ≥ 80% → B25 skip、 B26 直接 (= N=5 stability)
- 中間 (= 65-80%) → judgment call、 細部 finding を見て decide

---

## 2. Structural pre-check (= 原則 10、 config-default audit 含む)

### 2.1 Code state

| Item | Required | Verified by |
|---|---|---|
| FP-0034 Phase 1-3 landed | ✓ | `git log --grep "FP-0034" --oneline` |
| B23-PRE-1 SP refactor landed | ✓ | `python scripts/dogfood_sp_render.py --hide-legacy-tools --stats` = 2635 chars baseline |
| routing_decided event schema 登録 | ✓ | `grep "routing_decided" src/reyn/events/schemas.py` |
| Universal wrappers visible in tools= | ✓ | `dogfood_sp_render.py` で 4 wrappers 確認 |

### 2.2 Config defaults verified (= 原則 10 extension、 batch 23 lift)

prelude が assume する config 値は `python -c '...'` で actual 確認:

| Field | Prelude assumes | Actual | Action |
|---|---|---|---|
| `action_retrieval.hide_legacy_tools` | true | (verify via load_config) | reyn.local.yaml で固定済 |
| `action_retrieval.embedding_class` | standard | (verify) | reyn.local.yaml で固定済 |
| `sandbox.backend` | **noop** for S3-noop、 **auto** for S3-auto | (verify) | S3-noop は reyn.local.yaml に explicit override 必須 |
| `project_context_path` | unset for /tmp workspace | (verify) | /tmp dir には CLAUDE.md 不在のため自動 unset |
| `api_base` | localhost:4000 | (verify) | reyn.local.yaml で固定済 |
| LiteLLM proxy availability | healthy | `curl -s localhost:4000/health` | manual pre-check |

### 2.3 Driver + isolation pattern

| Item | Required | Verified by |
|---|---|---|
| `dogfood_b24_driver.py` 存在 | ✓ | (pre-batch fix B2 で land) |
| 7 scenarios in SCENARIOS dict | ✓ | `python scripts/dogfood_b24_driver.py --help` |
| `--agent-name` flag + auto-create | ✓ | (B23 driver から継承) |
| Per-cwd workspace `/tmp/reyn-b24-<sc>-r<N>/` | ✓ | sub-agent prompt template |

---

## 3. Scenarios + prompt-class taxonomy (= 原則 15 extension)

### Prompt structure 軸

batch 23 S1 で発見した **dual-intent AND-conjunction prompt 構造** の挙動を 2 baseline + 1 confound で測る:

| Class | Pattern | Predicted behavior |
|---|---|---|
| **P-explicit-AND** | 「(discovery), (execute)」 並列構造 | Parallel tool dispatch (= 100% per B23 evidence) |
| **P-explicit-SEQ** | 「(discovery). その後、 もし (execute)」 sequential | List_only then await (= 100% per H5 patch evidence) |
| **P-natural** | 「X についてありますか」 question form | Variable (= depends on action descriptor strength) |
| **P-natural-semantic** | 「X 関連のものを探したい」 search affordance | Trigger search_actions (= hypothesis、 measured here) |

### Scenarios (= N=3 per scenario、 isolated /tmp/ workspace)

#### S1A — P-explicit-AND parallel-tolerant (= verdict refinement)

- **Prompt**: 「 利用可能な skill の一覧を教えて、 その中から code_review を実行してください」 (= 同 B23 S1)
- **Prompt class**: P-explicit-AND
- **Expected path**: parallel list_actions + invoke_action (= deterministic per B23 evidence)
- **Driver verdict refinement**: parallel + error correctly surfaced in turn 2 = **verified** (= LLM の判断として valid path)
- **Structural pre-check**: ✓ (= B23 で確認済)

**4-outcome prediction (N=3 distribution)**:
- verified: 80% (= parallel-tolerant verdict、 error surface 確認)
- inconclusive: 10% (= error surface failure)
- refuted: 5% (= legacy tool 呼出 or hallucination)
- blocked: 5%

#### S1B — P-explicit-SEQ baseline

- **Prompt**: 「 利用可能な skill を確認してください。 その後、 もし code_review があれば実行してください」
- **Prompt class**: P-explicit-SEQ
- **Expected path**: Turn 1 list_actions only → Turn 2 (await result) describe_action or invoke_action or text reply
- **Structural pre-check**: ✓

**4-outcome prediction**:
- verified: 75% (= sequential dispatch、 next turn proper handling)
- inconclusive: 15% (= 並列に戻る、 stochastic minor)
- refuted: 5%
- blocked: 5%

#### S2 — routing_decided emit (N=3 stability)

- **Prompt**: 「 file__read を invoke_action で /etc/hostname に対して使ってください」 (= 同 B23 S2)
- **Prompt class**: P-explicit
- **Expected path**: invoke_action → routing_decided event → permission deny
- **Structural pre-check**: ✓

**4-outcome prediction**:
- verified: 85% (= B23 で N=1 verified、 N=3 安定性)
- inconclusive: 10%
- refuted: 0%
- blocked: 5% (= file permission system / litellm error)

#### S3-noop — exec gating empty variant (= B23 carry-over)

- **Prompt**: 「 sandboxed コマンド実行に使える action はありますか」
- **Pre-step**: `/tmp/reyn-b24-s3_noop-r<N>/reyn.local.yaml` に `sandbox: {backend: noop}` 追加
- **Expected path**: list_actions(category=['exec']) → empty result → LLM が 「無し」 と honest narrate
- **Structural pre-check**: `is_exec_available("noop")=False` verified via code

**4-outcome prediction**:
- verified: 75% (= empty result + honest acknowledgment)
- inconclusive: 15% (= hallucinate suggesting enable sandbox 等)
- refuted: 5% (= fake action 発明)
- blocked: 5%

#### S3-auto — exec describe path

- **Prompt**: 同 S3-noop
- **Config**: default `sandbox.backend=auto`
- **Expected path**: list_actions(category=['exec']) → 1 item → describe_action → narrate
- **Structural pre-check**: ✓

**4-outcome prediction**:
- verified: 85% (= B23 で N=1 確認済 pattern、 N=3 安定性)
- inconclusive: 10%
- refuted: 0%
- blocked: 5%

#### S4 — hot list cold start direct alias

- **Prompt**: 「 memory に何を覚えていますか」
- **Prompt class**: P-natural
- **Pre-state**: `.reyn/state/action_usage.jsonl` 空 (= freq=0、 default hot list seed のみ)
- **Expected path**: list_actions(category=['memory.entry']) OR hot alias `list_memory` 直接呼出 (= seed 含む場合)
- **Structural pre-check**: hot_list_seed=default で 5 universal + 5 Reyn flagship が候補

**4-outcome prediction**:
- verified: 60% (= cold start で direct alias 呼出 rate 低想定、 list_actions 経由が default)
- inconclusive: 25% (= LLM が直接 reply / 関連無 action 呼出)
- refuted: 5%
- blocked: 10%

#### S5 — search_actions semantic trigger

- **Prompt**: 「 現在使えるアクションの中から、 文字列処理関連のものを探したいです」
- **Prompt class**: P-natural-semantic
- **Expected path**: search_actions(query='文字列処理') → result inspect → narrate
- **Structural pre-check**: embedding_class=standard 設定済、 D14 gate 通過、 search_actions が tools= に visible (= `dogfood_sp_render --hide-legacy-tools` で確認)

**4-outcome prediction**:
- verified: 50% (= 「探す」 単語が search_actions の affordance と match、 ただし B-class affordance-bias で list_actions 選択も plausible)
- inconclusive: 30% (= list_actions(category=[]) 全件 or 部分一致 list)
- refuted: 10% (= invoke_action 直接、 関連無)
- blocked: 10%

---

## 4. Pre-execution checklist

- [ ] **Pre-batch fixes landed**:
  - [ ] B1: detect_attractor.py CLI doc sync
  - [ ] B2: scripts/dogfood_b24_driver.py with 7 scenarios
  - [ ] B3: dogfood-discipline.ja.md 原則 10 extension + 原則 12 自動化候補
- [ ] LiteLLM proxy healthy (`curl -s localhost:4000/health`)
- [ ] `python -c "from reyn.config import load_config; ..."` で config defaults 確認
- [ ] `reyn agent new b24_<scenario>_r<N>` で 全 21 agents (= 7 × N=3) pre-create
  - 或いは driver の auto-create に委ねる (= driver `--agent-name` flag で per-run agent name 指定)
- [ ] 5 sonnet sub-agents 並列 dispatch、 残 2 scenarios は 2nd wave

---

## 5. Expected outcome summary

| Scenario | Predicted verified % | Expected wall-clock |
|---|---|---|
| S1A (P-AND parallel-tolerant) | 80% | ~5-8s × 3 |
| S1B (P-SEQ baseline) | 75% | ~5-8s × 3 |
| S2 (routing_decided N=3) | 85% | ~5-8s × 3 |
| S3-noop (gating empty) | 75% | ~5-8s × 3 |
| S3-auto (describe path) | 85% | ~5-8s × 3 |
| S4 (hot cold start) | 60% | ~5-10s × 3 |
| S5 (search trigger) | 50% | ~5-10s × 3 |

**Batch verified avg (= rough)**: ~73% (= S1A/S2/S3-auto が pull-up、 S4/S5 が pull-down)

**Predicted batch Brier**: 0.3-0.5 (= calibration framework 稼働後の expected band)

**N**: 3 per scenario (= 21 LLM-driven invocations total)

**Wall-clock estimate**: 5 sonnet 並列 wave 1 (= 5 scenarios × N=3 sequential within agent) ~15-20 min、 wave 2 (= S4 + S5) ~10 min、 synthesis ~30 min = total **~1-1.5h**

---

## 6. Post-batch deliverables

- `findings.md` — per-scenario 4-outcome table、 attractor base rate (= dual-intent / SEQ / natural)、 per-finding severity
- `findings/B24-*.md` — HIGH/MED severity finding 別 file (= if any)
- `retrospective.md` — Expected vs actual / turning points / 原則 update / B25 or B26 申し送り

**Decision tree post-batch**:
- attractor surface (= verified < 65% in any scenario) → **B25 fix wave** (= 原則 16 pre-fix multi-agent context analysis)
- attractor 0% + S1A/S2/S3-auto/S1B 4 scenarios で verified ≥ 80% → **B25 skip、 B26 N=5 stability 直行**
- 中間 → judgment call、 individual finding 検討

---

## 7. Cross-references

- Batch 23 retrospective: `../2026-05-16-batch-23-fp-0034-practice/retrospective.md`
- Progression plan: `../fp-0034-progression.md`
- dogfood-discipline (原則 9 + extensions): `../../../contributing/dogfood-discipline.ja.md`
- Issue #36 (FP-0034): https://github.com/tya5/reyn/issues/36
