# 05 · L1.5 阶段感知检索（Stage-Aware Retrieval）受控消融实验

| | |
|---|---|
| 分支 | `exp/l15-stage-aware-retrieval`（从 `main` @ `fa73211` 建立） |
| 实验提交 | `8949a88` `feat: route CODE retrieval by stage, not by scope alone` |
| 评测方式 | `evaluate-retrieval-workflow --suite all --mode frozen`（L1.5 19 例 + regression 5 例 = 24 例） |
| 对照 | 同一命令在**未修改代码**上跑出的 `artifacts/l15-exp/baseline.json` |
| 结果 | `artifacts/l15-exp/experiment.json` |
| 日期 | 2026-10-03 |

> **口径**：frozen 模式冻结的是 EvidenceRequirement 与两轮 Query（`FrozenEvidencePlanner` /
> `FrozenSearchActionPlanner`），**检索本身是真实的**——真实 PostgreSQL + 真实 embedding。
> 因此本实验只改变「用哪种检索策略」，其余全部不变。样本量 24 例 / 56 个 action，单次运行。

---

## 1. 实验假设

当前 CODE 检索恒定使用 `Keyword + Vector → RRF Hybrid`，但检索链路有两个性质不同的阶段：

- **Round 0（语义发现）**：用户问题与 EvidenceRequirement 通常只有业务语义，尚未知道任何真实
  `class_name` / `symbol_name` / `signature`。此时 keyword 半边没有精确词可匹配。
- **Round 1（定向补查）**：Workspace 已含第一轮真实结果，可能已发现
  `PurchaseTicketTxService`、`doPurchaseInTransaction` 这类真实符号，此时精确匹配才有价值。

假设：

> **H1** 无真实符号的第一轮 CODE 查询，Vector 的 Gold Recall / CORE Satisfaction 不劣于 Hybrid。
> **H2** 已发现真实符号的第二轮 CODE 查询，Keyword 的 Gold Rank / Rescue Rate 优于 Hybrid。
> **H3** 上述阶段感知策略在不调整 Top-K 的前提下，不降低最终 CORE Coverage / Full-case /
> Context Survival / False READY。

---

## 2. 代码修改

新增 `src/devcontext/retrieval/symbols.py`：判定「这段文本是否点名了项目中真实存在的东西」。

```
symbols_in_text(text)      查询文本里的 Java 标识符形状（用户显式输入）
workspace_symbols(ws, rid) 已检索证据 citation 里的 class_name/symbol_name/signature
code_strategy_for(...)     有符号 → keyword，无符号 → vector，并返回决定它的符号
```

**判定的是形状，不是语义**：模式只匹配 camelCase、限定名、`@Annotation`、`.java`；
`订单 / 事务 / Redis / 锁 / 补偿` 一律不匹配——全部大写的缩写（HTTP/SQL/USB）也排除。
模式刻意**重述**而非 import `routing/router.py::_CODE_PATTERNS`，因为该模块属于本实验的
禁改范围；同时排除了其中「如何实现 / 在哪里 / 代码」这类**路由**提示词——它们表达的是
「这题要代码证据」，与「这段文本点名了项目里的某物」是两回事。

其余改动：

| 文件 | 改动 |
|---|---|
| `retrieval/policy.py` | `search_scope_with_trace(..., *, code_strategy=None)`；仅 CODE 分支生效，**不传参即维持原 Hybrid 行为** |
| `agentic/retrieval_controller.py` | `_execute` 为每个 CODE action 计算策略并传入；把决定它的符号记到 `SearchExecution.symbols` |
| `models.py` | `SearchExecution` 增加可选 `strategy` / `symbols` 字段（默认 None / 空） |
| `retrieval/service.py` | 构造 `SearchExecution` 时带上实际执行的 strategy |
| `evaluation/retrieval_workflow_runner.py` | 新增 round-level 指标与 per-action strategy trace（见下） |
| `tests/test_stage_aware_retrieval.py`（新） | 19 条测试：符号检测、证据符号、policy 边界、controller 分阶段选择 |

**因接口变更而更新的既有测试 2 处**（是契约变更，不是绕过）：
`tests/test_retrieval_controller.py` 的三个 FakePolicy 接受新参数；
`tests/test_ask_cli.py:516` 改为期待 round-0 CODE 用 `vector`（该断言正是本实验的主张）。

`uv run pytest` → **472 passed, 1 skipped**。

### 新增指标与 Strategy Trace

旧指标只有最终态，无法区分「第一轮就够」还是「第二轮救回」。新增：

- **Round 0**（以 round 0 自己的 context 计分）：`round0_core_requirement_total` /
  `_satisfied` / `_satisfaction_rate`、`round0_gold_group_total` / `_hit` / `_recall`、
  `round0_full_case_success_count` / `_rate`、`round0_strategy_counts`
- **Round 1**：沿用既有 `second_round_*`，新增 `round1_new_gold_group_count`
- **Per-action trace**：`strategy`、`symbols`、`gold_hit`、`gold_rank`
  （`gold_rank` = 该 requirement 任一 Gold group 首次命中的 1-based 名次）

> **一处已知标注缺口**：`BOTH` 路径的 `strategy` 为 `None`（报告里显示 `BOTH:unknown`）。
> MIXED 分支不在本实验范围内，未加标签；本批 56 个 action 里占 1 个，行为与 baseline 完全相同。

---

## 3. 控制变量

**未改动**：EvidencePlanner Prompt 与 `EvidenceRequirement` 定义、Frozen L1.5 requirement、
Frozen round_0/round_1 query、CoverageChecker 判定、Ground Truth、`CORE_TOP_K=5` /
`SUPPORTING_TOP_K=3`、EvidencePool、SourcePolicy、ContextBuilder、context budget、
chunk 数据、embedding 模型、RRF 参数（k=60）、benchmark case。

**DOCUMENT / BOTH / ANY 路径未改动。** 唯一例外是为 CODE 分支新增一个可选参数，
且**不传参时行为与改动前逐字一致**（`tests/test_stage_aware_retrieval.py::TestPolicySeam`
`test_without_a_code_strategy_the_default_is_unchanged` 断言了这个默认）。

---

## 4. Baseline

> ⚠ 仓库里 `benchmark/baselines/retrieval-workflow-v1.json` 是在 18 例数据集上生成的，
> 而当前 `l1.5-retrieval.jsonl` 有 19 例，`dataset_sha256` 不一致，工具报告
> `baseline_comparison: status=incompatible`。因此**本次对照不用它**，而是在未修改的
> `main` 代码上重跑了一份 `artifacts/l15-exp/baseline.json`。所有 baseline 数字均取自该文件。

新增的 round-level 指标在 baseline 那份 JSON 里不存在（那时还没有这些代码），
所以 round-0 指标是**离线重算**的：从 baseline 记录的 `round_contexts[0]` 重建 ContextBundle，
再调用**同一套** `_gold_requirement_satisfied` / `_matched_group_indexes`。

这个重算经过验证：对 experiment 的 24 条记录，离线值与代码内建的值
**24/24 完全一致，0 处不符**（`core_total`/`core_sat`/`gold_total`/`gold_hit` 四项全等）。
因此把同一函数用在 baseline 上是可信的。

---

## 5. Round 0 对比

| 指标 | Baseline | Experiment |
|---|---:|---:|
| round0 CORE 满足 | 18 / 31 | 18 / 31（持平） |
| round0 Gold group 召回 | **33 / 46** | **34 / 46** |
| round0 动作数 | 42 | 42（CODE 37 + DOCUMENT 4 + BOTH 1） |
| CODE 动作策略分布 | hybrid ×37 | **vector ×27 / keyword ×10** |

**逐 action 配对比较**（按 `case_id + action_id` 配对，56 个 action 中 CODE 37 个）：

| 转变 | n | 命中 | 平均 Gold Rank | 变好 | 变差 |
|---|---:|---|---:|---:|---:|
| hybrid → keyword（查询本身点名符号） | 10 | 8 → 8 | 1.62 → **1.50** | 1 | **0** |
| hybrid → vector（纯语义查询） | 27 | 18 → **19** | 1.94 → **1.74** | 6 | **0** |

**H1 结论**：**成立，且比预期更强。** 纯语义的 27 个动作改用 Vector 后**一个变差都没有**，
反而多命中 1 个、平均名次从 1.94 提升到 1.74（6 个变好、0 个变差）。也就是说在无语义符号的
第一轮，Hybrid 的 keyword 半边确实只贡献了噪声。

> **一个必须说明的事实**：本语料的 round 0 **并非全是语义查询**。33 个 round-0 查询里有 8 个
> 本身就是裸符号串，例如 `PurchaseTicketTxService 座位状态 条件更新`、
> `OrderTimeoutCloseJob scanTimeoutOrder 定时扫描`、`Redis 余票令牌桶 takeToken 准入`。
> 按规范「用户显式输入的真实符号也算真实符号」，这 8 条走 keyword 是正确的，但它们让
> 「Round 0 = Semantic」这个说法在本语料上不成立。**H1 的检验对象因此是那 27 条纯语义查询，
> 而不是整个 round 0。**

---

## 6. Round 1 对比

| 指标 | Baseline | Experiment |
|---|---:|---:|
| second-round eligible | 8 | 8 |
| **second-round rescued** | 6 | **7** |
| **rescue rate** | 0.75 | **0.875** |
| no-gain | 2 | **1** |
| partial-gain | 0 | 0 |
| **round1 新增 gold group** | 7 | **8** |

**逐 action 配对比较**（round 1 CODE，全部 11 条都从 hybrid 变为 keyword）：

| 转变 | n | 命中 | 平均 Gold Rank | 变好 | 变差 |
|---|---:|---|---:|---:|---:|
| hybrid → keyword | 11 | 6 → **8** | 2.17 → **1.62** | 4 | **0** |

**H2 结论**：**成立。** 第二轮 11 个动作命中数从 6 提升到 8（+33%），平均 Gold Rank 从 2.17
改善到 1.62，且同样**一个变差都没有**。这正是 keyword 对 class/method/signature 高权重匹配
能力的直接体现。

---

## 7. Final 对比

| 指标 | Baseline | Experiment | 变化 |
|---|---:|---:|---|
| **core_requirement_coverage** | 0.9231（24/26） | **0.9615（25/26）** | **+1 条** |
| **full_case_success_rate** | 0.7917（19/24） | **0.8750（21/24）** | **+2 例** |
| **context_survival_rate** | 0.9302（40/43） | **0.9545（42/44）** | +2 group |
| **expected_state_accuracy** | 0.8750 | **0.9583** | +2 例 |
| **false_ready_count** | 0 | 0 | 持平（未恶化） |
| average_search_action_count | 2.38 | 2.33 | −0.05（更少动作） |

按期望状态拆分（`by_expected_state` READY 组）：full-case 0.8421 → **0.9474**；PARTIAL 组
0.5 → 0.5；EMPTY 组 1.0 → 1.0。

**H3 结论**：**成立。** 四项最终指标全部不降、三项上升，False READY 保持 0，
且**平均检索动作数还略有下降**（没有靠增加 Top-K 或加检索量换分）。

---

## 8. Case-level Gain / Regression

**24 例中只有 2 例最终结果发生变化，两例都是改善，零回退。**

### 8.1 flow-02（CORE 0.0 → 1.0，PARTIAL → READY）：关键字精确匹配

| | |
|---|---|
| Requirement | `ER1` CORE/CODE，2 个 Gold group，期望满足 |
| Round 0 | query `用户注册 请求入口 服务流程` — baseline `hybrid` Gold rank **3**；experiment `vector` rank **2** |
| Round 1 | query `UserInfoController register UserLoginServiceImpl register` — baseline `hybrid` Gold rank **None（完全没检索到）**；experiment `keyword` rank **1** |
| 最终 | baseline 该 requirement 不满足 → `PARTIAL`；experiment 满足 → `READY` |

**归因：Keyword exact symbol matching。** 第二轮查询是纯符号串，hybrid 的向量半边把
`UserInfoController` 的语义邻居排在了前面，把真正的 Gold 挤出了 top-5；keyword 直接按符号精确命中。

### 8.2 why-03（full_case False → True，CORE 不变 1.0）：候选排序边界 / 证据合成

| | |
|---|---|
| Requirement | `ER2` SUPPORTING/CODE，1 个 Gold group，期望满足 |
| Round 0 | query `PurchaseTicketTxService 座位状态 条件更新` — baseline `hybrid` Gold rank **None**；experiment `keyword` Gold rank **None** |
| **action 级 Gold 结果没有变化** | 两次都没在这个 action 的结果里命中 Gold |
| 但 | baseline：候选**找到了** 2 个 group，最终 context **只留下 1 个**（`found=2, survived=1`，`dropped=[ER2#0]`，`truncated=True @16000 chars`）<br>experiment：`found=2, survived=2, dropped=[]`（`total_chars` 15823） |
| 最终 | baseline `ER2` 不满足 → full_case False；experiment 满足 → full_case True |

**归因：candidate ranking boundary / Evidence composition，不是检索命中改变。**
两份证据都存在，差别在于 keyword 的排序把该 Gold 推到了 16,000 字符截断线之上。
**这是一个边界效应，不是稳健的收益**——见 §10 的风险。

### 8.3 唯一一处 action 级改善未转化为 case 改善

`locate-01` round-0 CODE action 由 hybrid 变 vector，Gold rank 从 **None → 5**（多召回 1 个 group，
即 §5 中 33→34 的那一个），但该 case 的最终 CORE / full_case 未变（原本已满足）。
属「召回变好但结果不变」，无害。

---

## 9. 为什么发生变化

按规范要求的四类归因：

| 归因类别 | 本实验中的实例 | 是否为主要原因 |
|---|---|---|
| **Keyword exact symbol matching** | flow-02 round 1（None → rank 1）；round-1 整体 6→8 命中 | **是（主要）** |
| **Vector semantic recall** | round-0 纯语义 27 个动作：18→19 命中，rank 1.94→1.74 | **是（次要但稳定）** |
| **candidate ranking boundary** | why-03（16k 截断线两侧的排序差异） | 是（1 例，且脆弱） |
| Evidence composition | 同上，why-03 本质是 pool → bundle 的合成边界 | 同上 |

**没有任何一处回退。** 56 个配对 action 中，所有发生变化的比较里 `worse = 0`。

---

## 10. 是否建议合并该策略

### 结论：**建议合并，但附带三条必须写进 PR 的限制说明。**

**支持的理由（不是只看总分）：**

1. **H2 证据最强**：round-1 keyword 命中 6→8、平均 rank 2.17→1.62、4 变好 0 变差。
   这与 keyword 对符号的高权重匹配机制一致，不是巧合。
2. **H1 证据次强且方向干净**：无符号的第一轮改 vector，27 个动作 6 变好 0 变差、平均 rank 提升。
   说明 hybrid 的 keyword 半边在无语义符号时确实是噪声。
3. **H3 有加分**：core coverage +1、full case +2、state accuracy +2、survival +2，
   **同时动作数还减少了**（2.38 → 2.33），说明不是靠增加检索量换来的。
4. **零回退**：action 级与 case 级都没有任何一处变差，False READY 保持 0。

**必须同时声明的限制：**

1. **样本量小，绝对增益薄。** 24 例、56 个 action、单次运行。核心提升是 **+1 条 CORE 需求、
   +2 个 full case**。这不足以排除运气，建议合并前至少重跑 3 次确认方向稳定。
2. **有一例的收益来自边界效应而非机制。** why-03 的改善源于 16,000 字符截断线两侧的排序差异，
   Gold rank 在 action 级**根本没变**。如果 context budget 变化，它可能翻回去。
   **这一例不应算作该策略的机制性收益。** 剔除它之后，真实的机制性收益是 flow-02 一例。
3. **「Round 0 = 纯语义」在本语料上不成立。** 33 个 round-0 查询里有 8 个本身就是符号串
   （占 24%），它们走 keyword。所以本实验验证的是**内容驱动的路由**，而不是规范标题里
   「Round 0 Semantic → Vector」这个按轮次切分的说法。若后续要按轮次强制，需要单独实验。

### 规范要求的两个明确判断

```
是否支持 Round0 Semantic → Vector：支持，但需限定
```
支持——但要限定为「**无真实符号**的第一轮 CODE 查询用 Vector」。本语料 round 0 有 24% 的查询
本身点名了符号，那些走 keyword 更合适，实测也更准（平均 rank 1.50 优于 hybrid 的 1.62）。
**按轮次强制 vector 会丢掉这部分收益。**

```
是否支持 Round1 Symbol → Keyword：支持
```
支持，证据最充分：命中 6→8、rank 2.17→1.62、4 变好 0 变差、rescue rate 0.75→0.875。
且该策略**只在发现真实符号时触发**，不会因为 `round_index == 1` 就强制 keyword。

---

## 11. 复现

```bash
# baseline（未修改代码，main @ fa73211）
NO_PROXY='localhost,127.0.0.1,::1,.local,.aliyuncs.com' \
  uv run devcontext evaluate-retrieval-workflow --suite all --mode frozen \
  --output artifacts/l15-exp/baseline.json

# experiment（8949a88）
NO_PROXY='localhost,127.0.0.1,::1,.local,.aliyuncs.com' \
  uv run devcontext evaluate-retrieval-workflow --suite all --mode frozen \
  --output artifacts/l15-exp/experiment.json
```

`NO_PROXY` 必需：本机代理会打断 `dashscope.aliyuncs.com` 的 TLS 握手，
缺它时每个 embedding 调用都失败，且**静默退化为空检索**——那会把实验结论完全带偏。
本次 56 个 action 全部成功、0 失败。

原始数据：`artifacts/l15-exp/{baseline,experiment}.json`（各 24 条记录，含逐 action 的
strategy / symbols / gold_hit / gold_rank 与两轮 context）。
