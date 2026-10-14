这一阶段只讲：

> **`ContextBundle → CoverageChecker → RequirementCoverage`**

也就是系统已经拿到一批经过筛选的证据后，如何判断：

```text
ER1 已经够了
ER2 只找到一部分
ER3 完全没找到
```

这一步非常关键，因为它决定：

> **是否还要继续检索。**

---

# 1. 输入是什么

上一阶段最终得到：

```python
ContextBundle
```

里面有若干：

```python
ContextItem
```

每个 `ContextItem` 已经知道：

```text
它来自哪个文件
是 CODE 还是 DOCUMENT
对应哪个 class / method
支持哪些 ER
正文内容是什么
```

例如：

```text
C1
PurchaseTicketTxService#doPurchaseInTransaction
Supports: ER1

C2
PurchaseTicketServiceImpl#purchaseTickets
Supports: ER1, ER2

C3
购票设计文档
Supports: ER2
```

然后执行：

```python
coverage = self.coverage_checker.check(
    plan.requirements,
    bundle
)
```

---

# 2. CoverageChecker 不是判断“结果相关不相关”

它判断的是更严格的问题：

> **这些证据是否已经满足 EvidenceRequirement 的 `target + success_criteria`？**

比如：

```text
ER1

target:
确认购票事务边界

success_criteria:
必须看到事务入口、
事务包含哪些数据库操作、
失败时的回滚行为
```

现在虽然找到：

```java
@Transactional
public void doPurchaseInTransaction() {
    ...
}
```

但如果 Chunk 里没有说明：

```text
具体修改哪些表
失败如何回滚
```

那么不能简单因为看到：

```text
@Transactional
```

就判断：

```text
SATISFIED
```

更可能是：

```text
PARTIAL
```

---

# 3. CoverageChecker 第一层：先做确定性来源检查

它不是一上来就让 LLM 判断。

第一步先用程序规则检查：

```python
source_gap = _missing_sources(
    requirement,
    items
)
```

这一步只关心：

> **要求的来源类型有没有真正进入最终 Context。**

---

# 4. 比如 ER 要求 CODE

假设：

```text
ER1.source_requirement = CODE
```

但当前 ER1 名下只有：

```text
DOCUMENT
DOCUMENT
```

即使两个文档讲得非常像正确答案，也直接判：

```text
PARTIAL
```

如果一条证据都没有：

```text
MISSING
```

并且给出：

```text
missing_criteria:
缺少直接相关的 CODE 证据
```

---

# 5. BOTH 更严格

如果：

```text
source_requirement = BOTH
```

那么必须同时存在：

```text
CODE
+
DOCUMENT
```

例如现在：

```text
CODE ✅
DOCUMENT ❌
```

那么直接：

```text
PARTIAL
```

根本不会交给 LLM 判断语义是否充分。

因为来源条件本身都没满足。

---

# 6. 所以第一层可以理解为“硬约束”

```text
source_requirement
      ↓
程序规则检查
      ↓
来源不完整？
   ↙        ↘
 Yes        No
 ↓           ↓
直接       进入语义
PARTIAL     Coverage
/MISSING
```

这一步不需要 LLM。

---

# 7. 一个很重要的细节：截断证据不参与 Coverage

代码筛选证据时：

```python
items = [
    item
    for item in context.items
    if requirement.id in item.sub_question_ids
    and not item.truncated
]
```

注意：

```python
and not item.truncated
```

也就是说，如果 ContextBuilder 最后一条证据被截断了：

```text
C5 ... [truncated]
```

它不会拿来证明 ER 已经满足。

这是一个比较保守的设计。

原因是：

> 被截断后，关键结论可能刚好位于没进入 Context 的后半段。

因此不能拿“不完整证据” confidently 判：

```text
SATISFIED
```

---

# 8. 来源满足后，才进入 LLM 语义判断

假设：

```text
ER1 要 CODE
```

现在确实有：

```text
C1 CODE
C2 CODE
```

那么才进入：

```python
_semantic_check()
```

此时真正要回答：

> 这些代码内容是否足够满足 `success_criteria`？

---

# 9. 给 LLM 的输入不是整个 Context 混在一起

这里设计得比较好。

它会按 Requirement 组织：

```json
{
  "requirement_id": "ER1",
  "target": "...",
  "success_criteria": "...",
  "evidence": [...]
}
```

其中 `evidence` 只包含：

> **被 EvidencePool 标记为支持 ER1 的 ContextItem。**

也就是说：

```text
ER1
只看 ER1 的证据

ER2
只看 ER2 的证据
```

不会让 ER1 偷用 ER2 的证据。

Prompt 里也明确规定：

> 不得使用其他 Requirement 的证据补足当前 Requirement。

---

# 10. 为什么要这样隔离

假设：

```text
ER1：确认事务
ER2：确认 Redis 锁
```

当前：

```text
ER1 证据只找到了一点事务代码
ER2 找到了非常详细的 Redis 锁代码
```

如果把整个 Context 一股脑给模型，它可能产生一种错觉：

> “整体信息很多，好像已经比较充分。”

然后把 ER1 误判为 SATISFIED。

现在按 ER 隔离以后：

```text
ER1 判断时
只能看到 ER1 的证据
```

更严格。

---

# 11. LLM 最终只能输出三种正常状态

```text
SATISFIED
PARTIAL
MISSING
```

另外程序还有：

```text
UNVERIFIED
```

它不是 LLM 正常判断出来的，而是 Coverage 检查失败时的 fallback 状态。

---

# 12. SATISFIED

表示：

> 当前证据已经满足 target 和 success_criteria。

例如：

```text
ER1:
确认事务入口、范围、失败回滚
```

已有证据：

```text
C1:
@Transactional 方法入口

C2:
方法内部明确修改 Seat/Ticket

C3:
异常继续抛出，由 Spring 事务回滚
```

LLM可能输出：

```text
state = SATISFIED

evidence_ids = [101, 108, 112]

missing_criteria = []
```

---

# 13. PARTIAL

表示：

> 找到了相关证据，但不足以完整证明 Requirement。

例如：

```text
发现了 @Transactional
也看到了 Seat 更新
```

但没看到：

```text
失败回滚行为
```

则：

```text
state = PARTIAL
```

并输出：

```text
missing_criteria:
- 尚缺异常发生时的事务回滚证据
```

这个 `missing_criteria` 非常重要。

因为它会成为：

> **第二轮补查的输入。**

---

# 14. MISSING

表示：

> 当前没有足够直接相关的证据。

比如 ER：

```text
确认订单创建失败后的补偿逻辑
```

但 Context 里完全没有：

```text
补偿
异常处理
订单失败
```

那么：

```text
MISSING
```

例如：

```text
missing_criteria:
- 尚缺订单创建失败后的处理入口
- 尚缺已创建车票如何恢复的直接代码证据
```

---

# 15. `missing_criteria` 最多 3 条

代码限制：

```python
MAX_MISSING_CRITERIA = 3
```

这实际上是为了避免：

```text
CoverageChecker
```

输出一大堆泛泛问题。

它应该只告诉下一轮：

> **最关键还缺什么。**

例如比较好的：

```text
缺少 doPurchaseInTransaction 的异常回滚证据
```

而不是：

```text
还需要进一步了解系统整体架构、
数据库设计、异常机制、
分布式一致性……
```

---

# 16. `evidence_ids` 也受到严格限制

LLM不能随便说：

```text
证据是 Chunk 9999
```

程序会检查：

```python
allowed_ids = {
    item.chunk_id
    for item in evidence_by_id[requirement.id]
}
```

LLM输出的 `evidence_ids` 必须来自：

> 当前 ER 真正拥有的 ContextItem。

否则 Coverage 结果直接判非法。

---

# 17. SATISFIED 还必须满足两个程序规则

如果 LLM说：

```text
SATISFIED
```

那么：

### 必须有证据 ID

不能：

```text
SATISFIED
evidence_ids = []
```

否则非法。

---

### 不能还有 missing_criteria

不能：

```text
SATISFIED

但还缺：
事务回滚证据
```

这种自相矛盾输出会被拒绝。

---

# 18. PARTIAL / MISSING 反过来也必须说明缺什么

如果：

```text
state != SATISFIED
```

却：

```text
missing_criteria = []
```

也会判非法。

因为如果你说：

```text
证据不充分
```

就必须告诉系统：

> **到底还缺什么。**

否则第二轮没法针对性补查。

---

# 19. 所以 `RequirementCoverage` 最终长这样

结构：

```python
RequirementCoverage(
    requirement_id,
    state,
    evidence_ids,
    missing_criteria,
    reason,
    decision_source
)
```

例如：

```text
ER1

state:
SATISFIED

evidence_ids:
101, 103

missing_criteria:
[]

reason:
已有代码明确展示事务入口和覆盖的数据库更新
```

而：

```text
ER2

state:
PARTIAL

evidence_ids:
205

missing_criteria:
["缺少订单创建失败后的补偿实现"]

reason:
已有失败路径相关代码，但未展示补偿动作
```

---

# 20. 最终可能得到这样一组 Coverage

例如：

```text
ER1 事务
→ SATISFIED

ER2 分布式锁
→ SATISFIED

ER3 失败补偿
→ PARTIAL

ER4 设计背景
→ MISSING
```

现在系统第一次真正知道：

```text
哪些事实已经证明
哪些事实还没有证明
```

---

# 21. 哪些 Requirement 会触发第二轮？

这里非常关键。

当前代码只选择：

```python
item.priority == "CORE"
```

并且：

```python
state in {"PARTIAL", "MISSING"}
```

也就是：

```text
CORE + PARTIAL
→ 补查

CORE + MISSING
→ 补查
```

而：

```text
SUPPORTING + MISSING
```

不会触发第二轮。

---

# 22. 为什么 SUPPORTING 不补查？

因为第二轮搜索预算有限。

目标不是：

> 把所有可能有价值的信息全部找齐。

而是：

> **优先把“没有它就不能完整回答”的 CORE 证据补齐。**

比如：

```text
ER1 CORE：事务机制
ER2 CORE：失败补偿
ER3 SUPPORTING：设计背景
```

如果 ER2 没找到：

```text
必须继续搜
```

但 ER3 没找到：

```text
可以先不搜
```

否则 Agent 非常容易无限扩张。

---

# 23. 这就是 Second-round Rescue 的起点

你之前看到：

```text
15 个 CORE 进入第二轮
```

这些就是：

```text
第一次 Coverage 后
状态为 PARTIAL / MISSING
```

的 CORE Requirement。

然后其中：

```text
6 个经过第二轮变成 SATISFIED
```

所以：

```text
Second-round Rescue
= 6 / 15
= 40%
```

这个指标现在应该就非常直观了。

---

# 24. 如果 CoverageChecker 自己失败怎么办？

例如：

```text
LLM API 超时
JSON 不合法
输出字段不对
evidence_ids 非法
```

它最多尝试：

```text
2 次
```

如果仍然失败，不会乱猜：

```text
SATISFIED
```

而是变成：

```text
UNVERIFIED
```

例如：

```python
RequirementCoverage(
    state="UNVERIFIED",
    missing_criteria=(requirement.success_criteria,),
    reason="已有候选证据，但语义覆盖检查未能完成"
)
```

也就是说：

> **检查失败 ≠ 证据充分。**

这是为了防止 False READY。

---

# 25. 注意一个当前实现细节

第二轮触发条件是：

```python
state in {"PARTIAL", "MISSING"}
```

也就是说：

```text
UNVERIFIED
```

当前**不会进入第二轮补查**。

因为 UNVERIFIED 表示：

> 不是检索证据明确不足，而是“Coverage Checker 本身没完成判断”。

这时候重新检索不一定有意义。

问题更可能在：

```text
Coverage LLM / parsing
```

而不是 Retriever。

---

# 26. 为什么 CoverageChecker 这么重要

你可以想象没有它会怎样。

普通 RAG：

```text
搜 Top5
↓
直接给 LLM
↓
回答
```

即使 Top5 中：

```text
有事务
有锁
但完全没有失败补偿
```

LLM仍然可能根据已有上下文把回答“补完整”。

这就容易幻觉。

DevContext 多了一层：

```text
Context
↓
CoverageChecker
```

明确告诉系统：

```text
ER3 失败补偿
还没证明
```

于是系统可以：

```text
继续搜
```

而不是：

```text
直接猜
```

---

# 27. 这一步其实把“检索是否充分”变成了显式状态

以前只有：

```text
Search Score
```

例如：

```text
0.91
0.88
0.84
```

但高分不等于：

> 回答所需事实已经完整。

现在变成：

```text
ER1 SATISFIED
ER2 SATISFIED
ER3 PARTIAL
```

这个信息对 Agent 更有价值。

因为它回答的是：

> **当前调查完成到什么程度？**

---

# 28. 一个完整示例

用户问：

> “详细解释购票数据一致性是如何保证的。”

EvidencePlan：

```text
ER1 CORE
确认事务边界

ER2 CORE
确认并发控制

ER3 CORE
确认失败补偿
```

第一轮 Context：

```text
C1 PurchaseTicketTxService
Supports ER1

C2 PurchaseTicketServiceImpl 锁相关
Supports ER2

C3 OrderRemoteService 调用
Supports ER3
```

Coverage：

### ER1

证据中包含：

```text
@Transactional
Seat update
Ticket insert
```

因此：

```text
SATISFIED
```

---

### ER2

证据包含：

```text
用户锁
座位锁
lock/unlock
```

因此：

```text
SATISFIED
```

---

### ER3

只看到：

```text
调用订单服务
```

但是没有看到：

```text
订单失败以后怎么处理
```

因此：

```text
PARTIAL

missing_criteria:
缺少订单创建失败后的票务补偿实现
```

于是下一阶段只需要针对：

```text
ER3
```

继续搜索。

---

# 29. 到这里流程变成

```text
EvidencePlan
↓
SearchAction
↓
Retrieval
↓
EvidencePool
↓
ContextBundle
↓
CoverageChecker
        ↓
┌───────────────┐
│ ER1 SATISFIED │
│ ER2 SATISFIED │
│ ER3 PARTIAL   │
└───────────────┘
        ↓
发现：
ER3 是 CORE
而且 PARTIAL
        ↓
需要第二轮
```

---

# 30. 当前这一步最值得记住的三个概念

### 第一，Coverage 不是相关性

```text
相关 ≠ 充分
```

找到了相关代码：

```text
不代表已经满足 success_criteria
```

---

### 第二，Coverage 是按 Requirement 判断

不是：

```text
“整个 Context 看起来够不够？”
```

而是：

```text
ER1 够不够？
ER2 够不够？
ER3 够不够？
```

---

### 第三，`missing_criteria` 是下一轮的桥梁

第一轮不是简单得到：

```text
失败
```

而是得到：

```text
失败 + 到底缺什么
```

这使第二轮可以从：

```text
盲目再搜一次
```

升级成：

```text
针对缺失证据补查
```

---

到这里先停。

下一步就是整条链里**目前最值得分析的一步**：

> **对于 `CORE + PARTIAL/MISSING` 的 Requirement，系统如何收集第一轮发现的真实 `class_name / symbol_name / signature / file_path`，再生成第二轮 SearchAction。**

这一步也正好能解释为什么你当前第二轮只有 **40% Rescue**，以及未来为什么要从“Query Rewrite”升级成“Tool Replanning”。