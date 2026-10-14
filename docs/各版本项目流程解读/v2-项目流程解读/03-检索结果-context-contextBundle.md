可以，这一步我们就只讲：

> **检索返回一批 `SearchResult` 之后，系统如何一步步筛选，最终变成进入 Context 的证据。**

检索策略本身先快速带过：

```text
SearchAction
↓
RetrievalPolicy
↓
keyword / vector / hybrid / routed
↓
返回若干 SearchResult
```

真正重要的是后面这段：

```text
SearchResult[]
↓
SourcePolicy 标注
↓
EvidencePool 按 Requirement 收集
↓
EvidencePool.select() 做第一次筛选
↓
ContextBuilder 做第二次筛选
↓
ContextBundle
```

---

# 1. 检索返回的数据长什么样

每条检索结果是一个 `SearchResult`：

```python
SearchResult(
    id,
    source_type,
    chunk_type,
    file_path,
    content,
    start_line,
    end_line,
    class_name,
    symbol_name,
    signature,
    title,
    score,
    ...
)
```

比如检索：

```text
“购票事务 seat ticket”
```

可能得到：

```text
Result 1
id = 101
source_type = CODE
class_name = PurchaseTicketTxService
symbol_name = doPurchaseInTransaction
score = 0.91

Result 2
id = 205
source_type = DOCUMENT
file_path = docs/购票流程.md
score = 0.84

Result 3
id = 106
source_type = CODE
class_name = PurchaseTicketServiceImpl
symbol_name = purchaseTickets
score = 0.79
```

注意：

> 这里的 `score` 只是 Retriever 给出的相关度，不代表“最终应该进入上下文”。

后面还有一层证据筛选。

---

# 2. 检索结果首先经过 `SourcePolicy.classify()`

在 `RetrievalController._execute()` 中：

```python
pool.add_many(
    self.source_policy.classify(
        result,
        requirement.id
    )
    for result in execution.results
)
```

也就是说：

```text
SearchResult
↓
SourcePolicy.classify()
↓
EvidenceCandidate
```

---

# 3. 为什么不能直接把 SearchResult 放进去？

因为 `SearchResult` 只知道：

```text
这个 Chunk 是什么
它来自哪里
检索分数是多少
```

但是系统还需要知道：

```text
它是在支持哪个 ER？
它是当前实现还是历史文档？
它的证据权威性怎么样？
```

所以会包装成：

```python
EvidenceCandidate
```

结构大概：

```python
EvidenceCandidate(
    sub_question_id,
    search_result,
    source_role,
    temporal_status,
    authority_priority
)
```

这里 `sub_question_id` 现在实际上就是：

```text
ER1
ER2
ER3
```

只是变量名还保留了旧设计。

---

# 4. CODE 结果如何分类

代码非常直接：

```python
if result.source_type == "CODE":
    return EvidenceCandidate(
        requirement_id,
        result,
        "IMPLEMENTATION",
        "CURRENT",
        100
    )
```

所以所有源码默认认为：

```text
source_role = IMPLEMENTATION
temporal_status = CURRENT
authority_priority = 100
```

也就是说在当前系统里：

> **真实源码被认为是当前实现最强的证据。**

例如：

```text
PurchaseTicketTxService.java
```

会得到：

```text
IMPLEMENTATION
CURRENT
priority = 100
```

---

# 5. DOCUMENT 结果更复杂

文档不是默认都可信。

因为项目里可能同时存在：

```text
当前设计文档
旧规划
历史方案
验证报告
普通说明文档
```

所以会通过：

```text
config/source-policy.json
```

里的路径规则分类。

例如概念上可能是：

```text
docs/development/**
→ CURRENT_DESIGN
→ CURRENT
→ priority 90
```

而：

```text
docs/旧规划/**
→ HISTORICAL_PLAN
→ HISTORICAL
→ priority 40
```

所以文档结果可能被标为：

```text
CURRENT_DESIGN
CURRENT
80
```

或者：

```text
HISTORICAL_PLAN
HISTORICAL
40
```

这就是为什么系统不会简单地认为：

```text
所有文档 = 同等证据
```

---

# 6. 这一步实际上解决“证据权威性”

比如用户问：

> “当前购票事务是怎么实现的？”

检索出来：

```text
A. 当前 Java 代码
B. 3 天前的设计文档
C. 两个月前的开发计划
```

如果只是按照向量分数：

```text
旧开发计划 score = 0.93
源码 score = 0.82
```

很可能旧文档排在前面。

但 SourcePolicy 会告诉后面系统：

```text
源码：
CURRENT + IMPLEMENTATION + 100

旧计划：
HISTORICAL + HISTORICAL_PLAN + 40
```

于是后续选择时可以降低旧计划权重。

---

# 7. 然后所有结果进入 `EvidencePool`

这里非常重要。

EvidencePool 不是一个：

```text
所有搜索结果混在一起的大列表
```

它的数据结构实际上是：

```python
candidates_by_sub_question:
    dict[str, list[EvidenceCandidate]]
```

可以理解成：

```text
ER1
├─ candidate A
├─ candidate B
└─ candidate C

ER2
├─ candidate D
├─ candidate E
└─ candidate F
```

例如：

```text
ER1：确认数据库事务
```

检索回来：

```text
PurchaseTicketTxService
TransactionTemplate
购票事务设计文档
```

全部放进：

```text
EvidencePool["ER1"]
```

而：

```text
ER2：确认分布式锁
```

结果放进：

```text
EvidencePool["ER2"]
```

---

# 8. 这里会先做一次局部去重

`EvidencePool.add()`：

```python
if all(
    item.search_result.id != candidate.search_result.id
    for item in candidates
):
    candidates.append(candidate)
```

也就是说：

> **同一个 ER 下，相同 Chunk ID 只保存一次。**

比如：

```text
ER1 搜索：
购票事务

又一次搜：
doPurchaseInTransaction
```

都命中：

```text
chunk 101
```

那么：

```text
ER1
```

里面不会存两份 `101`。

---

# 9. 但同一个 Chunk 可以支持多个 ER

这个细节非常重要。

例如：

```text
PurchaseTicketTxService#doPurchaseInTransaction
```

可能同时支持：

```text
ER1：事务边界
ER2：座位状态修改
```

EvidencePool 允许：

```text
ER1 → chunk 101
ER2 → chunk 101
```

因为它只在：

```text
同一个 Requirement 内
```

去重。

不会全局禁止 Chunk 重复归属。

这是合理的，因为：

> 一个代码片段可以证明多个事实。

---

# 10. 真正筛选发生在 `EvidencePool.select()`

前面只是：

```text
把候选收进池子
```

真正进入 Context 之前，会执行：

```python
results, annotations = pool.select(
    plan.requirements,
    top_k
)
```

这一步才是真正的：

> **从很多候选中选出有限数量的证据。**

---

# 11. 第一个筛选原则：CORE 优先

代码先分：

```python
core = [...]
supporting = [...]
```

然后：

```python
for question in core:
    choose(question)
```

也就是说：

> **每个 CORE Requirement 优先至少拿一条证据。**

例如：

```text
ER1 CORE：事务
ER2 CORE：锁
ER3 CORE：失败补偿
ER4 SUPPORTING：设计背景
```

假设最终只能放 5 条：

它不会简单按照 Retriever Score：

```text
事务1
事务2
事务3
事务4
锁1
```

这样会导致：

```text
补偿完全没证据
```

而是优先保证：

```text
ER1 至少 1 条
ER2 至少 1 条
ER3 至少 1 条
```

---

# 12. 这是当前筛选逻辑最重要的设计

普通 RAG 通常：

```text
所有候选
↓
按 score 排序
↓
Top5
```

可能出现：

```text
Top5 全是同一个话题
```

DevContext 当前不是这样。

它先保证：

```text
Evidence Requirement Coverage
```

再考虑剩余排名。

也就是说它优化的不是：

```text
单纯相关度最高
```

而是：

> **回答这个问题所需的不同事实，都尽量至少有证据。**

---

# 13. `choose(question)` 内部怎么选候选？

它会对这个 Requirement 对应的候选排序：

```python
key = (
    -_effective_priority(candidate, question),
    original_rank
)
```

核心有两个因素。

### 第一因素：证据有效优先级

也就是：

```text
authority_priority
+
temporal adjustment
```

---

### 第二因素：原来的检索顺序

如果权威性一样：

```text
谁原本 Retriever 排得更靠前
谁优先
```

所以不是完全抛弃 Retriever Score。

只是把：

```text
证据权威性
```

放到了更高一层。

---

# 14. `_effective_priority()` 怎么处理时间？

这是一个很值得理解的地方。

假设 Requirement：

```text
temporal_scope = CURRENT
```

候选：

```text
A:
CURRENT
priority = 100

B:
HISTORICAL
priority = 90
```

程序会：

```text
CURRENT → +25
HISTORICAL → -25
```

所以：

```text
A effective = 125
B effective = 65
```

于是当前证据明显优先。

---

# 15. 如果问题问的是历史呢？

假设：

```text
temporal_scope = HISTORY
```

那么：

```text
HISTORICAL
```

会获得：

```text
+25
```

这样历史设计文档反而可能优先。

所以不是永远：

```text
源码 > 文档
```

而是：

> 要看 Requirement 想证明“当前事实”还是“历史事实”。

---

# 16. 第二个筛选原则：source_requirement

`choose()` 中还有：

```python
if result.source_type not in _owner_sources(question):
    continue
```

比如：

```text
ER1.source_requirement = CODE
```

那么即使：

```text
某个 DOCUMENT score 很高
```

也不会被选来满足 ER1。

因为：

```text
ER1 明确要求代码证据
```

---

# 17. 如果是 BOTH 呢？

这个是当前筛选中特别重要的一步。

假设：

```text
ER1
source_requirement = BOTH
```

意味着：

> 这个 Requirement 既需要 CODE，又需要 DOCUMENT。

第一次：

```python
choose(question)
```

可能先选到：

```text
CODE
```

然后系统专门检查：

```python
present = {
    当前为 ER1 已选的 source_type
}
```

如果发现：

```text
有 CODE
没 DOCUMENT
```

就会额外执行：

```python
choose(question, "DOCUMENT")
```

反之亦然。

所以 BOTH 的目标是：

```text
ER1
├─ 至少一个 CODE anchor
└─ 至少一个 DOCUMENT anchor
```

这就是所谓：

> **双来源锚点保留。**

---

# 18. 为什么这个机制很重要？

假设用户问：

> “项目设计文档说使用本地事务保证一致性，当前代码是怎么实现的？”

如果纯 TopK：

```text
5 个结果可能全是 CODE
```

或者：

```text
全是 DOCUMENT
```

那就无法真正比较：

```text
设计
vs
实现
```

所以 BOTH 会强制尽量留下两个来源。

---

# 19. 第三个筛选原则：SUPPORTING 后处理

处理完所有 CORE 后：

```python
for question in supporting:
    choose(question)
```

也就是说：

```text
CORE
优先拿位置

SUPPORTING
后拿位置
```

如果 Context 很小：

```text
SUPPORTING
可能一个都进不去
```

这是刻意设计的。

因为：

> 先保证能正确回答，再考虑补充解释。

---

# 20. 然后才填充剩余槽位

假设：

```text
top_k = 8
```

前面已经因为 CORE/BOTH 选了：

```text
4 条
```

还剩：

```text
4 条
```

系统就把所有还未选择的候选拿出来：

```python
remaining = [...]
```

再统一排序：

```text
effective_priority
+
该候选原本在自己搜索里的 rank
```

然后继续填：

```text
直到 max_results
```

---

# 21. 这里还有一次全局去重

有：

```python
selected_ids: set[int]
```

假设：

```text
chunk 101
```

同时属于：

```text
ER1
ER2
```

最终 Context 只会真正放：

```text
一份 chunk 101
```

不会：

```text
C1 = chunk101
C2 = chunk101
```

但是 Annotation 会记录：

```text
Supports: ER1, ER2
```

所以：

> **内容只出现一次，但“它支持哪些 Requirement”不会丢。**

这个设计很好。

---

# 22. 筛选完之后会产生 `annotations`

例如：

```python
annotations[101] = EvidenceAnnotation(
    source_role="IMPLEMENTATION",
    temporal_status="CURRENT",
    authority_priority=100,
    sub_question_ids=("ER1", "ER2")
)
```

也就是给每个最终选中 Chunk 附带：

```text
它是什么证据
它是什么时间状态
权威性多少
它支持哪些 ER
```

这些信息后面都会带进 Context。

---

# 23. 到这里完成第一次大筛选

原来可能：

```text
ER1 → 5 条
ER2 → 5 条
ER3 → 5 条

总共 15 个候选
```

经过 `EvidencePool.select(top_k=8)`：

```text
最终只留下 8 个唯一 Chunk
```

而且尽量保证：

```text
CORE coverage
↓
BOTH 双来源
↓
SUPPORTING
↓
剩余高质量候选
```

所以它不是单纯：

```text
15 条直接 score 排序取前 8
```

---

# 24. 然后进入 `ContextBuilder`

现在得到：

```text
SearchResult[]
+
EvidenceAnnotation
```

进入：

```python
ContextBuilder.build()
```

这又是第二次筛选。

主要解决两个问题：

```text
1. 再去重
2. 字符预算有限怎么办
```

---

# 25. ContextBuilder 第一件事：再次去重

```python
ranked_results = self._deduplicate(results)
```

仍然按：

```text
SearchResult.id
```

去重。

这属于防御性设计。

理论上 EvidencePool 已经去重了，但：

> ContextBuilder 不假设调用者一定传干净数据。

所以再检查一次。

---

# 26. ContextBuilder 还有一个 CODE / DOCUMENT anchor 机制

这一点和 EvidencePool 有点类似，但目的不同。

它会找：

```python
first_code
first_document
```

如果当前结果里两种都有：

```text
CODE
DOCUMENT
```

并且两个完整块能够塞入 Context Budget，

它会把：

```text
第一个 CODE
第一个 DOCUMENT
```

提前到最前面。

例如原顺序：

```text
CANDIDATE 1 CODE
CANDIDATE 2 CODE
CANDIDATE 3 CODE
CANDIDATE 4 DOCUMENT
```

会变成：

```text
CODE anchor
DOCUMENT anchor
剩余结果...
```

---

# 27. 为什么还要再做一次 source anchor？

因为接下来有：

```text
max_chars
```

限制。

如果一直按原排序：

```text
CODE
CODE
CODE
CODE
DOCUMENT
```

可能前几个大代码块把 Context 填满：

```text
DOCUMENT
永远没机会进去
```

所以 ContextBuilder 尽量先放：

```text
一个 CODE
+
一个 DOCUMENT
```

这属于：

> **防止字符预算导致某一种来源彻底消失。**

---

# 28. 接下来按照字符预算逐条塞入

例如当前 Budget：

```text
16000 chars
```

系统逐条：

```text
C1
↓
能放下 → 放

C2
↓
能放下 → 放

C3
↓
能放下 → 放
```

直到遇到一条：

```text
完整内容放不下
```

---

# 29. 最后一条允许截断

假设：

```text
剩余预算 = 1800 chars

当前 Chunk = 4000 chars
```

那么不会完全丢掉。

系统计算：

```text
header 占多少
truncation marker 占多少
```

然后截取：

```text
前 N 个字符
```

并加：

```text
… [truncated]
```

同时：

```python
item.truncated = True
```

---

# 30. 一旦截断一条，就停止

这点值得注意。

代码：

```python
if 超预算:
    截断当前 item
    break
```

所以不会：

```text
截断 C5
然后再去尝试 C6
```

而是：

```text
C5 部分进入
↓
结束 Context 构建
```

因此前面的排序非常重要。

因为：

> 越靠前的证据越有机会完整进入 Context。

---

# 31. 最终每个 ContextItem 不只是正文

会构造：

```python
ContextItem
```

其中保留：

```text
citation
content
chunk_id
chunk_type
score
retrieval_rank
source_role
temporal_status
authority_priority
sub_question_ids
```

所以最终 LLM 看到的不是：

```text
一坨代码文本
```

而是类似：

```text
[C1] CODE
File: xxx/PurchaseTicketTxService.java
Symbol: PurchaseTicketTxService#doPurchaseInTransaction
Signature: ...
Lines: 120-180
Score: ...
Evidence:
role=IMPLEMENTATION
time=CURRENT
priority=100
Supports: ER1, ER2

<真实代码>
```

---

# 32. 最终得到 `ContextBundle`

结构：

```python
ContextBundle(
    query,
    items,
    rendered_text,
    total_chars,
    max_chars,
    truncated
)
```

此时：

```text
检索得到的原始候选
```

已经变成：

```text
经过 Requirement-aware 筛选
经过 SourcePolicy 筛选
经过去重
经过 CODE/DOC 锚点保护
经过字符预算控制
的最终证据 Context
```

---

# 33. 用一个完整小例子理解

假设有：

```text
ER1 CORE：事务
ER2 CORE：补偿
ER3 SUPPORTING：设计背景
```

检索回来 12 条：

```text
ER1:
A CODE current
B CODE current
C old document
D CODE

ER2:
E CODE current
F CODE current
G document

ER3:
H document
I document

还有一些重复 Chunk
```

---

先经过 `SourcePolicy`：

```text
A → IMPLEMENTATION CURRENT 100
B → IMPLEMENTATION CURRENT 100
C → HISTORICAL_PLAN HISTORICAL 40
...
```

进入 EvidencePool。

---

`select()` 首先：

```text
ER1 CORE → 选 A
ER2 CORE → 选 E
```

然后：

```text
ER3 SUPPORTING → 选 H
```

还剩槽位：

```text
继续从 B/F/G/I...
```

按：

```text
effective priority + rank
```

填充。

例如最终：

```text
A
E
H
B
F
G
```

而：

```text
历史文档 C
```

因为 CURRENT 查询下优先级低，可能被淘汰。

---

然后 ContextBuilder：

```text
去重
↓
保证 CODE / DOCUMENT anchor
↓
按 max_chars 填
↓
必要时截断最后一块
```

最终可能：

```text
C1 A CODE
C2 H DOCUMENT
C3 E CODE
C4 B CODE
C5 F CODE（截断）
```

进入后续阶段。

---

# 34. 所以当前筛选其实有三层思想

你可以把它记成：

### 第一层：这个结果“是什么证据”

```text
SourcePolicy
```

回答：

```text
当前实现？
历史计划？
当前设计？
验证文档？
```

---

### 第二层：这个结果“该不该被选”

```text
EvidencePool.select()
```

回答：

```text
是不是 CORE？
支持哪个 ER？
来源符合要求吗？
BOTH 是否两边都有？
有没有重复？
```

---

### 第三层：这个结果“能不能塞进上下文”

```text
ContextBuilder
```

回答：

```text
有没有重复？
CODE/DOC 是否至少各保留一个？
字符预算够不够？
是否需要截断？
```

---

# 35. 这里最核心的一句话

当前 DevContext 并不是：

```text
Retriever TopK
=
最终 Context TopK
```

而是：

```text
Retriever
先负责“找候选”

↓

EvidencePool
负责“按证据需求选候选”

↓

ContextBuilder
负责“在上下文预算内装载证据”
```

这三个职责要分开。

---

# 36. 到这里先停

现在流程已经走到：

```text
SearchAction
↓
Retrieval
↓
SearchResult[]
↓
SourcePolicy
↓
EvidencePool
↓
EvidencePool.select()
↓
ContextBuilder
↓
ContextBundle
```

**下一步才是 `CoverageChecker`：系统拿着这个 `ContextBundle`，怎么判断 ER1/ER2/ER3 哪些已经证据充分、哪些 PARTIAL、哪些 MISSING，并据此决定是否触发第二轮搜索。**

这个阶段正好也能解释你之前看到的 `Second-round Rescue = 40%` 是怎么产生的。