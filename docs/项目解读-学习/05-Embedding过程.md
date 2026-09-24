## 一、先理解这条链路在做什么

前一步已经把 Java 和 Markdown 转成了统一的 `Chunk`：

```text
Java / Markdown
      ↓
    Chunk
      ↓
embedding_text()
```

Embedding 链路继续完成：

```text
embedding_text
      ↓
计算文本指纹
      ↓
查询本地缓存
      │
      ├─ 命中：直接复用 1024 维向量
      │
      └─ 未命中：批量调用百炼 Embedding API
                         ↓
                    校验向量
                         ↓
                    追加写入缓存
      ↓
所有 Chunk 都有向量
      ↓
事务化写入 PostgreSQL / pgvector
```

一句话概括：

> Embedding 把每个 Chunk 的语义文本转换成固定长度的数字向量；缓存避免相同文本反复调用外部 API。

主要代码入口：

- [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:22)：编排整个流程。
- [client.py](D:/Java-learning/DevContext/src/devcontext/embedding/client.py:15)：调用百炼 Embedding API。
- [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:7)：本地向量缓存。

---

# 二、什么是 Embedding

Embedding 可以理解为：

```text
一段文本
   ↓
Embedding 模型
   ↓
一组固定长度的浮点数
```

例如：

```text
为什么把 Feign 调用移出事务？
```

可能被转换成：

```text
[
  0.0123,
 -0.0841,
  0.0317,
 ...
]
```

本项目固定使用：

```text
模型：text-embedding-v4
维度：1024
```

所以每个 Chunk 最终得到：

```text
list[float]，长度必须等于 1024
```

这些数字的单个维度通常无法直接解释，但整个向量在高维空间中表示文本语义。

语义相近的文本，向量方向通常比较接近：

```text
“为什么缩短事务边界”
“远程调用为什么不能放在数据库事务里”
```

即使它们没有使用完全相同的关键词，也可能有较高的余弦相似度。

---

# 三、Embedding 在写入阶段和查询阶段分别做什么

Embedding 实际出现两次。

## 1. Ingestion 阶段：为所有 Chunk 生成文档向量

```text
Chunk.embedding_text()
        ↓
text-embedding-v4
        ↓
1024 维向量
        ↓
knowledge_chunk.embedding
```

这是离线索引构建过程。

当前验证数据有：

```text
代码 Chunk： 538
文档 Chunk：2363
总 Chunk：  2901
```

因此完整索引需要让 2901 个 Chunk 都拥有一个 1024 维向量。实际数据见 [verification.md](D:/Java-learning/DevContext/docs/verification.md:15)。

## 2. 搜索阶段：为用户问题生成查询向量

用户搜索：

```text
为什么要把 Feign 调用移出事务？
```

查询过程：

```text
用户问题
   ↓
embed_query()
   ↓
1024 维查询向量
   ↓
与数据库里的 2901 个 Chunk 向量计算 cosine similarity
   ↓
返回最相似的 Top-K
```

查询入口在 [service.py](D:/Java-learning/DevContext/src/devcontext/retrieval/service.py:15)。

需要注意：

> 当前缓存只用于文档 Chunk 的向量构建，不缓存查询向量。

所以相同查询执行多次时，当前仍会重复调用 Embedding API。

---

# 四、第一步：构造 `embedding_text`

Embedding API 不接收整个 `Chunk` 对象，而只接收：

```python
chunk.embedding_text()
```

代码是：

```python
def embedding_text(self) -> str:
    heading = " / ".join(self.heading_path)
    identity = self.signature or self.symbol_name or self.title or heading
    values = [identity, self.javadoc, self.content]
    return "\n\n".join(
        value.strip()
        for value in values
        if value and value.strip()
    )
```

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:53)。

因此送给 API 的文本主要由三部分组成：

```text
身份 identity

自然语言说明 javadoc

正文 content
```

## 代码 Chunk 示例

假设：

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

发送给 Embedding API 的文本是：

```text
public PurchaseReservationResult doPurchaseInTransaction(...)

在锁内原子完成选座、条件占座和车票写入

@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}
```

## 文档 Chunk 示例

假设：

```text
title =
为什么把 Feign 调用移出事务

content =
远程调用持续时间不可控。如果放在数据库事务中，
会延长连接和数据库锁的持有时间……
```

发送给 API：

```text
为什么把 Feign 调用移出事务

远程调用持续时间不可控。如果放在数据库事务中，
会延长连接和数据库锁的持有时间……
```

## 哪些字段不会发送给 Embedding API

以下字段不会进入输入：

```text
repository
source_type
chunk_type
file_path
module
package_name
class_name
start_line
end_line
content_hash
```

因为它们大多是工程位置或管理信息，不直接表示 Chunk 的语义。

---

# 五、第二步：计算 Embedding 指纹

Pipeline 为每个 `embedding_text` 计算 SHA-256：

```python
embedding_texts = [
    chunk.embedding_text()
    for chunk in chunks
]

fingerprints = [
    sha256(text.encode("utf-8")).hexdigest()
    for text in embedding_texts
]
```

见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:52)。

这里的 Fingerprint 表示：

```text
这段实际发送给 Embedding 模型的文本是什么
```

例如：

```text
embedding_text =
"public void purchaseTicket(...)\n\n购票方法说明\n\npublic void..."

fingerprint =
sha256(embedding_text)
```

结果是一个 64 位十六进制字符串：

```text
7b59cb088a058e094feb767c143be6214c4bf72a212e62514743fdd122dfbf85
```

指纹的作用不是语义检索，而是缓存查找：

```text
相同 embedding_text
→ 相同 Fingerprint
→ 可以复用同一向量
```

---

# 六、三个 Hash/Key 不要混淆

这里实际有三个概念。

## 1. Chunk 的 `content_hash`

```text
content_hash = SHA-256(chunk.content)
```

只包含正文。

存入数据库，用于表示正文内容版本。

## 2. Embedding Fingerprint

```text
fingerprint = SHA-256(chunk.embedding_text())
```

它包含：

```text
identity + javadoc + content
```

这是 Pipeline 查缓存时使用的值。

## 3. 最终缓存 Key

缓存把模型、维度和 Fingerprint 拼起来：

```text
cache_key =
model + ":" + dimensions + ":" + fingerprint
```

例如：

```text
text-embedding-v4:1024:7b59cb088a058e094feb767c143be6214c4bf72a212e62514743fdd122dfbf85
```

代码见 [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:24)。

完整关系：

```text
chunk.content
    ↓ SHA-256
content_hash
    ↓
写入数据库

chunk.embedding_text()
    ↓ SHA-256
fingerprint
    ↓ 加上模型和维度
text-embedding-v4:1024:fingerprint
    ↓
Embedding Cache Key
```

## 为什么缓存不能直接使用 `content_hash`

考虑这种情况：

```text
方法体 content 没变
Javadoc 变了
```

此时：

```text
content_hash     不变
embedding_text   变化
Embedding 向量   应该重新生成
```

如果缓存只使用 `content_hash`，就会错误复用旧向量。

同理，签名变化也可能改变 `embedding_text`。

所以缓存必须对“真正送给模型的完整文本”计算 Hash。

---

# 七、模型名和维度为什么也要放进缓存 Key

假设同一段文本：

```text
为什么调整事务边界
```

先使用：

```text
text-embedding-v4，1024 维
```

后来改成：

```text
另一个模型，1536 维
```

虽然文本没有变化，但两个模型生成的向量：

- 维度不同；
- 坐标空间不同；
- 不能相互比较；
- 不能写入同一个 `VECTOR(1024)` 列。

因此缓存 Key 必须包含：

```text
模型名
维度
文本 Fingerprint
```

变化规则：

| 变化 | 是否命中旧缓存 |
|---|---|
| 文本不变、模型不变、维度不变 | 命中 |
| 正文变化 | 不命中 |
| Javadoc 变化 | 不命中 |
| signature/title 变化 | 不命中 |
| 仅文件路径变化 | 命中 |
| 仅行号变化 | 命中 |
| 模型名变化 | 不命中 |
| 维度变化 | 不命中 |

这使缓存失效规则基本等同于：

> 只有会改变向量语义或向量空间的变化，才重新生成向量。

一个当前限制是：缓存键没有包含 `base_url`、提供商或模型版本号。如果服务端在模型名不变的情况下升级了模型，旧缓存仍会命中。

---

# 八、第三步：加载本地缓存

缓存文件是：

```text
artifacts/embedding-cache.jsonl
```

格式为 JSONL，一行一条向量：

```json
{
  "key": "text-embedding-v4:1024:7b59cb...",
  "vector": [0.0123, -0.0841, 0.0317]
}
```

实际向量有 1024 个浮点数。

初始化缓存：

```python
cache = EmbeddingCache(
    project_root() / "artifacts" / "embedding-cache.jsonl",
    settings.embedding_model,
    settings.embedding_dimensions,
)
```

见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:47)。

## 加载过程

构造 `EmbeddingCache` 时，如果文件存在，会逐行读取：

```python
with path.open(encoding="utf-8") as handle:
    for line in handle:
        try:
            item = json.loads(line)
            vector = item["vector"]
            if len(vector) == dimensions:
                self.values[item["key"]] = vector
        except (...):
            continue
```

见 [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:7)。

也就是说：

```text
JSON 正常 + 有 vector + 维度正确
→ 加载到内存字典

JSON 损坏 / 缺字段 / 类型错误 / 维度错误
→ 跳过这一行，继续加载后面
```

这样即使缓存最后一行因为进程异常而写了一半，也不会导致整个缓存不可用。

## 为什么还要检查维度

如果当前配置要求 1024 维，但文件里有：

```json
{"vector": [1.0, 2.0, 3.0]}
```

这一行会被忽略。

测试见 [test_embedding_cache.py](D:/Java-learning/DevContext/tests/test_embedding_cache.py:6)。

---

# 九、缓存查找过程

Pipeline 按原始 Chunk 顺序建立向量槽位：

```python
vectors = [
    cache.get(fingerprint)
    for fingerprint in fingerprints
]
```

结果可能是：

```text
[
    已缓存向量,
    None,
    已缓存向量,
    None,
    None,
    已缓存向量,
]
```

接着找出缺失位置：

```python
missing_indexes = [
    index
    for index, vector in enumerate(vectors)
    if vector is None
]
```

例如：

```text
missing_indexes = [1, 3, 4]
```

再只提取缺失文本：

```python
missing_texts = [
    embedding_texts[index]
    for index in missing_indexes
]
```

所以已经命中的文本不会再次发送到 API。

---

# 十、为什么需要保存 `missing_indexes`

假设原始顺序是：

| 原始 Chunk 索引 | Chunk | 缓存 |
|---:|---|---|
| 0 | A | 命中 |
| 1 | B | 未命中 |
| 2 | C | 命中 |
| 3 | D | 未命中 |
| 4 | E | 未命中 |
| 5 | F | 命中 |

初始化：

```text
vectors =
[
    vector_A,
    None,
    vector_C,
    None,
    None,
    vector_F,
]
```

缺失列表：

```text
missing_indexes = [1, 3, 4]
missing_texts   = [text_B, text_D, text_E]
```

API 返回：

```text
[vector_B, vector_D, vector_E]
```

不能直接把它们添加到 `vectors` 末尾，因为必须恢复到原始 Chunk 位置：

```text
vector_B → vectors[1]
vector_D → vectors[3]
vector_E → vectors[4]
```

这个映射由回调完成：

```python
def save_batch(start, batch_vectors):
    for offset, vector in enumerate(batch_vectors):
        chunk_index = missing_indexes[start + offset]
        vectors[chunk_index] = vector
        cache.append(fingerprints[chunk_index], vector)
```

见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:58)。

完成后：

```text
vectors =
[
    vector_A,
    vector_B,
    vector_C,
    vector_D,
    vector_E,
    vector_F,
]
```

这样 `chunks[i]` 与 `vectors[i]` 始终保持对应。

---

# 十一、第四步：批量请求 Embedding API

客户端入口：

```python
embed_documents(
    texts,
    on_batch=save_batch,
)
```

见 [client.py](D:/Java-learning/DevContext/src/devcontext/embedding/client.py:47)。

## 1. 默认批量大小

默认：

```python
batch_size = 10
```

而且强制限制：

```text
1 <= batch_size <= 10
```

否则直接抛异常：

```python
ValueError(
    "text-embedding-v4 batch_size must be between 1 and 10"
)
```

测试见 [test_embedding.py](D:/Java-learning/DevContext/tests/test_embedding.py:4)。

假设缺失 27 条文本，就会分成：

```text
Batch 1：10 条
Batch 2：10 条
Batch 3： 7 条
```

## 2. 为什么批量请求

如果逐个请求 2901 个 Chunk：

```text
2901 次 HTTP 请求
```

开销很大。

批量大小为 10 时，最坏大约：

```text
ceil(2901 / 10) = 291 次请求
```

缓存命中后，请求数通常会更少。

## 3. 批次回调的 `start`

客户端每完成一个批次，调用：

```python
on_batch(
    (batch_number - 1) * batch_size,
    batch_vectors,
)
```

假设批量大小为 10：

```text
Batch 1 start = 0
Batch 2 start = 10
Batch 3 start = 20
```

这里的 `start` 是：

```text
在 missing_texts 中的起始位置
```

不是原始 Chunk 索引。

Pipeline 再通过 `missing_indexes` 映射回原位置。

---

# 十二、发送给百炼的请求内容

项目使用百炼的 OpenAI 兼容接口：

```text
https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings
```

请求主体类似：

```json
{
  "model": "text-embedding-v4",
  "input": [
    "第一条 embedding_text",
    "第二条 embedding_text"
  ],
  "dimensions": 1024,
  "encoding_format": "float"
}
```

主要字段：

- `model`：Embedding 模型；
- `input`：一批文本；
- `dimensions`：输出维度；
- `encoding_format="float"`：返回浮点向量。

项目没有发送 DashScope 原生接口的：

```text
text_type
```

因为当前使用的是 OpenAI 兼容接口，架构决策明确不向该接口发送无效参数，见 [architecture-decisions.md](D:/Java-learning/DevContext/docs/architecture-decisions.md:21)。

---

# 十三、三种传输模式

客户端支持：

```text
openai
auto
curl
```

配置来自：

```text
EMBEDDING_TRANSPORT
```

默认是：

```text
curl
```

配置见 [config.py](D:/Java-learning/DevContext/src/devcontext/config.py:17) 和 [.env.example](D:/Java-learning/DevContext/.env.example:10)。

---

## 1. `openai` 模式

只使用 OpenAI Python SDK：

```python
response = self.client.embeddings.create(
    model=self.model,
    input=list(batch),
    dimensions=self.dimensions,
    encoding_format="float",
)
```

SDK 客户端配置：

```text
timeout = 30 秒
max_retries = 3
```

适用于 Python/OpenSSL 网络连接正常的环境。

如果发生 `APIConnectionError`：

```text
直接失败
不会回退到 curl
```

---

## 2. `auto` 模式

先尝试 OpenAI SDK：

```text
OpenAI SDK
    │
    ├─ 成功：继续使用 SDK
    │
    └─ APIConnectionError
           ↓
       切换到 curl
```

一旦连接错误触发回退：

```python
self._curl_fallback = True
```

后续批次会继续使用 curl，不会每个批次都重新尝试 SDK。

需要注意：

> `auto` 只对 `APIConnectionError` 自动切换 curl，不是所有 API 错误都会切换。

例如：

- API Key 错误；
- 请求参数错误；
- 服务返回某些 4xx；
- SDK 抛出非连接类型异常；

通常会直接失败或被包装为批次错误，而不是切换 curl。

---

## 3. `curl` 模式

从第一个批次开始就使用系统 curl。

当前 Windows 环境默认使用这个模式，因为验证中发现：

```text
Python/OpenSSL 请求百炼可能出现 TLS 提前关闭
系统 curl 可以正常连接
```

这样避免每次 ingestion 都先等待一次已知可能失败的 SDK 请求。

---

# 十四、curl 模式详细过程

实现见 [client.py](D:/Java-learning/DevContext/src/devcontext/embedding/client.py:105)。

## 1. 查找 curl

依次查找：

```text
curl.exe
curl
```

找不到就失败：

```text
Embedding connection failed and the curl fallback is unavailable
```

## 2. API Key 不进入命令行

curl 配置通过标准输入传入：

```text
url = "..."
request = "POST"
header = "Authorization: Bearer ..."
header = "Content-Type: application/json"
```

命令行参数里只有：

```text
curl --config - --data-binary @临时文件 ...
```

因此 API Key：

- 不出现在命令行参数；
- 不出现在进程列表；
- 不写入请求正文临时文件；
- 不写入缓存；
- 不写日志。

API Key 来自 `DASHSCOPE_API_KEY`，由 `SecretStr` 保存，项目的 `.env.example` 有意不提供该值。

## 3. 请求正文会写入临时文件

请求正文包含：

```text
模型
维度
实际 embedding_text
```

它被写入一个临时 JSON 文件：

```python
NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    suffix=".json",
    delete=False,
)
```

然后 curl 使用：

```text
--data-binary @request.json
```

请求结束后在 `finally` 中删除。

这意味着：

- API Key 不落盘；
- 但 Chunk 文本会短暂写入系统临时目录；
- 普通异常和超时都会进入 `finally` 删除；
- 如果系统断电或进程被强制终止，理论上可能留下临时请求文件。

另外，Embedding 本身意味着代码和文档正文会发送给外部百炼服务，这是该架构本身的安全边界。

## 4. curl 响应状态

命令附加：

```text
--write-out "\n__HTTP_STATUS__:%{http_code}"
```

所以 stdout 末尾会包含：

```text
__HTTP_STATUS__:200
```

代码把：

```text
响应 JSON
HTTP 状态码
```

分离出来处理。

---

# 十五、重试逻辑

## 1. curl 最多尝试四次

```python
for attempt in range(4):
```

可重试情况：

```text
curl 进程返回码非 0
HTTP 429
HTTP >= 500
响应正文包含 "already running"
```

退避等待是：

```text
第 1 次失败后：1 秒
第 2 次失败后：2 秒
第 3 次失败后：4 秒
第 4 次失败：不再等待，最终失败
```

也就是指数退避：

```python
time.sleep(2**attempt)
```

## 2. 超时重试

单次 curl 超时：

```text
30 秒
```

超时同样最多尝试 4 次。

全部超时后：

```text
Bailian curl request timed out after 4 attempts
```

## 3. `already running` 的批次拆分

如果一个 curl 批次最终失败，错误信息包含：

```text
already running
```

且批次至少有两条文本，客户端会把批次拆成两半：

```text
原批次 10 条
   ↓
前 5 条 + 后 5 条
```

分别重新请求。

这是针对当前服务端特定并发/任务状态异常的额外降级。

它只做一层对半拆分，不是无限递归拆到单条。

## 4. 不重试的错误

普通 HTTP 4xx，除了 429，一般不会重试，例如：

```text
400 参数错误
401 API Key 错误
403 无权限
```

这类错误继续请求通常没有意义。

---

# 十六、API 返回顺序如何保证

API 返回的 `data` 通常带：

```json
{
  "index": 0,
  "embedding": [...]
}
```

响应中的数组顺序理论上可能不等于输入顺序。

因此 SDK 和 curl 两条路径都会按 `index` 排序：

```python
ordered = sorted(
    response.data,
    key=lambda item: item.index,
)
```

或者：

```python
data = sorted(
    payload.get("data", []),
    key=lambda item: item["index"],
)
```

排序后才提取向量：

```python
[item.embedding for item in ordered]
```

这样可以保证：

```text
输入文本 0 → 返回向量 0
输入文本 1 → 返回向量 1
```

否则向量一旦错配到错误 Chunk，系统不会明显报错，但向量检索会被静默污染。

---

# 十七、返回向量怎样校验

每个批次返回后调用：

```python
_validate(batch_vectors, expected=len(batch))
```

见 [client.py](D:/Java-learning/DevContext/src/devcontext/embedding/client.py:94)。

进行两项检查。

## 1. 数量必须一致

输入 10 条文本，就必须返回 10 个向量：

```text
输入数 = 返回向量数
```

否则抛出：

```text
Embedding API returned N vectors for M inputs
```

## 2. 每个向量维度必须正确

每个向量长度必须是：

```text
1024
```

否则：

```text
Expected 1024-dimensional embeddings, got [...]
```

为什么要尽早检查：

- 防止 API 参数失效；
- 防止服务端返回默认维度；
- 防止错误响应被当成正常向量；
- PostgreSQL 列是 `VECTOR(1024)`；
- 不同维度的向量不能比较。

当前校验只验证数量和长度，没有逐个验证：

- 元素一定是浮点数；
- 元素没有 `NaN`；
- 元素没有正负无穷。

正常 API 响应不会出现这些问题，但这是当前校验边界。

---

# 十八、为什么每个成功批次立即写缓存

批次校验成功后：

```python
vectors.extend(batch_vectors)

if on_batch is not None:
    on_batch(start, batch_vectors)
```

Pipeline 的回调会同时做两件事：

```python
vectors[chunk_index] = vector
cache.append(fingerprint, vector)
```

这意味着缓存不是等 2901 个 Chunk 全部完成后一次性写，而是：

```text
Batch 1 成功 → 立即缓存
Batch 2 成功 → 立即缓存
Batch 3 失败 → 停止
```

虽然这次 ingestion 没有完成，但 Batch 1 和 Batch 2 已经保存在缓存。

下次重试：

```text
Batch 1/2 对应的文本命中缓存
只请求剩余文本
```

这对大量 Chunk 的外部 API 调用非常重要，可以避免网络在最后阶段失败后从头计费。

---

# 十九、缓存怎样追加写入

写缓存：

```python
def append(self, content_hash, vector):
    key = self.key(content_hash)
    self.values[key] = vector
    with self.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(...))
        handle.write("\n")
```

见 [cache.py](D:/Java-learning/DevContext/src/devcontext/embedding/cache.py:30)。

它同时更新：

```text
内存字典
磁盘 JSONL
```

## 为什么使用追加模式

优点：

- 每批完成即可持久化；
- 不用每次重写数十 MB 文件；
- 进程中途失败时已完成批次仍保留；
- 单行 JSON 损坏时可以跳过，不影响其他行。

## 同一个 Key 重复写入

追加缓存不会删除旧行。

如果同一个 Key 写入多次：

```text
旧 key → vector A
同 key → vector B
```

文件中两行都存在。

下次加载时按顺序读取：

```python
self.values[item["key"]] = vector
```

后面的值覆盖前面的值，所以内存里保留最后一条。

## 当前实际缓存状态

当前缓存文件大约：

```text
67,357,202 字节，约 67 MB
```

其中：

```text
有效 JSONL 行：3089
唯一 Key：      3011
当前 Chunk：    2901
```

这说明：

- 缓存里有约 78 行重复 Key；
- 还有一些历史版本或当前已不存在 Chunk 的向量；
- 缓存不会随着全量重建自动删除旧项；
- 缓存会持续增长。

缓存路径已在 [.gitignore](D:/Java-learning/DevContext/.gitignore:8) 中排除，不会提交到 Git。

---

# 二十、缓存当前没有做什么

当前缓存是一个非常简单的本地 JSONL 缓存，没有：

- 自动压缩；
- 删除过期 Key；
- 最大容量限制；
- LRU；
- 文件锁；
- 多进程并发写保护；
- 缓存版本；
- base URL 隔离；
- Provider 隔离；
- 向量数值完整性校验；
- 原子整体替换。

它适合：

```text
单机
单进程
少量开发数据
全量重建
```

不适合多个 ingestion 进程同时写同一个缓存文件。

如果两次 ingestion 并发运行，多个进程可能交叉追加，存在写入损坏风险。

---

# 二十一、同一文本能否复用一个向量

理论上可以。

假设两个不同 Chunk：

```text
Chunk A：file_path = A.md
Chunk B：file_path = B.md
```

但它们的：

```text
embedding_text
```

完全相同。

那么它们的 Fingerprint 和缓存 Key 相同，可以共享同一个向量。

这是合理的，因为文件路径没有进入 Embedding；相同语义文本本来就应该有相同向量。

不过当前 Pipeline 有一个细节：

- 如果运行开始前缓存已经有这个 Key，两个 Chunk 都会命中并复用。
- 如果运行开始时缓存为空，两个相同文本同时出现在 `missing_texts` 中，Pipeline 没有先对缺失文本去重，仍可能把相同文本重复发送给 API。

也就是说：

```text
跨运行复用：支持
同一首次运行内的缺失文本去重：没有实现
```

---

# 二十二、所有向量完成后怎样检查

API 调用结束后：

```python
completed_vectors = [
    vector
    for vector in vectors
    if vector is not None
]

if len(completed_vectors) != len(chunks):
    raise RuntimeError(
        "Embedding cache did not produce one vector per chunk"
    )
```

见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:66)。

这项检查确保：

```text
Chunk 数量 == 向量数量
```

为什么重要：

```text
chunks[0] 必须对应 vectors[0]
chunks[1] 必须对应 vectors[1]
...
```

如果有任何 `None`，过滤后的向量数量会减少，Pipeline 会立即失败，不会把位置错乱的向量写进数据库。

---

# 二十三、网络失败时数据库会怎样

这是当前实现比较稳妥的部分。

数据库操作发生在所有向量生成完毕之后：

```python
if len(completed_vectors) != len(chunks):
    raise ...

store = ChunkStore(...)
store.initialize()
store.replace_repository(...)
```

所以流程是：

```text
解析
→ 缓存查询
→ API 请求
→ 所有向量完成
→ 才触碰数据库数据
```

如果在 Embedding 阶段失败：

```text
旧数据库索引仍然存在
已经成功生成的批次保留在缓存
下一次可以继续复用
```

不会先删除旧索引，再发现向量只生成了一半。

---

# 二十四、数据库替换也是事务化的

入库使用：

```python
with connection.transaction():
    DELETE FROM knowledge_chunk WHERE repository = ...
    executemany(INSERT ...)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:86)。

事务效果：

```text
删除旧数据 + 插入全部新数据
```

要么全部成功，要么全部回滚。

如果插入第 2000 条时失败：

```text
DELETE 也会回滚
旧索引仍然存在
```

所以整体安全链路是：

```text
Embedding 阶段失败
→ 不进入数据库替换

数据库插入阶段失败
→ 事务回滚

只有所有步骤成功
→ 新索引替换旧索引
```

缓存不参与数据库事务，但这不是问题：缓存只是一种可复用的计算结果，不是当前线上索引本身。

---

# 二十五、API Key 是怎样管理的

配置：

```python
dashscope_api_key: SecretStr | None
```

环境变量名：

```text
DASHSCOPE_API_KEY
```

调用时：

```python
settings.api_key()
```

如果没有配置，立即失败：

```text
DASHSCOPE_API_KEY is not configured
```

项目约定：

- Key 使用系统环境变量；
- `.env.example` 不填写 Key；
- `.env` 被 Git 忽略；
- curl 模式不把 Key 放入命令行；
- 缓存只保存向量，不保存 Key；
- JSON 请求正文不保存 Key。

`SecretStr` 的主要作用是避免配置对象在日志或 `repr()` 中直接显示完整密钥，但 `api_key()` 最终仍需把明文传给客户端。

---

# 二十六、一次完整实例

假设本次有 6 个 Chunk：

```text
A、B、C、D、E、F
```

## 第一步：生成 Embedding 文本

```text
A → text_A
B → text_B
C → text_C
D → text_D
E → text_E
F → text_F
```

## 第二步：生成 Fingerprint

```text
A → hash_A
B → hash_B
C → hash_C
D → hash_D
E → hash_E
F → hash_F
```

## 第三步：查询缓存

假设 A、C、F 命中：

```text
vectors = [
    vector_A,
    None,
    vector_C,
    None,
    None,
    vector_F,
]
```

得到：

```text
missing_indexes = [1, 3, 4]
missing_texts   = [text_B, text_D, text_E]
```

## 第四步：调用 API

一个批次发送：

```json
{
  "model": "text-embedding-v4",
  "input": [
    "text_B",
    "text_D",
    "text_E"
  ],
  "dimensions": 1024
}
```

## 第五步：排序并校验返回

```text
返回 3 个向量
每个长度 1024
按 response.index 排序
```

## 第六步：回填原位置并缓存

```text
vector_B → vectors[1]
vector_D → vectors[3]
vector_E → vectors[4]
```

最后：

```text
vectors = [
    vector_A,
    vector_B,
    vector_C,
    vector_D,
    vector_E,
    vector_F,
]
```

## 第七步：事务化入库

```text
Chunk A + vector_A
Chunk B + vector_B
Chunk C + vector_C
Chunk D + vector_D
Chunk E + vector_E
Chunk F + vector_F
```

全部一起替换数据库中该仓库的旧索引。

---

# 二十七、当前测试覆盖与边界

目前自动化测试明确覆盖：

- Embedding 批量大小不能超过 10；
- 缓存加载时忽略错误维度向量；
- 实际百炼烟雾测试返回 1024 维；
- 数据库集成链路正常。

但单元测试还没有充分覆盖：

- SDK 返回数据乱序；
- `auto` 模式的 SDK → curl 切换；
- curl 429/5xx 重试；
- curl 超时重试；
- `already running` 批次拆分；
- API 少返回一个向量；
- API 返回 `NaN`；
- 缓存并发写；
- ingestion 中途失败后的恢复；
- 同一运行内重复文本去重。

因此当前实现已经通过真实环境验证，但重试和异常分支的自动化测试仍然偏少。

---

# 二十八、当前链路最值得注意的限制

## 1. 查询向量不缓存

重复查询相同问题，会重复调用 API。

## 2. 同一轮缺失文本不去重

两个 Chunk 的 `embedding_text` 相同时，如果缓存开始时没有该 Key，可能重复请求。

## 3. 缓存永不清理

文件会随着：

- Chunk 历史版本；
- 文档删除；
- 模型试验；
- 重复写入；

不断增长。

## 4. 缓存没有并发保护

不能安全支持多个 ingestion 进程同时写同一文件。

## 5. 缓存失效没有 Provider/Base URL 维度

同一个模型名切换到另一个兼容服务时，可能错误复用旧向量。

## 6. 服务端同名模型升级无法自动感知

只要模型字符串仍是：

```text
text-embedding-v4
```

缓存就认为向量空间没变。

## 7. 原始 Chunk 文本会发送到外部服务

对于私有代码库，这是一个需要明确接受的数据安全边界。

## 8. 临时请求文件短暂保存明文正文

正常结束会删除，但强制终止时可能残留。

## 9. 缓存文件中只有向量，没有原始文本

这有利于减少明文泄漏，但向量本身仍然属于项目衍生数据，所以也已被 Git 忽略。

---

# 二十九、如何准确理解这一层

这条链路可以分成四个职责：

```text
models.py
负责决定“什么文本表达这个 Chunk 的语义”

pipeline.py
负责缓存命中、缺失定位、顺序回填和整体编排

client.py
负责批量请求、传输降级、重试、排序和维度校验

cache.py
负责按模型 + 维度 + 文本指纹复用已有向量
```

最重要的五个结论是：

1. Embedding 的输入是 `embedding_text()`，不是整个 Chunk，也不是 `keyword_text()`。

2. 缓存根据 `SHA-256(embedding_text)` 查找，不是根据 `content_hash`。

3. 缓存 Key 还包含模型和维度，避免跨向量空间误复用。

4. 只有缓存缺失的文本才会批量请求 API，返回向量会映射回原始 Chunk 顺序。

5. 所有 Chunk 都拿到向量后才事务化替换数据库；中途失败不会破坏上一版索引。

整个过程压缩成一行就是：

```text
Chunk
→ embedding_text
→ SHA-256 指纹
→ 缓存命中或百炼 API
→ 1024 维向量
→ PostgreSQL VECTOR(1024)
```

下一步最自然的是学习“PostgreSQL / pgvector 存储与向量检索”，即这些 1024 维向量具体怎样写入数据库、`<=>` 怎样计算 cosine distance，以及为什么当前采用精确向量搜索而不是 HNSW。