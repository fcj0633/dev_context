# Symbol Graph V1 实现与验收

验证日期：2026-10-06。实现参考 `01-symbol graph设计思路.md` 与 `02-symbol设计误差说明.txt`，并落实本轮确认的五项修正。自动增强默认关闭。

## 1. 实际交付

链路为：Java 仓库两阶段分析 → Chunk/Symbol/Edge/Diagnostics 独立输出 → 同一 repository snapshot 事务落库 → 图查询 → 现有检索之后的有界增强 → EvidenceWorkspace → Coverage/EvidencePackage → 原有 Fast/Full 回答。

- JavaParser 与 SymbolSolver 固定为 3.28.2，配置 Java 21。各 `src/main/java` 源码根与 ReflectionTypeSolver 组合，不下载业务 Maven 依赖。
- `RepositoryJavaAnalyzer` 先登记可靠符号，再提取五类关系；源码 Solver 与主解析器共享 SymbolResolver 配置，支持其内部 AST 对 record getter 的解析。
- Symbol 自然身份包含完全限定所属类型及擦除后的声明参数类型。数组/可变参数统一，重载不会按名称或参数数量猜测匹配。
- 每个入图 Symbol 对应一个已有 CODE Chunk；Chunk 序号仅用于本次分析关联，数据库 ID 不充当稳定符号身份。
- `ChunkStore.replace_repository_snapshot()` 在一个事务中替换 Chunk、Symbol、Edge；同仓库写入使用事务级锁，失败保留旧快照。
- `code_graph` 负责真实程序实体及关系。`retrieval.symbols` 仍只负责检索策略所用的符号形态检测。
- 检索侧保留现有 keyword/vector/hybrid 路由。`SearchExecution.results`、`source_candidates` 保持原始检索语义；`graph_results`、`graph_trace` 独立保存增强。
- 图结果与原始候选在 SourcePolicy 分类后进入 Workspace。图增强启用时，最终 ContextBundle 中两轮原始检索候选都排在图补充候选之前；图证据仍完整参与 Workspace Coverage 和 discovered terms。
- Trace 分别记录精确定位与关系扩展新增证据、真实路径、跳数、截断、失败及耗时。图耗时属于 evidence retrieval 内部，不重复计入顶层 StageUsage。

## 2. 契约与边界

节点：CLASS、INTERFACE、METHOD、CONSTRUCTOR。

边：EXTENDS、IMPLEMENTS、CALLS、CONSTRUCTS、OVERRIDES。CALLS 指向静态声明；接口的实现候选通过反向 OVERRIDES 取得，不改写成运行时 Bean 绑定。

OVERRIDES 使用 SymbolSolver 的祖先类型、泛型替换、MethodUsage 签名兼容及返回类型结果，配合最小 static/private/可访问性过滤。不另写 Java 类型系统。接口未显式写 public 的方法仍视为公开契约；void 返回值匹配已覆盖回归测试。

| 情况 | 处理 |
|---|---|
| 缺少依赖或无法解析声明签名 | 保留 Chunk，不入图，记录 symbol_unresolved |
| 无法解析调用/继承/覆盖关系 | 跳过该关系，记录对应诊断 |
| 外部目标已解析 | 不创建外部节点，计为 resolved_external |
| 仓库目标未建节点，包括隐式构造器、record getter | 不写悬空边，计为 internal_target_not_indexed |
| 单个 Java 文件解析失败 | 记录 parse_failed，继续其他文件 |
| 所有文件解析失败、重复可靠身份、无效 owner/Chunk 引用、非法端点 | 致命分析契约错误，阻止新快照写入 |
| 图查询失败/数据库没有图/SQL 超时 | 保留原检索结果，记录图失败，不将原搜索标记为失败 |
| 请求 Deadline 已到期 | 停止图查询，保留已得到的基础证据，后续执行遵守既有 Deadline |

固定限制：最多 3 个锚点、2 条物理关系边、每节点 5 个不同邻居、每 action 8 个新增 Chunk（含精确定位补充）。OVERRIDES 计一跳。

邻接查询先去除重复调用点，再以 `PARTITION BY node_id` 分组排序和限额。实现会读取每节点第 6 个邻居作为截断探针，但只使用前 5 个进行扩展。限制不是 frontier 共用的全局 LIMIT。

LOCATION 可精确定位但不扩展关系；CODE、BOTH、ANY 增强代码侧，DOCUMENT 不增强。CONSTRUCTS 仅覆盖显式 `new` 到已有显式构造器，不包括隐式构造器链。匿名类/局部类、enum/record 无对应类型节点的声明保留原 Chunk，不创建猜测身份；方法引用、字段初始化及初始化块关系不在首版范围内。

未解析调用无法自动确认属于仓库内部，因此不使用 unresolved 总数伪造“内部关系解析率”分母。严格关系准确率和路径召回使用已标注源码 fixture；真实仓库输出解析分类计数及逐条缺口。

## 3. 使用与配置

```powershell
uv run devcontext init-db
uv run devcontext ingest
uv run devcontext graph symbol PayServiceImpl --json
uv run devcontext graph implementations 'T:edu.swu.fcj.my12306.biz.payservice.service.PayService' --json
uv run devcontext graph callees 'M:edu.swu.fcj.my12306.biz.payservice.service.impl.PayServiceImpl#notifyPayResult(java.lang.String)' --json
uv run devcontext evaluate-symbol-graph --fixture
uv run devcontext evaluate-retrieval-workflow --suite all --mode frozen --symbol-graph-ab --output artifacts/symbol-graph-real-workflow-ab.json
```

关系查询要求完整 symbol_key，简单名只用于 symbol 查询，可能返回多个重载/所属类型。CLI 报告代码位置、解析来源和截断；callers/callees/implementations 为直接关系，hierarchy 为最多两跳类型关系，每节点限制 5 个邻居。

| 配置 | 默认值 | 含义 |
|---|---|---|
| SYMBOL_GRAPH_ENABLED | false | 仅控制 RetrievalController 自动增强，不控制 ingestion 建图 |
| SYMBOL_GRAPH_QUERY_TIMEOUT_SECONDS | 2 | 数据库连接/单次查询安全超时，同时受请求剩余时间约束 |

100ms 是完整图增强的 P95 测量目标，不是 SQL 默认硬截止，也没有固定 100/200ms 剩余时间准入门槛。连接超时遵循 PostgreSQL 客户端整数秒限制；每批查询前重新检查请求 Deadline。

JavaParserRunner 的 `parse()` 保留兼容；新增 `analyze()` 返回 JavaAnalysisResult。`001_schema.sql`、`002_symbol_graph.sql` 按文件名顺序幂等执行，无迁移框架。已有索引必须重新 ingestion 才有图数据。

## 4. 验证结果

### 自动化测试

- Python：656 项通过，包含启用的 PostgreSQL 集成测试。
- Java：6 项通过，包含既有 CRLF Chunk 测试及跨模块调用、泛型覆盖、跨包接口隐式 public/void、record getter、缺失依赖、重复身份与嵌套调用归属。
- PostgreSQL：重复初始化、同仓库并发重建、跨仓库隔离、写入中途失败回滚、旧入口级联清理、每节点邻居限额与重复调用点去重通过。
- 关闭开关不构建图服务；增强不改写原始候选；第一轮图噪声不能挤掉第二轮原始证据；Deadline 无固定过严准入门槛的测试通过。

### 真实 my12306 快照

244 个 Java 文件全部解析成功，601 个 CODE Chunk、567 个 Symbol、430 条 Edge；与 3848 个 DOCUMENT Chunk 在同一事务中落库，总计 4449 个 Chunk。

| 节点 | 数量 | 关系 | 数量 |
|---|---:|---|---:|
| CLASS | 189 | CALLS | 221 |
| INTERFACE | 39 | CONSTRUCTS | 112 |
| METHOD | 315 | EXTENDS | 21 |
| CONSTRUCTOR | 24 | IMPLEMENTS | 20 |
| — | — | OVERRIDES | 56 |

声明签名未解析 34 条；调用未解析 2032 条；构造器调用未解析 25 条；覆盖/继承关系未解析分别为 11/36 条。外部目标已解析并跳过 372 次，仓库内未入图目标跳过 68 次。这些数据表示首版缺少业务依赖和生成代码时的覆盖缺口，不意味着不存在相应源码关系。

### 受控源码 Graph benchmark

8 类案例，使用固定初始候选及 Oracle Coverage，无 embedding/LLM 调用，临时 fixture 仓库结束后清理。

| 指标 | 结果 |
|---|---:|
| 平均证据召回 OFF / ON | 30.625% / 100% |
| 关系扩展 GRAPH_RESCUE 案例 | 7 |
| 关系扩展新增 Gold 命中 | 14 |
| 精确定位新增 Gold 命中 | 2 |
| 路径召回 | 100% |
| 新增证据 Noise Rate | 20% |
| 图增强 P95 | 约 50ms |
| False READY OFF / ON | 0 / 0 |

此基线刻意固定锚点，隔离图扩展能力；不能解读为真实语义检索召回从 30.625% 提升到 100%。

### 真实检索工作流 OFF/ON

在同一刷新后快照、同一 stage-aware 策略、初始查询、top-k 和上下文预算下，对 L1.5 + regression 共 24 个案例运行 frozen 模式；保留真实数据库搜索和 embedding，Coverage 使用冻结 Gold 的 Oracle。

| 指标 | OFF | ON |
|---|---:|---:|
| CORE requirement coverage | 96.15% | 96.15% |
| Full-case success | 83.33% | 83.33% |
| False READY | 0 | 0 |
| 执行错误 | 0 | 0 |

ON 共执行 56 个图增强 action，记录 49 次关系补充 Chunk、6 次精确定位补充 Chunk；这些为 action 级累计，不是全局唯一命中数量。图查询错误 0，图增强 P95 约 58ms。

首次 A/B 暴露图补充候选挤占后续原始候选的问题，已修复并加入小 ContextBundle 回归用例。工作流评测器同时修正了旧 `AnswerOptions(None, "legacy")` 构造方式，使实际评测兼容当前单参数接口。

当前真实案例组证明未回归及延迟目标达标，尚未证明总体业务质量提升；未执行实时 LLM Coverage 或 Fast/Full 正文质量 A/B。保持默认关闭，后续启用应基于更多跨文件关系 Gold 和业务评测，不能只看新增证据数量。

### 本地产物

- `artifacts/symbol-graph-v1-report.json`：固定候选与受控 Controller 的完整 Graph 对照。
- `artifacts/symbol-graph-real-workflow-ab.json`：真实检索工作流 OFF/ON，以及质量/耗时比较。
- `artifacts/java-chunks-diagnostics.json`：真实仓库逐条解析缺口与统计。
- `artifacts/java-chunks-symbols.jsonl`、`artifacts/java-chunks-edges.jsonl`：本次 Java 分析符号与关系。

自动测试与评测均保留既有冻结基线文件。本次历史 baseline 比较因数据集摘要不同标记为 incompatible，不覆盖旧基线；本次 OFF/ON 对照采用同一当前数据集。
