基于当前已经合并到 `main` 的实现，我认为 **Tool-driven Agent 现在已经具备比较合适的开发时机**。原因不是“项目需要加一个 Agent 名词”，而是底层工具终于足够丰富：

```text
之前：
Agent 能做的其实只有
search → search → search

现在：
普通检索
+ 文档检索
+ Symbol 精确定位
+ callers / callees
+ implementations
+ hierarchy
+ EvidenceWorkspace
+ Coverage
```

因此现在可以真正让模型根据**证据缺口选择不同能力**，而不是固定执行两轮搜索。

但我不建议推翻当前 `EvidencePlanner → RetrievalController → EvidencePackage → Fast/Full`。最合适的方案是：

> **保留 EvidencePlanner、EvidenceWorkspace、CoverageChecker 和 Fast/Full；用 Tool-driven Agent 替换当前 RetrievalController 内部固定的“SearchAction → Round0 → Coverage → Round1”执行策略。**

---

# 一、先明确当前系统为什么还不算 Tool Agent

当前主链路实际上是：

```text
User Question
      ↓
EvidencePlanner
      ↓
EvidenceRequirement
      ↓
SearchActionPlanner
      ↓
Round 0 Query
      ↓
RetrievalPolicy
      ↓
vector / keyword / hybrid
      ↓
可选自动 Symbol Graph Expansion
      ↓
EvidenceWorkspace
      ↓
CoverageChecker
      ↓
如果 CORE 不足
      ↓
SearchActionPlanner
      ↓
Round 1 Query
      ↓
Retrieval
      ↓
Coverage
      ↓
EvidencePackage
```

这里已经有：

- LLM Planner；
- 状态；
- 两轮反馈；
- Coverage；
- Graph。

但是模型实际上只决定：

> **“下一条搜索 Query 写什么？”**

它不能决定：

```text
我要先搜索代码
还是查文档？

现在已经找到 PayService，
下一步应该重新做语义检索，
还是直接查它的 callees？

这是接口，
下一步是不是应该找 implementations？

我已经知道方法名了，
是不是不用再 embedding search，
直接 resolve symbol？
```

所以现在仍然更准确地称作：

> **Agentic Retrieval Workflow**

而不是：

> **Tool-driven Agent。**

---

# 二、Tool-driven Agent 要解决的核心问题

现在固定控制器面对一个问题：

> “支付回调最终是如何推进订单状态的？”

可能：

```text
Round 0
vector search
    ↓
PayServiceImpl#payCallback

Coverage:
缺完整调用过程

Round 1
重新生成 Query
    ↓
keyword search
```

但实际上有了 Symbol Graph 后，最合理的动作很可能是：

```text
找到 PayServiceImpl#payCallback
        ↓
find_callees()
        ↓
发现接口方法
        ↓
find_implementations()
        ↓
得到下一段真实代码
```

也就是说：

> **第二步已经不应该永远是“再搜索一次”。**

Tool Agent 的价值就在这里。

---

# 三、推荐的最终架构

我建议演进成：

```text
                       User Question
                             │
                             ▼
                      EvidencePlanner
                             │
                             ▼
                       EvidencePlan
                             │
                             ▼
                ┌─────────────────────────┐
                │ Tool-driven AgentRuntime│
                │                         │
                │ Agent Planner           │
                │      ↕                  │
                │ AgentState / Memory     │
                │      ↕                  │
                │ Tool Registry           │
                └────────────┬────────────┘
                             │
          ┌──────────────────┼────────────────────┐
          │                  │                    │
          ▼                  ▼                    ▼
     Search Tools        Symbol Tools         Graph Tools
          │                  │                    │
     search_code         find_symbol          find_callers
     search_docs                              find_callees
                                              find_impls
                                              find_hierarchy
          │                  │                    │
          └──────────────────┼────────────────────┘
                             ▼
                      ToolResult
                             │
                             ▼
                    EvidenceWorkspace
                             │
                             ▼
                     CoverageChecker
                             │
             ┌───────────────┴──────────────┐
             │                              │
           READY                       GAP remains
             │                              │
             ▼                              └── Agent next step
      EvidencePackage
             │
        ┌────┴────┐
        ▼         ▼
       Fast      Full
```

最大的设计原则：

> **Agent 只负责“怎样取得证据”，不负责最终回答。**

---

# 四、哪些当前模块不要改

这是 V1 最重要的边界。

## 继续保留

```text
EvidencePlanner
EvidenceRequirement
EvidenceWorkspace
EvidenceCatalog
CoverageChecker
SourcePolicy
EvidencePackage
Fast
Full
Citation
```

也就是说：

```text
EvidencePlanner
```

仍然回答：

> 我要证明哪些事实？

Tool Agent 回答：

> 为了证明这些事实，现在应该使用哪个工具？

CoverageChecker 回答：

> 当前事实是否已经得到足够证据？

Fast / Full 回答：

> 如何把这些事实解释给用户？

四个职责完全分开：

```text
EvidencePlanner
     │
     │ WHAT
     ▼
EvidenceRequirement

Agent
     │
     │ HOW TO FIND
     ▼
Tools

Coverage
     │
     │ ENOUGH?
     ▼
EvidencePackage

Fast / Full
     │
     │ HOW TO EXPLAIN
     ▼
Answer
```

这是非常清晰的架构。

---

# 五、不要直接修改现有 RetrievalController

我推荐：

```text
保留：
RetrievalController
```

作为：

> Fixed / Baseline Retrieval Engine

新增：

```text
ToolDrivenRetrievalController
```

两者实现相同契约：

```python
retrieve(
    request: UserRequest,
    top_k: int,
) -> RetrievalOutcome
```

于是：

```text
                 RetrievalEngine
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
RetrievalController     ToolDrivenRetrievalController
   fixed 2 rounds             agent loop
```

外层：

```text
EvidenceDrivenWorkflow
```

完全不需要知道区别。

最后都是：

```text
RetrievalOutcome
    ↓
EvidencePackage
```

---

# 六、为什么不应该直接在现有 RetrievalController 里疯狂加 if

如果直接变成：

```python
if tool_agent:
    ...
elif graph:
    ...
elif second_round:
    ...
```

当前 `retrieval_controller.py` 已经负责：

```text
EvidencePlan
SearchAction
stage-aware retrieval
Graph integration
Coverage
Round1
Deadline
ContextBuilder
Package
```

继续塞 Agent 后会迅速变成：

```text
God Controller
```

所以这一次非常适合真正拆出：

```text
agent_runtime/
```

---

# 七、模块结构建议

新增：

```text
src/devcontext/tool_agent/
│
├── models.py
├── state.py
├── planner.py
├── runtime.py
├── registry.py
├── executor.py
├── memory.py
├── observation.py
│
├── tools/
│   ├── base.py
│   ├── search_code.py
│   ├── search_docs.py
│   ├── find_symbol.py
│   ├── find_callers.py
│   ├── find_callees.py
│   ├── find_implementations.py
│   └── find_hierarchy.py
│
└── trace.py
```

以及：

```text
src/devcontext/agentic/
└── tool_driven_retrieval_controller.py
```

我不建议把它放进：

```text
code_graph/
```

因为 Agent 的 Tool 不只是 Graph。

---

# 八、V1 最合适的 7 个工具

现在千万不要一下做二三十个 Tool。

第一版我建议就这 7 个。

---

## Tool 1：`search_code`

```text
search_code(query)
```

作用：

> 根据业务语义搜索代码。

底层继续复用：

```text
RetrievalPolicy
+
code_strategy_for()
```

模型**不能自己选择**：

```text
vector
keyword
hybrid
```

这是一个重要设计。

不要暴露：

```text
vector_search()
keyword_search()
hybrid_search()
```

三个工具。

模型只表达：

> 我要搜代码。

系统根据已有事实自动选择：

```text
没有 Symbol
→ vector

已有 Symbol
→ keyword
```

继续复用现在已经实验过的 stage-aware 策略。

---

# 九、Tool 2：`search_docs`

```text
search_docs(query)
```

作用：

```text
设计文档
验证报告
历史说明
架构说明
```

底层继续使用当前：

```text
DOCUMENT vector retrieval
```

并经过：

```text
SourcePolicy
```

所以 Agent 不需要自己判断：

```text
当前文档
历史计划
验证报告
```

---

# 十、Tool 3：`find_symbol`

```text
find_symbol(name)
```

例如：

```text
find_symbol("notifyPayResult")
```

返回：

```text
symbol_key
kind
file
lines
chunk_id
```

如果：

```text
只有一个可靠结果
```

则可以把对应 CODE Chunk 作为 Evidence。

如果存在：

```text
多个同名方法
```

返回：

```text
AMBIGUOUS
```

以及候选：

```text
PayService#notifyPayResult(...)
OrderService#notifyPayResult(...)
```

不能静默选第一个。

---

# 十一、Tool 4：`find_callers`

```text
find_callers(symbol_key)
```

底层：

```text
incoming CALLS
```

例如：

```text
谁调用：
M:PayService#notifyPayResult(...)
```

返回：

```text
caller symbols
+
relation
+
CODE chunks
```

---

# 十二、Tool 5：`find_callees`

```text
find_callees(symbol_key)
```

底层：

```text
outgoing CALLS
```

建议同时把：

```text
CONSTRUCTS
```

作为单独字段返回：

```text
calls:
[...]

constructs:
[...]
```

而不是再额外做一个工具。

这样 Tool 数量保持较小。

---

# 十三、Tool 6：`find_implementations`

类型：

```text
Interface
      ↓ incoming IMPLEMENTS
Class
```

方法：

```text
Interface method
      ↓ incoming OVERRIDES
Implementation method
```

例如：

```text
find_implementations(
  "M:PayService#pay(...)"
)
```

这会成为 Java Spring 项目非常重要的工具。

---

# 十四、Tool 7：`find_hierarchy`

用于：

```text
父类
子类
接口
继承链
```

最多：

```text
2 hops
```

继续复用 Symbol Graph 已有约束。

---

# 十五、为什么 V1 不需要 `read_file`

很多成熟 Coding Agent 都有：

```text
read_file
grep
list_dir
```

但当前 DevContext 不需要立即照搬。

因为当前知识系统已经是：

```text
Chunk Database
```

关系 Tool 返回的目标最终都能：

```text
symbol
↓
chunk_id
↓
CODE Chunk
```

已经足够生成回答。

加入：

```text
read_file(path, start, end)
```

会形成第二种完全不同的 Evidence 来源和 Citation 协议。

所以 V1 暂时不要。

---

# 十六、也不要加入 Shell / Code Execution Tool

V1 明确不要：

```text
bash
python execution
maven test
git
edit file
```

DevContext 当前目标还是：

> **Repository Understanding Agent**

不是：

> Coding Agent。

这是两个项目复杂度。

---

# 十七、Tool 的输出必须统一

成熟 Tool Agent 最重要的不是：

```text
model → function
```

而是：

> **工具返回结构必须稳定。**

建议：

```python
@dataclass
class ToolResult:
    call_id: str
    tool_name: str
    requirement_id: str

    status: str

    evidence_results: tuple[SearchResult, ...]

    observation: ToolObservation

    latency_ms: float

    error: ToolError | None = None
```

`status`：

```text
SUCCESS
EMPTY
PARTIAL
AMBIGUOUS
INVALID_ARGUMENT
ERROR
DEADLINE
```

---

# 十八、ToolObservation 不要只是字符串

例如：

```python
@dataclass
class ToolObservation:
    discovered_symbols: tuple[str, ...]
    graph_relations: tuple[RelationObservation, ...]
    file_paths: tuple[str, ...]
    truncated: bool
    summary: str
```

比如：

```text
find_callees(PayService#callback)
```

得到：

```text
discovered_symbols:
- M:PayService#notify(...)
- M:OrderClient#update(...)

relations:
PayService#callback
  --CALLS-->
PayService#notify

PayService#callback
  --CALLS-->
OrderClient#update
```

Agent 下一轮直接使用这些**真实 symbol_key**。

---

# 十九、禁止 Agent 自己编 Symbol Key

这是一个特别重要的安全约束。

Agent Planner 只能使用：

```text
available_symbols
```

中已经出现的 symbol。

例如 Memory 里有：

```text
M:demo.PayService#pay(String)
```

它才能调用：

```text
find_callees(
    "M:demo.PayService#pay(String)"
)
```

不能自己生成：

```text
M:demo.PayService#doSomething(...)
```

Runtime 必须验证。

如果不存在：

```text
INVALID_ARGUMENT
```

这样避免：

> **LLM hallucinated tool argument。**

---

# 二十、Agent 的 Memory 到底是什么

这个正好对应你最近学习的 Agent 架构。

这里的 Memory **不是用户长期记忆**。

是：

> 单次 Retrieval Agent 的执行记忆。

我建议：

```python
@dataclass
class AgentMemory:

    query: str

    evidence_plan: EvidencePlan

    coverage:
        dict[requirement_id, RequirementCoverage]

    available_symbols:
        dict[symbol_key, SymbolObservation]

    discovered_terms:
        dict[requirement_id, list[str]]

    action_steps:
        list[ActionStep]

    tool_history:
        list[ToolCallRecord]

    evidence_ids:
        dict[requirement_id, set[int]]

    error_history:
        list[ToolError]

    step_count: int
    tool_call_count: int

    stop_reason: str | None
```

但是：

> **Evidence 正文不要复制进 Memory。**

正文已经在：

```text
EvidenceWorkspace
```

Memory 只保存：

```text
ID
Symbol
状态
Tool history
Coverage gap
```

---

# 二十一、所以 EvidenceWorkspace 与 Memory 是不同东西

一定要把这两个区分开。

### EvidenceWorkspace

回答：

> 我目前拿到了哪些真实项目证据？

里面有：

```text
Chunk
Citation
Requirement ownership
正文
```

---

### AgentMemory

回答：

> 我已经做过什么？现在还缺什么？下一步有哪些合法工具可以用？

里面有：

```text
Step
ToolCall
ToolResult metadata
Coverage
Symbol
Error
```

关系：

```text
                AgentMemory
                   │
            管理执行状态
                   │
                   ▼
Agent Planner ─────┼───── Tool
                   │
                   ▼
            EvidenceWorkspace
                 证据本体
```

---

# 二十二、ActionStep 应该正式建模

这也是从固定 Workflow 升级到成熟 Agent 的关键。

```python
@dataclass
class ActionStep:

    step_number: int

    coverage_before:
        tuple[RequirementCoverage, ...]

    planned_calls:
        tuple[ToolCall, ...]

    results:
        tuple[ToolResult, ...]

    new_evidence_ids:
        tuple[int, ...]

    new_symbols:
        tuple[str, ...]

    coverage_after:
        tuple[RequirementCoverage, ...]

    progress:
        bool

    latency_ms: float
```

这样一次：

```text
Step 2
```

能够完整重放：

```text
为什么调用 Tool
Tool 返回什么
Workspace 新增什么
Coverage 有没有变化
```

这就是以后面试讲：

> Agent Memory

最好的真实项目案例。

---

# 二十三、Planner 输入不应该把整个 Workspace 都塞进去

这是 Agent 项目非常容易犯的问题。

如果每一轮都：

```text
User Query
+
全部代码
+
全部文档
+
所有 Tool 历史
+
全部 Graph
```

Token 会迅速爆炸。

AgentPlanner 只需要一个：

```text
AgentObservationView
```

例如：

```json
{
  "requirements": [
    {
      "id": "ER1",
      "target": "...",
      "state": "PARTIAL",
      "missing": [
        "缺少具体下游调用"
      ],
      "known_symbols": [
        "M:PayService#callback(...)"
      ]
    }
  ],

  "recent_steps": [
    {
      "tool": "search_code",
      "status": "SUCCESS",
      "new_evidence": 3
    }
  ],

  "available_tools": [...]
}
```

而不是把 3 个 CODE Chunk 全文再次发送。

---

# 二十四、Planner 应该输出什么

我建议继续采用项目现有熟悉的：

> **严格 JSON Contract**

而不是现在立刻依赖 OpenAI/DeepSeek 原生 Function Calling。

例如：

```json
{
  "actions": [
    {
      "requirement_id": "ER1",
      "tool": "find_callees",
      "arguments": {
        "symbol_key": "M:xxx.PayService#callback(java.lang.String)"
      },
      "reason": "已定位支付回调入口，需要补充下游调用"
    }
  ],
  "cannot_progress": false
}
```

一次允许：

```text
1～3 个 ToolCall
```

---

# 二十五、为什么先不要依赖原生 Tool Calling

当前系统已经同时支持：

```text
DeepSeek
OpenAI-compatible
```

而且已经有成熟：

```text
JSON parse
schema validation
fallback
```

如果现在强依赖某个 Provider 的：

```text
tool_calls
function_call
```

会把：

```text
Agent Runtime
```

与：

```text
LLM Transport
```

绑死。

V1 更好的设计是：

```text
LLM
↓
AgentDecision JSON
↓
Runtime
↓
Tool Registry
```

以后如果要支持 native function calling，只需要：

```text
NativeToolCallingAdapter
```

替换 Planner 输出层。

---

# 二十六、Agent Loop 应该怎么跑

我建议：

```python
while True:

    check_budget()

    if core_coverage_ready():
        STOP_READY

    decision = planner.plan(state)

    calls = validate(decision)

    if no_valid_call:
        STOP_NO_PROGRESS

    results = tool_executor.execute(calls)

    workspace.ingest(results)

    memory.record(step)

    if no_new_evidence:
        update_no_progress()

    coverage = coverage_checker.check(...)

    if ready:
        STOP_READY

    if limits_reached:
        stop(...)
```

但是实际实现：

> **禁止无界 `while True`。**

应该明确：

```text
MAX_AGENT_STEPS = 3
```

---

# 二十七、我推荐的硬上限

V1：

```text
MAX_AGENT_STEPS = 3

MAX_TOOL_CALLS = 8

MAX_CALLS_PER_STEP = 3

MAX_CONSECUTIVE_NO_PROGRESS = 1

MAX_PLANNER_FAILURES = 1
```

Graph Tool：

```text
max neighbors = 5
max hops = 2
```

继续沿用现有 Symbol Graph 契约。

---

# 二十八、为什么只给 3 Step

因为你不是在做：

> 自动完成一个半小时 Coding Task。

你在做：

> 针对一个问题找足够的 Repository Evidence。

实际应该类似：

```text
Step 1
search_code("支付回调流程")
    ↓
找到 PayService

Step 2
find_callees(PayService...)
    ↓
发现接口

Step 3
find_implementations(interface)
    ↓
证据足够
```

如果：

```text
3 Step
```

还没找到，

继续：

```text
10
20
50
```

多数情况下只会扩大噪声。

---

# 二十九、CoverageChecker 不能交给 Agent Planner 自己兼任

这是非常重要的架构决定。

不要做：

```text
Agent：
我觉得已经够了
→ READY
```

否则模型既：

```text
选择证据
```

又：

```text
裁判自己的证据
```

容易产生：

> **self-confirmation。**

应该继续：

```text
Agent Planner
     ↓
选择 Tool

CoverageChecker
     ↓
独立判断 Evidence 是否满足 Requirement
```

最终：

```text
READY
```

由：

```text
CoverageChecker
+
package_state()
```

决定。

---

# 三十、Agent 不能直接宣布 READY

Planner 最多可以说：

```text
cannot_progress=true
```

意思是：

> 当前没有合理 Tool 可以继续。

但：

```text
READY
```

必须来自 Coverage。

这样：

```text
Planner hallucination
```

不会导致：

```text
False READY
```

---

# 三十一、Coverage 不需要每次 ToolCall 都执行

如果：

```text
Step 1
```

里有：

```text
search_code
search_docs
```

不要：

```text
search_code
→ Coverage

search_docs
→ Coverage
```

而应该：

```text
Step
 ├─ Tool 1
 ├─ Tool 2
 └─ Tool 3

全部执行完成
      ↓
Workspace ingest
      ↓
Coverage once
```

也就是说：

> **Coverage 按 ActionStep 执行，而不是 ToolCall。**

最多：

```text
3 次 Coverage LLM
```

---

# 三十二、实际上多数问题应该只走 1～2 Step

因此设计目标不应该是：

```text
平均 Step = 3
```

而应该：

```text
简单问题 ≈ 1
跨文件关系 ≈ 2
困难关系 ≈ 3
```

最好评测：

```text
Mean steps to READY
```

保持：

```text
< 2
```

左右。

---

# 三十三、Tool Error 不能直接让 Agent 结束

这也是成熟 Agent 和简单 Workflow 的区别。

例如：

```text
find_symbol("Foo")
↓
AMBIGUOUS
```

Agent 下一步应该：

```text
search_code("Foo + 业务上下文")
```

---

例如：

```text
find_callees(...)
↓
EMPTY
```

可以：

```text
search_code(具体方法名)
```

---

例如：

```text
search_code
↓
Embedding timeout
```

可以记录：

```text
RETRYABLE
```

然后下一步尝试：

```text
find_symbol
```

如果已知 Symbol。

---

# 三十四、ToolError 建议结构化

```python
@dataclass
class ToolError:
    code: str
    message: str
    retryable: bool
```

错误类型：

```text
INVALID_ARGUMENT
AMBIGUOUS_SYMBOL
SYMBOL_NOT_FOUND
TOOL_TIMEOUT
DATABASE_ERROR
EMBEDDING_ERROR
DEADLINE_EXCEEDED
TOOL_UNAVAILABLE
```

不要 Planner 只看到：

```text
psycopg.errors....
```

---

# 三十五、No Progress Detection 非常重要

例如：

```text
Step 1
search_code("支付成功")
→ E1 E2 E3

Step 2
search_code("支付成功订单")
→ 还是 E1 E2 E3
```

虽然 Tool：

```text
SUCCESS
```

但 Agent 实际：

```text
0 new evidence
0 new symbol
coverage unchanged
```

就应该判断：

```text
NO_PROGRESS
```

而不是继续：

```text
Step 3:
再搜一次
```

---

# 三十六、Progress 可以这样定义

```text
progress =
    new_evidence_count > 0
    OR new_symbol_count > 0
    OR coverage_improved
```

Coverage：

```text
MISSING
→ PARTIAL
→ SATISFIED
```

属于 progress。

如果：

```text
连续一次完整 Step
```

：

```text
无新 Evidence
无新 Symbol
Coverage 不变
```

V1 就可以：

```text
STOP_NO_PROGRESS
```

---

# 三十七、建议的 StopReason

统一：

```text
READY

MAX_STEPS

MAX_TOOL_CALLS

NO_PROGRESS

NO_USEFUL_TOOL

DEADLINE

PLANNER_FAILED

ALL_TOOLS_FAILED
```

注意：

```text
READY
```

才是成功。

其他：

```text
EvidencePackage
```

根据 Workspace + Coverage 得到：

```text
PARTIAL
EMPTY
RETRIEVAL_FAILED
```

---

# 三十八、SourceRequirement 必须约束 Tool 权限

这是一个很重要的 Tool Permission 设计。

### CODE

允许：

```text
search_code
find_symbol
find_callers
find_callees
find_implementations
find_hierarchy
```

禁止：

```text
search_docs
```

除非 Requirement 是 ANY/BOTH。

---

### DOCUMENT

只允许：

```text
search_docs
```

---

### BOTH

允许所有。

而且 Coverage 最终必须：

```text
CODE + DOCUMENT
```

都有。

---

### ANY

允许 Agent 自己选择。

---

# 三十九、Tool Registry 可以负责权限

```python
@dataclass
class ToolSpec:

    name: str

    allowed_sources: set[str]

    schema: dict

    max_results: int

    cost: str
```

例如：

```text
find_callees:
    allowed_sources =
      CODE / BOTH / ANY
```

Planner 即使输出：

```text
DOCUMENT requirement
→ find_callees
```

Runtime 直接：

```text
INVALID_TOOL_FOR_REQUIREMENT
```

---

# 四十、Tool Planner 不能控制底层安全参数

LLM 不应该传：

```json
{
  "max_hops": 100,
  "top_k": 500,
  "timeout": 300
}
```

这些必须由 Runtime 固定。

Planner 能控制的应该很少：

```text
query
symbol_key
```

最多。

例如：

```text
search_code(query)
```

Runtime 固定：

```text
top_k = CORE_TOP_K / SUPPORTING_TOP_K
```

Graph：

```text
max_hops <= 2
neighbors <= 5
```

---

# 四十一、Agent 模式下应该关闭“自动 Graph Expansion”

当前：

```text
RetrievalController
    ↓
search
    ↓
CodeGraphExpander 自动增强
```

Tool Agent 上线后：

> **不能同时保留这层自动 Graph Expansion。**

否则：

```text
Agent：
search_code()

系统偷偷：
Graph Expansion

Agent：
find_callees()
```

同一 Graph 被用两遍。

也无法分析：

> Tool Agent 到底有没有主动选择正确 Tool。

因此：

```text
Fixed RetrievalController
    → 可选 automatic GraphExpander

ToolDrivenRetrievalController
    → 不自动 expand
    → Graph 必须显式 ToolCall
```

这点要写死。

---

# 四十二、Tool Agent 和现有 `SYMBOL_GRAPH_ENABLED` 要分开

现在：

```text
SYMBOL_GRAPH_ENABLED
```

控制的是：

> Fixed Retrieval 中自动 Graph Expansion。

新增：

```text
TOOL_AGENT_ENABLED=false
```

Agent 开启后：

```text
SYMBOL_GRAPH_ENABLED=false
```

也不代表：

```text
Agent 不能使用 Graph Tool
```

因为 Graph 数据一直存在。

应该：

```text
SYMBOL_GRAPH_ENABLED
= Fixed Retrieval 自动扩图开关

TOOL_AGENT_ENABLED
= 是否选择 ToolDrivenRetrievalController
```

这是两个不同概念。

---

# 四十三、建议配置非常少

不要搞 20 个环境变量。

V1：

```text
TOOL_AGENT_ENABLED=false

TOOL_AGENT_PLANNER_TIMEOUT_SECONDS=60
```

其他限制写代码常量：

```text
MAX_STEPS = 3
MAX_TOOL_CALLS = 8
MAX_CALLS_PER_STEP = 3
```

经过实验后再开放配置。

---

# 四十四、Planner 的 fallback 也要设计

Planner 输出 JSON 错误不能直接：

```text
Agent failed
```

可以有确定性 fallback。

例如 ER：

```text
source_requirement=CODE
```

且没有 Symbol：

```text
fallback:
search_code(requirement.target + success_criteria)
```

如果：

```text
已有唯一 symbol
+
success criteria 出现“调用”
```

可以：

```text
find_callees(symbol)
```

但 fallback 不要过度智能。

---

# 四十五、Agent Trace 要和当前 Trace 体系融合

新增：

```text
AgentTrace
```

里面：

```text
planner_calls
steps
tool_calls
stop_reason
total_agent_ms
```

每个 ToolCall：

```text
tool
args
requirement_id
status
latency
new_evidence_count
new_symbols
error
```

---

# 四十六、以后 `--debug` 应该能看到这样的输出

```text
Agent Retrieval

Step 1
  ER1
  tool: search_code
  query: "支付回调订单状态更新"
  strategy: vector
  results: 5
  new evidence: 5
  symbols discovered:
    PayServiceImpl#payCallback(...)

Coverage:
  ER1 PARTIAL
  missing:
    下游方法链

Step 2
  tool: find_callees
  symbol:
    M:...PayServiceImpl#payCallback(...)
  results: 3
  new evidence: 2

Coverage:
  ER1 SATISFIED

Stop:
  READY

Steps: 2
Tool calls: 2
Agent retrieval: 1.8s
```

这会非常适合展示和调试。

---

# 四十七、Agent 的评测不能只看最终 Coverage

这是这一功能最重要的一部分。

新增：

```text
benchmark/tool-agent-v1.jsonl
```

至少包含四种 Case。

---

## A. Tool Choice

例如：

```text
问题：
谁调用 notifyPayResult？
```

期望：

```text
find_symbol
→ find_callers
```

而不是：

```text
search_code
search_code
search_code
```

指标：

```text
Tool Selection Accuracy
```

---

# 四十八、B. Tool Efficiency

例如明明：

```text
find_symbol
```

一次就能解决。

Agent 却：

```text
search_code
search_docs
find_symbol
find_callers
```

虽然回答正确，

但是：

```text
Unnecessary Tool Calls
```

很高。

所以要统计：

```text
tool_calls_to_ready
```

和：

```text
redundant_tool_call_rate
```

---

# 四十九、C. Error Recovery

人为模拟：

```text
find_symbol → AMBIGUOUS
```

检查 Agent 是否：

```text
重新检索上下文
```

而不是：

```text
直接结束。
```

---

# 五十、D. No Progress

连续返回：

```text
相同 Chunk
```

Agent 应停止：

```text
NO_PROGRESS
```

而不是跑满预算。

---

# 五十一、真实业务 Case 应特别覆盖 Symbol Graph

现在可以直接复用你刚刚应该增加的业务 Graph-sensitive 场景：

```text
Controller → Service

Service → Interface → Impl

上游 caller

两跳本地调用链

继承/override

constructor relation
```

Tool Agent 的意义恰好就在这些 Case。

---

# 五十二、建议核心指标

### 正确性

```text
CORE Coverage

Full-case Success

False READY

Evidence Recall
```

---

### Agent 能力

```text
Tool Selection Accuracy

Tool Success Rate

Invalid Tool Call Rate

Ambiguous Symbol Recovery Rate

No-progress Detection Rate

Steps to READY
```

---

### 效率

```text
Mean Tool Calls

P95 Tool Calls

Mean Agent Steps

Planner LLM Calls

Coverage Calls

Retrieval latency

Token usage
```

---

### 结构能力

```text
Graph Tool Usage

Graph Rescue

Symbol-to-Graph transition success
```

例如：

```text
search_code
↓
symbol discovered
↓
find_callees
```

这类 transition 很值得单独统计。

---

# 五十三、A/B 至少比较三组

我建议未来评测：

```text
A:
Current Fixed Retrieval
Graph OFF

B:
Current Fixed Retrieval
Graph ON

C:
Tool Agent
```

保持：

```text
EvidencePlanner
Corpus
Top-k budgets
CoverageChecker
Fast/Full
```

一致。

于是能回答：

> 是 Symbol Graph 自动扩展有效？

还是：

> 由 Agent 主动选择 Graph Tool 更有效？

这非常有研究价值。

---

# 五十四、开启门槛建议

Tool Agent 不要做完就默认启用。

继续：

```text
TOOL_AGENT_ENABLED=false
```

只有满足：

```text
CORE Coverage >= Fixed baseline

False READY <= baseline

Graph-sensitive business cases 明显提升

Invalid Tool Call Rate < 5%

No Progress 能正确停止

平均 Tool Calls <= 4

P95 steps <= 3

Agent Retrieval latency 可接受
```

再考虑启用。

---

# 五十五、非常重要：不要为了 Agent 强行引入 LangGraph

当前真正需要的结构只有：

```text
State
Loop
Tool Registry
Branch
Retry
Stop
Trace
```

普通 Python 完全可以：

```python
for step_number in range(MAX_STEPS):
    ...
```

实现得很清楚。

此时如果为了简历：

```text
迁移 LangGraph
```

会产生：

```text
大量框架代码变化
+
几乎没有能力提升
```

我不推荐。

---

# 五十六、什么时候才值得上 LangGraph

以后如果真正需要：

```text
Checkpoint

Resume

Human approval

Long-running task

Parallel branches

Sub-agent

Durable execution
```

再考虑：

```text
LangGraph
```

现在：

> Tool Agent V1 应该首先证明 Agent Design 是对的。

而不是证明：

> 会用框架。

---

# 五十七、也不要做 Multi-Agent

目前完全不需要：

```text
SearchAgent
GraphAgent
DocumentAgent
ReviewerAgent
```

一个：

```text
ToolDrivenRetrievalAgent
```

已经够了。

工具本身负责专门能力。

这是：

```text
One Agent + Many Tools
```

而不是：

```text
Many Agents
```

后者会显著增加：

```text
LLM calls
latency
debug difficulty
```

---

# 五十八、V1 明确不做的内容

建议冻结：

```text
❌ Multi-Agent
❌ Long-term Memory
❌ MCP
❌ Shell
❌ Code Execution
❌ File Editing
❌ Git Tool
❌ Browser
❌ GraphRAG
❌ Agent-generated SQL
❌ Dynamic Tool Creation
❌ LangGraph migration
❌ Unlimited loop
```

这是一个：

> **Repository Evidence Agent**

而不是通用智能体平台。

---

# 五十九、具体开发阶段

我推荐分 7 个 Phase。

---

## Phase 0：冻结 Agent Contract

先只写模型和测试：

```text
ToolCall
ToolResult
ToolError
ToolObservation
ActionStep
AgentMemory
AgentDecision
StopReason
```

先确定：

```text
状态
错误
上限
```

不要先写 Prompt。

---

# 六十、Phase 1：Tool Registry

实现：

```text
search_code
search_docs
find_symbol
find_callers
find_callees
find_implementations
find_hierarchy
```

先通过：

```text
纯 Python fixture
```

测试。

这一步完全没有 Agent LLM。

---

# 六十一、Phase 2：Tool Executor

统一负责：

```text
schema validation
permission
deadline
latency
error classification
result normalization
```

例如：

```text
Agent
 ↓
ToolCall
 ↓
ToolExecutor
 ↓
Registry
 ↓
Tool
 ↓
ToolResult
```

不要让 Runtime：

```text
if tool == search...
elif tool == caller...
```

---

# 六十二、Phase 3：Agent Planner

加入：

```text
strict JSON
```

Planner 输入：

```text
requirements
coverage gaps
available symbols
recent steps
tool specs
```

输出：

```text
1～3 ToolCalls
```

先跑 Planner contract test。

---

# 六十三、Phase 4：Agent Runtime

实现：

```text
Plan
→ Act
→ Observe
→ Ingest
→ Coverage
→ Decide Continue
```

用：

```text
for step in range(MAX_STEPS)
```

而不是递归或无限循环。

---

# 六十四、Phase 5：ToolDrivenRetrievalController

新 controller：

```text
EvidencePlanner
↓
AgentRuntime
↓
EvidenceWorkspace
↓
Context
↓
EvidencePackage
```

返回值必须和现有：

```text
RetrievalController
```

完全一致。

因此：

```text
Fast
Full
```

零修改。

---

# 六十五、Phase 6：Trace + Failure Recovery

重点测试：

```text
Planner invalid JSON

Tool invalid args

ambiguous symbol

tool timeout

embedding failure

graph unavailable

no progress

deadline

max steps
```

确保：

```text
不会无限循环
不会 False READY
不会因为一个 Tool Error 丢失已有 Evidence
```

---

# 六十六、Phase 7：真实 A/B

至少：

```text
现有 L1.5 + regression

Symbol Graph business benchmark

Tool-Agent 专用 benchmark
```

三套都跑。

最终报告：

```text
Fixed Retrieval
vs
Fixed + Auto Graph
vs
Tool Agent
```

---

# 六十七、一个完整例子

用户问：

> “购票接口调用 PurchaseTicketService 后，真正执行占座的是哪个实现，完整调用链是什么？”

EvidencePlanner：

```text
ER1:
定位购票入口及服务调用

ER2:
确定接口实现

ER3:
恢复占座关键调用路径
```

---

### Step 1

Agent：

```json
{
  "actions": [
    {
      "requirement_id": "ER1",
      "tool": "search_code",
      "arguments": {
        "query": "购票 PurchaseTicketService"
      }
    }
  ]
}
```

结果：

```text
PurchaseTicketController
PurchaseTicketService

available symbols:
T:...PurchaseTicketService
M:...purchase(...)
```

Coverage：

```text
ER1 SATISFIED
ER2 MISSING
ER3 MISSING
```

---

### Step 2

Agent：

```text
find_implementations(
  M:PurchaseTicketService#purchase(...)
)
```

得到：

```text
PurchaseTicketServiceImpl#purchase(...)
```

以及 CODE Chunk。

Coverage：

```text
ER2 SATISFIED
ER3 PARTIAL
```

---

### Step 3

Agent：

```text
find_callees(
  M:PurchaseTicketServiceImpl#purchase(...)
)
```

得到：

```text
PurchaseTicketTxService#doPurchase(...)
```

Coverage：

```text
ER3 SATISFIED
```

Runtime：

```text
STOP_READY
```

最终：

```text
EvidencePackage
↓
Full
```

进行教学回答。

这才是真正的：

> **Tool-driven Repository Agent。**

---

# 六十八、和当前自动 Symbol Graph 的最大区别

现在：

```text
Search
↓
系统规则自动 Graph Expansion
```

模型不知道 Graph 为什么执行。

未来：

```text
Search
↓
Agent 看到 Evidence
↓
发现缺调用链
↓
主动 find_callees
↓
发现接口
↓
主动 find_implementations
```

这就形成：

```text
Reason
→ Tool
→ Observation
→ New Reason
→ Tool
```

Agent 能力真正建立起来了。

---

# 六十九、这个功能对于面试的价值也比“接 LangGraph”大

完成后可以这样描述：

> **将原有固定两轮 RAG 检索升级为有界 Tool-driven Retrieval Agent。EvidencePlanner 先定义待验证事实，Agent 基于 Coverage Gap 和已发现 Symbol 动态选择语义检索、文档检索、Symbol 定位及 callers/callees/implementations 等工具；每轮 ToolResult 统一进入 EvidenceWorkspace，由独立 CoverageChecker 决定是否满足证据需求，并通过最大 Step、Tool Call、No-Progress 与 Deadline 机制保证执行有界。**

继续：

> **Agent Memory 只保存执行状态、工具历史、Symbol 和 Coverage，不重复保存证据正文；EvidenceWorkspace 作为证据事实源。Planner 不能自行宣布 READY，也不能伪造 Symbol Key，所有 Graph Tool 参数必须来自已经解析出的 Symbol Handle。**

这已经是一套非常标准且扎实的 Agent 设计。

---

# 七十、我给当前项目的推荐开发顺序

结合 Symbol Graph 刚完成，而且真实业务 Graph 提升尚未完全证明，我会这样排：

```text
P0
真实 Graph-sensitive business benchmark
约半天

        ↓

P0
Tool Registry + Tool Contract
约半天

        ↓

P0
AgentState / ActionStep / Memory
约半天

        ↓

P0
Agent Planner + bounded runtime
约 1 天

        ↓

P1
ToolDrivenRetrievalController
约半天

        ↓

P1
Failure / No Progress / Trace
约半天

        ↓

P1
Fixed vs Auto Graph vs Tool Agent A/B
约半天
```

也就是：

> **大约 2～3 天可以形成一个完整 Tool Agent V1。**

---

# 最终建议

当前 DevContext 的下一次大改造，不应该是：

```text
GraphRAG
或
LangGraph rewrite
```

而应该是：

```text
                EvidencePlanner
                       ↓
                Evidence Goals
                       ↓
               Tool Agent Runtime
                ↙     ↓      ↘
          Search    Symbol    Graph
                ↘     ↓      ↙
                EvidenceWorkspace
                       ↓
                 CoverageChecker
                       ↓
                 EvidencePackage
                       ↓
                  Fast / Full
```

而且最重要的是保留当前项目已经做得比较好的三个边界：

> **EvidencePlanner 决定“要证明什么”；Agent 决定“用什么工具找”；CoverageChecker 独立决定“够不够”。**

这三个角色不要合并。这样实现出来的 Tool-driven Agent 才会是一次真正的架构升级，而不是给现有 RAG 外面套一个 `while + tool_call`。