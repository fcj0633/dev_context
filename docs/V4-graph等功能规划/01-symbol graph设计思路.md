我重新按当前 `dev_context` 的 **main 分支实现**梳理了一遍。先给结论：

> 当前最适合的 Symbol Graph 方案，不是再造一套“GraphRAG 检索系统”，而是给现有的 **Chunk → Hybrid Retrieval → EvidenceWorkspace → Coverage → Teaching Answer** 补上一层“确定性的代码关系索引”。

也就是说，**Chunk 仍然是最终证据单位，Symbol Graph 主要负责“从已找到的代码继续沿真实程序关系找到下一批 Chunk”**。

这是和你当前架构兼容性最好、改动收益比最高的方案。

---

# 一、先看当前项目到底缺什么

我检查的是当前 `main`，HEAD 为 `fa73211`。

现在 Java ingestion 的真实链路是：

```text
Java 文件
   ↓
JavaSourceParser
   ↓
JavaParser AST
   ↓
ChunkRecord
   ↓
CLASS
INTERFACE
METHOD
CONSTRUCTOR
   ↓
Python JavaParserRunner
   ↓
Chunk
   ↓
Embedding
   ↓
PostgreSQL knowledge_chunk
```

具体对应：

```text
java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java

src/devcontext/ingestion/java_parser_runner.py

src/devcontext/ingestion/pipeline.py

src/devcontext/storage.py
```

当前 JavaParser 依赖只有：

```xml
javaparser-core 3.28.2
```

所以现在能回答：

```text
这个类在哪里？
这个方法在哪里？
哪个 Chunk 与这个问题语义相关？
```

但没有真正解决：

```text
谁调用这个方法？

这个方法调用了谁？

这个接口有哪些实现？

Controller → Service → Mapper 到底怎么连起来？

调用接口方法以后，实际实现类在哪？

一个方法调用链为什么跨了 5 个文件？
```

因为当前：

```text
CLASS / METHOD
```

只是：

> **独立的文本块。**

还没有：

```text
METHOD A
   │
 CALLS
   ▼
METHOD B
```

这样的程序结构。

---

# 二、当前最关键的限制：甚至还没有 SymbolSolver

当前 `pom.xml`：

```xml
javaparser-core
```

而没有：

```xml
javaparser-symbol-solver-core
```

JavaParser 官方把两者区分得很明确：`javaparser-core` 负责 AST，而 SymbolSolver 在 AST 上进一步把使用位置解析到具体声明；官方的 3.28.2 版本正好提供 `javaparser-symbol-solver-core`。[GitHub](https://github.com/javaparser/javaparser?utm_source=chatgpt.com)

例如：

```java
orderService.payCallbackOrder(orderSn);
```

现在 AST 能告诉你：

```text
这是一个 MethodCallExpr
name = payCallbackOrder
scope = orderService
```

但我们真正希望得到：

```text
orderService 的类型
    ↓
OrderRemoteService

payCallbackOrder(String)
    ↓
真正声明：
com.xxx.OrderRemoteService#payCallbackOrder(String)
```

这就是 SymbolSolver 要解决的问题。

JavaSymbolSolver 本身就是为了把 AST 元素和 declaration 关联起来，并可以通过 `ParserConfiguration` 注入解析器。[GitHub](https://github.com/javaparser/javaparser/blob/master/javaparser-symbol-solver-core/src/main/java/com/github/javaparser/symbolsolver/JavaSymbolSolver.java?utm_source=chatgpt.com)

所以：

> **Symbol Graph 的第一块不是数据库，而是 Symbol Resolution。**

---

# 三、我建议的总体架构

当前：

```text
                         ┌──────── Keyword
Query → SearchAction ────┤
                         └──────── Vector
                              ↓
                           Hybrid
                              ↓
                         SearchResult
                              ↓
                       EvidenceWorkspace
```

改成：

```text
                         ┌──────── Keyword
                         │
Query → SearchAction ────┼──────── Vector
                         │
                         └──────── Exact Symbol
                                  ↓
                             Anchor Chunk
                                  ↓
                         ┌──────────────────┐
                         │   Symbol Graph   │
                         │                  │
                         │ CALLS            │
                         │ IMPLEMENTS       │
                         │ EXTENDS          │
                         │ OVERRIDES        │
                         └────────┬─────────┘
                                  ↓
                         Related Code Chunks
                                  ↓
                       EvidencePool / Workspace
                                  ↓
                             Coverage
                                  ↓
                         Teaching Answer
```

最重要的一点：

> **Symbol Graph 不替换 Hybrid Retrieval。**

它解决的是 Hybrid Retrieval 最弱的一部分：

> **“我已经找到一个正确方法了，但接下来应该沿什么真实程序关系继续找？”**

---

# 四、整个 Symbol Graph V1 的定位

我建议明确冻结：

## V1 要解决

```text
1. Symbol 精确定位
2. Class / Interface 继承实现关系
3. Method → Method 调用关系
4. Constructor 调用关系
5. Interface method → implementation method
6. callers / callees
7. 基于 Anchor 的 1~2 hop Graph Expansion
8. Graph Evidence 接入现有 EvidenceWorkspace
```

## V1 不解决

```text
Spring Bean 运行时精确注入
AOP 真实运行调用图
反射调用
MQ Producer → Consumer 语义关系
Controller Endpoint → API Graph
数据库表 → Mapper Graph
配置文件关系
跨语言调用
GraphRAG Community
Neo4j
```

这些以后可以形成：

```text
Symbol Graph V1
        ↓
Repository Graph V2
        ↓
GraphRAG V3
```

而不是第一版一次做完。

---

# 五、Symbol Graph 中到底存什么

我不建议一上来建：

```text
Class
Method
Field
Variable
Annotation
Package
File
Bean
Endpoint
SQL
Topic
Table
...
```

这会立刻把项目拖成一个知识图谱工程。

第一版只保留四类 Symbol：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

有一个非常现实的原因：

> **它们正好对应你现在已经存在的四类 CODE Chunk。**

当前 `knowledge_chunk` 已经有：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

因此：

```text
Symbol Node
     ↕
1 : 1
     ↕
Knowledge Chunk
```

这非常漂亮。

Graph 负责：

```text
关系
```

Chunk 负责：

```text
可供 LLM 阅读和引用的正文
```

---

# 六、第一版 Edge 应该只有 5 类

我推荐：

| Edge | 含义 |
|---|---|
| `EXTENDS` | 类/接口继承 |
| `IMPLEMENTS` | 类实现接口 |
| `CALLS` | 方法调用另一个方法 |
| `CONSTRUCTS` | 方法/构造器 `new` 某个构造器 |
| `OVERRIDES` | 子类/实现类方法覆盖父类/接口方法 |

暂时不要：

```text
USES
DEPENDS_ON
IMPORTS
REFERENCES
READS
WRITES
```

原因很简单。

例如：

```java
List<User> users = ...
```

如果把所有类型引用都作为边：

```text
UserService → List
UserService → User
UserService → String
UserService → Optional
...
```

图会非常快变成噪声图。

而前面的五种关系都非常有价值。

---

# 七、一个实际的 Graph 长什么样

例如：

```java
@RestController
class PayController {

    public void callback() {
        payService.payCallback();
    }
}
```

```java
class PayServiceImpl implements PayService {

    public boolean payCallback() {
        notifyPayResult();
    }
}
```

Graph：

```text
PayServiceImpl
     │
 IMPLEMENTS
     ▼
PayService


PayController#callback()
     │
    CALLS
     ▼
PayService#payCallback()
     ▲
     │ OVERRIDES
     │
PayServiceImpl#payCallback()
     │
    CALLS
     ▼
PayServiceImpl#notifyPayResult()
```

这就解决了一个普通 RAG 很麻烦的问题：

用户问：

> 支付回调的完整执行流程是什么？

Hybrid Search 可能首先找到：

```text
PayController#callback
```

Symbol Graph 接着直接扩展：

```text
callback
 ↓
PayService#payCallback
 ↓ implementation
PayServiceImpl#payCallback
 ↓
notifyPayResult
```

不用再让 LLM猜 Query。

---

# 八、Symbol ID 是整个设计最重要的数据结构之一

不能简单使用：

```text
method name
```

因为存在重载：

```java
pay(String orderSn)

pay(String orderSn, BigDecimal amount)
```

所以 Symbol 必须有唯一 key。

我建议：

## Class

```text
T:com.foo.payment.PayServiceImpl
```

## Interface

```text
T:com.foo.payment.PayService
```

类型前缀没必要区分 class/interface，kind 字段已经区分。

## Method

```text
M:com.foo.payment.PayServiceImpl#pay(java.lang.String)
```

## Constructor

```text
C:com.foo.payment.PayServiceImpl#<init>(OrderService,TicketService)
```

这样：

```text
simple_name:
pay

qualified_name:
com.foo.payment.PayServiceImpl#pay

canonical_signature:
pay(java.lang.String)

symbol_key:
M:com.foo.payment.PayServiceImpl#pay(java.lang.String)
```

---

# 九、为什么不能直接使用数据库 id 做 Symbol 身份

当前 ingestion 是：

```python
DELETE FROM knowledge_chunk
WHERE repository = ?
```

然后重新 INSERT。

意味着：

```text
chunk_id
```

每次全量重新索引都可能变化。

所以 Symbol Graph 不能认为：

```text
symbol 431
```

是永久身份。

应该：

```text
symbol_key = 稳定自然身份
```

数据库：

```text
id = 内部主键
symbol_key = 稳定业务身份
```

这也是未来做增量索引需要的基础。

---

# 十、这里我建议一个很重要的数据库设计

现在只有：

```text
knowledge_chunk
```

新增两张表即可：

```text
code_symbol
code_symbol_edge
```

不要 Neo4j。

---

# 十一、`code_symbol`

建议：

```sql
CREATE TABLE code_symbol (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    repository TEXT NOT NULL,

    symbol_key TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,

    simple_name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    canonical_signature TEXT,

    owner_symbol_id BIGINT,

    chunk_id BIGINT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(repository, symbol_key),

    FOREIGN KEY(chunk_id)
        REFERENCES knowledge_chunk(id)
        ON DELETE CASCADE,

    FOREIGN KEY(owner_symbol_id)
        REFERENCES code_symbol(id)
        ON DELETE CASCADE
);
```

这里有一个非常好的设计：

```text
code_symbol.chunk_id
```

直接指向：

```text
knowledge_chunk.id
```

因此 Graph 找到：

```text
PayServiceImpl#payCallback
```

以后不用另外生成新的“Graph 文本”。

直接：

```text
symbol
 ↓ chunk_id
knowledge_chunk
 ↓
SearchResult
 ↓
EvidenceWorkspace
```

---

# 十二、为什么 `FIELD` 暂时不建 Symbol

当前 JavaParser 没有生成 FIELD Chunk。

如果 Graph 建：

```text
FIELD orderRemoteService
```

但：

```text
FIELD
```

没有对应 Chunk。

Graph 查到了以后，还得重新设计：

```text
FieldEvidence
GraphEvidence
Citation
```

会迅速侵入整个 Evidence 模型。

而第一版我们完全可以：

```text
CLASS
METHOD
CONSTRUCTOR
```

解决 80% 的代码调用问题。

所以：

> V1 要刻意让所有 Symbol Node 都能映射到一个现有 Chunk。

这是一个很重要的范围控制。

---

# 十三、`code_symbol_edge`

建议：

```sql
CREATE TABLE code_symbol_edge (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    repository TEXT NOT NULL,

    source_symbol_id BIGINT NOT NULL,
    target_symbol_id BIGINT NOT NULL,

    edge_type TEXT NOT NULL,

    source_line INTEGER,

    resolution_kind TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(source_symbol_id)
        REFERENCES code_symbol(id)
        ON DELETE CASCADE,

    FOREIGN KEY(target_symbol_id)
        REFERENCES code_symbol(id)
        ON DELETE CASCADE
);
```

`resolution_kind` 建议：

```text
AST_EXACT
SYMBOL_SOLVER_EXACT
DERIVED_EXACT
```

例如：

```text
IMPLEMENTS
→ AST_EXACT / SymbolSolver

CALLS
→ SYMBOL_SOLVER_EXACT

OVERRIDES
→ DERIVED_EXACT
```

---

# 十四、为什么我要保留 `resolution_kind`

这是项目面试里很有价值的一点。

你不要让图里所有边都表现成：

```text
100% 相同来源
```

实际上：

```text
A extends B
```

和：

```text
service.foo()
```

确定关系的机制不同。

如果以后出现：

```text
unresolved
```

你能知道：

```text
这是解析失败

而不是：

代码里不存在关系
```

这和你现在：

```text
EMPTY

vs

RETRIEVAL_FAILED
```

的设计思想实际上是一致的。

---

# 十五、SymbolSolver 怎么接到当前 JavaParser

当前是：

```java
ParserConfiguration configuration =
    new ParserConfiguration()
        .setLanguageLevel(JAVA_21);
```

改成大概：

```text
CombinedTypeSolver
    │
    ├── ReflectionTypeSolver
    │
    ├── JavaParserTypeSolver(moduleA/src/main/java)
    │
    ├── JavaParserTypeSolver(moduleB/src/main/java)
    │
    └── JavaParserTypeSolver(moduleC/src/main/java)
```

然后：

```text
JavaSymbolSolver
       ↓
ParserConfiguration.setSymbolResolver(...)
```

`JavaParserTypeSolver` 官方要求传入的是 package root 所在的 source directory，因此你这个多模块 Java 仓库不能简单把整个 `my12306/` 根目录塞进去；应发现每个 `src/main/java` 后分别加入 CombinedTypeSolver。[GitHub](https://github.com/javaparser/javaparser/blob/master/javaparser-symbol-solver-core/src/main/java/com/github/javaparser/symbolsolver/resolution/typesolvers/JavaParserTypeSolver.java?utm_source=chatgpt.com)

所以需要一个：

```java
SourceRootDiscovery
```

扫描：

```text
my12306/
├── user-service/
│   └── src/main/java
├── ticket-service/
│   └── src/main/java
├── order-service/
│   └── src/main/java
└── pay-service/
    └── src/main/java
```

然后：

```text
CombinedTypeSolver

+ user-service/src/main/java
+ ticket-service/src/main/java
+ order-service/src/main/java
+ pay-service/src/main/java
```

---

# 十六、不建议第一版解析所有 Maven 依赖

比如：

```text
Spring
MyBatis
RocketMQ
Redisson
```

理论上还可以把：

```text
dependency jar
```

交给 `JarTypeSolver`。

但第一版没必要。

我们的 Symbol Graph 目标是：

> **Repository Internal Graph**

所以：

```text
List.add()
String.substring()
RocketMQTemplate.send()
```

这种外部方法就算能 resolve：

```text
target not in repository
```

直接跳过即可。

我们真正关心：

```text
PayService → OrderService
Controller → Service
Service → Mapper
Service → Feign interface
```

这些项目自身 Symbol。

---

# 十七、Java Parser 需要从“单文件解析器”升级为“仓库分析器”

现在：

```java
for file:
    parser.parse(file)
    → chunks
```

Symbol Graph 最好改成：

```text
RepositoryJavaAnalyzer
         ↓
Parse all CompilationUnit
         ↓
Pass 1
生成 Symbol
         ↓
Pass 2
解析 Relation
         ↓
输出
Chunks
Symbols
Edges
Diagnostics
```

这是非常重要的改变。

---

# 十八、为什么需要 Two-Pass

假设：

```java
A.java
```

里：

```java
b.foo();
```

目标方法：

```text
B#foo
```

定义在：

```text
B.java
```

如果：

```text
边解析 A
边立即建完整图
```

目标 Symbol 可能还没注册。

所以：

## Pass 1：建立 Symbol Table

遍历所有：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

得到：

```text
symbol_key
→ SymbolRecord
```

例如：

```text
M:com.demo.B#foo(String)
→ B.java:35
```

---

## Pass 2：建立 Relation

再解析：

```java
b.foo()
```

SymbolSolver 得：

```text
com.demo.B#foo(java.lang.String)
```

查：

```text
SymbolTable
```

命中：

```text
M:com.demo.B#foo(java.lang.String)
```

然后：

```text
A#bar
 --CALLS-->
B#foo
```

这会非常稳定。

---

# 十九、Java 侧建议的新数据结构

现在只有：

```java
ChunkRecord
```

新增：

```java
SymbolRecord
```

大致：

```java
record SymbolRecord(
    String repository,
    String symbolKey,
    String symbolKind,

    String simpleName,
    String qualifiedName,
    String canonicalSignature,

    String ownerSymbolKey,

    String filePath,
    String chunkType,
    Integer startLine,
    Integer endLine
) {}
```

以及：

```java
record SymbolEdgeRecord(
    String repository,

    String sourceSymbolKey,
    String targetSymbolKey,

    String edgeType,

    String sourceFilePath,
    Integer sourceLine,

    String resolutionKind
) {}
```

---

# 二十、不要把 Chunk / Symbol / Edge 混在一个 JSONL

当前：

```text
java-chunks.jsonl
```

每行 Python 都直接：

```python
Chunk.from_dict(...)
```

如果改成：

```json
{"record_type":"CHUNK"}
{"record_type":"SYMBOL"}
{"record_type":"EDGE"}
```

会破坏现在整个 Runner。

我推荐继续：

```text
artifacts/
├── java-chunks.jsonl
├── java-symbols.jsonl
└── java-symbol-edges.jsonl
```

然后：

```python
JavaParserRunner.parse()
```

升级成：

```python
JavaAnalysisResult(
    chunks=...,
    symbols=...,
    edges=...,
    diagnostics=...
)
```

---

# 二十一、Chunk 本身尽量不要改

这是我认为这套方案很好的地方。

当前：

```python
Chunk
SearchResult
ContextItem
EvidenceRef
Citation
```

基本都可以保持不动。

Graph 只负责：

```text
chunk_id A
 ↓
symbol A
 ↓ graph
symbol B
 ↓
chunk_id B
```

然后还是返回：

```python
SearchResult
```

因此：

```text
EvidencePool
EvidenceWorkspace
CoverageChecker
ExplanationPlanner
TeachingWriter
CitationRegistry
```

全部继续使用现在的模型。

这非常重要。

---

# 二十二、为什么 Graph Edge 本身不需要成为 Citation

假设 Graph 告诉我们：

```text
A#foo
  CALLS
B#bar
```

最终回答：

> `A#foo` 会继续调用 `B#bar`。

LLM 真正应该看到的证据是：

```java
void foo() {
    b.bar();
}
```

也就是：

```text
A#foo 的 METHOD Chunk
```

所以：

```text
Graph edge
```

负责：

> 找到 B。

而：

```text
Chunk
```

负责：

> 证明 A 调用了 B。

这样你的 Evidence 体系仍然是统一的：

```text
所有用户可见项目事实
必须落回真实代码 Chunk
```

而不是突然出现：

```text
[C1] Code
[G1] Graph
[E3] Edge
```

把 Citation 系统搞复杂。

---

# 二十三、当前 RetrievalController 应该怎么改

这是最关键的接入点。

当前 `_execute()`：

```python
execution =
    retrieval_policy.search_scope_with_trace(...)

annotated = [
    source_policy.classify(result, requirement.id)
    for result in execution.results
]

pool.add_many(annotated)

workspace.ingest(annotated)
```

改成：

```text
① 原来的 RetrievalPolicy
        ↓
② SearchResult anchors
        ↓
③ SymbolGraphExpander
        ↓
④ graph related SearchResult
        ↓
⑤ merge
        ↓
⑥ SourcePolicy
        ↓
⑦ EvidencePool
        ↓
⑧ EvidenceWorkspace
```

大概：

```python
execution = retrieval_policy.search_scope_with_trace(...)

expanded = graph_expander.expand(
    query=action.query,
    requirement=requirement,
    anchors=execution.results,
)

results = merge_search_and_graph(
    execution.results,
    expanded.results,
)

annotated = [
    source_policy.classify(result, requirement.id)
    for result in results
]

pool.add_many(annotated)
workspace.ingest(annotated, round_index)
```

---

# 二十四、这个位置为什么特别适合

因为下面这些东西完全不知道 Graph 的存在：

```text
EvidenceWorkspace
CoverageChecker
ContextBuilder
ExplanationPlanner
TeachingWriter
Reviewer
```

对它们来说只是：

```text
之前找到了：
C1
C2
C3

现在找到了：
C1
C2
C3
C7
C8
```

它不关心：

```text
C7 是向量搜来的

还是

从 C2 沿 CALLS 找来的
```

这就是非常好的架构分层。

---

# 二十五、Graph Expansion 不是“所有边 BFS 两层”

这里一定不要犯这个错误：

```text
找到方法
 ↓
所有 relation
 ↓
BFS 3 层
```

很快就炸了。

比如：

```text
OrderService
```

可能：

```text
50 callers
30 methods
10 interfaces
```

最后把几十个 Chunk 都塞 Workspace。

因此必须：

> **Question-aware + Bounded Graph Expansion**

---

# 二十六、V1 可以设计 4 种 Expansion Intent

不用再调用 LLM。

根据：

```text
action.query
+
requirement.target
+
success_criteria
```

做规则分类。

### CALL_CHAIN

关键词：

```text
调用
流程
链路
经过
如何执行
怎么处理
```

允许：

```text
CALLS outgoing
CONSTRUCTS outgoing
OVERRIDES both
```

---

### CALLERS

关键词：

```text
谁调用
哪里调用
被谁调用
调用方
入口
```

允许：

```text
CALLS incoming
```

---

### TYPE_HIERARCHY

关键词：

```text
实现类
接口实现
继承
父类
子类
override
implements
```

允许：

```text
IMPLEMENTS
EXTENDS
OVERRIDES
```

双向。

---

### LOCATION

例如：

```text
notifyPayResult 在哪里定义
```

不 Graph Expand。

因为 Hybrid/Exact Symbol 已经够了。

这是非常重要的：

> Symbol Graph 不应该让简单问题变复杂。

---

# 二十七、建议的第一版硬限制

建议：

```text
MAX_ANCHORS = 3

MAX_HOPS = 2

MAX_GRAPH_RESULTS_PER_ACTION = 8

MAX_NEIGHBORS_PER_NODE = 5
```

例如：

```text
Hybrid:
A
B
C
```

只取前三个 CODE Anchor。

A：

```text
hop1 → D E
hop2 → F
```

B：

```text
hop1 → G
```

最终最多：

```text
8 个 graph candidate
```

绝不能：

```text
while graph has nodes:
    expand()
```

---

# 二十八、Graph Ranking 不要和 Vector Score 硬加

现在：

```text
vector score
keyword score
RRF
```

已有自己的评分体系。

Graph 如果又造：

```text
final_score =
0.6 * vector +
0.4 * graph
```

很难解释。

建议 Graph 内部单独排序：

```text
1. hop 越小越优先
2. edge 越强越优先
3. anchor rank 越高越优先
4. symbol id 做稳定 tie-break
```

例如 Edge 权重只用于 Graph 内部：

```text
CALLS       1.00
CONSTRUCTS  0.95
OVERRIDES   0.90
IMPLEMENTS  0.85
EXTENDS     0.80
```

然后：

```text
Search Results
+
Graph Results
```

使用确定性 merge。

不要直接重新训练一个混合打分公式。

---

# 二十九、一个非常重要的设计：Graph Expansion 不应该挤掉 Anchor

例如原检索：

```text
1 PayServiceImpl#payCallback
2 PayCallbackTxService#markPaid
3 PayServiceImpl#notifyPayResult
```

Graph 查到：

```text
OrderConsumer
TicketConsumer
...
```

不能变成：

```text
1 OrderConsumer
2 TicketConsumer
3 ...
```

然后把真正最相关的 `payCallback` 丢了。

所以：

```text
Anchor 永远保留
```

Graph 是：

> 补充证据。

不是重新排序主检索。

---

# 三十、GraphStore 的 Python API

我建议新增：

```text
src/devcontext/graph/
```

结构：

```text
graph/
├── __init__.py
├── models.py
├── store.py
├── intent.py
├── expansion.py
└── service.py
```

核心接口：

```python
class SymbolGraphStore:

    def symbols_for_chunks(
        self,
        repository: str,
        chunk_ids: list[int]
    ) -> list[CodeSymbol]:
        ...

    def expand_outgoing(
        self,
        symbol_ids: list[int],
        edge_types: tuple[str, ...]
    ) -> list[GraphNeighbor]:
        ...

    def expand_incoming(...):
        ...

    def chunks_for_symbols(...):
        ...

    def find_symbols(
        self,
        repository: str,
        names: list[str]
    ):
        ...
```

然后：

```python
class SymbolGraphService:

    def expand(
        self,
        query,
        requirement,
        anchors
    ) -> GraphExpansion:
        ...
```

---

# 三十一、Graph 查询不需要 Neo4j

现在项目本身就有 PostgreSQL。

Symbol 数量可能是：

```text
几千
```

Edge：

```text
几千～几万
```

完全没必要：

```text
PostgreSQL
+
pgvector
+
Neo4j
```

第一版直接：

```sql
SELECT ...
FROM code_symbol_edge
WHERE source_symbol_id = ANY(...)
AND edge_type = ANY(...)
```

第二层：

```text
frontier
 ↓
再查一次
```

即可。

也就是：

```python
for hop in range(max_hops):
    neighbors = graph_store.expand(frontier)
```

但：

```text
max_hops = 2
max_nodes = 8
```

这是 **Bounded BFS**。

---

# 三十二、查询最好按 frontier 批量，不要 N+1

不要：

```python
for symbol in symbols:
    SELECT edges WHERE source = symbol
```

应该：

```sql
WHERE source_symbol_id = ANY(%s)
```

于是：

```text
hop 1 = 1 SQL

hop 2 = 1 SQL
```

最多两次。

Graph 检索开销基本可以控制在毫秒级。

这和你现在项目“生成慢、检索本身只有总时延约 2%”的情况也很匹配：

> Symbol Graph 的目的主要是提高**检索结构质量**，不是继续优化检索速度。

---

# 三十三、最值得做的是 CALLS

Graph V1 的核心价值不是：

```text
extends / implements
```

而是：

# CALLS

现在一个 METHOD Chunk 里面：

```java
public void foo() {
    service.bar();
}
```

AST：

```text
MethodDeclaration foo
    ↓
MethodCallExpr bar
```

SymbolSolver：

```text
bar
 ↓ resolve
com.demo.Service#bar(...)
```

然后：

```text
foo
 --CALLS-->
bar
```

这就是你的跨文件 Code RAG 能力真正跃迁的地方。

---

# 三十四、调用接口方法时怎么处理

这是 Spring 项目非常常见的问题。

例如：

```java
private PayService payService;

payService.pay();
```

SymbolSolver 很可能解析到：

```text
PayService#pay
```

而不是：

```text
PayServiceImpl#pay
```

这是正确的。

Symbol Graph：

```text
Controller#xxx
      │
    CALLS
      ▼
PayService#pay
      ▲
      │ OVERRIDES
      │
PayServiceImpl#pay
```

查询链路时：

```text
CALLS
+
reverse OVERRIDES
```

就可以继续走到实现类。

这比试图让 JavaParser“猜 Spring 最终注入哪个 Bean”靠谱得多。

---

# 三十五、不要第一版假装知道 Spring Runtime Binding

比如：

```java
@Autowired
private PaymentStrategy strategy;
```

有：

```text
AliStrategy
WechatStrategy
MockStrategy
```

静态分析无法天然保证运行时是哪一个。

第一版应该返回：

```text
PaymentStrategy
    ↑
implementation candidates
```

而不能声称：

```text
runtime 一定调用 AliStrategy
```

未来 Repository Graph V2 可以解析：

```text
@Qualifier
@Primary
@Bean
@Configuration
Spring Bean
```

再做 Spring-aware resolution。

这正好体现一个好的设计原则：

> **Symbol Graph 只保存自己能够确定的事实。**

---

# 三十六、`OVERRIDES` 如何构建

我建议不要依赖复杂的 LLM 或字符串名称。

Pass 1 已经有：

```text
Type Symbol Table
Method Symbol Table
```

同时有：

```text
Class IMPLEMENTS Interface
Class EXTENDS Parent
```

为 Method 额外构造：

```text
method_contract_key
```

比如：

```text
pay(java.lang.String)
```

然后：

```text
Child class
     ↓
ancestor types
     ↓
找相同 method_contract_key
```

找到：

```text
PayServiceImpl#pay(String)
PayService#pay(String)
```

建：

```text
PayServiceImpl#pay
 --OVERRIDES-->
PayService#pay
```

重载因此不会混：

```text
pay(String)
pay(String,Integer)
```

是两个 contract。

---

# 三十七、Constructor 单独建 `CONSTRUCTS`

例如：

```java
Foo foo = new Foo(a);
```

建立：

```text
CurrentMethod
 --CONSTRUCTS-->
Foo#<init>(...)
```

不建议全部都叫 `CALLS`。

因为以后问：

> Foo 是在哪里创建出来的？

可以直接：

```text
incoming CONSTRUCTS
```

语义非常清楚。

---

# 三十八、解析失败怎么办

这点一定要设计好。

不能：

```java
try {
   call.resolve()
} catch (...) {
   throw
}
```

否则一个：

```text
Spring 第三方类型 resolve 失败
```

可能导致：

```text
整个 Java 文件无法入库
```

正确的是：

```text
Chunk 解析成功
    ↓
照常入库

某一 Edge resolve 失败
    ↓
只记录 diagnostics
    ↓
不生成猜测 Edge
```

例如：

```text
symbols_total = 1243

call_sites = 3821

internal_resolved = 1579

external_resolved = 1940

unresolved = 302
```

这组数据非常值得保留。

---

# 三十九、我强烈建议新增 Graph Diagnostics

输出：

```text
JAVA_GRAPH_SUMMARY

symbols:
  classes=...
  interfaces=...
  methods=...
  constructors=...

relations:
  calls=...
  constructs=...
  extends=...
  implements=...
  overrides=...

resolution:
  internal_resolved=...
  external_skipped=...
  unresolved=...
  ambiguous=...
```

以及：

```text
top unresolved files
top unresolved symbols
```

这样面试被问：

> 你的调用图准确吗？

不是回答：

> “用了 JavaParser，所以准确。”

而是：

> “内部调用关系只写入 SymbolSolver 能确定解析、并且目标属于当前 Repository 的边；无法解析的调用不会生成推测边，同时 ingestion 会统计 unresolved rate，因此 graph coverage 是可量化的。”

这个回答质量高很多。

---

# 四十、数据库落库必须和 Chunk 保持同一个 Snapshot

当前：

```python
replace_repository()
```

执行：

```text
DELETE old chunks
INSERT new chunks
```

以后要变成：

```text
BEGIN

DELETE edges
DELETE symbols
DELETE chunks

INSERT chunks

chunk locator
    ↓
chunk_id

INSERT symbols

symbol_key
    ↓
symbol_id

INSERT edges

COMMIT
```

这样不会出现：

```text
Chunk 是新版本
Graph 是旧版本
```

或者：

```text
Graph 指向已经被删掉的 Chunk
```

建议重构成：

```python
replace_repository_snapshot(
    repository,
    chunks,
    embeddings,
    symbols,
    edges,
    embedding_model
)
```

而不是：

```text
先 replace chunks
commit

再 replace graph
commit
```

---

# 四十一、Symbol 怎么关联已有 Chunk

我反而不建议现在给 `knowledge_chunk` 强行增加：

```text
symbol_key
```

可以利用现有字段做一次 ingestion 内匹配：

```text
repository
file_path
chunk_type
start_line
end_line
```

例如：

```text
METHOD
PayServiceImpl.java
132-167
```

唯一对应一个：

```text
SymbolRecord
```

这样：

```text
现有 Chunk schema
```

几乎不用改。

如果后面做增量索引，再考虑在 Chunk 上显式存：

```text
symbol_key
```

也不迟。

---

# 四十二、Graph Exact Symbol Lookup 也值得一起做

Symbol Graph 不只是 BFS。

它还能解决：

```text
用户明确写了：
notifyPayResult
```

当前 keyword search 已经对：

```text
symbol_name exact
```

给了比较高的加分：

```sql
symbol_name exact → +12
class_name exact → +10
signature → +6
```

这个实现其实已经很好。

所以第一版不用替换它。

但可以额外：

```text
query identifiers
       ↓
code_symbol.simple_name
```

做：

```text
Exact Symbol Anchor
```

例如：

```text
notifyPayResult
```

精准得到：

```text
PayServiceImpl#notifyPayResult(...)
```

然后：

```text
Hybrid Anchor
+
Exact Symbol Anchor
```

去重后再 Graph Expansion。

注意：

> 这是强化 anchor recall，不是取代当前 keyword/hybrid。

---

# 四十三、为什么不用让 SearchActionPlanner 直接产生 Graph Action

比如：

```json
{
  "tool": "find_callers",
  "symbol": "notifyPayResult"
}
```

我认为现在还不要。

因为当前：

```text
SearchAction
```

设计非常简单：

```python
query
source_scope
```

而 Symbol Graph V1 首先应该证明：

> **Graph Expansion 本身确实提高了检索质量。**

所以当前保持：

```text
LLM 决定“查什么”

系统决定：
Hybrid Search
+
Graph Expansion
```

等后面做你计划中的：

# Tool-driven Agent Runtime

再把：

```text
find_symbol
find_callers
find_callees
find_implementations
```

变成真正 Tool。

这样演进顺序非常自然：

```text
Symbol Graph
      ↓
内部 Graph Retrieval
      ↓
稳定
      ↓
暴露成 Agent Tools
```

而不是反过来。

---

# 四十四、Graph Expansion 与当前 Round 0 / Round 1 的关系

当前：

```text
Round 0
Search
 ↓
Coverage

CORE 不够
 ↓
Round 1
Search
 ↓
Coverage
```

Symbol Graph 加进来以后：

```text
Round 0
Hybrid Search
   ↓
Anchor
   ↓
Graph Expansion
   ↓
Workspace
   ↓
Coverage
```

如果仍然不足：

```text
Round 1
新的 SearchAction
   ↓
Hybrid
   ↓
新的 Anchor
   ↓
Graph Expansion
   ↓
Coverage
```

非常自然。

Graph 不需要：

```text
Round 2
Round 3
```

它只是增强每个搜索动作。

---

# 四十五、它会直接改善你当前的第二轮 Query 问题

现在 `_discovered_terms()` 从 Workspace 提：

```text
class_name
symbol_name
signature
file_path
```

假如第一轮只搜到：

```text
PayServiceImpl#payCallback
```

之前第二轮可能要靠 LLM：

```text
猜下一步应该搜索什么
```

Graph 加入以后，第一轮可能直接找到：

```text
payCallback
notifyPayResult
PayNotifyMessageSender
...
```

于是：

```text
Workspace
```

本身就拥有更多真实项目术语。

即便 Coverage 仍不足，Round 1 的：

```text
discovered_terms
```

质量也会提高。

这就是：

> Graph 不仅补结果，还会改善现有 Agentic Retrieval 的下一轮输入。

---

# 四十六、EvidenceWorkspace 不需要改，但我建议增加 Graph Trace

虽然答案系统不用关心：

```text
这个 chunk 怎么来的
```

调试和评测非常需要。

建议不要改 EvidenceRef 的核心协议，而新增独立：

```python
GraphExpansionTrace
```

例如：

```json
{
  "action_id": "SA1",
  "intent": "CALL_CHAIN",
  "anchors": [
    {
      "chunk_id": 101,
      "symbol": "PayServiceImpl#payCallback"
    }
  ],
  "paths": [
    {
      "from": "PayServiceImpl#payCallback",
      "edges": [
        "CALLS"
      ],
      "to": "PayServiceImpl#notifyPayResult",
      "hop": 1,
      "chunk_id": 205
    }
  ],
  "added_chunks": [205, 319],
  "latency_ms": 4.7
}
```

然后 `--debug` 可以看到：

```text
Symbol Graph:
  intent: CALL_CHAIN
  anchors: 2
  expanded nodes: 6
  evidence added: 4
  max hop: 2
```

---

# 四十七、为什么 Trace 非常重要

因为以后做 A/B：

```text
baseline
vs
symbol graph
```

如果：

```text
Recall +8%
```

你还得知道：

> 到底是不是 Graph 带来的？

于是可以统计：

```text
Graph Added Evidence

Graph Unique Hits

Graph Rescue Cases
```

例如：

```text
baseline 没找到 TicketConsumer
graph 找到了 TicketConsumer
且 TicketConsumer 属于 Ground Truth
```

这才叫：

```text
GRAPH_RESCUE
```

不是只看最终 Recall。

---

# 四十八、Benchmark 必须单独新增一层

现在：

```text
L1
```

偏：

```text
单 Chunk Retrieval
```

L1.5：

```text
Evidence requirement coverage
```

Symbol Graph 建议新增：

# L1.25 / Graph Retrieval Benchmark

不一定真叫这个名字。

例如：

```text
benchmark/symbol-graph-v1.jsonl
```

case 类型建议：

| 类型 | 测什么 |
|---|---|
| `DEFINITION` | symbol anchor |
| `CALLER` | incoming CALLS |
| `CALLEE` | outgoing CALLS |
| `IMPLEMENTATION` | IMPLEMENTS / OVERRIDES |
| `INHERITANCE` | EXTENDS |
| `CALL_CHAIN` | 2-hop 路径 |
| `CROSS_FILE_FLOW` | 跨文件链路 |

---

# 四十九、最重要的几个指标

不要只看：

```text
Recall@5
```

Graph 需要：

### Symbol Resolution Rate

```text
resolved internal relationships
/
internal relationship attempts
```

---

### Edge Precision

抽样人工验证：

```text
Graph 说 A CALLS B

代码里是否真的如此
```

因为我们不写 fuzzy edge，理论上应该很高。

---

### Path Recall

例如 Ground Truth：

```text
A → B → C
```

系统是否找到：

```text
A
B
C
```

以及顺序。

---

### Graph Rescue Rate

```text
baseline miss
graph hit
```

这个最能证明功能价值。

---

### Noise Rate

```text
Graph 添加的结果中
多少与 GT 无关
```

防止：

```text
Recall 提高
Precision 崩掉
```

---

### Coverage Improvement

最重要的是接到现有系统：

```text
CORE Coverage
Full-case Success
False READY
```

应该继续观察。

尤其：

```text
False READY
```

不能变差。

---

# 五十、你目前的 L1.5 Benchmark 可以补哪些 Graph Case

尤其适合加入这种题：

```text
这个方法的上游入口是什么？

这个方法被哪些地方调用？

这个接口的实现类在哪里？

某个 Controller 请求最终经过哪些 Service？

支付通知如何从 Pay 流到下游？

某个异常补偿方法最终调用哪些组件？
```

这些是当前 Vector/Hybrid 最容易出现：

```text
每个节点单独能搜到

但无法稳定连成链
```

的场景。

---

# 五十一、第一版不要把“调用链完整”理解成 Runtime Trace

一定要在文档里明确：

> 当前得到的是 **Static Symbol Graph**。

它可以确定：

```text
source-level relation
```

但不代表：

```text
某一次线上请求真实一定走这条路径
```

例如：

```text
if
strategy pattern
Spring proxy
reflection
AOP
dynamic dispatch
```

都会影响 Runtime Call Graph。

面试可以直接说：

> 我的 Symbol Graph 是静态代码关系图，不冒充运行时 Trace。对于接口调用，我会解析到静态声明，再通过 IMPLEMENTS/OVERRIDES 关系扩展实现候选；Spring Bean、反射和 AOP 的运行时绑定属于 Repository Graph 下一阶段。

这反而显得严谨。

---

# 五十二、具体文件级改造方案

我建议第一阶段改动这样组织。

## Java Parser

当前：

```text
java-parser/
```

新增：

```text
SymbolRecord.java
SymbolEdgeRecord.java

RepositoryJavaAnalyzer.java
SymbolTable.java
SymbolGraphExtractor.java
SourceRootDiscovery.java
GraphDiagnostics.java
```

修改：

```text
pom.xml
JavaSourceParser.java
Main.java
```

---

## Python

新增：

```text
src/devcontext/graph/
    models.py
    store.py
    intent.py
    expansion.py
    service.py
```

修改：

```text
src/devcontext/ingestion/java_parser_runner.py

src/devcontext/ingestion/pipeline.py

src/devcontext/storage.py

src/devcontext/agentic/retrieval_controller.py

src/devcontext/observability/*
```

尽量不改：

```text
EvidencePlanner
EvidenceWorkspace
CoverageChecker
ContextBuilder
ExplanationPlanner
TeachingWriter
TeachingReviewer
```

这就是一个非常好的改造边界。

---

# 五十三、SQL 建议单独做 `002_symbol_graph.sql`

当前：

```text
001_schema.sql
```

不要把所有东西硬塞进去。

新增：

```text
sql/
├── 001_schema.sql
└── 002_symbol_graph.sql
```

然后把当前：

```python
initialize()
```

从：

```text
只加载 001_schema.sql
```

改为：

```python
for sql_file in sorted(sql/*.sql):
    execute
```

因为现在已经开始真正出现：

```text
Schema Evolution
```

这本身也很有工程价值。

---

# 五十四、一个完整 ingestion 流程

最后 ingestion 应该变成：

```text
                repository
                    │
                    ▼
          RepositoryJavaAnalyzer
                    │
       ┌────────────┼───────────────┐
       ▼            ▼               ▼
     Chunk        Symbol           Edge
       │            │               │
       ▼            │               │
Embedding            │               │
       │            │               │
       └──────┬─────┴───────────────┘
              ▼
       RepositorySnapshot
              │
              ▼
          PostgreSQL
              │
     ┌────────┼─────────┐
     ▼        ▼         ▼
 knowledge  symbol     edge
 _chunk
```

而不是：

```text
Graph 单独跑一遍
```

---

# 五十五、完整 Query 流程

最终：

```text
User
 │
 ▼
EvidencePlanner
 │
 ▼
EvidenceRequirement
 │
 ▼
SearchActionPlanner
 │
 ▼
SearchAction
 │
 ├─────────────────────────────┐
 │                             │
 ▼                             ▼
Hybrid Retrieval        Exact Symbol Lookup
 │                             │
 └─────────────┬───────────────┘
               ▼
          Anchor Chunks
               │
               ▼
       Graph Intent Classifier
               │
      ┌────────┼────────┐
      │        │        │
   CALLS   CALLERS   HIERARCHY
      │        │        │
      └────────┼────────┘
               ▼
          Bounded BFS
               │
               ▼
       Related Symbols
               │
               ▼
         Related Chunks
               │
               ▼
     Search + Graph Merge
               │
               ▼
         EvidencePool
               │
               ▼
      EvidenceWorkspace
               │
               ▼
           Coverage
               │
      ┌────────┴─────────┐
      │                  │
  SATISFIED         PARTIAL
                         │
                         ▼
                     Round 1
```

这一套与当前项目是连续演进，而不是重构重做。

---

# 五十六、我最推荐的实施阶段

如果要直接交给 Agent 开发，我不会让它一次完成所有功能。

## Phase 0：冻结契约

先写测试和数据结构：

```text
SymbolRecord
SymbolEdgeRecord
GraphExpansion
GraphTrace
```

冻结：

```text
symbol key
edge type
graph limits
```

---

## Phase 1：SymbolSolver + Symbol Index

只做：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

要求：

```text
Symbol 数与 Chunk 对齐
唯一 symbol_key
可精确定位
```

先不 CALLS。

---

## Phase 2：Type Graph

做：

```text
EXTENDS
IMPLEMENTS
OVERRIDES
```

因为比方法调用简单，适合作为 Graph 基础验证。

---

## Phase 3：Call Graph

加入：

```text
CALLS
CONSTRUCTS
```

并增加：

```text
unresolved diagnostics
```

这是最重要阶段。

---

## Phase 4：GraphStore + Retrieval

实现：

```text
chunk → symbol
symbol → neighbors
symbol → chunk
```

先 CLI 单独验证：

```text
graph symbol PayServiceImpl

graph callers notifyPayResult

graph callees payCallback
```

---

## Phase 5：接入 RetrievalController

才进入：

```text
Hybrid
+
Graph Expansion
+
EvidenceWorkspace
```

加 feature flag：

```text
SYMBOL_GRAPH_ENABLED=false
```

跑 A/B。

---

## Phase 6：Benchmark

只有满足：

```text
Graph Rescue ↑
CORE Coverage ↑
False READY = 0
Noise 可接受
Latency 增量很小
```

再切：

```text
SYMBOL_GRAPH_ENABLED=true
```

---

# 五十七、第一版验收标准我建议直接写死

至少满足：

```text
① ingestion 不因为单个 unresolved symbol 整体失败

② 100% Symbol Node 有对应 CODE Chunk

③ symbol_key 无重复

④ CALLS 只写 resolved internal edge

⑤ Graph Expansion max_hops <= 2

⑥ 单 SearchAction graph results <= 8

⑦ LOCATION 问题不自动膨胀 Graph

⑧ Graph Chunk 正常进入 EvidenceWorkspace

⑨ Citation 仍然引用原 CODE Chunk

⑩ baseline 模式行为不变

⑪ False READY 不增加

⑫ 可以明确统计 GRAPH_RESCUE
```

---

# 五十八、为什么我认为这版设计特别适合当前项目

因为你现在项目已经有三个很成熟的基础设施：

```text
Evidence Requirement
EvidenceWorkspace
Coverage
```

你其实**不缺再一个复杂 Agent Pipeline**。

真正缺的是：

```text
Retriever 只能看到文本相关性

看不到程序结构。
```

所以最合理的升级不是：

```text
再加 Planner
再加 Reviewer
再加 Query Rewrite
```

而是：

```text
底层知识表示升级：

Chunk
   ↓
Chunk + Symbol Relation
```

这会让现有所有上层能力一起受益。

---

# 五十九、这也会为后续 Tool Agent 铺路

Symbol Graph 稳定以后：

```text
GraphService
```

天然就可以变成工具：

```text
find_symbol(name)

find_callers(symbol)

find_callees(symbol)

find_implementations(symbol)

find_parent_types(symbol)

read_symbol(symbol)
```

Agent：

```text
用户：
支付成功以后为什么能更新订单？

Agent:
1. search_code("支付成功")
2. find_symbol(PayService...)
3. find_callees(payCallback)
4. find_implementations(...)
5. read_symbol(...)
```

也就是说：

> **Symbol Graph 是下一阶段 Tool Agent 真正有价值的工具基础。**

如果没有它，所谓 Tool Agent 最后还是：

```text
search_code()
search_code()
search_code()
```

本质依旧只是 RAG 换了一个壳。

---

# 六十、最终推荐架构

我会把 DevContext 后续目标冻结成：

```text
                 User Question
                       │
                       ▼
                Evidence Planner
                       │
                       ▼
               Retrieval Controller
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
    Keyword          Vector      Exact Symbol
        │              │              │
        └──────────────┼──────────────┘
                       ▼
                  Anchor Symbol
                       │
                       ▼
                 Symbol Graph
                       │
       ┌───────────────┼─────────────────┐
       ▼               ▼                 ▼
     CALLS         IMPLEMENTS         OVERRIDES
       │               │                 │
       └───────────────┼─────────────────┘
                       ▼
                  Code Chunks
                       │
                       ▼
                EvidenceWorkspace
                       │
                       ▼
                    Coverage
                       │
                       ▼
               EvidencePackage
                       │
                       ▼
               ExplanationPlan
                       │
                       ▼
                 Teaching Answer
```

底层：

```text
JavaParser + SymbolSolver
           │
           ▼
   Symbol / Edge Extractor
           │
           ▼
PostgreSQL + pgvector
 ├── knowledge_chunk
 ├── code_symbol
 └── code_symbol_edge
```

---

## 最核心的一句话

如果要把这次功能写成项目亮点，我甚至不建议说：

> “实现了 Symbol Graph。”

更好的表述是：

> **针对传统向量检索难以稳定恢复跨文件调用链的问题，在 JavaParser AST 基础上引入 Symbol Resolution，建立 Class/Method 级静态代码关系图，建模 CALLS、IMPLEMENTS、EXTENDS、OVERRIDES 等确定性关系；检索阶段先通过 Hybrid Retrieval 定位 Anchor Symbol，再执行有界 Graph Expansion，并将关联代码重新投影为现有 EvidenceWorkspace 中的可引用 Chunk，从而在不破坏原有 RAG、Coverage 和教学回答链路的情况下增强跨文件代码理解能力。**

这已经是一个非常适合作为 **DevContext 下一阶段核心功能** 的设计，而且比“直接接一个 GraphRAG 框架”更符合你这个项目目前真正缺失的能力。