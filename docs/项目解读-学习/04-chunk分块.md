## 一、这一部分解决的核心问题

前面的解析流程会得到两种差别很大的数据：

```text
JavaParser
→ CLASS / INTERFACE / METHOD / CONSTRUCTOR

Markdown Parser
→ DOCUMENT_SECTION
```

Java 关心：

```text
类名、方法名、签名、注解、Javadoc、源码行号
```

Markdown 关心：

```text
章节标题、标题路径、正文、文档行号
```

但后面的数据库和检索系统希望只有一套流程：

```text
所有解析结果
    ↓
统一 Chunk
    ↓
关键词文本 + 向量文本
    ↓
一张 knowledge_chunk 表
    ↓
Keyword / Vector / Hybrid Search
```

所以 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:8) 的任务是：

> 把来源不同、结构不同的代码和文档，收敛成统一的知识单元，并针对关键词检索和向量检索分别生成适合它们的文本。

最关键的关系是：

```text
                       ┌─→ keyword_text()
                       │      给字符串精确匹配、子串和 pg_trgm 使用
原始解析结果 → Chunk ──┤
                       │
                       └─→ embedding_text()
                              送给 Embedding 模型，生成语义向量
```

这里一定要先建立一个概念：

> `Chunk`、`keyword_text`、`embedding_text` 不是同一个东西。

- `Chunk` 是结构化事实。
- `keyword_text` 是面向关键词检索的文本投影。
- `embedding_text` 是面向语义向量检索的文本投影。

---

# 二、系统中实际存在的四层数据模型

这一部分容易混淆，因为项目中不止一个叫“Chunk”的东西。

| 层次 | 数据对象 | 作用 |
|---|---|---|
| Java 解析层 | `ChunkRecord` | JavaParser 输出的 DTO |
| Python 处理层 | `Chunk` | 入库前统一承载 Java 和 Markdown |
| PostgreSQL | `knowledge_chunk` | 持久化 Metadata、检索文本和向量 |
| 查询结果层 | `SearchResult` | 从数据库返回给 CLI 和评测的精简结果 |

完整过程是：

```text
Java AST
    ↓
Java ChunkRecord
    ↓ JSONL + snake_case
Python Chunk
    ↓
keyword_text() + embedding_text()
    ↓
knowledge_chunk
    ↓ SQL 查询
SearchResult
```

Markdown 没有 Java DTO 和 JSONL 这一步：

```text
Markdown 文件
    ↓
Markdown Parser
    ↓
直接构造 Python Chunk
```

Java 和 Markdown 最终都汇聚到同一个 Python `Chunk`。

当前 SQL 表实际有 22 列：17 个 Chunk 字段，加上 `id`、`keyword_text`、`embedding_model`、`embedding` 和 `created_at`。表结构见 [001_schema.sql](D:/Java-learning/DevContext/sql/001_schema.sql:4)。

---

# 三、`Chunk` 类本身

定义如下：

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

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:8)。

## 1. 为什么用 `@dataclass`

`dataclass` 自动生成：

- 构造函数 `__init__`；
- 调试显示 `__repr__`；
- 相等比较 `__eq__`。

因此不需要手写大量样板代码。

例如：

```python
chunk = Chunk(
    repository="my12306",
    source_type="CODE",
    chunk_type="METHOD",
    file_path="TicketService.java",
    content="public void purchaseTicket() {}",
)
```

## 2. `slots=True` 的作用

`slots=True` 不允许给对象临时添加未声明的字段：

```python
chunk.new_field = "value"
```

会抛出 `AttributeError`。

这是一道轻量的数据契约防线：解析器如果想增加新字段，必须正式修改 `Chunk` 模型，而不能临时往实例上挂属性。

但要注意：

> Python 类型注解本身不做运行时类型校验。

例如理论上仍然可以写：

```python
Chunk(
    repository=123,
    source_type="WRONG",
    chunk_type="UNKNOWN",
    file_path=[],
    content=None,
)
```

`dataclass` 不一定立刻阻止它。`source_type` 和 `chunk_type` 的合法值主要由数据库 CHECK 约束兜底。

## 3. 哪些字段必填

前五个字段没有默认值，必须提供：

```text
repository
source_type
chunk_type
file_path
content
```

这五个字段是代码和文档都具备的最小公共集合。

其他字段都有默认值，因为：

- Markdown 没有 `class_name`、`signature`；
- Java 没有 `title`、`heading_path`；
- 有些 Java 节点没有 Javadoc；
- 行号在理论上也可能不存在。

## 4. 为什么列表使用 `default_factory=list`

这两个字段：

```python
annotations: list[str] = field(default_factory=list)
heading_path: list[str] = field(default_factory=list)
```

不能写成：

```python
annotations: list[str] = []
```

因为普通默认列表可能被多个对象共享：

```text
Chunk A 修改 annotations
可能意外影响 Chunk B
```

`default_factory=list` 会为每个 Chunk 单独创建新列表。

---

# 四、17 个字段逐个解释

## A. 身份和分类字段

### 1. `repository`

表示 Chunk 属于哪个逻辑仓库。

当前值通常是：

```text
my12306
```

主要用途：

- 数据库按仓库隔离；
- 全量重建时只删除指定仓库的数据；
- 检索 SQL 都带：

```sql
WHERE repository = %s
```

当前项目只有一个仓库，但模型结构支持未来索引多个仓库。

它不进入 `keyword_text`，也不进入 `embedding_text`。原因是所有 Chunk 都叫 `my12306`，加入检索文本没有区分能力。

### 2. `source_type`

表示知识来源：

```text
CODE
DOCUMENT
```

对应数据库约束：

```sql
source_type IN ('CODE', 'DOCUMENT')
```

它用于：

- 区分代码与文档；
- 检索结果展示；
- Evaluation 匹配 Ground Truth；
- 后续可能进行 DOC/CODE 过滤。

但当前关键词和向量查询只按 `repository` 过滤，没有按 `source_type` 分路查询。

### 3. `chunk_type`

表示更具体的知识粒度：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
DOCUMENT_SECTION
```

它回答的是：

```text
这一条知识到底是什么？
```

例如：

```text
source_type = CODE
chunk_type  = METHOD
```

表示方法代码。

```text
source_type = DOCUMENT
chunk_type  = DOCUMENT_SECTION
```

表示 Markdown 章节。

### 4. `file_path`

保存相对于各自数据根目录的路径。

代码示例：

```text
services/ticket-services/src/main/java/.../PurchaseTicketTxService.java
```

文档示例：

```text
5-后续开发规划/D4-设计分析-Feign移出事务与支付通知异步化.md
```

它不保存本机绝对路径，原因包括：

- 避免把 `D:\Java-learning\...` 写进数据库；
- 换机器后路径仍然稳定；
- 对 Evaluation 更友好；
- 可以直接作为 Citation 的文件部分。

`file_path`：

- 进入 `keyword_text`；
- 不进入 `embedding_text`；
- 会返回到 `SearchResult`。

因为路径适合精确检索：

```text
PurchaseTicketTxService
D4-设计分析
ticket-services
```

但路径中也包含大量语义噪声：

```text
src/main/java/edu/swu/fcj/my12306/biz/...
```

所以不适合作为向量输入。

---

## B. Java 工程和代码 Metadata

### 5. `module`

由 Java 文件路径推导出的模块名，例如：

```text
gateway-services
ticket-services
user-services
order-services
pay-services
```

它来自 `src` 前面的路径段：

```text
services/ticket-services/src/main/java/...
         └─────────────┘
                module
```

Markdown Chunk 中是 `None`。

当前状态：

- 会存入数据库；
- 不进入 `keyword_text`；
- 不进入 `embedding_text`；
- 不返回到 `SearchResult`；
- 当前不能按 module 过滤搜索。

所以它目前主要是“保留下来的工程 Metadata”，但没有真正参与检索。

### 6. `package_name`

Java package，例如：

```text
edu.swu.fcj.my12306.biz.ticketservice.service.impl
```

Markdown 中为 `None`。

与 `module` 一样，它当前：

- 会入库；
- 不参与关键词文本；
- 不参与向量文本；
- 不出现在查询结果中。

因此用户输入：

```text
只搜索 edu.swu.fcj.my12306.biz.orderservice 包
```

当前检索层并没有真正的 package 过滤逻辑。

### 7. `class_name`

代码 Chunk 所属的类型名。

对于类型 Chunk：

```text
CLASS TicketServiceImpl
class_name = TicketServiceImpl
```

对于方法 Chunk：

```text
METHOD purchaseTicket
class_name = PurchaseTicketTxService
```

对于嵌套类，保存最近一层祖先类型名，不保存完整的：

```text
Outer.Inner
```

Markdown 中为 `None`。

它的重要用途有两个：

1. 进入 `keyword_text`。
2. 在关键词检索 SQL 中有独立的精确匹配加分。

当前规则：

```text
查询 token 精确等于 class_name
→ +10 分
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:95)。

### 8. `symbol_name`

表示当前 Chunk 的核心符号。

| Chunk 类型 | `symbol_name` |
|---|---|
| CLASS | 类名 |
| INTERFACE | 接口名 |
| METHOD | 方法名 |
| CONSTRUCTOR | 构造器名 |
| DOCUMENT_SECTION | `None` |

例如：

```text
class_name  = PurchaseTicketTxService
symbol_name = doPurchaseInTransaction
```

这是关键词检索中权重最高的结构化字段：

```text
查询 token 精确等于 symbol_name
→ +12 分
```

它也是代码 Evaluation 判断是否命中正确符号的重要依据。

### 9. `signature`

表示代码声明的紧凑摘要。

方法示例：

```text
public PurchaseReservationResult doPurchaseInTransaction(
    PurchaseTicketReqDTO requestParam,
    String userId,
    String username,
    String orderSn,
    Map<String, PassengerActualRespDTO> passengersById
)
```

类示例：

```text
public class TicketServiceImpl implements TicketService
```

接口示例：

```text
public interface OrderItemMapper extends BaseMapper<OrderItemDO>
```

`signature` 不是源码原文，而是 JavaParser 或本项目重新构造出的规范化声明。

它：

- 进入 `keyword_text`；
- 作为代码 Chunk 的主要 `embedding_text` 身份；
- 在关键词评分中有独立的包含匹配加分：

```text
查询 token 出现在 signature
→ +6 分
```

虽然它是 Metadata，但它包含高密度语义：

```text
doPurchaseInTransaction
scanTimeoutOrder
takeToken
userRegisterCachePenetrationBloomFilter
```

所以非常适合进入 Embedding。

### 10. `annotations`

保存注解字符串列表，例如：

```python
[
    "@Transactional(rollbackFor = Exception.class)"
]
```

或者：

```python
[
    "@Slf4j",
    "@Component",
    "@RequiredArgsConstructor",
]
```

它会作为数组单独存入 PostgreSQL：

```sql
annotations TEXT[]
```

但当前有一个很容易误解的地方：

> `annotations` 字段本身没有进入 `keyword_text()`，也没有进入 `embedding_text()`。

搜索 `@Transactional` 之所以通常仍能命中，是因为：

- 方法 AST Range 通常包含方法注解；
- 注解已经出现在方法 `content` 中；
- CLASS 摘要也会显式把类注解拼进 `content`。

所以目前注解的可检索性依赖 `content`，而不是 `annotations` 数组本身。

这意味着注解没有独立评分权重。

### 11. `javadoc`

保存 Java 节点的 Javadoc 内容，例如：

```text
* 在锁内原子完成选座、条件占座和车票写入；
* 必须由外层 Bean 调用以经过 Spring 事务代理。
```

它与方法源码分开存储，因为 Javadoc 通常不在方法 AST Range 内。

`javadoc`：

- 进入 `keyword_text`；
- 进入 `embedding_text`；
- 会存入数据库；
- 当前不会出现在 `SearchResult` 中。

它对向量检索尤其重要，因为用户提问往往是自然语言：

```text
为什么这个方法必须由外层 Bean 调用？
```

而 Javadoc 本身也是自然语言，比纯代码更容易与问题形成语义相似。

---

## C. Markdown Metadata

### 12. `title`

表示 Markdown 当前章节标题，不包含 `#`。

例如：

```markdown
## 为什么把 Feign 调用移出事务
```

得到：

```text
title = 为什么把 Feign 调用移出事务
```

对于文档标题前的前言，或者完全没有标题的文档：

```text
title = 文件名，不含 .md
```

Java Chunk 中为 `None`。

它：

- 进入 `keyword_text`；
- 是文档 `embedding_text` 的身份部分；
- 会返回到 `SearchResult`。

### 13. `heading_path`

保存 Markdown 的完整标题路径，例如：

```python
[
    "购票链路优化",
    "事务边界调整",
    "为什么把 Feign 调用移出事务",
]
```

在 `keyword_text` 中被拼成：

```text
购票链路优化 / 事务边界调整 / 为什么把 Feign 调用移出事务
```

Java Chunk 中是空列表。

一个非常重要的当前实现细节是：

> `heading_path` 进入关键词文本，但没有真正进入向量文本。

原因稍后解释。

---

## D. 内容、位置和完整性字段

### 14. `content`

这是整个 Chunk 最核心的字段，所有 Chunk 都必须有。

不同类型的含义不同：

| Chunk 类型 | `content` |
|---|---|
| METHOD | 原始方法声明、注解和方法体 |
| CONSTRUCTOR | 原始构造器声明和方法体 |
| CLASS | 人工生成的类摘要 |
| INTERFACE | 人工生成的接口摘要 |
| DOCUMENT_SECTION | Markdown 章节正文，不含标题行 |

它是唯一同时进入：

```text
keyword_text
embedding_text
```

的主体字段。

它还会作为检索结果正文返回。

### 15. `start_line`

Chunk 正文在源文件中的起始行，1-based。

例如：

```text
start_line = 69
```

### 16. `end_line`

Chunk 正文在源文件中的结束行，1-based、闭区间。

例如：

```text
end_line = 172
```

行号：

- 不进入关键词文本；
- 不进入向量文本；
- 会返回到 `SearchResult`；
- 用于 Citation 和人工核验。

不把行号放入 Embedding 的原因很直接：

```text
69、172 对“这段代码做什么”没有语义贡献
```

而且文件顶部增加一行注释后，后面所有行号都会变化。如果行号进入 Embedding，就会导致大量无意义的向量重新计算。

### 17. `content_hash`

表示：

```text
SHA-256(content)
```

代码：

```python
def __post_init__(self) -> None:
    if not self.content_hash:
        self.content_hash = sha256(
            self.content.encode("utf-8")
        ).hexdigest()
```

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:28)。

作用包括：

- 标识当前正文内容；
- 检查内容是否变化；
- 为未来增量索引提供基础；
- 存入数据库用于审计和一致性判断。

但它当前不是数据库唯一键，也不直接用于全量重建幂等性。

当前幂等性方式是：

```text
同一事务中删除 repository 的旧记录
→ 再插入全部新记录
```

---

# 五、`from_dict()` 与 `to_dict()`

## 1. `from_dict()`

JavaParser 输出 JSONL 后，Python 使用：

```python
Chunk.from_dict(json.loads(line))
```

实现是：

```python
@classmethod
def from_dict(cls, value: dict[str, Any]) -> "Chunk":
    allowed = cls.__dataclass_fields__.keys()
    return cls(**{
        key: value[key]
        for key in allowed
        if key in value
    })
```

它只保留 `Chunk` 已声明的字段。

例如 Java JSON 多了：

```json
{
  "future_field": "value"
}
```

Python 会忽略它，而不是传给构造器。

好处是：

- Java 输出偶尔增加额外字段，不会立即把旧 Python 代码打崩。

风险是：

- Java 新增了重要字段，但 Python 没同步模型时，该字段会被静默丢弃。

如果缺少可选字段，会使用默认值；如果缺少：

```text
repository
source_type
chunk_type
file_path
content
```

这些必填字段，构造 `Chunk` 会失败。

另一个细节是：

```python
if not self.content_hash:
    自动计算
```

如果输入已经带了非空 `content_hash`，Python会直接信任，不会重新验证它是否真的等于 `sha256(content)`。

Java 输出中已经计算了 Hash，所以正常流程没问题；但模型本身不强制验证。

## 2. `to_dict()`

```python
def to_dict(self) -> dict[str, Any]:
    return asdict(self)
```

它把 dataclass 转成普通字典，用于：

- 调试；
- JSON 序列化；
- 测试；
- 人工观察字段。

---

# 六、`keyword_text()` 详细解释

代码是：

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

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:40)。

## 1. 拼接顺序

总共八个槽位：

```text
① symbol_name
② class_name
③ signature
④ title
⑤ heading_path
⑥ file_path
⑦ javadoc
⑧ content
```

字段之间用单个换行：

```text
field 1
field 2
field 3
...
```

空值自动跳过，不会为 `None` 留空行。

## 2. METHOD Chunk 的实际形态

假设有：

```text
class_name  = PurchaseTicketTxService
symbol_name = doPurchaseInTransaction
signature   = public PurchaseReservationResult doPurchaseInTransaction(...)
file_path   = services/ticket-services/.../PurchaseTicketTxService.java
javadoc     = 在锁内原子完成选座、条件占座和车票写入
content     = @Transactional...
              public PurchaseReservationResult doPurchaseInTransaction(...) {
                  ...
              }
```

`keyword_text()` 大致是：

```text
doPurchaseInTransaction
PurchaseTicketTxService
public PurchaseReservationResult doPurchaseInTransaction(...)
services/ticket-services/.../PurchaseTicketTxService.java
在锁内原子完成选座、条件占座和车票写入
@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}
```

可以看出同一个方法名可能出现多次：

- `symbol_name` 中一次；
- `signature` 中一次；
- `content` 中一次。

这不是去重后的搜索文档，而是为了提高符号可发现性。

不过真正的高权重不是来自“出现三次”，而是数据库还直接对结构化列设置了单独评分。

## 3. DOCUMENT_SECTION 的实际形态

假设：

```text
title = Feign 移出事务
heading_path = [
    "购票链路优化",
    "事务边界调整",
    "Feign 移出事务"
]
file_path = 5-后续开发规划/D4-设计分析.md
content = 远程调用不应该占用本地数据库事务时间……
```

生成：

```text
Feign 移出事务
购票链路优化 / 事务边界调整 / Feign 移出事务
5-后续开发规划/D4-设计分析.md
远程调用不应该占用本地数据库事务时间……
```

这里 `title` 会在完整标题路径中再次出现，所以文档叶子标题也存在一定重复。

## 4. 哪些字段没有进入 `keyword_text`

明确没有：

```text
repository
source_type
chunk_type
module
package_name
annotations
start_line
end_line
content_hash
```

原因不完全相同：

- `repository`：所有记录几乎相同。
- `source_type`、`chunk_type`：更适合结构化过滤。
- 行号：没有主题语义。
- Hash：完全没有人类语义。
- `annotations`：当前通过 `content` 间接检索。
- `module`、`package_name`：当前尚未接入检索，这是实现限制，不代表它们永远不应该进入。

---

# 七、关键词检索实际怎样使用这些文本

这里有一个需要纠正的认识：

> 当前实现不是传统 PostgreSQL `tsvector/ts_rank` 全文检索，而是“结构化精确匹配 + 子串匹配 + `pg_trgm` 相似度”。

实现见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:95)。

## 1. 提取代码标识符

查询中通过正则提取英文式标识符：

```python
[A-Za-z_$][A-Za-z0-9_$]{2,}
```

例如：

```text
TicketServiceImpl 里的 purchaseTicket 怎么实现
```

提取：

```text
TicketServiceImpl
purchaseTicket
```

中文词不会进入这个标识符列表，但仍然会走整句子串和 trigram 相似度。

## 2. 评分组成

关键词分数是以下几部分之和：

```text
symbol_name 精确匹配       +12
class_name 精确匹配        +10
signature 包含 token        +6
keyword_text 包含完整查询    +2
pg_trgm similarity         +相似度
```

例如查询：

```text
purchaseTicket
```

某个正确 METHOD Chunk 可能得到：

```text
symbol_name == purchaseTicket   +12
signature 包含 purchaseTicket    +6
keyword_text 包含完整查询        +2
trigram similarity              +0.x
```

而一个只在正文中调用 `purchaseTicket()` 的其他方法，通常只有：

```text
keyword_text 包含查询       +2
trigram similarity          +0.x
```

所以声明本身一般会排在调用处前面。

## 3. CLASS 的双重加分现象

对于类型 Chunk：

```text
symbol_name = TicketServiceImpl
class_name  = TicketServiceImpl
```

查询 `TicketServiceImpl` 会同时触发：

```text
symbol_name 精确匹配 +12
class_name 精确匹配  +10
```

得到至少 `+22`。

这有助于类名查询稳定命中类摘要，但也属于重复加权：同一个事实被两个字段重复奖励。

## 4. `keyword_text` 是物化列

入库时执行：

```python
chunk.keyword_text()
```

然后将结果存进数据库的 `keyword_text` 列，见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:40)。

这意味着：

> 修改 `keyword_text()` 之后，数据库里的旧数据不会自动变化。

必须重新执行：

```text
devcontext ingest
```

才能让新拼接规则生效。

---

# 八、`embedding_text()` 详细解释

代码如下：

```python
def embedding_text(self) -> str:
    heading = " / ".join(self.heading_path)
    identity = (
        self.signature
        or self.symbol_name
        or self.title
        or heading
    )
    values = [identity, self.javadoc, self.content]
    return "\n\n".join(
        value.strip()
        for value in values
        if value and value.strip()
    )
```

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:53)。

它只包含三个逻辑槽位：

```text
identity
javadoc
content
```

之间使用两个换行：

```text
identity

javadoc

content
```

双换行相当于把“身份”“自然语言说明”“正文”分成三个段落。

## 1. `identity` 的优先级

这是一个短路 `or`：

```text
signature
    ↓ 没有才看
symbol_name
    ↓ 没有才看
title
    ↓ 没有才看
heading_path
```

它不是把四者全部拼接，而是只选择第一个非空值。

### 对代码 Chunk

当前所有代码 Chunk 都有 `signature`，所以实际使用：

```text
identity = signature
```

不会再单独加入：

```text
symbol_name
class_name
```

因为方法名通常已经包含在签名里。

例如：

```text
public PurchaseReservationResult doPurchaseInTransaction(...)
```

已经比单独的：

```text
doPurchaseInTransaction
```

包含更多语义。

### 对 Markdown Chunk

文档 Chunk 没有 `signature` 和 `symbol_name`，但一定会有 `title`，所以：

```text
identity = title
```

这导致：

```text
heading_path
```

实际上不会进入 Embedding。

例如：

```text
title = Decision

heading_path = [
    "DevContext 架构决策",
    "ADR-001 Java 解析器选型",
    "Decision"
]
```

当前 `embedding_text` 是：

```text
Decision

采用 JavaParser，并且只引入 javaparser-core。
```

而不是：

```text
DevContext 架构决策 / ADR-001 Java 解析器选型 / Decision

采用 JavaParser，并且只引入 javaparser-core。
```

因此，当标题本身非常泛化时：

```text
Decision
背景
方案
结论
实验结果
问题分析
```

向量可能缺少上级主题信息。

## 2. METHOD 的 Embedding 输入

假设方法：

```text
signature =
public PurchaseReservationResult doPurchaseInTransaction(...)

javadoc =
在锁内原子完成选座、条件占座和车票写入

content =
@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}
```

最终输入：

```text
public PurchaseReservationResult doPurchaseInTransaction(...)

在锁内原子完成选座、条件占座和车票写入

@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}
```

Embedding 模型同时看到：

- 方法身份；
- 自然语言业务说明；
- 实际实现。

这对自然语言问题非常有帮助：

```text
哪个方法在事务内完成选座和车票写入？
```

即使问题没有写出方法名，Javadoc 和源码也可能产生语义匹配。

## 3. DOCUMENT_SECTION 的 Embedding 输入

假设：

```text
title =
为什么把 Feign 调用移出事务

content =
远程调用持续时间不可控。如果放在数据库事务中，
会延长事务持有连接和锁的时间……
```

得到：

```text
为什么把 Feign 调用移出事务

远程调用持续时间不可控。如果放在数据库事务中，
会延长事务持有连接和锁的时间……
```

这种形式适合匹配：

```text
为什么需要缩短购票事务边界？
远程调用放在事务里有什么问题？
```

---

# 九、为什么关键词文本与向量文本必须分开

## 1. 关键词检索关心“字面上有没有”

关键词检索适合：

```text
purchaseTicket
TicketServiceImpl
D4-设计分析
@Transactional
RDelayedQueue
```

因此它需要：

- 符号名；
- 类名；
- 签名；
- 标题；
- 完整标题路径；
- 文件路径；
- Javadoc；
- 正文。

即使路径不是“语义”，只要用户可能直接输入，它对关键词检索就有价值。

## 2. 向量检索关心“意思是否相近”

向量检索适合：

```text
为什么远程调用不应该放在数据库事务中？
系统如何防止余票缓存被击穿？
超时未支付订单是怎样关闭的？
```

因此它需要的是：

- 高密度身份信息；
- 自然语言说明；
- 真实内容。

而这些字段会污染向量：

```text
D:\Java-learning\...
69
172
519d2ebf087cd...
my12306
DOCUMENT_SECTION
```

它们不会说明“这段内容在讲什么”。

## 3. 两套文本各自保留最适合的信息

| 字段 | Keyword | Embedding | 原因 |
|---|:---:|:---:|---|
| `symbol_name` | 是 | 通常被 signature 替代 | 精确符号检索 |
| `class_name` | 是 | 否 | 类名匹配强，但可能重复 |
| `signature` | 是 | 是 | 高密度代码语义 |
| `title` | 是 | 是 | 文档章节身份 |
| `heading_path` | 是 | 当前否 | 适合路径式关键词；当前向量缺失父级语义 |
| `file_path` | 是 | 否 | 适合文件名搜索，但包含路径噪声 |
| `javadoc` | 是 | 是 | 同时适合关键词和自然语言语义 |
| `content` | 是 | 是 | 知识主体 |
| `annotations` | 间接 | 间接 | 当前通过 content 出现 |
| `module` | 否 | 否 | 当前未接入 |
| `package_name` | 否 | 否 | 当前未接入 |
| `start_line/end_line` | 否 | 否 | 仅定位 |
| `content_hash` | 否 | 否 | 无语义 |

---

# 十、`content_hash` 与 Embedding 缓存键不是一回事

这是这一部分最容易混淆的地方。

## 1. `content_hash`

计算：

```text
content_hash = SHA-256(content)
```

只看正文。

例如：

```text
content = "public void purchaseTicket() {}"
```

相同正文一定得到相同 `content_hash`。

它存入数据库，用于表达：

```text
这个 Chunk 的正文内容是什么版本？
```

## 2. Embedding 指纹

Ingestion 中先计算所有：

```python
embedding_texts = [
    chunk.embedding_text()
    for chunk in chunks
]
```

然后：

```python
fingerprints = [
    sha256(text.encode("utf-8")).hexdigest()
    for text in embedding_texts
]
```

见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:52)。

也就是说：

```text
Embedding 指纹 = SHA-256(embedding_text)
```

由于 `embedding_text` 包含：

```text
identity + javadoc + content
```

所以它通常不等于：

```text
SHA-256(content)
```

## 3. 最终缓存键

缓存还会加上模型和维度：

```python
f"{model}:{dimensions}:{fingerprint}"
```

见 [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:24)。

最终类似：

```text
text-embedding-v4:1024:7f188473af76...
```

这样可以避免：

- 换了 Embedding 模型却错误复用旧向量；
- 换了向量维度却错误复用旧向量；
- Javadoc 或 signature 变化却错误复用旧向量。

## 4. 几种变化场景

| 变化 | `content_hash` | Embedding 指纹 | 是否重算向量 |
|---|---|---|---|
| 方法体变化 | 变化 | 变化 | 是 |
| Javadoc 变化，方法体不变 | 不变 | 变化 | 是 |
| signature 变化，content 同时变化 | 通常变化 | 变化 | 是 |
| 仅文件路径变化 | 不变 | 不变 | 否 |
| 仅行号变化 | 不变 | 不变 | 否 |
| Embedding 模型变化 | 不变 | 指纹文本不变，但缓存前缀变化 | 是 |
| 向量维度变化 | 不变 | 指纹文本不变，但缓存前缀变化 | 是 |

其中“仅文件路径变化不重算向量”是合理的，因为：

```text
文件从 A.java 移到 B.java
```

并不一定改变这段代码的语义。

但 `keyword_text` 会变化，因为它包含路径；重新 ingestion 后关键词列会更新。

---

# 十一、Chunk 如何真正入库

Ingestion 先得到：

```python
chunks = code_chunks + document_chunks
```

然后对每个 Chunk：

```text
embedding_text()
→ 查 Embedding 缓存
→ 缺失的调用百炼 API
→ 得到 1024 维 vector
```

接着 `replace_repository()` 构造数据库行：

```python
(
    chunk.repository,
    chunk.source_type,
    chunk.chunk_type,
    chunk.file_path,
    chunk.module,
    chunk.package_name,
    chunk.class_name,
    chunk.symbol_name,
    chunk.signature,
    chunk.annotations,
    chunk.javadoc,
    chunk.title,
    chunk.heading_path,
    chunk.content,
    chunk.keyword_text(),
    chunk.start_line,
    chunk.end_line,
    chunk.content_hash,
    embedding_model,
    embedding,
)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:40)。

所以数据库中的一行不是单纯的 Chunk，而是：

```text
原始 Chunk 字段
+
派生 keyword_text
+
embedding_model
+
embedding vector
+
数据库 id / created_at
```

---

# 十二、`SearchResult` 为什么不是完整 Chunk

查询结果使用另一个数据模型：

```python
@dataclass(slots=True)
class SearchResult:
    id: int
    source_type: str
    chunk_type: str
    file_path: str
    content: str
    start_line: int | None
    end_line: int | None
    class_name: str | None
    symbol_name: str | None
    signature: str | None
    title: str | None
    score: float
```

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:60)。

它只包含检索展示和评测需要的字段。

没有：

```text
repository
module
package_name
annotations
javadoc
heading_path
content_hash
keyword_text
embedding
embedding_model
```

这会带来几个实际结果。

## 1. 文档标题路径目前无法从搜索结果获取

数据库保存了：

```text
heading_path
```

但查询的 `RESULT_COLUMNS` 没有选择它。

因此当前 CLI 可以展示：

```text
title
file_path
start_line
```

但不能完整展示：

```text
系统设计 / 订单模块 / 事务边界
```

这是文档 Citation 的一个当前缺口。

## 2. 搜索结果看不到独立 Javadoc 和 annotations

这些信息可能已经出现在 `content`，但无法以结构化字段形式返回。

## 3. JSON 输出只有正文预览

`SearchResult.to_dict()` 会：

```python
data["content_preview"] = " ".join(self.content.split())[:300]
del data["content"]
```

也就是：

- 把换行压成空格；
- 截取前 300 个字符；
- 删除完整 `content`。

所以 CLI 的 JSON 格式和评测结果中通常看到的是预览，不是完整正文。

---

# 十三、关键词、向量与混合检索怎样使用这些数据

## 1. Keyword Search

读取：

```text
symbol_name
class_name
signature
keyword_text
```

综合计算分数。

适合：

```text
精确方法名
类名
注解
文件名
技术名词
标题
```

## 2. Vector Search

查询文本先生成查询向量：

```python
query_vector = client.embed_query(query)
```

然后与 `knowledge_chunk.embedding` 计算 cosine similarity：

```sql
1 - (embedding <=> query_vector)
```

适合：

```text
自然语言问题
同义表达
Why / How 类查询
```

## 3. Hybrid Search

两路各取至少 20 条：

```text
Keyword Top-20
Vector Top-20
```

再做 RRF：

```text
score(chunk) += 1 / (60 + rank)
```

见 [hybrid.py](D:/Java-learning/DevContext/src/devcontext/retrieval/hybrid.py:9)。

RRF 不比较关键词分数和向量相似度的绝对值，只看各自排名。

这正是统一 `Chunk` 模型的价值：

```text
同一个数据库 id
可以同时出现在关键词排名和向量排名中
→ 可以直接融合
```

---

# 十四、当前模型的几个重要限制

## 1. `heading_path` 没有进入 Embedding

对于标题很泛的章节：

```text
背景
原因
方案
Decision
Result
```

父标题往往才包含真正主题。

当前只 Embedding：

```text
title + content
```

没有：

```text
完整 heading_path + content
```

可能降低文档向量检索质量。

比较合理的改进方向可能是：

```python
identity = (
    signature
    or symbol_name
    or " / ".join(heading_path)
    or title
)
```

或者对文档单独处理：

```text
完整标题路径

正文
```

但修改后必须重新计算全部文档向量。

## 2. `module` 和 `package_name` 当前是“死 Metadata”

有值，但：

- 不参与 keyword；
- 不参与 embedding；
- 不用于 SQL 过滤；
- 不返回结果。

所以它们当前更像是为后续能力预留。

## 3. annotations 没有独立加权

搜索：

```text
@Transactional
```

依赖注解出现在 `content` 中。

更明确的做法可以是：

- 把 annotations 加入 `keyword_text`；
- SQL 增加注解数组匹配；
- 为注解精确命中单独加分。

当前还没有这样做。

## 4. CLASS/INTERFACE 存在重复

对类型 Chunk：

```text
symbol_name == class_name
```

因此：

- `keyword_text` 前两行重复；
- SQL 精确匹配会双重加分。

此外，CLASS 摘要的 `content` 已经包含：

- Javadoc；
- 类型声明。

而 `embedding_text` 又单独加入：

- signature；
- javadoc。

所以向量输入也会有部分重复。

## 5. Markdown 的 title 也有重复

由于 `heading_path` 最后一项通常就是 `title`：

```text
title = 事务边界
heading_path = 购票优化 / 事务边界
```

`keyword_text` 会同时包含两次“事务边界”。

这有利于召回，但也会在 trigram 相似度中产生一定重复权重。

## 6. 两套检索数据都是物化的

数据库保存的是当时计算出的：

```text
keyword_text
embedding
```

因此修改：

```python
keyword_text()
embedding_text()
```

不会影响已经入库的数据。

必须重新 ingestion：

```text
重新解析
→ 重新构造文本
→ 重新生成或复用向量
→ 重新写库
```

## 7. Python 模型没有完整业务校验

当前 `Chunk` 没有验证：

```text
source_type 是否合法
chunk_type 是否与 source_type 匹配
start_line 是否 <= end_line
METHOD 是否一定有 symbol_name
DOCUMENT_SECTION 是否一定有 title
content_hash 是否与 content 一致
content 是否为空
```

部分问题最终会被数据库约束发现，部分则可能正常入库。

---

# 十五、用一句话理解三个对象

可以把它们想象成一本书：

```text
Chunk
= 图书馆里的结构化书目记录
  包含书名、作者、位置、分类、正文等全部事实

keyword_text
= 为关键词搜索准备的倒排检索材料
  尽量包含人可能直接输入的名字、路径和原文

embedding_text
= 为语义模型准备的内容摘要
  去掉路径、行号、Hash，只保留“它是谁”和“它讲什么”
```

在本项目中：

```text
Chunk 是事实层
keyword_text 是字面检索特征
embedding_text 是语义检索特征
```

---

# 十六、推荐的源码阅读顺序

如果要自己逐行学习，建议按这个顺序：

1. 先看 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:8)

   重点理解：

   ```text
   17 个字段
   __post_init__
   keyword_text
   embedding_text
   SearchResult
   ```

2. 再看 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:22)

   观察：

   ```text
   code_chunks + document_chunks
   embedding_text()
   sha256(embedding_text)
   Embedding 缓存
   ```

3. 再看 [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:7)

   理解：

   ```text
   model + dimensions + embedding fingerprint
   ```

4. 再看 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:40)

   观察：

   ```text
   keyword_text 如何物化入库
   结构化字段如何单独加权
   embedding 如何用于 cosine search
   ```

5. 最后看 [hybrid.py](D:/Java-learning/DevContext/src/devcontext/retrieval/hybrid.py:9)

   理解为什么统一 Chunk 和统一数据库 ID 使 RRF 融合成为可能。

最值得牢牢记住的是：

```text
Parser 决定 Chunk 边界
Chunk 决定保留哪些事实
keyword_text 决定精确检索能看到什么
embedding_text 决定向量模型能理解什么
SearchResult 决定用户最终能看到什么
```

任何字段如果在这条链路中的某一步被漏掉，后面的组件都无法“自动知道”它。