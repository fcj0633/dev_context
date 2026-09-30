# DevContext Answer Quality V3：Explanation Planner 与教学型回答重构开发计划

## 0. 本轮任务定位

### 0.1 仓库与基线

目标仓库：

`fcj0633/dev_context`

开发前必须基于最新 `main`，重点阅读：

```text
README.md

src/devcontext/
├── agentic/
│   ├── evidence_workflow.py
│   ├── retrieval_controller.py
│   ├── evidence_models.py
│   ├── coverage.py
│   ├── search_actions.py
│   └── rewrite.py
│
├── answer/
│   ├── models.py
│   ├── planner.py
│   ├── generator.py
│   └── reviewer.py
│
├── context/
│   └── builder.py
│
├── planning/
│   └── evidence_planner.py
│
├── evaluation/
│   ├── answer_quality.py
│   └── answer_quality_runner.py
│
├── request.py
└── cli.py

benchmark/
├── l1.5-retrieval.jsonl
└── l2-answer-quality.jsonl

docs/
├── verification.md
└── 后续开发规划/
    ├── 08-证据驱动项目解读系统设计与实现.md
    └── 09-输入、检索、回答职责重构计划.md
```

当前重要基线提交附近：

```text
5d2d9f6 fix: correct second-round retrieval evaluation
```

**不要脱离当前实现重新设计一套系统。**

本轮原则：

> 保留已经完成的 Evidence-driven Retrieval，把升级重点放到 EvidencePackage 之后。

---

# 1. 是否优先开发 Explanation Planner

## 1.1 结论

**是，当前应当优先。**

但更准确地说：

> 不应该单独开发一个新的 `ExplanationPlanner` 类，而应该把现有 `AnswerPlanner → Generator → Reviewer` 升级成完整的 Teaching Explanation Pipeline。

理由有四个。

---

## 1.2 原因一：当前检索已经足以支撑回答质量优化

当前项目已经具备：

```text
EvidencePlanner
    ↓
SearchActionPlanner
    ↓
RetrievalController
    ↓
keyword / vector / hybrid
    ↓
EvidencePool
    ↓
CoverageChecker
    ↓
second-round targeted retrieval
    ↓
EvidencePackage
```

并且存在：

```text
L1
L1.5
L2
```

三层评测。

现阶段继续主要投入：

```text
Top-K 微调
第三轮 Query Rewrite
再增加 Sufficiency Checker
更多路由规则
```

边际收益已经开始下降。

现在最明显的用户问题已经从：

> 找不到证据

转变为：

> 找到了证据，但是不会把问题讲懂。

---

# 2. 当前回答为什么仍然和 ChatGPT 有明显差距

以用户提供的“余票桶”回答为核心案例。

当前 DevContext 一开始进入：

```text
定位与命名
Redis Hash
装载初始化
Lua
归还
锁
一致性
```

它基本沿着：

```text
代码模块
↓
实现细节
↓
实现细节
↓
实现细节
```

展开。

这是一份不错的：

> 源码说明书。

但还不是优秀的：

> 教学型系统解释。

---

## 2.1 ChatGPT 回答做得更好的第一件事：先建立“为什么”

ChatGPT 并没有先说：

```text
TicketAvailabilityTokenBucket
Hash
Lua
TTL
```

而是先构造一个问题：

```text
只有 10 张票
但同时有 50 个请求

如果全部进入购票链路：

50 个请求
→ 抢锁
→ 占线程
→ 占数据库连接
→ 开事务
→ SQL

但实际上只有 10 个请求有成功可能。
```

然后提出：

> 为什么另外 40 个明知不可能成功的请求，还一定要走到数据库才知道失败？

从这个问题自然引出 Redis 准入层。

这建立的是：

```text
问题
→ 约束
→ 为什么需要方案
→ 方案
```

当前 DevContext 更像：

```text
方案是什么
→ 方案内部有什么
```

这是最重要的差异之一。

---

# 3. 当前回答问题的系统性诊断

## 3.1 问题 A：以 Evidence 为中心，而不是以 Reader Mental Model 为中心

当前：

```text
EvidenceRequirement
→ 找证据
→ AnswerSection
```

虽然 `AnswerPlanner` 已经明确要求：

> Requirement 不能直接一一变成 Section。

这是正确的。

但目前 `AnswerPlan` 的结构仍然只有：

```python
direct_answer
summary_citation_labels
explanation_strategy
sections
unresolved_gaps
conflicts
answer_goal
answer_depth
```

每个 Section：

```python
title
purpose
key_points
evidence_labels
target_chars
```

这里缺少一个非常重要的信息：

> 用户最终应该在脑子里形成什么模型？

例如余票桶问题真正应该形成：

```text
MySQL = 真实库存

Redis Token Bucket
       =
进入真实库存竞争之前的准入凭证
```

然后所有内容都围绕这个心智模型展开。

现在没有对应的数据结构。

---

# 4. 问题 B：`explanation_strategy` 太弱

现在：

```python
explanation_strategy: str
```

本质是一个自由文本字段。

这不足以真正控制回答。

需要把它升级为结构化解释策略，例如：

```text
PROBLEM_SOLUTION

CONCEPT_BUILDUP

EXECUTION_FLOW

CAUSE_EFFECT

LAYERED_ARCHITECTURE

FAILURE_ANALYSIS

TRADEOFF

COMPARISON

NEGATIVE_CORRECTION

LOCATION_ONLY
```

并允许：

```text
primary_strategy
+
secondary_strategies
```

例如余票桶：

```text
primary:
PROBLEM_SOLUTION

secondary:
CONCEPT_BUILDUP
FAILURE_ANALYSIS
TRADEOFF
```

---

# 5. 问题 C：当前 System Prompt 过度限制教学能力

当前 `SYSTEM_PROMPT` 有一个非常重要的约束：

```text
不得使用模型记忆、常识或猜测补充 Context 中没有出现的项目实现细节。
```

“不得猜项目实现”完全正确。

问题是现在实际上容易被模型理解成：

> Context 没写的东西都不要讲。

这会严重削弱解释能力。

例如：

```text
Redis Lua 为什么需要原子执行
什么叫准入层
为什么先挡流量再抢锁
为什么偏松比偏紧安全
```

其中有一部分是：

> 通用计算机知识。

它不应该被当成：

> 当前项目事实。

---

# 6. 必须建立四层内容语义

本轮最重要的数据模型之一：

```text
PROJECT_FACT

PROJECT_INFERENCE

GENERAL_CONCEPT

ILLUSTRATIVE_EXAMPLE
```

## PROJECT_FACT

例如：

```text
purchaseTickets 在获取分布式锁之前调用 takeToken。
```

要求：

```text
必须有项目 Citation。
```

---

## PROJECT_INFERENCE

例如：

```text
由调用顺序可以看出，Token Bucket 的主要职责是前置准入，
而不是最终数据库库存校验。
```

要求：

```text
必须能追溯到项目证据。

措辞必须体现：
“因此”
“说明”
“从调用顺序来看”
```

不能装成直接代码事实。

---

## GENERAL_CONCEPT

例如：

```text
Redis Lua 可以把多个 Redis 操作作为一个不可被其它 Redis 命令插入的执行单元，
因此常用于“读取 → 判断 → 修改”这类需要原子性的操作。
```

允许模型使用基础技术知识解释。

但是：

> 不能因为 Redis Lua 通常这么写，就声称当前项目 Lua 一定采用某种内部算法。

---

## ILLUSTRATIVE_EXAMPLE

例如：

```text
假设只剩 10 张票，同时进来 50 个请求……
```

要求明确使用：

```text
假设
例如
可以想象
```

绝不能让用户误以为这是项目真实运行数据。

---

# 7. 当前最严重的架构瓶颈：ContextBundle

当前系统最终形成：

```text
EvidencePackage
    ↓
ContextBundle
    ↓
rendered_text
```

然后：

```text
AnswerPlanner
Generator
Reviewer
```

都使用这一个 `rendered_text`。

这就是下一阶段必须改掉的地方。

---

# 8. 当前 Context 限制的具体问题

`RetrievalController` 目前定义：

```python
COMPACT_CONTEXT_BUDGET = 8_000
BALANCED_CONTEXT_BUDGET = 16_000
BROAD_CONTEXT_BUDGET = 28_000
```

单位还是：

```text
字符
```

而不是 Token。

ContextBuilder 还会在预算不足时：

```text
直接截断 Chunk
```

形成：

```text
… [truncated]
```

这产生几个问题。

### 问题 1：证据一旦没进入 Context，后续所有阶段都看不到

即使 Retrieval 已经找到：

```text
C1
C2
C3
...
C20
```

最终只塞进去：

```text
C1
C2
C3
C4
```

那么：

```text
AnswerPlanner
Generator
Reviewer
```

实际上都只知道前四个。

---

### 问题 2：所有 Answer Section 抢同一个预算

例如：

```text
余票桶
```

可能涉及：

```text
入口
Hash
Lua
初始化
TTL
失败补偿
取消订单
MySQL
锁
查询缓存
```

这些内容全部争抢：

```text
28K chars
```

没有必要。

解释：

```text
Lua 原子性
```

时只需要 Lua 相关证据。

解释：

```text
Redis 和 MySQL 边界
```

时只需要另外一组证据。

---

# 9. 开源项目给出的关键启发

## 9.1 Aider：Context 是一个动态预算，而不是一次性塞满

Aider 的 Repo Map 不把整个仓库送给模型，而是：

```text
Repository
→ symbols
→ dependency graph
→ ranking
→ token budget
→ relevant repo map
```

而且 repo map 大小会根据当前任务动态调整。

对 DevContext 的直接启发：

> 不应该追求一个更大的 ContextBundle，而应该构建不同阶段需要的 Context View。

---

## 9.2 OpenHands：超过 Context 后做 Condensation

OpenHands 的 Agent 在 Context 过长时会进行：

```text
condense
```

使用压缩后的 Event View 继续推理；官方示例也展示了将旧历史压缩成摘要，同时保留最初的重要事件。

对 DevContext 的启发：

> 压缩可以发生，但原始证据不能消失。

因此必须区分：

```text
Evidence Storage

和

Prompt View
```

---

## 9.3 Continue：按需探索，而不是一次获得所有上下文

Continue 的 Plan Mode 提供：

```text
read_file
grep_search
glob_search
repo map
view_subdirectory
...
```

让模型在理解代码库时按需探索。

本轮不要求立刻把 DevContext 改成 Tool Agent。

但 Context 架构必须为以后：

```text
按需取证
```

留下接口。

---

# 10. 一个重要事实：目前 Context 限制主要是 DevContext 自己制造的

当前 DeepSeek API 的 `deepseek-flash`：

```text
Context Length: 1M tokens
Max Output: 384K tokens
```


而当前项目：

```text
Context:
8K / 16K / 28K chars

Explain answer max_tokens:
32768

Detailed answer:
2200–5000 中文字符
```

所以不要把：

```text
28K → 60K
```

作为解决方案。

这只是把常量变大。

正确架构应该是：

```text
Evidence Workspace
        ↓
按任务构造 Prompt Context View
        ↓
模型
```

---

# 11. 新总体架构

本轮最终目标：

```text
User Question
      │
      ▼
Evidence-driven Retrieval
      │
      ▼
EvidenceWorkspace
      │
      ├───────────────┐
      │               │
      ▼               │
Explanation Planner   │
      │               │
      ▼               │
ExplanationPlan       │
      │               │
      ▼               │
SectionContextAssembler
      │
      ▼
Section Draft Generator
      │
      ▼
Grounded Sections
      │
      ▼
Global Composer
      │
      ▼
Teaching Reviewer
      │
      ├── targeted revision
      │
      ▼
Final Answer
```

关键改变：

> Retrieval 输出的完整证据集合，不再直接等价于一次 Prompt Context。

---

# 12. Phase 0：先冻结当前基线

任何代码修改前必须完成。

执行：

```powershell
uv run pytest
uv run devcontext evaluate
uv run devcontext evaluate-retrieval-workflow --suite l1.5 --mode live
```

然后运行当前 L2：

```text
benchmark/l2-answer-quality.jsonl
```

至少保存以下几个问题的当前 `explain` 输出：

```text
flow-01
why-01
why-03
edge-01
negative-01
locate-01
```

另外加入当前核心 Golden Case：

```text
详细解释项目的余票桶是如何设计的
```

保存：

```text
artifacts/answer-quality-v2-baseline/
```

至少包括：

```text
question
EvidencePlan
SearchAction
Coverage
Context
AnswerPlan
Answer
Review
Stage latency
Token usage
```

后续所有质量改动必须与该基线 A/B。

---

# 13. Phase 1：新增 EvidenceWorkspace

## 13.1 不删除 ContextBundle

为了避免破坏已有：

```text
legacy
L1
L1.5
```

保留：

```python
ContextBundle
```

但新 `teach` 路径不再把它作为唯一证据容器。

---

## 13.2 新建

建议：

```text
src/devcontext/context/
├── builder.py             # legacy，保留
├── workspace.py
├── views.py
└── budget.py
```

---

## 13.3 EvidenceRef

```python
@dataclass
class EvidenceRef:
    chunk_id: int

    citation: Citation

    requirement_ids: tuple[str, ...]

    source_role: str
    temporal_status: str

    retrieval_score: float
    retrieval_rank: int

    truncated: bool

    content: str | None
```

注意：

`content` 可以延迟读取。

未来可以：

```text
chunk_id
→ ChunkStore
→ materialize
```

而不是 EvidencePackage 永远保存大段字符串。

---

# 14. EvidenceWorkspace

建议结构：

```python
@dataclass
class EvidenceWorkspace:
    original_query: str

    evidence_by_id: dict[int, EvidenceRef]

    requirement_index:
        dict[str, tuple[int, ...]]

    unresolved_requirements:
        tuple[str, ...]

    coverage:
        tuple[RequirementCoverage, ...]
```

核心 API：

```python
workspace.for_requirement(requirement_id)

workspace.for_requirements(ids)

workspace.by_citation(labels)

workspace.materialize(ids)

workspace.metadata_view()

workspace.stats()
```

**EvidenceWorkspace 不设置 28K 字符上限。**

它只是：

> 当前调查找到过的证据索引。

---

# 15. ContextView

真正送给模型的是：

```python
PromptContextView
```

例如：

```python
@dataclass
class PromptContextView:
    items: tuple[ContextItem, ...]

    rendered_text: str

    estimated_tokens: int

    budget_tokens: int

    omitted_evidence_ids: tuple[int, ...]
```

---

# 16. 不同阶段使用不同 View

## Planner View

Explanation Planner 不需要所有源码正文。

主要需要：

```text
Requirement
Coverage
Citation
source_role
symbol
file
heading
少量关键 excerpt
```

例如控制在：

```text
20K–50K tokens
```

但它是动态 Token budget，不是字符硬编码。

---

## Section View

例如章节：

```text
为什么余票桶放在锁之前
```

只加载：

```text
takeToken 调用点
锁代码
相关设计证据
```

不需要：

```text
订单支付
用户注册
其它无关 Chunk
```

因此每一节可以拥有自己的 Context。

---

## Reviewer View

Reviewer 不需要重新阅读所有 Retrieval Context。

输入：

```text
最终 Draft
+
Claim → Citation 映射
+
发生问题章节的相关 Evidence
```

如果发现：

```text
Section 4
UNSUPPORTED_CLAIM
```

再加载 Section 4 的 Evidence。

---

# 17. Context 预算从 chars 改为 tokens

新增：

```python
@dataclass
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
```

配置优先级：

```text
模型 API metadata
→ config override
→ conservative fallback
```

新增：

```python
TokenBudgetPolicy
```

例如计算：

```text
system prompt
+
question
+
plan
+
context
+
预留 output
+
安全余量
<= context window
```

不能继续：

```python
if chars > 28000:
    truncate
```

---

# 18. Context 超长时的处理顺序

不得简单截掉尾部。

执行：

```text
① 去除重复 Evidence

② 对同 Symbol 的相邻 Chunk 合并

③ CORE Evidence 优先

④ Section relevance 排序

⑤ 保留 Citation + metadata

⑥ 超大正文生成 EvidenceDigest

⑦ Raw Evidence 仍保留在 Workspace

⑧ 需要时重新 materialize
```

---

# 19. EvidenceDigest

可以新增：

```python
@dataclass
class EvidenceDigest:
    evidence_id: int

    summary: str

    preserved_facts: tuple[str, ...]

    citation: Citation
```

必须注意：

> Digest 是 Prompt 压缩结果，不是新的项目事实来源。

它必须始终链接回：

```text
raw evidence
```

---

# 20. Phase 2：实现真正的 ExplanationPlan

建议新建：

```text
src/devcontext/explanation/
├── __init__.py
├── models.py
├── planner.py
└── prompts.py
```

暂时保留：

```text
answer/planner.py
```

作为 compatibility adapter。

不要一次删除旧 AnswerPlanner。

---

# 21. ExplanationPlan 数据模型

建议至少包含：

```python
@dataclass
class ExplanationPlan:

    answer_goal: str

    direct_answer: str

    audience_model: str

    core_mental_model: str

    primary_strategy: str

    secondary_strategies: tuple[str, ...]

    prerequisite_concepts: tuple[str, ...]

    likely_misconceptions: tuple[str, ...]

    sections: tuple[ExplanationSection, ...]

    unresolved_gaps: tuple[str, ...]

    conflicts: tuple[EvidenceConflict, ...]
```

---

# 22. ExplanationSection

```python
@dataclass
class ExplanationSection:

    id: str

    title: str

    section_type: str

    teaching_goal: str

    depends_on: tuple[str, ...]

    key_points: tuple[str, ...]

    claim_plans: tuple[ClaimPlan, ...]

    evidence_labels: tuple[str, ...]

    teaching_devices: tuple[str, ...]

    target_tokens: int | None

    evidence_state: str
```

---

# 23. Section Type

限制 enum：

```text
DIRECT_ANSWER

PROBLEM_SETUP

MENTAL_MODEL

CONCEPT

EXECUTION_FLOW

MECHANISM

CAUSE

DESIGN_REASON

COMPARISON

FAILURE_SCENARIO

TRADEOFF

BOUNDARY

MISCONCEPTION

SUMMARY
```

不要让模型无限生成自由文本类型。

---

# 24. Teaching Device

```text
NONE

HYPOTHETICAL_EXAMPLE

COUNTER_EXAMPLE

A_B_REQUEST_TRACE

ANALOGY

SMALL_FLOW_DIAGRAM

TABLE

PSEUDOCODE
```

重要：

> Planner 只是“允许使用”，不是每一节都必须使用。

否则会重新变成机械模板。

---

# 25. ClaimPlan

这是本轮另一个非常重要的结构。

```python
@dataclass
class ClaimPlan:

    claim_goal: str

    claim_type: str

    evidence_labels: tuple[str, ...]

    confidence: str
```

`claim_type`：

```text
PROJECT_FACT
PROJECT_INFERENCE
GENERAL_CONCEPT
ILLUSTRATIVE_EXAMPLE
```

`confidence`：

```text
CONFIRMED
PARTIAL
UNVERIFIED
```

---

# 26. 为什么需要 Section Confidence

当前有一个实际问题：

CLI 可能显示：

```text
Sufficiency: insufficient
```

但正文大部分仍然用高度确定的语气解释。

最终才写：

```text
证据缺口
```



应该改变为：

```text
Redis Hash 结构          CONFIRMED

takeToken 调用位置       CONFIRMED

Lua 内部具体两阶段算法    UNVERIFIED

取消后归还逻辑           PARTIAL
```

然后 Generator 自动控制措辞。

---

# 27. Explanation Planner Prompt 核心规则

Planner 的职责不是：

> 生成目录。

而应该是：

> 设计理解路径。

System Prompt 至少明确：

```text
1. 不写最终回答。

2. 首先判断：
   用户最终想建立什么心智模型。

3. 调查顺序 != 教学顺序。

4. 不得把 Evidence Requirement 一一转换成章节。

5. 对复杂设计问题：
   优先考虑
   “问题 → 核心模型 → 实现 → why → failure → tradeoff → boundary”。

6. 只有真正有助理解时才设计 Example。

7. Example 不得被描述为真实项目数据。

8. 项目事实必须绑定 Evidence。

9. GENERAL_CONCEPT 可以用于解释技术原理，
   但不得反过来推断项目一定进行了某实现。

10. UNVERIFIED 内容不得进入确定性结论。
```

---

# 28. 余票桶问题的期望 ExplanationPlan

不能写死这个结果，但可以用它作为测试 Fixture。

期望类似：

```text
Answer Goal:
让读者理解余票桶为什么存在，
以及它为什么只是准入层而不是真实库存。

Core Mental Model:
Redis Token = 进入真实库存竞争的资格
MySQL Seat = 最终库存事实

Section 1:
为什么需要余票桶
type = PROBLEM_SETUP
device = HYPOTHETICAL_EXAMPLE

Section 2:
先建立最重要的两层库存模型
type = MENTAL_MODEL

Section 3:
请求真实经过什么路径
type = EXECUTION_FLOW

Section 4:
Hash + Lua 如何完成原子准入
type = MECHANISM

Section 5:
为什么需要懒初始化与 TTL
type = DESIGN_REASON

Section 6:
拿到 Token 但购票失败怎么办
type = FAILURE_SCENARIO

Section 7:
为什么“宁可偏松，不可偏紧”
type = TRADEOFF

Section 8:
最终到底是谁防止超卖
type = BOUNDARY

Summary:
余票桶负责少排人；
MySQL 负责把票卖对。
```

这就是“教学规划”和“检索规划”的区别。

---

# 29. Phase 3：修改 AnswerGenerator

现在：

```text
一个 Context
+
一个 AnswerPlan
+
一次 LLM
=
完整 detailed answer
```

这会成为下一阶段的瓶颈。

改成两种生成模式。

---

# 30. Fast Path

适用于：

```text
brief
简单 locate
简单 negative
简单 standard
```

执行：

```text
ExplanationPlan
+
Relevant Context View
↓
一次生成
```

保持低延迟。

---

# 31. Deep Path

适用于：

```text
复杂 detailed
deep
多章节
多 Evidence Requirement
```

执行：

```text
ExplanationPlan

     ↓

Section 1
+ Section Context 1
→ DraftSection1

Section 2
+ Section Context 2
→ DraftSection2

Section 3
+ Section Context 3
→ DraftSection3

...

     ↓

Global Composer

     ↓

Final Draft
```

---

# 32. DraftSection

```python
@dataclass
class DraftSection:

    section_id: str

    title: str

    text_with_citations: str

    used_citations: tuple[str, ...]

    invalid_citations: tuple[str, ...]

    evidence_state: str
```

---

# 33. Section Scoped Citation Validation

这是多阶段生成的一个非常大的优势。

假设 Section 3 只允许：

```text
C4
C7
C9
```

生成后：

```text
[C4][C7]
```

合法。

如果模型用了：

```text
[C15]
```

即使 `C15` 存在于整个 EvidenceWorkspace 中：

> 也不允许。

因为它没有绑定到当前 Section。

这样 Citation 从：

```text
“答案里引用了一个存在的 Chunk”
```

升级成：

```text
“这个章节只能使用 Planner 指定的证据”
```

Grounding 会更强。

---

# 34. Global Composer

Global Composer 输入：

```text
Question

ExplanationPlan

Section Drafts
```

原则：

```text
允许：
调整顺序
增加过渡
删除重复
统一术语
改善开头与结尾

禁止：
新增项目事实
新增 Citation
新增未出现的 Symbol
```

Composer 的作用只是：

> 让多次生成看起来仍然像一篇完整回答。

---

# 35. 不再使用固定 2200–5000 字符限制

当前：

```python
DEPTH_LIMITS = {
    "brief": ...,
    "standard": ...,
    "detailed": (... 2200, 5000)
}
```

这个设计必须调整。

`answer_depth` 应表达：

> 信息展开程度。

不应该表达：

> 强制字符区间。

建议：

```text
brief
standard
detailed
deep
```

并允许：

```text
auto
```

---

# 36. 新 OutputBudgetPolicy

例如：

```python
@dataclass
class OutputBudget:

    max_output_tokens: int

    preferred_sections: int

    allow_multi_pass: bool
```

最终预算由：

```text
question complexity
+
ExplanationPlan
+
user depth
+
model capabilities
```

共同决定。

不是固定：

```text
detailed = 5000 字。
```

---

# 37. `--depth` 语义

建议：

### brief

```text
直接回答
+
必要依据
```

### standard

```text
回答
+
主流程
+
关键 why
```

### detailed

```text
完整心智模型
+
机制
+
原因
+
failure
+
tradeoff
```

### deep

```text
尽可能系统解释，
允许多阶段生成，
不受 5000 中文字符限制。
```

---

# 38. CLI 新参数

修改：

```text
src/devcontext/request.py
src/devcontext/cli.py
```

支持：

```text
--depth brief
--depth standard
--depth detailed
--depth deep
```

建议增加：

```text
--max-output-tokens
```

作为高级覆盖参数。

但默认用户不需要手工控制。

---

# 39. 新 Answer Mode

不要立即替换当前：

```text
explain
```

新增：

```text
teach
```

所以兼容期：

```python
ANSWER_MODES = (
    "legacy",
    "explain",
    "teach",
)
```

意义：

```text
legacy
    老链路

explain
    当前 V2

teach
    本轮 Answer Quality V3
```

这样才能真正 A/B/C。

质量达标后再：

```text
teach → default
```

---

# 40. Phase 4：System Prompt V3

必须改变当前过于严格的知识边界。

新 Prompt 核心：

```text
你必须严格区分：

A. 当前项目事实
必须由 Evidence 支持。

B. 基于项目事实的推导
必须明确呈现为解释或推论。

C. 通用技术知识
可以用于帮助理解，
但不得冒充当前项目实现。

D. 假设案例
允许用于教学，
但必须明确表示“假设”。
```

---

# 41. 教学型写作规则

不要使用固定：

```text
结论
依据
实现
为什么
取舍
```

作为每节模板。

应该根据 Section Type 使用不同方式。

### PROBLEM_SETUP

优先：

```text
先制造问题
→ 再解释为什么需要方案
```

### EXECUTION_FLOW

优先：

```text
时序
→ 每一步职责
→ 下一步为什么发生
```

### MENTAL_MODEL

优先：

```text
先给一句核心模型
→ 再拆开概念
```

### FAILURE_SCENARIO

优先：

```text
正常状态
→ 故障发生
→ 状态如何变化
→ 系统如何恢复
```

### TRADEOFF

优先：

```text
方案 A
方案 B
为什么选择当前方案
代价是什么
```

---

# 42. Progressive Disclosure

详细回答要遵循：

```text
第一层：一句话

第二层：核心模型

第三层：完整流程

第四层：关键实现

第五层：为什么

第六层：失败情况

第七层：边界与取舍
```

而不是第一句话：

```text
TicketAvailabilityTokenBucket 是 Spring @Component...
```

---

# 43. Phase 5：Reviewer V2

当前 Reviewer 主要检查：

```text
UNSUPPORTED_CLAIM
SOURCE_CONFLICT
MISSING_CORE_POINT
REPETITION
MECHANICAL_STRUCTURE
POOR_ORDER
OVERCLAIM
LENGTH_VIOLATION
```

需要扩展。

---

# 44. 新 Reviewer Issue Types

增加：

```text
MISSING_MENTAL_MODEL

MISSING_WHY

POOR_SCAFFOLDING

MISLABELED_EXAMPLE

FACT_INFERENCE_CONFUSION

GENERAL_KNOWLEDGE_AS_PROJECT_FACT

SECTION_EVIDENCE_MISMATCH

UNHELPFUL_DETAIL

ABRUPT_TRANSITION
```

`LENGTH_VIOLATION` 不再以固定：

```text
2200–5000
```

判断。

改为：

```text
严重过短
明显重复
超出 OutputBudget
```

---

# 45. 不建议 Reviewer 每次重写全文

当前：

```text
Reviewer
→ final_answer_with_citations
```

容易让 Reviewer：

> 把 Generator 已经写好的好内容重新写坏。

建议 Reviewer 首先输出：

```json
{
  "accepted": false,

  "global_issues": [],

  "section_issues": [
    {
      "section_id": "S4",
      "issues": [...]
    }
  ],

  "revision_required": ["S4"]
}
```

然后：

```text
只重生成 S4
```

最后再次 Composer。

最多：

```text
1 次 targeted revision
```

不进入无限循环。

---

# 46. Phase 6：L2 Answer Quality V3

这是本轮绝对不能省略的部分。

现在 L2 已经很好地拥有：

```text
must_cover
must_not_claim
required_evidence
expected_explanation_shape
known_conflicts
```

保留全部。

新增教学指标。

---

# 47. benchmark schema 扩展

例如：

```json
{
  "id": "teach-token-bucket-01",

  "question":
    "详细解释项目的余票桶是如何设计的",

  "answer_depth":
    "detailed",

  "core_mental_model": [
    "Redis token bucket 是准入层",
    "MySQL seat 是最终库存事实源"
  ],

  "must_explain_why": [
    "为什么在锁之前取 token",
    "为什么 Redis 异常可以降级放行",
    "为什么失败后需要归还 token"
  ],

  "useful_scenarios": [
    "高并发请求超过实际库存",
    "取 token 后数据库事务失败"
  ],

  "misconceptions": [
    "Redis token 等于真实库存",
    "只靠 token bucket 就可以防止超卖"
  ],

  "pedagogy_expectations": [
    "先建立问题再介绍方案",
    "逐层展开",
    "区分项目事实与假设案例"
  ]
}
```

---

# 48. L2 新评测维度

Pairwise Judge 除现有维度外检查：

```text
Factual Correctness

Evidence Support

Directness

Mental Model Clarity

Why Depth

Progressive Disclosure

Example Usefulness

Failure Scenario Coverage

Tradeoff Explanation

Boundary Clarity

Coherence

Redundancy

Pedagogical Value
```

---

# 49. 不要把“更长”当成“更好”

当前 GPT 参考回答的重要价值不是：

> 写了很多字。

而是它建立了很多因果关系。

比如：

```text
为什么需要余票桶

为什么放在锁前

为什么又不能放太前

为什么多个席别必须原子扣

为什么初始化需要锁

为什么售罄必须写 0

为什么要 TTL

为什么偏松比偏紧安全
```



真正要评估的是：

```text
Why Coverage
```

不是：

```text
字数。
```

---

# 50. 新增 Pedagogy Judge

建议：

```text
src/devcontext/evaluation/
    pedagogy.py
```

输出：

```python
PedagogyScore(
    mental_model,
    causal_explanation,
    progressive_disclosure,
    examples,
    failure_reasoning,
    tradeoffs,
    readability,
)
```

分数只是内部评测。

---

# 51. Golden Answer 不建议直接复制 GPT

用户提供的 GPT 回答可作为：

> 质量参考。

不要直接作为模型输出模板。

应该把其中的优点抽象成：

```text
problem-first

mental model

progressive detail

why chain

hypothetical scenario

failure reasoning

tradeoff

boundary

summary
```

这样系统不会只在“余票桶”问题上表现好。

---

# 52. 回归要求

本次 Answer 重构：

**原则上不得修改：**

```text
keyword scorer
vector scorer
RRF
RetrievalPolicy
Evidence Requirement semantics
Coverage 基本定义
```

必须重新运行：

```text
pytest

L1

L1.5
```

确保 Answer 层修改没有反向污染 Retrieval。

---

# 53. 必须新增的单元测试

## Explanation Planner

```text
test_plan_has_core_mental_model

test_plan_does_not_copy_requirements_to_sections

test_plan_distinguishes_fact_and_example

test_plan_uses_failure_scenario_for_edge_question

test_locate_question_does_not_overplan

test_negative_question_corrects_false_premise
```

---

# 54. Context Workspace

```text
test_workspace_keeps_evidence_beyond_prompt_budget

test_section_view_only_materializes_relevant_evidence

test_large_evidence_does_not_disappear_after_planning

test_digest_preserves_original_evidence_reference

test_token_budget_not_char_budget
```

---

# 55. 重要的大 Context 测试

构造：

```text
100K+ chars
```

甚至更大的 synthetic evidence。

验证：

```text
Workspace 保存全部 Evidence refs

Planner View 正常构建

Section 1 可以看到前部 Evidence

Section 8 可以看到原本会被 28K 截掉的尾部 Evidence

最终回答成功生成
```

这是本轮“解除上下文瓶颈”的关键验收测试。

---

# 56. Output 测试

```text
test_deep_answer_can_exceed_old_5000_char_limit

test_detailed_answer_not_forced_to_hit_minimum_length

test_brief_locate_answer_stays_brief

test_multi_pass_sections_preserve_citations

test_composer_does_not_introduce_new_citations
```

---

# 57. Grounding 测试

```text
test_project_fact_requires_evidence

test_general_concept_does_not_require_project_citation

test_hypothetical_example_is_labeled

test_unverified_claim_not_written_as_fact

test_section_cannot_use_unbound_citation
```

---

# 58. Reviewer 测试

```text
test_reviewer_detects_missing_mental_model

test_reviewer_detects_project_fact_without_support

test_reviewer_detects_general_knowledge_as_project_fact

test_reviewer_targets_only_bad_section

test_review_does_not_rewrite_accepted_sections
```

---

# 59. Trace 也要升级

现有：

```text
stage_usage
answer_plan
review
```

很好。

新增：

```text
explanation_plan

context_views

section_drafts

section_citations

section_confidence

compression_events

revision_trace
```

`--debug` 可以显示：

```text
Explanation Strategy:
PROBLEM_SOLUTION + FAILURE_ANALYSIS

Mental Model:
Redis = admission
MySQL = truth

Context:
Workspace evidence: 23
Planner view: 9
S1 view: 4
S2 view: 3
...

Generation:
8 sections
1 targeted revision
```

这对后续面试展示也非常重要。

---

# 60. 推荐代码目录最终形态

```text
src/devcontext/

├── agentic/
│   ├── evidence_workflow.py
│   └── ...
│
├── context/
│   ├── builder.py             # V1 / legacy
│   ├── workspace.py           # new
│   ├── views.py               # new
│   ├── budget.py              # new
│   └── digest.py              # optional
│
├── explanation/
│   ├── __init__.py
│   ├── models.py
│   ├── planner.py
│   └── prompts.py
│
├── answer/
│   ├── models.py
│   ├── generator.py
│   ├── composer.py            # new
│   ├── reviewer.py
│   └── planner.py             # compatibility wrapper
│
└── evaluation/
    ├── answer_quality.py
    ├── answer_quality_runner.py
    └── pedagogy.py
```

---

# 61. EvidenceDrivenWorkflow 修改

当前：

```text
retrieve
↓
EvidencePackage
↓
_answer()
↓
AnswerPlanner
↓
Generator
↓
Reviewer
```

修改后：

```text
retrieve
↓
EvidencePackage
↓
EvidenceWorkspace
↓
ExplanationPlanner
↓
ExplanationPlan
↓
ContextViewBuilder
↓
SectionGenerator / FastGenerator
↓
Composer
↓
TeachingReviewer
↓
targeted revision?
↓
finalize
```

Legacy 与 Explain V2 保留。

---

# 62. 推荐新增 Workflow

不要继续让：

```text
EvidenceDrivenWorkflow._answer_explain()
```

膨胀成几百行。

建议：

```text
src/devcontext/explanation/workflow.py
```

实现：

```python
class TeachingExplanationWorkflow:

    def run(
        request,
        evidence_package,
    ) -> TeachingAnswerResult:
        ...
```

`EvidenceDrivenWorkflow` 只负责选择：

```text
legacy
explain
teach
```

---

# 63. LLM 调用预算

不要无限增加 LLM 调用。

## brief

建议：

```text
Explanation Plan
+
Generation

约 2 calls
```

---

## standard

```text
Explanation Plan
+
Generation
+
conditional review

2–3 calls
```

---

## detailed

```text
Explanation Plan
+
3–8 section drafts
+
Composer
+
Reviewer

约 6–11 calls
```

后续 Section Draft 可以并行。

---

## deep

允许更多章节。

但必须：

```text
MAX_SECTIONS
MAX_OUTPUT_TOKENS
MAX_REVISION_ROUNDS = 1
```

防止失控。

---

# 64. Context 大不是越大越好

虽然 DeepSeek 当前拥有 1M Context，不能因此写：

```text
直接把整个仓库塞进去。
```

大 Context 会增加：

```text
费用
延迟
无关信息
模型注意力稀释
```

正确目标：

> 系统不因为小固定窗口丢失能力，但仍尽量只把当前推理所需 Evidence 放进 Prompt。

也就是：

```text
可以看很多
≠
每次必须看很多。
```

---

# 65. 开源项目借鉴边界

## 从 Aider 借

```text
动态 token budget

重要信息优先

全局信息与局部详细信息分层

Context 是动态工作集
```


---

## 从 OpenHands 借

```text
历史/上下文 condensation

Raw information 与 condensed view 分离

Context overflow 是可恢复事件
```


---

## 从 Continue 借

```text
按需探索

read-only investigation

repo map / file / grep 是不同 Context 获取能力
```


---

## 本轮明确不借

暂时不要做：

```text
完整 Tool Agent
LangGraph
Symbol Graph
Repo Map
Web UI
Multi Agent
```

这些应该在 Answer Quality V3 验收后进入下一阶段。

否则本轮范围会再次失控。

---

# 66. 实施顺序

建议拆成四个 Commit。

## Commit 1

```text
refactor: add evidence workspace and token-aware context views
```

完成：

```text
EvidenceWorkspace

PromptContextView

TokenBudgetPolicy

legacy ContextBuilder compatibility

large-context tests
```

---

## Commit 2

```text
feat: add teaching explanation planner
```

完成：

```text
ExplanationPlan

ExplanationSection

ClaimPlan

Mental Model

Teaching Strategy

Section Confidence

teach mode
```

---

## Commit 3

```text
feat: add section-scoped grounded answer generation
```

完成：

```text
Fast Path

Deep Path

Section Context

DraftSection

Citation allowlist

Global Composer

deep answer mode
```

---

## Commit 4

```text
feat: add pedagogical review and answer-quality v3 evaluation
```

完成：

```text
Reviewer V2

Targeted Revision

Pedagogy Judge

L2 V3

Golden Case

A/B/C evaluation
```

---

# 67. 开发 Agent 每个 Commit 都必须执行

```text
pytest

相关新单元测试

至少一个真实 CLI 问题
```

不要等所有代码完成后才测试。

---

# 68. 最终验收 Golden Case：余票桶

执行：

```powershell
uv run devcontext ask `
  "详细解释项目的余票桶是如何设计的" `
  --answer-mode teach `
  --depth detailed `
  --debug
```

答案至少应自然完成以下认知路径：

```text
1. 为什么存在这个问题

2. 余票桶到底是什么

3. Redis Token 和真实 DB 库存的区别

4. 它位于请求链路哪里

5. 为什么放在锁之前

6. Redis Hash 为什么这样组织

7. 为什么多席别操作需要原子性

8. 初始化为什么需要懒加载与并发保护

9. TTL 为什么是自愈手段

10. 取 Token 之后数据库失败怎么办

11. Redis 失败怎么办

12. 最终防止超卖的边界到底在哪里

13. 当前证据不能确认哪些细节
```

不要求恰好 13 个章节。

事实上最好不要 13 个章节。

它们应该根据认知关系合并成大约：

```text
5–8 个自然章节。
```

---

# 69. Golden Case 的质量目标

当前 DevContext 的优点必须全部保留：

```text
代码事实可靠

Citation 可追溯

历史/当前来源区分

Evidence gap 不猜测
```

同时增加 GPT 参考答案的优点：

```text
问题驱动

心智模型

从浅入深

大量 why

合理假设案例

失败推演

对比

设计取舍

最后收束
```

GPT 参考答案最值得学习的不是某个句子，而是这种解释路径。比如它先区分“真实座位”和“号码牌”，再说明拿到号码牌并不代表一定坐上座位，这比直接讲 Redis Hash 更容易建立模型。

---

# 70. Release Gate

在把 `teach` 切成默认模式前，必须满足：

### Grounding

```text
invalid Citation = 0

不得新增已知 unsupported project claim

历史文档不得覆盖当前代码
```

### Retrieval

```text
L1 无明显回退

L1.5 False READY 继续保持 0
```

### Answer

与当前 `explain` 进行 blind A/B。

继续复用已有：

```text
swapped-order judge
```

避免位置偏差。

目标：

```text
Teaching V3 在复杂 detailed 问题上明显优于 V2。
```

当前代码已有：

```text
V2 blind win rate >= 70%
```

的发布思想。

本轮可以沿用：

```text
V3 vs V2 decisive cases win rate >= 70%
```

但不要只看这一项。

同时必须人工检查：

```text
余票桶

购票一致性

双请求抢同一座位

订单失败补偿

一个 locate 问题

一个 negative 问题
```

---

# 71. 最重要的非功能验收

本轮完成后，系统必须可以证明：

### 以前

```text
Context 最大约 28K chars

所有章节共享同一个 Context

Detailed 最多约 5000 中文字符
```

### 现在

```text
Evidence Workspace 可以拥有远大于单 Prompt 的证据集合

每个阶段按需构造 Context View

每个 Section 可以加载自己的 Evidence

Context 使用 token-aware budget

Detailed/Deep 输出不再受 5000 字符硬限制

模型 Context Window 只作为资源边界，
不再直接变成产品能力边界
```

---

# 72. 本轮明确禁止的错误实现

## 错误方案 1

```text
max_chars:
28000 → 100000
```

不算完成 Context V2。

---

## 错误方案 2

新增：

```text
ExplanationPlanner
```

但是输出仍然：

```text
title
purpose
evidence_labels
```

只是换了名字。

不算完成。

---

## 错误方案 3

只改 Prompt：

```text
“请像老师一样详细解释。”
```

不算完成。

---

## 错误方案 4

解除字数限制后：

```text
每个问题输出一万字。
```

不算完成。

目标是：

> 内容量由问题复杂度决定。

---

## 错误方案 5

为了让答案更丰富允许模型自由补项目事实。

绝对禁止。

应该：

```text
放宽通用教学知识
```

而不是：

```text
放宽项目事实 Grounding。
```

---

# 73. 本轮完成后 DevContext 的链路应该变成

```text
User Question

      ↓

Evidence Planner

      ↓

SearchAction Planner

      ↓

Retrieval Controller

      ↓

Evidence Workspace
“我找到了哪些事实”

      ↓

Explanation Planner
“用户需要建立什么理解”

      ↓

Explanation Plan

      ↓

Section Context Views
“这一部分具体需要哪些事实”

      ↓

Grounded Section Generation

      ↓

Composer
“把几个解释部分组织成一篇文章”

      ↓

Teaching Reviewer
“它是否既正确，又真的解释清楚了”

      ↓

Final Answer
```

这里非常重要的职责边界是：

```text
Retrieval：
回答“事实是什么”。

Explanation Planner：
回答“这些事实应该如何被理解”。

Generator：
回答“怎样把这个理解表达出来”。

Reviewer：
回答“表达是否正确且真正有帮助”。
```

---

# 74. 本轮结束后的下一阶段

只有本轮达到 Release Gate 后，再进入：

```text
Symbol Graph
+
Repo Map
+
Tool-driven Agent
```

因为这时 DevContext 会拥有两个稳固基础：

```text
下层：
证据检索正确

上层：
复杂问题解释清楚
```

再继续增加：

```text
CALLS
REFERENCES
IMPLEMENTS
EXTENDS
```

等 Code Intelligence 能力时，新能力能够真正转化为高质量答案。

否则即使现在建立完整 Call Graph：

> 系统依然可能只是把更多正确事实用“源码说明书”的方式输出。

---

# 75. 本轮项目价值的最终目标

本轮完成后，DevContext 不应再只是：

> “能够基于项目代码正确回答问题。”

而应达到：

> **能够先通过证据系统确认当前项目真实实现，再根据问题性质建立适合开发者理解的心智模型；在严格区分项目事实、项目推论、通用技术知识和假设案例的前提下，从问题背景、执行流程、设计原因、失败场景、权衡和正确性边界多个层次解释复杂工程问题。对于大型证据集合，系统不再依赖单个固定大小 Context，而是通过 Evidence Workspace、动态 Token Budget 和 Section-scoped Context 按需组织信息。**

这才是本轮开发的完成定义。