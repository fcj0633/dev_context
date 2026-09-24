可以。你现在最需要的不是“怎么写 Context Builder 代码”，而是先理解：

> **为什么 Retriever 已经能返回结果了，还需要再多一层 Context Builder？**

如果这一点没有理解清楚，后面看到 `ContextBundle`、`Citation`、去重、预算这些概念就会觉得像在“为了设计而设计”。

我从业务流程开始重新解释。

# 一、Context Builder 在整个项目中到底处于什么位置

你现在的 DevContext 大致是：

```text
用户问题
   ↓
Retriever
   ↓
SearchResult[]
   ↓
Context Builder
   ↓
ContextBundle
   ↓
LLM
   ↓
最终回答
```

可以把这几层理解成不同角色。

### Retriever 的职责

回答：

> **“知识库里哪些内容可能和这个问题相关？”**

例如用户问：

```text
购票事务是如何实现的？
```

Retriever 可能返回：

```text
1. PurchaseTicketTxService.doPurchaseInTransaction
2. PurchaseTicketServiceImpl.purchaseTickets
3. 事务设计.md / 事务边界章节
4. PurchaseTicketTxService 类 Chunk
5. PurchaseTicketService 接口
```

Retriever 到这里就结束了。

它只负责：

```text
找候选
+
排序
```

它不会考虑：

```text
这些结果是不是重复？
总共太长怎么办？
LLM最终应该看到哪些？
代码和文档怎么组织？
回答时怎么引用？
```

这些正是 Context Builder 的工作。

---

# 二、Context Builder 的业务角色是什么

一句话：

> **Context Builder 是“检索结果”和“LLM真正看到的上下文”之间的整理层。**

可以类比成：

```text
Retriever = 图书管理员帮你找书

Context Builder = 助理从这些书里挑出真正要看的几页，
整理好顺序、标好出处，然后交给你

LLM = 根据这几页资料回答问题
```

Retriever 返回的是：

```text
候选材料
```

Context Builder 输出的是：

```text
最终证据包
```

这两个概念非常重要。

---

# 三、为什么不能直接把 SearchResult[] 全部塞给 LLM

假设 Retriever 返回 Top 5：

```text
1. PurchaseTicketTxService 类 Chunk
2. doPurchaseInTransaction 方法 Chunk
3. purchaseTickets 方法 Chunk
4. PurchaseTicketService 接口
5. 事务设计文档
```

表面上看：

> 直接拼起来给 LLM 不就行了？

问题很多。

## 问题 1：重复

例如：

```text
CLASS Chunk：
包含整个 PurchaseTicketTxService 的大量代码

METHOD Chunk：
又包含 doPurchaseInTransaction
```

那么同一个方法可能出现两次。

LLM 得到：

```text
同样代码
同样代码
同样代码
```

不仅浪费 Token，还可能让某一部分证据权重过高。

---

## 问题 2：长度失控

Top 5 不代表一定短。

可能：

```text
Chunk1 = 3000字符
Chunk2 = 5000字符
Chunk3 = 4000字符
...
```

最后：

```text
20000~30000字符
```

以后接模型时可能：

- 占满 Context Window；
- 成本上升；
- 延迟增加；
- 真正重要的证据被大量无关内容淹没。

所以必须有：

```text
Context Budget
```

---

## 问题 3：来源不完整

对于 MIXED 问题：

```text
为什么这样设计？
代码怎么实现？
```

Retriever Top 5 可能全部是 CODE：

```text
CODE
CODE
CODE
CODE
CODE
```

虽然每一条都相关，但 LLM 没有设计文档。

最后它只能：

> 根据代码猜为什么这么设计。

这正是你想避免的。

Context Builder 可以在上游已经判断为 MIXED 时保证：

```text
至少保留一个 CODE
+
至少保留一个 DOC
```

---

## 问题 4：没有引用信息

如果只给 LLM：

```text
public void purchaseTickets() {
   ...
}
```

模型回答：

> 购票事务在这里执行……

用户会问：

> “哪里？”

如果没有额外信息，模型不知道：

```text
文件是什么
第几行
哪个类
哪个章节
```

所以必须在 Context 中带 Citation。

---

# 四、所以 Context Builder 本质上干什么

可以压缩成 5 件事：

```text
Retriever 返回候选
        ↓
1. 去重
        ↓
2. 选择哪些结果进入 Context
        ↓
3. 控制总长度
        ↓
4. 给每块内容附上出处
        ↓
5. 格式化成 LLM 容易理解的文本
```

最后变成：

```text
ContextBundle
```

---

# 五、先解释你最不清楚的：什么叫“去重”

## 1. 最简单的重复：同一个 Chunk 出现两次

例如 Keyword 搜到：

```text
chunk_id = 123
```

Vector 也搜到：

```text
chunk_id = 123
```

Hybrid 合并之后如果处理不好，可能会出现：

```text
123
123
```

这就是最简单的重复。

Context Builder 应该：

```text
只保留一份
```

所以 V1 最简单：

```text
seen_chunk_ids = set()
```

遇到同一个：

```text
chunk_id
```

第二次跳过。

---

# 六、但还有一种“不是同一个 Chunk，却高度重复”

例如：

```text
Result 1：
CLASS Chunk
PurchaseTicketTxService 整个类
```

里面包含：

```text
doPurchaseInTransaction()
```

然后：

```text
Result 2：
METHOD Chunk
doPurchaseInTransaction()
```

它们：

```text
chunk_id 不同
```

所以严格意义上不是重复。

但是给 LLM 看：

> 内容重复很多。

这叫：

```text
语义冗余
```

不过我之前建议：

> **V1 先不要解决。**

因为这会牵扯：

```text
CLASS和METHOD谁优先？
重叠多少算重复？
保留父Chunk还是子Chunk？
```

很容易变复杂。

所以 Context Builder V1 的去重只需要理解成：

> **不要把完全相同的检索 Chunk 重复塞进去。**

就够了。

---

# 七、Citation 到底是什么

Citation 不是内容。

它是：

> **“这段内容从哪里来的”的结构化描述。**

例如一个 Java Chunk：

```text
PurchaseTicketTxService.java
doPurchaseInTransaction()
69-118行
```

可以变成：

```text
Citation:
citation_id = C1
file_path = src/.../PurchaseTicketTxService.java
symbol = doPurchaseInTransaction
start_line = 69
end_line = 118
```

它相当于：

```text
这份证据的身份证
```

---

# 八、为什么不能只保留 file_path

因为：

```text
PurchaseTicketTxService.java
```

里面可能有 20 个方法。

如果你只告诉用户：

> 来源：PurchaseTicketTxService.java

不够准确。

所以 CODE Citation 最理想是：

```text
File
Class
Symbol
Lines
```

例如：

```text
[C1]
src/main/java/.../PurchaseTicketTxService.java
PurchaseTicketTxService.doPurchaseInTransaction(...)
Lines 69-118
```

这样未来回答：

```text
购票事务在 PurchaseTicketTxService.doPurchaseInTransaction 中执行 [C1]
```

用户就知道去哪看。

---

# 九、Markdown Citation 为什么不强调行号，而强调 heading_path

Markdown 更适合：

```text
文件
+
章节路径
```

比如：

```text
docs/订单与支付模块.md
订单模块 > 事务设计 > 事务边界
```

因为文档结构的真实语义不是：

```text
第 320-368 行
```

而是：

```text
“事务边界”这一节
```

所以 DOC Citation：

```text
[C2]
File: docs/订单与支付模块.md
Section: 订单模块 > 事务设计 > 事务边界
```

比：

```text
Lines 320-368
```

对用户更有意义。

---

# 十、Citation 的作用不只是“给用户看”

它还有三个重要作用。

## 1. 给 LLM 引用

可以要求模型：

```text
回答中的结论必须引用 [C1] [C2]
```

---

## 2. 以后验证 Citation

你未来可以检查：

```text
LLM说用了 [C2]
```

那：

```text
[C2] 是否真的支持这句话？
```

这就是：

```text
Citation Verification
```

---

## 3. 调试

如果答案出错：

```text
模型引用了 C3
```

你马上能回到：

```text
C3 对应哪个 Chunk
```

定位问题。

---

# 十一、ContextItem 又是什么

可以理解为：

> **一块准备交给 LLM 的“证据单元”。**

它包含两类数据。

第一类：

```text
真正给模型看的内容
```

例如：

```python
content
citation
```

第二类：

```text
系统内部调试信息
```

例如：

```python
retrieval_mode
score
keyword_rank
vector_rank
```

所以：

```python
ContextItem
```

本质是：

```text
一个 SearchResult
经过整理后
变成一个正式的 Context 证据
```

---

# 十二、举一个 ContextItem 例子

Retriever 原始结果：

```text
SearchResult
id=123
file_path=PurchaseTicketTxService.java
class_name=PurchaseTicketTxService
symbol=doPurchaseInTransaction
start_line=69
end_line=118
content=...
score=0.032
```

Context Builder 把它转成：

```text
ContextItem
│
├─ Citation
│   ├─ C1
│   ├─ PurchaseTicketTxService.java
│   ├─ doPurchaseInTransaction
│   └─ 69-118
│
├─ content
│   └─ 方法源码
│
└─ retrieval metadata
    ├─ mode=hybrid
    └─ score=...
```

---

# 十三、ContextBundle 是什么

这是另一个核心概念。

ContextBundle 可以直接理解成：

> **一次用户问题最终准备好的“整包上下文”。**

例如用户问：

```text
为什么购票要先经过令牌桶，事务在哪里执行？
```

最后 Context Builder 可能选：

```text
C1：purchaseTickets 方法
C2：doPurchaseInTransaction 方法
C3：令牌桶设计文档
C4：事务设计文档
```

这 4 个：

```text
ContextItem
```

放在一起，就是：

```text
ContextBundle
```

---

# 十四、为什么不直接返回 List<ContextItem>

因为一个完整 Context 还需要描述：

```text
这是哪个 Query 的 Context？
最终格式化文本是什么？
有多长？
有没有被截断？
```

所以：

```python
ContextBundle
```

可能是：

```python
ContextBundle(
    query="为什么购票要先经过令牌桶？",
    items=[C1, C2, C3],
    rendered_text="...",
    total_chars=10240,
    truncated=False
)
```

你可以把它理解成：

```text
LLM 请求之前的最终包裹
```

---

# 十五、`rendered_text` 又是什么

虽然程序内部保存的是：

```text
ContextItem对象
Citation对象
```

但 LLM 最终接收的是文本。

所以需要把结构化对象渲染成：

```text
[C1] Java source
File: src/.../PurchaseTicketTxService.java
Symbol: doPurchaseInTransaction(...)
Lines: 69-118

public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}


[C2] Documentation
File: docs/购票设计.md
Section: 购票系统 > 余票控制 > 令牌桶

令牌桶的主要作用是……
```

这一整段字符串就是：

```text
rendered_text
```

最终 LLM Prompt：

```text
Question:
为什么购票需要令牌桶？

Context:
{bundle.rendered_text}
```

---

# 十六、为什么还要做“预算控制”

Context Builder 的另一个非常核心的任务。

假设 Retriever：

```text
Top10
```

总长度：

```text
40000字符
```

但是你希望：

```text
最多12000字符
```

Context Builder 就需要：

```text
从排名高的开始加入
```

直到预算满。

例如：

```text
C1 = 3000
C2 = 2500
C3 = 4000
C4 = 2000
----------------
总计11500
```

然后 C5：

```text
3000
```

放进去就超过：

```text
14500
```

于是：

```text
不放 C5
```

最终：

```text
ContextBundle.total_chars = 11500
```

---

# 十七、为什么需要预算，不让 LLM 全看就行了吗

因为“更多 Context”不一定更好。

太多内容可能导致：

```text
1. 成本更高
2. 响应更慢
3. 无关信息变多
4. 重要信息被淹没
5. 模型更容易混淆
```

所以 Context Engineering 的核心之一就是：

> **不是把所有资料给模型，而是把最需要的资料给模型。**

这其实正是你项目的价值所在。

---

# 十八、“代码和文档平衡”到底是什么意思

不是机械地：

```text
50% CODE
50% DOC
```

而是保证问题真正需要的来源别被挤掉。

例如纯 CODE：

```text
PurchaseTicketTxService.doPurchaseInTransaction 在哪里？
```

Context 完全可以：

```text
CODE
CODE
CODE
```

没问题。

但是 MIXED：

```text
为什么这样设计？
代码在哪里？
```

如果 Top5：

```text
CODE
CODE
CODE
CODE
CODE
```

就不好。

Context Builder 在得到：

```text
require_mixed_sources=true
```

之后，可以做：

```text
至少选一个 CODE
至少选一个 DOCUMENT
剩余位置按排名
```

比如：

```text
C1 CODE
C2 DOC
C3 CODE
C4 CODE
```

这样才能保证 LLM 同时看到：

```text
How
+
Why
```

---

# 十九、注意：谁来判断 MIXED？

这里是职责边界。

Context Builder **不负责判断**：

```text
这个问题是不是 MIXED
```

因为这属于：

```text
Query Router
```

未来流程：

```text
Query
 ↓
Router
 ↓
MIXED
 ↓
Retriever
 ↓
Context Builder(require_mixed_sources=true)
```

Context Builder 只是：

> “你告诉我需要两类证据，我负责尽量把两类都放进去。”

这是一个很重要的设计原则：

```text
Router 决定需求
Builder 执行上下文组织
```

---

# 二十、为什么 Context Builder 不负责 LLM

为了保持组件职责单一。

如果 Context Builder 里面既做：

```text
去重
预算
Citation
```

又做：

```text
Prompt
LLM API
生成答案
```

后面会非常难测试。

你就分不清：

```text
Context错了
```

还是：

```text
LLM回答错了
```

所以：

```text
Context Builder
```

只输出：

```text
ContextBundle
```

然后：

```text
Answer Generator
```

单独处理：

```text
ContextBundle + Question
↓
LLM
```

---

# 二十一、从业务上看，Context Builder 解决的真正问题是什么

你可以把整个项目分成三个业务问题：

### 问题一：去哪里找？

```text
Retriever
```

解决。

---

### 问题二：找到一堆东西后，到底给模型看什么？

```text
Context Builder
```

解决。

---

### 问题三：模型根据这些证据怎么回答？

```text
LLM Generator
```

解决。

所以：

```text
Retrieval ≠ Context Engineering
```

Retriever 只是 Context Engineering 的前半部分。

真正决定模型最终看到什么的，是 Context Builder。

---

# 二十二、最后 Context Builder V1 的完整业务流程

我建议你脑子里固定成这一张图：

```text
用户问题
  │
  ▼
Retriever
  │
  │ 返回 Top-K SearchResult
  ▼
SearchResult[]
  │
  ▼
Context Builder
  │
  ├── 1. 删除完全重复 Chunk
  │
  ├── 2. 根据来源要求选择结果
  │
  ├── 3. 根据长度预算继续筛选
  │
  ├── 4. 为每个结果创建 Citation
  │
  ├── 5. 转为 ContextItem
  │
  └── 6. 渲染最终文本
  │
  ▼
ContextBundle
  │
  ├── query
  ├── items
  ├── rendered_text
  ├── total_chars
  └── truncated
  │
  ▼
LLM
```

---

# 二十三、最终实际效果是什么

假设用户输入：

```text
为什么购票需要令牌桶？事务最终在哪里执行？
```

现在的系统只能输出类似：

```text
SearchResult(...)
SearchResult(...)
SearchResult(...)
```

Context Builder 完成以后，你可以看到：

```text
Query:
为什么购票需要令牌桶？事务最终在哪里执行？

Context: 10524 / 12000 chars

[C1] Java source
File: src/.../PurchaseTicketServiceImpl.java
Symbol: purchaseTickets(...)
Lines: 220-280

<代码>


[C2] Documentation
File: docs/购票设计.md
Section: 余票设计 > 令牌桶

<为什么使用令牌桶>


[C3] Java source
File: src/.../PurchaseTicketTxService.java
Symbol: doPurchaseInTransaction(...)
Lines: 69-118

<事务代码>


[C4] Documentation
File: docs/事务设计.md
Section: 购票流程 > 事务边界

<事务设计说明>
```

这个东西还不是最终答案。

但是你可以问自己：

> **如果我是 LLM，只看这几份资料，能不能回答用户问题？**

如果答案是“能”，Context Builder 就成功了。

---

# 二十四、所以 Context Builder V1 真正需要设计的功能只有这些

我建议你现在记住这 6 个，不要更多：

1. **输入 SearchResult[]**
2. **去掉完全重复 Chunk**
3. **根据预算选择哪些 Chunk**
4. **MIXED 情况下保证基本来源覆盖**
5. **为每个 Chunk 生成 Citation**
6. **生成 ContextBundle 和 rendered_text**

其他：

```text
相邻Chunk合并
语义去重
复杂tokenizer
Parent Context
Reranker
自动判断Query类型
LLM调用
```

都不是 V1 必须内容。

---

# 二十五、一句话理解几个核心名词

最后你可以这样记：

```text
SearchResult
= Retriever 找到的一条候选资料
```

```text
Citation
= 这条资料“从哪里来”的身份证
```

```text
ContextItem
= 准备真正交给 LLM 的一条证据
```

```text
ContextBundle
= 某一个问题最终准备好的整包证据
```

```text
rendered_text
= 把这整包证据变成 LLM 可以直接阅读的文本
```

```text
Context Builder
= 把“搜索结果”整理成“LLM最终上下文”的中间层
```

最核心的一句话就是：

> **Retriever 决定“找到什么”，Context Builder 决定“最终让模型看到什么”。**

这就是它在整个 DevContext 项目里的真正角色。