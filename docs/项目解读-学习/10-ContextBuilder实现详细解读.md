# Context Builder V1：实现、设计与真实输出详细解读

# 一、这一部分在系统中的位置

前面三个学习文档已经把"知识怎么进来"讲完了：

```text
01  项目搭建
02  JavaParser         →  Java 源码 → CodeChunk
03  Markdown 解析       →  Markdown  → DocumentChunk
04  Chunk 分块
05  Embedding 过程      →  chunk → 1024 维向量
06  chunk 存储和索引     →  PostgreSQL + pg_trgm + pgvector + RRF
08  评测部分
09  评测框架 v2
```

到 06 结束，系统已经能做到：

```text
用户 Query
   ↓
Hybrid Retrieval（keyword + vector + RRF）
   ↓
SearchResult[]
```

**但 `SearchResult[]` 还不能直接交给 LLM。**

Context Builder 就插在这里：

```text
用户 Query
   ↓
RetrievalService.search("hybrid", query, top_k)
   ↓
SearchResult[]
   ↓
┌──────────────────────────┐
│     Context Builder      │   ← 本部分
│  去重 / 选源 / 预算 / 引用  │
└──────────────────────────┘
   ↓
ContextBundle
   ↓
rendered_text
   ↓
LLM（下一阶段）
```

核心代码：

- 数据结构：[models.py](D:/Java-learning/DevContext/src/devcontext/models.py:107)
- Builder 实现：[builder.py](D:/Java-learning/DevContext/src/devcontext/context/builder.py:21)
- 对外导出：[context/__init__.py](D:/Java-learning/DevContext/src/devcontext/context/__init__.py:1)
- CLI 入口：[cli.py](D:/Java-learning/DevContext/src/devcontext/cli.py:113)
- 测试：[test_context_builder.py](D:/Java-learning/DevContext/tests/test_context_builder.py:1)、[test_context_cli.py](D:/Java-learning/DevContext/tests/test_context_cli.py:1)

本部分**没有新增任何运行时依赖**：没有 LLM、没有 LangGraph、没有 Router、没有 Query Rewrite、没有 Reranker。

---

# 二、为什么 Retriever 之后还需要一层

这是理解 Context Builder 最关键的一步。如果这一点不清楚，后面看到 `Citation`、去重、预算就会觉得像"为了设计而设计"。

## 1. Retriever 的职责到哪里为止

Retriever 只回答一个问题：

> **知识库里哪些内容可能和这个问题相关？**

它负责：

```text
找候选
+
排序
```

它**不负责**：

```text
这些结果是不是重复？
总共太长怎么办？
LLM 最终应该看到哪些？
代码和文档怎么组织？
回答时怎么引用？
```

## 2. 直接拼接 `SearchResult[]` 会出什么问题

假设 Hybrid Top 5 返回：

```text
1. PurchaseTicketTxService 类 Chunk
2. doPurchaseInTransaction 方法 Chunk
3. purchaseTickets 方法 Chunk
4. PurchaseTicketService 接口
5. 事务设计文档
```

看起来"直接拼起来给 LLM 就行了"，但有四个问题。

### 问题 1：重复

同一个 Chunk 可能被 Keyword 和 Vector 同时命中，融合后如果处理不当就会出现两次：

```text
chunk_id = 123
chunk_id = 123
```

LLM 会看到同样的内容两遍——既浪费 Token，也可能让某一段证据的权重被错误放大。

### 问题 2：长度失控

Top 5 不代表短。一个 CLASS Chunk 可能有 3000 字符，五个加起来可能到 20000～30000 字符。

后果：

```text
占满 Context Window
成本上升
延迟增加
真正重要的证据被淹没
```

### 问题 3：来源不完整

对于 MIXED 问题（既问"为什么这么设计"，又问"代码在哪"），Top 5 有可能全是 CODE：

```text
CODE
CODE
CODE
CODE
CODE
```

LLM 手里没有任何设计文档，只能根据代码**猜**设计意图——这正是 DevContext 要避免的。

### 问题 4：没有出处

如果只给 LLM 一段裸代码：

```java
public void purchaseTickets() {
   ...
}
```

模型只能说"购票事务在这里执行"。

用户接着问"哪里？"——模型答不出来，因为它不知道：

```text
文件是什么
第几行
哪个类
哪个章节
```

## 3. 所以 Context Builder 要做五件事

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
        ↓
ContextBundle
```

一个类比：

```text
Retriever       = 图书管理员帮你把所有相关的书找出来
Context Builder = 助理从这些书里挑出真正要看的几页，
                  排好顺序、标好出处，交给 LLM
LLM             = 根据这几页资料回答问题
```

所以：

```text
Retriever 返回的是  → 候选材料
Context Builder 输出的是 → 最终证据包
```

最核心的一句话：

> **Retriever 决定"找到什么"，Context Builder 决定"最终让模型看到什么"。**

---

# 三、四个核心数据结构

这四类结构在 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:106) 中定义，都提供 `to_dict()`，可以直接给日志、API 或后续 LLM 层序列化。

## 1. `SearchResult`：Retriever 的一条候选

这是**输入**，不是 Context Builder 定义的，而是检索层返回的。

```python
@dataclass(slots=True)
class SearchResult:
    id: int                          # 数据库主键，去重的依据
    source_type: str                 # CODE / DOCUMENT
    chunk_type: str                  # METHOD / CLASS / CONSTRUCTOR / DOCUMENT_SECTION
    file_path: str
    content: str
    start_line: int | None
    end_line: int | None
    class_name: str | None
    symbol_name: str | None
    signature: str | None
    title: str | None
    score: float                     # RRF 融合后的分数
    annotations: list[str]
    heading_path: list[str]
```

关键点：**`id` 是数据库主键**，这让去重变成一件非常简单的事（见第五节）。

## 2. `Citation`：一条证据的"身份证"

`Citation` 不是内容，而是"这段内容从哪里来"的结构化描述。

```python
@dataclass(slots=True)
class Citation:
    label: str                       # 最终引用编号：C1 / C2 / C3 ...
    source_type: str                 # CODE / DOCUMENT
    file_path: str
    class_name: str | None           # 仅 CODE
    symbol_name: str | None          # 仅 CODE
    signature: str | None            # 仅 CODE
    start_line: int | None           # 仅 CODE
    end_line: int | None             # 仅 CODE
    heading_path: list[str]          # 仅 DOCUMENT
```

### 为什么 CODE 和 DOCUMENT 的字段不一样

Java 侧需要精确到"哪个类的哪个方法、第几行"：

```text
File:      .../OrderServiceImpl.java
Symbol:    OrderServiceImpl#createTicketOrder
Signature: public String createTicketOrder(TicketOrderCreateReqDTO requestParam)
Lines:     81-155
```

因为一个 278 行的 `TicketServiceImpl.java` 里有几十个方法，只给文件名等于没给。

Markdown 侧则需要"哪一节"：

```text
File:    7-模块设计文档/1-订单支付-车票.md
Heading: 十八、第四条主链路：超时关单
```

因为文档的真实语义单位不是"第 320-368 行"，而是"超时关单这一节"。用户看 `Heading` 比看行号更容易理解。

## 3. `ContextItem`：一条准备交给 LLM 的证据

```python
@dataclass(slots=True)
class ContextItem:
    citation: Citation               # 出处
    content: str                     # 真正进入 Context 的正文
    chunk_id: int                    # 来源 Chunk（调试用）
    chunk_type: str                  # METHOD / CLASS / ...
    score: float                     # 检索分数
    retrieval_rank: int              # 原始检索排名
    truncated: bool = False          # 本条正文是否被预算截断
```

它包含两类数据：

```text
给模型看的      →  citation + content
系统内部调试的  →  chunk_id / chunk_type / score / retrieval_rank / truncated
```

### 一个容易混淆的点：`retrieval_rank` 和"在列表里的位置"不是一回事

`retrieval_rank` 记录**它原本排第几**；而它在 `bundle.items` 里的位置是**最终顺序**。

这两者在 MIXED 双源锚点调整后**会不一致**。这是有意的设计（见第六节），好处是：
即使顺序被调整，你仍然能查出"它原来排第几"。

## 4. `ContextBundle`：一整包上下文

```python
@dataclass(slots=True)
class ContextBundle:
    query: str
    items: list[ContextItem]
    rendered_text: str               # 渲染后的完整文本
    total_chars: int                 # == len(rendered_text)
    max_chars: int                   # 本次预算
    truncated: bool                  # 是否发生预算截断
```

为什么不直接返回 `list[ContextItem]`？因为一个完整的 Context 还需要描述：

```text
这是哪个 Query 的 Context？
最终文本长什么样？
有多长？
有没有被截断？
```

## 5. `rendered_text`：给 LLM 看的最终文本

程序内部保存的是对象，但 LLM 最终接收的是**文本**。所以要把结构化对象渲染成字符串：

```text
[C1] CODE
File: services/order-services/src/main/java/.../OrderServiceImpl.java
Symbol: OrderServiceImpl#createTicketOrder
Signature: public String createTicketOrder(TicketOrderCreateReqDTO requestParam)
Lines: 81-155
Score: 0.031545

@Override
@Transactional(rollbackFor = Exception.class)
public String createTicketOrder(...) {
    ...
}
```

这一整段字符串就是 `rendered_text`，未来 LLM 的 Prompt 会是：

```text
Question:
{query}

Context:
{bundle.rendered_text}
```

---

# 四、完整执行流程

`ContextBuilder.build()` 的完整逻辑在 [builder.py:27-85](D:/Java-learning/DevContext/src/devcontext/context/builder.py:27)。

```text
build(query, results)
  │
  ├─ 0. 校验：query 不能是空白
  │
  ├─ 1. _deduplicate(results)
  │      └─ 按 result.id 去重，保留第一次出现
  │
  ├─ 2. 如果去重后为空 → 直接返回空 Bundle（truncated=False）
  │
  ├─ 3. _prioritize_sources(ranked_results)
  │      └─ 若同时存在 CODE 和 DOCUMENT，尝试把两个锚点提到最前
  │
  ├─ 4. 遍历 prioritized，逐条尝试放入预算
  │      ├─ 能完整放入        → 加入
  │      ├─ 放不下但能放一部分 → 截断正文 + 标记，然后 **break**
  │      └─ 连 Citation 都放不下 → 不加入，标记 truncated，**break**
  │
  └─ 5. 渲染 text → 组装 ContextBundle
```

有一个必须注意的细节：**第 4 步在遇到第一条放不下的结果时就 `break`**。

也就是说 Context Builder **不会**跳过大的结果去挑后面小的结果。原因是：

```text
检索结果的价值是按排名递减的。
如果跳过第 3 条去拿第 8 条，
等于用"能不能塞进预算"代替了"相关性"来做选择。
```

这是刻意的取舍：**保持排名顺序的语义**，而不是最大化填入的内容数量。

---

# 五、第一步：去重

## 1. 为什么去重键是 `id`

Hybrid 检索会把 Keyword 和 Vector 两路结果融合。同一个 Chunk 可能被两路同时命中：

```text
Keyword 命中 chunk_id = 123
Vector  命中 chunk_id = 123
```

`SearchResult.id` 就是数据库主键，所以判定"是不是同一条"变成一个集合查找问题：

```python
seen: set[int] = set()
for rank, result in enumerate(results, start=1):
    if result.id in seen:
        continue
    seen.add(result.id)
    unique.append(_RankedResult(retrieval_rank=rank, result=result))
```

## 2. 保留第一次出现，而不是保留分数最高的

代码是"遇到重复就跳过"，所以**保留的是第一次出现的那条**，连同它的 `score` 和 `retrieval_rank`。

为什么这样定？因为检索结果**本身就是按排名排好序的**，第一次出现的就是排名更高的那条。保留它，等于保留"更好的那个版本"。

测试固定了这个行为：

```python
first     = result(1, content="first",     score=0.9)
duplicate = result(1, content="duplicate", score=0.1)

bundle = ContextBuilder().build("query", [first, duplicate])

assert len(bundle.items) == 1
assert bundle.items[0].content == "first"    # 不是 "duplicate"
assert bundle.items[0].score == 0.9          # 不是 0.1
assert bundle.items[0].retrieval_rank == 1
assert bundle.truncated is False             # 单纯去重不算截断
```

## 3. 这一层去重"不做"什么

它只解决**完全相同的 Chunk**。

它**不解决**语义冗余。例如：

```text
CLASS Chunk     PurchaseTicketTxService 整个类
                └─ 里面包含 doPurchaseInTransaction()

METHOD Chunk    doPurchaseInTransaction()
```

这两条的 `chunk_id` 不同，严格说不算重复，但内容重叠很多。

V1 **故意不解决**这个问题，因为它会立刻牵扯出一堆难题：

```text
CLASS 和 METHOD 谁优先？
重叠多少算重复？
保留父 Chunk 还是子 Chunk？
```

这些属于 P1 的"语义去重"，不属于 V1。

所以 Context Builder V1 的去重，只需要理解成：

> **不要把完全相同的检索 Chunk 重复塞进去。**

---

# 六、第二步：MIXED 双源锚点

这是 V1 里唯一会**改变顺序**的逻辑，也是最需要理解的一段。

## 1. 它解决什么问题

用户问：

```text
为什么把 Feign 调用移出购票事务？具体改了哪些代码？
```

这个问题同时需要：

```text
设计文档（Why）
+
代码实现（How）
```

但 Hybrid 排出来的 Top-K 可能全是 DOCUMENT（设计文档通常写得"很像问题"，语义匹配度高），也可能是清一色的 CODE。

Context Builder 就做一件事：

> **如果检索结果里同时有 CODE 和 DOCUMENT，尽量保证两类都出现在 Context 前部。**

## 2. 具体算法

代码在 [builder.py:98-122](D:/Java-learning/DevContext/src/devcontext/context/builder.py:98)：

```text
1. 找到排名最高的 CODE      → first_code
2. 找到排名最高的 DOCUMENT  → first_document
3. 如果两者缺任何一个 → 原样返回，不做任何调整
4. 否则把这两个"锚点"按原始排名排序
5. 把两个锚点完整渲染出来，检查是否能同时放进预算
   ├─ 放不下 → 回退到原始排名顺序（不做任何复杂配额）
   └─ 放得下 → 锚点排最前，其余按原始顺序跟在后面
```

## 3. 用一个测试看清效果

```python
first_document  = result(1, source_type="DOCUMENT", content="doc one")   # 排名 1
second_document = result(2, source_type="DOCUMENT", content="doc two")   # 排名 2
code            = result(3, source_type="CODE",     content="code")      # 排名 3

bundle = ContextBuilder().build("query", [first_document, second_document, code])

assert [item.chunk_id for item in bundle.items] == [1, 3, 2]
```

变化过程：

```text
原始检索顺序        调整后 Context 顺序
   1 DOCUMENT           1 DOCUMENT   ← 锚点（排名最高的 DOCUMENT）
   2 DOCUMENT           3 CODE       ← 锚点（排名最高的 CODE）
   3 CODE               2 DOCUMENT   ← 剩下的按原顺序
```

注意 `retrieval_rank` 仍然是 `[1, 3, 2]`——它记录的是**原始排名**，不是新位置。这正是第三节说的"两者会不一致"。

## 4. 三个关键的"不做"

### 不做 1：缺少某一类时，不会二次检索

如果输入全是 DOCUMENT，Context Builder **不会**再去补一次 CODE 检索。

它**不会伪造来源**，也不会自己判断"这个问题应该也要代码"。

这是职责边界：检索是 Retriever 的事，Context Builder 不越界。

测试固定了这一点：

```python
bundle = ContextBuilder().build(
    "query",
    [result(1, source_type="DOCUMENT"), result(2, source_type="DOCUMENT")],
)
assert {item.citation.source_type for item in bundle.items} == {"DOCUMENT"}
```

### 不做 2：锚点放不下时，回退而不是"挤一挤"

如果两个完整锚点加起来超出预算，代码直接 `return ranked_results`，回到原始顺序。

```python
if len(ITEM_SEPARATOR.join(anchor_blocks)) > self.max_chars:
    return ranked_results
```

**不做"各砍一半"这类复杂配额。** 因为"砍多少"没有客观依据，反而会让行为难以解释。

### 不做 3：V1 不判断"这个问题是不是 MIXED"

这一点必须诚实说明。

设计文档 `p6-contextBuilder设计思想.md` 第十九节的原则是：

```text
Router 决定需求
Builder 执行上下文组织
```

理想流程是：

```text
Query → Router → 判定 MIXED → Retriever
      → Context Builder(require_mixed_sources=true)
```

但 **V1 还没有 Router**（Router 在后续里程碑）。

所以 V1 用的近似条件是：

```text
检索结果里同时出现了 CODE 和 DOCUMENT
        ↓
就当作需要双源保留
```

这是一个**近似**，不是精确的意图判断。它可能带来副作用：一个纯 CODE 问题如果恰好有一条文档排进了 Top-K，也会触发锚点提升。

这个近似在 V1 是可接受的，因为：

```text
1. 它只在"两类都存在"时才生效
2. 它不删除任何结果，只调整顺序
3. 接入 Router 后，这个条件会被真实的 MIXED 判断替换
```

---

# 七、第三步：字符预算与截断

## 1. 预算是什么

默认预算 6000 字符：

```python
DEFAULT_MAX_CHARS = 6000
```

可以通过构造参数或 CLI 的 `--max-chars` 覆盖。

## 2. 预算算的是"渲染后的完整文本"，不只是正文

这一点很重要。预算统计覆盖：

```text
[C1] CODE                    ← 标签
File: ...                    ← 路径
Symbol: ...                  ← 符号
Signature: ...               ← 签名
Lines: 81-155                ← 行号
Score: 0.031545              ← 分数
                             ← 空行分隔符
正文内容                       ← 正文
==============               ← 条目之间的分隔符（\n\n）
```

而不是"只算正文长度"。原因是：**LLM 实际收到的就是渲染后的文本**，Citation 头也要占 Token。

所以：

```text
ContextBundle.total_chars == len(rendered_text)
```

这条等式是硬保证，测试直接断言：

```python
assert constrained.total_chars == len(constrained.rendered_text)
assert constrained.total_chars <= constrained.max_chars
```

## 3. 逐条放入的算术

每一轮循环都重新计算剩余空间：

```python
separator_length = len(ITEM_SEPARATOR) if blocks else 0
current_length   = len(ITEM_SEPARATOR.join(blocks))
available        = self.max_chars - current_length - separator_length
```

`separator_length` 只在"前面已经有条目"时才计入——因为第一条前面不需要分隔符。

然后：

```text
len(full_block) <= available  →  完整放入
否则                          →  尝试截断
```

## 4. 截断的逻辑

```python
budget_truncated = True
content_budget = (
    available
    - len(header)              # Citation 头
    - len(CONTENT_SEPARATOR)   # 头和正文之间的空行
    - len(TRUNCATION_MARKER)   # "… [truncated]"
)
if item.content and content_budget >= 1:
    item.content = item.content[:content_budget] + TRUNCATION_MARKER
    item.truncated = True
    items.append(item)
    blocks.append(self._render_block(header, item.content))
break
```

三个要点：

### 要点 1：优先保住 Citation

截断的是**正文**，不是 Citation 头。

因为 Citation 是"可验证性"的来源。一段没有出处的代码对 LLM 价值很低，而带有明确 `File / Symbol / Lines` 的截断代码仍然可用。

### 要点 2：加 `… [truncated]` 标记

正文被截断时，末尾会加上 `TRUNCATION_MARKER = "… [truncated]"`。

这个标记有两层意义：

```text
对 LLM：明确告知"这里不完整"，避免它把截断当成完整实现来推理
对人：  一眼看出这条被预算裁过
```

### 要点 3：放不下就整条不加

如果连 Citation 头和至少 1 个正文字符都塞不下：

```python
if item.content and content_budget >= 1:   # ← 不满足这个条件
    ...
# 于是 items / blocks 都不追加，直接 break
```

结果是一个**更小、但没有残缺条目**的 Bundle。这比"塞一条只剩文件名的空壳"更干净。

## 5. `truncated` 字段的确切含义

`bundle.truncated` 为 `True` 只有两种情形：

```text
1. 有某条正文被预算截断了
2. 有结果因为预算放不下而被整个丢弃
```

注意一个**容易误解的边界情况**：

如果第一条的 Citation 头就放不下，会得到：

```python
ContextBundle(
    items=[],                 # 空
    rendered_text="",         # 空
    total_chars=0,
    truncated=True,           # ← 但 truncated 是 True
)
```

也就是说：

```text
truncated = True  ≠  "有内容"
truncated = True  =  "发生了预算导致的舍弃"
```

而"检索结果本来就是空的"是另一回事——那种情况下 `truncated=False`（见第八节）。

测试固定了这条：

```python
constrained = ContextBuilder(max_chars=len(header) - 1).build("query", [search_result])

assert constrained.items == []
assert constrained.rendered_text == ""
assert constrained.total_chars == 0
assert constrained.truncated is True
```

## 6. 为什么还需要预算

一个自然的疑问：让 LLM 多看一点不行吗？

不行。因为"更多 Context"不等于"更好"：

```text
1. 成本更高
2. 响应更慢
3. 无关信息变多
4. 重要信息被淹没
5. 模型更容易混淆
```

这正是 Context Engineering 的核心之一：

> **不是把所有资料给模型，而是把最需要的资料给模型。**

## 7. 为什么用"字符"而不是"Token"

V1 用的是字符数，这是一个**刻意的简化**：

```text
优点：无需 tokenizer 依赖、计算零成本、行为完全确定、可精确断言
代价：字符数与 Token 数不是线性关系
     （中文、代码、符号的字符/Token 比差异很大）
```

项目自己的 README 把"tokenizer / token 预算"明确列为**后续方向**，不属于 V1。

在当前阶段，字符预算足以完成它的核心任务：**防止 Context 无限膨胀，并让行为可测试、可复现。**

---

# 八、边界情况与它们的设计含义

`build()` 对四类输入有明确定义的行为。

## 1. 空检索结果

```python
bundle = ContextBuilder().build("query", [])
```

返回：

```python
ContextBundle(
    query="query",
    items=[],
    rendered_text="",
    total_chars=0,
    max_chars=6000,
    truncated=False,      # ← 注意这里是 False
)
```

**为什么 `truncated=False`**：没有内容不是因为"被预算挤掉"，而是因为"本来就没检索到"。这两种情况必须区分开——否则你会误以为"预算太小"，而去调大 `--max-chars`。

CLI 输出：

```text
Query: no matches
No context found.
```

## 2. 空 query

```python
@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
```

**为什么直接报错而不是返回空 Bundle**：空 query 是调用方的编程错误，不是正常的业务状态。让它"安静地返回空结果"会掩盖 bug。

注意这里用的是 `query.strip()` 判空，所以 `"   "`（纯空格）也会被拒绝。

## 3. 非正数预算

```python
def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than 0")
```

在**构造时**就报错，而不是等到 `build()` 时才发现。这是"尽早失败"原则——配置错误越早暴露越好。

## 4. 输入只有单一来源

前面已经说明：不会伪造另一类来源，也不会触发二次检索。

---

# 九、真实 CLI 输出

CLI 子命令在 [cli.py:37-40](D:/Java-learning/DevContext/src/devcontext/cli.py:37) 定义，执行逻辑在 [cli.py:113-122](D:/Java-learning/DevContext/src/devcontext/cli.py:113)。

```powershell
uv run devcontext context "购票事务是如何实现的？" --top-k 5
```

| 参数 | 默认值 | 含义 |
|---|---:|---|
| positional `query` | 必填 | 查询问题 |
| `--top-k` | 5 | 传给 Hybrid Retrieval 的候选数量 |
| `--max-chars` | 6000 | 渲染文本字符预算 |

`context` 命令**固定使用 Hybrid 检索**，不暴露检索策略参数——这是刻意的，避免把"检索怎么配"和"上下文怎么组织"两件事混在一起。

## 示例 1：Java Citation 与预算截断

```powershell
uv run devcontext context "OrderServiceImpl.createTicketOrder" --top-k 5 --max-chars 3000
```

实际输出开头：

```text
Query: OrderServiceImpl.createTicketOrder

[C1] CODE
File: services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/remote/OrderRemoteService.java
Symbol: OrderRemoteService#createTicketOrder
Signature: abstract Result<String> createTicketOrder(@RequestBody TicketOrderCreateRemoteReqDTO requestParam)
Lines: 18-19
Score: 0.032018

@PostMapping("/api/order-service/order/ticket/create")
Result<String> createTicketOrder(...);

[C2] CODE
File: services/order-services/src/main/java/edu/swu/fcj/my12306/biz/orderservice/service/impl/OrderServiceImpl.java
Symbol: OrderServiceImpl#createTicketOrder
Signature: public String createTicketOrder(TicketOrderCreateReqDTO requestParam)
Lines: 81-155
Score: 0.031545

@Override
@Transactional(rollbackFor = Exception.class)
public String createTicketOrder(...) {
    ...
    OrderItem… [truncated]
```

这一段同时验证了：

```text
[C1] [C2] 连续编号       ← 引用稳定
Java 完整文件路径
Class#method 形式符号     ← _code_symbol()
方法签名
起止行号（18-19 是接口声明，所以只有 2 行）
检索分数（6 位小数）
… [truncated]            ← 预算截断生效
```

注意 `[C1]` 指向的是**接口**（`OrderRemoteService`，Feign 客户端），`[C2]` 指向的是**实现**。这正说明检索质量对 Context 质量的影响——Context Builder 只能整理它拿到的结果。

## 示例 2：Markdown Citation

```powershell
uv run devcontext context "订单关闭的代码和设计依据" --top-k 10 --max-chars 6000
```

实际输出包含：

```text
[C1] DOCUMENT
File: 7-模块设计文档/1-订单支付-车票.md
Heading: 十八、第四条主链路：超时关单
Score: 0.016393

这是订单模块第二核心。
...
```

以及：

```text
[C8] DOCUMENT
File: 7-模块设计文档/3-订单支付-支付.md
Heading: 二十五、支付和关单发生并发怎么办？ > 情况 B：关单先成功
Score: 0.015625
```

验证了：

```text
Markdown 文件路径
完整有序标题层级（用 " > " 连接，来自 heading_path）
稳定引用编号（一直到 C8 都连续）
```

### 一个必须解释清楚的现象

这个查询名义上问的是"代码和设计依据"，但 **Hybrid Top 10 全部是 DOCUMENT**。

那么为什么没有 CODE？

因为 Context Builder 的规则是：**只有当输入里同时存在两类来源时才会做双源保留**。这次检索结果里一条 CODE 都没有，所以：

```text
Context Builder 没有补查
Context Builder 没有伪造 CODE
Context Builder 只是如实渲染了它收到的 10 条文档
```

**这是当前检索结果的表现，不是 Context Builder 的数据丢失。**

这是一个很好的观察点：如果你希望这类问题一定能拿到代码，正确的做法是

```text
改进检索（让 CODE 能进 Top-K）
或
引入 Router + 二次检索（后续里程碑）
```

而不是让 Context Builder 去"变"出代码。

---

# 十、几条贯穿始终的设计原则

## 1. 职责单一：不碰 LLM

Context Builder 只输出 `ContextBundle`，不调用 LLM。

为什么坚持这条边界？因为如果它既做去重/预算/Citation，又做 Prompt/LLM 调用/答案生成，那么出问题时你**分不清**：

```text
是 Context 组织错了
还是 LLM 回答错了
```

所以流程被拆成两段：

```text
Context Builder
      ↓
ContextBundle
      ↓
Answer Generator（独立组件）
      ↓
LLM
```

这也让 Context Builder 可以被完全单元测试——不需要 API Key、不需要网络、不需要数据库。

## 2. 稳定引用：先排序，再编号

编号在**最终选定并排序之后**才分配：

```python
label = f"C{len(items) + 1}"
```

带来的性质：

```text
编号从 C1 连续递增，不出现空洞
相同输入 + 相同顺序 + 相同预算 → 完全相同的引用编号和渲染文本
```

测试直接断言了确定性：

```python
first  = builder.build("query", results)
second = builder.build("query", results)

assert [item.citation.label for item in first.items] == ["C1", "C2", "C3"]
assert first.to_dict() == second.to_dict()
```

**为什么"稳定"很重要**：因为 Citation 会被 LLM 引用（`[C1]`、`[C2]`），也会被用来做验证和调试。如果同一份输入两次运行编号不同，那么"LLM 引用了 C3"这句话就无法回溯。

（顺带说明：下游的 `AnswerGenerator` 会提取答案里的 `[C数字]`，并**拒绝任何不在当前 Context 中的编号**——见 [generator.py:45-49](D:/Java-learning/DevContext/src/devcontext/answer/generator.py:45)。这让"引用稳定"成为一条硬约束。）

## 3. 不伪造来源

检索到什么就渲染什么。

```text
缺 CODE  →  不补
缺 DOC   →  不补
结果为空 →  返回空 Bundle
```

这条原则保证了 Context 的**可解释性**：Context 里出现的每一块内容，都能追溯到一次真实检索命中的 Chunk。

## 4. 顺序即优先级

`break` 的语义、锚点提升的语义，都在表达同一件事：

> **排名高的结果优先获得预算。**

这让"为什么这条内容在 Context 里、那条不在"始终有一个可解释的答案。

## 5. 一切可观察

`ContextItem` 保留了 `chunk_id` / `chunk_type` / `score` / `retrieval_rank` / `truncated`。

`ContextBundle` 暴露 `total_chars` / `max_chars` / `truncated`。

原因和整个项目一致：

```text
Agent / RAG 项目最怕：结果不好，但不知道为什么。
```

---

# 十一、测试覆盖了什么

本部分共 **14 个测试函数**（其中 2 个是参数化，展开后为 **16 个用例**）：

```text
test_context_builder.py   12 个函数 → 14 个用例（2 个参数化各 2 例）
test_context_cli.py        2 个函数 →  2 个用例
合计                      14 个函数 → 16 个用例
```

整体测试套件结果为 `37 passed`（含数据库集成测试）。

测试文件的组织方式很值得学习：**每个测试都对应一条被明确固定的行为约定**。

## [test_context_builder.py](D:/Java-learning/DevContext/tests/test_context_builder.py:1)

| 测试 | 固定的行为 |
|---|---|
| `test_duplicate_chunk_keeps_first_result_only` | 去重保留第一次出现，纯去重不算截断 |
| `test_java_citation_and_rendering` | Java Citation 全部字段 + 渲染格式（含 `Class#method`） |
| `test_markdown_citation_and_rendering` | Markdown Citation 字段 + `Heading: A > B` 格式 |
| `test_citation_numbers_and_rendered_text_are_stable` | 编号连续且输出确定 |
| `test_budget_truncates_last_item_and_counts_full_rendered_text` | 截断标记、`total_chars` 一致性、不超预算 |
| `test_item_is_not_added_when_citation_header_does_not_fit` | 放不下就不产生残缺条目，且 `truncated=True` |
| `test_mixed_sources_are_prioritized_when_both_anchors_fit` | 双源锚点优先，顺序为 `[1, 3, 2]` |
| `test_mixed_sources_fall_back_to_rank_order_when_anchors_do_not_fit` | 锚点放不下时回退原始排名 |
| `test_single_source_input_does_not_invent_another_source` | 不伪造另一类来源 |
| `test_empty_results_return_empty_bundle` | 空结果返回空 Bundle 且 `truncated=False` |
| `test_empty_query_is_rejected`（参数化 2 例） | 空 query / 纯空格报错 |
| `test_non_positive_budget_is_rejected`（参数化 2 例） | 非正预算报错 |

## [test_context_cli.py](D:/Java-learning/DevContext/tests/test_context_cli.py:1)

| 测试 | 固定的行为 |
|---|---|
| `test_context_cli_uses_hybrid_retrieval_and_prints_context` | CLI **固定**调用 `("hybrid", query, top_k)`，参数透传正确 |
| `test_context_cli_prints_clear_message_for_empty_results` | 空结果输出 `Query: ...\nNo context found.\n` |

注意这两个 CLI 测试用了 `monkeypatch` 替换 `RetrievalService`，所以**不需要数据库、不需要网络就能验证 CLI 行为**——这正是"Context Builder 不依赖 LLM"带来的可测试性收益。

---

# 十二、V1 的边界：什么没做

README 明确记录了后续方向。理解"为什么不做"和"做了什么"同样重要。

| 未实现 | 为什么 V1 不做 |
|---|---|
| tokenizer / token 预算 | 需要引入 tokenizer 依赖；字符预算已能防止膨胀，且完全确定可测 |
| 语义去重 | 牵扯"CLASS 与 METHOD 谁优先""重叠多少算重复"，容易失控 |
| 相邻 Chunk 合并 | 需要判断"相邻"的语义边界，V1 没有这个信息 |
| Parent Context | 会引入"方法 + 所属类"的层级关系，属于结构调整 |
| 动态来源配额 | V1 只做"两类都放前面"，配额比例缺少客观依据 |
| 查询意图识别（Router） | 属于独立组件，V1 用"结果里是否同时有两类"近似 |
| 缺失来源二次检索 | 会越界到检索层；且 V1 坚持"不伪造、不补查" |
| Query Rewrite | 属于 Agentic Retry 的一部分 |
| Reranker | 先要证明 RRF 的基线表现，否则无法归因 |
| LLM | 职责分离，属于 Answer Generator |
| LangGraph | 需要先有可编排的多个决策点 |

一句话：

> **V1 只做"把检索结果整理成可信证据包"这一件事，并把这件事做到可测试、可解释、确定。**

---

# 十三、和检索层的关系：没有改动任何东西

本部分是一个**纯新增的消费层**，没有回头修改检索：

```text
Hybrid 候选池     仍然是 max(20, top_k)
RRF               仍然是 k = 60
Keyword / Vector / Hybrid SQL    未修改
数据库表与索引                    未修改
retrieval-v1 评测基线             未修改
```

（可对照 [service.py:58-70](D:/Java-learning/DevContext/src/devcontext/retrieval/service.py:58) 与 [hybrid.py:9-22](D:/Java-learning/DevContext/src/devcontext/retrieval/hybrid.py:9)：候选池 `pool_size = max(20, top_k)`，融合 `reciprocal_rank_fusion([keyword, vector], k=60, top_k=top_k)`。）

这一点很重要，它保证了：

```text
Context Builder 不会改变检索指标
评测结果的前后对比依然有效
出问题时可以确定不是检索层被动过
```

---

# 十四、一句话总结

把这一部分压缩成几句话：

```text
SearchResult         = Retriever 找到的一条候选资料
Citation             = 这条资料"从哪里来"的身份证
ContextItem          = 准备真正交给 LLM 的一条证据
ContextBundle        = 某一个问题最终准备好的整包证据
rendered_text        = 把这整包证据变成 LLM 可以直接阅读的文本
Context Builder      = 把"搜索结果"整理成"LLM 最终上下文"的中间层
```

流程：

```text
Query
  ↓
RetrievalService.search("hybrid", query, top_k)
  ↓
SearchResult[]
  ↓
按 id 去重（保留第一次出现）
  ↓
尝试保留 CODE / DOCUMENT 双源锚点
  ↓
按 max_chars 逐条放入，必要时截断正文并 break
  ↓
按最终顺序生成稳定编号 [C1] [C2] ...
  ↓
ContextBundle（含 rendered_text / total_chars / truncated）
  ↓
CLI 打印 rendered_text
```

最核心的一句：

> **Retriever 决定"找到什么"，Context Builder 决定"最终让模型看到什么"。**

判断 Context Builder 是否成功，只需要问自己一个问题：

> **如果我是 LLM，只看这份 `rendered_text`，能不能回答用户的问题？**

如果答案是"能"，Context Builder 就完成了它的职责。
