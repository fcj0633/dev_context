# Continue 调研：面向 DevContext 初学者的设计解读

## 0. 先回答最重要的问题：我为什么要看 Continue？

如果你现在只学过基础 RAG，你脑子里的流程可能是：

```text
文档
↓
分块 Chunk
↓
Embedding
↓
存入向量数据库
↓
用户提问
↓
把问题 Embedding
↓
相似度搜索
↓
找到相关 Chunk
↓
交给 LLM
```

这个理解没有错。

但 DevContext 开始进入一个比普通“文档问答 RAG”复杂很多的场景：

```text
不是几十篇普通文章

而是：

Java 项目源码
+
Markdown 设计文档
+
类名
+
方法名
+
注解
+
文件路径
+
代码行号
+
自然语言设计解释
```

例如用户问：

```text
purchaseTicket 方法在哪里？
```

这其实不太像普通语义问题。

因为答案中最重要的信息是：

```text
purchaseTicket
```

这是一个**精确的方法名**。

但另外一个问题：

```text
为什么购票之前需要 Token Bucket？
```

这里可能完全没有一个叫：

```text
WhyUseTokenBucket
```

的方法。

你真正需要找到的是设计文档中关于：

```text
限流
高并发
库存保护
削峰
```

等语义相关内容。

所以当你真正开始做 DevContext 时，会遇到第一个重要问题：

> 以前学的“Embedding + Vector Search”到底够不够？

Continue 的价值就在这里。

它不是一本教材。

它相当于：

> 一个已经真正踩过这些坑的工程案例。

我们不是去复制它，而是去观察：

```text
它遇到了什么问题？
↓
为什么简单方案不够？
↓
它最后如何拆系统？
↓
这个设计思想对 DevContext 有没有帮助？
```

这才是调研 Continue 的意义。

---

# 1. Continue 和 DevContext 到底是什么关系？

可以把二者理解成：

```text
Continue
=
成熟 Coding Agent / Coding Assistant

DevContext
=
把其中“Context Retrieval”这一小块拆出来学习
```

DevContext 明确不是要实现：

```text
自动写代码
自动改代码
Git 操作
Terminal
IDE 插件
完整 Coding Agent
```

它真正想研究的是：

```text
Repository

↓
怎么读取

Parse

↓
怎么理解结构

Chunk

↓
怎么形成可检索知识单元

Index

↓
怎么建立搜索能力

Retrieve

↓
怎么找到相关内容

Rank

↓
怎么决定哪些更相关

Context

↓
怎么交给 LLM
```

DevContext 项目定义本身就是围绕：

> 如何为 LLM 找到正确、完整、可验证的项目 Context？

展开的。

所以你阅读 Continue 时，只需要盯住这一条链：

```text
代码仓库
→ Chunk
→ Index
→ Retrieval
```

Continue 的 UI 怎么写、IDE 插件怎么通信、模型 Provider 怎么配置，都可以完全不管。

---

# 2. 第一件要从 Continue 学到的事：不要直接 Repository → Embedding

这是整个 Continue 调研里最重要的设计思想。

你最开始可能会自然想到：

```text
遍历 my12306

↓

读所有 .java

↓

每 1000 token 切一块

↓

Embedding

↓

pgvector
```

这就是：

```text
Repository → Embedding
```

看起来很合理。

但问题很快就会出现。

假设：

```java
@Transactional
public void purchaseTicket(PurchaseTicketReqDTO request) {
    checkTicket();
    lockSeat();
    createOrder();
}
```

如果机械按照 1000 token 切分，有可能得到：

```text
Chunk A

@Transactional
public void purchaseTicket(...){
    checkTicket();

--------------------------

Chunk B

    lockSeat();
    createOrder();
}
```

这时候第二块已经失去了很多信息。

LLM 看到：

```text
lockSeat();
createOrder();
```

却不知道：

```text
属于哪个类？
属于哪个方法？
是否有 @Transactional？
代码在哪个文件？
```

这就是普通文本 RAG 搬到代码场景后出现的第一个问题。

Continue 的解决思路不是：

> “Embedding 模型换得更强。”

而是：

> **Embedding 之前先把知识组织好。**

因此 Continue 实际上形成了：

```text
Repository
    ↓
Chunk
    ↓
不同 Index
```

而不是简单：

```text
Repository
    ↓
Embedding
```

它把 Repository、Chunk 和 Index 分成不同层次。

---

# 3. Chunk 层到底解决什么问题？

你之前学习 RAG 时可能把 Chunk 理解为：

> 为了防止文本太长，所以把文本切小。

这是 Chunk 的一个作用，但在 DevContext 里面远远不够。

更准确的理解应该是：

> **Chunk 是系统认为“可以独立理解和检索的一份知识单元”。**

例如 Markdown：

```text
## 为什么使用 Token Bucket

购票流量可能瞬间放大……
为了保护后端库存系统……
```

这个 Section 本身就是一个很自然的知识单元。

所以：

```text
Heading
+
正文
```

应该放在一起。

而 Java：

```java
@Transactional
public void purchaseTicket(...) {
    ...
}
```

这个 Method 本身也是一个自然的知识单元。

因此：

```text
Markdown
→ 按 Heading 组织

Java
→ 按 AST / Method / Class 组织
```

而不是：

```text
所有文件
→ 每 1000 字符切一次
```

Continue 的代码切分也是“结构优先”：类、方法等语法节点能够整体保留时就整体保留，只有太长时才进一步降级切分。Markdown 则优先按照标题层级递归切。

这对 DevContext 的直接启发就是：

```text
Chunking 不是字符处理问题

而是：

“知识应该以什么边界被保存？”
```

这已经开始进入：

```text
Context Engineering
```

而不仅仅是“调用 Embedding API”。

---

# 4. 为什么 DevContext 要使用 JavaParser？

现在 Continue 给了你一个思想：

> Java 代码不能乱切，要认识 Method / Class。

下一步就出现问题：

```text
程序怎么知道哪里是 Method？
```

你当然可以用字符串：

```java
if (line.contains("public"))
```

但 Java 语法复杂得多。

比如：

```java
@Transactional
@Override
public List<Ticket> purchaseTicket(
        PurchaseTicketReqDTO request) {
    ...
}
```

靠字符串规则很快就会失控。

所以需要：

```text
Java Source
↓
Parser
↓
AST
```

AST 可以暂时理解成：

> **程序眼中的代码结构树。**

例如：

```java
class TicketService {

    @Transactional
    public void purchaseTicket() {
    }
}
```

解析后可以理解成：

```text
Class
└── TicketService
      └── Method
            ├── Annotation: Transactional
            ├── Name: purchaseTicket
            ├── StartLine
            ├── EndLine
            └── MethodBody
```

这时候你就可以非常自然地生成：

```text
CodeChunk

file:
TicketService.java

class:
TicketService

method:
purchaseTicket

annotation:
@Transactional

startLine:
120

endLine:
188

content:
完整 method source
```

所以 JavaParser 调研不是为了让你系统学习 JavaParser API。

它真正只需要证明一件事情：

> JavaParser 能不能帮 DevContext 从 Java 源码里稳定提取这些信息？

也就是：

```text
package
class
method
annotation
source code
start line
end line
```

只要答案是“可以”，它在 V1 的任务基本就完成了。

---

# 5. 为什么 Continue 同时有 Keyword Search 和 Vector Search？

这是第二个你现在特别值得理解的设计。

你已经知道 Vector Search：

```text
Query
↓
Embedding
↓
Vector
↓
相似度搜索
```

它最大的优势是：

> 不需要使用完全一样的词，也能找到意思接近的内容。

例如：

```text
用户：
系统如何自动取消超时订单？

文档：
通过 Redisson 延迟队列触发订单关闭
```

两句话字面并不完全相同。

但语义相近。

所以 Vector Search 很适合这种：

```text
自然语言
概念
设计原因
业务机制
```

查询。

但是现在换一个问题：

```text
RDelayedQueue 在哪里使用？
```

这里：

```text
RDelayedQueue
```

不是一个普通语义概念。

它是：

```text
Java Symbol / API 名称
```

你真正想要的是：

> 哪个文件里面真的出现了字符串 `RDelayedQueue`？

这种时候 Keyword Search 往往更加直接。

再例如：

```text
PurchaseTicketReqDTO
TicketServiceImpl
purchaseTicket
@Transactional
```

这些都属于精确 Identifier。

所以可以简单记成：

```text
Vector Search
擅长：
“意思像不像？”

Keyword Search
擅长：
“这个词到底有没有出现？”
```

Continue 本身就同时使用 Embedding-based retrieval 和 Keyword Search，而不是把二者视为互斥方案。

这就是 DevContext 为什么设计：

```text
             Chunk

          /         \

Keyword Search      Vector Search
     │                   │

精确匹配             语义匹配
```

而不是只有：

```text
Chunk
↓
Embedding
```

---

# 6. 一个非常具体的 my12306 示例

假设项目里有：

```java
public class TicketServiceImpl {

    public void purchaseTicket(...) {
        tokenBucket.tryAcquire();
    }
}
```

设计文档里写着：

```text
## Token Bucket 设计

在高并发购票场景中，为避免大量无效请求直接进入库存和座位计算逻辑，
系统在购票流程前增加 Token Bucket……
```

现在三个问题：

### 问题 A

```text
purchaseTicket 方法在哪里？
```

最适合：

```text
Keyword Search
```

因为：

```text
purchaseTicket
```

本身就是精确 Symbol。

---

### 问题 B

```text
系统为什么要在购票前限流？
```

最适合：

```text
Vector Search
```

因为用户甚至没有输入：

```text
Token Bucket
```

但是：

```text
限流
保护库存
高并发
```

在语义上和设计文档非常接近。

---

### 问题 C

```text
为什么购票前要使用 Token Bucket？
具体在哪里实现？
```

这就是 DevContext 最重要的：

```text
MIXED
```

你既需要：

```text
文档：
为什么这样设计
```

又需要：

```text
代码：
在哪里实现
```

这也是为什么 DevContext 项目定义特别把 DOC / CODE / MIXED 分开，其中 MIXED 是主要研究对象。

到这里，你应该开始看到：

> Continue 调研不是离 DevContext 很远。

其实 Continue 的每个设计问题都直接对应你接下来会碰到的问题。

---

# 7. 为什么需要“同一个 Chunk 建两种 Index”？

这部分原报告很容易把人看晕。

你可以把它想象成图书馆。

一本书真正的内容只有一份：

```text
《Java 并发编程》
```

但是图书馆可以给它建立不同查找方式：

```text
书名索引
作者索引
主题索引
编号索引
```

书并没有复制四份。

只是：

> 查找它的方法不同。

DevContext 也是这样。

真正的知识：

```text
knowledge_chunk
```

只保存一次。

例如：

```text
chunk_id = 101

class_name = TicketServiceImpl
method_name = purchaseTicket
content = ...
embedding = [...]
```

然后建立两种搜索能力：

```text
knowledge_chunk
       │
       ├── Full Text Index
       │      ↓
       │   Keyword Search
       │
       └── Vector Index
              ↓
          Semantic Search
```

Continue 的架构更加复杂，会有多个 artifact；但 DevContext 没有必要复制这套复杂度。

Continue 调研最终给出的简化建议也是：

```text
Repository
→ Parser
→ knowledge_chunk
       ↓        ↓
     FTS      Vector
```

也就是：

> **借思想，不抄实现。**

Continue 复杂的多 artifact 设计适合它自己的多语言、本地 IDE、多 workspace 场景；DevContext 可以在 PostgreSQL 中用一张 `knowledge_chunk` 表同时承担 metadata、全文检索和向量检索。

---

# 8. 为什么 Chunk 还要保存 Metadata？

假设 Vector Search 返回：

```text
@Transactional
public void purchaseTicket(...) {
    ...
}
```

如果只有 content，你只能告诉模型：

```text
这里有一段相关代码。
```

但如果 Chunk 同时保存：

```text
repository
module
file_path
package_name
class_name
method_name
annotations
start_line
end_line
content
```

模型就知道：

```text
这是：
ticket-service

里的：
TicketServiceImpl

中的：
purchaseTicket

位于：
TicketServiceImpl.java

第：
120-188 行
```

于是回答就能变成：

```text
Token Bucket 的调用位于
TicketServiceImpl.java Lines 120-188
中的 purchaseTicket 方法。
```

这就是：

```text
Metadata
```

真正的价值。

它不是为了数据库字段显得丰富。

而是在帮助：

```text
Retrieval
+
Ranking
+
Context Understanding
+
Citation
```

同时工作。

Continue 的普通 RAG Chunk 本身并没有直接携带 `class_name / method_name / annotations`，符号信息被放进另一个 CodeSnippets 索引；调研因此建议 DevContext **不要照搬这一点**，而是因为项目只支持 Java，直接把强类型符号元数据放进 `knowledge_chunk`。

这就是一次典型的：

```text
研究 Continue
≠
复制 Continue
```

---

# 9. 为什么需要 RRF？

现在你已经有：

```text
Keyword Retriever
+
Vector Retriever
```

例如用户搜索：

```text
Token Bucket 如何实现？
```

Keyword Search 可能返回：

```text
1. TokenBucketHandler.java
2. TokenBucket.md
3. TicketService.java
```

Vector Search 可能返回：

```text
1. 高并发限流设计.md
2. TicketService.java
3. TokenBucket.md
```

问题来了：

> 最终给 LLM 哪几个？

最直觉的方法可能是直接比较 score。

例如：

```text
Vector:

A = 0.87
B = 0.81
```

而 Keyword：

```text
C = 8.7
D = 5.3
```

那：

```text
0.87
和
8.7
```

谁更相关？

实际上不能直接比较。

因为二者完全不是一种评分系统。

于是 DevContext 使用：

```text
RRF
```

它的思想不是比较具体分数，而是：

> 看一个 Chunk 在不同搜索结果中排第几名。

例如：

```text
Chunk A

Keyword: 第 2
Vector:  第 1
```

说明：

```text
两个 Retriever 都认为它重要。
```

那么它最终就应该获得较高排名。

你现在不用深入数学。

先记：

```text
RRF = 用“排名”融合多个 Retriever

而不是硬比较完全不同的 score。
```

这已经足够支撑 V1。

---

# 10. Continue 的增量索引现在需要学吗？

需要“理解”，但暂时不需要“实现”。

假设今天：

```text
my12306
有 1000 个文件。
```

第一次建立：

```text
1000 个文件
→ Parse
→ Chunk
→ Embedding
```

没有问题。

第二天你只修改：

```text
TicketService.java
```

如果系统又：

```text
重新读取 1000 个文件
重新 Chunk
重新 Embedding
```

显然浪费。

成熟系统于是会记录：

```text
content hash
```

例如：

```text
TicketService.java

旧内容
↓
SHA256
↓
abc123
```

修改之后：

```text
新内容
↓
SHA256
↓
xyz789
```

系统发现：

```text
abc123 != xyz789
```

于是知道：

> 只有这个文件需要重新索引。

Continue 的实现甚至进一步先比较修改时间，只有必要时再重新计算 hash，并把变化进一步分为 compute / delete / addTag / removeTag。

但这里非常重要：

### 你现在不要实现这套完整东西。

DevContext 只有 10～15 天核心开发周期。

所以：

```text
V1

点击重新索引
↓
删除旧数据
↓
全部重新 Parse
↓
全部重新 Embedding
```

完全可以。

然后到了 P1 再实现：

```text
content_hash
↓
判断文件有没有变化
↓
只处理变化文件
```

这就是：

```text
Full Reindex
→ Incremental Index
```

的演进。

所以调研 Incremental Index 的目的不是：

> “Day 1 我要把 Continue 这套东西复刻出来。”

而是：

> “我提前知道未来系统规模大了以后，应该往哪里升级。”

---

# 11. Continue 最值得你学的，其实不是代码

如果把几十页调研报告压缩成真正应该进入你脑子的内容，我认为只有下面几个思想。

## 思想一：RAG 的效果不是从 Embedding 才开始决定的

以前：

```text
Chunk
→ Embedding
→ Search
```

容易让人觉得核心是 Embedding。

现在应该变成：

```text
Repository

↓
Parsing

↓
Chunk Design

↓
Metadata Design

↓
Index Design

↓
Retrieval Strategy

↓
Ranking

↓
Context Selection

↓
LLM
```

Embedding 只是其中一个组件。

---

## 思想二：先组织知识，再检索知识

如果 Java 方法已经被错误切碎：

```text
Method
↓
乱切
↓
三个没有上下文的 Chunk
```

那么后面：

```text
Embedding
Retriever
Reranker
LLM
```

再强也只能尽量补救。

所以：

```text
好的 Retrieval

首先依赖：

好的 Knowledge Representation。
```

也就是：

```text
知识怎样被 Parse
怎样被 Chunk
保存什么 Metadata
```

---

## 思想三：不同 Query 需要不同 Retrieval

不要认为：

```text
Vector Search = RAG
```

更好的认识应该是：

```text
Symbol Query
→ Keyword 更强

Semantic Query
→ Vector 更强

Mixed Query
→ 多路 Retrieval
```

这也是为什么 DevContext 后面设计：

```text
DOC
CODE
MIXED
```

Router。

---

## 思想四：成熟系统的设计不能直接复制

Continue 需要处理：

```text
多语言
IDE
多 workspace
多 branch
本地运行
离线运行
```

DevContext 当前只有：

```text
Java
+
Markdown
+
my12306
+
个人学习项目
```

所以不能看到 Continue 有：

```text
四种 Index
SQLite
LanceDB
IndexTag
global_cache
```

就全部搬过来。

那叫：

```text
Cargo Cult
```

也就是“因为别人用了，所以我也用”。

你真正应该问的是：

```text
Continue为什么需要它？
↓
DevContext有没有同样的问题？
↓
如果有：
最简单解决方案是什么？
```

---

# 12. Continue 中哪些设计 DevContext 应该参考？

建议只真正吸收下面这些。

### ① Repository → Chunk → Index 分层

不要：

```text
Repository → Embedding
```

而是：

```text
Repository

↓
Parser

↓
Chunk

↓
Index
```

---

### ② Structure-aware Chunk

Markdown：

```text
Heading-aware
```

Java：

```text
AST-aware
```

---

### ③ Keyword + Vector 两种 Retrieval

```text
Keyword
→ 精确 Identifier

Vector
→ Semantic Meaning
```

---

### ④ Chunk 保存 Metadata

特别是 Java：

```text
file
package
class
method
annotation
startLine
endLine
```

---

### ⑤ Citation

最终不能只说：

```text
根据代码……
```

而应该尽可能做到：

```text
TicketServiceImpl.java
Lines 120-188
```

---

### ⑥ content_hash

V1 暂时不用它做完整增量更新。

但 Schema 可以保留：

```text
content_hash
```

为以后做增量索引准备。

---

# 13. 哪些 Continue 内容你现在可以直接跳过？

以下内容看到“知道有这么回事”即可：

```text
IndexTag
global_cache
addTag
removeTag
IndexLock
SQLite 并发控制
LanceDB JSON cache
多 workspace 索引隔离
分支级索引隔离
远程 Index Cache
IDE Recent Files
复杂 Repo Map
```

这些东西对理解成熟系统有价值。

但是对你现在：

```text
Day 1
Day 2
Day 3
```

开始开发 DevContext 没有直接帮助。

不要为了“我好像还有很多没学懂”而停下来研究。

---

# 14. 你目前真正需要理解到什么程度？

如果下面这些问题你能够自己回答，就已经足够进入 DevContext 开发。

### Q1

为什么 Java 不能简单每 1000 token 切一次？

你应该能回答：

> 因为代码有 Method、Class 等天然语义边界，固定长度切割可能破坏方法结构，所以应该优先根据 AST 生成 Chunk。

---

### Q2

JavaParser 在 DevContext 中干什么？

应该能回答：

> 把 Java Source 解析成 AST，让系统能够获得 Class、Method、Annotation、Range 等结构信息，从而生成结构化 CodeChunk。

---

### Q3

为什么需要 Keyword Search？

应该能回答：

> `purchaseTicket`、`RDelayedQueue`、`TicketServiceImpl` 等 Identifier 是精确字符串，Keyword Search 通常比纯语义检索更加稳定。

---

### Q4

为什么还需要 Vector Search？

应该能回答：

> 用户经常使用与源码或文档完全不同的自然语言描述问题，因此需要语义检索找到“意思相关”而非“字符串完全一样”的内容。

---

### Q5

为什么需要 Hybrid？

应该能回答：

> 代码项目既存在 Symbol Query，又存在 Semantic Query，因此 Keyword 与 Vector 是互补关系。

---

### Q6

为什么 Chunk 要保存 method_name？

应该能回答：

> 它不仅帮助检索和排序，也帮助 LLM 理解代码属于哪个方法，并最终生成可验证 Citation。

---

### Q7

为什么使用 RRF？

应该能回答：

> Keyword 和 Vector 的 score 不在同一个评分空间，所以使用排名融合比直接比较原始 score 更合理。

---

### Q8

为什么 V1 可以不做增量索引？

应该能回答：

> 当前项目规模和学习周期有限，V1 优先验证完整 Retrieval Pipeline；全量重建实现简单，增量索引属于工程优化，可以放到 P1。

如果这 8 个问题能够解释清楚：

> Continue 调研对你来说已经基本完成了。

你不需要能够解释 `CodebaseIndexer.ts` 的每一行。

---

# 15. 把 Continue 放回整个 DevContext 学习路线

最后重新看 DevContext：

```text
my12306
│
├── Java Source
│       ↓
│   JavaParser
│       ↓
│   Method/Class Chunk
│
├── Markdown Docs
│       ↓
│   Markdown Parser
│       ↓
│   Heading Chunk
│
└──────────────┐
               ↓
        knowledge_chunk
               │
         ┌─────┴─────┐
         ↓           ↓
       FTS         pgvector
         ↓           ↓
      Keyword       Vector
         └─────┬─────┘
               ↓
              RRF
               ↓
        Top-K Context
               ↓
          LangGraph
               ↓
             LLM
```

你现在学 Continue，是在理解这张图的中间部分：

```text
Repository
↓
Chunk
↓
Index
↓
Retrieve
```

接下来研究 JavaParser，是解决：

```text
Java Source
↓
到底怎么变成好的 CodeChunk？
```

接下来研究 RAGFlow，则是在解决：

```text
已经有 Chunk 以后
↓
Retriever 怎么组合？
↓
Hybrid 为什么有价值？
↓
什么时候 Rerank？
↓
怎么 Evaluation？
```

所以这三个项目其实分别承担三个不同角色：

```text
Continue
↓
“代码知识系统整体怎么组织？”

JavaParser
↓
“Java 代码怎么变成结构化知识？”

RAGFlow
↓
“这些知识应该怎么被检索和评测？”
```

这样理解以后，你就不应该把“开源项目调研”看成开发 DevContext 之前额外增加的一门课。

它真正的作用是：

> **让你在开始编码之前知道哪些问题一定会出现，以及成熟系统曾经如何处理这些问题。**

然后 DevContext 再选择其中最简单、最适合 10～15 天 MVP 的实现。

---

# 16. 对原 Continue 调研报告的阅读建议

原来的 `Continue调研.md` 不建议删除。

它更适合作为：

```text
工程参考文档
```

而不是：

```text
你的第一遍学习文档
```

今后可以采用“两层文档”：

```text
第一层：学习理解版
回答：

为什么有这个问题？
这个机制解决什么？
用一个 my12306 例子解释。
DevContext 怎么用？
我现在需要掌握到什么程度？

                ↓

第二层：源码证据版
回答：

Continue 哪个类？
哪个文件？
什么数据结构？
具体怎么实现？
源码在哪里？
```

平时学习看第一层。

真正编码时，例如准备写：

```text
CodeChunk
FTS
Incremental Index
```

再回来查第二层对应章节。

这会比现在从 `artifactId → IndexTag → cacheKey → RefreshIndexResults` 一路啃下来有效得多。

---

# 17. 最终应该形成的学习认知

完成 Continue 这一部分后，你真正应该获得的不是：

> “我看懂了 Continue 的 CodebaseIndexer。”

而是下面这套认识：

```text
以前：

RAG
=
Chunk
+
Embedding
+
Vector DB
+
LLM
```

升级为：

```text
工程化 RAG / Context Retrieval
=
Parsing

+
Structure-aware Chunking

+
Metadata

+
Keyword Index

+
Vector Index

+
Hybrid Retrieval

+
Ranking / Fusion

+
Context Selection

+
Citation

+
Evaluation
```

而 DevContext 的价值，恰好就是让你亲手把这条链第一次完整做出来。

这才是这次 Continue 调研与你学习 Agent 开发之间最直接的关系。
