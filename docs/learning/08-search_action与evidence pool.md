可以把 `SearchAction` 和 `EvidencePool` 理解为检索结果进入回答上下文之前的两个不同层次：

```text
EvidenceRequirement
    ↓
SearchAction
“这一次搜什么、为谁搜、最多拿多少条”
    ↓
返回若干 SearchResult
    ↓
EvidencePool
“这些结果分别支持哪条需求，如何去重、分配名额、选择进入 Context”
    ↓
ContextBuilder
“最终能放多少条、是否受字符预算截断”
```

它们的职责并不相同：

- `SearchAction` 管理一次查询动作；
- `EvidencePool` 管理多次查询产生的候选结果；
- `ContextBuilder` 才决定最终送给模型的 Context；
- `CoverageChecker` 再判断这些 Context 是否真正满足证据需求。

相关实现主要位于：

- [search_actions.py](D:/Java-learning/DevContext/src/devcontext/agentic/search_actions.py)
- [retrieval_controller.py](D:/Java-learning/DevContext/src/devcontext/agentic/retrieval_controller.py)
- [evidence.py](D:/Java-learning/DevContext/src/devcontext/evidence.py)
- [builder.py](D:/Java-learning/DevContext/src/devcontext/context/builder.py)

---

## 一、SearchAction 是什么

`SearchAction` 表示“一次实际搜索动作”，它不是搜索结果，也不是证据需求。

当前结构大致是：

```text
SearchAction
  action_id
  requirement_id
  round_index
  query
  source_scope
  reason
  decision_source
  error
```

各字段作用如下：

| 字段 | 含义 |
|---|---|
| `action_id` | 搜索动作编号，例如 `SA1`、`SA2` |
| `requirement_id` | 本次搜索服务于哪条证据需求，例如 `ER1` |
| `round_index` | 第几轮搜索，第一轮为 0，补检索为 1 |
| `query` | 实际提交给检索系统的搜索文本 |
| `source_scope` | 限制搜索 CODE、DOCUMENT、BOTH 或 ANY |
| `reason` | 为什么要执行这次查询 |
| `decision_source` | Query 来自 LLM 还是确定性回退 |
| `error` | 本次搜索发生异常时记录异常类型 |

最重要的是 `requirement_id`。它让搜索结果从产生时就有明确归属：

```text
SA1 为 ER1 搜索
SA2 为 ER2 搜索
SA3 为 ER3 搜索
```

这样后面才能判断：

```text
ER1 的证据是否找齐？
ER2 是否只有部分证据？
某个 Chunk 到底支持哪一条需求？
```

如果只把所有搜索结果放进一个大列表，就很难回答这些问题。

---

## 二、SearchAction 是怎样产生的

### 1. 第一轮：每条 EvidenceRequirement 一次查询

RetrievalController 会先把证据需求排序：

```text
CORE 在前
SUPPORTING 在后
同一优先级按 ER 编号排序
```

然后把全部需求交给 `SearchActionPlanner`。

正常情况下，每条 Requirement 生成一个 SearchAction。例如：

```text
ER1 CORE
确认购票入口、关键状态变化和执行顺序

ER2 CORE
确认数据库如何防止重复占座

ER3 SUPPORTING
确认设计文档如何描述令牌桶职责
```

第一轮可能生成：

```text
SA1 → ER1
query = 购票入口 状态更新 执行顺序

SA2 → ER2
query = 座位状态 条件更新 影响行数

SA3 → ER3
query = 余票令牌桶 准入职责 真实库存
```

这里的 SearchAction 不改变 Requirement。它只能把 Requirement 转换成更适合搜索的表达。

### 2. 第二轮：只补查未满足的 CORE

第一轮检索并完成 Coverage 检查后，只有状态为以下两种的 CORE 才会进入第二轮：

```text
PARTIAL
MISSING
```

以下需求不会执行第二轮：

```text
SATISFIED
SUPPORTING
UNVERIFIED
```

`UNVERIFIED` 表示已有候选，但 CoverageChecker 自身检查失败。系统不会在这种情况下盲目补查，因为它无法确认问题出在检索还是检查器。

第二轮 Query 会额外使用：

- 第一轮缺少的内容；
- 已经发现的类名；
- 已经发现的方法名；
- 文件路径；
- 方法签名；
- 文档标题；
- 之前执行过的 Query。

例如：

```text
第一轮 Query：
购票 数据一致性 座位更新

第一轮发现：
PurchaseTicketTxService
SeatAllocator
allocate
PurchaseTicketTxService.java

Coverage 缺口：
尚未找到座位状态条件更新和影响行数检查

第二轮 Query：
PurchaseTicketTxService SeatAllocator allocate
seat_status AVAILABLE 条件更新 影响行数
```

第二轮 Query 不能和第一轮完全相同；如果已经发现真实项目符号，新的 Query 必须尽量使用这些符号。

---

## 三、一次 SearchAction 最多返回多少结果

这是理解当前实现最关键的地方。

单个 SearchAction 的返回数量不是由用户传入的全局 `top_k` 决定，而是由 Requirement 优先级决定：

```text
CORE Requirement       → 最多返回 5 条
SUPPORTING Requirement → 最多返回 3 条
```

对应常量是：

```text
CORE_TOP_K = 5
SUPPORTING_TOP_K = 3
```

因此：

| Requirement 类型 | 单次 Action 最多返回 |
|---|---:|
| CORE | 5 条 |
| SUPPORTING | 3 条 |

这里说的是交给 EvidencePool 的 `SearchExecution.results` 数量。数据库中实际匹配到的结果可能更多，但单个 Action 最终只向 EvidencePool 提供最多 5 条或 3 条。

如果数据库实际只找到两条，那么就只返回两条，并不会为了凑够 5 条而加入无关结果。

---

## 四、CODE、DOCUMENT、BOTH 的返回数量有什么区别

虽然这里不展开具体检索算法，但需要说明返回数量的语义。

### 1. CODE

如果 CORE Requirement 要求 CODE：

```text
单次最多返回 5 条 CODE
```

如果是 SUPPORTING：

```text
单次最多返回 3 条 CODE
```

### 2. DOCUMENT

同理：

```text
CORE       → 最多 5 条 DOCUMENT
SUPPORTING → 最多 3 条 DOCUMENT
```

### 3. BOTH

`BOTH` 很容易被误解成：

```text
5 条 CODE + 5 条 DOCUMENT = 10 条
```

实际不是。

对于一条 CORE 的 BOTH SearchAction，最终交给 EvidencePool 的总数仍然最多是 5 条：

```text
CODE 与 DOCUMENT 交错组合
最终总数 ≤ 5
```

理想情况下可能是：

```text
第 1 条 CODE
第 2 条 DOCUMENT
第 3 条 CODE
第 4 条 DOCUMENT
第 5 条 CODE
```

也可能因为一侧候选不足而变成：

```text
CODE、DOCUMENT、CODE、CODE、CODE
```

SUPPORTING 的 BOTH 同理，总数最多为 3，不是 3+3。

BOTH 在内部会取得更大的文档候选池，但这些内部候选不会全部进入 EvidencePool。EvidencePool 接收的是交错合并后的最终 5 条或 3 条。

### 4. ANY

ANY 表示不强制来源类型，但最终数量仍遵守：

```text
CORE       → 最多 5 条
SUPPORTING → 最多 3 条
```

所以 `source_scope` 决定结果来自哪里，不改变单次 Action 的输出上限。

---

## 五、一轮查询总共可能得到多少结果

假设 EvidencePlan 有：

```text
ER1 CORE
ER2 CORE
ER3 SUPPORTING
```

第一轮最多得到：

```text
ER1：5 条
ER2：5 条
ER3：3 条

合计最多 13 条
```

注意，这里的 13 条是“多个 SearchAction 返回记录的总和”，不代表一定有 13 个不同 Chunk。

可能出现重复：

```text
SA1 返回 Chunk 100
SA2 也返回 Chunk 100
```

所以实际不同 Chunk 数量可能少于 13。

如果 ER1 和 ER2 第一轮都没有满足，第二轮可以再各取最多 5 条：

```text
第一轮最多：13 条
第二轮最多：10 条
累计返回最多：23 条
```

但最终 Context 并不会因此放入 23 条。所有候选还要经过 EvidencePool 的筛选和全局 `top_k` 限制。

---

## 六、整个问题最多执行多少 SearchAction

控制器有一个防御性上限：

```text
MAX_SEARCH_ACTIONS = 12
```

但按照当前 EvidencePlan 的正常结构约束：

```text
Requirement 最多 6 条
CORE 最多 4 条
```

第一轮所有 Requirement 各执行一次：

```text
最多 6 个 Action
```

第二轮只有 CORE 可以再执行一次：

```text
最多 4 个 Action
```

所以正常计划下，理论上最多是：

```text
6 + 4 = 10 个 SearchAction
```

`MAX_SEARCH_ACTIONS = 12` 是额外的安全保护，防止兼容输入或后续代码变化让动作数量失控。

如果采用极限计划：

```text
4 条 CORE
2 条 SUPPORTING
```

而且 4 条 CORE 全部需要第二轮，那么返回记录的理论上限为：

```text
第一轮：
4 × 5 + 2 × 3 = 26 条

第二轮：
4 × 5 = 20 条

累计：
26 + 20 = 46 条
```

这 46 条仍然是所有 Action 的返回记录总数。经过重复结果合并后，EvidencePool 中的不同 Chunk 数通常会更少；最终 Context 数量还会进一步受到全局 `top_k` 和字符预算限制。

---

# 七、EvidencePool 是什么

EvidencePool 不是检索器，它不发出 Query，也不调用数据库。

它的职责是管理已经返回的结果：

```text
EvidencePool
  candidates_by_sub_question
```

虽然内部字段还保留了旧名称 `sub_question_id`，在当前新流程里实际上代表的是：

```text
requirement_id
```

可以把它理解成下面这样的结构：

```text
ER1
  ├─ Chunk 101
  ├─ Chunk 102
  └─ Chunk 103

ER2
  ├─ Chunk 102
  ├─ Chunk 201
  └─ Chunk 202

ER3
  ├─ Chunk 301
  └─ Chunk 302
```

这里 Chunk 102 同时出现在 ER1 和 ER2 名下，表示同一条证据可能支持多条 Requirement。

EvidencePool 主要负责四件事：

1. 保存每条 Requirement 的候选；
2. 对候选进行来源与时间标注；
3. 对同一 Requirement 内的结果去重；
4. 从多个 Requirement 中公平选择最终 Context 候选。

---

## 八、结果进入 EvidencePool 时会附加什么信息

检索返回的是 `SearchResult`，大致包含：

```text
chunk_id
source_type
file_path
content
score
class_name
symbol_name
heading_path
```

进入 EvidencePool 时，`SourcePolicy` 会把它包装成 `EvidenceCandidate`：

```text
EvidenceCandidate
  requirement_id
  search_result
  source_role
  temporal_status
  authority_priority
```

### 1. CODE 的默认标注

当前代码证据会被自动标记为：

```text
source_role = IMPLEMENTATION
temporal_status = CURRENT
authority_priority = 100
```

这是因为当前源码通常是判断“系统现在怎么实现”的最高优先级证据。

### 2. DOCUMENT 的标注

文档根据 SourcePolicy 的路径规则进行标注，例如：

```text
VERIFICATION
CURRENT_DESIGN
GENERAL_DOCUMENT
HISTORICAL_PLAN
UNKNOWN
```

时间状态可能是：

```text
CURRENT
HISTORICAL
FUTURE
UNKNOWN
```

未匹配任何规则的文档默认：

```text
source_role = UNKNOWN
temporal_status = UNKNOWN
authority_priority = 50
```

EvidencePool 不是简单按照搜索分数合并结果。它同时考虑：

- 候选是否属于当前 Requirement；
- 来源类型是否符合要求；
- 证据权威性；
- 时间状态是否符合问题；
- 原始检索排名。

---

## 九、EvidencePool 如何去重

去重分两个阶段。

### 1. 同一 Requirement 内去重

`EvidencePool.add()` 会检查同一 Requirement 名下是否已经存在相同 Chunk ID。

例如：

```text
ER1 第一轮得到 Chunk 100
ER1 第二轮又得到 Chunk 100
```

EvidencePool 只保留一份：

```text
ER1
  └─ Chunk 100
```

所以第二轮重复命中第一轮结果，不会让池中的证据数量虚增。

### 2. 不同 Requirement 之间保留归属关系

如果同一个 Chunk 同时支持 ER1 和 ER2：

```text
ER1 → Chunk 100
ER2 → Chunk 100
```

EvidencePool 在候选映射中允许它分别出现在两个 Requirement 名下，因为这表示两个不同的归属关系。

但在生成最终 Context 时，Chunk 100 只会出现一次。

最终注解会记录：

```text
Chunk 100
supports = ER1, ER2
```

这样既避免把相同正文重复交给模型，又不会丢失“它同时支持两条证据需求”的信息。

---

## 十、EvidencePool 如何选择最终结果

所有 SearchAction 执行完成后，RetrievalController 会调用：

```text
pool.select(requirements, top_k)
```

这里的 `top_k` 是整个问题的全局结果数量上限，不是单次 Action 的 5 或 3。

正常 evidence-driven `ask` 的默认值是：

```text
PLANNED_TOP_K = 12
```

也就是说：

```text
单个 CORE Action：最多贡献 5 条候选
单个 SUPPORTING Action：最多贡献 3 条候选
所有 Action 累积：可能有几十条记录
最终被 EvidencePool 选出：默认最多 12 个不同 Chunk
```

用户显式传入 `--top-k` 时，会覆盖默认的 12。允许范围是 1～100。

EvidencePool 按四个阶段选择。

### 第一阶段：每条 CORE 先尝试保留一条

例如：

```text
ER1 CORE 有 5 条候选
ER2 CORE 有 5 条候选
ER3 CORE 有 4 条候选
```

它不会先把 ER1 的 5 条全部放进去，而是先尝试：

```text
ER1 选 1 条
ER2 选 1 条
ER3 选 1 条
```

这样可以避免前面的 CORE 吃掉全部 Context 名额。

这里是“尝试保证”，不是绝对保证。如果用户把全局 `top_k` 设置得比 CORE 数量还小，例如 4 条 CORE 但 `top_k=2`，那么不可能每条 CORE 都得到一条。

默认 `top_k=12`，而 CORE 最多 4 条，因此正常情况下有足够的基础名额。

### 第二阶段：BOTH 类型 CORE 补齐两类来源

如果某条 CORE 要求：

```text
source_requirement = BOTH
```

EvidencePool 会检查已经选择的结果中，这条 Requirement 是否同时拥有：

```text
CODE
DOCUMENT
```

如果只有 CODE，会再尝试选择一条 DOCUMENT；如果只有 DOCUMENT，则尝试补一条 CODE。

例如：

```text
ER1 要求 BOTH

第一阶段：
选中 Chunk 101 CODE

第二阶段：
再选择 Chunk 105 DOCUMENT
```

这样 ER1 至少有机会把代码和文档同时送入 Context。

但前提是：

- 检索结果中确实存在两类来源；
- 全局 `top_k` 还有空间；
- 另一类候选没有因为重复而被占用。

如果 SearchAction 的最终 5 条里本来就没有 DOCUMENT，EvidencePool 无法凭空补出文档。

### 第三阶段：每条 SUPPORTING 尝试保留一条

CORE 的基础证据和 BOTH 来源完成后，才轮到 SUPPORTING：

```text
ER4 SUPPORTING → 尝试选 1 条
ER5 SUPPORTING → 尝试选 1 条
```

这体现了明确的优先级：

```text
CORE 完整性
高于
SUPPORTING 丰富度
```

### 第四阶段：使用剩余名额

完成上述最低分配后，如果全局 `top_k` 仍有剩余，才从所有未选候选中继续补充。

补充时主要按照：

```text
证据有效优先级降序
然后按原始检索排名
```

因此最终选择不等于把所有搜索结果按相似度分数重新全局排序。

EvidencePool 优先保证：

1. 每条 CORE 都有基础证据；
2. BOTH CORE 尽量具有双来源；
3. SUPPORTING 不被完全饿死；
4. 剩余名额再交给高权威证据。

---

## 十一、时间状态如何影响 EvidencePool 排序

候选的基础优先级来自 SourcePolicy，例如：

```text
当前 CODE：100
未分类 DOCUMENT：50
```

EvidencePool 还会根据 Requirement 的 `temporal_scope` 调整有效优先级。

如果 Requirement 要求当前状态：

```text
temporal_scope = CURRENT
```

那么：

```text
CURRENT 候选：基础优先级 + 25
HISTORICAL 候选：基础优先级 - 25
FUTURE 候选：基础优先级 - 25
```

例如：

```text
当前代码：
100 + 25 = 125

历史文档：
70 - 25 = 45
```

这意味着用户询问“当前如何实现”时，当前代码会明显优先于历史规划文档。

如果问题询问历史演进：

```text
temporal_scope = HISTORY
```

则 HISTORICAL 证据获得加成。

这不是直接删除历史文档，而是在名额有限时让符合问题时间范围的证据更容易进入 Context。

---

## 十二、EvidencePool 选出 12 条后，是否一定全部可用

不一定。

EvidencePool 的输出还要经过 `ContextBuilder`。

最终结果同时受两个上限限制：

```text
数量上限：top_k，默认 12
字符上限：8000 / 16000 / 28000
```

例如 EvidencePool 选出 12 条，但每条代码都很长，字符预算只够放 7 条，那么最终 Context 可能是：

```text
前 6 条完整
第 7 条截断
后 5 条没有进入 Context
```

CoverageChecker 只把以下证据视为可用：

```text
进入最终 Context
并且没有被截断
并且归属于当前 Requirement
```

因此需要区分三个数字：

| 层级 | 表示什么 |
|---|---|
| SearchAction 返回数 | 某次查询最多拿回多少结果 |
| EvidencePool 候选/选择数 | 多次查询累积后，最多选多少不同 Chunk |
| 最终可用 Context 数 | 真正完整进入模型输入、可以参与 Coverage 的数量 |

举例：

```text
3 个 SearchAction 共返回 13 条
去重后 EvidencePool 中有 10 个不同 Chunk
全局 top_k=12，因此 10 个都可以被选择
字符预算最终只完整放入 7 个
第 8 个被截断
```

那么：

```text
搜索返回记录：13
不同候选：10
EvidencePool 选择：10
Context Item：8
Coverage 可用证据：7
```

这也是为什么检索评测要增加 Context Survival，不能只看 SearchAction 有没有命中目标。

---

## 十三、完整示例

假设系统生成三条需求：

```text
ER1 CORE BOTH
确认订单关闭的代码实现和设计依据

ER2 CORE CODE
确认关闭订单时修改了哪些状态

ER3 SUPPORTING DOCUMENT
确认测试或验证材料记录了什么
```

### 第一轮返回

```text
SA1 → ER1，最多 5 条
  Chunk 11 CODE
  Chunk 12 DOCUMENT
  Chunk 13 CODE
  Chunk 14 DOCUMENT
  Chunk 15 CODE

SA2 → ER2，最多 5 条
  Chunk 13 CODE
  Chunk 21 CODE
  Chunk 22 CODE
  Chunk 23 CODE
  Chunk 24 CODE

SA3 → ER3，最多 3 条
  Chunk 31 DOCUMENT
  Chunk 32 DOCUMENT
  Chunk 12 DOCUMENT
```

总返回记录：

```text
5 + 5 + 3 = 13 条
```

但存在重复：

```text
Chunk 13 同时出现在 ER1、ER2
Chunk 12 同时出现在 ER1、ER3
```

所以全局不同 Chunk 只有 11 个。

### EvidencePool 内部结构

```text
ER1
  11 CODE
  12 DOCUMENT
  13 CODE
  14 DOCUMENT
  15 CODE

ER2
  13 CODE
  21 CODE
  22 CODE
  23 CODE
  24 CODE

ER3
  31 DOCUMENT
  32 DOCUMENT
  12 DOCUMENT
```

### 选择过程

第一步，每条 CORE 一条：

```text
ER1 → Chunk 11 CODE
ER2 → Chunk 13 CODE
```

第二步，ER1 要求 BOTH，补文档：

```text
ER1 → Chunk 12 DOCUMENT
```

第三步，SUPPORTING 一条：

```text
ER3 → Chunk 31 DOCUMENT
```

目前选择了：

```text
11、13、12、31
```

第四步，如果全局 `top_k=8`，再从剩余候选中补 4 条：

```text
21、14、22、32
```

最终 8 个不同 Chunk：

```text
11、13、12、31、21、14、22、32
```

其中 Chunk 12 的注解可能是：

```text
supports = ER1, ER3
```

虽然它支持两条需求，Context 中只渲染一次。

### 字符预算处理

如果字符预算只能完整放入前 6 条，第 7 条被截断，那么最终：

```text
Context Item：7 条
完整可用于 Coverage：6 条
```

第 7 条虽然出现在 Context 中，但不会用于证明 Requirement 已满足。

---

## 十四、发生错误时如何处理

某个 SearchAction 执行失败时，RetrievalController 不会终止全部查询。

例如：

```text
SA1 成功 → 5 条结果
SA2 失败 → TimeoutError
SA3 成功 → 3 条结果
```

执行历史会保留：

```text
SA2.error = TimeoutError
```

但 EvidencePool 只接收 SA1 和 SA3 的结果。之后仍然会正常构建 Context 和执行 Coverage 检查。

因此单个查询失败的影响范围主要是：

```text
对应 Requirement 没有新候选
```

而不是：

```text
整个用户问题直接失败
```

后续 Coverage 通常会将相关 Requirement 判断为 `MISSING`、`PARTIAL` 或 `UNVERIFIED`。

---

## 十五、两个模块的职责边界

### SearchAction 负责

- 一次查询服务于哪条 Requirement；
- 查询文本是什么；
- 查询哪类来源；
- 当前是第一轮还是第二轮；
- 单次查询最多接收 5 条或 3 条结果；
- 记录查询原因、生成来源和执行错误。

### SearchAction 不负责

- 多条查询结果的全局去重；
- 不同 Requirement 之间的结果分配；
- 决定最终 Context 放多少条；
- 判断证据是否真正充分。

### EvidencePool 负责

- 按 Requirement 保存候选；
- 同一 Requirement 内按 Chunk ID 去重；
- 标记来源权威性和时间状态；
- 优先保证 CORE；
- 为 BOTH CORE 尽量保留两种来源；
- 为 SUPPORTING 保留基本名额；
- 将相同 Chunk 合并为一个最终 Context 项；
- 保存一个 Chunk 支持多条 Requirement 的关系。

### EvidencePool 不负责

- 生成 Query；
- 执行检索；
- 修改底层检索排名；
- 判断 `success_criteria` 是否满足；
- 决定最终回答怎么写；
- 保证被选中的结果一定能完整放入字符预算。

---

## 十六、最容易混淆的三个“数量”

当前系统中有三个完全不同的数量限制：

```text
单个 Action 的结果上限
CORE = 5
SUPPORTING = 3
```

```text
EvidencePool 最终选择上限
默认全局 top_k = 12
用户可以通过 --top-k 修改
```

```text
Context 字符上限
根据 EvidencePlan 复杂度取 8000、16000 或 28000
也可以由用户显式覆盖
```

所以“一个 Query 能检索多少结果”的准确回答是：

> 一条 CORE 查询最终最多向 EvidencePool 提供 5 条结果，一条 SUPPORTING 查询最多提供 3 条；多条查询和第二轮结果会累积进 EvidencePool，但默认最终最多选择 12 个不同 Chunk。即使选中了 12 个，仍可能因为 Context 字符预算只保留其中一部分，最后一条若被截断也不能用于判定证据满足。

这也是 SearchAction 和 EvidencePool 的核心配合关系：

```text
SearchAction 控制单次取多少
EvidencePool 控制多次结果怎么分
ContextBuilder 控制最终实际放多少
CoverageChecker 控制其中多少真正算有效证据
```