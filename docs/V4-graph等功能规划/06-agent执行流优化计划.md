

# V5：让 Evidence Loop 真正闭环

目标：

> 让系统知道“当前缺什么证据”，并主动补齐，而不是让 Agent 猜下一步。

---

# 一、当前代码状态判断

## 已经完成的能力

### 1. Hybrid Retrieval

成熟度：

★★★★★

不用继续优化。

原因：

你已经做过：

- query router
- stage aware retrieval
- keyword/vector策略
- L1.5实验

当前问题不是召回不到。

---

## 2. Symbol Graph

成熟度：

★★★★☆

已经达到可用状态。

当前能力：

```
Symbol

CLASS
INTERFACE
METHOD
CONSTRUCTOR


Edge

CALLS
CONSTRUCTS
IMPLEMENTS
EXTENDS
OVERRIDES
```

实际价值：

已经证明：

```
普通搜索:
找到相关代码


Graph:
找到代码之间关系
```

区别明显。

不要继续扩展。

---

## 3. Tool Agent

成熟度：

★★★☆☆

现在属于：

> 能运行，但不是可靠调查员。


原因不是工具少。

7个工具已经足够：

```
search_code

search_docs

find_symbol

find_callers

find_callees

find_implementations

find_hierarchy
```

问题：

Agent不知道：

```
什么时候必须调用Graph
什么时候搜索已经够
什么时候不能READY
```

---

# 二、当前真正的问题定位

我认为有三个核心问题。

---

# 问题1：EvidenceRequirement表达能力不足

当前：

```
EvidenceRequirement

target

success_criteria

status

missing
```

类似：

```
需要证明：
OrderService.createTicketOrder实现流程
```

但是对于系统：

这个需求可能有多个含义。


例如：

用户：

> OrderService.createTicketOrder接口由谁实现？


真正需求：

不是：

```
找到OrderService
找到OrderServiceImpl
```

而是：

需要证明：

```
Interface Method

        OVERRIDES

Implementation Method
```

但是现在系统没有表达：

```
需要什么关系
```

---

# 所以第一阶段不是改Agent

而是增加：

## Retrieval Requirement Schema


新增：

```python
class RetrievalNeed:

    evidence_type:

        CODE
        DOCUMENT
        RELATION


    relation_type:

        CALLS
        CALLED_BY
        IMPLEMENTS
        OVERRIDES
        EXTENDS


    anchor_requirement:

        NEED_SYMBOL
        NEED_METHOD
        NEED_CLASS


```


例如：

用户问题：

```
OrderService.createTicketOrder实现是谁？
```

转换：

```json
{
 "evidence_type":"RELATION",

 "relation_type":"OVERRIDES",

 "anchor":
 "OrderService#createTicketOrder"
}
```


此时后续所有模块都有依据。

---

# 为什么这是第一步？

因为它改动最小。

不用：

- 重写Agent
- 重写Coverage
- 重写Graph


只是增加：

```
Requirement
        |
        |
 Retrieval Need
```

这一层。


---

# 三、第二阶段：修改 Tool Agent 决策逻辑

当前：

大概：

```
Agent看到缺口

↓

LLM决定tool
```

问题：

LLM需要自己理解：

```
缺什么
```

容易错误。


改成：

```
Requirement

↓

Retrieval Need

↓

Tool候选集合

↓

Agent选择
```


例如：

---

## Case1

需求：

```
谁调用createOrder
```

系统：

```json
{
relation_type:"CALLS",
direction:"INCOMING"
}
```


Tool候选：

```
find_callers
```

Agent只能：

```
确认参数
```

不是：

从7个工具乱选。


---

## Case2

需求：

```
接口实现是谁
```


候选：

```
find_implementations
```

---

## Case3

需求：

```
订单创建流程
```


候选：

```
find_callees

+
search_code
```

---

这样Agent从：

```
自主探索
```

变成：

```
受约束规划
```

更加符合企业Agent设计。

---

# 四、第三阶段：修复最大的质量问题——False READY


当前数据：

```
固定:
False READY 8


Graph:
False READY 3


Agent:
False READY 11
```


说明：

Graph已经改善。

Agent反而下降。


核心原因：

Coverage判断：

```
相关证据
=
完整证据
```


例如：

找到：

```
OrderService.java

OrderServiceImpl.java
```


Coverage:

```
OK
```

但是：

缺：

```
implements关系
```


---

所以增加：

# Evidence Completeness Gate


不要马上改Coverage。


增加一层：

```
EvidenceValidator
```


流程：

现在：

```
EvidenceWorkspace

↓

Coverage LLM

↓

READY
```


改：

```
EvidenceWorkspace

↓

Structural Validator

↓

Coverage LLM

↓

READY
```


---

Structural Validator负责：

关系问题：

检查：

```
CALLS

OVERRIDES

IMPLEMENTS

EXTENDS
```


是否存在。

---

例如：

Requirement:

```
IMPLEMENTATION
```

Validator:

```
是否存在:

Interface Method

        OVERRIDES

Implementation Method

```

没有：

直接：

```
PARTIAL
```


不要让LLM犯这种错误。

---

# 五、第四阶段：让Graph和Tool共享结果

当前：

自动Graph：

```
GraphExpansionTrace
```

Tool:

```
ToolObservation
```


两个系统重复。


统一：

增加：

```
EvidenceRelation
```


结构：

```python
class EvidenceRelation:

 source_symbol

 target_symbol

 relation_type

 source:

    auto_graph

    tool_agent


 confidence

```


然后：

Workspace:

现在：

```
chunks
```

增加：

```
relations
```


变成：

```
EvidenceWorkspace

   Chunks

   Symbols

   Relations
```


这样：

Answer阶段：

仍然引用Chunk。


Coverage阶段：

可以看到关系。


---

# 六、第五阶段：重新设计Agent停止条件

当前：

```
Coverage enough

↓

STOP
```


问题：

Coverage太宽。


改：

Agent Stop:

必须满足：

```
Semantic Coverage

AND

Structural Coverage
```


例如：

普通代码解释：

只需要：

```
Semantic
```


但是：

调用链：

需要：

```
Semantic
+
Relation
```


---

# 七、不要做的事情

非常重要。

## 1. 不增加Graph能力

不要：

```
Spring Bean Graph

MQ Graph

Database Graph
```

原因：

当前瓶颈不是关系数量。

---

## 2. 不增加Tool数量

不要：

```
read_file

shell

grep

git
```


当前项目定位：

不是Claude Code。

你的定位：

```
代码知识库问答Agent
```

不是：

```
Coding Agent
```


---

## 3. 不学习LangGraph重构

现在有人容易想到：

```
是不是应该改LangGraph
```

不需要。

你现在的问题：

不是workflow框架。


---

# 八、具体执行计划（可直接给Agent开发）

---

# Phase 0：基线冻结（0.5天）


目标：

保证后续优化可评价。


任务：

保存：

```
tool-agent-live-abc.json

symbol graph report

fast regression

full regression
```


新增：

```
V5 baseline report
```


---

# Phase 1：Retrieval Need（1天）


修改：

```
evidence/schema.py

planner/

retrieval/
```


新增：

```
RetrievalNeed
```


输出debug：

例如：

```
Requirement:

实现是谁


Need:

RELATION

OVERRIDES

anchor:
OrderService#createOrder
```


测试：

增加10个固定case。

---

# Phase 2：Tool Agent约束选择（1-2天）


修改：

```
tool_agent/planner.py

runtime.py
```


目标：

Agent action:

从：

```
自由选择
```

变：

```
候选工具空间
```


指标：

观察：

```
Graph Tool调用率
```

不是越高越好。


重点：

TA06
TA07

这些失败case。


---

# Phase 3：Relation Evidence（2天）


新增：

```
EvidenceRelation
```


修改：

```
EvidenceWorkspace
Coverage
```


目标：

Coverage知道：

```
代码关系
```


---

# Phase 4：Structural Gate（1天）


新增：

```
StructuralCoverageChecker
```


规则：

|需求|必须关系|
|-|-|
|谁调用|CALLS|
|谁实现|IMPLEMENTS/OVERRIDES|
|继承|EXTENDS|
|流程|CALL_CHAIN|


---

# Phase 5：重新评测（1天）


跑：

## Oracle

目标：

Agent:

```
84.62%
→95%+
```


## Semantic

目标：

当前：

```
CORE 75.56

Full 70.83

False READY 11
```


达到：

至少：

```
CORE >90

Full >85

False READY <=5
```


---

# 九、最终架构目标


完成后：

```
User Query

↓

Evidence Planner

↓

Evidence Requirement

↓

Retrieval Need

↓

---------------------

固定检索
Graph
Tool Agent

---------------------

↓

Evidence Workspace

    Chunk Evidence

    Relation Evidence


↓

Structural Validator

↓

Coverage

↓

Answer
```


---

# 十、优先级排序（结合你当前项目）

如果只做三件事：

## 第一：

Retrieval Need

★★★★★

收益最大。


## 第二：

Structural Coverage Gate

★★★★★

直接降低False READY。


## 第三：

Tool Agent Tool Selection约束

★★★★☆


---

我的建议：

**不要马上进入V5大版本开发。**

下一次提交应该是一个小而关键的 PR：

> `feat: introduce retrieval need schema for evidence planning`

因为它会成为后面 Graph、Tool Agent、Coverage 三者共同的基础。

当前项目已经过了“增加能力”的阶段，进入的是：

> **让已有能力形成可靠闭环的阶段。**