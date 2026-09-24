# DevContext-Java 开发里程碑

> 文档编号：`03-development-milestones`
> 阶段：M0 Project Freeze
> 本文把 `初步开发计划.md` 的阶段划分，转换成**适合 Agent 执行**的里程碑。
> 每个里程碑统一使用：`Goal / Input / Tasks / Output / Acceptance Criteria / Dependency / Forbidden`。

---

## 0. 使用约定

### 0.1 里程碑总览

| 里程碑 | 名称 | 一句话目标 | 依赖 |
|---|---|---|---|
| M0 | Project Freeze | 冻结 Scope / 架构 / 技术栈 / 路线图 | — |
| M1 | JavaParser Minimal Prototype | 一个真实 Java 文件 → CodeChunk JSON | M0 |
| M2 | Repository Parsing | 整个仓库 → CODE / DOC Chunks | M1 |
| M3 | Storage | Chunks → PostgreSQL | M2 |
| M4 | Vector Baseline | Vector Only 检索可运行 | M3 |
| M5 | Keyword Baseline | Keyword Only 检索可运行 | M3 |
| M6 | Evaluation V0 | ≥30 条 Query + Recall@K / MRR | M4, M5 |
| M7 | Hybrid RRF | Keyword + Vector → RRF | M6 |
| M8 | Context Builder | Top-K → LLM Context + Citation | M7 |
| M9 | Generation | Question → Context → Answer + Citation | M8 |
| M10 | Query Router | DOC / CODE / MIXED 路由 | M9 |
| M11 | LangGraph | Router → Retrieve → Context → Generate | M10 |
| M12 | Agentic Retry | Context Evaluation + Rewrite + Retry ≤ 2 | M11 |

### 0.2 执行顺序原则

`初步开发计划.md` §4.1 规定的顺序是硬约束：

```text
Parsing → Chunking → Storage → Retrieval → Evaluation → Generation → Agentic Routing
```

**不得**反向执行（`LangGraph → Agent → 再回来补 Retriever`），否则后期出问题时无法判断是 Parser / Chunk / Retriever / Router / Prompt / LLM 哪一层出错。

### 0.3 两条贯穿所有里程碑的硬约束

**约束 A：每一层都必须能独立运行。**

```text
java-parser --file <某个 .java>        → 直接输出 CodeChunk JSON
retriever.retrieve(query)             → 直接输出 Top-K Chunks（不经过 LLM）
markdown_chunker --file <某个 .md>    → 直接输出 Section Chunk JSON
```

不允许出现"必须启动整个 DevContext 才能验证 Parser 是否正常"的情况。

**约束 B：每个里程碑结束都必须写测试并记录发现的问题。**

对应 `初步开发计划.md` §82 的每日固定流程：

```text
① 确定一个核心目标 → ② 最小实现 → ③ 在真实 my12306 数据上运行
→ ④ 保存输出结果 → ⑤ 写测试 → ⑥ 记录问题 → ⑦ Commit → ⑧ 进入下一阶段
```

### 0.4 环境前提（重要）

**本轮环境实测结果**（详见 `00-p0-scope.md` §6.4）：

```text
本轮工作沙箱：JDK 11 / 无 Maven / 无 Docker / 出网仅允许 api.deepseek.com
```

**结论：从 M1 开始，所有验证必须在开发者本机（Windows 11）执行，不能在沙箱内完成。**

M1 的第一个 Task 就是确认本机环境（见 M1 的 Tasks 第 1 项）。

---

# M0 Project Freeze

## Goal

冻结 P0 的范围、架构、技术栈与路线图，使项目具备"可以直接进入开发"的完整约束。

## Input

```text
项目规划文档/devContex-项目背景和约束.md
项目规划文档/调研需求.md
项目规划文档/初步开发计划.md
项目规划文档/开发前任务冻结.md
调研文档/Continue调研.md
调研文档/JavaParser调研.md
调研文档/RAGFlow调研.md
（以及对应的"清晰解读"版本）
```

## Tasks

- 阅读全部项目资料与三份调研
- 冻结 P0 Scope 与 Non-goals
- 整理 Architecture Decision（ADR）
- 整理未冻结技术项与版本核验清单
- 把开发计划转换为可执行的里程碑
- 执行设计冲突检查
- 核实 my12306 仓库与本机环境的实际状态

## Output

```text
docs/development/00-p0-scope.md
docs/development/01-architecture-decisions.md
docs/development/02-tech-stack-todo.md
docs/development/03-development-milestones.md
```

## Acceptance Criteria

- [ ] P0 Scope 明确，Non-goals 完整
- [ ] 11 条已冻结决策各有 ADR 记录（Decision / Context / Reason / Alternative / Why Not / Status）
- [ ] 每个未冻结技术项有 Options / Comparison / Recommendation / Status
- [ ] 所有未核验的版本标为 `TO-VERIFY`，未伪装成 `FINAL`
- [ ] 冲突检查完成，每条冲突有 Source A / Source B / Suggested Resolution / Severity
- [ ] M1～M12 里程碑可执行，每个都有验收标准
- [ ] **本轮未写任何实现代码**
- [ ] **本轮未修改 my12306**

## Dependency

无

## Forbidden

本阶段不得：

* 开始业务编码（JavaParser / Retriever / Database / Embedding / LangGraph 均不实现）
* 扩大技术调研范围（不引入 LlamaIndex / Haystack / GraphRAG / Qdrant / Milvus / Elasticsearch）
* 技术堆叠（Redis / Kafka / Celery / Neo4j / Kubernetes / 复杂 DDD）
* 推翻 Frozen Decision
* 移动或重命名现有文件、改 package、初始化完整应用、创建大量空模块
* 修改 my12306

---

# M1 JavaParser Minimal Prototype

## Goal

验证 JavaParser 可以把**一个真实的 my12306 Java 文件**转换为结构正确、源码位置准确的 DevContext CodeChunk JSON。

这是调研假设第一次经过**真实工程代码**验证。

## Input

一个真实的 Java 文件（已实测存在）：

```text
D:\Java-learning\12306Project\12306\my12306\services\ticket-services\
  src\main\java\edu\swu\fcj\my12306\biz\ticketservice\service\impl\TicketServiceImpl.java
```

（278 行；`package edu.swu.fcj.my12306.biz.ticketservice.service.impl`）

**为什么选它**：`TicketServiceImpl` 在项目定义与 JavaParser 调研中被反复用作示例（DEF §10、R-JAVA §32），且它包含 PROJECT 关心的全部节点类型——类级注解、方法注解、方法、构造器、Javadoc。

## Tasks

1. **确认本机环境**（沙箱不具备，见 §0.4）
   - `java -version` → 必须是 **21**
   - `mvn -version`
   - 确认可访问 Maven 仓库以下载依赖
2. 建立 `java-parser` 模块（Maven 项目，独立于 my12306 的父 POM）
3. 添加 `javaparser-core` 依赖（**稳定版，非 SNAPSHOT**；具体版本见 `02-tech-stack-todo.md` §2）
4. 配置 Java 21 编译目标
5. 建立 `CodeChunk` DTO
6. 配置 `ParserConfiguration`：
   - `LanguageLevel.JAVA_21`
   - `storeTokens = true`（**绝不能关**，否则 Range 丢失）
   - `attributeComments = true`
7. 复用单个 `JavaParser` 实例（不用 `StaticJavaParser`），调用 `parse()` 得到 `ParseResult<CompilationUnit>`
8. 提取 `package_name`（`getPackageDeclaration()`，注意 `Optional`）
9. `findAll(MethodDeclaration.class)` 提取 Method
10. `findAncestor(TypeDeclaration.class)` 提取所属类
11. 提取方法名（`getNameAsString()`）
12. 提取注解（`getAnnotations()` + `getNameAsString()`）
13. 提取签名（**`getDeclarationAsString()`**，不用 `getSignature()`）
14. 提取 Range（`getRange()`，做 `Optional` 守卫）
15. **按 Range 从原始文件切片**得到 `content`（不用 `toString()`）
16. 提取 Javadoc（`getJavadocComment()`，**独立字段**）
17. 提取 Constructor（`ConstructorDeclaration`，注意没有 `getType()`）
18. 生成 CLASS Summary Chunk（类注解 + 类声明 + extends/implements + 成员方法签名列表）
19. 用 Jackson 序列化为 JSON（UTF-8）
20. 编写测试
21. 与源码**逐字段人工核验**

## Output

```json
[
  {
    "sourceType": "CODE",
    "chunkType": "METHOD",
    "repository": "my12306",
    "module": "ticket-services",
    "filePath": "services/ticket-services/src/main/java/.../TicketServiceImpl.java",
    "packageName": "edu.swu.fcj.my12306.biz.ticketservice.service.impl",
    "className": "TicketServiceImpl",
    "methodName": "...",
    "signature": "...",
    "annotations": ["..."],
    "javadoc": "...",
    "startLine": 0,
    "endLine": 0,
    "content": "..."
  }
]
```

## Acceptance Criteria

- [ ] `package_name` 正确（实测应为 `edu.swu.fcj.my12306.biz.ticketservice.service.impl`）
- [ ] `class_name` 正确（`TicketServiceImpl`）
- [ ] `method_name` 正确
- [ ] `annotations` 正确（含类级与方法级；**用后缀匹配**，防全限定名写法）
- [ ] `signature` 正确且为人类可读形式（`getDeclarationAsString()`）
- [ ] `start_line` 正确（**1-based**）
- [ ] `end_line` 正确（**1-based**，闭区间，**不做 ±1 转换**）
- [ ] `content` 与原始 Source 的对应行**逐字符一致**
- [ ] `@Transactional` 等注解行**被包含在** `content` 与行号范围内
- [ ] Interface 方法 / 抽象方法（**无方法体**）**不被遗漏**
- [ ] Constructor 被识别为 `CONSTRUCTOR` 而非 `METHOD`
- [ ] CLASS Chunk 是**摘要形态**，不是整类源码
- [ ] Parse Failure **不导致程序整体崩溃**
- [ ] 输出 JSON 可以被正常反序列化
- [ ] 输出为 **UTF-8**，中文 Javadoc 不乱码
- [ ] Tests Pass

## Dependency

M0

## Forbidden

本阶段不得：

* 接 PostgreSQL
* 接 Embedding
* 接 LangChain / LangGraph
* 实现 SymbolSolver
* 遍历整个 Repository（这是 M2 的事）
* 处理 Field / Lambda / Enum / Record / Anonymous Class / Local Class
* 实现超长方法的二次切分（P1）
* 修改 my12306

---

# M2 Repository Parsing

## Goal

把 M1 的单文件能力扩展到**整个仓库**，产出完整的 CODE Chunks 与 DOC Chunks，并输出可复核的统计报告。

## Input

**两个根目录**（实测确认代码与文档不在同一根下，见 `01-architecture-decisions.md` ADR-012 / Conflict 06）：

```text
code_root = D:\Java-learning\12306Project\12306\my12306\
doc_root  = D:\Java-learning\12306Project\docs\
```

实测规模：

```text
code_root: 236 个 .java（排除 target/），5 个 Maven 模块
doc_root : 84 个 .md（其中约 30 个为第三方前端库 README，必须排除）
```

## Tasks

### 2.1 Repository Scanner

1. 实现文件发现：分别扫描 `code_root` 与 `doc_root`
2. 实现忽略规则：
   - 代码根：`target/`、`.git/`、`.idea/`、`node_modules/`、`generated/`
   - 文档根：`docs/5-后续开发规划/baseline/results/**`（第三方前端库，见 `01-architecture-decisions.md` Risk 03）
3. 记录 `source_root` 标记（区分来自代码根还是文档根）
4. `file_path` 统一为相对各自根的路径
5. 输出被排除的文件清单（便于复核）

### 2.2 Java 解析批量执行

6. 遍历全部 `.java`，复用同一 `JavaParser` 实例
7. 计算文件级 SHA-256 作为 `content_hash`
8. 对每个文件生成 METHOD / CLASS / CONSTRUCTOR Chunk
9. **解析失败的文件**：记录 `parse_problems` 并继续，不中断
10. 统计 `is_oversized` Chunk 数量（见 `01-architecture-decisions.md` Risk 02）

### 2.3 Markdown Chunker

11. 实现 Heading-aware 切分（`#` / `##` / `###`）
12. 生成 `heading_path`（形如 `购票系统 / 事务设计 / 为什么缩小事务边界`）
13. 生成 `title`、`start_line`、`end_line`、`content`
14. **超长 Section 先标记 `oversized`**，否按 P1 处理

### 2.4 报告

15. 产出 `ingestion_report.json`

## Output

```text
CodeChunk JSON（CODE）
DocChunk JSON（DOC）

ingestion_report.json:
{
  "code_root": "...",
  "doc_root": "...",
  "java_files": 236,
  "markdown_files_total": 84,
  "markdown_files_excluded": 30,
  "markdown_files_indexed": 54,
  "method_chunks": 0,
  "class_chunks": 0,
  "constructor_chunks": 0,
  "doc_section_chunks": 0,
  "oversized_chunks": 0,
  "parse_failures": 0,
  "excluded_paths": []
}
```

## Acceptance Criteria

- [ ] 能扫描真实 code_root 并识别全部 `.java`
- [ ] 能扫描 doc_root，且**第三方前端库 README 被正确排除**
- [ ] 被排除的文件清单可见
- [ ] `module` 字段正确（gateway / ticket / user / order / pay services）
- [ ] 解析失败的文件被记录，**且不影响其余文件**
- [ ] 统计数字可人工复核（抽查若干文件，数量对得上）
- [ ] `ingestion_report.json` 中 `method_chunks` / `class_chunks` / `doc_section_chunks` 均为非零
- [ ] 全部 Chunk 的 `content_hash` 非空
- [ ] Tests Pass

## Dependency

M1

## Forbidden

本阶段不得：

* 接 PostgreSQL（数据库在 M3）
* 接 Embedding
* 实现增量索引（P0 允许全量重建，`content_hash` 只保存备用）
* 修改 my12306
* 把 `docs/5-后续开发规划/baseline/results/` 下的第三方 README 纳入索引

---

# M3 Storage

## Goal

把 M2 产出的 Chunks 完整写入 PostgreSQL + pgvector，并验证数据可被正确查回。

## Input

```text
M2 产出的 CodeChunk / DocChunk JSON
M0 冻结的 Schema 定义（见 02-tech-stack-todo.md §8）
```

## Tasks

1. 编写 `docker-compose.yml`，用 `pgvector/pgvector` 镜像（**固定 tag，不用 `latest`**）
2. 启动并验证扩展可加载：`CREATE EXTENSION vector;`
3. 编写 `sql/schema.sql`
4. 编写 `sql/indexes.sql`（FTS 的 GIN 索引 + pgvector 的 HNSW 索引）
5. 实现 Python 侧数据库访问层（`psycopg` 3，见 `02-tech-stack-todo.md` §3）
6. 实现 Chunk 批量写入
7. 实现 `content_hash` 保存
8. **验证全量重建可重复**：整体删除后重跑，结果一致
9. 用 `.env` 管理连接配置（`.env.example` 提交，`.env` 进 `.gitignore`）

## Output

```text
docker-compose.yml
sql/schema.sql
sql/indexes.sql
app/db/（连接与查询封装）
.env.example
```

建表核心结构（字段集见 `02-tech-stack-todo.md` §8）：

```sql
CREATE TABLE knowledge_chunk (
    id            BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL,
    source_type   TEXT   NOT NULL,   -- DOCUMENT / CODE
    chunk_type    TEXT   NOT NULL,   -- DOCUMENT_SECTION / CLASS / METHOD / CONSTRUCTOR
    source_root   TEXT,              -- 见 ADR-012
    file_path     TEXT   NOT NULL,
    module        TEXT,
    package_name  TEXT,
    class_name    TEXT,
    method_name   TEXT,
    title         TEXT,
    heading_path  TEXT,
    signature     TEXT,
    annotations   TEXT[],
    javadoc       TEXT,
    content       TEXT   NOT NULL,
    content_hash  TEXT   NOT NULL,
    start_line    INT,
    end_line      INT,
    is_oversized  BOOLEAN DEFAULT FALSE,
    embedding     vector(N),          -- N 在 M3 前确定，见 02-tech-stack-todo.md §5
    created_at    TIMESTAMP DEFAULT now(),
    updated_at    TIMESTAMP DEFAULT now()
);
```

## Acceptance Criteria

- [ ] `docker compose up` 可启动数据库，扩展自动可用
- [ ] `knowledge_chunk` 表建立，DOC / CODE 共用
- [ ] FTS 的 `tsvector` 列与 GIN 索引建立，**字段加权生效**（A: method_name/class_name；B: signature/annotations；C: content）
- [ ] pgvector 的 HNSW 索引建立
- [ ] 全部 M2 Chunks 入库，行数与 `ingestion_report.json` 一致
- [ ] `content_hash` 全部非空
- [ ] 能执行并看到正确结果：

```sql
SELECT chunk_type, class_name, method_name, file_path, start_line, end_line
FROM knowledge_chunk
WHERE class_name = 'TicketServiceImpl';
```

- [ ] 删除全部 Chunk 后重跑 ingestion，结果与首次一致
- [ ] `.env` 未被提交，`.env.example` 已提交
- [ ] Tests Pass

## Dependency

M2

## Forbidden

本阶段不得：

* 接 Embedding（向量列此时允许为空）
* 接 LLM
* 引入 ORM / Alembic（见 `02-tech-stack-todo.md` §3）
* 实现增量索引
* 在 schema 中为"多 Embedding 模型 / 多维度"预留结构（FREEZE §5.9 禁止动态多模型 Schema）

---

# M4 Vector Baseline

## Goal

建立第一个可运行的检索 Baseline：Vector Only。此阶段**不判断效果好坏**，只验证链路完整可运行。

## Input

```text
M3 入库的 knowledge_chunk（含 content 与 metadata）
Embedding 模型（在 M3 前已确定，见 02-tech-stack-todo.md §5）
```

## Tasks

1. 实现 `EmbeddingInputBuilder`
2. 实现 Code Embedding Input 组装
3. 实现 Doc Embedding Input 组装
4. 实现批量 Embedding 调用
5. 把向量写回 `knowledge_chunk.embedding`
6. 实现 `vectorRetriever(query, topK)`
7. 手工执行三条测试查询（见下）
8. 保存检索结果用于观察

## Output

```text
app/embedding/（EmbeddingInputBuilder + 模型调用）
app/retrieval/vector_retriever.py
```

Embedding Input 格式（**注意哪些字段不得进入**）：

```text
[Code]
Type: METHOD
Class: TicketServiceImpl
Method: purchaseTicket
Signature: public ... purchaseTicket(...)
Annotations: Transactional
Javadoc: ...
Code:
...
```

```text
[Doc]
Document: D4-设计分析-Feign移出事务与支付通知异步化.md
Section: 事务设计 / 为什么调整事务边界
Content:
...
```

**绝对不得进入 Embedding 的字段**（R-RAG §2.6 / PLAN §9.3）：

```text
start_line / end_line / content_hash / repository_id / created_at / updated_at
```

## Acceptance Criteria

- [ ] 全部 Chunk 都有非空 `embedding`，维度等于表定义中的 `N`
- [ ] Embedding 输入中**不含** metadata-only 字段
- [ ] `vectorRetriever` 可**脱离 LLM** 独立运行
- [ ] 返回结果包含 `chunk_id` / `source_type` / `chunk_type` / `file_path` / `class_name` / `method_name` / `content` / `similarity`
- [ ] 三条手工查询可执行并返回结果：

```text
purchaseTicket 方法在哪里？
为什么使用 Token Bucket？
订单是怎样自动关闭的？
```

- [ ] **本阶段不评价效果优劣**，只确认 Pipeline 可运行
- [ ] Tests Pass

## Dependency

M3

## Forbidden

本阶段不得：

* 判断"效果好不好"（`初步开发计划.md` §36 明确要求）
* 引入多 Embedding 模型或动态维度
* 接 Keyword Retrieval（M5）
* 接 LLM
* 在检索前按相似度阈值过滤（阈值只影响展示，不应影响 Baseline 观察）

---

# M5 Keyword Baseline

## Goal

建立第二个检索 Baseline：Keyword Only，目标是让**精确标识符查询**能稳定命中。

## Input

```text
M3 入库的 knowledge_chunk（含 tsvector 列）
```

## Tasks

1. 实现 `keywordRetriever(query, topK)`
2. 确认 `tsvector` 的字段加权生效（`setweight` A/B/C）
3. 确认使用 **`simple` 字典**（不做词干化，避免破坏标识符）
4. 用 `ts_rank` 排序
5. 手工执行符号类查询（见下）
6. **记录失败案例**，不立即优化

## Output

```text
app/retrieval/keyword_retriever.py
```

建议的 `tsvector` 加权定义：

```sql
setweight(to_tsvector('simple', coalesce(method_name,'')), 'A') ||
setweight(to_tsvector('simple', coalesce(class_name,'')),  'A') ||
setweight(to_tsvector('simple', coalesce(annotations,'')), 'B') ||
setweight(to_tsvector('simple', coalesce(signature,'')),   'B') ||
setweight(to_tsvector('simple', coalesce(content,'')),     'C')
```

## Acceptance Criteria

- [ ] `keywordRetriever` 可**脱离 LLM** 独立运行
- [ ] 返回结果包含 `chunk_id` / `rank` / `ts_rank` 分数 / 定位字段
- [ ] 以下精确标识符能命中对应 Chunk：

```text
RDelayedQueue
PurchaseTicketReqDTO
TicketServiceImpl
purchaseTicket
@Transactional
FeignClient
```

- [ ] 命中 `method_name` 的结果**排在**仅命中 `content` 的结果之前（验证字段加权）
- [ ] **子串匹配的失败案例被记录**（例如搜 `Ticket` 命中不到 `TicketServiceImpl`）——这是 P1 `pg_trgm` 的依据（见 `01-architecture-decisions.md` Risk 01）
- [ ] Tests Pass

## Dependency

M3

## Forbidden

本阶段不得：

* 立刻引入 `pg_trgm`（`初步开发计划.md` §40 明确"不要 Day 4 就立刻扩展"）
* 引入 Elasticsearch
* 实现 RRF（M7）
* 用 `LIKE '%x%'` 代替 FTS

---

# M6 Evaluation V0

## Goal

建立 ≥30 条带 Ground Truth 的 Benchmark，实现 Recall@3 / Recall@5 / MRR / Latency 的计算，并对 Vector Only 与 Keyword Only 分别产出指标。

**Benchmark 不要等项目做完才写**——此阶段就建立。

## Input

```text
M4 的 vectorRetriever
M5 的 keywordRetriever
doc_root 中的真实设计文档与实验报告
```

**Ground Truth 的真实出处**（已实测存在，见 `00-p0-scope.md` §6.3）：

```text
docs/5-后续开发规划/D2-设计分析-余票令牌桶与Lua.md
docs/5-后续开发规划/D4-设计分析-Feign移出事务与支付通知异步化.md
docs/5-后续开发规划/baseline/D2余票令牌桶与Lua实验报告.md
docs/5-后续开发规划/baseline/D4跨服务事务边界与可靠补偿实验报告.md
```

## Tasks

1. 编写 Benchmark Query（**配额按 MIXED 优先**，见下）
2. 为每条 Query 标注 Ground Truth
3. 实现指标计算：Recall@3 / Recall@5 / MRR / Latency
4. 实现评测运行器：**调用生产 Retriever**，`threshold = 0`
5. 分别跑 Vector Only 与 Keyword Only
6. 产出第一张对照表

## Output

```text
benchmark/questions.json
benchmark/ground_truth.json
app/evaluation/（指标计算 + 运行器）
evaluation/results/（首轮结果）
```

Query Schema：

```json
{
  "id": "Q001",
  "query": "purchaseTicket 方法在哪里？",
  "type": "CODE",
  "groundTruth": [
    {
      "filePath": "services/ticket-services/src/main/java/.../TicketServiceImpl.java",
      "className": "TicketServiceImpl",
      "methodName": "purchaseTicket",
      "startLine": 0,
      "endLine": 0
    }
  ]
}
```

**Query 配额**（解决 `01-architecture-decisions.md` Conflict 03）：

```text
CODE    8 ~ 10
DOC     8 ~ 10
MIXED   12 ~ 15
合计    30 ~ 35
```

## Acceptance Criteria

- [ ] ≥30 条 Query，且 **MIXED 数量最多**（反映"DEF §22 要求 MIXED 占较高比例"）
- [ ] 三类均覆盖：CODE / DOC / MIXED
- [ ] 每条 Query 有明确 Ground Truth
- [ ] CODE 的 Ground Truth 含 `file_path` / `class_name` / `method_name` / 行号
- [ ] DOC 的 Ground Truth 含 `file_path` / `heading_path`
- [ ] MIXED 的 Ground Truth **同时包含** DOC Chunk 与 CODE Chunk
- [ ] 可计算 Recall@3 / Recall@5 / MRR / Latency
- [ ] 评测**调用生产同一份 Retriever 代码**
- [ ] 评测前**不按相似度阈值过滤**（`threshold = 0`，取完整排名后再截断到 K）
- [ ] 产出首张对照表：

| Strategy | Recall@3 | Recall@5 | MRR | Avg Latency |
|---|---:|---:|---:|---:|
| Vector Only | 实测 | 实测 | 实测 | 实测 |
| Keyword Only | 实测 | 实测 | 实测 | 实测 |

- [ ] **表中不得出现编造数字**（DEF §24 明确禁止）
- [ ] Tests Pass（指标计算必须有单元测试覆盖）

## Dependency

M4、M5

## Forbidden

本阶段不得：

* 先加阈值的过滤再算指标
* 另写一条简化版 Retriever 用于评测（必须复用生产代码）
* 用 LLM 判断"回答对不对"来替代检索指标
* 照搬 RAGFlow 的 nDCG@10 / 公开数据集形态（目标不同，见 `01-architecture-decisions.md` ADR-008）
* 为了让数字好看而调整 Benchmark

---

# M7 Hybrid RRF

## Goal

引入 `HybridRetriever`：两路各取 Top-N 召回，用 RRF 融合排名，得到第三套指标。

## Input

```text
M4 的 vectorRetriever
M5 的 keywordRetriever
M6 的 Benchmark 与指标计算
```

## Tasks

1. 实现 RRF 融合
2. 实现候选集组装：Keyword Top-N + Vector Top-N
3. 实现结果去重（**按 `chunk_id`**，因为两路复用同一份 Chunk）
4. 返回调试信息（`ftsRank` / `vectorRank` / `rrfScore`）
5. 在同一 Benchmark 上跑第三套指标
6. 补齐三策略对照表
7. 执行 Failure Analysis

## Output

```text
app/ranking/rrf.py
app/retrieval/hybrid_retriever.py
evaluation/results/（三策略对照）
evaluation/failure_analysis.json
```

RRF 公式（k 起步取 60，**不调大量参数**）：

```text
score(chunk) = Σ 1 / (k + rank_i(chunk))
```

每条结果的调试信息：

```json
{
  "chunkId": "...",
  "ftsRank": 2,
  "vectorRank": 5,
  "rrfScore": 0.031,
  "filePath": "...",
  "methodName": "purchaseTicket"
}
```

## Acceptance Criteria

- [ ] `HybridRetriever` 可运行
- [ ] 融合**按 rank，不按 raw score**
- [ ] 去重键为 `chunk_id`（两路复用同一份 Chunk）
- [ ] 每条结果返回 `fts_rank` / `vector_rank` / `rrf_score`
- [ ] 三策略对照表补齐：

| Strategy | Recall@3 | Recall@5 | MRR | Avg Latency |
|---|---:|---:|---:|---:|
| Vector Only | 实测 | 实测 | 实测 | 实测 |
| Keyword Only | 实测 | 实测 | 实测 | 实测 |
| Hybrid + RRF | 实测 | 实测 | 实测 | 实测 |

- [ ] 对每条失败 Query 给出分层归因：

```text
Parsing → Chunk → Metadata → Keyword Retrieval → Vector Retrieval → Fusion
```

- [ ] 失败案例 Schema 完整：`query` / `expected_chunk` / `retrieved_top5` / `failure_stage` / `reason` / `possible_fix`
- [ ] 结果表中**不得出现编造数字**
- [ ] Tests Pass（RRF 是纯函数，必须用参数化测试覆盖）

## Dependency

M6

## Forbidden

本阶段不得：

* 改用 raw score weighted sum（FREEZE §5.10）
* 引入 Reranker（P1）
* 调整大量权重参数（RRF 只有 k 一个参数）
* 为了提升指标而针对 Benchmark 过拟合（例如手工给某些 Query 加规则）

---

# M8 Context Builder

## Goal

把 Top-K Chunks 组装成**带 Citation 的 LLM Context**。到此才开始处理"给模型看什么"的问题。

## Input

```text
M7 的 HybridRetriever 输出
```

## Tasks

1. 实现去重（按 `chunk_id`）
2. 实现 Token Budget 限制
3. 实现 DOC / CODE 各自的格式化模板
4. 实现 Citation 组装
5. 实现"必要时补 CLASS Context"（检索到 METHOD 时可带上所属 CLASS 摘要）
6. 在末尾附"使用约束"指令

## Output

```text
app/context/context_builder.py
app/context/citation.py
```

Code Context 格式：

```text
[CODE]
File:  services/ticket-services/src/main/java/.../TicketServiceImpl.java
Class: TicketServiceImpl
Method: purchaseTicket
Lines: 120-188
Code:
...
```

Doc Context 格式：

```text
[DOC]
File:    docs/5-后续开发规划/D4-设计分析-Feign移出事务与支付通知异步化.md
Section: 事务设计 / 为什么调整事务边界
Content:
...
```

Citation 形式：

```text
Java：      TicketServiceImpl.java Lines 120-188
Markdown：  D4-设计分析-Feign移出事务与支付通知异步化.md
            Section: 为什么调整事务边界
```

## Acceptance Criteria

- [ ] Context 组装可独立运行（输入 Chunk 列表，输出格式化 Context）
- [ ] 去重按 `chunk_id`
- [ ] Token Budget 生效，超限时有明确裁剪策略
- [ ] DOC / CODE 使用不同模板，可被区分
- [ ] **每个 Chunk 都带可验证的 Citation**
- [ ] Java Citation 的行号与 Chunk 的 `start_line` / `end_line` 一致
- [ ] Markdown Citation 的 Section 与 `heading_path` 一致
- [ ] Context 末尾附有"仅依据以上内容回答 / 信息不足就说明"的约束指令
- [ ] **不做复杂的 citation reconstruction**（JavaParser 已给出确定行号，不需要事后用相似度猜）
- [ ] Tests Pass

## Dependency

M7

## Forbidden

本阶段不得：

* 接 LLM（M9）
* 实现复杂 Prompt Engineering（第一版保持极简）
* 用相似度反推 Citation 来源
* 在 Context Builder 里做"类上下文补全"之外的检索逻辑（State 只保存跨 Node 共享的数据，DEF §18）

---

# M9 Generation

## Goal

完成最小问答闭环：`Question → Context → Answer + Citation`。

## Input

```text
M8 的 Context Builder
LLM API（M9 前实测可达，见 02-tech-stack-todo.md §5）
```

## Tasks

1. 接入 LLM（通过 LangChain 的统一接口，见 `02-tech-stack-todo.md` §5）
2. 编写极简 Prompt
3. 实现 `Question + Context → Answer`
4. 验证 Citation 出现在答案中
5. 验证"Context 不足时"的行为

## Output

```text
app/generation/（LLM 接入 + Prompt）
```

Prompt 第一版只要求：

```text
根据 Context 回答
不要使用 Context 之外的信息
如果 Context 不够：明确说明
引用对应文件 / 方法 / 行号
```

## Acceptance Criteria

- [ ] 能对 CODE / DOC / MIXED 三类 Query 各生成一次答案
- [ ] Answer 携带 Citation
- [ ] Context 不足时，Answer **明确说明"不足"**而不是编造
- [ ] 不引入复杂 Prompt Engineering
- [ ] 不实现 LLM-as-Judge
- [ ] Tests Pass（至少覆盖 Prompt 组装与 Context 不足分支）

## Dependency

M8

## Forbidden

本阶段不得：

* 实现多轮对话 / 对话历史
* 实现 Streaming（P1）
* 实现代码生成 / 代码修改（Non-goals）
* 用 LLM 做检索决策（路由在 M10，且初期为 Rule-based）

---

# M10 Query Router

## Goal

实现 DOC / CODE / MIXED 路由，使系统根据问题性质选择检索策略。

## Input

```text
M9 的完整问答链路
```

## Tasks

1. 实现 Query 分析
2. 初期用 **Rule-based** 判断
3. 定义路由到检索策略的映射
4. 在 Benchmark 上验证路由准确性

## Output

```text
app/graph/query_router.py
```

路由规则（初期）：

```text
存在明显 Identifier（CamelCase / ClassName / methodName() / annotation / .java）
    → 倾向 CODE

存在 Why / Design / Trade-off / 为什么 / 设计 / 取舍
    → 倾向 DOC

同时存在两者
    → MIXED
```

## Acceptance Criteria

- [ ] 能区分 DOC / CODE / MIXED 三类
- [ ] 以下示例路由正确：

```text
purchaseTicket 在哪里？                    → CODE
为什么使用 Token Bucket？                  → DOC
为什么 Feign 调用要移出事务？具体改了哪些代码？ → MIXED
```

- [ ] 在 Benchmark 上的路由准确率被记录
- [ ] 路由错误案例被记录
- [ ] Tests Pass

## Dependency

M9

## Forbidden

本阶段不得：

* 一开始就用 LLM 做分类（`初步开发计划.md` §68 明确"不要一开始 LLM 分类"）
* 把检索参数暴露给用户调节（由 Router 决定，见 R-RAG §5.7）
* 实现跨语言检索

---

# M11 LangGraph

## Goal

用 LangGraph 把 Router / Retrieve / Context / Generate 编排成工作流。

**此时已经有真正可编排的东西了**（Retriever、Router、Context Builder、LLM）。

## Input

```text
M10 的 Router
M7 的 HybridRetriever
M8 的 Context Builder
M9 的 Generation
```

## Tasks

1. 定义 State（只保存跨 Node 共享的数据）
2. 实现 Node：`classify_query` / `retrieve` / `build_context` / `generate`
3. 实现 Conditional Edge（DOC / CODE / MIXED 分支）
4. 组装 **DAG 形态**的图（**本阶段不加循环**）
5. 验证一次完整编排
6. 记录每一步的中间状态

## Output

```text
app/graph/workflow.py
app/graph/state.py
```

State 初步定义（DEF §18，**不得把所有局部临时变量写入**）：

```text
question
rewritten_query
query_type
doc_results
code_results
merged_results
context
context_quality
retry_count
answer
citations
```

## Acceptance Criteria

- [ ] LangGraph Workflow 可完成一次完整编排
- [ ] State 字段与定义一致，**未混入局部临时变量**
- [ ] DOC / CODE / MIXED 三个分支均可走通
- [ ] **本阶段为 DAG，不含循环**（循环在 M12）
- [ ] 每一步的中间状态可被观察（对应 `初步开发计划.md` §84 的可观察性要求）
- [ ] Tests Pass

## Dependency

M10

## Forbidden

本阶段不得：

* 加入 Retry / Rewrite / Loop（这是 M12）
* 把 LangGraph 当作架构中心（DEF §6：LangChain / LangGraph 是 building block，不是项目架构）
* 实现 Multi-Agent（Non-goals）

---

# M12 Agentic Retry

## Goal

加入 Context Evaluation 与受限 Retry，完成 Agentic Retrieval 闭环。

## Input

```text
M11 的 LangGraph DAG
```

## Tasks

1. 实现 `evaluate_context` 节点：判断当前 Context 是否足够
2. 实现 `rewrite_query` 节点
3. 实现 Conditional Edge：`Enough → generate` / `Not Enough → rewrite → retrieve`
4. 实现 Retry 计数与上限（`MAX_RETRY = 2`）
5. 实现超限后的明确终止
6. 记录 `retry_count` 与每次重试的差异

## Output

```text
app/graph/workflow.py（加入循环）
app/graph/nodes/context_evaluator.py
app/graph/nodes/query_rewriter.py
```

Context Evaluation 输出示例：

```json
{
  "enough": false,
  "reason": "Only design documentation found; implementation code is missing."
}
```

## Acceptance Criteria

- [ ] `evaluate_context` 可判定 Enough / Not Enough
- [ ] Not Enough 时执行 Query Rewrite 并重新检索
- [ ] **Retry 上限 = 2**，不出现无限循环
- [ ] 两次重写后仍不足时，系统**明确返回**"当前索引内容不足以可靠回答该问题"，**而不是继续让模型推测**
- [ ] `retry_count` 被记录并可在结果中观察
- [ ] 至少找到 1 个"第一次 Context 不足、重试后改善"的真实案例（用于 Demo）
- [ ] Tests Pass（循环终止条件必须有测试覆盖）

## Dependency

M11

## Forbidden

本阶段不得：

* 允许无限循环
* 让 LLM 自行决定是否重试（重试次数由代码硬限制）
* 把 Retry 当作"提高指标的手段"而忽略其成本（延迟与 token 都增加）

---

## 附录 A：里程碑依赖图

```text
M0 Project Freeze
 │
 └─> M1 JavaParser Minimal Prototype
      │
      └─> M2 Repository Parsing
           │
           └─> M3 Storage
                │
                ├─> M4 Vector Baseline ──┐
                │                        │
                └─> M5 Keyword Baseline ─┤
                                         │
                                         └─> M6 Evaluation V0
                                              │
                                              └─> M7 Hybrid RRF
                                                   │
                                                   └─> M8 Context Builder
                                                        │
                                                        └─> M9 Generation
                                                             │
                                                             └─> M10 Query Router
                                                                  │
                                                                  └─> M11 LangGraph
                                                                       │
                                                                       └─> M12 Agentic Retry
```

## 附录 B：与 `初步开发计划.md` 时间线的对应

| 里程碑 | 对应 `初步开发计划.md` 的阶段 | 参考天数 |
|---|---|---|
| M0 | 第 0 阶段（Schema / Scope 冻结） | Day 0 |
| M1 | 阶段二（JavaParser 最小验证） | Day 1 |
| M2 | 阶段三 + 阶段四（Repository Ingestion + Markdown Chunker） | Day 2 |
| M3 | 阶段五（PostgreSQL + pgvector） | Day 3 上午 |
| M4 | 阶段六（Embedding Pipeline + Vector Only） | Day 3 下午 |
| M5 | 阶段七（PostgreSQL FTS） | Day 4 上午 |
| M6 | 阶段八 + 阶段九（Benchmark V0 + 两个 Baseline） | Day 4 下午 ~ Day 5 上午 |
| M7 | 阶段十（RRF Hybrid）+ 阶段十一（Failure Analysis） | Day 5 下午 ~ Day 6 上午 |
| M8 | 阶段十二（Context Builder） | Day 6 下午 |
| M9 | 阶段十三（LLM Answer） | Day 7 |
| M10 | 阶段十四（Query Router） | Day 8 |
| M11 | 阶段十五（LangGraph） | Day 9 |
| M12 | 阶段十六（Context Evaluation + Retry） | Day 10 |

Day 11～15 为 P1 优化与文档，**不在本文件范围**。

## 附录 C：每个里程碑的 Git Commit 建议

```text
M1  feat(parser): add JavaParser method chunk extraction
M1  feat(parser): add class summary chunks
M1  feat(parser): add constructor chunks
M2  feat(scan): add repository scanner with code/doc roots
M2  feat(chunk): add markdown heading chunker
M3  feat(storage): add knowledge chunk schema
M3  feat(storage): add pgvector and fts indexes
M4  feat(embedding): add embedding input builder
M4  feat(retrieval): add vector retriever
M5  feat(retrieval): add postgres full text retriever
M6  feat(eval): add retrieval benchmark and metrics
M7  feat(rank): add reciprocal rank fusion
M8  feat(context): add context builder and citations
M9  feat(generation): add llm answer with citations
M10 feat(graph): add query routing
M11 feat(graph): add langgraph workflow
M12 feat(graph): add context evaluation and retry
```

每个里程碑独立 Commit，便于后期回退、比较与写 README。

## 附录 D：里程碑阻塞项速查

| 里程碑 | 开始前必须确认 |
|---|---|
| M1 | JavaParser 稳定版本号；Repository 双根模型（ADR-012）；本机 JDK 21 / Maven；配置管理方式 |
| M3 | Embedding 模型与向量维度 `N`；PostgreSQL + pgvector Docker tag；Python 数据库驱动；Schema 全部细节；本机 Docker |
| M4 | Embedding API 实测可达 |
| M6 | Benchmark 配额（MIXED 优先，Conflict 03） |
| M9 | LLM API 实测可达 |

详见 `02-tech-stack-todo.md` §11。
