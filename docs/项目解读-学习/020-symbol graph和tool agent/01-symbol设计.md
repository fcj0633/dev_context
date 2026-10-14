这次重新来。我不再从 `RepositoryJavaAnalyzer.java` 一行一行往下拆，而是把 **Symbol Graph 当成一本书里的一个完整章节体系** 来讲。

核心原则改成：

> **先解释“为什么要这样设计”，再解释“系统如何一步步实现这个设计”，只有真正影响理解的地方才进入源码细节。**

这样最后你应该形成的是：

> “我知道 Symbol Graph 为什么存在、它抽象了什么、它为什么采用两阶段分析、为什么要 SymbolSolver、为什么要严格区分 Chunk/Symbol/Edge、为什么宁缺勿错。”

而不是只记住几个类和 SQL。

---

# 一、重新规划：Symbol Graph 应该怎么学

我建议把整个 Symbol Graph 拆成 **7 章**。

---

## 第 1 章：为什么普通 RAG 不够，需要 Symbol Graph

这一章回答一个问题：

> **文本检索已经能找到代码了，为什么还需要图？**

重点理解：

```text
Vector / Keyword
解决：
“哪段代码和问题最相关？”

Symbol Graph
解决：
“这段代码和哪些代码存在真实程序关系？”
```

这是整个功能的起点。

---

## 第 2 章：Symbol Graph 到底在表示什么

这一章不谈 JavaParser。

先建立数据模型：

```text
Node：
CLASS
INTERFACE
METHOD
CONSTRUCTOR

Edge：
EXTENDS
IMPLEMENTS
CALLS
CONSTRUCTS
OVERRIDES
```

重点解释：

- 为什么选择这四种节点；
- 为什么不把变量、字段、MQ、数据库表全塞进去；
- 为什么 `CALLS` 和 `OVERRIDES` 必须分开；
- 为什么 Graph 不是 Chunk 的替代品。

这一章学完应该能自己画出：

```text
Controller#createOrder
        |
      CALLS
        ↓
OrderService#createOrder
        ↑
    OVERRIDES
        |
OrderServiceImpl#createOrder
```

---

## 第 3 章：从 Java 仓库到“可分析的程序模型”

这时才介绍 JavaParser 和 SymbolSolver。

重点不是 API，而是理解两个层次：

```text
JavaParser
→ 看懂代码的语法结构

SymbolSolver
→ 判断一个名字真正指向哪个声明
```

再解释为什么一个多模块 Java 项目不能：

```text
一份文件一份文件孤立解析
```

而需要：

```text
Repository-level Analysis
```

这一章会解释 Source Root、类型解析和跨文件解析，但不会陷入 API。

---

## 第 4 章：为什么采用“两阶段构图”

这是 Symbol Graph 最核心的架构思想之一。

第一阶段：

```text
先发现并登记所有 Symbol
```

第二阶段：

```text
再解析所有 Symbol 之间的关系
```

重点解释：

> 为什么不能边扫描文件边直接建 CALLS？

以及：

```text
Pass 1 = 建实体
Pass 2 = 建关系
```

这种设计为什么在图、编译器、数据库导入中都很常见。

---

## 第 5 章：Symbol 身份如何设计

这一章解释：

> 图里怎么判断“这是同一个方法”？

重点：

```text
OrderService#create(String)

OrderService#create(Long)
```

为什么不是一个 Symbol。

再讲：

```text
T:
M:
C:
```

为什么需要：

```text
qualified type
+
method name
+
parameter type
```

这里只在这里深入一次 Symbol Key 的实现。

---

## 第 6 章：五种关系是怎么可靠构建的

这一章按关系讲：

```text
EXTENDS
IMPLEMENTS
CALLS
CONSTRUCTS
OVERRIDES
```

但重点不是“遍历哪个 AST Node”，而是：

> **每种关系到底代表什么语义事实？**

例如：

```text
CALLS
```

为什么只代表静态调用声明；

为什么：

```text
OrderService service
```

的调用不能直接认为目标是：

```text
OrderServiceImpl
```

而必须：

```text
CALLS → interface method
OVERRIDES ← implementation method
```

`OVERRIDES` 会是唯一稍微深入 Java 类型系统实现的一节。

---

## 第 7 章：可靠性设计——为什么宁可缺边，也不能猜边

最后解释：

```text
能确定
→ Graph

不能确定
→ Diagnostic

系统契约坏了
→ Fail
```

这里讲：

```text
resolved_internal
resolved_external
unresolved
internal_target_not_indexed
```

以及最重要的设计哲学：

> **Symbol Graph 是“程序事实层”，不是“LLM 推测层”。**

这一章结束后，再进入下一大部分：

```text
Symbol Graph 如何落库
→ 如何查询
→ 如何接入 RAG
```

---

# 二、现在正式重新开始第一章

# 第 1 章：为什么 DevContext 需要 Symbol Graph？

先完全忘掉 JavaParser、PostgreSQL、SymbolSolver。

从项目最基本的问题出发。

---

## 1.1 DevContext 最开始拥有的是什么能力？

本质上是：

```text
Repository
    ↓
切成 Chunk
    ↓
Keyword / Vector Retrieval
    ↓
找到相关代码/文档
    ↓
LLM 回答
```

假设用户问：

> `closeTimeoutOrder` 是怎么释放座位的？

普通 RAG 可以搜索：

```text
closeTimeoutOrder
订单关闭
释放座位
```

很可能找到：

```java
void closeTimeoutOrder(...) {
    ...
}
```

到这里，RAG 已经完成了它擅长的事情：

> **根据文字和语义，把一个入口代码找出来。**

---

# 三、问题出现在“入口之后”

假设源码里：

```text
closeTimeoutOrder
        ↓
rollbackTicketSeats
        ↓
updateSeatStatus
```

用户真正想知道的是：

> “完整的释放座位过程是什么？”

那么系统不仅需要：

```text
找到 closeTimeoutOrder
```

还要继续找到：

```text
closeTimeoutOrder 调用了谁？

rollbackTicketSeats 又调用了谁？
```

这时候普通 RAG 开始遇到问题。

---

# 四、为什么不能继续依赖 Vector Search？

假设：

```java
void closeTimeoutOrder(...) {
    rollbackTicketSeats(...);
}
```

目标方法：

```java
void rollbackTicketSeats(...) {
    ...
}
```

从“文本语义”看：

```text
closeTimeoutOrder

rollbackTicketSeats
```

可能有一定相关性，

但两者真正的关系并不是：

> “它们语义相似”。

而是：

> **源码中存在一条明确的 CALLS 关系。**

这两种知识是不同的。

---

# 五、文本相关性和程序关系是两种知识

可以把 Repository Knowledge 分成两层。

第一层：

```text
Semantic Knowledge
```

例如：

```text
“订单关闭”
和
“超时取消订单”
语义相关
```

这是：

```text
Embedding
BM25
Keyword
```

擅长的。

---

第二层：

```text
Structural Knowledge
```

例如：

```text
A 调用了 B

B 实现了接口 C

D 继承 E
```

这些不是“语义相似”，而是：

> **确定性的程序结构事实。**

这就是 Symbol Graph 要补上的知识层。

---

# 六、因此 Symbol Graph 不是来替代 RAG 的

这是第一个很重要的设计结论。

错误理解：

```text
有了 Graph
↓
以后不用 Vector Search 了
```

实际设计：

```text
Vector / Keyword
负责找到入口

Symbol Graph
负责从入口沿真实程序关系继续导航
```

也就是：

```text
Query
   ↓
Search
   ↓
Anchor
   ↓
Graph
   ↓
Related Code
```

这里的：

```text
Anchor
```

可以理解成：

> 图搜索的起点。

---

# 七、一个最典型的例子

用户问：

> `TicketOrderController.createTicketOrder` 最终由哪个 Service 实现执行？

普通搜索很容易先找到：

```text
TicketOrderController#createTicketOrder
```

然后源码里：

```text
Controller
      |
      | CALLS
      ↓
OrderService#createTicketOrder
```

这里还是接口。

再继续：

```text
OrderServiceImpl#createTicketOrder
      |
      | OVERRIDES
      ↓
OrderService#createTicketOrder
```

最终得到：

```text
Controller
    ↓ CALLS
Interface Method
    ↑ OVERRIDES
Implementation Method
```

这套关系用：

```text
Vector Similarity
```

很难稳定表达。

但是 Graph 天然适合。

---

# 八、为什么这里不是简单的“调用链图”？

这是第二个重要设计点。

如果只做：

```text
A CALLS B
```

还不够。

Java 项目大量使用：

```text
Interface
Inheritance
Override
Constructor
```

例如：

```java
OrderService orderService;
orderService.createTicketOrder();
```

静态类型是：

```text
OrderService
```

所以真正能够确定的调用关系：

```text
Controller
    ↓ CALLS
OrderService#createTicketOrder
```

不能直接写：

```text
Controller
    ↓ CALLS
OrderServiceImpl#createTicketOrder
```

因为：

> 编译期并不知道 Spring 最终注入哪个 Bean。

因此需要另外一条关系：

```text
OrderServiceImpl#createTicketOrder
       ↓ OVERRIDES
OrderService#createTicketOrder
```

这样：

```text
CALLS
```

和：

```text
OVERRIDES
```

各自表达一个真实事实。

---

# 九、这体现了 Symbol Graph 的第一条核心原则

> **不同语义的关系必须分开建模。**

不能做一个万能 Edge：

```text
RELATED_TO
```

也不能：

```text
DEPENDS_ON
```

把所有东西都塞进去。

否则图会变成：

```text
A 跟 B 有关系
```

但你不知道：

```text
是调用？
继承？
实现？
覆盖？
构造？
```

这样对 Agent 和检索都没什么用。

---

# 十、为什么只选择四种 Node？

当前 V1 节点：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

为什么没有：

```text
FIELD
VARIABLE
ANNOTATION
MQ_TOPIC
DATABASE_TABLE
CONFIG_KEY
```

不是因为它们没价值。

而是因为 V1 要解决的是：

> **最核心的 Java 程序执行与类型关系。**

这四种节点已经能够表达：

```text
类结构
接口
方法调用
构造调用
实现关系
继承关系
```

而且最重要的是：

> 当前项目已经有对应的 CODE Chunk。

所以：

```text
Graph Node
↔
CODE Chunk
```

可以稳定一一关联。

---

# 十一、为什么“Node 必须能映射到 Chunk”很重要？

假设图里有：

```text
Method A
Method B
```

Graph 告诉你：

```text
A CALLS B
```

但是最终 LLM 回答不能只得到：

```text
“Graph says A calls B.”
```

它还需要阅读：

```java
void A() {
    B();
}
```

和：

```java
void B() {
    ...
}
```

所以：

```text
Graph
```

负责：

> **找到哪段代码。**

而：

```text
Chunk
```

负责：

> **把真实源码提供给 LLM。**

因此当前系统不是：

```text
Graph → LLM
```

而是：

```text
Graph
   ↓
Symbol
   ↓
Chunk
   ↓
EvidenceWorkspace
   ↓
LLM
```

---

# 十二、这就解释了 Symbol Graph 和 Chunk 的本质区别

可以记成一句：

> **Chunk 是“内容单位”，Symbol 是“程序实体单位”。**

Chunk 关注：

```text
给模型看什么文本？
```

Symbol 关注：

```text
这个程序实体是谁？
```

Graph Edge 关注：

```text
两个程序实体之间是什么关系？
```

所以三个概念：

```text
Chunk
Symbol
Edge
```

绝对不能混在一起。

---

# 十三、因此完整知识模型其实是三层

可以把 DevContext 当前代码知识理解成：

```text
第一层：Content Layer

Chunk
源码正文
文档正文


第二层：Identity Layer

Symbol
Class
Method
Constructor


第三层：Relation Layer

CALLS
EXTENDS
IMPLEMENTS
OVERRIDES
CONSTRUCTS
```

普通 RAG 主要工作在：

```text
Content Layer
```

Symbol Graph 加入：

```text
Identity + Relation Layer
```

这就是功能上的真正升级。

---

# 十四、为什么叫 Symbol Graph，而不是 Code Graph？

严格来说，现在项目实现的是：

> **以 Java Symbol 为节点的静态程序关系图。**

不是完整：

```text
Repository Graph
```

因为 Repository Graph 还可以有：

```text
API Endpoint
MQ Topic
DB Table
Configuration
Spring Bean
Module
File
```

例如未来可能：

```text
Controller
    ↓ exposes
/api/order

Producer
    ↓ publishes
ORDER_DELAY_TOPIC

Mapper
    ↓ accesses
t_order
```

但当前 V1 没有这些。

所以叫：

```text
Symbol Graph
```

更准确。

---

# 十五、为什么也不是 GraphRAG？

GraphRAG 通常是：

```text
文本
↓
实体关系抽取
↓
Knowledge Graph
↓
Community / Global Search
```

很多关系可能来自：

```text
LLM extraction
```

而当前 Symbol Graph：

```text
Java Source
↓
AST + Type Resolution
↓
Deterministic Relations
```

例如：

```text
A CALLS B
```

来自源码事实，

不是让模型读代码后猜：

> “看起来 A 可能调用 B。”

所以它更接近：

> **Static Program Analysis**

而不是传统 GraphRAG。

---

# 十六、到这里可以先得到第一个完整认知

Symbol Graph 的目标不是：

> 做一个很复杂的图数据库。

而是：

> **给 RAG 增加一个“程序结构导航层”。**

最终架构：

```text
用户问题
    ↓
Keyword / Vector
    ↓
找到入口 Chunk
    ↓
找到对应 Symbol
    ↓
沿 Graph Relation 导航
    ↓
找到相关 Symbol
    ↓
映射回相关 Chunk
    ↓
EvidenceWorkspace
    ↓
回答
```

这就是整个 Symbol Graph 的最基本设计。

---

# 十七、为什么这一设计对代码问答特别适合？

代码问题通常可以分两类。

### 第一类：定位问题

> `PayChannelFactory` 在哪里？

普通：

```text
keyword search
```

已经很好。

不需要 Graph。

---

### 第二类：关系问题

> 谁调用它？

> 它调用谁？

> 哪个类实现了这个接口？

> 从 Controller 到真正执行方法经过哪些节点？

这些问题：

```text
Graph
```

天然比纯文本 Search 更合适。

所以 Graph 的作用不是：

```text
所有问题都扩图
```

而是：

> **在“关系型问题”上提供确定性能力。**

这也解释了后来为什么要做：

```text
Graph Intent
```

以及再后来 Tool Agent 的：

```text
find_callers
find_callees
find_implementations
```

---

# 十八、本章最重要的四个结论

先不进入实现，你只需要记住这四句话：

### 第一

> **RAG 解决文本相关性，Symbol Graph 解决程序结构关系。**

### 第二

> **Graph 不是替代 Search，而是 Search 找到 Anchor 后的结构化导航能力。**

### 第三

> **Symbol 是程序实体，Chunk 是可阅读证据，两者必须分开。**

### 第四

> **Graph 只保存能够可靠证明的静态程序事实，而不是运行时猜测。**

---

# 十九、下一章怎么讲

下一章再进入：

# 第 2 章：Symbol Graph 到底如何抽象 Java 程序

这一章仍然不会大量讲代码。

会重点讲：

```text
为什么是：

CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

以及：

```text
为什么是：

EXTENDS
IMPLEMENTS
CALLS
CONSTRUCTS
OVERRIDES
```

然后用一个完整的小型 Java 示例：

```text
Controller
Service Interface
ServiceImpl
BaseService
Exception
```

先手工画出整张 Symbol Graph。

只有把“图应该长什么样”彻底理解以后，第三章才进入：

```text
JavaParser
SymbolSolver
Two-Pass Analysis
```

这样学习顺序会比之前直接钻 `RepositoryJavaAnalyzer` 清晰很多。