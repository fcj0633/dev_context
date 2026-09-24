# DevContext-Java 技术决策 TODO

> 文档编号：`02-tech-stack-todo`
> 阶段：M0 Project Freeze
> **本文只整理"真正还没有冻结的 Implementation Decision"。**
> **不重新讨论 Frozen Architecture**（那部分见 `01-architecture-decisions.md`）。

---

## 0. 本文的边界

### 0.1 已冻结、本文不再讨论

以下由 `开发前任务冻结.md` §5 冻结，本文**不提供替代方案**：

```text
Java 解析器        → JavaParser（不是 tree-sitter / ANTLR / 正则 / LLM）
Java Chunk 粒度    → METHOD 主体 + CLASS 摘要 + CONSTRUCTOR
Java content 来源  → Range + 原始文件切片
Markdown Chunk     → Heading-aware
存储               → PostgreSQL + pgvector
Keyword Retrieval  → PostgreSQL FTS
Vector Retrieval   → pgvector
Hybrid Fusion      → RRF
Evaluation         → 独立于 Generation
SymbolSolver       → P0 不做
Reranker           → P0 不做
LangGraph          → Retrieval Pipeline 稳定后接入
```

### 0.2 本文的 Status 约定

| Status | 含义 |
|---|---|
| `PROPOSED` | 有依据但未经实验或官方页面复核，可在 M1 前推翻 |
| `TO-VERIFY` | 已有候选但**版本号尚未从官方来源确认**，需专门核验 |
| `BLOCKING` | 不确认就无法进入下一个里程碑 |

**本文不把任何 `PROPOSED` 伪装成 `FINAL`。**

### 0.3 本轮版本核验的实际限制（必须如实说明）

`开发前任务冻结.md` §14 要求版本结论必须记录 `Source / Checked Date / Stable Version / Compatibility`，并优先使用官方 Documentation / GitHub Release / Package Registry。

**本轮未能做到。** 执行本轮工作的隔离环境存在出网白名单限制：

```text
允许：api.deepseek.com
阻断：github.com / pypi.org / maven central / postgresql.org / 其他
```

因此 §9 的版本表来自**可访问的搜索结果标题与第三方镜像站**，**不是官方页面**。所有版本结论一律标 `TO-VERIFY`。

`开发前任务冻结.md` §14 同时给出了备选路径：

> 如果版本核验工作明显扩大本轮范围，可以只将其记录到 `tech-stack-todo.md`，留给下一轮专门执行。

本文采用这条路径：**版本号记录为待核验，把核验工作整体移交给下一轮**（见 §11）。

---

## 1. Runtime

## Decision: Java 版本

### Problem

DevContext 由两部分组成：Java 侧的 Parser 模块（独立 CLI）与 Python 侧的主系统。Java 侧需要选定编译目标与运行时版本。它必须能解析 Java 21 源码（被索引仓库使用 Java 21），同时它自身的编译目标可以独立选择。

### Options

1. Java 21（与被索引仓库一致）
2. Java 17（LTS，兼容性更广）
3. Java 11（沙箱当前版本，仅作说明）

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **Java 21** | 与本机 my12306 运行环境一致；避免"能解析但没在本机验证过"的风险；LTS | 需要本机 JDK 21（**已有，见下**） | 低 |
| Java 17 | LTS，生态兼容性最广 | 本机需额外装一个 JDK；与 my12306 的 21 环境不一致，增加"两个 JDK"的心智负担 | 中 |
| Java 11 | 沙箱已有 | 不支持 `record` / `sealed` 等语法；且 my12306 用 21 | 高（不可行） |

### Recommendation

**Java 21。**

### Reason

1. **被索引仓库就是 Java 21。** 已实测确认 my12306 根 `pom.xml`：

```xml
<properties>
    <java.version>21</java.version>
    <spring-cloud.version>2023.0.3</spring-cloud.version>
</properties>
```

使用同一个版本可以避免"Parser 的 JDK 与被解析代码的 JDK 不一致"这类难以排查的问题。

2. **开发者的构建链路已经是 Java 21。** my12306 是 Spring Boot 3.3.4 项目（Spring Boot 3.x 要求 Java 17+），说明本机已具备 JDK 21 环境，不需要新增安装。
3. **JavaParser 的 LanguageLevel 需要显式设为 21。** R-JAVA §7.5 指出 `ParserConfiguration` 的默认语言级别是 `POPULAR`（`ParserConfiguration.java:284`），其具体对应版本未查证。应显式设置：

```java
new ParserConfiguration()
    .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
    .setStoreTokens(true)        // 绝不能关，见 ADR-003
    .setAttributeComments(true);
```

4. **Java 11 不可行。** 本轮工作沙箱的 JDK 是 11，但它只用于文档工作；M1 的 JavaParser 原型**无法在沙箱内运行**，必须在开发者本机的 JDK 21 上执行（见 §10）。

### Status

`PROPOSED`（依据充分，但需 M1 前在本机确认 `java -version`）

---

## Decision: Python 版本

### Problem

DevContext 的主系统（Ingestion / Retrieval / Evaluation / Agent）用 Python 实现。需要选定版本，它决定了可用的类型语法、`asyncio` 能力，以及第三方库（LangChain / LangGraph）的最低要求。

### Options

1. Python 3.12
2. Python 3.11
3. Python 3.13
4. Python 3.10（本轮沙箱版本）

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| Python 3.12 | 性能与错误信息改进明显；生态成熟，LangChain / LangGraph 完全支持；`type` 语句等新语法可用 | 无实质缺点 | 低 |
| Python 3.11 | 稳定，兼容性最广 | 比 3.12 少了部分性能与语法特性 | 低 |
| Python 3.13 | 最新 | RAGFlow 快照的 `AGENTS.md` 声称其后端要求 Python 3.13+，但那是 RAGFlow 的需求，不是 DevContext 的；过新版本会让部分依赖尚未发布 wheel | 中 |
| Python 3.10 | 沙箱现成 | 缺少 3.11+ 的部分类型语法；且沙箱**不用于运行本项目** | — |

### Recommendation

**Python 3.12**（回退选项 3.11）。

### Reason

1. **LangChain / LangGraph 的版本要求尚未核验**（见 §9），选 3.11/3.12 是安全区间——两者都远高于主流 AI 库的最低要求，同时避开 3.13 可能存在的 wheel 缺失问题。
2. **本项目规模不需要 3.13 的新特性。** DevContext 的计算量集中在 Embedding 调用与 SQL 查询，不在纯 Python 循环上。
3. **沙箱版本无关。** 沙箱的 Python 3.10.12 只用于本轮文档工作；实际开发在开发者本机。

### Status

`PROPOSED`（需 M1 前在本机确认 `python --version`；若本机已有 3.11 或 3.12，直接采用，不强制升级）

---

## 2. Java Parser

## Decision: JavaParser 稳定版本

### Problem

Frozen Decision 已确定使用 JavaParser，但**具体版本未定**。`开发前任务冻结.md` §7（Task 3）对该项提出了三条硬性要求：

```text
Stable Release
支持 Java 21
不使用 SNAPSHOT
```

### Options

| 候选 | 来源 | 是否符合三条要求 |
|---|---|---|
| `3.28.x`（稳定） | 调研快照 readme 的依赖示例给出 `3.28.2`；第三方镜像站可见 `3.28.0` / `3.28.2` | 待核验 |
| `3.29.0-SNAPSHOT` | 调研快照 `pom.xml` 的 master 分支版本 | **否 —— SNAPSHOT，明确禁止** |
| `3.26.x` | 知识截止期内已知的稳定线 | 待核验是否支持 Java 21 的完整语法 |

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **3.28.x** | 调研快照的 readme 自身就以该版本作为依赖示例，说明这是当时的推荐稳定版；来源可追溯到项目自身文档 | 版本号需官方复核 | 低 |
| 3.29.0-SNAPSHOT | 最新 | **SNAPSHOT 版本不稳定、可能随时变动、不可复现构建**，违反明确要求 | 高（不可行） |
| 3.26.x | 稳定 | 是否覆盖 my12306 使用的全部 Java 21 语法未经核验 | 中 |

### Recommendation

**`javaparser-core` 的 `3.28.x` 稳定版**，且**只引入 `javaparser-core`，不引入 `javaparser-symbol-solver-core`**。

**具体补丁号（3.28.0 / 3.28.1 / 3.28.2 …）在下一轮版本核验中从 Maven Central 确认后锁定。**

### Reason

1. **3.29.0-SNAPSHOT 必须排除。** 调研仓库快照的 master 分支版本是 SNAPSHOT（R-JAVA §0 明确标注），SNAPSHOT 不可复现，直接违反"不使用 SNAPSHOT"。
2. **3.28.x 有来自项目自身的依据。** `参考项目/javaparser-master/readme.md` 给出的依赖示例是 `3.28.2`。这是个可追溯的、来自官方仓库文档的信号，比第三方博客可靠。
3. **只引 core 的理由已在 ADR-001 论证**：`javaparser-core` 的 `<dependencies>` 段为空（零运行时依赖），而 `javaparser-symbol-solver-core` 会带入 javassist / guava / checker-qual。这与"独立 CLI、输出 JSON"的设计契合。
4. **Java 21 支持需要显式配置而非依赖默认值。** 无论选哪个版本，都必须显式设置 `LanguageLevel.JAVA_21`（见 §1 的 Java 版本决策），不能依赖 `POPULAR` 默认值。
5. **必须同时确认它的最低 JDK 要求。** JavaParser 自身编译目标与本项目的 Java 21 是否兼容，需在核验时一并确认。

### Status

`TO-VERIFY`

> **核验要点**（下一轮执行）：
> 1. Maven Central 上 `com.github.javaparser:javaparser-core` 的最新 **stable** 版本号
> 2. 该版本的**最低 JDK 要求**（能否在 JDK 21 上运行）
> 3. 该版本的 **Java 21 语法覆盖度**（`record` / `sealed` / pattern matching）
> 4. 是否支持 `ParserConfiguration.LanguageLevel.JAVA_21`
>
> 核验完成后把 Status 改为 `FINAL` 并锁定补丁号。

---

## 3. Database

## Decision: PostgreSQL 版本

### Problem

存储与全文检索依赖 PostgreSQL 的 `tsvector` / `ts_rank` / `setweight` 等能力。需要选定主版本，它同时约束 pgvector 的可选版本。

### Options

1. PostgreSQL 18.x（搜索结果指向的当前主版本）
2. PostgreSQL 17.x
3. PostgreSQL 16.x

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **18.x** | 当前主版本，官方仍在发布小版本（搜索结果出现 18.3 / 18.4 release notes）；pgvector 有针对 pg18 的打包（`postgresql-18-pgvector`） | 需核验官方支持状态 | 低 |
| 17.x | 上一主版本，稳定 | 不如 18 新；Docker 镜像同样可用 | 低 |
| 16.x | 更保守 | 接近维护末期 | 低 |

### Recommendation

**PostgreSQL 18.x**（回退选项 17.x）。

### Reason

1. **P0 用不到版本特有能力。** DevContext 只用 FTS（`tsvector` / `ts_rank`）、`pgvector` 扩展、普通 SQL。16 以上版本都满足。因此选择依据是"官方仍在积极维护的主版本"，而不是特定功能。
2. **Docker Compose 一键启动是 P0 要求**（DEF §27-16），主流版本都有官方镜像，切换成本极低。这意味着这个决策**即使选错也不会阻塞项目**。
3. **优先与 pgvector 的打包情况对齐**：搜索结果中出现了明确标注 PostgreSQL 18 的 pgvector 包（`postgresql-18-pgvector (0.8.6-1)`），说明两者已组合使用。

### Status

`TO-VERIFY`（需确认官方支持的主版本与 Docker 镜像 tag）

---

## Decision: pgvector 版本

### Problem

向量检索依赖 pgvector 扩展。需要确定版本，并保证与所选 PostgreSQL 主版本兼容。

### Options

1. `0.8.6`（搜索结果中与 PostgreSQL 18 组合打包的版本）
2. `0.8.3` / `0.8.2`（同一 `0.8.x` 线）
3. `0.8.7`（搜索结果中标注为 unreleased）

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **0.8.6** | 搜索结果中明确存在针对 PostgreSQL 18 的打包（`postgresql-18-pgvector 0.8.6-1`）；`0.8.x` 线含 HNSW | 需官方复核 | 低 |
| 0.8.3 / 0.8.2 | 同样属 0.8 线 | 更旧 | 低 |
| 0.8.7 | 最新 | 搜索结果标注 **unreleased**，**不可用于生产** | 高（不可行） |

### Recommendation

**pgvector `0.8.x` 稳定版**（当前信号指向 `0.8.6`），**不使用 `0.8.7`**。

### Reason

1. **`0.8.7` 是 unreleased 版本。** 与"不使用 SNAPSHOT"是同一条原则——不可复现的版本不进 P0。
2. **`0.8.x` 提供 HNSW 索引**，这是 P0 向量检索需要的。参数上对应 RAGFlow 的 `knn_num_candidates` 概念（R-RAG §4）：pgvector 侧由 `hnsw.ef_search` 控制。
3. **Docker 镜像 `pgvector/pgvector:pg18` 是推荐落地方式**——它把 PostgreSQL 与 pgvector 打包在一起，省去"手动编译扩展"的步骤。具体 tag 需核验。

### Status

`TO-VERIFY`

---

## Decision: PostgreSQL 与 pgvector 的兼容性

### Problem

pgvector 是 PostgreSQL 扩展，每个 pgvector 版本只支持特定范围的 PostgreSQL 主版本。选错组合会导致扩展无法加载。

### Options

1. 由 Docker 镜像统一提供（`pgvector/pgvector:pgNN`）
2. 在已有 PostgreSQL 上自行编译安装 pgvector
3. 使用打包发行版提供的组合（如 `postgresql-18-pgvector`）

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **Docker 镜像** | 版本组合由官方维护者预先验证；一行 `image:` 即可；符合 DEF §27-16 的 Docker Compose 要求 | 需要本机有 Docker | **最低** |
| 自行编译 | 版本自由 | 需要 `pg_config` / 编译链；Windows 上尤其麻烦 | 高 |
| 发行版打包 | 有包管理器校验 | Windows 上无对应包管理路径 | 中 |

### Recommendation

**用 `pgvector/pgvector` 官方 Docker 镜像固定 PostgreSQL 与 pgvector 的组合**，在 `docker-compose.yml` 中显式写死具体 tag（不用 `latest`）。

### Reason

1. **把"兼容性"问题消解在镜像选择上。** 这正是 DEF §12 选择"PostgreSQL + pgvector 单库"的延伸——连版本兼容也不需要自己维护。
2. **符合 P0 的 Docker Compose 要求**（DEF §27-16）。
3. **固定具体 tag 而非 `latest`** 是可复现性的前提。M3 阶段必须把 tag 写入 `docker-compose.yml` 并记录在 README 中。
4. **需要核验的具体项**：镜像 tag 命名规则、该 tag 内含的 pgvector 版本、该镜像在 Windows + Docker Desktop 下的可用性。

### Status

`TO-VERIFY`（**BLOCKING 级别的细节**：不确认 tag 就无法写 `docker-compose.yml`，但只需选一个即可推进）

---

## Decision: Python 端数据库访问方式

### Problem

Python 侧需要完成三类差异很大的操作：

```text
1. 普通 CRUD（写入 Chunk、读取评测结果）
2. FTS 查询（tsvector / ts_rank / setweight 相关 SQL）
3. pgvector 查询（向量距离运算符 <=> / <->）
4. Evaluation 的 RRF 与指标计算 SQL
```

这些操作里，**SQL 表达力是核心**，而不是对象映射。

### Options

1. `psycopg`（v3，直连驱动 + 显式 SQL）
2. `psycopg2`（旧驱动）
3. SQLAlchemy（ORM 或 Core）
4. asyncpg（异步驱动）
5. 其他（`pg8000`、DuckDB 等）

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **psycopg 3** | 原生支持 pgvector 的 `vector` 类型适配；SQL 直写，FTS / 向量运算符一目了然；参数化查询安全；支持同步与异步 | 需要手写 SQL（但本项目**本来就需要**手写） | **低** |
| psycopg2 | 生态最成熟 | 维护模式，对 pgvector 类型适配不如 v3 顺 | 中 |
| SQLAlchemy ORM | 模型声明清晰，迁移方便 | **为了做 FTS/pgvector/RRF 必须大量 `text()` 逃生**，ORM 的抽象在此处不产生价值却增加一层；引入 Alembic 等配套复杂度 | **高** |
| asyncpg | 性能最好 | 需要额外的 pgvector 类型注册；异步模型会给 P0 增加不必要的并发复杂度（ingestion 是批处理，不是高并发服务） | 中 |
| pg8000 | 纯 Python | 生态与类型支持弱于 psycopg | 中 |

### Recommendation

**`psycopg`（v3）**，同步接口为主，SQL 显式书写。

不引入 ORM，不引入 Alembic。Schema 用 `.sql` 文件管理（DEF §26 的目录结构已预留 `sql/schema.sql` 与 `sql/indexes.sql`）。

### Reason

1. **项目的 SQL 本身就是核心交付物。** `setweight` 字段加权、`ts_rank` 排序、`embedding <=> query_vec` 向量检索、RRF 的 SQL 实现——这些是"能被解释、能被面试讲"的部分（DEF §33 原则四：核心模块必须确保开发者能够解释）。ORM 会把它们藏进抽象层，**与项目目标相反**。
2. **明确需要避免"默认采用复杂 ORM"。** `开发前任务冻结.md` §7（Task 3）特别提示：

> 不要因为"企业项目常用 ORM"就默认采用复杂 ORM。

3. **项目规模不需要 ORM。** 实测语料约 236 个 Java 文件 + 约 54 个有效 md，Chunk 数量在千级到万级，表只有 `knowledge_chunk` / `evaluation_case` / `evaluation_result` 等少数几张。ORM 解决的是"大团队 + 大量实体 + 频繁迁移"的问题，本项目没有这些约束。
4. **pgvector 与 psycopg 3 的适配是成熟组合。** pgvector 官方提供了 psycopg 的类型注册方式，读写 `vector` 列不需要手工字符串拼接。
5. **不选 asyncpg**：ingestion 是批处理（一次处理一个仓库），retrieval 是单查询低 QPS，Evaluation 是离线脚本。异步带来的并发收益在此场景接近零，而调试成本与类型注册成本是实打实的。

### Status

`PROPOSED`（**BLOCKING**：M3 开始前必须确认，它决定 `app/db/` 的全部写法）

---

## 4. Serialization

## Decision: Java CodeChunk 的 JSON 序列化库

### Problem

Java Parser 模块（独立 CLI）需要把 CodeChunk 列表输出为 JSON，交给 Python 侧读取。需要选定序列化库。

`开发前任务冻结.md` §7（Task 3）对此的要求是：

```text
简单
稳定
无额外复杂架构
```

### Options

1. Jackson（`jackson-databind`）
2. Gson
3. `javaparser-core-serialization`（JavaParser 自带模块）
4. 手工拼 JSON 字符串

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **Jackson** | 事实标准；注解驱动；Spring Boot 已传递依赖它（**零新增依赖**）；处理 `Optional`、中文、转义都很成熟 | 对简单 DTO 略显"重"，但重的是库不是用法 | **最低** |
| Gson | API 更简洁 | 需要**新增一个依赖**；Spring Boot 生态默认是 Jackson | 低 |
| `javaparser-core-serialization` | JavaParser 官方 | 序列化的是**完整 AST 节点树**，不是扁平的 Chunk 列表；引入后还要写转换层 | **高（方向错误）** |
| 手工拼接 | 零依赖 | 转义、嵌套、中文处理全靠自己写，容易出错 | 中 |

### Recommendation

**Jackson（`jackson-databind`）。**

### Reason

1. **零新增依赖。** my12306 是 Spring Boot 3.3.4 项目，Jackson 已在依赖树中。Java Parser 模块即使完全独立（不在 my12306 的 Maven 父项目下），引入 Jackson 也只是一个成熟的、无传递依赖问题的坐标。
2. **`javaparser-core-serialization` 是错误方向。** R-JAVA §12 已明确论证：它输出的是完整 AST 的 JSON（节点树），而 DevContext 需要的是**扁平的 Chunk 列表**（每个 Chunk 带 metadata）。两者结构不同，用它只会引入一次冗余转换。

> 补充说明：JavaParser 自 3.6.17 起支持 AST 序列化为 JSON（`readme.md:81`），有独立模块，但**不适用于本场景**。

3. **满足"简单、稳定、无额外复杂架构"。** Jackson 的用法在本场景只需"一个 DTO 类 + `ObjectMapper.writeValueAsString()`"，不需要自定义序列化器、不需要多态类型、不需要视图（View）。
4. **需要显式确定两点**（列入核验项）：
   - `Optional` 字段的处理方式（`getBody()` / `getRange()` 都是 `Optional`，DTO 里应转换为 `null` 或省略，而不是序列化 `Optional` 对象）
   - 中文标注（`annotations` / `javadoc` 可能是中文）的编码方式——必须统一 UTF-8

### Status

`PROPOSED`

---

## 5. LLM / Agent

> **重要**：本节的组件**不是第一阶段开发依赖**（`开发前任务冻结.md` §7 Task 3 明确说明）。此处只确定**版本兼容性**，不进入 M1～M8。

## Decision: Embedding 模型与向量维度

### Problem

Frozen Decision 要求"P0 固定一个 Embedding Model、一个 Vector Dimension"（FREEZE §5.9），不做动态多模型 Vector Schema。

**这个决策有一个硬性前置影响**：pgvector 的列类型是 `vector(N)`，`N` 必须是编译期已知的常量。因此**模型选型直接决定建表语句**——`N` 定错就要重建表并重新 embedding 全部 Chunk。

### Options

1. 闭源 API Embedding（如 OpenAI `text-embedding-3-*` 系列，维度 1536 / 3072 可配）
2. 中文/多语言开源 Embedding（如 BGE 系列，维度常见 768 / 1024）
3. 本地 `sentence-transformers` 运行开源模型
4. 国产 API Embedding 服务

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **闭源 API** | 零部署；质量稳定；维度可选 | 需要 API Key 与网络；**代码含大量中文注释/中文 Javadoc，需确认多语言表现**；成本按量计费 | **低** |
| 多语言开源（API 形式） | 中文表现通常更好；维度较小（索引更小） | 需确认服务可用性与稳定性 | 低 |
| 本地模型 | 无网络依赖、零成本、可复现 | 需要下载模型权重与推理环境（torch / onnx）；**增加部署复杂度**；本机（Windows）环境配置成本高 | 中～高 |
| 国产 API | 网络可达性好 | 需确认维度与稳定性 | 低 |

### Recommendation

**暂不锁定具体模型。** 先确定两条硬性约束，模型在 M4 之前选定：

```text
约束 1：向量维度 N 必须是唯一的、固定的、在建表时就写死的值
约束 2：模型必须能处理「中文文档 + 英文代码标识符」混合内容
```

**倾向：优先选择"网络可达 + 无需本地部署"的 API 形式**，避免在 Windows 本机配置本地推理环境。

### Reason

1. **URL 可达性是本项目的现实约束。** 本轮执行环境对出网的严格限制（§0.3）提示：Embedding API 的可达性必须在选型时就验证，而不是等到 M4 才发现调不通。**这一点必须在 M4 之前实测确认。**
2. **语料是中文文档 + 英文代码的混合体。** 实测文档目录中的设计分析、实验报告均为中文（如 `D4-设计分析-Feign移出事务与支付通知异步化.md`），而 Java 代码是英文标识符。R-JAVA §13.4 特别指出：**中文 Javadoc 对语义检索的帮助可能很大**（"自然语言描述 + 自然语言问题，embedding 匹配度高于纯代码"）。因此模型的多语言能力是可验证的实验变量，不是可选特性。
3. **维度错误的代价是重建。** ADR-005 已决定 pgvector 单列存储向量。若中途换模型，需要 `ALTER TABLE` 改列类型 + 重新 embedding 全部 Chunk。**因此这个决策必须在 M3 建表前完成**，宁可先定一个、后续用实验数据再评估。
4. **不选本地模型**：DevContext 的学习目标是 Context Retrieval，不是模型部署。在 Windows 上配置 torch/onnx 推理环境会挤占本就有限的 10～15 天周期。

### Status

`TO-VERIFY`（**BLOCKING**：M3 建表前必须确定 `N`）

> **核验要点**：候选模型的实际可用维度；API 从本机的可达性；中文 + 英文代码混合内容上的表现；是否支持批量 embedding（ingestion 需要处理数千个 Chunk）。

---

## Decision: LLM Generation 的模型与接入方式

### Problem

M9 需要一个 LLM 完成"Question + Context → Answer + Citation"。DEF §25 把"LLM integration"划给 Python 侧。

### Options

1. 直接用官方 SDK（`openai` / `anthropic` / 对应厂商 SDK）
2. 通过 LangChain 的统一接口（`langchain-openai` 等）
3. 通过自建 HTTP 封装

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **LangChain 统一接口** | 与 ADR-011 决定的 LangGraph 技术栈天然一致；换模型成本低；Prompt 模板（`ChatPromptTemplate`）现成 | 多一层抽象；库升级可能带来 breaking change | 低 |
| 官方 SDK 直连 | 依赖最少、行为最透明 | 与 LangGraph 的集成需自己接；换模型要改代码 | 中 |
| 自建封装 | 完全可控 | 重复造轮子 | 中 |

### Recommendation

**通过 LangChain 的统一接口接入**（具体 provider 在 M9 前确定）。

### Reason

1. **ADR-011 已决定使用 LangGraph**，而 LangGraph 与 LangChain 的模型抽象是配套的。若 Generation 层直连官方 SDK，则 State / Node 里要自己处理模型调用的差异。
2. **DEF §6 明确 LangChain 的定位**："LangChain 不是项目架构中心"，但它是 `Model / Embedding / Prompt / Retriever / Output Parser` 的 building block。用它做模型接入正好符合这个定位——不把它当架构，只把它当适配层。
3. **换模型成本低**，这在"模型可达性未经实测"的现实下（见上一条决策）是有价值的缓冲。
4. **M9 才需要**，因此具体 provider 可以推迟决定。但**必须在 M9 之前实测确认可达性**。

### Status

`PROPOSED`

---

## Decision: LangChain 版本

### Problem

需要确定 LangChain 的稳定版本，并确认它与所选 Python 版本、LangGraph 版本兼容。

### Options

1. 最新稳定版（搜索结果指向 1.x 线）
2. 0.3.x 线

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **最新稳定版（1.x）** | 与 LangGraph 1.x 配套；官方维护中 | 需核验与本项目 Python 版本的兼容性 | 低 |
| 0.3.x | 知识截止期内熟悉 | 属旧线，与 LangGraph 1.x 组合需核验 | 中 |

### Recommendation

**LangChain 最新稳定版（1.x 线）**，与 LangGraph 的主版本保持同一代。

### Reason

1. **不在第一阶段使用，因此"最新稳定版"是安全选择**——真正接入时（M9）距离现在还有多个里程碑，用旧版本反而可能在接入时遇到已修复的问题。
2. **必须与 LangGraph 版本同代核验**（见下一条），不能各自独立决定。
3. **需要核验的最低 Python 版本**：这直接约束 §1 的 Python 版本决策。

### Status

`TO-VERIFY`

---

## Decision: LangGraph 版本

### Problem

确定 LangGraph 稳定版本，与 LangChain 版本配套。

### Options

1. 最新稳定版（搜索结果指向 1.x 线，例如第三方版本页显示 `1.2.5`）
2. 0.2.x 线

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **最新稳定版（1.x）** | 与 LangChain 1.x 配套；`StateGraph` / `ConditionalEdge` / cycle 等 P0 需要的能力齐全 | 需核验 | 低 |
| 0.2.x | 知识截止期内熟悉 | 属旧线 | 中 |

### Recommendation

**LangGraph 最新稳定版（1.x 线）**。

### Reason

1. **P0 需要的能力在 1.x 中是稳定的 API**：`State`（对应 DEF §18 定义的 State 字段）、`Node`、`Conditional Edge`（DOC/CODE/MIXED 路由）、`Cycle`（Retry ≤ 2）。
2. **版本号本身不是阻塞项**，因为 ADR-011 已决定 M11 才接入，届时再锁定实际版本即可。此处只需确认"1.x 与所选 LangChain / Python 兼容"。
3. **必须一起核验的项**：LangChain ↔ LangGraph 的版本配对关系、两者的最低 Python 版本。

### Status

`TO-VERIFY`

---

## 6. Testing

## Decision: Java 测试框架

### Problem

Java Parser 模块需要一个测试框架，用于验证 Chunk 抽取的正确性（`className` / `methodName` / `annotations` / `signature` / `startLine` / `endLine` / `content` 与源码一致）。

`开发前任务冻结.md` §7（Task 3）要求"Java：JUnit"。

### Options

1. JUnit 5（Jupiter）
2. JUnit 4
3. TestNG

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **JUnit 5** | 已是 Spring Boot 3.3.4 的默认测试框架（**零新增依赖**）；参数化测试（`@ParameterizedTest`）非常适合"对多个 Java 文件断言同一组不变量" | 无实质缺点 | **最低** |
| JUnit 4 | 生态成熟 | Spring Boot 3.x 已迁移到 JUnit 5；用它需要额外配置 | 中 |
| TestNG | 功能强大 | 与 Spring Boot 默认栈不一致，需新增依赖 | 中 |

### Recommendation

**JUnit 5（Jupiter）**。

### Reason

1. **零新增依赖。** my12306 使用 Spring Boot 3.3.4，其 `spring-boot-starter-test` 已经包含 JUnit 5。若 Java Parser 作为独立模块，引入 JUnit 5 也是标准做法。
2. **参数化测试正好匹配本项目的验证方式。** R-JAVA §14.7 建议"照搬 JavaParser 官方 `NodePositionTest` 的断言形式"——即对一批文件断言"每个 MethodDeclaration 都有 Range"。这正是 `@ParameterizedTest` + `@MethodSource` 的典型用法：

```text
对 my12306 的每个 .java 文件：
    断言所有 MethodDeclaration 都有 Range          （来自 R-JAVA NodePositionTest 的思路）
    断言 start_line <= end_line
    断言 content 与原始文件对应行一致
```

3. **测试在本项目中有额外意义**：M1 的验收标准之一就是"Parse Failure 不导致程序整体崩溃"，需要用测试覆盖解析失败的文件。

### Status

`PROPOSED`

---

## Decision: Python 测试框架

### Problem

Python 侧需要一个测试框架，用于验证 Markdown Chunker、FTS 查询、RRF 计算、指标计算等。

`开发前任务冻结.md` §7（Task 3）要求"Python：pytest"。

### Options

1. pytest
2. unittest（标准库）
3. nose2

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **pytest** | 事实标准；`fixture` 适合"准备测试用 Chunk 数据"；`parametrize` 适合"多组查询断言 RRF 排序"；断言即 `assert`，可读性好 | 需新增依赖（但属开发依赖） | **最低** |
| unittest | 零依赖 | 样板代码多；参数化不如 pytest 直观 | 中 |
| nose2 | 已不再活跃 | — | 高 |

### Recommendation

**pytest**。

### Reason

1. **RRF 与指标计算天然适合单元测试。** `score(d) = Σ 1/(k + rank_i(d))` 是纯函数，可以用 `parametrize` 覆盖"只在一路出现 / 两路都出现 / 排名相同 / 排名不同"等组合。R-CONT §12 也把 Continue 的 `pure-function-unit-tests` 思路作为可借鉴项。
2. **Markdown Chunker 需要参数化测试。** 不同标题层级、无标题文档、超长 Section 等情形需要并列断言，`parametrize` 是最自然的表达方式。
3. **Evaluation 脚本本身需要可测试。** 指标计算（Recall@K / MRR）如果算错，整个项目的结论都不可信——这是 P0 最需要测试覆盖的部分之一。

### Status

`PROPOSED`

---

## 7. Configuration

## Decision: 配置管理方式

### Problem

需要管理以下配置项：

```text
DB connection        （host / port / db / user / password）
Embedding Model      （provider / model name / API key / dimension）
LLM API              （provider / model / API key）
Repository Path      （code_root / doc_root —— 见 ADR-012）
Ingestion 参数        （ignore 规则 / batch size）
```

`开发前任务冻结.md` §7（Task 3）要求确定用 `.env` / config file / 环境变量中的哪一种。

### Options

1. `.env` + `.env.example`（`python-dotenv` 读取）
2. YAML / TOML 配置文件
3. 纯环境变量
4. 混合：`.env` 提供密钥与路径，YAML 提供结构化参数

### Comparison

| Option | 优点 | 缺点 | P0 复杂度 |
|---|---|---|---|
| **`.env` + `.env.example`** | 与 DEF §26 目录结构已预留的 `.env.example` 一致；密钥不进代码库；本地开发体验好；12-factor 风格 | 只能表达键值对，不适合嵌套结构 | **最低** |
| YAML | 可表达嵌套（如 `ingestion.ignore: [...]`） | 需要解析库；密钥仍需另找地方放 | 中 |
| 纯环境变量 | 无文件、适合容器 | 本地开发需要每次 export；路径与密钥混在一起难管理 | 中 |
| 混合 | 各取所长 | **两套配置来源 = 两处需要读的地方**，P0 不值 | 中 |

### Recommendation

**`.env` + `.env.example` 两文件方案。** 结构化参数（如 ignore 列表）在 P0 阶段**先按简单约定处理**（逗号分隔字符串），不引入 YAML。

### Reason

1. **DEF §26 的推荐目录结构已经预留了 `.env.example`**，采用 `.env` 与既定设计一致。
2. **密钥必须与代码分离。** Embedding / LLM 的 API Key 不能进版本库。`.env` + `.gitignore` 是最小成本的方案。
3. **P0 的配置项数量少且扁平。** 上述 5 类配置都是键值对，没有真正的嵌套需求。为"将来可能需要的嵌套"引入 YAML 解析层，违背"不为架构完整性加技术"的原则（FREEZE §13.3）。
4. **`Repository Path` 需要两个键**（ADR-012 的双根模型）：

```text
DEVCONTEXT_CODE_ROOT=
DEVCONTEXT_DOC_ROOT=
```

5. **`.env.example` 必须提交、`.env` 必须在 `.gitignore` 中**——这是必须写进 M0/M1 的检查项，否则密钥可能被误提交。

### Status

`PROPOSED`

---

## 8. 需要在 M3（建表）之前冻结的 Schema 细节

以下项来自 `01-architecture-decisions.md` 的冲突记录，**不解决就无法建表**。

| # | 待冻结项 | 来源 | 建议取值 |
|---|---|---|---|
| 1 | `chunk_type` 命名 | Conflict 01 | `DOCUMENT_SECTION` / `CLASS` / `METHOD` / `CONSTRUCTOR` |
| 2 | `knowledge_chunk` 完整字段集 | Conflict 02 | 采用 PLAN §6 字段集（含 `signature` / `annotations` / `javadoc` / `title`）+ 新增 `is_oversized` / `source_root` |
| 3 | `annotations` 存储形态 | R-JAVA §8 | 字符串数组（`text[]` 或 JSONB），**不是布尔标记** |
| 4 | `javadoc` 与 `content` 的关系 | R-JAVA §13.4 | **独立字段**，不并入 `content` |
| 5 | `embedding` 的维度 `N` | §5 Embedding 决策 | 建表前必须确定 |
| 6 | 行号基准 | R-JAVA §7.1 | 全链路 **1-based**（JavaParser 原生如此，不做 ±1 转换） |
| 7 | `file_path` 的相对基准 | ADR-012 | 双根下需明确；建议相对各自根记录 + `source_root` 标记 |
| 8 | 相似度阈值 | PLAN §51 | 评测时 `threshold = 0`；检索默认值另定 |

**建议**：M0 结束前把上表整理为 `sql/schema.sql` 的定稿依据，并在 M3 直接落地。

---

## 9. 版本核验记录

### 9.1 核验限制说明

**本次核验未能访问官方来源。** 执行环境出网白名单仅允许 `api.deepseek.com`，`github.com` / `pypi.org` / Maven Central / `postgresql.org` 均被阻断。

因此下表的版本号来自**可访问的搜索结果标题与第三方镜像站**，**一律标 `TO-VERIFY`**，不得作为最终依据。

**核验日期**：2026-09-22

### 9.2 版本信号表（全部待官方复核）

| 组件 | 检出信号 | 信号来源类型 | 是否为官方 | Status |
|---|---|---|---|---|
| JavaParser | `3.28.0` / `3.28.2` 稳定线；master 为 `3.29.0-SNAPSHOT` | 第三方版本聚合站、发行版打包页、**调研快照 readme** | 部分是（readme 来自官方仓库） | `TO-VERIFY` |
| PostgreSQL | `18.x`（出现 18.3 / 18.4 release notes） | 官方站点搜索结果 | 标题来自官方 | `TO-VERIFY` |
| pgvector | `0.8.6`（有 `postgresql-18-pgvector 0.8.6-1` 打包）；`0.8.7` 标注 unreleased | 发行版打包页、PGXN CHANGELOG | 否 | `TO-VERIFY` |
| psycopg | `3.3.2` | 发行版构建信息 | 否 | `TO-VERIFY` |
| LangChain | `1.x` 线 | 官方文档 changelog 搜索结果 | 部分是 | `TO-VERIFY` |
| LangGraph | `1.2.5` | 第三方版本页 | 否 | `TO-VERIFY` |
| Jackson | `2.21.2`（`com.fasterxml`）与 `3.0.4`（`tools.jackson`） | 第三方版本页 | 否 | `TO-VERIFY` |
| JUnit | `5.14.3`（`junit-bom`） | 官方文档站点搜索结果 | 部分是 | `TO-VERIFY` |
| pytest | `9.1.x`（出现 9.1.0 / 9.1.1） | 官方邮件列表、发行版构建 | 部分是 | `TO-VERIFY` |
| Java（本机） | `21` | **实测**（`pom.xml` 读取） | — | **已确认** |
| Spring Boot（被索引仓库） | `3.3.4` | **实测** | — | **已确认** |
| Python（本机） | 未确认 | — | — | `TO-VERIFY` |

### 9.3 下一轮核验清单

按 `开发前任务冻结.md` §19，**下一轮**专门执行 `Technical Version Verification`。核验时**必须使用官方来源**：

```text
JavaParser   → Maven Central / GitHub Releases  （确认最新 stable + 最低 JDK + Java 21 支持）
PostgreSQL   → postgresql.org 官方版本页         （确认当前支持的主版本与 EOL）
pgvector     → GitHub Releases / PGXN            （确认 stable 版本与 PostgreSQL 兼容区间）
psycopg      → psycopg.org release notes         （确认 stable 版本与最低 Python）
LangChain    → 官方 changelog                    （确认 stable 版本与最低 Python）
LangGraph    → PyPI / 官方 changelog             （确认 stable 版本及与 LangChain 的配对）
Jackson      → Maven Central                     （确认 2.x 与 3.x 的取舍，见下）
JUnit        → junit.org                         （确认 JUnit 5 最新 stable）
pytest       → docs.pytest.org changelog          （确认 stable 版本）
```

**Jackson 的一个额外核验点**：搜索结果同时出现 `com.fasterxml.jackson.core:jackson-databind 2.21.2` 与 `tools.jackson.core:jackson-databind 3.0.4`，说明 Jackson 3 已发布并**更换了 groupId**。需要确认：

- my12306 的 Spring Boot 3.3.4 依赖的是 Jackson 2.x ——**Java Parser 模块应与被索引项目保持一致，用 2.x**，避免同一构建中出现两套 Jackson。
- Jackson 3 的迁移不在 P0 范围。

### 9.4 核验记录模板（下一轮使用）

```markdown
| 组件 | Stable Version | Source（官方 URL） | Checked Date | Compatibility | Status |
|---|---|---|---|---|---|
| JavaParser | | | | 最低 JDK = ? / 支持 Java 21 = ? | FINAL |
| PostgreSQL | | | | 与 pgvector 兼容 | FINAL |
| pgvector | | | | 支持 PG 版本区间 | FINAL |
| psycopg | | | | 最低 Python | FINAL |
| LangChain | | | | 最低 Python / 与 LangGraph 配对 | FINAL |
| LangGraph | | | | 最低 Python / 与 LangChain 配对 | FINAL |
| Jackson | | | | 与 Spring Boot 3.3.4 一致（2.x） | FINAL |
| JUnit | | | | 与 Spring Boot 3.3.4 一致 | FINAL |
| pytest | | | | 最低 Python | FINAL |
```

---

## 10. 本机环境待确认清单

> 本轮工作在隔离环境中进行，其工具链**不代表开发者本机**。以下项需在 M1 开始前在本机确认。

| # | 待确认项 | 为什么需要 | 确认方式 |
|---|---|---|---|
| 1 | JDK 21 可用 | M1 的 JavaParser 原型必须运行在 21 上（见 §1） | `java -version` |
| 2 | Maven 可用 | Java Parser 模块的构建（`pom.xml`） | `mvn -version` |
| 3 | Docker + Docker Compose 可用 | M3 需要启动 PostgreSQL + pgvector（DEF §27-16） | `docker --version` / `docker compose version` |
| 4 | Python 版本 | 决定 §1 的 Python 决策落地 | `python --version` |
| 5 | 出网可达性（Embedding API） | M4 的 Embedding 调用（见 §5 决策） | 实测一次 API 调用 |
| 6 | 出网可达性（LLM API） | M9 的 Generation 调用 | 实测一次 API 调用 |
| 7 | 磁盘空间 | Embedding 模型 + Docker 镜像 + 索引数据 | — |
| 8 | 本轮沙箱不具备的能力 | 沙箱 JDK 11 / 无 Docker / 无 Maven / 出网受限 → **M1 起必须在开发者本机执行，不能在沙箱内验证** | — |

> **第 8 项是重要限制**：本轮只能产出文档。从 M1 开始的任何验证（JavaParser 能否跑通、SQL 是否可执行、指标是否算对）都必须在开发者本机进行。

---

## 11. 未冻结项汇总

| # | 待确认项 | Status | 阻塞的里程碑 | 是否 BLOCKING |
|---|---|---|---|---|
| 1 | Java 版本（本机确认 21） | `PROPOSED` | M1 | 否 |
| 2 | Python 版本 | `PROPOSED` | M2 | 否 |
| 3 | JavaParser 稳定版本号 | `TO-VERIFY` | M1 | **是** |
| 4 | PostgreSQL 主版本 | `TO-VERIFY` | M3 | 否（Docker 一行可换） |
| 5 | pgvector 版本 | `TO-VERIFY` | M3 | 否 |
| 6 | PostgreSQL + pgvector 的 Docker tag | `TO-VERIFY` | M3 | **是**（无法写 `docker-compose.yml`） |
| 7 | Python 数据库驱动（psycopg 3） | `PROPOSED` | M3 | **是**（决定 `app/db/` 写法） |
| 8 | JSON 序列化库（Jackson） | `PROPOSED` | M1 | 否 |
| 9 | **Embedding 模型与向量维度 N** | `TO-VERIFY` | M3 / M4 | **是**（决定建表语句） |
| 10 | LLM provider 与接入方式 | `PROPOSED` | M9 | 否 |
| 11 | LangChain 版本 | `TO-VERIFY` | M11 | 否 |
| 12 | LangGraph 版本 | `TO-VERIFY` | M11 | 否 |
| 13 | JUnit 版本/用法 | `PROPOSED` | M1 | 否 |
| 14 | pytest 版本/用法 | `PROPOSED` | M2 | 否 |
| 15 | 配置管理方式（`.env`） | `PROPOSED` | M1 | 否 |
| 16 | Schema 细节（§8 的 8 项） | 未冻结 | M3 | **是** |
| 17 | Repository 双根模型（ADR-012） | `PROPOSED` | M1 | **是** |

### 按里程碑排序的阻塞项

```text
M1 之前必须确认：
  · JavaParser 稳定版本号（#3）
  · Repository 根目录模型（#17）
  · 本机 JDK 21 / Maven（§10-1、§10-2）
  · 配置管理方式（#15，影响第一批文件）

M3 之前必须确认：
  · Embedding 模型与向量维度 N（#9）
  · PostgreSQL + pgvector Docker tag（#6）
  · Python 数据库驱动（#7）
  · Schema 全部细节（#16）
  · 本机 Docker（§10-3）

M4 之前必须确认：
  · Embedding API 实测可达（§10-5）

M9 之前必须确认：
  · LLM API 实测可达（§10-6）
```

**建议**：把上表中 4 个 `TO-VERIFY` 的 `BLOCKING` 项（#3 / #6 / #9，以及 Schema 细节 #16）合并为下一轮的**唯一任务**——即 `开发前任务冻结.md` §19 所述的 `Technical Version Verification` + `my12306 Repository Reconnaissance`。
