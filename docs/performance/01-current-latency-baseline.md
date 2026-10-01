# DevContext 回答延迟基线（Performance Baseline v1）

> 本轮只建立基线，**不含任何优化**。所有数字来自真实运行，不是静态推测。
> 复现命令见 §3.1；**必须先满足 §2.1 的代理条件**，否则数据整体作废。
>
> **口径（重要）**：9 个 case × 3 次 = 27 个样本，单机、单次会话、n=3。
> 每次 `ask` 都是独立 Python 进程，样本之间不共享客户端/连接/缓存，
> 因此本文一律用 `first_sample` / `repeat_sample`，**不使用「冷启动/热运行」**。
> n=3 的 p50 只能说明量级，不能当作稳态分布。

---

## 1. 当前完整调用链（teach 模式）

以下路径逐跳对照代码确认，非推测。

```
cli.py:613  ask 分支
  └─ cli.py:492 _planned_workflow()          answer_mode == "teach" 时构造 TeachingExplanationWorkflow
      └─ evidence_workflow.py:85 EvidenceDrivenWorkflow.run()
          ├─ retrieval_controller.py:78 RetrievalController.retrieve()
          │   ├─ :84  EvidencePlanner.plan()                          ← LLM
          │   ├─ :109 SearchActionPlanner.plan_actions(round_index=0) ← LLM
          │   ├─ :124 _execute(round 0)
          │   │      └─ :253 RetrievalPolicy.search_scope_with_trace()
          │   │             └─ retrieval/service.py:63 BailianEmbeddingClient.embed_query()  ← Embedding
          │   ├─ :133 _build_context()
          │   ├─ :140 CoverageChecker.check(round 0)                  ← LLM（最多 2 次尝试）
          │   └─ 【条件】:163 仅当存在 CORE 需求为 PARTIAL/MISSING：
          │         :165 plan_actions(round_index=1) → :183 _execute → :191 _build_context
          │         :195 CoverageChecker.check(round 1)
          └─ evidence_workflow.py:328 _answer_teach()
              └─ explanation/workflow.py:96 TeachingExplanationWorkflow.run()
                  ├─ :106 ExplanationPlanner.plan()                    ← LLM
                  ├─ :134 分支
                  │     ├─ multi-pass → :135 _write_in_sections()
                  │     │      :295 for section in plan.sections        ← 每节一次 LLM
                  │     │      :317 SectionComposer.compose()           ← LLM（第 1 次）
                  │     └─ fast      → :140 TeachingWriter.write()      ← LLM
                  ├─ :148 stage teaching_draft（**覆盖上面全部**）
                  └─ 【条件】:157 仅当 drafts 且 needs_llm_review(plan, issues)：
                        :159 TeachingReviewer.review()                  ← LLM
                        【条件】:164 仅当 review.revision_required：
                          :166 _revise()  ← 按节重写，每节一次 LLM
                          :170 SectionComposer.compose()  ← **第 2 次** Composer
```

**四点与直觉不同的地方**（均已核对代码）：

1. `EvidencePlanner` / `SearchActionPlanner` **不是独立节点**，由 `RetrievalController.retrieve()` 内部调用；
   同一个 planner 对象在 round 0 与 round 1 各调用一次。
2. **round 0 / round 1 不是循环**，是 `retrieval_controller.py:108-152` 与 `:163-207` 两段手写 inline 代码块。
3. **Fast 与 multi-pass 不是同一个循环**：fast 走 `writer.write()`（`workflow.py:140`）；
   multi-pass 走 `_write_in_sections()`（`:295`）逐节生成后 Compose。判据是 `budget.allow_multi_pass`。
4. **Composer 可以执行两次**：首次在 `_write_in_sections` 内（`:317`，被 `teaching_draft` 覆盖），
   修订后再次执行（`:170`，被 `teaching_revision` 覆盖）。因此 **Composer 没有自己的顶层 stage**，
   它的真实成本只能从 LLM 调用级数据看到（见 §4.4）。

### 1.1 计时层级与「不得相加」的约定

| 层级 | 载体 | 是否参与 `unattributed` 求和 |
|---|---|---|
| 顶层 stage（划分整个请求，互斥） | `StageUsage`（`agentic/models.py:13`） | **是，且仅此一层** |
| 单次 LLM 调用 | `LLMCallTrace` | 否（子级） |
| 单节生成 / 修订 | `SectionExecutionTrace` | 否（子级，被 `teaching_draft` 覆盖） |
| 单次 embedding | `EmbeddingCallTrace` | 否（子级） |
| 单个检索动作 | `RetrievalActionTrace` | 否（子级） |

顶层 stage 白名单见 `observability/report.py:TOP_LEVEL_STAGES`。

---

## 2. 测试环境

| 项 | 值 |
|---|---|
| Git commit | `3b9bde337912330cffb4292ca774f107c93231f3` + 本轮未提交改动 |
| Python | 3.13.15（`uv`） |
| 平台 | Windows 11 (10.0.26100) |
| 回答模式 | `teach`（默认） |
| 深度 | **不强制**，由 `ExplanationPlanner` 自选（见 §4.1 的口径说明） |
| LLM | `deepseek-flash` @ `https://api.deepseek.com`，context 131072 / max output 32768 |
| Embedding | `text-embedding-v4`，dims 1024，`EMBEDDING_TRANSPORT=curl` |
| 向量库 | `devcontext-postgres`（pgvector 0.8.6 / PG18），`127.0.0.1:5432` |
| 语料 | 3112 chunks，全部带 embedding（CODE 538 / DOCUMENT 2574），未重新 ingest |
| 采集日期 | 2026-10-01 |
| 每 case 次数 | 3（`--repeat 3`，单进程，`first_sample` + `repeat_sample` ×2） |
| API Key | 未记录 |

### 2.1 代理前置条件（**复现本基线必须先满足**）

本机 `HTTP_PROXY` / `HTTPS_PROXY` = `http://127.0.0.1:12334`（hiddify），`NO_PROXY` 默认不含 `aliyuncs.com`。
2026-10-01 实测两个 endpoint 表现**不一致**：

| endpoint | 走代理 | 绕过代理 |
|---|---|---|
| `api.deepseek.com` | `401`（通） | `401`（通） |
| `dashscope.aliyuncs.com` | `000`（**不通**） | `400`（通） |

采集命令一律带 `NO_PROXY`（见 §3.1）。不加的后果不是报错而是**静默降级**：
retrieval 全部失败，用户侧显示「没有检索到足够的项目上下文」，与「知识库确实没有证据」无法区分。
本次 162 个 retrieval action **全部成功、0 失败**，即采集期间代理条件始终满足。

---

## 3. 测量方法

### 3.1 采集命令

```bash
NO_PROXY="localhost,127.0.0.1,::1,.local,.aliyuncs.com" \
  uv run devcontext ask "<问题>" --answer-mode teach --repeat 3 \
  --perf-json artifacts/perf/<case>.json
```

### 3.2 口径

- **`total_latency_ms`**：`workflow.run()` 前后 wall clock，不含 CLI 打印。
- **token**：`input_tokens` / `output_tokens` 一律为 API `usage` 返回的 **actual** 值；
  按字符估算的只有 `final_answer_tokens_estimated`，字段名显式带 `estimated`（C7）。
- **`discarded_llm_latency_ms`**：`success=False` 或 `wasted=True` 的调用耗时之和。
  `wasted` 由 `mark_last_call_wasted()` 在 6 个丢弃点标记（三个 planner 的 fallback、
  coverage 重试、composer 两条丢弃分支、reviewer fallback）。
- **`repeated_embeddings`**：同一请求内同一 query 被 embed 次数 > 1 的项。

### 3.3 行为不变性

按 C2 用 Mock LLM 验证：`tests/test_perf_trace.py::TestInstrumentationChangesNothing` 断言
开关 tracing 时答案文本、citations、`stats["path"]`、`stats["trace"]`（除新增 `section_execution` 外）
逐键相等。全套测试 **451 passed, 1 skipped**（其中 24 条为本轮新增）。

---

## 4. 实测结果

### 4.1 案例与总体性能

9 个 case × 3 次 = **27 个样本，0 次失败**。

| case | 类 | 轮次 | 路径 | sections | revisions | LLM 调用 | min | **p50** | max |
|---|---|---|---|---|---|---|---|---|---|
| `locate-01` | A | 1 | fast | 0 | 0 | 5 / 5 / 5 | 24.5 | **26.3** | 31.4 |
| `locate-02` | A | 1 | fast | 0 | 0 | 5 / 7 / 5 | 36.1 | **45.1** | 65.5 |
| `why-02` | B | 2 | multi_pass | 7 | 0 | 15 / 20 / 15 | 232.6 | **294.6** | 405.0 |
| `negative-02` | B | 2 | multi_pass | 7 | 0 | 14 / 7 / 18 | 100.9 | **209.9** | 312.6 |
| `flow-06` | B | 2 | fast | 0 | 0 | 7 / 16 / 7 | 100.4 | **125.5** | 273.8 |
| `teach-token-bucket-01` | C | 2 | multi_pass | 10 | 0 | 18 / 19 / 7 | 132.0 | **353.2** | 397.3 |
| `flow-01` | C | 2 | multi_pass | 11 | 3 | 23 / 7 / 20 | 113.7 | **435.9** | 471.1 |
| `edge-03` | D | 2 | multi_pass | 4 | 0 | 11 / 18 / 11 | 192.5 | **195.9** | 320.3 |
| `flow-05` | D | 2 | multi_pass | 6 | 1 | 16 / 7 / 7 | 100.3 | **113.6** | 240.2 |

**总体：min 24.5s / p50 192.5s / max 471.1s / mean 198.1s。**
即「一个较复杂问题 3–5 分钟」的说法被证实，但**最简单的定位类问题也要 25–65 秒**。

> **口径说明（重要）**：`--depth` 未强制，深度由 `ExplanationPlanner` 自选。
> 因此**不能**按 l2 语料里 `answer_depth` 的字面值给本表分类——
> 语料标记为 `standard` 的 `why-02` 三次全部走了 multi-pass 并生成 7 节。
> 「路径」列由实测 `sections > 0` 判定，这才是本轮可用的事实。

**轮次（C5）**：27 个样本中 **22 个进入了 round 1**。两个 D 类候选
（`edge-03`、`flow-05`）在全部 6 个样本里都出现了 `round_index=1`，**候选条件成立**，
可以代表「第二轮成本」。仅 `locate-01` / `locate-02` 三次都是单轮。

### 4.2 三次重复之间的差异极大（最值得注意的一条）

同一问题、同样参数，路径与耗时都可能不同：

| case | sample0 | sample1 | sample2 | 倍数 |
|---|---|---|---|---|
| `flow-01` | 471.1s（11 节） | **113.7s（3 节，fast）** | 435.9s（8 节） | **4.1×** |
| `teach-token-bucket-01` | 397.3s（10 节） | 353.2s（11 节） | **132.0s（3 节，fast）** | **3.0×** |

差异的来源是 **`planned_section_count`**（见 §4.6 相关性）。这意味着：
**当前延迟不是由问题难度决定的，而主要由 Planner 这一次打算写几节决定的。**

### 4.3 各阶段耗时（顶层 stage，27 个样本汇总）

| stage | 出现次数 | 均值 | 最大 | 占总耗时 |
|---|---:|---:|---:|---:|
| `teaching_draft` | 27 | 74.8s | 240.0s | **37.7%** |
| `explanation_planning` | 27 | 39.9s | 75.3s | **20.1%** |
| `coverage_check` | 49 | 12.4s | 26.2s | 11.4% |
| `teaching_review` | 11 | 53.8s | 120.0s | 11.1% |
| `teaching_revision` | 7 | 62.2s | 97.0s | 8.1% |
| `search_action_planning` | 49 | 6.2s | 18.2s | 5.6% |
| `evidence_planning` | 27 | 7.8s | 17.1s | 3.9% |
| `evidence_retrieval` | 49 | 2.2s | 4.2s | 2.0% |

`coverage_check` / `search_action_planning` / `evidence_retrieval` 出现 49 次 =
27 个样本 × round 0，再加 22 个 round 1。

**计时完整性**：27 个样本的 `unattributed` 最大 **23.5 ms**，最大占比 **0.0100%**，远超 spec §15 的
`< 5%` 目标。顶层 stage 真的划分了整个请求，没有漏计的时段。

### 4.4 LLM 调用级（Composer 只在这里可见）

27 个样本共 **315 次 LLM 调用**（均值 11.7 次/样本，最大 23 次）。

| stage | 调用数 | 合计 | 单次均值 | 占总 LLM 时间 |
|---|---:|---:|---:|---:|
| `teaching_draft` | 131 | 1763.4s | 13.5s | 33.6% |
| `explanation_planning` | 27 | 1076.7s | 39.9s | 20.5% |
| **`composer`** | 21 | 690.4s | **32.9s** | **13.2%** |
| `coverage_check` | 49 | 607.4s | 12.4s | 11.6% |
| `teaching_review` | 11 | 591.9s | **53.8s** | 11.3% |
| `search_action_planning` | 49 | 301.7s | 6.2s | 5.8% |
| `evidence_planning` | 27 | 210.7s | 7.8s | 4.0% |
| **合计** | **315** | **5242.2s** | — | 100% |

**Composer 单次 32.9s，比任何单节写作都贵**（13.5s），且它在 spec §11 里被当作次要项。
**Reviewer 单次 53.8s，是全场最贵的单次调用。**

**Sequential LLM Depth（spec §23.2）**：当前链路全程同步、无并发，
因此**串行深度 = 该次请求的 LLM 调用总数**。实测最大 **23 层**
（最慢的 `flow-01` 样本：规划 6 次 + 逐节写作 11 次 + Composer 1 + Reviewer 1 + 逐节修订 3 + Composer 1）。
这 23 层里每一层都必须等上一层返回，正是 §4.7 中 84% 集中在写/评/修三块的成因。

tokens：输入 1,356,696 / 输出 1,121,237。
**Token 放大 ≈ 8.8×**（全部 LLM 输入 token ÷ 最终答案估算 token）——
为了产出 217,779 字符的答案，内部消耗了约 247 万 token。

### 4.5 Retrieval 拆分（spec §5 的答案）

162 个 retrieval action 全部成功，合计 107.0s：

| 环节 | 合计 | 占 action 总耗时 |
|---|---:|---:|
| Embedding API | 69.7s | **65.2%** |
| Keyword SQL | 25.5s | 23.9% |
| Vector SQL | 11.7s | 10.9% |
| Python Fusion | 0.0s | 0.0% |

**结论：Retrieval 慢在 Embedding API，不在 PostgreSQL，也不在 Fusion。**
Fusion 耗时是 0，Vector SQL 只占 11%。

但必须同时看到量级：**整个 retrieval 只占总耗时的 2.0%**。
即使把 embedding 优化到 0，端到端也只快约 1.3%。

**重复 Embedding**：27 个样本中 **2 个样本**出现重复（`teach-token-bucket-01` sample2、
`flow-01` sample1，各 4 次重复调用、2 个不同 query）。
成因是混合作用域动作（`retrieval/policy.py:43-49` 先 CODE 后 DOCUMENT，各自在
`retrieval/service.py:63` 独立 embed）。**这是依赖作用域的偶发现象，不是通例** ——
其余 25 个样本为 0。本轮只记录，不修复（spec §6）。

### 4.6 什么和总耗时正相关（27 个样本）

| 维度 | 与总耗时的相关系数 |
|---|---:|
| `llm_calls` | **+0.98** |
| LLM 输出 token | **+0.98** |
| `sections` | **+0.94** |

三者高度共线，本质上指向同一件事：**总耗时 ≈ LLM 调用次数 × 单次耗时**。
而调用次数主要由 `planned_section_count` 决定（每节一次写作调用）。

### 4.7 Critical Path

**`flow-01` 最慢样本（471.1s，11 节，3 次修订）**：

```
evidence_planning                8.5s   1.8%
search_action_planning r0        4.3s   0.9%
evidence_retrieval r0            3.1s   0.6%
coverage_check r0                4.5s   1.0%
search_action_planning r1        6.3s   1.3%
evidence_retrieval r1            0.7s   0.1%
coverage_check r1               13.6s   2.9%
explanation_planning            34.4s   7.3%
teaching_draft                 240.0s  50.9%
teaching_review                 75.8s  16.1%
teaching_revision               79.9s  17.0%
------------------------------------------------
TOTAL                          471.1s
```

检索 + 规划前端合计 **12.3%**；写作 / 评审 / 修订三块合计 **84.0%**。

**`teach-token-bucket-01` sample0（397.3s，10 节）**：

```
explanation_planning            45.5s  11.4%
teaching_draft                 174.7s  44.0%
teaching_review                120.0s  30.2%   ← discarded_llm_latency = 120.0s
（检索前端合计约 12.2%）
```

这一条暴露的情形比 spec §8 描述的更具体：**Reviewer 调用在 `timeout_seconds=120`
的客户端超时上限处被掐断**（`llm/deepseek.py:22`），调用**根本没有返回**
（`finish_reason=None`，错误为 `DeepSeek request timed out after 120 seconds`），
评审被跳过，`revisions=0`，答案在没有有效评审的情况下直接发出 —— 代价是 2 分钟。
§4.8 的两个失败样本之一是它。

### 4.8 Wasted LLM Latency

| 指标 | 值 |
|---|---|
| 合计 | **693.6s** |
| 每样本均值 | **25.7s** |
| 单样本最大 | **120.0s** |
| 出现丢弃的样本 | **21 / 27** |

**约 78% 的请求都存在被丢弃的 LLM 时间，平均每次请求白烧 25.7 秒。**

315 次调用中有 **25 次被标记为 wasted**（7.9%），**2 次直接失败**，两次的成因不同且都可定位：

| 成因 | 位置 | 耗时 | 证据 |
|---|---|---:|---|
| **客户端超时** | `teaching_review`（`teach-token-bucket-01` s0） | 120.0s | `finish_reason=None`，`DeepSeek request timed out after 120 seconds` |
| **输出被截断** | `search_action_planning`（`flow-06` s2） | 18.2s | `finish_reason=length` |

前者说明 `timeout_seconds=120` 是当前**实际会触发**的上限，且触发时该阶段完全没有产出；
后者说明 `finish_reason=length` 这条路径在本轮真实出现过（正是 spec §23.4 列出的情形之一）。

### 4.9 逐节生成

27 个样本共写出 **119 节**（含 14 次修订重写）：单节均值 **13.7s**，最大 **28.5s**，合计 1630.9s。

---

## 5. 瓶颈排序（**依据本次实测，未采用 spec §26 的预设顺序**）

| 顺位 | 瓶颈 | 实测耗时 | 占比 | 是否在关键路径 | 性质 |
|---|---|---|---|---|---|
| **P0** | `teaching_draft`（逐节串行写作） | 均值 74.8s，最大 240.0s | 37.7% | 是 | 串行累加，节数由 Planner 决定 |
| **P1** | `explanation_planning` | 均值 39.9s，最大 75.3s | 20.1% | 是 | 单次调用，且**输入很少却输出很大** |
| **P2** | `composer`（隐藏在两个 stage 内） | 均值 32.9s/次 × 21 次 | 13.2% | 是 | 最贵的单次调用之一 |
| **P3** | `coverage_check` | 均值 12.4s × 49 次 | 11.4% | 是 | 轮 0 / 轮 1 各一次，且会重试 |
| **P4** | `teaching_review` | 均值 53.8s × 11 次 | 11.1% | 条件执行 | 最贵单次调用；且有 120s 全废的记录 |
| **P5** | `teaching_revision` | 均值 62.2s × 7 次 | 8.1% | 条件执行 | 串行逐节重写 |
| — | 检索整个环节 | 均值 2.2s × 49 次 | 2.0% | 是 | **不是瓶颈**；其中 embedding 占 65.2% |
| — | 被丢弃的 LLM 时间 | 每样本 25.7s | ≈13% | 是 | 与上面各项重叠，单独可回收 |

**与 spec §26 预设顺序的差异（必须指出）：**
spec 猜 `P0 Section Writer / P1 Coverage / P2 Composer / P3 Reviewer / P4 ExplanationPlanner / P5 Retrieval`。
实测是 **`ExplanationPlanner` 排第 2（不是第 4）**、`Coverage` 排第 4（不是第 1），
而 **Retrieval 根本不是瓶颈（2%）**。这正是本轮不做基于猜测优化的理由。

---

## 6. 下一轮优化候选（**本轮不实现**，spec §27）

按「预期 wall-clock 收益」排序，供下一轮选型：

1. **让节数可控**（最高杠杆）。总耗时与 `sections` 相关系数 +0.94，
   同一问题因节数不同可差 4 倍。方向：让 `ExplanationPlanner` 的节数与问题复杂度建立稳定关系，
   或按深度设上限。
2. **处理 wasted latency**。21/27 样本存在丢弃，均值 25.7s/样本。
   方向：Planner 输出校验前置（先校验结构与字段再解析全文）、Reviewer 失败后的降级策略。
3. **Section 并行**。`_write_in_sections`（`workflow.py:295`）是纯串行，
   节与节之间只共享 `core_mental_model`，无数据依赖。这是 §4.7 中 50.9% 的直接来源。
4. **Reviewer 条件化 / 降级**。单次 53.8s 是全场最贵；11 次评审带来 7 次修订
   （命中率不低，说明它确实在工作，不能简单关掉），但其中一次花满 120s 输出全废、
   最终没有产生任何修订。方向应针对**失败时的降级与超时预算**，而非直接去掉评审。
5. **Composer 确定性化或轻量化**。单次 32.9s，任务只是重排与过渡；
   spec §11 已提出 `deterministic_join` 这条现成退路（`composer.py:91`）。
6. **Embedding batch / cache**。仅对 retrieval 的 2% 有效，收益有限；
   且重复 embedding 只在混合作用域下偶发（2/27）。**优先级最低。**

**明确不列入**：更换默认模型、降低回答质量、修改 Benchmark 语义（spec §1）。

---

## 7. 本轮代码改动说明

| 文件 | 改动 |
|---|---|
| `src/devcontext/observability/trace.py`（新增） | `LLMCallTrace` / `EmbeddingCallTrace` / `RetrievalActionTrace` / `SectionExecutionTrace` / `PerfRecorder`，仅依赖 stdlib |
| `src/devcontext/observability/recorder.py`（新增） | `capture()` / `llm_stage()` 上下文管理器；6 个 `record_*` 与 `mark_last_call_wasted()` |
| `src/devcontext/observability/report.py`（新增） | `TOP_LEVEL_STAGES`、`build_perf_report()`、`render_summary()` |
| `llm/deepseek.py` | `generate()` 拆为「计时+记录」外壳与 `_generate()`；新增 `last_finish_reason` |
| `planning/evidence_planner.py`、`agentic/search_actions.py`、`agentic/coverage.py`、`explanation/{planner,writer,composer,reviewer}.py` | 各调用点包 `llm_stage(...)`；丢弃点调 `mark_last_call_wasted()` |
| `agentic/retrieval_controller.py` | `_execute` 逐 action 记 `RetrievalActionTrace`；round 0/1 的 stage 加 `round_index` |
| `embedding/client.py` | `embed_query` 计时并记录；新增 `effective_transport` |
| `explanation/workflow.py` | 逐节 `SectionExecutionTrace`；`TeachingAnswerResult.section_traces` |
| `agentic/models.py` | `StageUsage` 增加 `round_index: int \| None = None` |
| `cli.py` | `ask --perf / --perf-json / --repeat N` |
| `tests/test_perf_trace.py`（新增） | 24 条测试，含断言 C1 求和口径与行为不变性 |

**两处相对原计划的偏离**（理由见执行计划 §2.5）：
stage 标签走环境上下文而非 `generate()` 参数（仓库有 24 个只接受 `messages` 的测试替身，改签名会全破）；
trace 数据类放在 `observability/` 而非 `agentic/models.py`（后者 import `answer.generator`，
与 `llm/deepseek.py` 会形成循环导入）。

**已知缺口**：perf JSON 未记录 Planner 实际选择的 `answer_depth`，只记录了 `planned_section_count`。
补此项需改代码；采集期间修改会让数据集前后不一致，故留到下一轮。

---

## 8. 复现

```bash
uv run pytest                         # 451 passed, 1 skipped
```

```bash
NO_PROXY="localhost,127.0.0.1,::1,.local,.aliyuncs.com" uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode teach --perf --repeat 3
```

原始数据：`artifacts/perf/<case>.json`（9 个 case × 3 样本）+ `artifacts/perf/manifest.json`。
