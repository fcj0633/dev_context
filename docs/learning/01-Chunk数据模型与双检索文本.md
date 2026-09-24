# 01 · Chunk 数据模型与双检索文本

> 学习路线位置：**数据模型层**（第 1 篇）
> 重点源码：`src/devcontext/models.py`（全文 84 行）
> 前置：无。本篇是后续所有篇章的基础。
> 本篇最重要的一句话：**`Chunk` ≠ `keyword_text` ≠ `embedding_text`。**

---

## 1. 这一模块解决什么问题

整个 DevContext 的输入侧有两种完全不同的语料：

```text
Java 源码   → 由 JavaParser 按 AST 边界切分
Markdown    → 由标题层级切分
```

但下游的检索侧只有一套逻辑：

```text
一个 SQL 查询 → 一批候选 → 排序 → Top-K
```

如果 Java 切出来的东西和 Markdown 切出来的东西是两种不同的结构，那么检索层就必须写两套 SQL、两套排序、两套融合。RRF 也没法用。

所以 `models.py` 解决的是一个**收敛问题**：

```text
把两种来源、四种切分方式，收敛成一个统一的数据模型
```

它定义了三个东西：

| 对象 | 作用 | 出现位置 |
|---|---|---|
| `Chunk` | 入库前的统一知识单元 | 解析 → 入库 |
| `SearchResult` | 检索结果单元 | 检索 → 评测 |
| `keyword_text()` / `embedding_text()` | 从同一个 `Chunk` 派生两段**用途不同**的文本 | 入库前计算 |

其中 `keyword_text()` 与 `embedding_text()` 是本篇的核心。它们是同一个 `Chunk` 的两个"投影"：

```text
Chunk ──→ keyword_text()   → 存入 keyword_text 列 → 给关键词检索用
      └──→ embedding_text() → 送去 Embedding API → 给向量检索用
```

**为什么要分两段文本，而不是把所有字段拼一大坨？** 这是本篇要回答的核心问题（第 9 节）。

---

## 2. 在完整系统中的位置

```text
① JavaParser / MarkdownParser
        ↓  产出
② Chunk（本篇）  ──────────────┐
        ↓                      │
③ keyword_text()               │  同一个对象
④ embedding_text()             │  派生两段文本
        ↓                      │
⑤ Embedding API → Vector       │
        ↓                      │
⑥ knowledge_chunk 一行  ←──────┘
        ↓
⑦ Keyword Retrieval（读 keyword_text 列 + 其他字段）
⑧ Vector Retrieval （读 embedding 列）
        ↓
⑨ RRF → Top-K
        ↓
⑩ Evaluation（读 SearchResult）
```

两个衔接点必须记住：

- **上游**：`java_parser_runner.py` 把 Java 输出的 JSONL 逐行 `Chunk.from_dict(...)`；`markdown_parser.py` 直接构造 `Chunk(...)`。两条路都汇聚到这一个类。
- **下游**：`storage.replace_repository()` 调用 `chunk.keyword_text()` 填充列；`ingestion/pipeline.py` 调用 `chunk.embedding_text()` 送去算向量。**这两个调用点是全文最重要的两行代码。**

---

## 3. 必须先知道的最少概念

### 3.1 `@dataclass(slots=True)` 意味着什么

```python
@dataclass(slots=True)
class Chunk:
```

`dataclass` 自动生成 `__init__` / `__repr__` / `__eq__`。`slots=True` 额外做一件事：**禁止给实例动态添加未声明的属性**。

对项目的实际影响：如果某次解析产出了一个新字段（比如 `source_root`），而 `Chunk` 里没声明，那么 `chunk.source_root = ...` 会直接报 `AttributeError`。这是一个**刻意的防线**：字段一旦要进数据模型，就必须显式声明，不能悄悄走样。这也意味着"新增一个字段"是有固定改动清单的，见本篇末尾的学习完成标准第 5 条。

### 3.2 三层"数据模型"不要混淆

| 名称 | 是什么 | 字段数 | 代码位置 |
|---|---|---|---|
| `ChunkRecord`（Java） | Java 侧的输出 DTO | 17 | `java-parser/.../ChunkRecord.java` |
| `Chunk`（Python） | Python 侧的入库前对象 | 17 | `models.py:9-57` |
| `knowledge_chunk`（SQL） | 数据库表的列 | 21 | `sql/001_schema.sql` |
| `SearchResult` | 检索结果对象 | 12 | `models.py:61-83` |

`ChunkRecord` 与 `Chunk` 字段**一一对应**，靠 Java 侧 Jackson 的 `SNAKE_CASE` 命名策略 + Python 侧 `Chunk.from_dict` 对齐。数据库比它们多出 `id` / `keyword_text` / `embedding_model` / `embedding` / `created_at` 五列——因为这几列是"入库时才产生的"，不属于解析产物。

### 3.3 `SearchResult` 与 `Chunk` 的区别

`SearchResult` **不是** `Chunk` 的子集或视图，它是一个独立的数据类，只有 12 个字段，而且**不含 `keyword_text` / `embedding` / `content_hash` / `heading_path` / `annotations` / `javadoc`**。

它对应的是 SQL 里的 `RESULT_COLUMNS`：

```python
RESULT_COLUMNS = """
    id, source_type, chunk_type, file_path, content, start_line, end_line,
    class_name, symbol_name, signature, title
"""
```

两个必须知道的后果：

1. **`heading_path` 不在检索结果里**。所以文档类 Citation 若要按标题路径呈现，必须先改 `RESULT_COLUMNS` 与 `SearchResult`。
2. `SearchResult.to_dict()` 会把 `content` 换成 300 字预览：

```python
data["content_preview"] = " ".join(self.content.split())[:300]
del data["content"]
```

也就是说，`--format json` 输出与评测报告里的 `top_results` **不含完整 content**，只有前 300 字符（且空白已被折叠）。这是很多人第一次看评测报告时会困惑的地方。

---

## 4. 关键源码入口

| 位置 | 内容 |
|---|---|
| `src/devcontext/models.py:9-26` | `Chunk` 的 17 个字段声明 |
| `src/devcontext/models.py:28-30` | `__post_init__`：自动计算 `content_hash` |
| `src/devcontext/models.py:32-35` | `from_dict`：从 Java JSONL 构造 Chunk |
| `src/devcontext/models.py:40-51` | ★ `keyword_text()` |
| `src/devcontext/models.py:53-57` | ★ `embedding_text()` |
| `src/devcontext/models.py:61-83` | `SearchResult` 与 `to_dict()` |
| `src/devcontext/ingestion/pipeline.py:52-53` | 调用 `embedding_text()` 并算指纹的地方 |
| `src/devcontext/storage.py:67` | 调用 `keyword_text()` 写库的地方 |
| `src/devcontext/storage.py:16-19` | `RESULT_COLUMNS`（检索结果投影） |
| `sql/001_schema.sql:4-29` | `knowledge_chunk` 的 21 列 |

---

## 5. Input / Output

### 5.1 `Chunk` 本身

**Input**：解析器给出的原始字段（Java 走 JSONL，Markdown 走构造函数）。
**Output**：一个带 17 个字段的不可动态扩展的对象。

```text
Input（Java 侧 JSONL 一行）
{"repository":"my12306","source_type":"CODE","chunk_type":"METHOD",
 "file_path":"services/.../PurchaseTicketTxService.java","module":"ticket-services",
 "class_name":"PurchaseTicketTxService","symbol_name":"doPurchaseInTransaction",
 "signature":"public PurchaseReservationResult doPurchaseInTransaction(...)",
 "annotations":["@Transactional(rollbackFor = Exception.class)"],
 "javadoc":"* 在锁内原子完成选座...","content":"@Transactional(...)\n    public ...",
 "start_line":69,"end_line":172,"content_hash":"519d2ebf..."}

Output（Chunk 对象）
Chunk(repository='my12306', source_type='CODE', chunk_type='METHOD', ...,
      content_hash='519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c')
```

### 5.2 `keyword_text()`

**Input**：Chunk 的 8 个字段位。
**Output**：一个用 `\n` 连接的字符串，`doPurchaseInTransaction` 这条实例是 **6899 字符**。

### 5.3 `embedding_text()`

**Input**：Chunk 的 4 个候选字段位（identity 级联 + javadoc + content）。
**Output**：一个用 `\n\n` 连接的字符串，同一条实例是 **6734 字符**。

### 5.4 `content_hash`

**Input**：`self.content`。
**Output**：64 位十六进制 SHA-256。**注意它只用 `content`，不含任何其他字段。**

---

## 6. 主执行流程

一个 `Chunk` 从"被构造"到"产生两段文本"的完整过程：

```text
① MarkdownParser 直接构造 Chunk(...)
   或
   JavaParserRunner 读 JSONL → Chunk.from_dict({...})

② __post_init__()
   if not self.content_hash:
       self.content_hash = sha256(self.content.encode("utf-8")).hexdigest()

③ ingestion/pipeline.py 里：
   embedding_texts = [chunk.embedding_text() for chunk in chunks]
   fingerprints    = [sha256(text).hexdigest() for text in embedding_texts]
   → 查 EmbeddingCache → 缺的送去 Embedding API

④ storage.replace_repository() 里：
   chunk.keyword_text()  → keyword_text 列
   chunk.content_hash    → content_hash 列
   embedding             → embedding 列
```

用真实数字描述这一步的规模：538 个代码 Chunk + 2363 个文档 Chunk = 2901 个 `Chunk` 对象，每个都会走一遍 `embedding_text()` 与 `keyword_text()`。

---

## 7. 关键源码逐段解释

### 7.1 字段声明（`models.py:9-26`）

```python
@dataclass(slots=True)
class Chunk:
    repository: str
    source_type: str
    chunk_type: str
    file_path: str
    content: str
    start_line: int | None = None
    end_line: int | None = None
    module: str | None = None
    package_name: str | None = None
    class_name: str | None = None
    symbol_name: str | None = None
    signature: str | None = None
    annotations: list[str] = field(default_factory=list)
    javadoc: str | None = None
    title: str | None = None
    heading_path: list[str] = field(default_factory=list)
    content_hash: str = ""
```

两个细节：

- **只有前 5 个字段是必填的**（`repository` / `source_type` / `chunk_type` / `file_path` / `content`）。其余全部有默认值。这直接反映了"两种语料共享一个模型"的现实：文档 Chunk 没有 `class_name`，代码 Chunk 没有 `heading_path`，但两者都有 `content`。
- 两个列表字段用 `field(default_factory=list)`，而不是 `= []`。这是 Python 的经典陷阱处理：默认值会在实例间共享，用可变对象做默认值会导致一个实例改列表所有实例都变。

### 7.2 逐字段讲解（按"它是什么 / 谁产生它 / 谁消费它"）

#### A. 身份与分类

**`repository`**（`str`，必填）
值恒为 `"my12306"`（来自 `Settings.repository_name`）。它出现在每一条 SQL 的 `WHERE repository = %s` 里。**当前只有单个仓库在用，但结构上支持多仓库隔离**。

**`source_type`**（`str`，必填）
只有两个取值：`"CODE"` 或 `"DOCUMENT"`。由 `sql/001_schema.sql:7` 的 CHECK 约束兜底。它的消费者是评测的判分函数——`benchmark/cases.jsonl` 里每条 Ground Truth 都带 `source_type`，`_matches()` 首先比的就是它。

**`chunk_type`**（`str`，必填）
五个取值：`CLASS` / `INTERFACE` / `METHOD` / `CONSTRUCTOR` / `DOCUMENT_SECTION`。同样有 CHECK 约束。真实分布：METHOD 316、CLASS 165、INTERFACE 38、CONSTRUCTOR 19、DOCUMENT_SECTION 2363。

**`file_path`**（`str`，必填）
**相对各自根目录**的路径，例如：

```text
services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java
```

注意它不含 `D:\Java-learning\12306Project\` 这一段。原因是双根目录模型（ADR-012）：代码根与文档根是两个物理路径，如果存绝对路径，就会把本机目录结构泄漏进索引，也让"换一台机器重建索引"产生不同的 `content_hash` 之外的差异。它进入 `keyword_text`，**但不进入 `embedding_text`**（第 9 节解释为什么）。

**`module`**（`str | None`）
由 Java 侧 `detectModule()` 从路径推出：**找路径里的 `src`，取它前一段**。真实验证：

```text
services/ticket-services/src/main/java/...      →  src 在索引 2  →  module = "ticket-services"
services/gateway-services/src/main/java/...     →  module = "gateway-services"
```

真实分布：ticket-services 219、user-services 135、pay-services 86、order-services 85、gateway-services 13。Markdown 侧不设此字段（恒为 `None`）。
**它不参与 `keyword_text`，也不参与 `embedding_text`**——也就是说，当前无法用它检索（见第 11 节）。

**`package_name`**（`str | None`）
Java 包名，例如 `edu.swu.fcj.my12306.biz.ticketservice.service.impl`。同样既不进 keyword_text 也不进 embedding_text。

#### B. 代码语义 Metadata（Java 专属）

**`class_name`**（`str | None`）
对 METHOD / CONSTRUCTOR：方法所在的**最近一层**类型声明的名字（由 `callable.findAncestor(TypeDeclaration.class)` 得到）。
对 CLASS / INTERFACE：就是这个类型自己的名字。

**这一点非常重要，它是第 06 篇"CLASS 双重加分问题"的根源**：对类型级 Chunk，`class_name == symbol_name`。真实例子：

```text
INTERFACE OrderItemMapper  →  symbol_name = "OrderItemMapper"
                              class_name  = "OrderItemMapper"
```

**`symbol_name`**（`str | None`）
对 METHOD / CONSTRUCTOR：方法名或构造器名。
对 CLASS / INTERFACE：类型名。
它是 **评测判分代码题的唯一依据**（`cases.jsonl` 里 CODE 题的 `"symbol": "doPurchaseInTransaction"`），也是关键词检索权重最高（+12）的字段。

**`signature`**（`str | None`）
由 JavaParser 的 `getDeclarationAsString(true, true, true)` 生成——**重新格式化过的声明字符串**，不是源码原文。真实值：

```text
public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)
```

对 CLASS / INTERFACE，`signature` 由 `typeDeclaration()` 手工拼装，会带上 `extends` / `implements` / `permits` 与类型参数：

```text
public interface OrderItemMapper extends BaseMapper<OrderItemDO>
```

实测 538 个代码 Chunk 中**没有一个是空 signature**（`signature` 恒有值）。原因：`typeRecord()` 与 `callableRecord()` 两个分支都必然设置它。

**`annotations`**（`list[str]`）
注解字符串列表，来自 `NodeWithAnnotations.getAnnotations()`。真实值举例：

```text
doPurchaseInTransaction                    → ["@Transactional(rollbackFor = Exception.class)"]
userRegisterCachePenetrationBloomFilter    → ["@Bean"]
TicketAvailabilityTokenBucket (CLASS)      → ["@Slf4j", "@Component", "@RequiredArgsConstructor"]
```

**关键细节：`annotations` 字段本身不进 `keyword_text`，但注解文本会通过 `content` 间接进入。** 因为注解落在方法的 `Range` 之内（见第 02 篇），所以 `content` 的第一行往往就是注解。这一点在第 8.2 节用真实输出证明。

**`javadoc`**（`str | None`）
Javadoc 的**内容**（已去掉 `/**` `*/` 与每行的 ` * ` 前缀，并 `.strip()`）。真实值：

```text
* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。
```

注意它保留了开头的 `*` —— 因为 `getJavadocComment().getContent()` 返回的是 `*` 之后到 `*/` 之前的原文，代码只做了 `.strip()`。

实测 **538 个代码 Chunk 中有 250 个没有 Javadoc**（占 46%），此时 `javadoc = None`，会在两段文本里被过滤掉。

#### C. 文档语义 Metadata（Markdown 专属）

**`title`**（`str | None`）
文档 Chunk 的所属标题文字（不含 `#`）。Java 侧恒为 `None`。真实例子：`"Q3 · 三层各自的职责（本层核心）"`。
Python 侧 Markdown 解析器**不把 `#` 打进 title**（正则 `^(#{1,6})\s+(.+?)\s*$` 的捕获组 2）。

**`heading_path`**（`list[str]`）
完整标题路径。真实例子：

```text
["DevContext-Java 架构决策记录（ADR）", "ADR-001 Java 解析器选型", "Decision"]
```

它是 `title` 的**上级序列**（含 `title` 自己作为最后一项）。Java 侧恒为 `[]`。
它进入 `keyword_text`（用 `" / "` 连接），**不进入 `embedding_text`**（第 9 节解释）。

#### D. 定位字段

**`start_line` / `end_line`**（`int | None`）
1-based 行号，**闭区间**。
- Java：来自 `Range.begin.line` / `Range.end.line`。
- Markdown：来自解析器的行计数（且不含标题行，见第 03 篇）。
- 它们**不进入任何一段检索文本**——只用于 Citation 展示与人工核验。

**`content`**（`str`，必填）
真正的内容本体。
- Java METHOD/CONSTRUCTOR：**原始源码切片**（不是 `toString()` 的重新格式化结果）。
- Java CLASS/INTERFACE：**摘要**，不是整个类的源码。
- Markdown：该章节的正文（不含标题行）。
它是唯一**同时进入两段文本**的字段，也是入库后最终给人看的字段。

**`content_hash`**（`str`）
`sha256(content)`，由 `__post_init__` 自动补。真实核对：

```text
content_hash          = 519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c
sha256(content)       = 519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c   ← 一致
sha256(embedding_text)= 7f188473af76d1dc48660ab8059c0d0246b2c878fa018b74b7c98284a475c8a0   ← 不同
```

**它进入数据库的 `content_hash` 列，但不进 `embedding_text`。** 而 Embedding 缓存用的却是**另一个 hash**（`sha256(embedding_text)`），这是第 04 篇的重点，也是本篇第 10 节要纠正的误解之一。

### 7.3 `keyword_text()` 的真实源码

```python
def keyword_text(self) -> str:
    values = [
        self.symbol_name,
        self.class_name,
        self.signature,
        self.title,
        " / ".join(self.heading_path),
        self.file_path,
        self.javadoc,
        self.content,
    ]
    return "\n".join(value for value in values if value)
```

三个必须回答的问题：

**哪些字段被拼进去？顺序是什么？**
8 个槽位，顺序是：

```text
① symbol_name          （最高身份）
② class_name
③ signature
④ title                （文档标题）
⑤ " / ".join(heading_path)  （标题路径，单个槽位）
⑥ file_path
⑦ javadoc
⑧ content              （永远最长，放最后）
```

**空值怎么处理？**
`"\n".join(value for value in values if value)` —— **只保留 falsy 检查为真的项**。

这意味着两类值会被自动跳过：

| 情况 | 结果 |
|---|---|
| `None`（如 Java Chunk 的 `title`） | 跳过 |
| 空字符串（如 `heading_path = []` 时 `" / ".join([])` 得到 `""`） | 跳过 |
| 空列表（如 Markdown Chunk 的 `annotations = []`） | **本来就不在槽位里** |

因此**不会出现空行**。真实输出可证：`TicketAvailabilityTokenBucket`（CLASS）的 `keyword_text` 第 1 行是 `public class TicketAvailabilityTokenBucket`，第 2 行直接是 `@Slf4j`——中间的 `symbol_name`/`title` 等槽位没有留空行。

`annotations` 与 `embedding`、`content_hash`、`start_line`、`module`、`package_name` **都不在槽位列表里**。

---

## 8. 用一个真实数据走完整流程

下面三个例子全部取自已运行的产物，可直接复核。

### 8.1 METHOD：`doPurchaseInTransaction`

**Chunk 字段**（来自 `artifacts/java-chunks.jsonl`）：

| 字段 | 真实值 |
|---|---|
| `repository` | `my12306` |
| `source_type` / `chunk_type` | `CODE` / `METHOD` |
| `file_path` | `services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java` |
| `module` / `package_name` | `ticket-services` / `edu.swu.fcj.my12306.biz.ticketservice.service.impl` |
| `class_name` / `symbol_name` | `PurchaseTicketTxService` / `doPurchaseInTransaction` |
| `signature` | `public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)` |
| `annotations` | `["@Transactional(rollbackFor = Exception.class)"]` |
| `javadoc` | `* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。` |
| `title` / `heading_path` | `None` / `[]` |
| `start_line` / `end_line` | `69` / `172` |
| `content_hash` | `519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c` |
| `len(content)` | **6488 字符** |

**`keyword_text()` 的真实输出**（6899 字符；下面是前 20 行，也就是 8 个槽位的分界）：

```text
doPurchaseInTransaction                                                                  ← ① symbol_name
PurchaseTicketTxService                                                                   ← ② class_name
public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)   ← ③ signature
services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java   ← ⑥ file_path
* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。   ← ⑦ javadoc
@Transactional(rollbackFor = Exception.class)                                             ← ⑧ content 第 1 行
    public PurchaseReservationResult doPurchaseInTransaction(
            PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn,
            Map<String, PassengerActualRespDTO> passengersById) {
        TrainDO trainDO = loadTrain(requestParam.getTrainId());
        ...
（后面 6400+ 字符全部是方法体原文，直到第 172 行）
```

注意 ④ `title` 与 ⑤ `heading_path` **完全没有出现**——因为它们分别是 `None` 和 `[]`，被 `if value` 过滤了。这就是"空值怎么处理"的真实效果。

**`embedding_text()` 的真实输出**（6734 字符；前 6 行）：

```text
public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)   ← identity（= signature）

* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。   ← javadoc

@Transactional(rollbackFor = Exception.class)                                             ← content 开始
    public PurchaseReservationResult doPurchaseInTransaction(
```

对比两段文本，可以得到本篇最重要的观察：

```text
keyword_text 的第 1 行是 symbol_name           → "doPurchaseInTransaction"
embedding_text 的第 1 行是 signature           → "public PurchaseReservationResult doPurchaseInTransaction(...)"
```

**同样的信息，在两段文本里的"位置"和"形态"完全不同。** 关键词检索看到的是"一个干净的符号"，向量检索看到的是"一句完整的声明"。

### 8.2 METHOD（无 Javadoc）：`userRegisterCachePenetrationBloomFilter`

这个例子更小（content 仅 340 字符），可以完整展示。

**Chunk 字段**：

| 字段 | 真实值 |
|---|---|
| `file_path` | `services/user-services/src/main/java/edu/swu/fcj/my12306/biz/userservice/config/RBloomFilterConfiguration.java` |
| `class_name` / `symbol_name` | `RBloomFilterConfiguration` / `userRegisterCachePenetrationBloomFilter` |
| `signature` | `public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient)` |
| `annotations` | `["@Bean"]` |
| `javadoc` | `None` |
| `start_line` / `end_line` | `19` / `24` |

**`content` 全文**（340 字符，原始源码切片）：

```java
@Bean
    public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient) {
        RBloomFilter<String> cachePenetrationBloomFilter = redissonClient.getBloomFilter(USER_REGISTER_BLOOM_FILTER_NAME);
        cachePenetrationBloomFilter.tryInit(64L, 0.03D);
        return cachePenetrationBloomFilter;
    }
```

**`keyword_text()` 全文**：

```text
userRegisterCachePenetrationBloomFilter          ← ① symbol_name
RBloomFilterConfiguration                        ← ② class_name
public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient)   ← ③ signature
services/user-services/src/main/java/edu/swu/fcj/my12306/biz/userservice/config/RBloomFilterConfiguration.java   ← ⑥ file_path
@Bean                                            ← ⑧ content 第 1 行（注解！）
    public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient) {
        RBloomFilter<String> cachePenetrationBloomFilter = redissonClient.getBloomFilter(USER_REGISTER_BLOOM_FILTER_NAME);
        cachePenetrationBloomFilter.tryInit(64L, 0.03D);
        return cachePenetrationBloomFilter;
    }
```

这里有一个**必须亲自确认的细节**：第 5 行的 `@Bean` 来自 **`content`**，不是来自 `annotations` 字段。证据很直接——`annotations` 字段本身不在 `keyword_text()` 的 8 个槽位里。所以"注解能被搜到"完全依赖"注解恰好落在源码 Range 内"这个事实。

**`embedding_text()` 全文**：

```text
public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient)   ← identity

@Bean                                            ← content 开始（没有 javadoc 槽位，所以只有一次 \n\n 分隔）
    public RBloomFilter<String> userRegisterCachePenetrationBloomFilter(RedissonClient redissonClient) {
        RBloomFilter<String> cachePenetrationBloomFilter = redissonClient.getBloomFilter(USER_REGISTER_BLOOM_FILTER_NAME);
        cachePenetrationBloomFilter.tryInit(64L, 0.03D);
        return cachePenetrationBloomFilter;
    }
```

与 8.1 一样：`file_path` 消失了，`class_name` 消失了，`@Bean` 保留（因为在 content 里）。

### 8.3 CLASS：`TicketAvailabilityTokenBucket`

**Chunk 字段**：

| 字段 | 真实值 |
|---|---|
| `chunk_type` | `CLASS` |
| `file_path` | `services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/handler/ticket/tokenbucket/TicketAvailabilityTokenBucket.java` |
| `start_line` / `end_line` | `37` / `214` |
| `class_name` / `symbol_name` | `TicketAvailabilityTokenBucket` / `TicketAvailabilityTokenBucket`（**两者相同**） |
| `signature` | `public class TicketAvailabilityTokenBucket` |
| `annotations` | `["@Slf4j", "@Component", "@RequiredArgsConstructor"]` |
| `javadoc` | `* Redis 余票令牌桶只负责购票准入，不是库存事实源。\n * 异常时始终降级放行，最终是否能占座仍由 MySQL 条件更新决定。` |
| `len(content)` | **1715 字符** |

**`content` 的开头**（这是 CLASS Summary，不是整个类源码）：

```text
* Redis 余票令牌桶只负责购票准入，不是库存事实源。
 * 异常时始终降级放行，最终是否能占座仍由 MySQL 条件更新决定。
@Slf4j
@Component
@RequiredArgsConstructor
public class TicketAvailabilityTokenBucket {
  private static final long TAKE_SUCCESS = 1L;
  private static final DefaultRedisScript<Long> TAKE_TOKEN_SCRIPT = loadScript("lua/take_token_from_bucket.lua");
  private final StringRedisTemplate stringRedisTemplate;
  ...
  public TokenTakeResult takeToken(TokenTakeRequest request);
  public void returnToken(...);
}
```

**`embedding_text()` 的真实输出开头**（1827 字符）：

```text
public class TicketAvailabilityTokenBucket      ← identity（= signature）

* Redis 余票令牌桶只负责购票准入，不是库存事实源。     ← javadoc 槽位
 * 异常时始终降级放行，最终是否能占座仍由 MySQL 条件更新决定。

* Redis 余票令牌桶只负责购票准入，不是库存事实源。     ← ← 又出现一次！
 * 异常时始终降级放行，最终是否能占座仍由 MySQL 条件更新决定。
@Slf4j
@Component
@RequiredArgsConstructor
public class TicketAvailabilityTokenBucket {     ← 这一行也和 identity 重复
  private static final long TAKE_SUCCESS = 1L;
  ...
```

**Javadoc 在 `embedding_text` 里出现了两次。** 原因很清楚：`typeRecord()` 构造的 `content`（CLASS Summary）**本身就以 Javadoc + 注解 + 声明开头**，而 `embedding_text()` 又单独取了一次 `javadoc` 槽位。同理，`signature`（`public class TicketAvailabilityTokenBucket`）也与 content 的声明行重复。

这是一个**真实的、代码可证的重复**，不是我的误读。它列入第 11 节。

### 8.4 DOCUMENT：一个真实 Markdown Chunk

取自已运行的 Markdown 解析器（源文件是本仓库的 `docs/development/01-architecture-decisions.md`，可一条命令复现）：

| 字段 | 真实值 |
|---|---|
| `source_type` / `chunk_type` | `DOCUMENT` / `DOCUMENT_SECTION` |
| `file_path` | `01-architecture-decisions.md` |
| `title` | `Decision` |
| `heading_path` | `['DevContext-Java 架构决策记录（ADR）', 'ADR-001 Java 解析器选型', 'Decision']` |
| `start_line` / `end_line` | `38` / `39` |
| `module` / `package_name` / `class_name` / `symbol_name` / `signature` / `javadoc` / `annotations` | 全部为 `None` / `None` / `None` / `None` / `None` / `None` / `[]` |
| `content` | `采用 **JavaParser**，且**只引入 `javaparser-core`**。` |

**`keyword_text()` 全文**（143 字符）：

```text
Decision                                                    ← ④ title
DevContext-Java 架构决策记录（ADR） / ADR-001 Java 解析器选型 / Decision   ← ⑤ heading_path（" / " 连接）
01-architecture-decisions.md                                ← ⑥ file_path
采用 **JavaParser**，且**只引入 `javaparser-core`**。        ← ⑧ content
```

① `symbol_name`、② `class_name`、③ `signature`、⑦ `javadoc` 全部为 `None`，被过滤。**文档 Chunk 的 `keyword_text` 因此只剩 4 行。**

**`embedding_text()` 全文**（55 字符）：

```text
Decision                                                    ← identity（= title）

采用 **JavaParser**，且**只引入 `javaparser-core`**。        ← content
```

**对比就出来了**：

```text
keyword_text  里有 heading_path 与 file_path
embedding_text 里两者都没有，只剩 title + content
```

也就是说，**文档的"它在哪一章"这个信息只进入了关键词检索，没有进入向量检索**。这是第 11 节要讲的第二个缺陷。

（说明：`my12306` 的文档 Chunk 形状与此完全一致。评测产物里可见的真实文档切片例如 `6-面试复习/模块2-核心链路深挖.md` 第 431–445 行、标题 `取舍 2：为什么"展示余票"和"令牌余量"必须是两个 Key`。此处用本仓库文档做示例，是因为它能在你的机器上一条命令复现完整输出。）

---

## 9. 关键设计为什么这样做

### 9.1 结论：`Chunk` ≠ `keyword_text` ≠ `embedding_text`

这不是"同一个东西的两种叫法"，而是**三次不同的投影**：

```text
              Chunk（17 个字段，结构化的）
                │
     ┌──────────┴──────────┐
     ↓                     ↓
keyword_text()        embedding_text()
8 个槽位               3 个槽位（identity + javadoc + content）
\n 连接               \n\n 连接
进 keyword_text 列     送 Embedding API
给“字符串匹配”用        给“语义相似”用
```

三个对象的关系可以用一句话概括：

> `Chunk` 是**事实**；`keyword_text` 与 `embedding_text` 是**针对两种检索方式各自做的特征工程**。

如果只有一个文本表示，就必然要在两种检索之间妥协：要么把路径、行号塞进 embedding 污染语义空间，要么为了 embedding 干净而让关键词检索失去路径这个检索维度。

### 9.2 为什么 `file_path` 适合 Keyword，但不适合 Embedding

**适合 Keyword 的原因**：路径里有**人能直接用作查询词的信息**。

```text
.services/ticket-services/.../PurchaseTicketTxService.java
.services/user-services/.../RBloomFilterConfiguration.java
.6-面试复习/模块2-核心链路深挖.md
```

用户完全可能这样问："`模块3-技术专题.md` 里怎么讲缓存三防？"——此时 `file_path` 是**唯一**能命中的字段。而且路径是"字符串身份"，正好是关键词检索的强项。

**不适合 Embedding 的原因**，有两个层次：

第一层是**语义噪声**。路径里有一大段所有 Chunk 都共享的前缀：

```text
services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/
```

536 个代码 Chunk 里有大量 Chunk 共享这段前缀。把这段公共前缀放进 embedding 输入，会把所有 Chunk 的向量**互相拉近**——本来靠方法体区分开的两条语义，被"我们都在同一个包里"这件事稀释了。这与第 08 篇要讲的"余弦相似度窄带"问题（0.79~0.81）是同一个方向上的恶化。

第二层是**它不含方法语义**。`PurchaseTicketTxService.java` 这个名字里的 `PurchaseTicket` 确实有语义，但 `Tx`、`impl`、`edu.swu.fcj` 没有。而 `signature` 已经提供了同一批语义信息，且更精确（它带返回类型与参数类型）。

### 9.3 为什么 `signature` 是 Metadata，却应该进入 Embedding

这是本篇最反直觉的一点，必须讲透。

**判断标准不是"它是不是元数据"，而是"它是不是语义"。**

`signature` 确实是结构化字段（由 `getDeclarationAsString()` 生成，不是源码原文），但它承载的是**代码里密度最高的语义**：

```text
doPurchaseInTransaction           → "在事务内完成购票"
takeToken                         → "取令牌"
userRegisterCachePenetrationBloomFilter → "用户注册缓存穿透布隆过滤器"
scanTimeoutOrder                  → "扫描超时订单"
```

这些标识符本身就是中文语义的英文压缩。把它们放在 `embedding_text` 的**开头**（`identity` 槽位），效果是**给每个代码 Chunk 加了一句"我是干什么的"的自我声明**。

对比 `file_path`：同样是元数据，但它提供的是**位置**而不是**语义**，所以一个进、一个不进。

这也解释了为什么 `identity` 用**级联 `or`** 而不是全拼：

```python
identity = self.signature or self.symbol_name or self.title or heading
```

- 代码 Chunk：`signature` 有值 → 取它（symbol_name 已包含在 signature 里，不必重复）
- 文档 Chunk：`signature` 为 `None` → 落到 `title`
- 极端的无标题文档：才落到 `heading`

**只取第一个非空值**，得到一段紧凑、不重复的声明。这是一种"用最少 token 表达最多身份"的压缩。

### 9.4 为什么 `start_line` / `content_hash` 不能进入 Embedding

**`start_line` / `end_line`**：

- 它们是**纯位置信息**，对"这段代码做什么"零贡献。`69`、`172` 两个数字进入 embedding 输入只会稀释真正语义 token 的权重。
- 更严重的是**它们不稳定**。往文件顶部加一行注释，所有后续方法的行号全变。而行号变了 embedding 输入就变了，缓存全部失效，全量重新计费。

**`content_hash`**：

- 它是**高熵的、无语义的**摘要字符串（`519d2ebf087cd7d4...`）。任何 embedding 模型看到它，只能当成一段随机噪声。
- 更实际的风险是**缓存雪崩**：如果把 `content_hash` 放进 `embedding_text`，那么它就是输入的一部分，输入一变 hash 就变——形成"hash 依赖自身"的退化。实际后果是缓存**永远不命中**（第 04 篇会看到缓存 key 正是 `sha256(embedding_text)`）。

### 9.5 `keyword_text` 为什么放最后、`content` 为什么最长

`content` 在 `keyword_text` 里是**第 8 个槽位（最后）**，而且长度占绝对多数（`doPurchaseInTransaction`：content 6488 / 总 6899 ≈ 94%）。这个顺序是有意的：

关键词检索的 SQL 并不是"按出现顺序读文本"，而是：

```sql
CASE WHEN symbol_name = token THEN 12.0 ...
CASE WHEN class_name  = token THEN 10.0 ...
CASE WHEN strpos(signature, token) > 0 THEN 6.0 ...
CASE WHEN strpos(keyword_text, query) > 0 THEN 2.0 ...
+ similarity(keyword_text, query)
```

前三条是**按列**命中的（不读 `keyword_text`），只有最后两条才读拼接后的 `keyword_text`。所以把 `content` 放最后的意义是：**它不会影响前三条精确匹配臂，只影响 trigram 相似度**——而 `content` 恰恰是 trigram 相似度最需要的东西（它是唯一包含方法体的字段）。这是一个"分工明确"的排列。

---

## 10. 常见误解

### 误解 1：`content_hash` 就是 Embedding 缓存的 key

**不是。** 这是两个不同的 hash，作用完全不同：

```text
Chunk.content_hash        = sha256(chunk.content)            → 存进数据库，用于内容一致性
Embedding Cache 的 key    = "model:dim:sha256(embedding_text)" → 只用于查缓存
```

真实值对比（同一条 `doPurchaseInTransaction`）：

```text
content_hash                  = 519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c
sha256(embedding_text)        = 7f188473af76d1dc48660ab8059c0d0246b2c878fa018b74b7c98284a475c8a0
```

两者不同。**为什么必须不同**：如果缓存用 `content_hash`，那么"content 没变但 javadoc 改了"这种情况会误命中旧向量——因为 `javadoc` 进了 `embedding_text` 却没进 `content_hash`。详细场景推演在 `04-Ingestion与Embedding完整链路.md`。

### 误解 2：`keyword_text` 是数据库生成列 / 视图

**不是。** 它是一个**物化的 `TEXT` 列**，由 Python 在插入前算好：

```python
# storage.py:67
chunk.keyword_text(),
```

后果很重要：**改 `keyword_text()` 的逻辑，必须重新 ingestion 才会生效**，改代码不会自动更新已有数据。（同理 `embedding` 也是物化的。）

### 误解 3：`annotations` 字段参与检索

`annotations` **不在 `keyword_text()` 的 8 个槽位里**，也不是独立的评分臂。`@Bean`、`@Transactional`、`@Component` 之所以能被搜到，是因为它们**恰好落在源码 Range 内、从而出现在 `content` 里**（第 8.2 节用真实输出证明）。所以：

```text
搜 @Transactional  →  能命中（靠 content）
但拿不到“注解字段优先”的加权
```

对 CLASS Chunk 情况略好一点：`typeRecord()` 把注解显式拼进了 Summary，所以它一定在 content 里。

### 误解 4：`module` 和 `package_name` 可以用来过滤或检索

**当前都不能。** 它们**既不进 `keyword_text` 也不进 `embedding_text`**，也没有出现在 `RESULT_COLUMNS` 里。它们目前只是存在库里、可用于人工排查的元数据。所以"只搜 `order-services` 模块"这类需求当前无法通过检索实现。

### 误解 5：文档的 `heading_path` 会进入 Embedding

**不会。** 因为 `identity = signature or symbol_name or title or heading`，而文档 Chunk 的 `title` 一定有值，所以级联在 `title` 处就停了，`heading` 永远轮不到。第 8.4 节的真实输出是最直接的证据：

```text
heading_path = ['DevContext-Java 架构决策记录（ADR）', 'ADR-001 Java 解析器选型', 'Decision']
embedding_text = "Decision\n\n采用 **JavaParser**，且**只引入 `javaparser-core`**。"
```

（`heading_path` 进了 `keyword_text`，所以它在关键词检索里是有效的。）

### 误解 6：`Chunk` 和 `SearchResult` 是同一个东西的两面

它们**字段不同、来源不同**。`SearchResult` 来自 SQL 行（`RESULT_COLUMNS`），不含 `heading_path` / `annotations` / `javadoc` / `embedding` / `keyword_text` / `content_hash`，且 `to_dict()` 会把 `content` 截成 300 字预览。看到 `top_results` 里没有 `heading_path`，不是 bug，是投影里就没有这一列。

---

## 11. 当前实现的限制/缺陷

### 11.1 CLASS / INTERFACE 的 `embedding_text` 存在真实重复（代码可证）

`typeRecord()` 拼出的 `content` 以 **Javadoc + 注解 + 声明** 开头；而 `embedding_text()` 又单独取了 `javadoc` 槽位，`identity` 又重复了声明。对 `TicketAvailabilityTokenBucket` 的真实测量：

```text
embedding_text 总长           = 1827 字符
其中 identity（signature）    =  39 字符   ← 与 content 里的声明行重复
其中 javadoc                  =  69 字符   ← 与 content 开头的两行完全重复
```

也就是说，**约 108 字符（≈6%）的 embedding 输入是纯粹的重复**。对 METHOD Chunk 也存在同类问题（`identity` 与 content 首行的多行声明重复），只是形态不同（一个单行 vs 一个换行版），不是逐字重复。

影响：轻微。它不会让检索失效，但会浪费 token、并且让 Javadoc 的语义权重被"说两遍"而虚高。

### 11.2 文档的 `heading_path` 不进 embedding（代码可证）

如第 10 节误解 5 所述。实测样本已证明。影响：文档 Chunk 的向量失去了一层"它属于哪个主题"的上下文，尤其对 `Decision`、`2.1 设计原因`、`三、有哪些约束` 这类**标题本身很短、但上级标题信息量很大**的章节不利。

### 11.3 `annotations` 不作为独立评分臂（代码可证）

注解既不在 `keyword_text` 槽位，也不在 SQL 评分臂里。对 METHOD Chunk，注解能被搜到**完全依赖"注解落在 Range 内部"**这个实现细节（事实成立，见第 02 篇），一旦将来切片策略改变，这个能力可能静默失效。

### 11.4 `module` / `package_name` 是"死字段"

存在、有值、但不参与任何检索路径，也不在结果投影里。跨模块查询（"`order-services` 里怎么关超时订单"）拿不到模块级加权。

### 11.5 TYPE 级 Chunk 的 `symbol_name == class_name`

对 CLASS / INTERFACE，两个字段同值。这本身不是错，但它导致第 06 篇要讲的**关键词检索双重加分**（同一个 token 同时命中 +12 与 +10 两臂）。在本篇的视角下，还有一个附加现象：**`keyword_text` 的前两行会完全相同**。真实例子：

```text
INTERFACE OrderItemMapper 的 keyword_text 前 3 行：
OrderItemMapper                                        ← symbol_name
OrderItemMapper                                        ← class_name（同上）
public interface OrderItemMapper extends BaseMapper<OrderItemDO>   ← signature
```

### 11.6 `keyword_text` 与 `embedding` 都是物化列，改了逻辑不会自动生效

必须重新 `ingest`。这一点在实验里很容易被误判为"改了代码没效果"。

### 11.7 超长 Chunk 尚未被截断（实测：当前未触发）

最长的 `content` 是 `doPurchaseInTransaction` 的 **6488 字符**，全库平均 436 字符。实测统计：

```text
content > 8192 字符的 Chunk : 0 个（共 538 个）
content > 6000 字符的 Chunk : 1 个
```

所以"超长方法 embedding 截断丢信息"这个已被记录的风险，在当前语料上**尚未触发**。但它是一条需要持续观察的红线——`6488` 已经是唯一接近上限的一条。

---

## 12. 建议亲自执行的实验

### 实验 A：打印同一个 Chunk 的三种形态（主实验）

在项目根目录建一个临时脚本 `tmp_chunk_inspect.py`：

```python
import json, sys
from hashlib import sha256
sys.path.insert(0, "src")
from devcontext.models import Chunk

# 从真实产物里取一条
target = None
with open("artifacts/java-chunks.jsonl", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("symbol_name") == "doPurchaseInTransaction":
            target = row
            break
assert target, "未找到目标 Chunk"

chunk = Chunk.from_dict(target)

print("=" * 70)
print("① 原始 Chunk 字段")
print("=" * 70)
for k, v in chunk.to_dict().items():
    text = repr(v)
    print(f"{k:15} = {text[:160]}{' ...' if len(text) > 160 else ''}")

print()
print("=" * 70)
print(f"② keyword_text()   长度 = {len(chunk.keyword_text())}")
print("=" * 70)
print(chunk.keyword_text()[:1200])

print()
print("=" * 70)
print(f"③ embedding_text() 长度 = {len(chunk.embedding_text())}")
print("=" * 70)
print(chunk.embedding_text()[:1200])

print()
print("=" * 70)
print("④ 三个 hash 对比")
print("=" * 70)
print("content_hash            =", chunk.content_hash)
print("sha256(content)         =", sha256(chunk.content.encode()).hexdigest())
print("sha256(keyword_text)    =", sha256(chunk.keyword_text().encode()).hexdigest())
print("sha256(embedding_text)  =", sha256(chunk.embedding_text().encode()).hexdigest())
```

运行：`python tmp_chunk_inspect.py`

**人工比较清单**（这是本实验的真正目的）：

1. `keyword_text` 的第 1 行是什么？`embedding_text` 的第 1 行是什么？为什么不同？
2. `file_path` 在哪一段里出现了？哪一段里没有？
3. `module` / `package_name` / `start_line` 有没有出现在任何一段里？
4. `content_hash` 与 `sha256(embedding_text)` 是否相等？为什么必须不等？
5. 注释掉 `embedding_text()` 里的 `self.javadoc`，重新打印——哪一部分消失了？（改完记得改回来，**不要提交这些临时改动**。）

### 实验 B：把一个 METHOD 换成 CLASS 与 DOCUMENT 各跑一遍

把上面脚本里的 `symbol_name` 过滤条件分别换成：

```python
if row.get("symbol_name") == "TicketAvailabilityTokenBucket" and row.get("chunk_type") == "CLASS":
```

观察 CLASS 的 `embedding_text` 前 10 行——你应该能看到 **Javadoc 出现两次**（第 11.1 节的缺陷）。

DOCUMENT 类型不在 `java-chunks.jsonl` 里（那是 Java 专用产物），需要直接调 Markdown 解析器：

```python
import sys
from pathlib import Path
sys.path.insert(0, "src")
from devcontext.ingestion.markdown_parser import parse_markdown_file

root = Path("docs/development")
chunks = parse_markdown_file(root, root / "01-architecture-decisions.md")
for c in chunks[:5]:
    print("title       =", c.title)
    print("heading_path=", c.heading_path)
    print("keyword_text=", repr(c.keyword_text()))
    print("embedding   =", repr(c.embedding_text()))
    print("-" * 60)
```

**观察点**：`keyword_text` 里有 `heading_path`，`embedding_text` 里没有；`embedding_text` 的 `identity` 落到了 `title`。

### 实验 C：验证 `content_hash` 的自动计算与稳定性

```python
import sys
from hashlib import sha256
sys.path.insert(0, "src")
from devcontext.models import Chunk

args = dict(repository="my12306", source_type="DOCUMENT",
            chunk_type="DOCUMENT_SECTION", file_path="x.md", content="同一段内容")

a, b = Chunk(**args), Chunk(**args)
print("自动计算:", a.content_hash)
print("手工计算:", sha256(b"同一段内容").hexdigest())
print("两次构造一致:", a.content_hash == b.content_hash)

c = Chunk(**{**args, "content": "改过的内容"})
print("改内容后:", c.content_hash)
```

预期：`content_hash` 自动非空、两次构造一致、改 `content` 后 hash 变化。同时可验证 `__post_init__` 的"只在为空时计算"语义——手工传入一个假 hash 再看它会不会被覆盖。

---

## 13. 学完后应该能够回答的问题

1. `Chunk` 的 17 个字段里，哪 5 个是必填的？为什么恰好是这几个？
2. `keyword_text()` 的 8 个槽位分别是什么、顺序如何、空值如何处理？
3. `embedding_text()` 的 `identity` 是怎么选出来的？为什么用 `or` 级联而不是全拼？
4. 为什么 `file_path` 进 `keyword_text` 但不进 `embedding_text`？
5. `signature` 是元数据，为什么反而要进 `embedding_text`？
6. `start_line` / `content_hash` 进入 `embedding_text` 会带来哪两类具体问题？
7. `content_hash` 与 Embedding 缓存 key 里的 hash 为什么必须不同？各自由什么算出来？
8. 文档 Chunk 的 `heading_path` 为什么永远进不了 `embedding_text`？源码里是哪一行导致的？
9. `annotations` 字段参与检索吗？`@Bean` 能被搜到靠的是什么？
10. 对 CLASS Chunk，`symbol_name` 与 `class_name` 是什么关系？这会在检索层引发什么后果？
11. `Chunk` 和 `SearchResult` 有哪些字段差异？为什么会缺 `heading_path`？
12. 如果我要新增一个 `source_root` 字段，需要改哪几层？各层的文件与位置是什么？

---

## 本章源码阅读任务

### 第一遍

只看（约 84 行，通读一遍）：

- `src/devcontext/models.py`（全文）

目标：理解整体流程——知道有 `Chunk` / `SearchResult` 两个类，以及 `keyword_text()` / `embedding_text()` 两个方法。

### 第二遍

重点看：

- `models.py:40-51` 的 `keyword_text()`（数清 8 个槽位与过滤条件）
- `models.py:53-57` 的 `embedding_text()`（看清 `or` 级联与 `.strip()`）
- `models.py:28-30` 的 `__post_init__`（看清 `if not self.content_hash` 这个条件）

目标：理解**关键转换**——同一个对象如何被投影成两段不同用途的文本，以及"只在空值时计算 hash"这一语义。

### 第三遍

带着问题阅读：

1. 为什么 `keyword_text()` 用 `"\n".join(...)` 而 `embedding_text()` 用 `"\n\n".join(...)`？（提示：分槽位 vs 分段落）
2. `embedding_text()` 里 `.strip()` 加在生成器表达式里而不是最后，会产生什么差异？
3. 如果我把 `file_path` 从 `keyword_text()` 挪到 `embedding_text()`，哪些 Benchmark 题目会变好、哪些会变差？（结合 `06` / `09` 两篇）
4. `from_dict` 里的 `allowed = cls.__dataclass_fields__.keys()` 这一行防御的是什么？（提示：Java 侧多输出了一个字段会怎样）

---

## 调试观察点

**断点位置 1**：`src/devcontext/ingestion/pipeline.py:52`

```python
embedding_texts = [chunk.embedding_text() for chunk in chunks]
```

观察变量：

| 变量 | 预期形态 |
|---|---|
| `chunks` | `list[Chunk]`，长度 **2901**（538 CODE + 2363 DOCUMENT） |
| `chunks[0]` | 一个 `Chunk`，`source_type` 一定是 `"CODE"`（Java 排在前面） |
| `embedding_texts` | `list[str]`，长度同 `chunks`，元素长度从几十到 6734 不等 |
| `chunks[i].embedding_text() == embedding_texts[i]` | 恒为 `True` |

**断点位置 2**：`src/devcontext/ingestion/pipeline.py:53`

```python
fingerprints = [sha256(text.encode("utf-8")).hexdigest() for text in embedding_texts]
```

观察：

| 变量 | 预期形态 |
|---|---|
| `fingerprints` | 长度 2901 的 64 位 hex 字符串列表 |
| `len(set(fingerprints))` | **略小于 2901** —— 存在重复的 embedding 输入（内容完全相同的 Chunk） |
| `fingerprints[i] == chunks[i].content_hash` | **可能相等也可能不等** —— 这是在调试时最容易看错的一处，两者算的是不同文本 |

**断点位置 3**：`src/devcontext/storage.py:67`

```python
chunk.keyword_text(),
```

观察：

| 变量 | 预期形态 |
|---|---|
| `chunk.keyword_text()` | 多行字符串，第一行是 `symbol_name` 或 `title` |
| `"\n" in chunk.keyword_text()` | `True` |
| `chunk.keyword_text().split("\n")[0]` | `doPurchaseInTransaction`（在断点命中该 Chunk 时） |
| 是否含空行 | **不应含空行** —— 若出现空行，说明 `if value` 过滤逻辑被改坏了 |

**Java 侧观察点**（配合第 02 篇）：`JavaSourceParser.parse()` 内

| 变量 | 预期形态 |
|---|---|
| `unit` | `CompilationUnit`，`unit.getPackageDeclaration()` 有值 |
| `records` | `List<ChunkRecord>`，长度 = 类型数 + 方法数 + 构造器数 |
| `records.get(1).signature()` | 方法签名是**单行**的（`getDeclarationAsString` 的结果），而 `content()` 是多行原文 |

---

## 学习完成标准

学完后我应该能够：

1. **不看源码**说出 `keyword_text()` 的 8 个槽位及其顺序，并解释为什么 `content` 放最后；
2. **不看源码**写出 `embedding_text()` 的三行实现，并说明 `identity` 的级联顺序；
3. 给定任意一个 Chunk（METHOD / CLASS / INTERFACE / DOCUMENT），**手写出**它的 `keyword_text` 与 `embedding_text` 的大致结构（哪些槽位有值、哪些被过滤）；
4. 解释 `content_hash` 与 Embedding 缓存 hash 的区别，并**举出三个具体改动场景**说明哪个 hash 会变、要不要重新请求 API；
5. **添加一个新的 Metadata 字段并说全改动清单**——例如加一个 `source_root`（值为 `"code"` 或 `"doc"`），应该改：Java 侧 `ChunkRecord.java`（加字段）、`JavaSourceParser`（填值）、`ChunkRecord` 构造调用（两处：`typeRecord` / `callableRecord`）、`models.py` 的 `Chunk`（加字段）、`sql/001_schema.sql`（加列）、`storage.replace_repository`（INSERT 列表加一项）、以及**决定它进不进 `keyword_text()` / `embedding_text()`**；
6. 指出当前 `Chunk` 数据模型的**至少三个真实缺陷**（CLASS 的 embedding 重复、文档 `heading_path` 不进 embedding、`annotations` 无独立评分臂），并说明每个缺陷会影响哪一类 Query。
