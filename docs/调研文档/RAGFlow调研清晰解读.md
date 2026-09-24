# RAGFlow 调研解析：从 Chunk 到 Retrieval、Rerank 与 Evaluation

## 0. 先把三个调研项目放到一张图里

到目前为止，我们已经研究了三个不同层次的问题。

Continue 回答：

```text
一个代码仓库怎样变成可检索 Context？
```

它让我们建立了：

```text
Repository
↓
Chunk
↓
Index
↓
Retrieve
```

这个总体架构。

JavaParser 回答：

```text
Java Source 怎样变成高质量 Code Chunk？
```

于是我们得到：

```text
Java Source
↓
AST
↓
Method / Class
↓
Structured CodeChunk
```

现在轮到 RAGFlow。

它研究的是后半段：

```text
已经有 Chunk 了

↓

怎样搜索？

↓

Keyword 与 Vector 怎么配合？

↓

多个结果怎么融合？

↓

要不要 Rerank？

↓

怎样知道 Retrieval 到底有没有变好？
```

所以三个项目可以这样定位：

```text
Continue
=
系统怎么分层

JavaParser
=
Java 知识怎么组织

RAGFlow
=
组织好的知识怎么检索和评测
```

这就是这次调研最重要的背景。

---

# 1. 为什么 DevContext 需要研究 RAGFlow？

你目前已经知道最基础的 RAG：

```text
Question
↓
Embedding
↓
Vector Search
↓
Top-K
↓
LLM
```

如果只是做一个简单的“PDF 问答 Demo”，这条链可能已经够用。

但 DevContext 想研究的是一个更工程化的问题：

> 为什么最终检索出来的是这几个 Chunk，而不是另外几个？

这会继续拆成很多问题：

```text
Chunk 到底怎么切？

Keyword Search 是否需要？

Vector Search 是否够用？

两路结果怎么融合？

为什么不能直接比较 score？

Rerank 应该放在哪？

Top-K 应该取多少？

如果最终答案错了，
到底是 Retriever 错了还是 LLM 错了？
```

RAGFlow 的价值就是：

> 它是一个已经把这些问题都真正工程化实现了的 RAG 系统。

因此我们不是去学习：

```text
RAGFlow 怎么部署
RAGFlow UI 怎么写
RAGFlow Agent 怎么做
```

而是只看：

```text
Chunking
Full-text Retrieval
Vector Retrieval
Hybrid Retrieval
Rerank
Retrieval Evaluation
```

这也是原调研明确限定的范围。

---

# 2. RAGFlow 给我们的第一个重要结论：没有“万能 Chunk 大小”

你刚开始学 RAG 时，很容易形成一个习惯：

```text
chunk_size = 500
overlap = 50
```

然后不管什么文件都这么切。

但 RAGFlow 本身完全不是这么设计的。

它针对不同数据类型采用不同 Chunk 策略：

```text
普通文档
→ General

问答对
→ 一个 Q&A 一个 Chunk

表格
→ 一行一个 Chunk

论文
→ 摘要 / 章节 / 小节

法律文档
→ 法律结构

PPT
→ 一页 / 一张幻灯片一个 Chunk

短文档
→ 整篇一个 Chunk
```

也就是说，RAGFlow 的设计思想不是：

> 为所有内容找一个最好的 `chunk_size`。

而是：

> **先判断这类信息天然的语义单元是什么。**

原调研从 RAGFlow 的多种 parser/chunker 中得到的核心结论就是：问答、表格、幻灯片、法律条款等根本不存在统一的“最佳固定长度”。

---

# 3. 这和 DevContext 有什么关系？

DevContext 只有两种主要知识：

```text
Markdown
Java
```

它们的自然语义单元完全不同。

Markdown：

```markdown
## 为什么需要 Token Bucket

...
```

自然边界是：

```text
Heading
+
Section
```

所以：

```text
Markdown
→ Heading-aware Chunk
```

Java：

```java
@Transactional
public void purchaseTicket(...) {
    ...
}
```

自然边界是：

```text
Method
Class
Constructor
```

所以：

```text
Java
→ AST-aware Chunk
```

因此 DevContext 不需要 RAGFlow 的十几种 chunker。

但要借它一个非常重要的思想：

> **Chunk Strategy 应该由内容结构决定，而不是由一个全局 token 数决定。**

于是：

```text
.md
↓
Markdown Chunker

.java
↓
JavaParser Chunker
```

已经足够。

原调研最终也是这个取舍：借“按数据类型分派策略”，但 DevContext 只保留 Markdown 和 Java 两个分支。

---

# 4. RAGFlow 的父子 Chunk 是什么？

这是一个很值得理解的 RAG 思想。

假设有一篇很长的文档。

如果 Chunk 太大：

```text
2000 token
```

优点：

```text
上下文完整
```

缺点：

```text
Embedding 表达过于平均
```

例如一个 Chunk 同时讲：

```text
事务
Redis
锁
缓存
限流
```

那么它的向量语义会非常分散。

反过来，如果 Chunk 太小：

```text
100 token
```

优点：

```text
检索匹配很精准
```

缺点：

```text
找到以后上下文不够
```

于是存在经典矛盾：

```text
小 Chunk
→ 好找，但信息少

大 Chunk
→ 信息多，但不好找
```

RAGFlow 的一种解决方案叫：

```text
Parent-Child Chunk
```

结构类似：

```text
Parent Chunk
完整一段较大内容

├── Child Chunk A
├── Child Chunk B
└── Child Chunk C
```

搜索时：

```text
用 Child 搜
```

因为 Child 更精准。

命中以后：

```text
带 Parent 给 LLM
```

因为 Parent 上下文更完整。

RAGFlow 的子 Chunk 会携带父文本 `mom`，这正是在解决“细粒度召回”和“完整回答上下文”之间的矛盾。

---

# 5. DevContext 现在要不要做 Parent-Child Chunk？

V1 不需要。

原因在于 Java 本身已经提供一个非常好的自然粒度：

```text
Method
```

大多数情况下：

```text
Method
```

既不会像整类一样太大，

也不会像任意 100 token 一样太小。

它本身已经是：

```text
相对精准
+
相对完整
```

的语义单元。

所以目前：

```text
METHOD Chunk
+
CLASS Summary Chunk
```

就已经相当于一种轻量层级关系。

例如：

```text
TicketServiceImpl
CLASS Summary

↓

purchaseTicket
METHOD Chunk

cancelTicket
METHOD Chunk

refundTicket
METHOD Chunk
```

未来如果真的需要 Parent-Child，可以把：

```text
CLASS Summary
→ Parent

METHOD
→ Child
```

而不必照搬 RAGFlow 那种“同一段文本再切一遍”的方案。

---

# 6. RAGFlow 第二个重要启发：不是所有 Metadata 都应该塞进 Embedding 文本

这是非常值得你现在建立的意识。

假设一个 Chunk 有：

```text
class_name
method_name
content
file_path
start_line
end_line
content_hash
repository_id
```

最简单的方法可能是：

```text
全部拼成字符串
↓
Embedding
```

但这并不好。

例如：

```text
start_line = 120
end_line = 188

content_hash =
9a73f1c438...
```

这些信息对于：

```text
“这段代码表达什么意思？”
```

没有语义帮助。

反而可能污染 Embedding。

RAGFlow 在表格模式里明确区分三种字段用途：

```text
Indexing
Metadata
Both
```

也就是说一个字段可以：

```text
只参与搜索

只作为过滤条件

两者都参与
```

这个设计可以直接映射到 DevContext。

---

# 7. DevContext 的字段应该怎样分类？

例如：

### Both：既参与检索，也属于 Metadata

```text
class_name
method_name
signature
annotations
content
```

原因：

这些字段既对搜索有帮助，又是结果解释的重要信息。

例如：

```text
method_name = purchaseTicket
```

Keyword Search 非常重要。

同时返回结果时你也要告诉用户：

```text
这是 purchaseTicket 方法。
```

---

### Index / Metadata

```text
file_path
module
```

它们可以帮助搜索：

```text
ticket-service
TicketServiceImpl.java
```

也可以作为过滤：

```text
只搜索 ticket-service
```

---

### Metadata Only

```text
start_line
end_line
content_hash
repository_id
```

这些是系统定位数据。

不应该进入 Embedding 正文。

这个思想非常实用：

> **Metadata 不等于 Embedding Input。**

---

# 8. 为什么需要 Full-text Search？

这部分和 Continue 的结论会再次互相验证。

考虑：

```text
RDelayedQueue
PurchaseTicketReqDTO
TicketServiceImpl
purchaseTicket
@FeignClient
```

这些内容都有一个共同特征：

```text
Identifier
```

也就是程序中的精确名字。

假设用户问：

```text
RDelayedQueue 在哪里使用？
```

这时候最有价值的行为不是：

```text
找“语义相似”的东西
```

而是：

```text
哪个 Chunk 真的出现 RDelayedQueue？
```

所以：

```text
Full-text / Keyword Search
```

非常适合这类问题。

RAGFlow 的官方搜索说明本身也明确指出，当需要处理专有名词、型号、标识符和固定术语等精确匹配时，应该提高全文检索一侧的重要性。

这对 Code RAG 尤其重要。

---

# 9. 为什么 Vector Search 仍然不可替代？

换一个问题：

```text
系统是如何实现订单超时关闭的？
```

代码和文档可能写的是：

```text
Redisson RDelayedQueue
DelayQueueConsumer
cancelOrder
closeOrder
```

用户却没有输入这些词。

他只表达了：

```text
订单
超时
自动关闭
```

这时候 Keyword Search 可能很弱。

Vector Search 的优势就是：

```text
不要求字面一样

只要求语义接近
```

所以：

```text
自然语言 Why / How
```

更依赖：

```text
Semantic Retrieval
```

于是最终可以形成一个非常清晰的分工：

```text
Identifier / Symbol
→ Keyword

Natural Language
→ Vector

Mixed
→ Keyword + Vector
```

原调研最后也把 DevContext 的 Symbol、Natural Language 和 MIXED 三类查询明确映射到了这两条检索路径。

---

# 10. 为什么不能只做 Vector Search？

这是 RAG 初学阶段非常容易误解的一点。

你可能会想：

> Embedding 已经能理解语义了，那为什么还需要 Keyword Search？

因为：

```text
Vector Search
```

解决的是：

```text
意思像不像
```

而不是：

```text
字符串是不是完全匹配
```

例如：

```text
RDelayedQueue
```

对 Embedding 模型来说，可能只是一个比较特殊的 token。

但对代码系统来说：

```text
RDelayedQueue
```

本身就是答案线索。

因此：

```text
Vector
```

和：

```text
Keyword
```

不是“新旧技术替代关系”。

而是：

```text
两种不同信号
```

---

# 11. Full-text Search 中“字段权重”为什么重要？

假设用户搜索：

```text
purchaseTicket
```

现在两个 Chunk：

Chunk A：

```text
method_name:
purchaseTicket
```

Chunk B：

```text
content:
// 调用 purchaseTicket 接口
```

虽然都包含：

```text
purchaseTicket
```

但显然：

```text
A
```

更应该排前面。

原因是：

> 方法名命中，比正文偶然出现更有意义。

RAGFlow 的全文检索会给不同字段不同权重，例如标题、重要关键词、问题字段和正文拥有不同 boost。

DevContext 完全可以借这个思想：

```text
method_name
class_name
→ 高权重

annotations
signature
→ 中高权重

content
→ 普通权重
```

例如 PostgreSQL：

```text
A:
method_name / class_name

B:
signature / annotations

C:
content
```

这样：

```text
purchaseTicket
```

直接命中：

```text
method_name
```

的结果就会靠前。

---

# 12. 为什么字段加权是一个很值得优先做的优化？

因为它：

```text
不需要模型
不增加 LLM 调用
不需要新的数据库
逻辑简单
可解释
```

但收益非常直接。

这类优化往往比：

```text
换更强 Embedding Model
```

更值得优先尝试。

这也是 Context Engineering 很重要的一点：

> **检索质量不只是模型决定的，数据结构和 Ranking 规则同样重要。**

---

# 13. RAGFlow 的 Hybrid Retrieval 到底怎么做？

这里需要特别注意：

> RAGFlow 并没有使用 RRF。

它使用：

```text
Weighted Sum
```

也就是类似：

```text
final_score
=
0.7 × term_score
+
0.3 × vector_score
```

默认情况下，全文检索和向量检索各占一定权重。

乍看这似乎和 DevContext 不一样。

DevContext 准备使用：

```text
RRF
```

那是不是说明我们选错了？

不是。

关键区别在于：

> **RAGFlow 为了加权求和，额外做了大量工作让两种 score 可以放到同一个尺度上比较。**

---

# 14. 为什么 Keyword Score 和 Vector Score 不能直接相加？

举个最简单的例子。

Vector Search：

```text
Chunk A
similarity = 0.87
```

Full-text Search：

```text
Chunk B
score = 7.4
```

那么：

```text
7.4 > 0.87
```

是否说明 B 更相关？

当然不能。

因为：

```text
0.87
```

是 Vector Similarity。

```text
7.4
```

是另一个完全不同的评分体系。

这就像：

```text
90 分考试成绩
和
3.8 GPA
```

不能直接说：

```text
90 > 3.8
```

所以前面 DevContext 选择：

```text
RRF
```

就是为了避开这个问题。

---

# 15. RAGFlow 为什么又能直接加权？

因为它主动把两边做成：

```text
可比较尺度
```

例如全文侧不直接拿 BM25 原始分数，而自己计算 term similarity；

不同引擎还会进行归一化。

于是最后能得到类似：

```text
term_similarity = 0.82
vector_similarity = 0.76
```

然后：

```text
0.7 × 0.82
+
0.3 × 0.76
```

才有意义。

原调研特别指出，RAGFlow 之所以能使用 weighted sum，是因为它主动消除了两路 score 空间的不一致；而纯 term 检索时甚至要关闭原本针对混合相似度设计的阈值，这反过来也说明不同 score 空间不能随便混用。

---

# 16. 那为什么 DevContext 还是选择 RRF？

因为 DevContext 的目标不同。

RAGFlow 是一个产品。

它希望用户可以调：

```text
Vector Weight = 0.3
0.5
0.8
```

所以必须解决：

```text
不同 score 如何归一化
```

DevContext 是学习和评测项目。

我们更关心：

```text
Keyword 第几名？

Vector 第几名？

两边都认为重要吗？
```

RRF 直接基于：

```text
Rank
```

而不是：

```text
Raw Score
```

例如：

Keyword：

```text
A 第1
B 第2
C 第3
```

Vector：

```text
C 第1
A 第2
D 第3
```

那么 A：

```text
Keyword 第1
Vector 第2
```

显然两路都认为它很相关。

RRF 会把 A 排得很高。

所以 DevContext 的取舍是：

```text
放弃连续权重控制

换来：

不用归一化
实现简单
容易解释
容易 Evaluation
```

这不是 RAGFlow 错、DevContext 对。

而是：

```text
目标不同
→ 工程选择不同
```

---

# 17. RRF 应该怎样理解，而不是只背公式？

你现在不用重点背：

```text
1 / (k + rank)
```

更重要的是理解：

> RRF 相信“多个 Retriever 一致认为某个结果重要”本身就是一种强信号。

例如：

```text
purchaseTicket
```

一个 METHOD Chunk：

Keyword：

```text
第 1
```

Vector：

```text
第 3
```

另一个文档 Chunk：

Keyword：

```text
第 20
```

Vector：

```text
第 1
```

那么第一个 Chunk 在多个信号下都表现不错。

这往往说明它：

```text
稳定相关
```

RRF 就是在利用：

```text
Ranking Consensus
```

---

# 18. 一个很重要的设计：召回和排序不要混在一起

RAGFlow 有一个值得注意的工程思想：

```text
先召回 Candidate
↓
再排序
```

而不是：

```text
一次 Search
直接得到最终结果
```

可以理解成：

```text
阶段 1：

尽量别漏掉正确答案
Recall

↓

阶段 2：

把真正重要的内容排前面
Ranking
```

这两阶段追求的目标不同。

第一阶段关注：

```text
正确 Chunk 有没有进候选集？
```

第二阶段关注：

```text
进来以后能不能排到 Top-K？
```

原调研把 RAGFlow 首轮召回、后续使用用户权重重新计算的实现解释为“召回与排序分离”的典型实例。

---

# 19. DevContext 的两阶段可以怎么设计？

非常自然：

```text
Question

↓

Keyword Retriever
取 Top 20

+

Vector Retriever
取 Top 20

↓

Candidate Set

↓

RRF

↓

Top 10 / Top 20

↓

可选 Rerank

↓

最终 Top-K
```

这里第一阶段：

```text
Keyword Top-N
Vector Top-N
```

主要负责：

```text
Recall
```

RRF / Rerank：

```text
Ranking
```

这就是一个非常标准的检索系统结构。

---

# 20. 什么是 Rerank？

假设第一阶段拿到 50 个候选 Chunk：

```text
Chunk 1
Chunk 2
...
Chunk 50
```

Keyword / Vector Search 都是相对便宜的快速检索。

但它们对 Query 和 Chunk 的理解有限。

Reranker 则可以对每一个候选做更仔细的判断：

```text
Query
+
Chunk
↓
Cross Encoder
↓
relevance score
```

例如：

```text
Question:
为什么 Feign 调用要移出事务？

Chunk:
事务中包含远程调用会扩大事务时间……
```

Reranker 直接同时看：

```text
Question + Chunk
```

因此往往比单独计算两个 Embedding 后做 cosine 更准确。

所以流程是：

```text
Retriever
→ 大范围快速找

Reranker
→ 小范围仔细排
```

---

# 21. 为什么不能直接用 Reranker 搜整个数据库？

因为贵。

假设：

```text
100000 Chunk
```

Vector Search：

```text
ANN
```

可以非常快找到几十个候选。

但如果让 Cross Encoder：

```text
Question + Chunk 1
Question + Chunk 2
...
Question + Chunk 100000
```

逐个跑模型，

成本和延迟都会非常高。

所以标准结构是：

```text
Recall
↓
Candidate Set
↓
Rerank
```

这也是 RAGFlow 的实现结构：先取固定数量候选，再进行可选的 rerank，之后再做阈值过滤和最终 Top-K。

---

# 22. RAGFlow 的 Rerank 有一个非常值得注意的设计

RAGFlow 并没有让 Reranker 完全接管最终分数。

它仍然保留：

```text
Keyword Signal
```

然后把：

```text
原 Vector Similarity
```

替换为：

```text
Reranker Similarity
```

大致变成：

```text
Keyword
+
Reranker
```

而不是：

```text
只相信 Reranker
```

为什么？

考虑：

```text
RDelayedQueue
```

这样的 Identifier。

语义模型可能不一定认为它“很相关”。

但是 Keyword 明明已经：

```text
精确匹配
```

如果 Reranker 完全接管排序，可能反而把精确 Symbol 命中的结果压下去。

原调研因此认为，RAGFlow 保留关键词信号的思想对 DevContext 比较有参考价值。

---

# 23. DevContext 的 Rerank 现在要做吗？

P0 / V1：

```text
不做
```

先完成：

```text
Keyword
+
Vector
+
RRF
```

为什么？

因为你首先应该证明：

```text
Hybrid
是否比 Vector Only 好？
```

如果一开始就加入：

```text
Reranker
```

最终结果变好了，你反而不知道是：

```text
RRF 有效果

还是

Reranker 有效果
```

这会破坏 Evaluation 的可解释性。

正确实验顺序应该是：

```text
Baseline 1
Vector Only

Baseline 2
Keyword Only

Experiment
Keyword + Vector + RRF

P1
+ Reranker
```

这样每一步都能量化归因。

---

# 24. Reranker 有哪些真实工程坑？

原调研里这里很有价值。

第一：

```text
不同 Reranker
输出 score 尺度可能不同
```

有些模型输出：

```text
0 ~ 1
```

有些可能输出：

```text
负数 logits
```

如果后面还要：

```text
加权
阈值过滤
```

就会出问题。

所以可能需要：

```text
Normalization
```

但也不能：

```text
所有模型都无脑 Min-Max
```

因为原本已经校准到 `[0,1]` 的模型被重新归一化后，绝对阈值的意义会发生改变。原调研把这总结成 DevContext P1 接入 rerank 时必须处理的分数量纲问题。

---

# 25. 第二个 Rerank 坑：输入必须是原始文本

你可能为了 FTS 建了一份：

```text
tokenized text
```

例如：

```text
purchase ticket service token bucket
```

这种数据适合搜索引擎。

但不能把这个东西直接喂给 Neural Reranker。

Reranker 应该看：

```text
原始自然代码/文档内容
```

例如：

```java
@Transactional
public void purchaseTicket(...) {
    ...
}
```

或者：

```markdown
## 为什么调整事务边界
...
```

RAGFlow 源码特别强调：reranker 应吃自然文本，而不是词干化、拆词后的索引文本，否则相关性分数可能被明显压低。

这个经验以后非常实用：

```text
Index Representation
≠
Model Input Representation
```

---

# 26. 到这里，完整 Retrieval Pipeline 应该怎么理解？

你现在可以把 RAG Retrieval 看成：

```text
                 Query

                   ↓

            Query Analysis

                   ↓

      ┌────────────┴────────────┐

      ↓                         ↓

Keyword Retrieval         Vector Retrieval

      ↓                         ↓

Top-N                      Top-N

      └────────────┬────────────┘

                   ↓

             Candidate Set

                   ↓

                  RRF

                   ↓

          [Optional Rerank]

                   ↓

               Final Top-K

                   ↓

            Context Builder

                   ↓

                  LLM
```

这比最开始：

```text
Question
↓
Embedding
↓
Vector DB
↓
LLM
```

已经完整很多。

这就是 RAGFlow 调研真正带来的认知升级。

---

# 27. 为什么 Retrieval Evaluation 是整个 RAGFlow 调研最值得学的一部分？

这是整个项目里非常重要的一点。

假设：

```text
Question
↓
Retriever
↓
LLM
↓
Answer
```

最后答案错了。

你怎么知道错在哪里？

可能是：

```text
A.
Retriever 根本没找到正确 Chunk
```

也可能是：

```text
B.
Retriever 找对了，
但 LLM 理解错了
```

也可能：

```text
C.
Retriever 找到了正确内容，
但是正确 Chunk 排在第 20，
Context Window 没放进去
```

如果你只看最终 Answer：

```text
你根本区分不了。
```

所以必须把 Retrieval 单独拿出来测。

---

# 28. RAGFlow 官方也明确这样做

RAGFlow 提供：

```text
Retrieval Testing
```

官方建议是：

```text
先测试 Retrieval

确认：
正确 Chunk 有没有召回
内容是否完整
排序是否合理

然后再去 Chat / Search / Agent
```

它甚至给出了非常清晰的故障定位逻辑：

```text
答案不好

├── 正确 Chunk 没找到
│
│   → 查：
│      Parsing
│      Chunk
│      Metadata
│      Retrieval 参数
│
└── 正确 Chunk 已找到

    → 查：
       Model
       Prompt
       Application
```

这正是为什么 Retrieval 必须独立于 LLM Generation 进行测试。

---

# 29. 这对你理解 RAG 非常重要

以后不要说：

> 我的 RAG 回答效果不好。

这是一个过于模糊的描述。

应该拆成：

```text
Parsing Quality

Chunk Quality

Retrieval Recall

Ranking Quality

Context Quality

Generation Quality
```

例如：

```text
Recall@5 = 95%

但是最终回答仍然差
```

那么问题大概率不在 Retriever。

相反：

```text
Recall@5 = 40%
```

那你再换 Prompt 基本没有意义。

因为正确知识根本没有进 Context。

---

# 30. Retrieval Test 和 Benchmark 不是一回事

这是原调研一个非常重要的发现。

RAGFlow 的：

```text
Retrieval Test
```

本质是：

```text
输入一个 Question

↓

看看返回哪些 Chunk

↓

人工判断
```

它没有：

```text
Ground Truth
Recall@K
MRR
```

也就是说它更像：

```text
调试工具
```

而不是：

```text
量化实验系统
```

但是 RAGFlow 另外还有独立 benchmark 脚本，会在公开数据集上计算 nDCG、MAP、MRR。

这告诉我们：

```text
Manual Retrieval Debugging

和

Quantitative Evaluation

是两个东西。
```

---

# 31. DevContext 为什么必须自己做 Benchmark？

因为你的问题不是：

> RAGFlow 在 MS MARCO 上表现怎么样？

而是：

> **我把 DevContext 从 Vector Only 改成 Hybrid + RRF 后，在 my12306 上到底有没有变好？**

这只能用你自己的数据测。

例如建立 30 个问题：

```text
Q1:
purchaseTicket 方法在哪里？

Ground Truth:
TicketServiceImpl.java
purchaseTicket
Lines 120-188
```

```text
Q2:
为什么使用 Token Bucket？

Ground Truth:
TokenBucket设计.md
Section: ...
```

```text
Q3:
为什么 Feign 调用要移出事务，
具体改了哪些代码？

Ground Truth:
事务设计.md
+
TicketServiceImpl.java
```

然后分别跑：

```text
Vector Only

Keyword Only

Hybrid + RRF
```

比较：

```text
Recall@3
Recall@5
MRR
Latency
```

这才是真正的 Evaluation。

---

# 32. Recall@K 应该怎样理解？

例如问题：

```text
Token Bucket 在哪里调用？
```

正确 Chunk：

```text
purchaseTicket METHOD
```

Retriever Top 5：

```text
1. A
2. B
3. purchaseTicket
4. D
5. E
```

那么：

```text
Recall@5 = Hit
```

因为正确答案出现在前 5。

如果：

```text
Top 5 都没有
```

就是：

```text
Miss
```

30 个问题中：

```text
27 个 Hit
```

那么：

```text
Recall@5
=
27 / 30
=
90%
```

这是：

> Retriever 有没有把正确知识找回来？

---

# 33. MRR 又解决什么问题？

Recall 只问：

```text
找到了吗？
```

但不关心：

```text
排第几？
```

例如：

系统 A：

```text
正确答案总在第 1
```

系统 B：

```text
正确答案总在第 5
```

它们：

```text
Recall@5
```

可能完全一样。

但显然 A 更好。

所以需要：

```text
MRR
```

它会奖励：

```text
正确答案排名更靠前
```

所以：

```text
Recall
=
有没有召回

MRR
=
排得好不好
```

这两个一起看非常适合 DevContext。

---

# 34. 一个很重要的 Evaluation 细节：评测时不要先用阈值过滤

假设 Retriever 已经返回：

```text
正确 Chunk
排名第 4
```

但因为：

```text
similarity < 0.5
```

被阈值过滤掉。

最后你看到：

```text
没召回
```

你无法判断：

```text
搜索本身没找到

还是

阈值把它删了
```

所以 RAGFlow benchmark 会把：

```text
similarity_threshold = 0
```

先拿到完整 Ranking，

然后再计算指标。原调研明确认为 DevContext 的 Recall@K 评测也应该采取同样做法。

这个思想非常重要：

> **Evaluation 先测 Retriever 原始能力，再测后处理策略。**

不要把多种因素混起来。

---

# 35. Evaluation 必须调用真正的生产 Retriever

不要写一个：

```text
evaluationRetriever
```

然后生产环境用：

```text
realRetriever
```

否则你测的根本不是生产行为。

RAGFlow benchmark 调用的就是线上实际使用的 Retrieval 逻辑。

DevContext 也应该：

```text
Production:

retriever.retrieve(query)

Evaluation:

retriever.retrieve(query)
```

区别只是 Evaluation 后面做：

```text
Ground Truth Comparison
```

而不是：

```text
LLM Generation
```

原调研把这一点作为 DevContext 应直接借鉴的设计原则。

---

# 36. 为什么 Retrieval Result 应该展示“分解后的分数”？

如果最终只返回：

```text
score = 0.031
```

你很难知道它为什么排在这里。

更好的结果应该包含：

```text
chunk_id

keyword_rank:
2

vector_rank:
5

rrf_score:
...

source:
TicketServiceImpl.java
```

这样如果一个结果很奇怪：

```text
Keyword Rank = 1
Vector Rank = 50
```

你立刻知道：

> 它主要是被精确字符串拉上来的。

如果：

```text
Keyword Rank = 无
Vector Rank = 1
```

说明：

> 它主要是语义召回。

RAGFlow 本身会把总体、向量和 term 相关性分别暴露出来用于调试。原调研因此建议 DevContext 输出 `fts_rank / vector_rank / rrf_score`。

这个功能对你的项目展示也非常有价值。

因为面试时你不只是说：

```text
我实现了 Hybrid Search。
```

而是能展示：

```text
这个 Query：

Keyword 排第 2
Vector 排第 7
RRF 后排第 1
```

这就真正证明你理解了 Retrieval。

---

# 37. LangGraph Router 和 RAGFlow 有什么关系？

RAGFlow允许通过各种参数调：

```text
Vector Weight
minimum_should_match
threshold
...
```

不同 Query 可能需要不同设置。

例如：

```text
RDelayedQueue
```

应该：

```text
更相信 Keyword
```

而：

```text
为什么订单会自动关闭？
```

应该：

```text
更相信 Semantic
```

RAGFlow 在部分地方通过权重和联动规则来控制：

```text
全文侧到底应该多严格
```

DevContext 则打算把这个判断提到更高层：

```text
LangGraph Router
```

例如：

```text
Query

↓

CODE
→ 更强调 Symbol / Keyword

DOC
→ 更强调 Semantic

MIXED
→ 两路都走
```

这是一个很重要的区别。

DevContext 不希望让用户自己调：

```text
vectorWeight = 0.38
minMatch = 0.27
```

而是希望系统根据问题类型：

```text
自动决定 Retrieval Strategy
```

原调研也把 RAGFlow 中参数联动的设计作为 DevContext Router 的一个额外论据。

---

# 38. RAGFlow 哪些复杂能力现在不要学？

下面这些看到名字即可：

```text
多 Doc Engine

自研 Tokenizer

中日韩分词

Synonym Dictionary

PageRank

Tag Rank Feature

Auto-keyword

Auto-question

Cross-language Retrieval

Knowledge Graph

复杂 Parent-Child Chunk
```

为什么？

因为这些都是：

```text
RAGFlow 产品规模
```

产生的复杂度。

而 DevContext 当前只有：

```text
Java
Markdown
PostgreSQL
my12306
```

所以不要因为 RAGFlow 做了：

```text
6 个 Search Engine
14 个 Chunker
```

就觉得自己的项目“太简单”。

恰恰相反。

一个好的学习项目应该：

> 把复杂系统里最关键的思想提炼出来，再用最简单的方式实现。

原调研最终明确不采用多 chunker 注册体系、自研 tokenizer、多引擎抽象、加权求和、PageRank/Tag、LLM 内容增强、GraphRAG 等复杂能力。

---

# 39. RAGFlow 最值得 DevContext 借鉴的内容

如果压缩成真正值得记住的东西，我认为只有下面几条。

第一：

```text
不同内容
→ 不同 Chunk Strategy
```

第二：

```text
Keyword
和
Vector
解决不同问题
```

第三：

```text
字段的重要性不同
→ 应该加权
```

第四：

```text
Recall
和
Ranking
应该分开
```

第五：

```text
Retriever
和
Reranker
职责不同
```

第六：

```text
Retrieval
必须独立于 Generation 测试
```

第七：

```text
Evaluation
必须有 Ground Truth 和指标
```

第八：

```text
Evaluation
必须走和生产相同的 Retrieval Pipeline
```

这些才是 DevContext 真正需要带走的知识。

---

# 40. 经过三轮调研后，DevContext 的架构现在为什么越来越清晰？

现在我们可以从源头重新走一遍。

## 第一步：Repository

```text
my12306
```

---

## 第二步：Parsing

Markdown：

```text
Heading Parser
```

Java：

```text
JavaParser
```

---

## 第三步：Chunk

Markdown：

```text
Document Section
```

Java：

```text
METHOD
CLASS Summary
CONSTRUCTOR
```

---

## 第四步：Metadata

```text
file
module
package
class
method
annotations
signature
heading
lines
```

---

## 第五步：Index

同一份：

```text
knowledge_chunk
```

建立：

```text
FTS
+
Vector
```

---

## 第六步：Query Routing

```text
DOC

CODE

MIXED
```

---

## 第七步：Recall

```text
Keyword Top-N
+
Vector Top-N
```

---

## 第八步：Fusion

```text
RRF
```

---

## 第九步：P1 Optional

```text
Reranker
```

---

## 第十步：Context Builder

拼装：

```text
content
metadata
citation
```

---

## 第十一步：LLM

```text
Answer
+
Citation
```

---

## 第十二步：Evaluation

```text
Recall@3
Recall@5
MRR
Latency
Citation Accuracy
```

这就是三个项目的调研最后共同指向的完整体系。

---

# 41. 从“基础 RAG”到“工程 RAG”，你现在的认知应该发生什么变化？

最开始：

```text
RAG
=
Embedding
+
Vector DB
```

现在应该升级成：

```text
RAG
=
Parsing

+
Chunk Design

+
Metadata Design

+
Index Design

+
Keyword Retrieval

+
Vector Retrieval

+
Hybrid Recall

+
Rank Fusion

+
Optional Rerank

+
Context Selection

+
Citation

+
Evaluation
```

Embedding 只是其中：

```text
Vector Retrieval
```

的一部分。

这也是为什么 DevContext 是一个非常适合继续深入 Agent / RAG 的学习项目。

它不是再做一个：

```text
上传 PDF
→ 问答
```

Demo。

而是在研究：

> **LLM 在回答之前，到底怎样获得正确 Context。**

---

# 42. RAGFlow 与 Agent 开发到底有什么关系？

你可能会觉得：

```text
RAGFlow
```

还是传统 RAG，

和：

```text
Agent
```

有什么关系？

实际上 Agent 最关键的问题之一就是：

```text
下一步应该拿什么 Context？
```

Agent 可以调用：

```text
search_docs
search_code
read_file
grep
database
web
```

但工具再多，最终还是一个问题：

> **怎样找到当前任务最有价值的信息？**

DevContext 现在研究的：

```text
Query Analysis
Routing
Retrieval
Evaluation
Retry
```

已经开始接近：

```text
Agentic Retrieval
```

例如：

```text
Question

↓

第一次 Retrieval

↓

Context Evaluation

↓

Enough?
   │
   ├── Yes → Answer
   │
   └── No
        ↓
    Rewrite Query
        ↓
     Retrieve Again
```

这已经不是固定：

```text
Question → Vector DB
```

了。

而是：

```text
根据状态调整检索行为
```

这就是 LangGraph 后面真正应该发挥作用的地方。

---

# 43. RAGFlow 调研以后，LangGraph 的价值也更容易理解

如果只是：

```text
Question
↓
Embedding
↓
Search
↓
Answer
```

其实根本不需要 LangGraph。

普通函数调用就够了。

真正需要 LangGraph 是出现：

```text
Query Type

Routing

Multiple Retriever

Context Evaluation

Retry

Query Rewrite

State
```

例如：

```text
START

↓

analyze_query

↓

DOC / CODE / MIXED

↓

retrieve

↓

context enough?

├─ yes
│   ↓
│ answer
│
└─ no
    ↓
 rewrite
    ↓
 retrieve
```

到这里：

```text
State
Node
Conditional Edge
Cycle
```

才真正有意义。

所以你现在不应该“为了使用 LangGraph 而使用 LangGraph”。

而应该等 Retrieval Pipeline 需要：

```text
决策
+
状态
+
重试
```

以后再引入。

---

# 44. DevContext V1 的 Retrieval 部分最终应该做到什么？

V1 真正需要的其实非常有限：

```text
1.
Keyword Search

2.
Vector Search

3.
两路各取 Top-N

4.
RRF

5.
返回 Top-K

6.
保存：
keyword rank
vector rank
rrf score

7.
Citation

8.
Evaluation
```

暂时不要：

```text
Cross Encoder

Parent-child Retrieval

Synonym Expansion

Auto Question

Auto Keyword

PageRank

Knowledge Graph
```

你需要首先完成：

```text
最小但完整的 Retrieval 闭环
```

---

# 45. DevContext P1 再增加什么？

原调研里真正值得 P1 考虑的主要是：

```text
pg_trgm
```

解决：

```text
Ticket
→ TicketServiceImpl
```

这种代码 Identifier 子串匹配。

然后：

```text
Cross Encoder Rerank
```

验证：

```text
RRF
+
Rerank
```

是否继续提升。

以及：

```text
检索分数可视化
失败案例分析
```

这些都是：

```text
直接服务 Retrieval Quality / Evaluation
```

的能力，而不是为了堆技术栈。

---

# 46. 这份调研里一个特别值得注意的地方：不要盲目认为 RAGFlow 的方案就是“更先进”

例如 RAGFlow 使用：

```text
weighted_sum
```

DevContext 使用：

```text
RRF
```

不能得出：

```text
RAGFlow 更成熟
所以 DevContext 应该改成 weighted_sum
```

正确的推理应该是：

```text
RAGFlow 为什么这么做？

↓

因为产品需要连续调节 vector weight

↓

为了连续调权
它必须统一两种 score 的尺度

↓

DevContext 是否也需要这个？

↓

不需要

↓

那么 RRF 更简单
更容易实验
```

这就是：

```text
Engineering Decision
```

而不是：

```text
Copy Open Source
```

---

# 47. 另一个例子：为什么不照搬 RAGFlow Tokenizer？

RAGFlow 有：

```text
自研 Tokenizer
```

看起来很高级。

但为什么有？

因为它需要支持：

```text
中文
英文
多语言
多个 Search Engine
```

它希望：

```text
ES
Infinity
OceanBase
...
```

行为尽可能一致。

DevContext：

```text
PostgreSQL Only
```

因此：

```text
PostgreSQL FTS
+
simple dictionary
+
pg_trgm
```

已经够了。

所以：

> 技术选型不是看谁功能最多，而是看问题边界。

---

# 48. 你现在真正需要掌握的 10 个问题

如果下面 10 个问题你能解释，RAGFlow 调研对你来说就已经完成。

### 1. 为什么没有统一 Chunk Size？

因为：

```text
不同信息类型的天然语义边界不同。
```

---

### 2. 为什么代码和 Markdown 要不同 Chunker？

因为：

```text
Markdown 的结构单位是 Heading/Section，
Java 的结构单位是 Method/Class。
```

---

### 3. 为什么不能只有 Vector Search？

因为：

```text
Identifier / Symbol
需要精确字面匹配。
```

---

### 4. 为什么不能只有 Keyword Search？

因为：

```text
自然语言问题和文档/代码用词可能完全不同。
```

---

### 5. 为什么需要 Hybrid？

因为：

```text
一个 Java 项目同时存在
Symbol Query
+
Semantic Query。
```

---

### 6. 为什么 DevContext 使用 RRF？

因为：

```text
Keyword 与 Vector
原始 Score 不在同一评分空间，
RRF 按 Rank 融合，
不需要归一化。
```

---

### 7. Reranker 是做什么的？

```text
Retriever：
快速找候选

Reranker：
在小候选集上精细排序
```

---

### 8. 为什么 Retrieval 要单独 Evaluation？

因为：

```text
否则无法区分
Retrieval Error
和
Generation Error。
```

---

### 9. Recall@K 和 MRR 的区别？

```text
Recall@K：
找没找到

MRR：
排得靠不靠前
```

---

### 10. 为什么 Evaluation 必须复用生产 Retriever？

因为：

```text
否则测出来的不是实际系统行为。
```

如果这十个问题你能够自己回答：

> 就已经可以停止继续阅读 RAGFlow 源码，进入开发。

---

# 49. 三份调研最终应该形成的统一认知

现在可以真正把 Continue、JavaParser、RAGFlow 合起来。

## Continue

告诉你：

```text
Repository
↓
Chunk
↓
Index
↓
Retrieval
```

为什么要分层。

---

## JavaParser

告诉你：

```text
Java Source
↓
AST
↓
Method / Class
↓
Code Chunk
```

怎样生成高质量代码知识。

---

## RAGFlow

告诉你：

```text
Chunk
↓
Keyword + Vector
↓
Fusion
↓
Rerank
↓
Evaluation
```

怎样把知识找到。

---

最终：

```text
                    my12306

                       ↓

            ┌──────────┴──────────┐

            ↓                     ↓

       Markdown Docs          Java Source

            ↓                     ↓

      Heading Parser          JavaParser

            ↓                     ↓

      Document Chunk          Code Chunk

            └──────────┬──────────┘

                       ↓

                knowledge_chunk

                       ↓

            ┌──────────┴──────────┐

            ↓                     ↓

        PostgreSQL FTS         pgvector

            ↓                     ↓

         Keyword               Vector

            └──────────┬──────────┘

                       ↓

                      RRF

                       ↓

                [P1 Reranker]

                       ↓

                 Context Builder

                       ↓

               Context Evaluation

                 /           \

              Enough       Not Enough

                │              │
                │          Query Rewrite
                │              │
                └──────◄───────┘

                       ↓

                      LLM

                       ↓

              Answer + Citation
```

这就是 DevContext 的完整学习价值。

---

# 50. 最终你真正学的不是三个开源项目

表面上你在研究：

```text
Continue
JavaParser
RAGFlow
```

但真正学到的是三类工程能力：

```text
Continue
↓
Architecture Thinking
```

```text
JavaParser
↓
Knowledge Representation
```

```text
RAGFlow
↓
Retrieval Engineering
```

最后汇总起来就是：

```text
Context Engineering
```

也就是：

> **不是单纯研究“模型怎样回答”，而是研究“在模型回答之前，什么信息应该进入 Context、这些信息从哪里来、怎样组织、怎样找到、怎样验证它找对了”。**

这才是 DevContext 和普通 RAG Demo 最本质的区别。

---

# 51. 你接下来不需要继续扩大调研范围

基于这三份调研，现在已经足够进入开发。

继续研究第四、第五个大型开源项目的边际收益已经很低。

因为最核心的问题都已经找到参考答案：

```text
Repository 怎么组织？
→ Continue

Java 怎么 Chunk？
→ JavaParser

Retrieval 怎么做？
→ RAGFlow
```

接下来真正能让你理解这些知识的，不是继续阅读源码，而是：

```text
自己实现

↓

跑 my12306

↓

建立 Benchmark

↓

发现 Retrieval 失败案例

↓

修改 Chunk / Metadata / Retriever

↓

重新 Evaluation
```

这一步开始以后，你才会真正理解：

```text
为什么 method_name 要加权？

为什么 Vector 会漏掉 Identifier？

为什么 RRF 能把一个结果拉上来？

为什么 Chunk 太大会影响 Embedding？

为什么 Retrieval Evaluation 很重要？
```

这些问题靠继续读十份报告都不如自己跑一次实验理解得深。
