# 04 - Answer Quality V3 实现记录与功能说明

> 本文档记录 `02-Explanation Planner 与教学型回答重构开发计划.md` 的落地结果。
>
> | | |
> |---|---|
> | 基线提交 | `5d2d9f6` |
> | 本轮提交 | **17 个**（`010e3cb` → `6507b35`），已推送 Gitee + GitHub |
> | 测试 | **427 passed, 1 skipped**（基线 309 → 本轮新增 118 个测试，分布在 7 个新测试文件） |
> | 新增源码 | 16 个文件、2871 行；新增测试 1788 行 |
> | 覆盖文档 | 02 的 §0–§75 全部；执行中的偏离与发现见 `03-...md` 各 §x.x 小节 |

---

## 一、完成总览

| Phase | 内容 | 状态 | 提交 |
|---|---|---|---|
| 0 | 冻结基线 + 黄金用例 | ✅ 完成 | `010e3cb` `eb3c9cd` |
| 1A | EvidenceWorkspace + CitationRegistry | ✅ 完成 | `ea1eb66` |
| 1B | Coverage 解耦 + Token 预算 + `RETRIEVAL_FAILED` | ✅ 完成 | `e56ce7a` |
| 2 | ExplanationPlan + `teach` 模式 + Depth 契约 | ✅ 完成 | `68afc8b` |
| 3 | Fast/Deep 双路径 + Section-scoped 校验 + Composer | ✅ 完成 | `672cc79` |
| 4 | Reviewer V2 + 定向修订 + runner 泛化 + Pedagogy Judge | ✅ 完成 | `6c1b8f2` `0f4cbc0` `5428bfc` |
| 收尾 | 按类型写作 / token 预算接线 / Trace 升级 / A/B 开关 | ✅ 完成 | `bbf3fd1` `6507b35` |
| **§70** | **Release Gate 盲测 A/B** | ❌ **未运行** | — |

**一句话结论：四个 Phase 的功能全部落地并实测通过；唯一未完成的是 §70 定义的发布门槛本身——机器已就绪（CLI 开关已补），但 A/B 结果尚未产出。**

---

## 二、链路变化

### 动手前

```
UserRequest → EvidencePlanner → SearchActionPlanner → RetrievalController
  → EvidencePool（局部变量）
  → pool.select(top_k)             ← 第一道有损闸门
  → ContextBuilder(max_chars)      ← 第二道有损闸门：截断并 break，之后全部丢弃
  → CoverageChecker(读这个已截断的 bundle)
  → EvidencePackage(只带 bundle，不带 pool)
  → AnswerPlanner → 单次生成 → AnswerReviewer → Answer
```

三个后果：证据在进 Context 前就被丢掉；Coverage 判定受呈现预算影响；第二轮检索的种子也读被截断的 bundle。

### 现在

```
UserRequest → EvidencePlanner → SearchActionPlanner → RetrievalController
  → EvidencePool ─┬─→ EvidenceWorkspace + CitationRegistry（增量注册，绝不重编号）
                  │        │
                  │        ├─→ CoverageView（当轮快照，judge 前冻结）→ CoverageChecker
                  │        └─→ 第二轮种子来自 workspace，不再受第一轮截断影响
                  └─→ ContextBuilder（legacy，保留给 explain / L1 / L1.5）
  → freeze() → EvidencePackage{evidence_catalog, evidence_workspace}
  → ExplanationPlanner → ExplanationPlan
  → Fast Path（≤3 节）或 Deep Path（逐节生成，每节只喂自己的证据）
  → SectionComposer → TeachingReviewer（三档门控）→ 定向修订（≤1 轮）→ Answer
```

---

## 三、逐模块说明

### 3.1 EvidenceWorkspace：不再丢证据

| 文件 | 内容 |
|---|---|
| `context/workspace.py:17` | `EvidenceRef` |
| `context/workspace.py:62` | `EvidenceWorkspace` |

**为什么需要。** `ContextBuilder` 的预算是硬停：第一个装不下的 chunk 之后**全部丢弃**（`context/builder.py:64-81`）。余票桶问题涉及入口、Hash、Lua、初始化、TTL、失败补偿、取消订单、MySQL 兜底、锁、查询缓存十个方面，全部争抢同一个 28K 字符预算。

**关键设计：`EvidenceRef` 故意没有 `truncated` 字段。** 截断是**视图**的属性，不是证据的属性。把两者分开，正是 workspace 能持有超过任何单个 prompt 的原因。

**生命周期（`retrieval_controller.py:91,117,171,199`）。** 一次 `retrieve()` 建一个 workspace；每轮 `_execute` 后 `ingest(annotated, round_index)`；全部检索结束后 `freeze()`。四条不变式：

1. 每轮后从当前 pool 产出**当轮** Coverage 快照
2. `CitationRegistry` 全程增量注册
3. **绝不重新编号**（第二轮再找到同一 chunk 时复用原 id）
4. 全部检索完成后 `freeze()` 成不可变 `evidence_catalog`

**`for_round` 是累积语义，不是各轮互斥。** 第 1 轮的覆盖率判定必须能看到第 0 轮的证据——legacy 控制器判第 1 轮时用的就是整个 pool。互斥视图本身就是行为变更。真正要守的是**不向前泄漏**：第 0 轮看不见第 1 轮才召回的证据。

### 3.2 CitationRegistry：证据的稳定身份

| 文件 | 内容 |
|---|---|
| `context/registry.py:58` | `CitationRegistry`（`E1`/`E2`…） |
| `context/registry.py:20` | `EvidenceCatalog`（冻结后的不可变快照） |

**为什么需要。** `ContextBuilder` 按**位置**分配标签（`label = f"C{len(items) + 1}"`，`context/builder.py:54`），于是同一个 chunk 在 bundle A 里是 `C1`、在 bundle B 里是 `C7`。标签作为身份毫无用处——而 Phase 3 的 section-scoped 校验恰恰依赖身份。

**与 `C` 标签的关系。** 两个命名空间并存且不混淆：legacy / explain 继续用 `C`（按位置），teach 用 `E`（按身份）。`CITATION_PATTERN` **没有**被放宽成 `[CE]`——放宽会让 legacy 输出里的 `[E3]` 从"被忽略"变成"已用引用"，而 legacy bundle 只有 `C` 标签，`cli.py` 的 `citations[label]` 会直接 `KeyError`。改为新增 `EVIDENCE_CITATION_PATTERN`（`answer/generator.py:18`）并给 `strip_citations` / `extract_citations` 加默认值等于原值的 `pattern` 参数。

### 3.3 Views 与 Token 预算

| 文件 | 内容 |
|---|---|
| `context/views.py:37` | `CoverageView` 协议 |
| `context/views.py:48` | `WorkspaceCoverageView`（带 `round_index`） |
| `context/views.py:63` | `PromptContextView`（`to_bundle()` 交给既有消费方） |
| `context/views.py:100` | `build_context_view`（按 requirement **或** label 绑定） |
| `context/estimator.py:21,41` | `TokenEstimator` 协议 + `HeuristicTokenEstimator` |
| `context/budget.py:9,57` | `ModelCapabilities` + `TokenBudgetPolicy` |

**Coverage 只换输入源，不换调用结构。** `CoverageChecker.check(requirements, view)` 内部本来就是 requirement 分区的**单次批量调用**（`agentic/coverage.py:79`）。改动只是把输入从 `context.items` 换成 `view.items_for(rid)`——**严禁**拆成每 ER 一次调用，那会把成本乘以需求数（最多 6 倍）。

**预算从字符改为 token。** 一个字符数不是模型的属性：28,000 字符既可能撑爆小窗口，也可能让大窗口白白浪费。`TokenBudgetPolicy.decide()` 先给响应留出份额，剩下的才是上下文；`fixed_tokens` 是系统提示 + 问题 + 计划等不可裁剪部分，连它都装不下就判 `allowed=False`。

**已接线点：** teach 路径每个视图的上限由模型窗口推导（`explanation/workflow.py:_view_char_budget`）；窗口小到装不下任何东西是**合法结果**，返回空视图而非报错。

**估算必须可校准。** `pyproject.toml` 没有任何 tokenizer，所以 `HeuristicTokenEstimator` 刻意**高估**（CJK 一字一 token）。`compare_estimate()` 把估算与 API 实测配对记录，偏差被测量而非假设掉。

### 3.4 ExplanationPlan：把"怎么讲"变成被规划的产物

| 文件 | 内容 |
|---|---|
| `explanation/models.py:151` | `ExplanationPlan` |
| `explanation/models.py:118` | `ExplanationSection` |
| `explanation/models.py:80` | `ClaimPlan` |
| `explanation/models.py:20,37,51,59` | 14 种 section_type / 8 种 teaching_device / 四层 claim_type / 10 种 strategy |
| `explanation/planner.py:64` | `ExplanationPlanner.plan()` |
| `explanation/planner.py:96` | `parse_explanation_plan()`（严格校验） |
| `explanation/planner.py:276` | `fallback_explanation_plan()`（确定性兜底） |

**证据计划与解释计划的分工（`explanation/prompts.py`）：** EvidencePlan 是"我要查什么"；ExplanationPlan 是"我应该按什么认知顺序讲"。这是整轮要补的那个缺失决策。

**构造上强制的规则（`models.py:93`、`planner.py:161`）：**

| 规则 | 为什么 |
|---|---|
| `ExplanationPlan` 必须有非空 `core_mental_model` | 直击"没有贯穿全文的组织轴" |
| 章节标题不得等于任何 requirement 的 target | 命名即调查项，说明是复制而非规划 |
| `LOCATION_ONLY` 最多 2 节 | 简单定位问题不该被过度规划 |
| `NEGATIVE_CORRECTION` 必须含 `MISCONCEPTION` 节 | "纠正错误前提"的机制 |
| `PROJECT_FACT` 必须绑定证据 | 无证据的断言不是事实 |
| `ILLUSTRATIVE_EXAMPLE` 必须 `conditional=True` 且写明 `assumptions` | 假设案例不得伪装成项目真实行为 |
| `conditional` 的 claim 不得是 `CONFIRMED` | 条件判断不能宣称已确认 |
| `deep` 仅在 `teach` 可用 | 见 §3.7 |

### 3.5 生成：Fast / Deep 双路径

| 文件 | 内容 |
|---|---|
| `explanation/budget.py:44` | `budget_for()` → `OutputBudget` |
| `explanation/budget.py:25` | `FAST_PATH_MAX_SECTIONS = 3` |
| `explanation/writer.py:89` | `write()`（单次） |
| `explanation/writer.py:151` | `write_section()`（逐节，只喂该节证据） |
| `explanation/composer.py:29` | `SectionComposer.compose()` |

**分路判据：** `> 3 节` 或 `detailed`/`deep` 走 deep 路径。一次生成跨八节会失去论证的连贯性；逐节生成则让 Citation 变得可强制。

**Section-scoped Citation Validation（`writer.py:151`）。** 每一节只能使用**它自己绑定的标签**。用了 workspace 里存在的、但不属于本节的证据，记入 `invalid_citations` 而非放行。这把 Citation 从"引了一个存在的 chunk"升级为"这一节只能使用 Planner 指定的证据"。

**Composer 违反禁令时的处置是"丢弃"而非"修补"（`composer.py:41`）。** 若编排后的正文出现草稿里没有的 Citation，输出**整体回退**为章节草稿的确定性拼接（`_deterministic_join`，`composer.py:91`），不做局部替换。理由：能凭空造出一个 Citation 的调用，它在同一段文本里的其他判断也都不可信；局部修补等于挑着信。

**固定字数上限的废除。** `OutputBudget.max_output_tokens` 由"计划复杂度 + 模型能力"决定；`deep` 无上限。实测黄金用例产出 **9319 / 18524 / 18783 字**（三次不同运行），远超旧的 5000 字窗口，且这是结构正确带来的自然结果。

### 3.6 接地校验与 Reviewer

| 文件 | 内容 |
|---|---|
| `explanation/grounding.py:32` | `grounding_issues()`（确定性） |
| `explanation/reviewer.py:82` | `TeachingReviewer` |
| `explanation/reviewer.py:211` | `needs_llm_review()`（三档门控） |
| `explanation/reviewer.py:23` | `MAX_REVISION_ROUNDS = 1` |

**确定性与 LLM 互补，不是替代。** 确定性检查（零成本、不漏）负责可确定判定的四条：

| 检查 | 触发条件 |
|---|---|
| `PROJECT_FACT_WITHOUT_EVIDENCE` | 计划断言项目事实但该节没绑证据 |
| `CONDITIONAL_NOT_MARKED` | 计划含条件推演，正文没写成条件句 |
| `EXAMPLE_NOT_LABELLED` | 计划含假设案例，正文没标明这是假设 |
| `UNVERIFIED_WRITTEN_AS_FACT` | 章节证据未经验证，正文没有说明 |

LLM Reviewer 接在其后，负责需要读懂语义的部分（`MISSING_MENTAL_MODEL` / `MISSING_WHY` / `POOR_SCAFFOLDING` / `FACT_INFERENCE_CONFUSION` / `GENERAL_KNOWLEDGE_AS_PROJECT_FACT` / `SECTION_EVIDENCE_MISMATCH` / `UNHELPFUL_DETAIL` / `ABRUPT_TRANSITION` / `MISLABELED_EXAMPLE`）。

**同时接受两套 issue 词表（`reviewer.py:17`）。** 9 类是新增的，但既有 8 类保留（含 `UNSUPPORTED_CLAIM`）——上一轮的 legacy Reviewer **恰好抓到过一处真实过度声称**（把未索引的 `.lua` 说成"Lua 原子取还"）。一个说不出 `UNSUPPORTED_CLAIM` 的教学 Reviewer，会在它要替代的那个 Reviewer 更强的地方更弱。

**三档门控（`reviewer.py:211`）：**

| 路径 | 门控 |
|---|---|
| `brief` / `LOCATION_ONLY` | **只跑确定性检查**——一两句话的答案，让能改写它的 Reviewer 介入，风险大于收益 |
| `standard` | 确定性检查发现问题或计划含冲突时才上 LLM |
| `detailed` / `deep` | 必跑 |

**定向修订（`explanation/workflow.py:_revise`）。** Reviewer 输出 `section_issues` + `revision_required`，工作流**只重生成被点名的章节**，然后重新编排。最多一轮——第二轮是在审阅一次审阅。不让 Reviewer 替换一份基本没问题的答案。

**审稿失败不阻塞答案。** 调用异常或 JSON 畸形时返回 `accepted=True` + 记录 `error`。让答案的可用性依赖一个"职责就是可选"的组件，是把权衡放错了地方。

### 3.7 `teach` 模式与 Depth 契约

| 文件 | 内容 |
|---|---|
| `request.py:8` | `ANSWER_MODES = ("legacy","explain","teach")` |
| `request.py:9` | `TEACH_DEPTHS`（含 `deep`） |
| `request.py:22` | `depths_for()` |
| `request.py:30` | `EVIDENCE_SOURCE_BY_MODE` |
| `agentic/evidence_workflow.py:_answer` | 三分支显式分发 |

**`deep` 仅 teach 可用，且是跨字段校验。** `AnswerOptions("deep","explain")` 在构造时就抛。但守卫有一条**非显然的可达路径**：调用方不指定 depth、把深度交给 planner 决定，而 planner 返回了 `deep`——此时 `override is None` 会跳过覆盖检查。`explanation/planner.py:114` 拦的正是这条。

**未知模式不得静默落到 explain。** `_answer` 的三分支之后是 `raise ValueError`。静默 fallthrough 会让 depth 契约在上一层失效。

**`evidence_source` 按模式选择"什么算有证据"（`request.py:30`）。** `legacy`/`explain` → `bundle`；`teach` → `workspace`。**这个 gating 是必须的**：若无条件改成以 workspace 为准，legacy/explain 的行为会变，冻结的 L1.5 期望（如 `flow-06` 的 `EMPTY`）会跟着变。若不 gating，teach 路径下"检索 bundle 为空但 workspace 有证据"会被短路成"当前没有检索到足够的项目上下文"。

### 3.8 评测

| 文件 | 内容 |
|---|---|
| `evaluation/answer_quality_runner.py:41` | `PairwiseEvaluationConfig(baseline_mode, candidate_mode)` |
| `evaluation/answer_quality.py:24` | `NO_UPPER_BOUND_DEPTHS`（`deep` 无上限） |
| `evaluation/answer_quality.py` `TEACHING_FIELDS` | 5 个可选教学字段 |
| `evaluation/pedagogy.py:60` | `PedagogyScore` / `PedagogyJudge` |

**runner 泛化。** 原本硬编码 `V1_MODE`/`V2_MODE`，加第三种模式就得改 runner。现在 judge 只说 `BASELINE` / `CANDIDATE`，**评测对模式名保持盲**；runner 最后把结果映射回调用方的模式名。

**schema 迁移顺序（关键）。** `from_dict` 原本是 `set(value) != required` 的严格校验。教学字段以**可选 + 默认空**的方式加入，于是 18 个已冻结的案例原样加载；未知字段仍被拒（放宽不等于放开）。**数据集与其 loader 不能同时改形。**

**`deep` 不设字数上限。** 给 deep 设上限等于把这一轮废掉的东西又装回去。

**Pedagogy Judge 的 7 个维度：** 心智模型 / 因果解释 / 渐进展露 / 示例 / 失败推演 / 取舍 / 可读性。明确告诉评测**不要因为更长就给更高分**——长度正是让源码说明书通过的那个指标。只评 candidate 臂，该臂失败时跳过。

**未评上分不记 0 分。** 任一维度缺失时 `overall` 为 `None`，汇总只对真正评到分的案例取均值并单列 `errors`。把"没评成"记成 0，读出来是"这份回答没有教学价值"——比真相强得多。

### 3.9 Trace 与 `--debug`

| 文件 | 内容 |
|---|---|
| `agentic/models.py:205` | `AgenticTrace.teaching` |
| `explanation/workflow.py:_trace_block` | 教学生成块的组装 |
| `cli.py:_print_teaching_summary` | `--debug` 摘要 |

`--debug` 现在直接打印这轮做了什么：

```
Explanation Strategy:
PROBLEM_SOLUTION + CONCEPT_BUILDUP + TRADEOFF + FAILURE_ANALYSIS

Mental Model:
余票桶是购票准入用的近似令牌池，不是库存账本……

Context:
  Workspace evidence: 20
  Bound evidence: 14
  S1 view: 3
  S2 view: 5

Generation:
  9 sections
  0 targeted revision
```

`compression_events` **存在且为空**，不是缺失——读者必须能区分"没有发生"与"没有记录"。`teaching` 与 `explanation_plan` 只在 teach 路径出现，legacy / explain 的 trace 因此**逐字未变**。

---

## 四、关键设计决策与代价

| 决策 | 理由 | 代价 |
|---|---|---|
| Coverage 读 workspace 而非 bundle | 呈现预算不该影响"是否找到证据"的判定 | live 模式下 payload 变大，更容易触到 token 上限（已提上限，送摘要留待 Phase 5） |
| `E` 与 `C` 双命名空间并存 | 放宽单一正则会让 legacy 的 `[E3]` 变成 KeyError | 两套标签需分别维护 |
| `for_round` 累积语义 | legacy 判第 1 轮用的是整个 pool | 语义更绕，需专测"不向前泄漏" |
| Composer 违规即整体丢弃 | 造过 Citation 的调用不可局部信 | 编排收益可能被全部放弃 |
| 定向修订只重生成被点名章节 | 不让 Reviewer 替换基本没问题的答案 | Reviewer 的判断错误会保留下来 |
| Reviewer 失败视为通过 | 不让答案依赖可选组件 | 审稿静默失效时不易察觉（已记 `error`） |
| `evidence_source` 按模式 gating | 无条件改会改掉 legacy/explain 的行为 | 多一个模式维度需维护 |
| 修 `DEPTH_LIMITS`/`DEPTH_CHAR_RANGES` 留到 Phase 4 之后 | 它们管的正是 A/B 要对比的 explain 基线，同时改会混淆两个变量 | explain 路径仍带旧上限 |
| `EvidenceDigest` 推迟 | 1B/2 没有消费方，写了就是死代码 | coverage payload 仍送整块正文 |

---

## 五、实测证据

### 5.1 Coverage 的四次演进（本轮的意外主战场）

| 运行 | 结果 |
|---|---|
| Phase 0 基线 | ER1–ER6 全 `UNVERIFIED`，`check_error = CoverageCheckError` |
| Phase 2 | 全 `UNVERIFIED`，`RuntimeError: ... finish_reason=length` |
| Phase 3 | 全 `UNVERIFIED`，`CoverageCheckError: coverage evidence ids are invalid` |
| **Phase 4** | **ER1 PARTIAL，ER2–ER5 SATISFIED，无 `check_error`** |

四次是**三个不同的根因**：解析失败 → 输出被 token 上限截断 → 批次级全有或全无降级。

关键在第二次到第三次之间：Phase 1B 加的那条"异常消息保留在诊断通道"（`agentic/models.py:error_detail`）**是唯一让这条路可查的东西**——只记类名时，`finish_reason=length` 与真解析失败在 trace 里长得一模一样。第三次到第四次则印证了前一次诊断是对的：报错会变，说明 length 那个判断成立。

### 5.2 黄金用例的真实产出（余票桶，`--answer-mode teach --depth detailed`）

| 字段 | 实际值 |
|---|---|
| `core_mental_model` | "余票桶是购票准入用的近似令牌池，不是库存账本：它按'车次×区间'建桶、按'席别'放字段，宁可多发令牌让数据库条件更新兜底，也不因少发令牌造成有票买不到。" |
| `primary_strategy` | `PROBLEM_SOLUTION`；secondary = `CONCEPT_BUILDUP` / `TRADEOFF` / `FAILURE_ANALYSIS` |
| 章节 | **8–10 节**：`DIRECT_ANSWER → MENTAL_MODEL → CONCEPT×2 → EXECUTION_FLOW → MECHANISM → DESIGN_REASON → FAILURE_SCENARIO → BOUNDARY → SUMMARY` |
| 与需求的关系 | **6 条 requirement → 8–10 节，不是一一映射** |
| 教学手段 | 逐节不同（`TABLE` / `SMALL_FLOW_DIAGRAM` / `A_B_REQUEST_TRACE` / `PSEUDOCODE` / `COUNTER_EXAMPLE`），有节为 `NONE` |
| `likely_misconceptions` | 3 条，含"以为分桶维度是车厢、时间片或订单批次" |
| 篇幅 | 9319 – 18783 字（三次运行），远超旧上限 |
| 章节级证据强度 | `evidence_state` 在 CONFIRMED / PARTIAL 间区分 |

对照开工时的五个差距：G1 先立问题 → `DIRECT_ANSWER`/`PROBLEM_SETUP` 顺序解决；G2 心智模型 → `core_mental_model` 解决；G3 评测只量字数 → 结构判据 + Pedagogy 取代；G4 证据外推演 → 四层 `claim_type` + `assumptions` 解决；G5 自信与证据不符 → 章节级 `evidence_state` 解决。

### 5.3 回归证据

| 套件 | 结果 |
|---|---|
| `pytest` | 427 passed, 1 skipped |
| frozen L1.5 + regression（24 例） | 相对 Phase 0 基线**只有 `edge-01` 两处已知且有意**的差异（coverage 修复带来的正确状态变化） |
| L1 `evaluate` | `comparable`，验收集合一致，无超过噪声底的 recall/MRR 变化 |

`edge-01` 是本轮最好的例证：检索**找到了** ER1 的两条 ground-truth 证据组，8000 字符的答案 bundle **把两条都丢了**（`context_survival`: 找到 2 组、存活 0 组）。旧口径下 coverage 读 bundle 报 PARTIAL（错的）；现口径读 workspace 报 READY（对的）。

---

## 六、未完成项（逐条说明理由）

### 6.1 §70 的 Release Gate 盲测 A/B —— **未运行**

这是唯一「机器就绪、结论未出」的一项。

- **能力已具备**：`evaluate-answers --baseline-mode explain --candidate-mode teach`（`cli.py`，本轮补的开关）；judge 用 swapped-order 防位置偏差并给出 `v2_blind_win_rate_at_least_70pct` 门禁；Pedagogy Judge 独立出分。
- **为什么还没跑**：成本。每例两臂 × 若干 LLM 调用 + 盲测 + 教学评分；19 例全量很贵。建议先跑单例 `--only teach-token-bucket-01`。
- **它是硬门槛**：`teach` 切成默认模式之前必须满足。

### 6.2 §70 的人工检查清单 —— 未做

余票桶 / 购票一致性 / 双请求抢同一座位 / 订单失败补偿 / 一个 locate / 一个 negative。这是发布前的人工判断，不是自动化能替代的。

### 6.3 §18⑥ / §19 `EvidenceDigest`（含 §54 的 digest 测试）—— 有意推迟

**不做，不是因为没时间**：digest 只在"视图已按 token 预算构建、需要压缩超长正文"时才有意义，而那个溢出路径在 Phase 1B/2 不存在。**现在写就是死代码。**

**连带后果（必须说清）**：coverage 的 payload 仍送**整块正文**而不是摘要——这正是它偏大的原因。本轮把 token 上限从 4096 提到 32768 只是让它**不再越界**，没有让它**变小**。这条与 §7.5 的 `PromptContextView` / 摘要路径一起属下一阶段。

### 6.4 §7 的"三种 View"没有做成三个类 —— 有意偏离

文档写的是 Planner View / Section View / Reviewer View 三个概念。实现为**一个参数化 builder**（`context/views.py:build_context_view`），差异只体现在传入的是 requirement id 还是 evidence label、以及预算大小。

**理由**：三者除"传哪些 id"之外没有区别，三个近似类只会带来三份需要同步维护的代码。功能等价，测试覆盖的是同一组行为（`tests/test_context_views.py`）。这是**偏离措辞而非偏离意图**。

### 6.5 说明：§67 的执行偏差

§67 要求"每个 Commit 都必须执行 pytest + 相关新单元测试 + **至少一个真实 CLI 问题**"。

- `pytest`：**每个 commit 前都跑了**，无例外。
- 真实 CLI 问题：在 Phase 2 / 3 / 4 之后各跑了一次完整 teach 实测（余票桶）。**Phase 1A / 1B / 4A / 4B 之后没有单独跑**——1A/1B 靠单元测试 + 冻结 L1.5 逐案零差异守住（对纯重构而言这是更强的证据），4A/4B 的改动被随后的 Phase 4 实测覆盖。

---

## 七、如何运行与验证

### 7.1 环境前置（两个必须）

```bash
# Docker Desktop 需已启动
docker compose up -d postgres

# 必须绕过代理：否则 DashScope embedding 全废、检索静默返回空
export NO_PROXY="localhost,127.0.0.1,::1,.local,.aliyuncs.com"
```

第二条不是可选项。本机 `HTTPS_PROXY` 指向本地代理，会让 `curl` 对 `dashscope.aliyuncs.com` 的 TLS 握手失败；**失败是静默且误报的**——keyword 仍可用，vector/hybrid 全废，而输出是 `Sufficiency: insufficient` + "当前没有检索到足够的项目上下文"，读起来像"知识库没有这份证据"。故障留档见 `artifacts/answer-quality-v2-baseline/PROXY-FAILURE-token-bucket.explain.txt`。

### 7.2 离线快速验证（秒级）

```bash
uv run pytest
```

预期 `427 passed, 1 skipped`。

```bash
uv run devcontext evaluate-retrieval-workflow --suite all --mode frozen --output artifacts/l15.json
```

冻结模式**零 LLM 成本**：固定 query 回放 + oracle 判定，端到端跑真实 `retrieve()`。任何检索行为变化都会在 24 例里暴露。预期 `quality_passed: true`，`false_ready_count: 0`。

### 7.3 检索层验证

```bash
uv run devcontext evaluate
```

预期 `baseline_comparison.status == "comparable"`；注意 L1 **不是逐位可复现的**，MRR 有约 ±0.015 的噪声底。

### 7.4 教学型回答（主要验收）

```bash
uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode teach --depth detailed --debug
```

约 5 分钟（8–10 节 + 编排 + 审稿）。**先看 `--debug` 顶部那段摘要**，它比读全文更快看出这轮做了什么：

- `Mental Model` 应是一句可复述的话，而不是某一节的结论
- `Context` 里 `Workspace evidence` 应明显大于 `Bound evidence`，且各 `Sn view` 明显更小
- `Generation` 的节数应**不等于** requirement 数

### 7.5 契约验证（各一条命令）

```bash
# deep 仅 teach 可用：下面这条应报错
uv run devcontext ask "余票桶在哪个类里？" --answer-mode explain --depth deep
```

```bash
# deep 在 teach 下可用，且不受字数上限约束
uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode teach --depth deep --debug
```

```bash
# 三种模式对照（同一问题）
uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode explain --depth detailed
```

### 7.6 Release Gate（尚未运行）

```bash
# 建议先跑单例
uv run devcontext evaluate-answers --baseline-mode explain --candidate-mode teach \
  --only teach-token-bucket-01 --output artifacts/l2-teach-vs-explain.json
```

```bash
# 全量 19 例（贵）
uv run devcontext evaluate-answers --baseline-mode explain --candidate-mode teach \
  --output artifacts/l2-full.json
```

**报告里该看什么：**

| 字段 | 预期 |
|---|---|
| `modes` | `{"baseline": "explain", "candidate": "teach"}` |
| `judge_winners` | 用模式名（`explain` / `teach`），不是 `V1`/`V2` |
| `acceptance` 的 `v2_blind_win_rate_at_least_70pct` | `passed` 需为 `true`，即 candidate 在决出胜负的案例里赢率 ≥ 70% |
| `pedagogy.mean` | 七个维度出分；未评上分的案例计入 `errors` 而**不是 0 分** |
| `pedagogy.errors` | 若大于 0，说明评测调用本身失败，此时**不应**把 mean 当作结论 |

**还有一件最该看的**：`final_coverage`。若又出现全部 `UNVERIFIED`，读 `check_error` 里带的消息——保留异常消息正是为这一刻加的。

---

## 八、改动清单

### 新增源码（2871 行）

```
src/devcontext/context/registry.py        124   CitationRegistry / EvidenceCatalog
src/devcontext/context/workspace.py       228   EvidenceRef / EvidenceWorkspace
src/devcontext/context/views.py           189   CoverageView / PromptContextView / build_context_view
src/devcontext/context/estimator.py        78   TokenEstimator / HeuristicTokenEstimator
src/devcontext/context/budget.py          120   ModelCapabilities / TokenBudgetPolicy
src/devcontext/explanation/models.py      237   ExplanationPlan / Section / ClaimPlan
src/devcontext/explanation/planner.py     458   ExplanationPlanner + 严格校验 + 确定性兜底
src/devcontext/explanation/prompts.py      79   Planner 系统提示 + 枚举块
src/devcontext/explanation/writer.py      208   TeachingWriter（单次 / 逐节）
src/devcontext/explanation/composer.py    101   SectionComposer + 确定性回退
src/devcontext/explanation/grounding.py    86   确定性接地校验
src/devcontext/explanation/reviewer.py    225   TeachingReviewer + 三档门控
src/devcontext/explanation/budget.py       76   OutputBudget / budget_for
src/devcontext/explanation/workflow.py    465   TeachingExplanationWorkflow
src/devcontext/explanation/__init__.py     69   导出
src/devcontext/evaluation/pedagogy.py     128   PedagogyScore / PedagogyJudge
```

### 主要修改

| 文件 | 改动 |
|---|---|
| `agentic/retrieval_controller.py` | 持有 workspace；Coverage 改读 view；第二轮种子改读 workspace；错误消息保留 |
| `agentic/coverage.py` | 输入改为 view；保留异常消息；evidence id 非法时过滤而非拒批 |
| `agentic/evidence_models.py` | `evidence_catalog` + `evidence_workspace` 字段；`RETRIEVAL_FAILED` 状态 |
| `agentic/evidence_workflow.py` | `_AnswerOutcome`；三分支显式分发；`_answer_teach` |
| `agentic/models.py` | `error_detail()`；`EvidenceStatus.state`；`AgenticTrace.teaching` |
| `answer/generator.py` | `RETRIEVAL_FAILED_ANSWER`；`EVIDENCE_CITATION_PATTERN`；`pattern` 参数 |
| `answer/models.py` | `TEACHING_ISSUE_TYPES` |
| `context/builder.py` | 抽出 `render_items()`（`build()` 行为逐字未变） |
| `evaluation/answer_quality.py` | `PairwiseEvaluationConfig` 前身的 judge 泛化；教学字段；`deep` 无上限 |
| `evaluation/answer_quality_runner.py` | `PairwiseEvaluationConfig`；Pedagogy 接入 |
| `evaluation/retrieval_workflow_runner.py` | oracle 改读 view；`false_ready` 拆分 |
| `cli.py` | `teach`/`deep` 选项；teach 客户端工厂；`--debug` 摘要；A/B 开关 |
| `request.py` | `teach` / `deep` / 跨字段校验 / `evidence_source` |
| `models.py` | `EvidenceConflict` 上移（避免 explanation → answer 的环） |
| `config.py` | `model_capabilities()`；有效窗口配置 |

### 新增测试（1788 行）

```
tests/test_evidence_workspace.py     230   registry + workspace
tests/test_context_views.py          246   view + estimator + budget
tests/test_explanation_planner.py    271   plan 校验 + 兜底
tests/test_teaching_workflow.py      193   分发 + 契约
tests/test_section_generation.py     400   分节 / 校验 / 预算 / 接地 / trace
tests/test_teaching_reviewer.py      315   审稿解析 + 门控 + 定向修订
tests/test_pedagogy.py               133   教学评分
```

---

## 九、下一步

1. **跑 §70 的盲测 A/B**（先单例，再全量），这是 `teach` 转默认的唯一硬门槛。
2. **按 §70 清单人工检查六个案例。**
3. **`EvidenceDigest` + coverage payload 送摘要**（§18⑥ / §19 / §7.5），解决"payload 偏大"这个已知未解问题。
4. **`DEPTH_LIMITS` / `DEPTH_CHAR_RANGES` 的最终处置**——A/B 完成后才动，否则会混淆变量。
5. 之后才是 `01` 里的 Symbol Graph / Repo Map / Tool-driven Agent。
