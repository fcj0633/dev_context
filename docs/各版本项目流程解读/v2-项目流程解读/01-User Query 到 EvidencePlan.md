好，我们严格按“单步调试”的方式来。**这一条只讲第 1 步：用户问题如何变成 `EvidencePlan`。** 暂时不进入 SearchAction、关键词/向量检索。

---

# 第 1 步：User Query → EvidencePlan

当前主流程的入口在：

```python
RetrievalController.retrieve(request, top_k)
```

其中真正开始检索流程的第一件事是：

```python
plan = self.evidence_planner.plan(request.original_query)
```

所以目前链路刚开始是：

```text
用户问题
   ↓
UserRequest
   ↓
EvidencePlanner
   ↓
EvidencePlan
```

注意：

> **这一阶段还没有搜索数据库，也没有生成关键词 Query，也没有做向量检索。**

它只解决一个问题：

> **“要回答用户这个问题，我必须先找到哪些事实？”**

---

## 1. 用户问题先被包装成 `UserRequest`

代码在：

```text
src/devcontext/request.py
```

核心结构：

```python
@dataclass(frozen=True, slots=True)
class UserRequest:
    original_query: str
    answer_options: AnswerOptions = AnswerOptions()
    context_budget_override: int | None = None
```

例如用户问：

```text
详细解释当前购票占座的数据一致性是如何保证的
```

那么大致得到：

```python
UserRequest(
    original_query="详细解释当前购票占座的数据一致性是如何保证的"
)
```

这里有一个很重要的设计。

`UserRequest` **不分析语义**。

它的注释直接写的是：

```text
Validated request DTO;
it performs no semantic interpretation.
```

也就是说它只负责：

```text
接收问题
+
校验问题是否合法
```

而不会：

```text
❌ 判断用户在问事务
❌ 判断用户在问 Redis
❌ 拆分问题
❌ 生成检索 Query
```

---

# 2. `answer_options` 也不会影响检索需求

`UserRequest` 里还有：

```python
AnswerOptions
```

比如：

```python
depth_override="detailed"
```

表示：

> 用户希望回答详细一点。

但是源码明确规定：

> **回答多详细，不应该影响要搜索哪些项目事实。**

例如：

```text
“简单解释购票事务”

和

“详细解释购票事务”
```

它们需要找到的核心事实应该基本相同。

区别只是最终：

```text
怎么组织答案
讲多少
```

而不是：

```text
搜索不同的事实
```

所以这里刻意把：

```text
Answer presentation
```

和：

```text
Evidence retrieval
```

分开了。

这个设计是合理的。

---

# 3. `RetrievalController` 把原问题交给 `EvidencePlanner`

代码：

```python
plan = self.evidence_planner.plan(
    request.original_query
)
```

也就是：

```text
"详细解释当前购票占座的数据一致性是如何保证的"
```

直接进入：

```python
EvidencePlanner.plan()
```

代码位置：

```text
src/devcontext/planning/evidence_planner.py
```

---

# 4. EvidencePlanner 的目的不是“拆子问题”

这个地方是你现在架构里非常重要的一次变化。

以前常见的做法可能是：

```text
用户问题

↓ 拆成

Q1：Redis 怎么做？
Q2：数据库事务怎么做？
Q3：锁怎么做？
Q4：失败怎么办？
```

但当前 `EvidencePlanner` 的思想已经不是：

> 我要回答哪些章节？

而是：

> **我要确认哪些事实？**

所以它输出的单位叫：

```python
EvidenceRequirement
```

而不是：

```python
SubQuestion
```

源码里旧的 `QuestionPlan / SubQuestion` 甚至已经被标成：

```python
Deprecated compatibility model
```

也就是说现在正式路径已经转向：

```text
Question decomposition
        ↓
Evidence requirement planning
```

---

# 5. 什么是 `EvidenceRequirement`

结构非常简单：

```python
@dataclass
class EvidenceRequirement:

    id: str

    target: str

    success_criteria: str

    priority: str

    temporal_scope: str

    source_requirement: str
```

我们逐个理解。

---

## `target`

表示：

> **我要确认什么项目事实。**

例如，以下只是帮助理解的示意：

```text
确认购票过程中数据库状态更新的事务边界
```

注意它不是：

```text
“介绍数据库事务”
```

因为后者像回答章节。

而前者是在说：

> 我要找到一个可以被项目证据验证的事实。

---

# 6. `success_criteria`

这个非常重要。

它表示：

> **找到什么程度，才算这个 EvidenceRequirement 已经满足？**

例如示意：

```text
证据需要明确展示事务入口、
事务覆盖的数据库操作，
以及失败时是否回滚
```

于是以后 CoverageChecker 才能判断：

```text
现在找到的证据够不够？
```

没有 `success_criteria` 的话：

```text
找到一个出现 @Transactional 的 Chunk
```

系统就可能误以为：

```text
已经找齐了
```

实际上你真正需要的可能还有：

```text
事务包含哪些操作？
失败发生在哪里？
哪些东西不在事务里？
```

所以：

```text
target
```

回答：

> 我要找什么？

而：

```text
success_criteria
```

回答：

> 找到什么程度才算够？

---

# 7. `priority`

只有：

```text
CORE
SUPPORTING
```

### CORE

表示：

> 缺了它，不能完整回答原问题。

### SUPPORTING

表示：

> 有它更好，但缺少不一定阻止回答。

后面你会看到：

> **第二轮补查目前只针对 CORE。**

但那是后面的步骤，我们现在先不展开。

---

# 8. `temporal_scope`

可以是：

```text
CURRENT
HISTORY
FUTURE
ANY
```

它解决的是一个非常现实的问题。

比如项目 docs 里可能存在：

```text
旧设计方案
```

但代码已经改了。

用户问：

```text
“当前是怎么实现的？”
```

那么 EvidenceRequirement 应该标：

```text
CURRENT
```

防止以后：

```text
历史文档
```

把：

```text
当前代码
```

覆盖掉。

---

# 9. `source_requirement`

可以是：

```text
CODE
DOCUMENT
BOTH
ANY
```

这个字段非常重要。

例如：

### CODE

```text
必须从源码证明。
```

例如：

> “这个方法当前是否加了 `@Transactional`？”

显然应该查 CODE。

---

### DOCUMENT

```text
必须从文档证明。
```

比如：

> “项目最初为什么选择这种架构？”

代码可能证明不了“为什么”。

---

### BOTH

要求：

```text
代码证据
+
文档证据
```

例如：

> “设计文档声称这样实现，当前代码是否也是如此？”

---

### ANY

任意直接证据即可。

---

# 10. 谁生成这些 EvidenceRequirement？

目前是：

> **LLM。**

`EvidencePlanner` 会构造：

```python
LLMMessage("system", EVIDENCE_PLANNER_SYSTEM_PROMPT)

LLMMessage(
    "user",
    用户原问题
)
```

然后调用：

```python
client.generate(...)
```

所以这一阶段第一次使用 LLM。

---

# 11. Prompt 对 LLM 限制得非常严格

这是理解当前设计的关键。

System Prompt 明确要求：

```text
你的唯一任务：
列出必须掌握、能够由项目知识库验证的事实。
```

然后明确禁止它做这些事情：

```text
❌ 不生成 Search Query

❌ 不设计回答章节

❌ 不写最终答案

❌ 不决定回答篇幅

❌ 不生成 answer_goal

❌ 不生成 explanation_strategy
```

这一点非常关键。

也就是说：

```text
EvidencePlanner
```

负责：

```text
WHAT

我需要哪些事实？
```

而不是：

```text
HOW

这些事实怎么搜？
```

---

# 12. 这一步甚至禁止生成 Query

Prompt 里直接规定：

```text
禁止输出：

query
retrieval_query
answer_goal
answer_depth
sections
direct_answer
...
```

所以：

```text
EvidencePlanner
```

绝对不应该产生：

```text
"PurchaseTicket Transactional Redis"
```

这样的检索字符串。

这会留给**下一阶段 SearchActionPlanner**处理。

所以整个职责边界目前是：

```text
EvidencePlanner
      ↓
“我要知道什么？”

下一阶段
      ↓
“我要怎么搜？”
```

这两个现在是明确分开的。

---

# 13. LLM 输出严格 JSON

LLM 不能随便返回文字。

要求类似：

```json
{
  "schema_version": 2,
  "requirements": [
    {
      "target": "...",
      "success_criteria": "...",
      "priority": "CORE",
      "temporal_scope": "CURRENT",
      "source_requirement": "CODE"
    }
  ]
}
```

注意一个有意思的地方：

LLM **不能自己生成 `ER1`**。

Prompt甚至明确禁止输出：

```text
id
```

---

# 14. `ER1 / ER2` 是程序自己生成的

程序解析 JSON 时：

```python
for index, raw in enumerate(
    raw_requirements,
    start=1
):
```

然后：

```python
EvidenceRequirement(
    f"ER{index}",
    ...
)
```

所以：

```text
LLM：
只负责内容

程序：
负责稳定 ID
```

最终才形成：

```text
ER1
ER2
ER3
```

这样更加稳定。

---

# 15. 程序会严格校验 LLM 输出

不是 LLM 输出什么就接受什么。

例如要求：

### Requirement 数量

```text
1 ～ 6
```

---

### CORE 数量

```text
至少 1 个
最多 4 个
```

---

### priority

只能：

```text
CORE
SUPPORTING
```

---

### temporal_scope

只能：

```text
CURRENT
HISTORY
FUTURE
ANY
```

---

### source_requirement

只能：

```text
CODE
DOCUMENT
BOTH
ANY
```

---

还会检查：

```text
❌ target 不能为空
❌ target 不能重复
❌ 不能乱加字段
❌ 不能多行
❌ 长度不能超限
```

也就是说：

```text
LLM 输出
↓
结构化校验
↓
合法
↓
EvidencePlan
```

---

# 16. 还有一个“小型逻辑校验”

比如用户明确问：

```text
“当前……”
“目前……”
“现在……”
```

代码：

```python
_asks_for_current_state(query)
```

会检测：

```text
当前
目前
现在
现有
```

如果用户问的是当前状态：

```text
当前系统怎么实现？
```

但是 LLM 输出所有 requirement 都是：

```text
FUTURE
```

程序会直接认为这个 Plan 非法：

```python
raise EvidencePlanError(...)
```

这说明现在不是纯粹相信 LLM，而是：

```text
LLM reasoning
+
programmatic validation
```

---

# 17. 如果 LLM 出错怎么办？

不会直接让整个检索失败。

`EvidencePlanner.plan()`：

```python
try:
    ...
    return self._parse(...)
except Exception:
    return fallback_evidence_plan(query)
```

所以如果：

```text
LLM 超时
JSON 错误
字段错了
Requirement 不合法
```

会进入：

```python
fallback_evidence_plan()
```

---

# 18. fallback 非常保守

Fallback 只生成一个 Requirement：

```text
ER1

target:
找到能够直接支持原问题的当前项目证据

success_criteria:
至少获得一条与原问题直接相关、
能够用于回答的项目证据

priority:
CORE

temporal_scope:
CURRENT

source_requirement:
ANY
```

同时：

```python
decision_source="fallback"
```

所以系统还能继续运行，只是规划能力变弱。

---

# 19. 最终这一阶段输出什么？

就是：

```python
EvidencePlan
```

结构：

```python
@dataclass
class EvidencePlan:

    original_query: str

    requirements:
        tuple[EvidenceRequirement, ...]

    decision_source:
        "llm" / "fallback"

    schema_version:
        2
```

所以此时 State 从：

```text
用户只给了一个自然语言问题
```

变成：

```text
原问题

ER1：需要确认事实 A
ER2：需要确认事实 B
ER3：需要确认事实 C
...
```

---

# 20. 到这里为止，系统还没有“检索”

这是这一阶段最重要的理解。

目前只是：

```text
用户：
“详细解释当前购票占座的数据一致性”
          ↓
      UserRequest
          ↓
    EvidencePlanner
          ↓
   EvidenceRequirement
          ↓
      EvidencePlan
```

还没有：

```text
❌ keyword_search

❌ embedding

❌ vector search

❌ hybrid

❌ PostgreSQL 查询

❌ Top5

❌ ContextBuilder
```

---

# 21. 这一阶段用一句话总结

你可以把 `EvidencePlanner` 理解成：

> **检索开始之前先列“调查清单”。**

类似你去分析一个陌生 Java 项目。

用户问：

> “为什么这个项目购票不会超卖？”

你不会第一反应随便搜 `"超卖"`。

而是先想：

```text
为了证明这个结论，我至少需要确认哪些事实？
```

这就是 EvidencePlanner 当前的作用。

---

## 当前只记住这个职责分界

```text
UserRequest
       ↓
EvidencePlanner
       ↓
EvidencePlan

解决：
“要找什么事实？”

不解决：
“具体怎么找？”
```

**下一步才是：`EvidencePlan → SearchActionPlanner → 第一轮 SearchAction`，也就是这些 EvidenceRequirement 怎么被真正转化为检索 Query。**

你确认这一步理解后，我再只讲第 2 步。