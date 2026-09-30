# 03 - Answer Quality V3 开发计划（可执行版）

> **修订三**。修订一 → 修订二：一次对 `main` 的 `RetrievalController` / `EvidencePool` / `ContextBuilder` / `AgenticAnswerResult` / L2 runner / `Settings` 的逐行复核，**Phase 1 的架构被推翻重做**并新增三处数据契约。修订二 → 修订三：补入四条执行约束（Workspace 生命周期、Coverage 批量 LLM、`RetrievalState`/`EMPTY` 迁移、Teach dispatch 与 Depth 契约），其中前两条修正了修订二的写法。
>
> G1–G5 归因、四层内容语义、Golden Case、四 Commit、Release Gate、范围收敛全部保留。
>
> - 基线提交：`5d2d9f6 fix: correct second-round retrieval evaluation`（已验证为当前 `HEAD`）
> - 索引仓库：`D:\Java-learning\12306Project\12306\my12306`（`src/devcontext/config.py:17`）
> - 对比原始文件：`C:\Users\Administrator\Desktop\txt文件\余票桶\当前项目回答.txt` 与 `同目录\chat gpt余票桶回答.txt`
> - 路径提示：V3 目录名用**半角**逗号，V1/V2 用全角，路径匹配时注意

## 修订二变更摘要

| # | 变化 | 性质 |
|---|---|---|
| 1 | **Phase 1 推翻**：EvidenceWorkspace 的冻结点上移到 `EvidencePool` 之后，不能在 `EvidencePackage` 之后重建 | 架构级阻塞，必须修 |
| 2 | 新增 §3.5「证据承载链路的重新界定」，重画真实链路 | 由 #1 引出 |
| 3 | 新增 requirement-scoped `CoverageView`，把 Coverage 与 Answer Context 解耦 | 新增 P0 |
| 4 | 新增 `CitationRegistry`，全局稳定证据身份；`CITATION_PATTERN` 需放宽 | 新增 P0 |
| 5 | 新增 `TeachingAnswerResult` 契约（采用**扩展现有类型**而非新建类型） | 新增 P0 |
| 6 | 新增 `TokenEstimator` 抽象 + 估算/实测偏差回填 | 新增 P0 |
| 7 | L2 runner 泛化为 baseline/candidate 两臂；benchmark schema 先向后兼容再加字段 | 顺序修正 |
| 8 | G4 验收从「证据外推演」改为「带 assumptions 的条件推演」 | 正确性修正 |
| 9 | Reviewer 门控改为三档，不再「所有 teach 路径都跑 LLM Reviewer」 | 成本/风险修正 |
| 10 | 基线 manifest 落到 `benchmark/baselines/`（已跟踪），raw 仍放 `artifacts/`（被 ignore） | 可复现性修正 |
| 11 | Phase 1 拆成 1A（Workspace + Registry）与 1B（Views + Token） | 可执行性 |

修订三补入的四条执行约束：

| # | 变化 | 性质 |
|---|---|---|
| 12 | **Workspace 生命周期精确定义**：每轮 Retrieval 后从当前 `EvidencePool` 构造 Coverage 快照；`CitationRegistry` 增量注册、绝不重编号；全部检索结束后 `freeze()` 成 `evidence_catalog`。修正修订二"只在第一轮之后构造一次"的写法 | 修正 §3.5.4、§6 |
| 13 | **Coverage 保持单次批量 LLM**：Requirement-scoped 的是 Evidence View，不是 LLM call。禁止拆成每 ER 一次调用（最多 6 倍成本） | 修正 §7.1 |
| 14 | **`RetrievalState` / `EMPTY` 迁移**：teach 路径的 evidence existence 以 Workspace 为准，同步修改 `_answer()` 的提前退出条件 | 新增 P0，§7.3 |
| 15 | **Teach dispatch 与 Depth 契约**：`deep` 仅允许 `teach`；`EvidenceDrivenWorkflow` 显式三分支分发，未知模式不得静默落到 explain；Teach 拥有独立 client policy | 新增 P0，§8.5 |

---

## 0. 先决条件：当前工作区需要先提交

执行任何代码改动**之前**必须处理这一项，否则四个 commit 的 diff 会与文档搬家混在一起，无法审阅。

当前 `git status`（已验证）：

```
 D docs/后续开发规划/01-项目开发方向转型.md          （共 9 个）
 D docs/项目规划文档/P0-...p9-*.md                  （共 10 个）
?? docs/V1-项目规划文档，基础搭建/
?? docs/V2-后续开发规划，检索优化/
?? docs/V3-后续高级功能规划,全面提升/
```

V1/V2/V3 三目录重组已经做完但从未提交。**先单独提交这次重组**，commit message 建议 `docs: regroup planning documents into V1/V2/V3`。

---

## 1. 为什么做这件事（Context）

DevContext 的检索侧已经闭环：Evidence Planner → SearchAction → 两轮 Retrieval → CoverageChecker → EvidencePackage，配套 L1 / L1.5 评测，最近六个提交全在这个范围。

但用户侧产出没有跟上。同一个"余票桶"问题，本项目 `explain` 输出 64 行、7 节、零代码块；ChatGPT 输出 1709 行、24 节。差距**不在事实准确性**——恰恰相反，本项目那 64 行几乎每句都锚在真实标识符上，而 ChatGPT 那篇没有出现一个真实类名。

所以问题不是"检索不到"，而是"检索到了，但讲不懂"。本轮全部工作发生在 `EvidencePackage` 之后——**但修订二明确了这条边界线本身需要移动**，见 §3.5。

---

## 2. 差距的实测归因（保留自修订一）

### 2.1 对比口径

| | 本项目回答 | ChatGPT 回答 |
|---|---|---|
| 来源 | `uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode explain` | 同一问题，附带 D2 文档上下文 |
| 体量 | 64 行（正文 46 行） | 1709 行 |
| 章节 | 7 节，全部名词短语标题 | 24 节，其中 11 节标题本身是"为什么…" |
| 代码块 | 0 | 每节 3–8 个，含 ASCII 流程图 |
| 类比 | 0 | "游乐园发号码牌"等 |
| 边界反例 | 2 处 | 8 处 |
| 证据缺口 | 独立第七节 | 无此节 |
| 真实标识符 | 约 30 个 | 0 个 |

### 2.2 五条归因，每条对应一处代码机制

**G1｜叙事起点是结论，不是问题。** 本项目第一节是"定位与命名：它是一道购票准入闸门"。ChatGPT 先用"10 张票 / 50 个请求 / 其中 40 个注定失败却仍要抢锁、占线程、占连接、开事务"建立矛盾，再问"为什么这 40 个请求要走到数据库才知道失败"。
→ 机制：`AnswerPlan.sections`（`answer/models.py:53-76`）由 Evidence Requirement 合并而来，`ANSWER_PLANNER_SYSTEM_PROMPT`（`answer/planner.py:28-34`）只要求"把调查项合并为读者容易理解的章节"，系统里**不存在"先建立问题"的章节类型**。

**G2｜没有贯穿全文的心智模型。** ChatGPT 反复回到"数据库 `t_seat` 是真实库存，Redis 只是'允许多少请求继续往下抢票'的近似凭证"。本项目有等价表述，但只出现在第一节正文里。
→ 机制：`AnswerPlan` 无 `core_mental_model` 字段；`generate_explained_draft`（`answer/generator.py:90`）只收到 `sections` 列表。

**G3｜系统在优化"覆盖"，不在优化"解释"。** `evaluation/answer_quality_runner.py:311-361` 的六项验收里，内容相关的只有 `detailed_in_2200_5000_at_least_80pct`（字数）与人工核对的 `must_cover`，**没有任何 why 覆盖率指标**。
→ 机制：评测口径决定产出形态。即使把 prompt 写得再教学化，只要验收还是"字数 + 事实覆盖"，模型就会退回罗列。

**G4｜被禁止讲证据里没有的东西——而 ChatGPT 最出彩的部分恰恰来自那里。** ChatGPT 第十八、十九节（"桶过期后 `HINCRBY` 自动重建残缺桶""`EXISTS`+`HINCRBY` 仍有 TTL 竞态"）在证据里根本不存在，是纯推理产物。
→ 机制：`SYSTEM_PROMPT`（`answer/generator.py:34-42`）"不得使用模型记忆、常识或猜测补充 Context 中没有出现的项目实现细节"。这条同时挡住幻觉与推演。**本轮最大取舍**。

**G5｜事实上的自信与声明的证据不足相矛盾。** CLI 打出 `Sufficiency: insufficient` / `Retries: 1`，正文却通篇确定语气。
→ 机制：`AnswerPlan` 无 confidence 字段；Reviewer 门控（`agentic/evidence_workflow.py:215-224`）只在 `detailed`、或 `standard` 且出现冲突/零引用/异常时触发——**`brief` 从不审稿**。

### 2.3 本项目有两处比 ChatGPT 更准，必须保住

**已对 my12306 真实源码独立核对**：`services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/handler/ticket/tokenbucket/TicketAvailabilityTokenBucket.java`

1. **`loadBucket` 的触发判据。** ChatGPT 说"Lua HGET 发现 bucket 不存在"；本项目说"第一步是字段级缺失判定，因为 Key 存在不代表字段完整"。**本项目正确**——该文件 `:133-137` 的注释原文即"Key 存在不代表字段完整；只要本次请求涉及的任一席别缺失，就重新装载完整桶"，实现是 `hMultiGet` + `allNonNull`。
2. **整桶补齐的口径。** ChatGPT 说"缺失席别补 0"；本项目说"按车型支持的**全部**席别补齐"。**本项目正确**——该文件 `:161-169` 取 `VehicleTypeEnum.findSeatTypesByCode(train.getTrainType())` 遍历全部席别，注释原文"按车型支持的全部席别补 0，避免售完席别永久触发'field 缺失'重装"。

顺带核实的第三处：`normalize(Map<Integer,Integer>)`（`:190-198`）用 `TreeMap` 合并重复席别、过滤 `count <= 0`——这条被 DevContext 答案列为"证据缺口"，实际代码里是明确可读的。

**结论：本轮所有教学化改动都不得削弱这条可追溯性。** Release Gate 里有一条硬门槛专门守它，判据已写进 `benchmark/l2-answer-quality.jsonl` 的 `teach-token-bucket-01.must_not_claim`（含上述行号）。

---

## 3. 两文档冲突的裁决（保留自修订一）

### 3.1 范围：以 02 第 65 节为准

01 的"五天计划"把 Symbol Graph / Repo Map / LangGraph Tool Agent / Web UI / 增量索引全排进去；02 第 65 节明确"**本轮明确不借**"这五项。**裁决：本轮只做 02 的 Commit 1–4，01 的五天计划降级为路线图。** 理由见 02 第 74 节：只有下层检索正确、上层解释清楚之后，再接入 `CALLS / REFERENCES / IMPLEMENTS / EXTENDS` 才能转化为答案质量。

### 3.2 内容分层：四层（02 第 6 节）

01 第 16 节是三层，02 第 6 节与 `ClaimPlan.claim_type` 是四层。**裁决：用四层**——`PROJECT_FACT / PROJECT_INFERENCE / GENERAL_CONCEPT / ILLUSTRATIVE_EXAMPLE`。

### 3.3 路径漂移：修正

02 第 0 节把 V2 两份文档写作 `docs/后续开发规划/08-...`、`09-...`。实际在 `docs/V2-后续开发规划，检索优化/`。本文档以实际路径为准。

### 3.4 修订一的一处边界被推翻

修订一写着"本轮全部工作都发生在 `EvidencePackage` 之后"。**这条太严格，必须改**，原因见 §3.5。修订二的表述是：

> **本轮不修改 Keyword / Vector / RRF / RetrievalPolicy / SearchAction 的检索算法与语义，但允许修改 `EvidencePool → Context → Coverage → EvidencePackage` 的证据承载方式**，以解除固定 Context 对后续能力的限制。

这不是重新优化 Retrieval，而是改 retrieval result 的生命周期。

---

## 3.5 证据承载链路的重新界定（修订二核心）

### 3.5.1 修订一的架构错误

修订一写的是：

```
RetrievalController → EvidencePackage → EvidenceWorkspace → ExplanationPlanner
```

并称"在 `agentic/evidence_workflow.py:run` 之后新增 Workspace 构造点"。

**这个顺序做不到目的。** 真实链路（已逐行核对）：

```
RetrievalController.retrieve()                          # retrieval_controller.py:74
  pool = EvidencePool()                                 # :91   完整候选在这里，是局部变量
  pool.add_many(...)                                    # :232
  _build_context(request, plan, pool, top_k)            # :126, :179
    └─ pool.select(plan.requirements, top_k)            # :252  ← 第一道有损闸门（按 top_k 截）
    └─ ContextBuilder(max_chars=budget).build(...)      # :254  ← 第二道有损闸门（截断 + break）
  coverage_checker.check(plan.requirements, bundle)     # :130, :183  ← 只看到已截断的 bundle
  EvidencePackage(..., bundle, ...)                     # :199-208     只带 bundle，不带 pool
```

`EvidencePackage` 的九个字段（`agentic/evidence_models.py:88`）是 `original_query / evidence_plan / context_bundle / requirement_coverage / unresolved_requirements / retrieval_state / search_history / coverage_rounds`——**没有完整证据池**。

所以若 Workspace 建在 `EvidencePackage` 之后，它包装的只是已经被截断两次的 `ContextBundle`：

```
28000 chars ContextBundle
        ↓
  EvidenceWorkspace        ← 看起来升级了，瓶颈没解除
```

### 3.5.2 比批评指出的还严重两处

复核时发现两个批评未提及、但同样致命的问题：

**(a) 有损闸门有两道，不是一道。** 批评只点出 `ContextBuilder` 的 `break`。实际上 `_build_context` 在进 `ContextBuilder` 之前先调了 `pool.select(plan.requirements, top_k)`（`retrieval_controller.py:252`），`select` 内部在 `len(selected) >= max_results` 时就停止收候选（`evidence.py:91-92, 148-149`）。也就是说 top_k 先砍一刀，字符预算再砍一刀。**Workspace 必须同时越过这两道。**

**(b) 第二轮的查询扩展也读被截断的 bundle。** `_discovered_terms(bundle, followup_requirements)`（`retrieval_controller.py:159`）从 `bundle.items` 里提取类名 / 方法名 / heading 喂回第二轮 SearchAction。截断丢掉的 identifer，第二轮也拿不到。**这意味着截断不只是损失回答素材，还在悄悄降低第二轮检索质量**——这正好解释了为什么 `Sufficiency: insufficient` 与 `Retries: 1` 同时出现却仍能写出大量真实标识符：第二轮补回来了一部分，但补的是"第一轮截断后还能看见的种子"能找回的那部分。

**(c) 副产物：Coverage 的 `MISSING` 可能是假阴性。** CoverageChecker 判定 `ER5 = MISSING` 时，事实可能是"检索到了 E 条，但 ContextBuilder 没让 CoverageChecker 看见"。这会误触发第二轮检索，并让最终 `Sufficiency` 偏低。**修订一完全漏掉了这一条。**

### 3.5.3 修订后的链路

```
EvidencePlanner → SearchActionPlanner → RetrievalPolicy
                                              │
                                    EvidencePool（每轮追加）
                                              │
                    ┌─────────────────────────┴────────────────────────┐
                    ▼                                                  ▼
        EvidenceWorkspace + CitationRegistry                 Legacy ContextBuilder
        （完整证据，增量注册，绝不重编号）                       （8K/16K/28K，保留）
                    │                                                  ▼
      ┌─────────────┼──────────────┐                             ContextBundle
      ▼             ▼              ▼                                   │
CoverageView   PlannerView   SectionView                               ▼
（当轮快照）          │              │                            Legacy Coverage
      │              └──────────────┘                                   │
      ▼                                                               ▼
CoverageChecker（单次批量 LLM）                                explain / L1 / L1.5
      │
      ▼
   freeze() → EvidencePackage
              ├── evidence_plan
              ├── evidence_catalog   ← NEW（稳定 ID → 证据的反查表，freeze 后不可变）
              ├── context_bundle     ← 兼容保留（explain 路径语义不变）
              ├── requirement_coverage
              └── search_history / coverage_rounds / retrieval_state
```

### 3.5.4 Workspace 与 Registry 的生命周期（修订二补充）

生命周期必须精确定义，否则"每轮"与"全程"会混淆。

```python
def retrieve(self, request, top_k) -> RetrievalOutcome:
    registry = CitationRegistry()                     # ① 一次请求只建一次
    workspace = EvidenceWorkspace()                   # ① 同上
    pool = EvidencePool()

    pool.add_many(...round 0...)                      # ② 每轮追加
    registry.register(pool)                           # ② 增量注册，只增不改
    view0 = workspace.coverage_snapshot(pool, plan)   # ③ 当轮 Coverage 快照
    coverage0 = self.coverage_checker.check(plan.requirements, view0)

    ... 第二轮（若需要）同样三步 ...

    catalog = registry.freeze()                       # ④ 全部检索结束后冻结
    workspace.freeze()
```

四条不变式：

1. **每轮 Retrieval 后**从当前 `EvidencePool` 构造一个 Coverage snapshot。第 0 轮的快照只反映第 0 轮找到的证据——这正是当前代码的语义（`:130` 与 `:183` 各调一次 `check`），只是输入源从截断的 `bundle` 换成当轮的 Workspace 视图。
2. **`CitationRegistry` 在同一次 `retrieve()` 生命周期内增量注册**。新增证据分配新 ID，已有证据的 ID 永不改变。第二轮若召回第一轮已有的 chunk，复用它原有的 ID。
3. **绝不重新编号**。这是 Phase 3 的 Section-scoped Citation Validation 能够成立的前提。
4. **全部检索完成后 `freeze()`** 成最终 `evidence_catalog`，此后不可变，随 `EvidencePackage` 一起交给解释层。

> 这条生命周期是对 §6.1 的收紧：修订二初稿写的是"在 `pool.add_many` 之后、`_build_context` 之前**一次性**构造 Workspace"。正确的是**每轮构造 Coverage 快照、增量注册、最后冻结**。差别在于：如果只在第一轮之后构造，第二轮新召回的证据就不在第 0 轮的 Coverage 判定里，而 `CoverageRound(0, ...)` 会被写成"它漏掉了第二轮才找到的东西"这种时序错乱的记录。

**裁决：采用"给 `EvidencePackage` 增加 `evidence_catalog` 字段"的方案，而不是另起 `RetrievalOutcome.workspace`。**

理由：语义上 `EvidencePackage` 本来就该代表"检索阶段冻结下来的全部证据"，现在它只是"冻结下来的一部分 Context"。增加一个 catalog 字段既纠正了这个语义，又让 `explain` / legacy 路径的既有消费方（`_capture_arm` 读 `result.context_bundle`）零改动。

---

## 4. 目标架构（修订二）

```
User Question
  → Evidence Planner → SearchAction Planner → Retrieval Controller   （检索算法不改）
  → EvidenceWorkspace（完整证据 + 稳定 ID）        「我找到了哪些事实」
  → Explanation Planner → ExplanationPlan         「用户需要建立什么理解」
  → Section Context Views                          「这一部分需要哪些事实」
  → Grounded Section Generation（Fast / Deep 双路径）
  → Global Composer
  → Teaching Reviewer（三档门控 + 最多一次定向修订）
  → TeachingAnswerResult
```

新增文件（当前均不存在，已验证）：

```
src/devcontext/
├── context/     workspace.py  registry.py  views.py  budget.py  estimator.py
│                （builder.py 保留为 legacy）
├── explanation/ __init__.py  models.py  planner.py  prompts.py  workflow.py
├── answer/      composer.py                      （planner.py 降级为兼容适配器）
└── evaluation/  pedagogy.py
```

---

## 5. Phase 0 — 冻结基线

**任务**

1. 提交 §0 描述的 docs V1/V2/V3 重组。
2. 跑通并留存：
   ```bash
   uv run pytest
   uv run devcontext evaluate
   uv run devcontext evaluate-retrieval-workflow --suite l1.5 --mode live
   ```
3. 把**余票桶问题加入基准集**。已验证 `benchmark/l2-answer-quality.jsonl` 现有 18 例（`flow-01..06` / `why-01..04` / `edge-01..04` / `negative-01..02` / `locate-01..02`），**余票桶不在其中**。
4. 保存 `explain` 模式的当前输出到 `artifacts/answer-quality-v2-baseline/`，含余票桶那题的**逐字全文**。

**修订二补充：基线可复现性。**

`.gitignore` 明确排除 `artifacts/*`，所以上面的 raw 输出**不会进 Git**——换机器、换 Agent、面试复现时基线就没了。改为双轨：

```
artifacts/answer-quality-v2-baseline/        ← raw，大体积，不进 Git
benchmark/baselines/answer-quality-v2-manifest.json   ← 进 Git
```

manifest 记录：`git_sha / case_id / mode / depth / model / timestamp / config_digest / answer_sha256 / token_usage / judge_result / key_metrics`。

这个位置**已存在且已被跟踪**（`benchmark/baselines/retrieval-v1.json`、`retrieval-workflow-v1.json`），follow 既有惯例即可。

**验收**：manifest 已在 `git ls-files` 中；raw 目录含余票桶全文；`pytest` 全绿；L2 基准 19 例。

### 5.1 执行 Phase 0 时发现的三条约束（修订三补充）

**(a) L1.5 与 L2 的 id 必须一一对应。** `tests/test_benchmark_frozen.py:58-63` 断言 `[case["id"] for case in l15] == [case["id"] for case in l2]` 且两者长度相等。因此往 L2 加 `teach-token-bucket-01` 就**必须**同时加一个 L1.5 孪生条目，否则冻结守卫失败。另有两处会连带失败，且都值得顺手修：

- `tests/test_answer_quality.py:15` 硬编码 `len(cases) == 18` 与各前缀计数 —— 需更新为 19 并加上 `teach-` 一条。
- `tests/test_answer_quality.py:31` 用 `[-1]` 取**最后一个**案例，并断言 `checks.in_target_range is True`。它隐含依赖"最后一个案例是 `brief`"（原先的 `locate-02`，150–500 区间）。**这是脆弱写法**，应改为按 id 取，否则任何追加都会静默改变它的语义。

**(b) L1.5 条目的字段是强约束的、且 `expected_retrieval_state` 是派生的**（`evaluation/retrieval_workflow_runner.py`）：

| 约束 | 位置 |
|---|---|
| `expected_retrieval_state` 必须等于 `_derived_expected_state(requirements)`，不可自由填写 | `:223-227` |
| 至少一条 CORE requirement | `:222` |
| `expected_satisfied == true` 时 `relevant` 不能为空 | `:253` |
| `relevant` 的 source 必须与 `source_requirement` 匹配 | `:263-264` |
| CORE 必须同时有 `round_0` 与 `round_1`；SUPPORTING 只有 `round_0` | `:265-271` |

因此孪生条目**必须从实测的最终 Context 反推**，不能凭猜——猜错会让该案例永久失败，并被误读为检索回退。

**(c) L1.5 的 oracle 用最终 `context_bundle` 判定，且排除 `truncated` 项**（`:453` 与 `:143`）。这说明 **L1.5 自身也受 §3.5 的截断影响**。它同时给了 Phase 1A 一条更硬的验收：explain 路径下 L1.5 的结果必须逐案不变。

**(d) `.lua` 文件根本不在索引里。** 已验证：`SELECT count(*) FROM knowledge_chunk WHERE file_path LIKE '%.lua'` → **0**。摄取只覆盖 Java（JavaParser）与 Markdown。这解释了为什么 DevContext 答案把"两个 Lua 脚本的脚本体"列为证据缺口——**不是模型没找到，而是它从未被摄取**。

> 这条值得单独记住：`take_token_from_bucket.lua` 与 `return_token_to_bucket.lua` 里正是"余额不足时整单失败还是部分扣减""是否刷新 TTL"这类问题的答案所在，而 §2.2 的 G4 又恰好要求允许讲"证据里没有的东西"。两者叠加会出现一个危险组合：**读者最想要的那部分，恰好是索引覆盖不到的那部分**。因此 teach 路径的条件推演（§11）在这类主题上必须格外明确地标注"缺少脚本本体证据"，而不能因为"通用原理可以讲"就滑向断言项目实现。

**(e) 检索失败会被静默误报为"证据不足"——这是一条产品级缺陷，不只是环境问题。**

执行 Phase 0 时遇到过真实案例：本机 `HTTPS_PROXY` 指向本地代理，导致 `curl` 对 `dashscope.aliyuncs.com` 的 TLS 握手失败，**所有 embedding 调用失败**。此时 keyword 检索仍可用，但 vector 与 hybrid 全废。而系统对用户说出来的话是：

```
Sufficiency: insufficient
Requirements: ER1 missing, ER2 missing, ER3 missing, ER4 missing, ER5 missing, ER6 missing
Answer: 当前没有检索到足够的项目上下文，无法可靠回答该问题。
Sources: (none)
```

**读起来是"知识库没有这份证据"，真相是"检索接口不可达"。** 信号其实在 trace 里——10 个 search action 全部带着 `err=RuntimeError`，`stop_reason: empty`——只是从未浮现到人类可见的输出上。原因是 `retrieval_controller.py:241` 只把异常**类名**记进 action，消息被丢弃。

这意味着 `retrieval_state = "EMPTY"` 目前把两种完全不同的情况混成一个值：

| 真实情况 | 应有状态 | 现在都变成 |
|---|---|---|
| 检索跑了，确实没有相关证据 | `EMPTY` | `EMPTY` |
| 检索根本没跑成（网络/依赖失败） | 需要独立状态，如 `RETRIEVAL_FAILED` | `EMPTY` |

**裁决：并入 §7.3 的 `RetrievalState` 迁移一起做。** 当全部 search action 都带 error 时，不得输出 `EMPTY` + "证据不足"的结论，而应显式区分。这一条同时是对 G5 的加强——「系统自称的确定性与它的实际证据状态不一致」还有一个更极端的版本：**系统自称"没有证据"，而它其实连检索都没成功。**

失败输出已留档：`artifacts/answer-quality-v2-baseline/PROXY-FAILURE-token-bucket.explain.txt`。

**(f) 孪生条目落地后的回归证据（已实测）。**

新增 `teach-token-bucket-01` 后跑全量 frozen（19 L1.5 + 5 regression = 24 例），与已提交的 `benchmark/baselines/retrieval-workflow-v1.json`（23 例）逐桶比对：

| 桶 | 基线 | 本次（截取同一批 23 例） | 结论 |
|---|---|---|---|
| READY（18 例）| `all_core_satisfied` 16/18 = 0.8889，`state_accuracy` 0.8333 | 16/18 = 0.8889，0.8333 | **逐位相同** |
| PARTIAL（4 例）| `state_accuracy` 1.0 | 1.0 | 相同 |
| EMPTY（1 例）| `state_accuracy` 0.0 | 0.0 | 相同 |
| `false_ready_count` | 0 | 0 | 相同 |

新案例本身：`actual_state = READY`、`full_case_success = True`、`core_requirement_coverage = 1.0`、`false_ready = False`。加入后 READY 桶升为 17/19 = 0.8947。

> 附带确认了一条口径：`all_core_satisfied_case_rate` 的分母是**该类期望状态的案例数**（16/18），不是全集。跨版本比对这个指标时不要用全集做分母，否则会误判成回退。

---

## 6. Phase 1A — EvidenceWorkspace 与 CitationRegistry

`refactor: freeze evidence workspace and stable citation registry`

**前置**：本 Phase 是本轮唯一触及 `agentic/` 的改动，必须作为独立 commit 以便回滚。

**任务**

1. **在 `RetrievalController.retrieve()` 内持有 Workspace 与 Registry（每轮更新，最后冻结）。** `EvidencePool.candidates_by_sub_question`（`evidence.py:59`）本身就是完整候选的载体，无需新增检索。按 §3.5.4 的四条不变式接线：`retrieve()` 入口建 Workspace + Registry → 每轮 `pool.add_many` 后增量注册并产出当轮 Coverage 快照 → 全部检索结束后 `freeze()`。**不要只在第一轮之后构造一次。**

2. `context/workspace.py`：`EvidenceRef`（02 第 13.3 节）与 `EvidenceWorkspace`（02 第 14 节）。API：`for_requirement` / `for_requirements` / `by_citation` / `materialize` / `metadata_view` / `stats`。**无任何字符或 token 上限。**

3. `context/registry.py`：`CitationRegistry`。

```python
@dataclass(frozen=True, slots=True)
class CitationRegistry:
    """一次请求生命周期内稳定不变的证据身份。"""
    by_evidence_id: Mapping[str, Citation]   # "E1" -> Citation
    by_chunk_id: Mapping[int, str]           # chunk_id -> "E1"
```

   在 `retrieve()` 生命周期内**增量注册**：新证据分配新 ID，已有证据的 ID 永不改变（第二轮召回第一轮已有的 chunk 时复用原 ID）。此后 `PlannerView` / `SectionView` / `ReviewerView` 都只持有子集，**绝不重新编号**。全部检索结束后 `freeze()`，产出不可变的 `evidence_catalog`。

   > **原因**：当前 label 是 `ContextBuilder` 按 Context 顺序现场生成的——`label = f"C{len(items) + 1}"`（`context/builder.py:54`）。同一 Chunk 在不同 View 里会拿到不同编号，Section 1 的 `[C1]` 与 Section 2 的 `[C1]` 可能根本不是同一条证据。没有稳定身份，Phase 3 的 Section-scoped Citation Validation 和 Composer 都无址可依。

4. **`CITATION_PATTERN` 必须放宽。** 当前是硬编码 `C` 前缀：

```python
CITATION_PATTERN = re.compile(r"\[(C\d+)\]")     # answer/generator.py:14
```

   裁决：**teach 路径端到端使用 `E` 前缀**，把正则改为 `r"\[([CE]\d+)\]"`（`strip_citations` `:221` 与 `extract_citations` `:262` 都基于它，一处改动两处受益）。`explain` / `legacy` 继续用 `C` 前缀，L1 / L1.5 / L2 既有基线因此保持可比。

   > 备选方案是保持 `C` 前缀、每个 View 内部重新编号 + 维护 `label → evidence_id` 映射，由 Composer 统一重编。**不采用**：多一层映射就多一处静默错位的可能，而 allowlist 校验恰恰靠这个映射的正确性。牺牲的只是 CLI 上显示 `[E1]` 而非 `[C1]`。

5. `EvidencePackage` 增加 `evidence_catalog` 字段（见 §3.5.3）。因为它是 `frozen dataclass`，新增字段须带默认值，保证既有构造点不破。

6. **不改** `context/builder.py` 的 legacy 行为——L1 / L1.5 依赖它。`break` 语义只在 legacy 路径保留，新路径不复用。

**测试**

- `test_workspace_keeps_evidence_beyond_prompt_budget`
- `test_workspace_has_no_char_limit`
- `test_registry_assigns_stable_ids_across_views` ← 同一 chunk 在任意 View 中 ID 不变
- `test_registry_survives_two_retrieval_rounds`
- `test_evidence_package_catalog_absent_for_legacy_path_is_tolerated`
- **关键验收**：构造 100K+ 字符 synthetic evidence，断言 Workspace 保存全部 `EvidenceRef`、catalog 可反查、**第 8 节能看到旧 28K 预算下会被尾部丢弃的证据**。

**验收**：上述测试全绿；余票桶在 `explain` 模式下的输出与 Phase 0 快照**逐字一致**（证明本 Phase 零行为变更）；`pytest` 全绿。

---

## 7. Phase 1B — Coverage 解耦与 Token 预算

`refactor: add requirement-scoped coverage views and token budget policy`

**任务**

1. **`CoverageView`：换输入源，不换调用结构（修订二 P0）。**

   现状已核对：`CoverageChecker.check` **内部本来就是 requirement 分区的单次批量调用**——它按 `requirement.id in item.sub_question_ids` 把 `context.items` 分到 `evidence_by_id`（`agentic/coverage.py:50-56`），然后**一次** `_semantic_check(eligible, evidence_by_id)` 把全部 eligible requirement 打包成一个 payload 发出去（`:79`、`:85-114`）。问题只在输入源：它读的是 `context.items`，并且**显式排除 `truncated` 项**（`:54`）。

   因此改动是**只换输入源，不动调用结构**：

```python
# 现在：coverage_checker.check(plan.requirements, bundle)          retrieval_controller.py:130 / :183
# 改为：coverage_checker.check(plan.requirements, view)            view 为当轮 Workspace 快照
```

   **契约：一次 Coverage Round 仍然只调用一次 semantic checker。** Requirement-scoped 的是 **Evidence View**，不是 LLM call。**严禁**把它拆成每 ER 一次 LLM 调用——那会把 Coverage 的调用数与成本乘以 ER 数量（最多 6 倍）。

   附带收益：Workspace 视图里的证据天然没有"被回答预算截断"的概念，`:54` 的 `not item.truncated` 过滤在新路径下不再误伤，`MISSING` 的假阴性（§3.5.2c）随之消失。**Coverage 的判定语义与状态枚举不变**（`COVERAGE_STATES`，`agentic/evidence_models.py:11`），确定性检查逻辑（`_missing_sources`）与状态机（`_parse` 的六条校验）全部保留。

2. **顺带修 §3.5.2b**：`_discovered_terms` 的数据源从 `bundle` 改为 Workspace 的 requirement 视图，让第二轮的种子不再受第一轮截断影响。

3. **`RetrievalState` / `EMPTY` 迁移（修订二 P0）。**

   现状：`package_state(plan, context, coverage)` 以 `if not context.items: return "EMPTY"` 判定（`agentic/evidence_models.py:155`）；`_answer()` 的提前退出条件是 `if package.retrieval_state == "EMPTY" or not package.context_bundle.items:`（`agentic/evidence_workflow.py:130`）。两者都以 **legacy `ContextBundle.items`** 为准。

   在 teach 路径下这条判据失效——`context_bundle` 可能只是 `final_cited_bundle`，而 `final_cited_bundle` 是**解释层跑完之后**才有的，用在 `_answer()` 的入口判断上就成循环依赖。而且 `EvidencePackage.evidence_items`（`:102-104`）与 `to_dict()`（`:110`）也都从 `context_bundle.items` 取值。

   裁决：

   - **teach 路径的 evidence existence 一律以 Workspace 为准。** `package_state` 增加一个接受 evidence count 的重载/参数，由调用方决定是数 `context_bundle.items`（legacy / explain）还是数 Workspace（teach）。
   - **`_answer()` 的提前退出条件同步修改**：`EMPTY_CONTEXT_ANSWER` 只在 Workspace 真的没有任何证据时触发，不再因为 `context_bundle` 为空而触发。
   - `evidence_items` 与 `to_dict()` 保持读 `context_bundle`（兼容既有 trace / 评测消费方），但 teach 路径的 `context_bundle` 在 `_answer()` 阶段尚未被 `final_cited_bundle` 覆盖，因此**不可**在该阶段用它做存在性判断。

   - **新增 `RETRIEVAL_FAILED` 状态，把"没找到"与"没跑成"分开（见 §5.1(e)）。** 当本轮全部 search action 都带 `error` 时，`retrieval_state` 不得落回 `EMPTY`——那会把网络/依赖故障谎报成"知识库没有这份证据"。**注意这条的波及面比看起来大**，必须成组修改：

     | 受影响处 | 位置 |
     |---|---|
     | 状态枚举本身 | `agentic/evidence_models.py:12`（`RETRIEVAL_STATES`，被 `EvidencePackage.__post_init__` 校验）|
     | 状态计算 | `agentic/evidence_models.py:150-161`（`package_state`）|
     | legacy 充分性映射 | `:141-147`（`to_legacy_sufficiency` 的 `enough = retrieval_state == "READY"`）|
     | 原因文案 | `:218-223`（`_coverage_reason` 的字典缺 key 会 `KeyError`）|
     | trace 的 `stop_reason` | `agentic/evidence_workflow.py:101`（`package.retrieval_state.lower()`）|

   > 这条如果漏掉，会出现一个很难查的症状：teach 路径下 `ContextBuilder` 预算恰好一个 chunk 都没装进去时（`ContextBuilder.build` 在 `available` 装不下第一个 block 时会 `break` 且 `items` 为空），系统会错误地输出 `EMPTY_CONTEXT_ANSWER`——而 Workspace 里明明有几十条证据。

4. `context/views.py`：`PromptContextView`（02 第 15 节）+ 三种 View（Planner / Section / Reviewer，02 第 16 节）。

5. `context/estimator.py`（修订二新增，P0）：`TokenEstimator` 协议。

```python
class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...
```

   `pyproject.toml` 当前依赖为 `openai / pgvector / psycopg / pydantic-settings`（`pyproject.toml:11-16`），**没有任何 tokenizer**。因此裁决：第一版用保守估算 + 安全 margin，**并且必须在 Trace 里回填实测值**，把偏差标出来而不是假装精确：

```
estimated_input_tokens = 18231
actual_input_tokens    = 17542
error_ratio            = 3.9%
```

   数据来源现成：`_stage_usage` 已经从 `client.last_usage` 读 `prompt_tokens` / `completion_tokens`（`retrieval_controller.py:334-335`）。
   **重点不是第一版做到 tokenizer 精确，而是不再使用"28000 字符"这种与模型能力无关的硬编码。**

6. `context/budget.py`：`TokenBudgetPolicy` + `ModelCapabilities(context_window, max_output_tokens)`。配置优先级 `模型 API metadata → config override → conservative fallback`。`Settings`（`config.py:9-33`）当前**没有 context window 字段**，需新增；**不要把 DeepSeek 的 1M / 384K 硬编码进业务代码**。超长时降级顺序按 02 第 18 节八步；`EvidenceDigest`（02 第 19 节）必须始终链接回 raw evidence。

**测试**

- `test_coverage_view_sees_evidence_dropped_by_answer_budget` ← 直击 §3.5.2c 假阴性
- `test_second_round_seed_not_limited_by_first_round_truncation` ← 直击 §3.5.2b
- `test_coverage_round_uses_single_llm_call` ← 守住"View 分区、LLM 调用不分区"
- `test_coverage_snapshot_reflects_round_local_pool` ← 第 0 轮快照不得包含第二轮才召回的证据
- `test_teach_empty_state_derived_from_workspace_not_bundle` ← 直击 §7.3
- `test_section_view_only_materializes_relevant_evidence`
- `test_large_evidence_does_not_disappear_after_planning`
- `test_digest_preserves_original_evidence_reference`
- `test_token_budget_not_char_budget`
- `test_estimator_records_actual_vs_estimated`

**验收**：余票桶重跑，`Sufficiency` 与 `Retries` 的取值若发生变化，必须能解释（是假阴性被修掉，还是真的缺证据）。**这是本 Phase 最有价值的一条观测。**

---

## 8. Phase 2 — ExplanationPlan 与 `teach` 模式

`feat: add teaching explanation planner`

**任务**

1. `explanation/models.py`：`ExplanationPlan`（02 第 21 节）、`ExplanationSection`（第 22 节）、`SectionConfidence`（第 26 节），以及**修订二调整过的** `ClaimPlan`：

```python
@dataclass(frozen=True, slots=True)
class ClaimPlan:
    claim_goal: str
    claim_type: str                     # 四层，见 §3.2
    evidence_labels: tuple[str, ...]
    confidence: str                     # CONFIRMED / PARTIAL / UNVERIFIED
    assumptions: tuple[str, ...] = ()   # NEW
    conditional: bool = False           # NEW
```

   枚举：02 第 23 节 14 种 `section_type`、第 24 节 8 种 `teaching_device`。

2. `explanation/prompts.py`：02 第 27 节十条规则。第 2 条（**先判断用户最终要建立什么心智模型**）与第 3 条（**调查顺序 ≠ 教学顺序**）直击 G1/G2。

3. `explanation/planner.py` + `explanation/workflow.py`：`TeachingExplanationWorkflow.run(request, evidence_package) -> TeachingAnswerResult`（02 第 62 节）。**不要**让 `agentic/evidence_workflow.py:_answer_explain`（`:175-257`，已 82 行）继续膨胀。

4. `answer/planner.py` 降级为兼容适配器，**本轮不删除**。

5. **Teach dispatch 与 Depth 契约（修订二 P0）。**

   (a) **模式扩展。** `request.py:7-8` 的 `ANSWER_MODES` 增 `teach`、`ANSWER_DEPTHS` 增 `deep`；`cli.py:125-133` 的 argparse `choices` 同步。

   (b) **`deep` 仅允许 `teach`。** `legacy` / `explain` 仍只支持 `brief / standard / detailed`。这条必须是**跨字段校验**，加在 `AnswerOptions.__post_init__`（`request.py:22-26`）——现在的校验是 depth 与 mode 各自独立判定的，无法表达"deep 配 legacy 非法"：

```python
_LEGACY_DEPTHS = ("brief", "standard", "detailed")
if self.answer_mode != "teach" and self.depth_override == "deep":
    raise ValueError("depth 'deep' requires answer_mode 'teach'")
```

   (c) **显式分发。** `EvidenceDrivenWorkflow` 当前是二元的——`_answer()` 里 `if answer_mode == "legacy": ... return self._answer_explain(...)`（`agentic/evidence_workflow.py:145,173`）。改为三分支显式分发：

```python
if mode == "legacy":   return self._answer_legacy(...)
if mode == "explain":  return self._answer_explain(...)
if mode == "teach":    return self._answer_teach(...)      # → TeachingExplanationWorkflow
raise ValueError(...)                                       # 未知模式不得静默落到 explain
```

   **不得让未知模式静默 fallthrough 到 `explain`** —— 那会让 `deep` 契约在 `EvidenceDrivenWorkflow` 里被绕过。

   (d) **Teach 拥有自己的 client policy。** Planner / Writer / Composer / Reviewer 四个阶段的模型、`reasoning_effort`、`max_tokens` 由 teach 独立配置，不复用 explain 的。现状参照：`cli.py:282 _answer_client(settings, explain=...)`、`:327 _answer_planner_factory`、`:338 _reviewer_factory` 都是为 explain 调的。新增对应的 teach 工厂，并允许 `Settings`（`config.py:28-31`）的分角色覆盖增加 teach 专属项。理由：teach 的 Writer 是逐 Section 生成、Reviewer 是教学型审稿，与 explain 的单次生成在预算和提示词上都不同，共用一个 client policy 会互相牵制。

   `explain` 行为本轮完全不变。

6. **`TeachingAnswerResult` 契约（修订二新增，P0）。** 采用**扩展现有类型**而非新建：

```python
@dataclass(frozen=True, slots=True)
class AgenticAnswerResult:
    answer_result: AnswerResult
    context_bundle: ContextBundle              # teach 路径下 = final_cited_bundle
    trace: AgenticTrace
    citation_registry: CitationRegistry | None = None    # NEW
    workspace_stats: Mapping[str, Any] | None = None     # NEW
```

   > **原因**：现在 `run` 返回 `AgenticAnswerResult(answer_result, package.context_bundle, trace)`（`evidence_workflow.py:123`），而 `build_citation_trace(answer_result, package.context_bundle)`（`:105`）与 CLI 的 Sources 都从这个 `context_bundle` 反查。teach 路径的引用来自多个 Section View，不属于任何一个原始 bundle，直接复用会错位。
   >
   > 裁决：**teach 路径把 `context_bundle` 字段填成 `final_cited_bundle`**——只 materialize 最终真正被引用的那些证据（Workspace 有 23 条，最终引用 4 条，bundle 就只装这 4 条），标签用 `E*`。这样 CLI Sources、`build_citation_trace`、L2 的 `_context_digest`（`evaluation/answer_quality_runner.py:49-67`）**全部零改动**继续工作。
   >
   > 比"新建 `TeachingAnswerResult` 类型"更好的地方：不引入 CLI 分支，不给 `explain` 路径制造第二套结果形状。

7. 余票桶的期望 `ExplanationPlan` 按 02 第 28 节固化为测试 fixture。

**测试**（02 第 53 节）

- `test_plan_has_core_mental_model`
- `test_plan_does_not_copy_requirements_to_sections` ← 直击 G1
- `test_plan_distinguishes_fact_and_example` ← 直击 G4
- `test_plan_uses_failure_scenario_for_edge_question`
- `test_locate_question_does_not_overplan`
- `test_negative_question_corrects_false_premise`
- `test_teaching_result_final_bundle_contains_only_cited_evidence`（修订二新增）
- `test_teaching_result_registry_labels_survive_resolution`（修订二新增）
- `test_deep_depth_rejected_outside_teach_mode`（修订三新增，跨字段校验）
- `test_unknown_answer_mode_does_not_fall_through_to_explain`（修订三新增）
- `test_teach_dispatch_uses_teaching_workflow`（修订三新增）

**验收**：余票桶 `--answer-mode teach` 的 `ExplanationPlan` 含可贯穿全文的 `core_mental_model`；`sections` 与 Evidence Requirement **不是一一对应**；至少一个 `FAILURE_SCENARIO` 与一个 `TRADEOFF` 章节。

---

## 9. Phase 3 — Section-scoped 生成与 Composer

`feat: add section-scoped grounded answer generation`

**任务**

1. Fast Path / Deep Path 双模式（02 第 29–31 节）：`brief` 与简单 `locate` 走单次生成；`detailed` / `deep` 或多章节走逐 Section 生成。
2. `answer/composer.py`：Global Composer（02 第 34 节）。允许调整顺序、增加过渡、去重、统一术语；**禁止新增项目事实、新增 Citation、新增未出现的 Symbol**。
3. **Section Scoped Citation Validation**（02 第 33 节）：第 3 节只允许它绑定的证据；即使 `C15`/`E15` 存在于整个 Workspace 也算违规。这把 Citation 从"引了一个存在的 Chunk"升级为"这个章节只能使用 Planner 指定的证据"。**依赖 Phase 1A 的 `CitationRegistry`——没有稳定 ID 这条无法实现。**
4. **废除固定字数区间**：`answer/planner.py:22-26` 的 `DEPTH_LIMITS` 与 `evaluation/answer_quality.py:13-17` 的 `DEPTH_CHAR_RANGES` 是同一套硬约束，两者都要改为按 `OutputBudget`（02 第 36 节）判断。`--depth` 语义按 02 第 37 节四档。
5. `DraftSection`（02 第 32 节）。

**测试**（02 第 56、57 节）

- `test_deep_answer_can_exceed_old_5000_char_limit` ← 直击 G3 的硬约束
- `test_detailed_answer_not_forced_to_hit_minimum_length`
- `test_brief_locate_answer_stays_brief`
- `test_multi_pass_sections_preserve_citations`
- `test_composer_does_not_introduce_new_citations`
- `test_project_fact_requires_evidence`
- `test_general_concept_does_not_require_project_citation`
- `test_hypothetical_example_is_labeled`
- `test_unverified_claim_not_written_as_fact`
- `test_section_cannot_use_unbound_citation`
- `test_conditional_claim_rendered_with_assumptions`（修订二新增，见 §11）

**验收**：`--answer-mode teach --depth deep` 在余票桶上不再受 5000 中文字符上限约束；`invalid_citations` 为 0；Composer 输出中不存在未在章节草稿里出现过的 Citation。

---

## 10. Phase 4 — 教学型 Reviewer 与 L2 V3

`feat: add pedagogical review and answer-quality v3 evaluation`

### 10.1 修订二：先泛化，再加模式

**当前 L2 runner 是硬编码两臂的**（`evaluation/answer_quality_runner.py:29-35`）：

```python
V1_MODE = "legacy"
V2_MODE = "explain"
ANSWER_MODES = (V1_MODE, V2_MODE)
V1_WINNER = "V1"
V2_WINNER = "V2"
```

Judge 输出也是 `V1 / V2`。所以单纯往 `ANSWER_MODES` 里加 `teach` 是不够的。**裁决：先泛化，再加模式：**

```python
@dataclass(frozen=True, slots=True)
class PairwiseEvaluationConfig:
    baseline_mode: str
    candidate_mode: str
```

Judge 内部只关心 `BASELINE` / `CANDIDATE`，最后映射回 mode。这样 `legacy vs explain`、`explain vs teach`、`teach-v1 vs teach-v2` 都不用再改 runner。

### 10.2 修订二：benchmark schema 的迁移顺序

`AnswerQualityCase.from_dict` 是严格字段校验：

```python
if set(value) != required:
    raise ValueError("answer quality case has invalid fields")   # answer_quality.py:37-38
```

所以"先给余票桶加新字段、再慢慢回填其余 18 例"这个顺序**会立刻把 loader 搞挂**。正确顺序：

```
① 升级 AnswerQualityCase，新增 teaching 字段并给默认值
② 旧 18 例继续可加载（回归测试守住）
③ 加入第 19 例（余票桶），写全新字段
④ 逐步回填旧 18 例
⑤ 最后再决定是否把新字段变为 required
```

**数据集与 loader 不得同时大改。**

### 10.3 其余任务

1. `answer/reviewer.py`：新增 02 第 44 节的 9 类 issue；`LENGTH_VIOLATION` 不再按 2200–5000 判断。既有 8 类（`answer/models.py:13-22`）保留。
2. 定向修订（02 第 45 节）：Reviewer 输出 `section_issues` + `revision_required`，**只重生成被点名的章节**，最多一轮。
3. **Reviewer 门控三档（修订二调整，不采纳"所有 teach 路径都跑 LLM Reviewer"）**：

| 路径 | 门控 | 理由 |
|---|---|---|
| `brief` / `locate` | **deterministic reviewer**：Citation 合法 + Claim CONFIRMED + 无 unresolved core | 答案可能只有一行（"入口位于 `UserController#registerUser`"）。再跑一轮 LLM 更慢、更贵，还有被改坏的风险 |
| `standard` | 条件 LLM reviewer | 沿用现有 `_answer_explain` 的判定思路（`evidence_workflow.py:215-224`），但去掉"零引用/异常才触发"的过窄条件 |
| `detailed` / `deep` | Teaching Reviewer | 完整教学型审稿 |

   这比"所有 teach 必须 Reviewer"更成熟，也修掉了 G5 里"`brief` 从不审稿"的问题——只是用确定性审稿而非 LLM 审稿来修。
4. `evaluation/pedagogy.py`：`PedagogyScore(mental_model, causal_explanation, progressive_disclosure, examples, failure_reasoning, tradeoffs, readability)`（02 第 50 节）。
5. benchmark schema 扩展（02 第 47 节）：`core_mental_model` / `must_explain_why` / `useful_scenarios` / `misconceptions` / `pedagogy_expectations`，按 §10.2 顺序。
6. Pairwise Judge 增维度（02 第 48 节），重点评 **Why Coverage 而非字数**（02 第 49 节）。
7. Trace 扩展（02 第 59 节）：`explanation_plan` / `context_views` / `section_drafts` / `section_citations` / `section_confidence` / `compression_events` / `revision_trace`，外加修订二新增的 `estimated_vs_actual_tokens`。

**测试**（02 第 58 节）

- `test_reviewer_detects_missing_mental_model`
- `test_reviewer_detects_project_fact_without_support`
- `test_reviewer_detects_general_knowledge_as_project_fact`
- `test_reviewer_targets_only_bad_section`
- `test_review_does_not_rewrite_accepted_sections`
- `test_brief_uses_deterministic_reviewer`（修订二新增）
- `test_runner_accepts_arbitrary_mode_pair`（修订二新增）
- `test_old_cases_load_after_schema_extension`（修订二新增，守住 §10.2）

**验收**：见 §11。

---

## 11. Release Gate

### Grounding（硬门槛）

- `invalid_citations == 0`
- 不新增任何已知 unsupported project claim
- 历史文档不得覆盖当前代码（`source-policy.json` 已把"后续规划"标为 `HISTORICAL_PLAN` / `FUTURE` / 优先级 30）
- **本项目特有的正确性门槛**：余票桶答案必须仍然说对 §2.3 的两处——`loadBucket` 走字段级缺失判定（`hMultiGet`），补齐按车型支持的全部席别整桶补齐。**回退即视为验收失败。**

### G4 判据的修正（修订二）

修订一写的是"至少一个'证据里没有、但由通用原理推出'的失效场景"。**这条必须改，因为它留了一个幻觉边界漏洞。**

`GENERAL_CONCEPT` 是"Redis 的通用原理"，`ILLUSTRATIVE_EXAMPLE` 是"假设案例"——但两者都跨不过中间那一步：

> **项目实现是否满足这个场景的前提？**

以 ChatGPT 第十八节为例，它能断言"当前实现存在这个竞态"，前提是它知道项目的 Lua 是 `EXISTS→HINCRBY` 这样写的。而证据里**没有 Lua 脚本体**。所以正确的说法只能是：

> "如果归还逻辑直接对不存在的 Hash 自增且没有原子保护，那么一般会产生 X 风险；**当前提供的项目证据不足以确认本项目是否满足这个前提**。"

修订二要求 `ClaimPlan` 携带 `assumptions` + `conditional`（§8.1），生成时必须渲染成"如果……那么……"而非"当前项目会……"。

**修正后的判据**：

| 判据 | 通过标准 |
|---|---|
| 存在至少一个条件推演章节 | 有，且 `conditional = true` 且 `assumptions` 非空 |
| 该推演的措辞 | 是"如果 X 则 Y"+"证据不足以确认前提"，**不是**"当前项目存在 Y" |
| 正文不得出现的措辞 | 任何把未确认前提写成项目事实的句子 |

### Retrieval（不得回退）

- L1 无明显回退；L1.5 的 False READY 继续保持 0
- **新增**：`CoverageView` 上线后，`Sufficiency` 分布的变化必须有解释（§7 验收）

### Answer（盲测）

- `teach` vs `explain` 走 swapped-order judge，沿用 `WIN_RATE_THRESHOLD = 0.70`（`evaluation/answer_quality_runner.py:38`），经 §10.1 泛化后的 `PairwiseEvaluationConfig(baseline_mode="explain", candidate_mode="teach")`
- 人工逐条检查：余票桶、购票一致性、双请求抢同一座位、订单失败补偿、一个 `locate` 问题、一个 `negative` 问题

### 人工判据（机器测不出，但本轮真正的验收）

拿 Phase 0 的余票桶快照与 `teach` 新输出并排读：

| # | G# | 判据 | 通过标准 |
|---|---|---|---|
| 1 | G1 | 是否先建立"10 张票 / 50 个请求 / 40 个注定失败"这类矛盾，再引入方案？ | 首个章节是 `PROBLEM_SETUP` |
| 2 | G2 | 是否有一句心智模型，全文出现 ≥2 次、能作组织轴？ | `core_mental_model` 可被读者复述 |
| 3 | G3 | 章节标题里有多少是"为什么…"？ | ≥3 个 why 型章节，且各回答不同的"为什么" |
| 4 | G4 | 是否有条件推演章节且标注正确？ | 见上表 |
| 5 | G5 | 逐节读下来，能否判断哪句坐实、哪句推测？ | 存在 `UNVERIFIED` / `PARTIAL` 章节且措辞相应软化 |
| 6 | — | 是否仍能跳到每一个类/常量/行号？ | 真实标识符密度不低于基线 |

---

## 12. 回归红线（原则上不得修改）

02 第 52 节，已验证这些位置存在：

| 不得改 | 位置 |
|---|---|
| keyword scorer | `retrieval/service.py` |
| vector scorer / RRF (k=60) | `retrieval/hybrid.py` |
| `RetrievalPolicy` | `retrieval/policy.py:21,72` |
| Evidence Requirement 语义 | `planning/evidence_models.py:13,26` |
| Coverage 判定语义与状态枚举 | `agentic/coverage.py:27`、`agentic/evidence_models.py:11` |

**边界说明**：Phase 1B 会修改 Coverage 的**输入**（改为 requirement-scoped view），但**不改判定语义与状态枚举**。这与上表不冲突——红线保护的是语义，不是调用点。

每个 Commit 必须重跑 `pytest` / L1 / L1.5。

---

## 13. 本轮明确不做（及理由）

02 第 72 节五条"禁止的错误实现"全部采纳，补充本文档的裁决：

| 不做 | 理由 |
|---|---|
| `max_chars: 28000 → 100000` | 治标。DeepSeek 已有 1M context，问题从来不是窗口不够，而是"所有阶段抢同一个 Context"且"证据在进 Context 前就被丢"。 |
| 新建 `ExplanationPlanner` 但输出仍是 `title/purpose/evidence_labels` | 换名不换结构，G1–G5 一条都不会改善。 |
| 只改 prompt"请像老师一样详细解释" | prompt 补不了"没有章节类型、没有心智模型字段、没有 why 评测"。G3 已证明评测口径决定产出形态。 |
| 解除字数限制后每个问题输出一万字 | `OutputBudget` 须由 `问题复杂度 + ExplanationPlan + 用户 depth + 模型能力` 共同决定。 |
| 为让答案更丰富而允许模型自由补项目事实 | **绝对禁止**。放宽的是通用教学知识，不是项目事实 grounding。见 §2.3。 |
| 新建 `TeachingAnswerResult` 类型 | 用扩展 `AgenticAnswerResult` + `final_cited_bundle` 替代，避免 CLI 分支与两套结果形状。见 §8.6。 |
| 保持 `C` 前缀 + 每 View 重编号 | 多一层映射 = 多一处静默错位，而 allowlist 校验正靠它。见 §6.4。 |
| Symbol Graph / Repo Map / LangGraph / Web UI / 增量索引 / L3 评测 | 02 第 65 节。留待 Release Gate 通过后，理由见 02 第 74 节。 |

---

## 14. 已识别的风险与坑

1. **Phase 1A 是本轮唯一触及 `agentic/` 的改动，风险最高。** 它同时改了检索结果的承载结构、增了 `EvidencePackage` 字段、动了 citation 正则。**必须独立 commit、必须用 `explain` 逐字快照守住零行为变更。** 若守不住，说明越界了。
2. **`explain` 与 `teach` 会长期并存。** `answer/planner.py` 降级为兼容适配器后，`DEPTH_LIMITS` 仍对它生效。**对策**：Phase 3 完成后立即 A/B 确认 `teach` 稳定胜出，再决定是否设为 default（02 第 39 节）。
3. **`answer_quality_runner._capture_arm` 依赖 `result.context_bundle`**（`evaluation/answer_quality_runner.py:124-127`，读 `context_chars` / `context_max_chars` / `context_truncated`）。§8.6 的 `final_cited_bundle` 方案让它继续工作，但**语义变了**：teach 路径下这几个字段度量的是"最终引用集合"而非"检索时上下文"。L2 报告里必须显式标注这个口径差异，否则跨模式比较会误导。
4. **Token 预算需要真实模型能力元数据。** `Settings`（`config.py:9-33`）没有 context window 字段。裁决：动态/配置化，**不要把 1M / 384K 硬编码进业务代码**。
5. **余票桶 Golden Case 依赖索引已建立。** `devcontext_code_root` 指向 `D:\Java-learning\12306Project\12306\my12306`（`config.py:17`），artifacts 被 gitignore，换机器需重新 ingest。
6. **多章节生成显著抬高调用数与成本。** 02 第 63 节预估 `detailed` 约 6–11 次调用。必须硬约束 `MAX_SECTIONS` / `MAX_OUTPUT_TOKENS` / `MAX_REVISION_ROUNDS = 1`。
7. **放宽 citation 正则会影响所有消费方。** `CITATION_PATTERN` 被 `strip_citations`（`:221`）与 `extract_citations`（`:262`）共用。改动前须确认无第三处硬编码 `C` 前缀。**已核对：`benchmark/` 下的既有基线是 L1/L1.5 检索评测，不含 citation label 断言，因此放宽正则不会让它们失效。**

---

## 15. 交付规范

每个 Phase 结束时必须产出一份《改动说明》：

1. **改了哪些文件** —— 逐个列出，附行号范围
2. **为什么这么改** —— 指向 §2 的 G1–G5，或 §3.5 的哪一处机制
3. **代价与取舍** —— 例如"Section-scoped 生成把 LLM 调用数从 2 提到 6–11"、"teach 路径 CLI 显示 `[E1]` 而非 `[C1]`"
4. **怎么验证** —— 可执行命令 + 预期输出，不能只写"运行测试"
5. **没有做什么，以及为什么** —— 逐条列举，理由必须是测量的或有出处的，不得写"时间不够"

文档正文与代码注释中的每一处事实陈述都必须给出 `类名#方法名:行号`（本仓库既有文档已保持这一粒度）。

---

## 16. 修订后的开发顺序

```
Phase 0    提交 docs 重组 + L1/L1.5/L2 基线 + Golden manifest
              ↓
Phase 1A   EvidencePool → EvidenceWorkspace + CitationRegistry
           （越过两道有损闸门，稳定证据身份）
              ↓
Phase 1B   Workspace → CoverageView / PlannerView / SectionView
           → TokenEstimator + TokenBudgetPolicy
              ↓
Phase 2    ExplanationPlan（mental model / section type / claim plan
           / confidence / assumptions）+ teach / deep 模式
              ↓
Phase 3    Fast Path + Section-scoped Deep Path → Composer → Citation Validation
              ↓
Phase 4    Teaching Reviewer（三档门控）+ targeted revision
           + Pedagogy Evaluation + explain vs teach 盲测
              ↓
Release Gate（含 §2.3 正确性门槛 与 §11 G4 条件推演判据）
              ↓
默认模式切换 → 再进入 Symbol Graph / Repo Map / Tool Agent
```

---

## 17. 一句话总结

本轮不改检索算法，只改**检索结果的承载方式**和**证据之后的解释方式**——先用一个在 `EvidencePool` 处冻结、带稳定 ID 的 EvidenceWorkspace 让证据不再在进 Context 前就被丢掉，再用一个被规划的 ExplanationPlan 决定讲什么顺序，最后用一套度量 why 覆盖率而非字数的评测决定它是否真的讲懂了。
