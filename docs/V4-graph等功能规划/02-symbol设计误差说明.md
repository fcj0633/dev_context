我检查了当前最新 `main`。结论先说：

> **图片中选择 1：完整 V1 闭环（Recommended）。**
>
> 但要按当前最新代码对我们之前的 Symbol Graph 方案做几处修正。核心方案没有过时，真正变化的是**检索策略和回答链路**，不是 JavaParser / Chunk / Graph 基础层。

当前 `main` 已经从我们设计方案时的 `fa73211` 前进到 **`346a047`**，中间多了 14 个提交，最新主要是 Fast / Full 回答链路合并。[查看最新合并提交 346a047](https://github.com/fcj0633/dev_context/commit/346a04764c0024d80210b2ba558499411e9aa9f5?utm_source=chatgpt.com)

---

# 一、先说最重要的判断：原 Symbol Graph 方案大约 80% 仍然成立

这次最新代码并没有提前实现真正的 Symbol Graph。

当前仍然是：

```text
Java
 ↓
javaparser-core
 ↓
CLASS / INTERFACE / METHOD / CONSTRUCTOR
 ↓
Chunk
 ↓
Embedding / Keyword
 ↓
PostgreSQL
```

仍然**没有**：

```text
javaparser-symbol-solver-core

code_symbol

code_symbol_edge

CALLS

IMPLEMENTS

OVERRIDES

CONSTRUCTS

Graph Expansion
```

README 当前也仍然明确写着：

```text
当前版本不包含 SymbolSolver、调用图……
```

所以我们之前判断：

> “当前系统已经很会找文本，但缺少程序关系层。”

仍然完全成立。

真正变化的是上层：

```text
旧：
teach / explain / multi-pass / single-stream

现在：
Fast
Full
+
旧路径兼容
```

所以 Symbol Graph 的底层价值反而没变。

---

# 二、最大的误差 1：我之前按“CODE = Hybrid Retrieval”设计，现在已经不是了

这是当前实现相对于我们之前计划**最重要的变化**。

最新代码增加了：

[`src/devcontext/retrieval/symbols.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/retrieval/symbols.py)

但注意：

> **这个 `symbols.py` 不是 Symbol Graph。**

它只是：

```text
检测 query / workspace 中
有没有“像 Java Symbol 的字符串”
```

比如：

```text
PurchaseTicketTxService
loadBucket
@Transactional
UserReuseUtil.java
```

然后当前 `RetrievalController` 会做：

```text
没有 symbol
    ↓
vector search

已有明确 symbol
    ↓
keyword search
```

所以现在 Evidence Controller 的 CODE 检索已经是：

```text
Semantic Question
    ↓
Vector

发现 Class / Method
    ↓
Keyword
```

而不是我们之前假设的：

```text
CODE
 ↓
Hybrid
```

这次 stage-aware retrieval 的实验也已经合入了 RetrievalController。

因此 Symbol Graph V1 不能再设计成：

```text
Hybrid Retrieval
     ↓
Graph Expansion
```

应该修改成：

```text
Current Retrieval Strategy
(Vector / Keyword / Hybrid)
          ↓
      Code Anchors
          ↓
   Exact Symbol Mapping
          ↓
    Symbol Graph
          ↓
   Graph Expansion
```

也就是：

> **Graph 接在现有检索策略之后，不替换现有 stage-aware 策略。**

这个修改很重要。

---

# 三、最大的误差 2：当前已经有一个“symbol”概念，但它和我们要做的 Symbol 完全不是一回事

现在：

```python
symbols_in_text(...)
workspace_symbols(...)
detected_symbols(...)
```

里面的 `symbol` 本质上是：

```text
“像类名 / 方法名的字符串”
```

例如：

```text
PayServiceImpl
notifyPayResult
```

而我们 Symbol Graph 中的 Symbol 应该是：

```text
有唯一身份的程序实体
```

例如：

```text
M:com.xxx.PayServiceImpl#notifyPayResult(java.lang.String)
```

两者不能混。

所以我建议调整之前的模块命名。

不要再建立：

```text
devcontext/symbol/
```

或者：

```text
retrieval/symbol_graph.py
```

容易和现在的：

```text
retrieval/symbols.py
```

混淆。

我更建议直接建：

```text
src/devcontext/code_graph/
```

例如：

```text
code_graph/
├── models.py
├── store.py
├── query.py
├── expansion.py
├── intent.py
└── trace.py
```

概念上明确区分：

```text
retrieval.symbols
= Symbol-shaped text detection
= 检索策略辅助

code_graph.CodeSymbol
= 真正的程序实体

code_graph.SymbolEdge
= 真正的程序关系
```

这个命名修改是必要的。

---

# 四、最大的误差 3：回答层现在已经重构为 Fast / Full，所以 Symbol Graph 更不应该碰回答层

最新 main 已经形成：

```text
User
 ↓
RequestPolicy
 ↓
RetrievalController
 ↓
EvidencePackage
 ↓
        ┌── Fast
        │
        └── Full
```

Fast / Full 都共享前面的：

```text
EvidencePlanner
SearchAction
RetrievalController
EvidenceWorkspace
EvidencePackage
```

这对 Symbol Graph 其实是好消息。

我们之前还在讨论：

```text
teach
explain
single_stream
```

需要不要分别适配。

现在可以进一步收敛：

> **Symbol Graph 完全做到 EvidencePackage 之前结束。**

也就是：

```text
Symbol Graph
    ↓
找到更多 CODE Chunk
    ↓
EvidenceWorkspace
    ↓
后面完全不知道 Graph 存在
```

Fast 不用改。

Full 不用改。

V3 Planner 不用改。

Writer 不用改。

Reviewer/旧路径也不用改。

这是当前架构下最正确的切入点。

---

# 五、最大的误差 4：RetrievalController 现在有 Deadline，Graph Expansion 必须尊重它

最新 `RetrievalController` 已经出现：

```python
remaining_seconds()
```

以及：

```text
V3_TEACHING_RESERVE_SECONDS = 120

FOLLOWUP_ESTIMATED_SECONDS = 35
```

意味着现在整个系统开始有：

> **时间预算意识。**

我们之前只是从 Graph 爆炸角度规定：

```text
MAX_HOPS = 2
MAX_ANCHORS = 3
MAX_RESULTS = 8
```

现在还应该再增加：

```text
Graph Expansion 必须是：

deterministic
bounded
no LLM
deadline-aware
```

即：

```text
remaining time 不足
    ↓
直接跳过 Graph Expansion

而不是影响最终回答
```

不过这里不应该设很大的“Graph 专用时间”。

正常 PostgreSQL 图查询应该是：

```text
毫秒级
```

所以比较合理：

```text
Graph 不应该成为新的 latency stage。
```

如果一次 graph expansion 居然：

```text
2s
5s
10s
```

那应该修 Graph 查询，而不是给它更多预算。

---

# 六、我们之前的核心架构，反而应该继续保留

下面这些我检查当前代码后认为都**不用推翻**：

| 原方案 | 当前判断 |
|---|---|
| JavaParser + SymbolSolver | 保留 |
| Repository 两阶段分析 | 保留 |
| CLASS / INTERFACE / METHOD / CONSTRUCTOR | 保留 |
| EXTENDS | 保留 |
| IMPLEMENTS | 保留 |
| CALLS | **核心** |
| CONSTRUCTS | 保留 |
| OVERRIDES | 保留 |
| PostgreSQL 存 Graph | 保留 |
| 不引入 Neo4j | 保留 |
| Graph Node 映射现有 Chunk | **强烈保留** |
| Graph 本身不作为 Citation | **强烈保留** |
| Bounded Graph Expansion | 保留 |
| MAX_HOPS≈2 | 保留 |
| EvidenceWorkspace 作为接入点 | **保留** |
| Feature Flag | **必须保留** |
| A/B Evaluation | **必须保留** |

所以并不存在：

> “最新代码已经让 Symbol Graph 方案失效”。

实际上只是需要把它适配到更新后的 retrieval architecture。

---

# 七、还有一个需要修正的细节：Graph 不应该只处理 `source_scope == CODE`

之前我给出的伪代码比较偏：

```python
if CODE:
    graph.expand(...)
```

现在重新看当前 RetrievalPolicy 后，我建议改掉。

因为 EvidenceRequirement 还有：

```text
CODE
DOCUMENT
BOTH
ANY
```

尤其你的项目核心就是：

```text
CODE + DOCUMENT 混合证据
```

因此正确设计应该是：

```text
CODE
 └── 对 CODE results Graph Expansion

DOCUMENT
 └── 不 Graph

BOTH
 ├── CODE candidates → Graph Expansion
 └── DOCUMENT candidates 原样保留

ANY
 ├── 如果存在 CODE candidates → Graph Expansion
 └── DOCUMENT 保留
```

即：

```python
code_candidates =
    execution.source_candidates.get("CODE", [])

graph_results = graph.expand(code_candidates)

merge(
    execution.results,
    graph_results
)
```

Graph 永远只增强：

```text
CODE side
```

而不会破坏 MIXED 检索。

---

# 八、一个很重要的新发现：当前 `retrieval/symbols.py` 可以复用，但不能承担 Symbol Lookup

现在里面：

```python
symbols_in_text(query)
```

做的只是 pattern match。

比如：

```text
FooBarManager
```

即使项目里根本没有这个类，

它仍然可能被判成：

```text
symbol-shaped
```

因此这个模块可以继续负责：

```text
“Query 是否像包含代码符号”
```

但是不能负责：

```text
“这个 Symbol 在仓库中是否真实存在”
```

Symbol Graph 上线之后可以形成两层：

```text
symbols_in_text()
      ↓
Candidate Symbol Name
      ↓
CodeGraphStore.find_symbol()
      ↓
Real CodeSymbol
```

例如：

```text
notifyPayResult
      ↓
find_symbols("notifyPayResult")
      ↓
M:xxx.PayServiceImpl#notifyPayResult(String)
```

这会比当前只有字符串 shape detection 强非常多。

---

# 九、数据库方案基本不变，但我会调整一个之前的建议

之前我建议：

```text
001_schema.sql
002_symbol_graph.sql
```

从工程长期性上没问题。

但最新代码当前：

```python
ChunkStore.initialize()
```

仍然只加载：

```text
001_schema.sql
```

项目暂时还没有 migration framework。

所以这次 V1 有两个可接受方案。

我更倾向继续：

```text
001_schema.sql
002_symbol_graph.sql
```

但只做一个非常小的 Schema Loader：

```python
for file in sorted(sql/*.sql):
    execute(file)
```

不要引入：

```text
Alembic
Flyway
Liquibase
version table
migration engine
```

Symbol Graph 不应该顺便把项目变成数据库迁移项目。

---

# 十、真正应该改的是 ingestion 的事务闭环

这一点依然非常重要。

现在还是：

```python
replace_repository()

DELETE chunks
INSERT chunks
COMMIT
```

Symbol Graph 不能：

```text
先更新 Chunk
commit

再更新 Graph
commit
```

否则可能：

```text
Chunk = version N

Graph = version N-1
```

所以必须升级成：

```text
replace_repository_snapshot()
```

一次事务：

```text
BEGIN

删除旧 graph edge
删除旧 symbol
删除旧 chunks

插入 chunks

建立 chunk locator → chunk_id

插入 symbols

建立 symbol_key → symbol_id

插入 edges

COMMIT
```

这部分属于：

> **Symbol Graph V1 必须做的工程完整性。**

不能留到以后。

---

# 十一、我现在会调整 Graph 接入点的具体设计

当前：

[`src/devcontext/agentic/retrieval_controller.py`](https://github.com/fcj0633/dev_context/blob/main/src/devcontext/agentic/retrieval_controller.py)

真实流程已经是：

```text
SearchAction
    ↓
code_strategy_for()
    ↓
vector / keyword / existing policy
    ↓
SearchExecution
    ↓
SourcePolicy
    ↓
EvidencePool
    ↓
EvidenceWorkspace
```

正确的新链路：

```text
SearchAction
    ↓
code_strategy_for()
    ↓
Existing Retrieval
    ↓
SearchExecution
    ↓
extract CODE anchors
    ↓
CodeGraphExpander
    ↓
Graph Candidate Chunks
    ↓
deterministic merge
    ↓
SourcePolicy
    ↓
EvidencePool
    ↓
EvidenceWorkspace
```

这个位置比我们之前想的还确定。

---

# 十二、当前 Fast / Full 重构以后，为什么反而更应该选择图片中的方案 1

现在回答图片的问题。

图片有两个方案：

**1. 完整 V1 闭环**

```text
解析
↓
落库
↓
查询命令
↓
证据链路接入
↓
A/B 评测
↓
feature flag 默认关闭
```

**2. 只做图索引与查询**

```text
解析
↓
落库
↓
查询命令

STOP
```

然后以后再接自动检索。

我的选择明确是：

# 选 1：完整 V1 闭环

不是因为“做得越多越好”，而是因为只做到 2，**无法证明 Symbol Graph 对 DevContext 有价值。**

---

# 十三、如果只做方案 2，会出现一个很尴尬的项目状态

你会拥有：

```text
code_symbol

code_symbol_edge

graph callers

graph callees
```

CLI 可以：

```text
查询调用者
查询被调用者
```

但是用户：

```bash
devcontext ask "支付回调完整流程是什么"
```

仍然走：

```text
vector / keyword
↓
Chunk
↓
LLM
```

Symbol Graph 完全没有参与。

那么面试官问：

> Symbol Graph 给你的 RAG 带来了什么提升？

你只能回答：

> “目前已经建好了图，后续准备接检索。”

这个功能从项目角度其实是：

> **半成品基础设施。**

---

# 十四、而方案 1 才能形成完整的技术故事

最终可以说：

```text
问题：
传统向量检索无法稳定恢复跨文件调用链

↓

方案：
JavaParser + SymbolSolver

↓

数据：
CodeSymbol + SymbolEdge

↓

检索：
语义/关键词先找 Anchor

↓

结构扩展：
CALLS / IMPLEMENTS / OVERRIDES

↓

证据：
Graph 结果重新映射成原 CODE Chunk

↓

接入：
EvidenceWorkspace

↓

验证：
baseline vs graph A/B

↓

结果：
Graph Rescue / Call-chain Recall / CORE Coverage
```

这才是真正的：

> **问题 → 设计 → 实现 → 接入 → 验证**

也就是一个完整面试项目亮点。

---

# 十五、方案 1 并不意味着“一次性全部打开”

图片里这一点我非常赞同：

> **默认关闭 Feature Flag。**

建议：

```text
SYMBOL_GRAPH_ENABLED=false
```

上线初期：

```text
Current Retrieval
        │
        ├── false
        │     ↓
        │   baseline
        │
        └── true
              ↓
          + graph expansion
```

因此方案 1 实际是：

```text
完整实现
+
低风险接入
+
默认关闭
+
A/B 验证
```

这比只开发一半更合理。

---

# 十六、我建议把“完整 V1”具体冻结成 6 个阶段

这不是重新设计，而是根据最新 main 修正后的交付边界：

```text
Phase 1
Java Symbol Analysis

javaparser-symbol-solver-core
SourceRootDiscovery
CodeSymbol
SymbolEdge
GraphDiagnostics


Phase 2
Transactional Graph Index

code_symbol
code_symbol_edge
repository snapshot transaction


Phase 3
Graph Query

find_symbol
find_callers
find_callees
find_implementations
find_hierarchy


Phase 4
Retrieval Integration

current vector/keyword retrieval
        ↓
anchors
        ↓
bounded graph expansion
        ↓
EvidenceWorkspace


Phase 5
Observability

GraphExpansionTrace
graph latency
graph nodes
graph added chunks
graph rescue


Phase 6
Evaluation

baseline OFF
vs
graph ON
```

功能开关一直保持：

```text
OFF
```

直到第 6 阶段验证。

---

# 十七、A/B 评测现在比我们生成计划时更加重要

因为最新项目已经有了：

```text
Stage-aware Retrieval
```

而且它本身有实验数据支持。

所以不能上线 Symbol Graph 后看到：

```text
Recall 变高
```

就说：

> Graph 有效果。

需要区分：

```text
Stage-aware Retrieval 的提升

vs

Symbol Graph 新增加的提升
```

所以必须：

```text
同一 main
同一 benchmark
同一检索策略
同一 top-k
同一 context budget

唯一变量：

SYMBOL_GRAPH_ENABLED
```

才能得到：

```text
Graph OFF
vs
Graph ON
```

这是图片方案 1 中“A/B 评测”必须保留的原因。

---

# 十八、建议新增的 Graph 指标

最终不要只输出 Recall。

应该至少有：

```text
symbol_resolution_rate

internal_edge_count

unresolved_call_count

graph_expansion_count

graph_added_chunks

graph_unique_gold_hits

graph_rescue_cases

call_chain_recall

noise_rate

core_requirement_coverage

false_ready
```

尤其我最关注：

```text
GRAPH_RESCUE
```

例如：

```text
Baseline:

PayServiceImpl#payCallback ✅
TicketConsumer ❌


Graph:

PayServiceImpl#payCallback
        ↓ CALLS
...
        ↓
TicketConsumer ✅
```

那么：

```text
TicketConsumer
```

就是 Graph 真正救回来的证据。

---

# 十九、当前最新代码下，我会额外加一个验收条件

因为 Fast 默认延迟目标现在是：

```text
45 秒
```

Full：

```text
300 秒
```

Symbol Graph 本身绝不能显著增加时间。

建议新增：

```text
Graph Expansion P95 < 100ms
```

或者至少：

```text
单 SearchAction Graph Expansion
不得进入秒级
```

否则实现就是有问题。

因为它只是：

```text
2-hop PostgreSQL adjacency lookup
```

不涉及：

```text
Embedding
LLM
```

理论上非常便宜。

---

# 二十、所以最终判断

当前最新代码与我们生成 Symbol Graph 计划时，最大的误差不是：

> “Symbol Graph 方案错了。”

而是：

```text
1. CODE 检索已经从固定 Hybrid
   → stage-aware vector/keyword

2. 已经存在 retrieval/symbols.py
   但只是符号字符串检测

3. Answer 层已经收敛成 Fast / Full

4. RetrievalController 新增 Deadline / time reserve

5. SearchExecution 已经能够记录 strategy / detected symbols
```

因此需要做的是：

> **让 Symbol Graph 适配这些新能力，而不是重新设计整个方案。**

而底层核心判断仍然没变：

```text
没有 SymbolSolver
没有 Symbol Index
没有 Relation Graph
没有 Call Graph
没有 Graph Retrieval
```

---

# 最终选择

**我选择图片中的：**

> **① 完整 V1 闭环（Recommended）**

并且我建议给开发 Agent 的范围明确写成：

> Symbol Graph V1 必须完成 **解析 → 符号/关系抽取 → 原子落库 → 图查询 → RetrievalController 有界图扩展 → EvidenceWorkspace 接入 → Trace → A/B 评测** 的完整闭环；默认 `SYMBOL_GRAPH_ENABLED=false`，不得直接改变现有生产检索结果。实现必须建立在当前 stage-aware CODE retrieval 和 Fast/Full 共用 EvidencePackage 的最新架构上，不允许按旧方案把 CODE 固定改回 Hybrid，也不得修改 Fast/Full Writer、Planner 或回答协议。

这会是我现在认为风险和项目价值最平衡的选择。