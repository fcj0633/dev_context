好，这一步我们只讲：

> **第 2 步：`EvidencePlan → SearchActionPlanner → 第一轮 SearchAction`**

也就是：

```text
上一阶段：
“我要找哪些事实？”

这一阶段：
“为了找到这些事实，我应该搜什么？”
```

还**没有真正查数据库**。真正执行关键词 / 向量 / Hybrid，是下一步。

---

# 1. 从上一阶段拿到什么？

上一阶段输出：

```python
EvidencePlan
```

里面有若干：

```python
EvidenceRequirement
```

例如为了理解，假设得到：

```text
ER1
target:
确认购票过程中数据库事务的边界

success_criteria:
需要找到事务入口、事务覆盖的操作以及失败回滚行为

priority:
CORE

source_requirement:
CODE
```

以及：

```text
ER2
target:
确认购票过程中分布式锁如何参与并发控制

success_criteria:
需要找到锁的获取位置、锁粒度以及释放方式

priority:
CORE

source_requirement:
CODE
```

现在系统已经知道：

```text
需要找什么
```

但是还不知道：

```text
应该拿什么字符串去搜索
```

所以进入：

```python
SearchActionPlanner
```

---

# 2. 在 `RetrievalController` 里怎么进入这一步？

当前流程里先做了：

```python
plan = self.evidence_planner.plan(
    request.original_query
)
```

然后：

```python
first_requirements = tuple(
    sorted(
        plan.requirements,
        key=lambda item: (
            item.priority != "CORE",
            int(item.id[2:])
        ),
    )
)
```

这里先做了一件很简单但重要的事情：

> **CORE Requirement 优先。**

例如：

```text
原本：
ER1 SUPPORTING
ER2 CORE
ER3 CORE

排序后：
ER2 CORE
ER3 CORE
ER1 SUPPORTING
```

这样后面的检索会优先处理真正影响答案完整性的证据。

---

# 3. 然后调用 `SearchActionPlanner`

代码大致是：

```python
first_actions = self.action_planner.plan_actions(
    request.original_query,
    first_requirements,
    round_index=0,
)
```

注意几个参数：

```text
original_query
requirements
round_index = 0
```

`round_index=0` 表示：

> **这是第一次搜索。**

此时还没有：

```text
previous search result
missing criteria
discovered symbol
```

所以第一轮 Planner 能看到的信息主要就是：

```text
用户原问题
+
EvidenceRequirement
```

---

# 4. `SearchActionPlanner` 的任务是什么？

它不是重新决定：

> “需要哪些证据？”

这个事情已经由 EvidencePlanner 做完了。

它只负责：

> **针对每个 EvidenceRequirement，生成一个适合检索的 Query。**

所以职责关系是：

```text
EvidencePlanner
↓
WHAT
“我要找什么事实？”

SearchActionPlanner
↓
HOW TO SEARCH
“先用什么查询表达去找这个事实？”
```

注意，目前它还不是完整的：

```text
Tool Planner
```

因为它现在主要决定的是：

```text
Query
```

而不是：

```text
semantic_search?
symbol_search?
read_file?
find_references?
```

这一点正是我们前面讨论未来要升级的地方。

---

# 5. 当前 `SearchAction` 长什么样？

目前核心结构可以理解成：

```python
SearchAction(
    action_id,
    requirement_id,
    round_index,
    query,
    source_scope,
    reason,
    decision_source,
)
```

例如可能得到：

```python
SearchAction(
    action_id="SA1",
    requirement_id="ER1",
    round_index=0,
    query="购票 占座 事务 数据一致性",
    source_scope="CODE",
    reason="查找购票事务及数据库更新实现",
    decision_source="llm",
)
```

这一步最重要的字段是：

```text
requirement_id
query
source_scope
```

---

# 6. `requirement_id` 有什么作用？

例如：

```text
SA1 → ER1
SA2 → ER2
SA3 → ER3
```

这意味着：

> 后面搜回来的证据，不只是“相关结果”，而是知道自己是在支持哪个 Requirement。

比如：

```text
ER1：
确认事务边界

SA1：
搜索事务相关代码
```

得到：

```text
PurchaseTicketTxService.doPurchaseInTransaction
```

后面 EvidencePool 就能记录：

```text
这个 Chunk 是为了支持 ER1 找到的
```

这对后续 CoverageChecker 非常重要。

否则所有 Chunk 混在一起：

```text
Result1
Result2
Result3
```

系统根本不知道：

> 哪些证据是在证明哪一个事实。

---

# 7. `query` 是怎么来的？

如果有 LLM：

```python
client.generate(...)
```

SearchActionPlanner 会把：

```text
原始问题
EvidenceRequirement.target
EvidenceRequirement.success_criteria
source_requirement
```

交给 LLM。

让模型生成：

```text
适合查当前项目的检索表达
```

例如用户原问题：

```text
当前购票占座的数据一致性是如何保证的？
```

Requirement：

```text
确认购票数据库事务边界
```

最终 Query 可能是：

```text
购票 占座 事务 数据库 更新
```

或者：

```text
购票事务 seat ticket update
```

这一步才第一次开始：

> **把用户自然语言目标转换成“检索语言”。**

---

# 8. 为什么不能直接拿 `target` 搜？

这是非常关键的。

假设 Requirement：

```text
确认数据库层最终阻止重复状态更新的机制
```

这个表达很适合人理解。

但是源码很可能根本不存在：

```text
“最终阻止重复状态更新”
```

这种文字。

真正代码可能是：

```sql
UPDATE ticket
SET status = ?
WHERE id = ?
AND status = ?
```

或者代码里：

```java
updateWrapper.eq(TicketDO::getStatus, oldStatus);
```

所以：

```text
EvidenceRequirement
```

是：

> **描述证据需求。**

而：

```text
Search Query
```

应该：

> **尽量接近项目中可能出现的表达。**

两者不能完全等同。

---

# 9. 这一点也是当前第二轮存在问题的根源之一

第一轮时：

```text
Requirement
→ Query
```

通常是合理的。

因为系统什么项目 Symbol 都不知道。

但是第二轮时情况已经变了。

比如第一轮已经发现：

```text
PurchaseTicketTxService
doPurchaseInTransaction
```

这时候还继续：

```text
Requirement
→ 再生成一个自然语言 Query
```

就可能浪费掉非常有价值的信息。

这也是为什么你当前：

```text
Second-round Rescue ≈ 40%
```

还有明显提升空间。

不过第二轮我们暂时不讲，后面单独拆。

---

# 10. 第一轮为什么一个 Requirement 一个 SearchAction？

目前设计基本是：

```text
ER1
↓
SA1

ER2
↓
SA2

ER3
↓
SA3
```

而不是：

```text
所有 Requirement
↓
一个超级 Query
```

这样做的好处非常大。

假设用户问：

> “解释购票一致性。”

如果只生成：

```text
购票 一致性 Redis Lock Transaction MQ
```

很容易 Top5 全被某一类内容占满。

比如：

```text
4 个 Redis Chunk
1 个 Lock Chunk
```

结果事务证据完全没有。

现在则：

```text
ER1：事务
→ 专门搜索事务

ER2：锁
→ 专门搜索锁

ER3：补偿
→ 专门搜索补偿
```

这就是：

> **Requirement-driven Retrieval**

而不是：

> **Question-driven single-query Retrieval**

这是当前架构非常重要的优点。

---

# 11. `source_scope` 从哪里来？

它基本继承：

```python
EvidenceRequirement.source_requirement
```

例如：

```text
ER1.source_requirement = CODE
```

那么 SearchAction：

```text
source_scope = CODE
```

如果 Requirement 是：

```text
DOCUMENT
```

那么：

```text
source_scope = DOCUMENT
```

如果：

```text
BOTH
```

后面的 RetrievalPolicy 会知道：

> 代码和文档两边都要处理。

所以 SearchAction 不只是：

```text
搜什么
```

还带着：

```text
在哪类来源里搜
```

---

# 12. 为什么不是让 LLM 自己决定 CODE / DOCUMENT？

因为这个决定已经在 EvidencePlanner 阶段完成了。

例如：

```text
ER1：
确认当前方法是否有事务
source_requirement = CODE
```

SearchActionPlanner 不应该突然说：

```text
我觉得搜文档吧
```

否则各层职责会混乱。

现在的设计是：

```text
EvidencePlanner：
决定证据类型

SearchActionPlanner：
生成具体搜索表达

RetrievalPolicy：
决定底层到底怎么执行
```

职责比较清楚。

---

# 13. Planner 也会严格校验 LLM 输出

和 EvidencePlanner 一样：

> 不是模型输出什么都接受。

当前它要求每一个 Action 必须有：

```text
requirement_id
query
reason
```

而且数量要和输入 Requirement 一致。

例如输入：

```text
ER1
ER2
ER3
```

模型必须生成：

```text
ER1 对应一个 Action
ER2 对应一个 Action
ER3 对应一个 Action
```

不能漏掉：

```text
ER2
```

也不能多生成：

```text
ER4
```

---

# 14. 程序自己生成 `SA1 / SA2`

和 EvidenceRequirement 一样：

LLM不掌握稳定 ID。

程序根据位置生成：

```python
SearchAction(
    f"SA{len(history) + offset}",
    ...
)
```

第一轮可能：

```text
SA1
SA2
SA3
```

第二轮继续：

```text
SA4
SA5
```

所以整个历史可以记录：

```text
SA1 Round0
SA2 Round0
SA3 Round0
SA4 Round1
...
```

这对评测、Trace 很有用。

---

# 15. 第一轮不存在 `discovered_terms`

你特别要注意这个区别。

第一次调用：

```python
plan_actions(
    original_query,
    first_requirements,
    round_index=0
)
```

没有传：

```text
history
coverage
discovered_terms
```

因为：

```text
还没真正搜索
↓
自然也没有发现任何项目 Symbol
```

所以第一轮本质上是：

```text
Natural Language
↓
Natural Language Query Generation
```

例如：

```text
用户：
为什么购票不会超卖？

↓

Requirement：
确认并发购票过程中阻止重复占用座位的实现

↓

Search Query：
购票 并发 座位 锁 占座
```

这种情况非常适合 Semantic / Hybrid Discovery。

---

# 16. 如果 LLM 生成 SearchAction 失败怎么办？

同样有 fallback。

核心思想大致是把：

```text
original_query
+
requirement.target
+
其他可利用信息
```

拼起来形成查询。

第一轮由于没有 missing criteria 和 discovered terms，大概类似：

```text
原始问题 + requirement.target
```

例如：

```text
详细解释购票一致性
+
确认数据库事务边界
```

得到一个可执行 Query。

所以即使：

```text
LLM API 出错
JSON 不合法
```

系统仍然可以继续。

---

# 17. 现在这一阶段结束时，得到什么？

比如 EvidencePlan：

```text
ER1：确认事务
ER2：确认锁
ER3：确认失败补偿
```

经过 SearchActionPlanner 后可能变成：

```text
SA1
requirement = ER1
query = "购票 事务 seat ticket update"
scope = CODE

SA2
requirement = ER2
query = "购票 分布式锁 座位 lock"
scope = CODE

SA3
requirement = ER3
query = "购票失败 订单失败 补偿 rollback"
scope = CODE
```

现在系统已经从：

```text
“我需要什么证据？”
```

推进到了：

```text
“第一轮我准备分别搜什么？”
```

---

# 18. 但现在依然还没查数据库

这是这一条最重要的边界。

当前：

```text
User Query
↓
EvidencePlanner
↓
EvidencePlan
↓
SearchActionPlanner
↓
SearchAction[]
```

到这里为止：

```text
❌ 还没有 PostgreSQL

❌ 还没有 Keyword Search

❌ 还没有 Vector Search

❌ 还没有 Hybrid

❌ 还没有 Top5 Result
```

只是已经准备好了：

```text
要执行的搜索任务
```

---

# 19. 你可以把这一步类比成什么？

如果你自己调查一个 Java 项目：

上一阶段：

```text
我要搞清楚：
1. 事务
2. 锁
3. 补偿
```

这一阶段：

```text
那我先准备搜索：

“purchase transaction”
“seat lock”
“order failure compensation”
```

但你此时还**没有按 Enter**。

下一步才是真正：

> 按下搜索键。

---

# 20. 到目前为止的完整两步

现在你可以先形成这张图：

```text
用户问题
   ↓
UserRequest
   ↓
EvidencePlanner
   ↓
EvidencePlan
   │
   ├─ ER1
   ├─ ER2
   └─ ER3
   ↓
SearchActionPlanner
   ↓
SearchAction[]
   │
   ├─ SA1(query, source_scope)
   ├─ SA2(query, source_scope)
   └─ SA3(query, source_scope)
```

两个 Planner 的区别一定要分清：

```text
EvidencePlanner
→ 决定要证明哪些事实

SearchActionPlanner
→ 决定第一轮用什么 Query 去寻找这些事实
```

而**下一步**才会进入当前检索过程中非常关键的一层：

> `SearchAction → RetrievalController._execute() → RetrievalPolicy`

也就是：**SA1 的 Query 到底怎么被决定走 Keyword、Vector、Hybrid，以及最终怎么从 PostgreSQL/pgvector 取回 TopK Chunk。**

这一步会涉及你前面一直关心的 `CODE keyword Recall@5`、Hybrid、MIXED 策略，所以值得单独详细讲。