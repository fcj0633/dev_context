当前 Tool Agent 的执行流，最好不要理解成：

> “LLM 想一个工具 → 调函数 → 拿结果”。

实际代码已经形成了一套比较完整的 **受约束取证循环**：

```text
EvidencePlanner
      ↓
EvidencePlan
      ↓
AgentRuntime
      ↓
Observation
      ↓
AgentPlanner
      ↓
ToolCall
      ↓
ToolExecutor
      ↓
RepositoryTools
      ↓
ToolResult
   ↙         ↘
Evidence    Observation
   ↓           ↓
Workspace     Memory
   \           /
    \         /
     Coverage
        ↓
  Progress / Stop
        ↓
 下一轮 or EvidencePackage
```

它真正的设计思想是：

> **让 LLM 负责“选择下一步取证动作”，但不让 LLM 控制底层检索算法、图查询参数、数据库、预算和停止状态。**

下面按照真实执行顺序解释。

---

# 一、Tool Agent 在整个项目中的位置

先把 Tool Agent 放回 DevContext 总体架构。

原来的 Fixed Retrieval 大概是：

```text
用户问题
   ↓
EvidencePlanner
   ↓
EvidencePlan
   ↓
RetrievalController
   ↓
第一轮 Search
   ↓
Coverage
   ↓
必要时第二轮 Search
   ↓
EvidencePackage
   ↓
Fast / Full
```

Tool Agent 替换的只是中间这一段：

```text
“怎么取得 Evidence”
```

改成：

```text
EvidencePlan
    ↓
Tool Agent 循环调查
    ↓
EvidencePackage
```

所以：

```text
EvidencePlanner
Fast
Full
EvidencePackage
Citation
```

这些核心契约没有被推翻。

对应入口是：

[`ToolDrivenRetrievalController`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/agentic/tool_driven_retrieval_controller.py)

它本身非常薄：

```text
EvidencePlanner
      ↓
创建 Workspace / Pool
      ↓
runtime.run(...)
      ↓
build_context(...)
      ↓
package_state(...)
      ↓
EvidencePackage
```

这意味着：

> Tool Agent 被设计成一种 **RetrievalEngine 实现**，而不是重新做一个问答系统。

这是整个模块最重要的架构定位。

---

# 二、第一阶段：EvidencePlanner 先告诉 Agent“需要证明什么”

Tool Agent 并不是拿到用户问题后直接乱搜。

最前面仍然经过：

```text
EvidencePlanner
```

产生：

```text
EvidencePlan
```

里面包含若干：

```text
EvidenceRequirement
```

例如用户问：

> OrderService.createTicketOrder 是怎么实现的，同时文档如何描述？

Planner 可能拆成：

```text
ER1
source = CODE
target = createTicketOrder 的实际实现
success_criteria = 找到接口和实现流程

ER2
source = DOCUMENT
target = 设计文档中的说明
success_criteria = 找到设计依据
```

因此 Agent 的责任不是：

> “理解整道问题并回答”。

而只是：

> **围绕已经定义好的 EvidenceRequirement 找证据。**

这就是当前职责划分：

```text
EvidencePlanner
= WHAT needs proving

AgentPlanner
= HOW to retrieve it

CoverageChecker
= IS it enough

Fast / Full
= HOW to explain it
```

这四个职责分开是当前架构比较成熟的一点。

---

# 三、第二阶段：Runtime 初始化一份“调查状态”

核心循环位于：

[`tool_agent/runtime.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/runtime.py)

进入：

```python
runtime.run(query, plan, workspace, pool)
```

后首先创建：

```text
AgentMemory
```

一开始所有 Requirement 都是：

```text
MISSING
```

例如：

```text
ER1 = MISSING
ER2 = MISSING
```

这里要理解：

> `AgentMemory` 并不是聊天 Memory，也不是用户长期记忆。

它是：

> **一次 Retrieval 请求内部的执行状态。**

当前保存：

```text
coverage
available_symbols
candidates
steps
tool_history
evidence_ids
errors
planner_failures
recovery_used
stop_reason
```

对应：

[`tool_agent/models.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/models.py)

---

# 四、为什么 Memory 不直接保存完整代码正文？

这是非常重要的设计。

工具搜索得到的可能是：

```text
500 行 Java
300 行 Markdown
```

如果每一步都把这些正文写进：

```text
AgentMemory
```

下一轮 Planner Prompt 会越来越大。

所以当前严格分离：

```text
EvidenceWorkspace
= 保存真正 Evidence 正文

AgentMemory
= 保存执行状态和元数据
```

工具执行后写入 Memory 的 `ToolResult` 会被处理成：

```text
evidence_results = ()
returned_ids = (...)
```

也就是说：

> Memory 只记“我找到过 Chunk 123、456”，正文仍由 Workspace 持有。

这是成熟 Agent 设计里很重要的：

> **Execution State ≠ Knowledge Store**

---

# 五、每一轮不是把整个 Memory 丢给 LLM，而是先构造 Observation

这是当前设计另一个比较好的地方。

真正给 Planner 的不是：

```text
AgentMemory 全量 dump
```

而是：

```text
AgentObservationView
```

对应：

[`tool_agent/observation.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/observation.py)

它大概会把当前状态压缩成：

```text
用户 Query

Requirements:
  ER1
  target
  success_criteria
  当前 Coverage
  missing_criteria
  known_symbols

Candidates

最近三步执行摘要

可用 Tool

remaining_steps
remaining_calls
```

例如：

```text
ER1:
  target:
    找到 createTicketOrder 实现

  state:
    PARTIAL

  missing:
    尚缺具体实现代码

  known_symbols:
    M:OrderService#createTicketOrder(...)
```

Planner 实际看到的是这种：

> **决策视图**

而不是整个系统内部对象。

---

# 六、为什么 Observation 需要控制大小？

当前还有：

```text
8000 estimated tokens
```

Observation Budget。

超过以后，不是随便截断，而是按优先级删除：

```text
先删多余 Symbol handle
↓
再删 Candidate
↓
再删旧 Step
↓
Evidence Goal 最后才动
```

这反映一个很好的 Context Engineering 原则：

> **对 Agent 来说，“要完成什么任务”比“过去所有执行细节”更重要。**

所以 Requirement 和 Tool Contract 优先保留。

---

# 七、这里还有一个非常重要的概念：Observation 同时决定“能力边界”

`AgentObservationView` 除了：

```text
payload
```

还有：

```text
confirmed_keys
```

也就是说：

> Planner 当前看到哪些 CONFIRMED Symbol，Runtime 就只允许 Graph Tool 使用哪些 Symbol。

这个设计非常关键。

假设 Memory 中历史上曾经出现过：

```text
M:A#foo()
M:B#bar()
M:C#baz()
```

但由于 Observation Budget，这一轮只暴露：

```text
M:A#foo()
M:B#bar()
```

那么：

```text
confirmed_keys
```

也只有 A、B。

Graph Tool 不能偷偷使用 C。

所以：

```text
LLM Context
```

和：

```text
LLM Capability
```

是一致的。

不是：

> 模型看不到，但其实还能调用。

---

# 八、第三阶段：Planner 只负责产生 ToolCall

然后进入：

[`tool_agent/planner.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/planner.py)

Planner Prompt 对职责限制得很严格：

```text
只选择工具
不回答问题
不修改 Requirement
不判断 READY
```

输出必须是：

```json
{
  "actions": [
    {
      "requirement_id": "ER1",
      "tool": "find_implementations",
      "arguments": {
        "symbol_key": "M:..."
      },
      "reason": "补充接口实现证据"
    }
  ],
  "cannot_progress": false
}
```

因此 LLM 的输出不是：

```text
最终答案
```

而是：

```text
AgentDecision
```

---

# 九、为什么 Planner 不负责判断 READY？

这是特别值得理解的一点。

如果允许 Planner：

```text
“我感觉证据够了，READY。”
```

就会形成：

```text
同一个模型
既负责找证据
又负责宣布自己已经找够
```

容易自我确认。

当前刻意分开：

```text
Planner
→ 选择 Tool

CoverageChecker
→ 判断 Evidence 是否充分
```

所以 Runtime 最终：

```text
READY
```

不是 Planner 返回的。

这是一个典型的：

> **Decision / Verification Separation**

---

# 十、Planner 每一步最多输出三个 ToolCall

当前固定：

```text
MAX_STEPS = 3
MAX_TOOL_CALLS = 8
MAX_CALLS_PER_STEP = 3
```

因此每轮：

```text
1 ~ 3 Tool Calls
```

整个请求：

```text
最多 3 Step
最多 8 Calls
```

这是设计上的：

> **Bounded Agent**

而不是：

```text
while not done:
    ask LLM
```

无限循环。

为什么这样？

因为 Repository Retrieval 是线上请求的一部分，需要：

```text
可预测延迟
可预测 Token
可测试行为
明确停止
```

所以 Agent 自由度是有限的。

---

# 十一、同一个 Step 内的 ToolCall 必须相互独立

这是当前一个非常关键的执行模型。

假设 Planner 一步返回：

```text
Call 1:
find_symbol("OrderService")

Call 2:
find_implementations(
    M:OrderService#createOrder(...)
)
```

Call 2 依赖 Call 1 的新结果。

当前不允许。

因为这一轮所有 ToolCall 都必须基于：

```text
Step 开始时 Observation
```

已有的信息。

所以正确执行是：

```text
Step 1

find_symbol
    ↓
确认 Symbol


Step 2

find_implementations
```

为什么这么设计？

如果允许同一步依赖：

```text
Call2 = f(Call1 result)
```

Runtime 就必须支持：

```text
DAG
动态参数绑定
中途 replanning
partial observation refresh
```

复杂度会大幅增加。

当前选择：

> **Step 内可并列，Step 间有依赖。**

很像：

```text
Plan
→ batch act
→ observe
→ replan
```

---

# 十二、Planner 失败以后不是立即结束

Planner 不是完全依赖 LLM。

如果第一次出现：

```text
JSON 错误
Contract 错误
Timeout
可恢复的模型调用错误
```

系统会：

```text
fallback_decision(...)
```

也就是：

```text
LLM Planner
      ↓ 失败
Deterministic Planner
```

而不是：

```text
LLM失败
→ 整个检索失败
```

但如果 Planner 连续失败：

```text
planner_failures > 1
```

则：

```text
PLANNER_FAILED
```

停止。

---

# 十三、当前 fallback 是有能力边界的

这里可以深入一点当前实现。

Fallback 会：

```text
优先 CORE Requirement
↓
跳过 SATISFIED
↓
根据缺口生成 query
↓
search_code / search_docs
```

它目前只对一个比较窄的 Graph 场景有确定逻辑：

```text
已知一个 METHOD
+
missing 中出现：
“下游” / “callee”

→ find_callees
```

也就是说当前 fallback 对：

```text
caller
implementation
hierarchy
```

支持还不完整。

这是为什么前面说：

> 当前 Tool Agent 的基础执行框架比决策层成熟。

---

# 十四、第四阶段：ToolCall 不是直接执行，先进入 ToolExecutor

这是整个工具模块最重要的“安全边界”。

对应：

[`tool_agent/executor.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/executor.py)

流程：

```text
ToolCall
   ↓
Executor Validate
   ↓
合法？
 ┌─┴──────┐
No        Yes
↓          ↓
INVALID   Handler
```

为什么不能：

```python
registry.handlers[tool_name](...)
```

直接调？

因为 Planner 是 LLM。

必须把 LLM 输出当成：

> **不可信输入。**

---

# 十五、Executor 第一层：工具是否存在

Planner 如果生成：

```text
read_file
run_sql
grep_repo
```

但 Registry 没有：

```text
INVALID_ARGUMENT
```

不会动态反射任意函数。

---

# 十六、第二层：Requirement 是否合法

每个 ToolCall 必须绑定：

```text
requirement_id
```

例如：

```text
ER1
```

Executor 会检查：

```text
ER1 是否真在 EvidencePlan 中。
```

模型不能创建：

```text
ER999
```

然后偷偷搜别的任务。

---

# 十七、第三层：Source Permission

Tool Registry 对每个工具定义：

```text
allowed_sources
```

例如：

```text
search_docs

DOCUMENT / BOTH / ANY
```

Graph Tool：

```text
CODE / BOTH / ANY
```

因此一个 Requirement 如果是：

```text
DOCUMENT
```

Planner 却输出：

```text
find_callees
```

会被拒绝。

这意味着：

> EvidenceRequirement 不只是 Planner Prompt，它还是执行权限的一部分。

---

# 十八、第四层：Tool Arguments 必须严格匹配 Schema

当前 Tool 都只有一个业务参数。

例如：

```text
search_code
→ query
```

```text
find_symbol
→ name
```

```text
find_callers
→ symbol_key
```

Planner 不能传：

```json
{
  "symbol_key": "...",
  "hops": 10,
  "limit": 100,
  "sql": "..."
}
```

因为 Registry Schema：

```text
additionalProperties = false
```

Executor 还会再次检查：

```text
set(arguments) == {required_argument}
```

所以模型不能控制：

```text
top_k
strategy
timeout
hops
SQL
```

这些始终归系统所有。

---

# 十九、第五层，也是最重要的一层：Graph Tool 只能使用 CONFIRMED Symbol

如果 Tool 参数是：

```text
symbol_key
```

Executor 要检查：

```python
value in confirmed_keys
```

否则：

```text
INVALID_ARGUMENT
```

这意味着 LLM 即使知道 Symbol Key 格式，也不能自己编：

```text
M:xxx.OrderService#foo(...)
```

必须先经过：

```text
find_symbol
```

或者：

```text
search_code
→ symbols_for_chunks
```

真正发现。

所以：

```text
SymbolObservation(CONFIRMED)
```

实际上是一种：

> **Graph Capability Handle**

只有拿到这个 Handle，才能继续查图。

---

# 二十、CANDIDATE 为什么不能使用？

例如：

```text
find_symbol("BaseDO")
```

返回：

```text
order.BaseDO
pay.BaseDO
user.BaseDO
```

这时候它们都是：

```text
CANDIDATE
```

不是：

```text
CONFIRMED
```

因此不能：

```text
随便挑一个
→ find_hierarchy
```

必须：

```text
进一步 Search
或更精确 find_symbol
```

确认身份。

这是为了避免最危险的一类 Agent 错误：

> **起始 Anchor 选错，但后面的 Graph 调用全部“正确”。**

---

# 二十一、第五阶段：真正的 Tool Handler 才开始工作

Executor 验证通过后：

```text
ToolRegistry
```

找到：

```text
RepositoryTools
```

对应方法。

真实七个 Tool：

```text
search_code
search_docs
find_symbol
find_callers
find_callees
find_implementations
find_hierarchy
```

对应：

[`tool_agent/tools.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/tool_agent/tools.py)

注意：

> RepositoryTools 不是 Agent Runtime。

它只负责：

```text
给定合法 ToolCall
→ 把能力执行出来
→ 返回 ToolResult
```

---

# 二十二、`search_code` 的设计最能体现“Agent 控制意图，系统控制算法”

Planner 只生成：

```text
search_code(query)
```

它不能指定：

```text
keyword
vector
hybrid
```

真正的策略继续走已有：

```text
code_strategy_for(...)
```

因此：

```text
Agent
决定：
“我要搜代码，Query 是什么”

Retrieval Policy
决定：
“keyword 还是 vector”
```

这是非常合理的职责边界。

否则 Agent 既要理解：

```text
业务问题
```

又要理解：

```text
BM25 / vector / stage-aware policy
```

认知负担太重。

---

# 二十三、Search Tool 还有第二个作用：把 Chunk 转成 Symbol

例如：

```text
search_code("OrderService createTicketOrder")
```

得到：

```text
SearchResult Chunk
```

然后如果 Graph Store 可用：

```text
symbols_for_chunks()
```

得到：

```text
CONFIRMED Symbol
```

因此一次 `search_code` 的输出其实是两条轨道：

```text
CODE Chunk
    ↓
Evidence

Symbol
    ↓
Agent Capability
```

这非常重要。

因为下一轮就可以：

```text
find_implementations
find_callers
find_callees
```

所以：

> `search_code` 是普通 RAG 世界进入 Graph 世界的桥。

---

# 二十四、`find_symbol` 的职责不是搜索代码，而是获取可靠 Anchor

例如：

```text
find_symbol(
  "OrderService.createTicketOrder"
)
```

如果唯一匹配：

```text
SUCCESS
```

返回：

```text
对应 Chunk
+
CONFIRMED Symbol
```

如果多个：

```text
AMBIGUOUS
```

返回：

```text
Candidate Symbol[]
```

但：

```text
evidence_results = empty
```

也不会加入：

```text
available_symbols
```

这就是一个很明确的设计：

> **Ambiguity 是信息，不是 Evidence。**

---

# 二十五、Graph Tool 本质上是底层关系查询的语义封装

例如：

```text
find_callers(symbol)
```

内部：

```text
incoming CALLS
```

`find_callees`：

```text
outgoing CALLS + CONSTRUCTS
```

`find_implementations`：

如果 Symbol 是 METHOD：

```text
incoming OVERRIDES
```

如果是 Type：

```text
incoming IMPLEMENTS
```

`find_hierarchy`：

```text
EXTENDS + IMPLEMENTS
incoming + outgoing
最多两跳
```

LLM 不需要知道：

```text
edge direction
edge type组合
hop
neighbor limit
```

它只表达：

> “我要找调用者/被调用者/实现/层级”。

这就是 Tool Abstraction 的价值。

---

# 二十六、Tool Handler 最终统一返回 ToolResult

这是 Runtime 可以通用处理七种 Tool 的基础。

`ToolResult` 大概是：

```text
call

status

evidence_results

observation

error

latency

new_evidence_count
new_symbol_count
new_ownership_count
```

这里最重要的是：

```text
evidence_results
```

和：

```text
observation
```

必须分开理解。

---

# 二十七、Evidence 和 Observation 是两条不同的信息流

例如：

```text
find_callers(B)
```

查到：

```text
A --CALLS--> B
```

ToolResult 中：

### Evidence

```text
A 对应的真实 CODE Chunk
```

用于：

```text
EvidenceWorkspace
Coverage
最终答案
Citation
```

### Observation

```text
A --CALLS--> B

A 是 CONFIRMED Symbol
```

用于：

```text
AgentMemory
下一轮 Planner
```

所以：

```text
Evidence
= 给回答系统看的内容

Observation
= 给 Agent 控制系统看的状态
```

这条边界设计得很好。

---

# 二十八、第六阶段：Runtime 接到 ToolResult 后，首先做 SourcePolicy

这时候还不能直接：

```text
workspace.add(result)
```

而是：

```text
result.evidence_results
↓
SourcePolicy.classify(...)
```

也就是把 SearchResult 与：

```text
requirement_id
source provenance
```

绑定。

然后：

```text
EvidencePool.add_many()
EvidenceWorkspace.ingest()
```

这里体现：

> Tool Agent 并没有绕开原来的 Evidence Infrastructure。

它只是换了“Evidence 从哪里来”。

---

# 二十九、为什么同时需要 EvidencePool 和 EvidenceWorkspace？

可以粗略理解为：

```text
EvidencePool
= 所有候选 Evidence 池
```

用于后续：

```text
ContextBuilder
```

选择最终 Context。

而：

```text
EvidenceWorkspace
= 当前 Retrieval 已经掌握了哪些真实 Evidence
```

用于：

```text
Coverage
Agent Progress
Debug
```

因此：

```text
Tool Result
↓
Pool
+
Workspace
```

两个都更新。

---

# 三十、`new_ownership_count` 是什么？

这是一个挺值得理解的细节。

假设 Chunk 123 已经存在于 Workspace：

```text
ER1 → Chunk123
```

现在另外一个 Requirement：

```text
ER2
```

也发现 Chunk123 对自己有价值。

虽然：

```text
没有新增 Chunk
```

但新增了：

```text
ER2 → Chunk123
```

这个 Evidence Ownership。

所以 Runtime 的 Progress 不只看：

```text
有没有新 Chunk
```

还看：

```text
有没有新的 requirement-chunk ownership
```

否则这种：

```text
同一证据支持另一个 Requirement
```

会被错误判断成“无进展”。

---

# 三十一、然后才写入 AgentMemory

执行：

```python
memory.observe(result)
```

会：

```text
CONFIRMED Symbol
→ available_symbols

CANDIDATE
→ candidates

Evidence IDs
→ evidence_ids

Tool metadata
→ tool_history

Error
→ errors
```

但再次强调：

> 正文不会写入 Memory。

---

# 三十二、第七阶段：整批 ToolCall 完成后才做 Coverage

这是一处很重要的修复设计。

假设同一 Step 有三个 Tool：

```text
search_code
search_docs
find_symbol
```

不能：

```text
Tool1 没找到
→ 立刻 NO_PROGRESS
```

因为 Tool2/Tool3 可能有收获。

所以当前：

```text
执行完整个 ActionStep
↓
把 Evidence 全部写入 Workspace
↓
然后只做一次 Coverage
```

也就是：

```text
batch act
→ observe
→ coverage
```

而不是：

```text
act1
→ coverage
→ act2
→ coverage
```

这减少：

```text
LLM Coverage 调用次数
```

也让“一轮”的语义更完整。

---

# 三十三、Coverage 完全独立于 Tool Result

CoverageChecker 不看：

```text
某 Tool Status=SUCCESS
```

然后说：

```text
够了
```

它只看：

```text
EvidenceRequirement
+
Workspace 中属于该 Requirement 的 Evidence
```

这意味着：

```text
Tool成功
≠
Requirement satisfied
```

这是正确的。

例如：

```text
find_symbol SUCCESS
```

可能只找到接口声明，

但 Requirement 是：

```text
完整实现流程
```

Coverage 仍应该：

```text
PARTIAL
```

---

# 三十四、但这也正是当前系统最大的质量问题

理论设计是正确的：

```text
Coverage 独立判断。
```

但当前 `CoverageChecker` 主要读取：

```text
Chunk 内容
```

并不知道：

```text
Graph Relation 是否真正被证明。
```

所以可能出现：

```text
接口 Chunk
+
实现类 Chunk
```

LLM 就判：

```text
SATISFIED
```

尽管没有真正拿到：

```text
OVERRIDES
```

Relation。

这也是当前：

```text
Tool Agent False READY = 11
```

比 Auto Graph：

```text
3
```

更差的重要原因。

也就是说：

> 执行框架本身已经比较稳，但“Evidence 足够”的语义还不够严。

---

# 三十五、第八阶段：Runtime 判断“这一轮有没有进展”

Coverage 完成后，Runtime 不只是看：

```text
state 有没有变化。
```

而是四类 Progress：

```text
新增 Evidence

OR

新增 Confirmed Symbol

OR

新增 Requirement-Chunk Ownership

OR

Coverage 改善
```

状态等级：

```text
MISSING / UNVERIFIED = 0

PARTIAL = 1

SATISFIED = 2
```

例如：

```text
MISSING
→ PARTIAL
```

算进展。

```text
PARTIAL
→ SATISFIED
```

算进展。

---

# 三十六、为什么新 Symbol 也算 Progress？

假设：

```text
find_symbol
```

只确认了：

```text
OrderService#createOrder
```

但暂时没有补充最终 Evidence。

如果只按 Evidence 判断：

```text
0 new chunks
→ NO_PROGRESS
```

Agent 就会停止。

但实际上它已经获得：

> 下一轮可以调用 Graph Tool 的 Anchor。

所以：

```text
new Confirmed Symbol
```

必须算 Progress。

这说明当前 Progress 定义是围绕：

> **“下一步的检索能力有没有增加”**

而不是只看最终回答材料。

---

# 三十七、每一步最后会形成一个 ActionStep

这相当于：

> 一轮 Agent 的审计记录。

里面包括：

```text
coverage_before

planned_calls

results

new_evidence

new_symbols

new_ownerships

coverage_after

progress

latency
```

所以 Debug 时可以清楚回答：

```text
Step 2 为什么继续？
```

或者：

```text
为什么 Step 1 后 READY？
```

这比单纯保存 ToolCall Log 更强。

---

# 三十八、第九阶段：Stop Policy

Coverage 和 Progress 完成后，Runtime 按顺序检查停止。

最重要的是：

```text
所有 CORE Requirement SATISFIED
→ READY
```

然后：

```text
达到 8 Calls
→ MAX_TOOL_CALLS
```

如果：

```text
没有 Progress
```

看是否存在：

```text
retryable error
```

如果有，并且 Recovery 还没用：

```text
允许一次恢复
```

否则：

```text
ALL_TOOLS_FAILED
```

或者：

```text
NO_PROGRESS
```

最终最多：

```text
3 Step
```

到达：

```text
MAX_STEPS
```

---

# 三十九、为什么 Recovery 只有一次？

这是为了避免：

```text
DB timeout
→ retry
→ timeout
→ retry
→ timeout
→ retry...
```

Tool Agent 的任务不是：

> 无限重试基础设施故障。

而是：

> 在有限预算中换一种可用取证路径。

例如：

```text
Graph temporary unavailable
```

下一轮可以尝试：

```text
search_code
```

但全请求只有一次这种：

```text
tool-switch recovery
```

机会。

---

# 四十、错误被分成两类

当前设计有明显区分。

### 可恢复业务/基础设施错误

例如：

```text
DB timeout
connection failure
embedding temporary failure
graph unavailable
```

转成：

```text
ToolError(
    retryable=True
)
```

Agent 可以换路。

### 程序契约错误

例如：

```text
AnalysisContractError
非法程序状态
配置/认证致命错误
```

不会伪装成：

```text
EMPTY
```

而是直接抛出。

也就是：

> **环境不稳定可以恢复，程序逻辑坏了必须暴露。**

---

# 四十一、第十阶段：Runtime 结束后重新回到 RetrievalController 世界

Agent 最终返回：

```text
RuntimeOutcome
```

包含：

```text
Memory
CoverageRounds
SearchHistory
StageUsage
Trace
```

然后：

```text
ToolDrivenRetrievalController
```

继续：

```text
EvidencePool
↓
RetrievalController._build_context()
↓
ContextBundle
```

也就是说最终 Context Packing：

> 仍然复用 Fixed Retrieval 的机制。

Agent 没有自己的：

```text
ContextBuilder
```

这避免了：

```text
Fixed Context 一套规则
Agent Context 又一套规则
```

---

# 四十二、最终还是生成标准 EvidencePackage

最终：

```text
EvidencePackage
```

包括：

```text
plan
bundle
coverage
missing_requirements
state
search_history
coverage_rounds
workspace_snapshot
workspace
```

所以 Fast / Full 完全不需要知道：

```text
这些 Evidence 是 search_code 找的
还是 find_callees 找的
```

只消费：

```text
EvidencePackage。
```

这是很好的：

> **Execution Strategy Encapsulation**

---

# 四十三、把完整工具执行流串成一张图

这是我建议你真正记住的版本：

```text
User Query
   │
   ▼
EvidencePlanner
“我要证明什么？”
   │
   ▼
EvidencePlan
   │
   ▼
┌───────────────────────────────┐
│ AgentRuntime                  │
│                               │
│  ① Build Observation          │
│        │                      │
│        ▼                      │
│  ② AgentPlanner              │
│     “下一步找什么？”           │
│        │                      │
│        ▼                      │
│     ToolCall[]                │
│        │                      │
│        ▼                      │
│  ③ ToolExecutor              │
│     权限 / 参数 / Symbol校验    │
│        │                      │
│        ▼                      │
│  ④ RepositoryTools           │
│        │                      │
│    ┌───┼────────────┐         │
│    ▼   ▼            ▼         │
│ Search Symbol      Graph      │
│    └───┼────────────┘         │
│        ▼                      │
│     ToolResult                │
│      /     \                  │
│     ▼       ▼                 │
│ Evidence  Observation         │
│     │       │                 │
│     ▼       ▼                 │
│ Workspace  Memory             │
│      \      /                 │
│       ▼    ▼                  │
│  ⑤ CoverageChecker            │
│        │                      │
│        ▼                      │
│  ⑥ Progress Check             │
│        │                      │
│        ▼                      │
│  ⑦ Stop / Next Step           │
│                               │
└──────────────┬────────────────┘
               │
               ▼
          ContextBuilder
               │
               ▼
         EvidencePackage
               │
         ┌─────┴─────┐
         ▼           ▼
       Fast         Full
```

---

# 四十四、这个执行流最核心的设计思想

如果从“成熟 Agent 架构”角度总结，我认为当前最值得学习的是五点。

第一：

> **LLM 只拥有决策权，没有基础设施控制权。**

它选择：

```text
search_code
find_callees
```

但不能控制：

```text
SQL
top_k
hop
timeout
Graph Direction
```

第二：

> **ToolCall 是不可信输入，ToolExecutor 是安全边界。**

Prompt 只是软约束，Executor 才是真正硬约束。

第三：

> **Evidence 和 Agent State 分离。**

```text
Workspace
= 正文

Memory
= 状态
```

避免 Prompt 无限膨胀。

第四：

> **Plan → Batch Act → Observe → Verify → Replan**

而不是简单：

```text
LLM → Tool → LLM → Tool
```

每一轮都有 Coverage 和 Progress。

第五：

> **最终仍然回到统一 EvidencePackage 契约。**

所以 Agent 是检索策略，而不是新的 Answer Pipeline。

---

# 四十五、结合现有测试，当前执行流哪里成熟，哪里还不成熟？

基础设施这一层其实已经不错：

```text
非法 Tool Call 率 ≈ 0.99%

平均 Calls = 2.10

P95 Step = 2

受控正确动作序列
CORE / Recall / Full-case = 100%
```

这说明：

```text
Tool Registry
Executor
Graph Capability
Memory
Runtime
Budget
Error Handling
```

这些底层已经基本工作。

真正的问题出现在：

```text
Observation
→ Planner Decision
→ Coverage
```

这一段。

真实 Agent：

```text
Graph Tool 使用太少
Allowed Tool Sequence 命中率 31.25%
False READY = 11
```

说明当前不是：

> “工具执行不好”。

而是：

> **Agent 没有稳定判断下一步该用什么工具，并且 Coverage 有时过早认为证据足够。**

所以从当前代码来看，整个 Tool Agent 可以分成：

```text
执行层
★★★★☆

决策层
★★★☆☆

证据充分性判断
★★★☆☆
```

这也是为什么现在继续增加 Tool 数量意义不大。

---

你现在如果继续学习这个模块，下一步最适合深入的是：

# **AgentMemory + Observation 为什么这样设计，以及一轮 Step 结束后下一轮 Planner 到底能看到什么**

因为这是理解：

> “为什么同样七个工具，受控测试可以 100%，真实模型却只选对 31.25% 工具序列”

的关键。