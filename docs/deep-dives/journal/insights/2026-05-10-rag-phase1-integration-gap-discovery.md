# 2026-05-10 — RAG Phase 1 integration gap: 「production grade」 judgment の撤回

> Batch 17 dogfood で ADR-0033 RAG-extensible OS Phase 1 (= 12 commits、 +131
> tests、 mkdocs strict、 e2e smoke 全 green) が **integration 経路で構造的に
> 未到達** と判明。 acceptance criteria の boxes ✓ が cross-layer wiring を
> 含まなかった、 という discipline-level の学びを記録する。

## 観測

「Phase 1 production grade で landed」 と self-declare した直後の dogfood で:

- 6 件 release-blocker bug surface (= CRITICAL × 2、 HIGH × 4)
- 2 件は ToolRegistry → build_tools → router dispatch の 3 layer wiring 漏れ
- 1 件は OS-layer の P4 candidate_outputs 設計 gap
- 残 3 件は HIGH 経路の prompt / state / config 漏れ

production user が `reyn chat` で recall を呼ぼうとしても、 tool が LLM 視界
不在で 0/5 invoke。 「framework foundation landed」 narrative が虚像。

## 因果分析

### 表層: acceptance criteria の boxes はすべて ✓ だった

ADR-0033 §6 で 12 件の acceptance criteria を立て、 Wave 1+2+3J landing で
すべて ✓ にした。 Tier 2 + Tier 3 test 2204 passed、 mkdocs strict pass、 直接
API smoke 完走。 私はこれで Phase 1 完了と判断した。

### 中層: layer 間 wiring が boxes に含まれなかった

ADR-0026 (= unified tool registry、 2026-05-09 Accepted) で 「ToolRegistry に
登録すれば router/phase 両 surface から dispatch される」 と設計した。 しかし
実装は **registry 登録 + 各 surface 側の inclusion list 追加の 2 step**。
Phase 4 で「registry 経由 dispatch 統一」 した時に、 build_tools (=
function calling injection 関数) と _REGISTRY_DISPATCH_TOOLS (= dispatch
allowlist) の 2 つを surface ごとの 「opt-in」 にしていた。

私が Wave 2F+G dispatch agent に与えた prompt は 「ToolDefinition 登録 +
get_default_registry に追加」 を要求したが、 build_tools への inclusion を
明示していなかった。 Agent は registry 登録のみで完了報告 → main agent も
boxes ✓ → integration 経路は実装されないまま release ready 判定。

### 深層: 「設計されている」 と 「動作している」 の混同

ADR-0026 が 「両 surface から dispatch」 を設計したことで、 **設計書ベースで
整合性を取った私が 「動作も整合する」 と暗黙仮定**。 実際は registry 経由
dispatch を完全に implementation する Phase 4 が部分的だった (= file/MCP は
registry 経由統一済、 router-side function calling tools は build_tools
legacy 経路温存)。

ADR の Phase 4 status を読み直すと、 「Registry 経由 dispatch 統一」 が部分的
であることは notes に記載されていたが、 私は high-level の 「Accepted +
Implemented」 status だけを見て judgment した。

## 教訓

### 教訓 1: acceptance criteria は cross-layer wiring を含めて initial design

ADR draft 段階で acceptance criteria を立てる時、 「X が登録される」 系の
boxes は **「下流の Y / Z layer から見える」 boxes と pair で書く**。

❌ Bad:
- [ ] recall ToolDefinition が ToolRegistry に登録される

✓ Good:
- [ ] recall ToolDefinition が ToolRegistry に登録される
- [ ] recall が `build_tools()` 出力に含まれる (= router LLM の tools= に injected)
- [ ] recall が `_REGISTRY_DISPATCH_TOOLS` に含まれる (= router_loop が dispatch する)
- [ ] recall を引数とした chat 1 turn で `tool_called` event が emit される (= integration smoke)

acceptance criteria boxes の数が増えるが、 **「✓ で release ready」 の確度が
劇的に向上**。

### 教訓 2: dogfood で structural bug と attractor を切り分ける discipline

batch 14 までの 9 原則 framework は LLM 行動 (= attractor) 中心。 batch 17 で
「OS structural gap」 が attractor prediction を妨害する pattern が surface。

R-RAG1 (= recall invoke 忘れ) を予測した。 0% invoke を観測。 「attractor
confirmed」 と結論したくなる。 しかし真の root cause は build_tools 欠落、
attractor 起こす機会が無かった。

**新原則 10** (= `feedback_*.md` 候補): dogfood で attractor を予測する前に、
「観測対象が LLM 視界 + OS dispatch 経路に存在するか」 の **structural pre-check**
を実施する。 prelude の R-attractor table に pre-check status 列追加で
operationalize。

### 教訓 3: 「設計が一貫している」 ≠ 「実装が動作している」

ADR-0026 のような大きな architectural decision を Accepted status にした時、
**Phase X 完了 = Phase X までの implementation が production-grade** という
高 expectation を build する。 後続 ADR (= 0033 RAG) を立てる時に この前提に
寄りかかると、 partial implementation の gap を継承する。

具体的には ADR-0026 Phase 4 が 「Registry 経由 dispatch 統一」 を coarse-name
ops (= file/mcp/run_skill) で完了したが router-side fine-grained function
calling tools (= web_search/recall/etc) は build_tools legacy 経路温存だった。
ADR-0033 を立てる時、 私は ADR-0026 の status だけを見て 「registry に登録すれば
両 surface から見える」 と仮定。 結果 Wave 2 prompt が wiring gap を放置。

→ **新規 ADR draft 時は「依存先 ADR の Phase 完了の精度」 を rebaseline**:
status = Accepted でも Phase 4 status を実装ベース で再確認する step を入れる。

### 教訓 4: end-to-end smoke の最低限定義

batch 17 直前に私が走らせた smoke は 「IndexBackend write/query/drop API direct
call + CLI list/describe/rm」。 これは layer 1+5 のみ test。 LLM 経路 (= layer 2:
chat → router → tool) を含まなかった。

→ **release ready judgment 前の minimum end-to-end smoke**:
- LLM-driven 1 turn で recall tool 呼ぶ (= layer 2 経路)
- LLM-driven 1 turn で indexing → query loop (= layer 2+3+4 経路)

これは LLM API key 必要、 fake provider で部分代替可能。 absence of OPENAI_API_KEY
は smoke skip の言い訳にはならない (= fake provider で wiring 経路は test 可能)。

## 改善 action

### 即時 (= batch 17 fix wave 内)

1. ADR-0033 Acceptance criteria に layer-wiring boxes 追加 (= retroactively)
2. fix wave A 着手: B17-S6-1 + S9-1 fix
3. fix wave B 着手: B17-S1-1 + S5-2/S8-1 + S8-3 fix
4. retest (= batch 18) で batch 14 水準復帰

### 短期 (= 1-2 wave)

5. dogfood discipline doc (= `docs/deep-dives/contributing/dogfood-discipline.md`)
   に **原則 10 (= structural pre-check)** 追加
6. ADR template (= `docs/deep-dives/decisions/README.md` の Format section) に
   acceptance criteria の cross-layer boxes 推奨追加
7. testing policy (= `docs/deep-dives/contributing/testing.md`) で 「Tier 4
   integration smoke」 の位置づけを議論 (= 既存 Tier 2 strict 化 vs 新 Tier
   設置)

### 中期 (= phase 2)

8. ADR-0026 Phase 4 の "Registry 経由 dispatch 統一" を完全完了 (= router-side
   build_tools の legacy 経路 sunset)、 これにより ToolDefinition 登録 = 全
   surface から見える pattern を保証
9. Tier 4 integration smoke を CI に組み込む (= release ready check の一部)

## 関連

- ADR-0026 unified tool registry: `docs/deep-dives/decisions/0026-unified-tool-registry.md`
- ADR-0033 RAG-extensible OS: `docs/deep-dives/decisions/0033-rag-extensible-os.md`
- Batch 17 findings: `docs/deep-dives/journal/dogfood/2026-05-10-batch-17-rag-phase-1/findings.md`
- Batch 17 retrospective: `docs/deep-dives/journal/dogfood/2026-05-10-batch-17-rag-phase-1/retrospective.md`
- Dogfood discipline: `docs/deep-dives/contributing/dogfood-discipline.md`
- Existing 9 principles framework (= principle 6 wrong-layer trap): batch 7-14
  retrospectives lift
