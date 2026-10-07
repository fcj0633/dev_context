# V5 Agent 执行流优化：实现与验收

日期：2026-10-08。开发分支：`codex/v5-evidence-loop`。

依据 `06-agent执行流优化计划.md` 及 26 条补充约束实现。交付采用一个分支、按依赖分拆逻辑 commits；不按天分阶段，不扩展现有预算。

## 1. 当前交付与启用结论

已实现不可变 v3 取证计划、共享结构证据、限定端点的批量 metadata hydration、统一 CombinedCoverage、决策候选与双层校验、需求级 Graph 权限、关系进展和失败状态，以及 V5 三组基线和默认启用门槛。

当前 **不允许默认开启 Agent**。`Settings.tool_agent_enabled` 和 `.env.example` 仍为 false；`symbol_graph_enabled` 仍为 false。现有用户显式环境配置继续优先。回退 `TOOL_AGENT_ENABLED=false` 后走 Fixed＋structural metadata，结构校验继续生效。

已完成的真实生产规划最新一轮为 **28/28 通过**。Python **747 passed、11 skipped**；Java parser **6 passed**。PostgreSQL 无法连接，显式数据库集成尝试失败。144 次 semantic、72 次 Oracle 和 12 次回答验收因此尚未执行，不报告覆盖提升、False READY 改善或默认启用达标。

## 2. 执行链与代码位置

```text
EvidencePlan 统一入口归一化 v2 → 不可变 v3 RetrievalNeed
  → Fixed / AutoGraph / ToolAgent
  → Workspace Chunk / Symbol / Relation / ownership / provenance / Probe
  → source deterministic check
  → structural check
  → 单批 semantic CoverageChecker / OracleChecker
  → 更新进展、补证或停止
  → 冻结原有 Chunk EvidencePackage
  → Fast / Full Writer 与现有 Citation
```

| 模块 | 入口/职责 |
|---|---|
| `planning/retrieval_need.py` | 四类不可变 need，冻结嵌套关系/路径契约，五类物理边校验，生产规划提示 |
| `planning/evidence_models.py` | 唯一内部计划语义；旧调用在构造入口归一化，输出始终 v3 |
| `planning/evidence_planner.py`、`agentic/fast.py` | 生产 Full/Fast 解析、v3 输出；无效响应仍安全 fallback |
| `context/relations.py`、`context/workspace.py` | 已确认实体、物理关系、独立归属/来源、Probe、路径；轮次隔离与冻结 |
| `code_graph/store.py`、`code_graph/hydration.py` | 按需求合并 edge types；单次 induced-relations 查询只读取已取得端点之间的关系 |
| `agentic/structural_coverage.py` | 来源→结构→语义；结构证明与断链/方向/端点/Probe 状态验证 |
| `tool_agent/policy.py` | 确定性候选、DecisionPolicyValidator、路径下一段候选推导 |
| `tool_agent/observation.py`、`runtime.py`、`structure.py` | 需求级已确认 key 快照；执行前拦截与一次 fallback；结构入库、进展、终止诊断 |
| `evaluation/tool_agent_runner.py` | 新 paired A/B/C；共享不可变计划、独立 Chunk/Relation Gold、质量与严格相对门槛 |
| `evaluation/v5_acceptance.py`、`v5_fingerprint.py` | 汇总生产、检索、回答、测试证据；配置/语料一致性；缺项即禁止启用 |
| `scripts/run_v5_validation.py` | 真实生产规划→数据库先决检查→semantic→Oracle→回答→启用判定；新目录保存每轮原始记录 |

以上路径相对于 `src/devcontext`，脚本路径相对于项目根目录。

## 3. 数据契约与关键行为

### 3.1 不可变计划

`EvidencePlan`、`EvidenceRequirement`、`RetrievalNeed` 和嵌套 spec/segment 均为 frozen dataclass；需求及路径段使用 tuple。v2 仅在统一入口兼容一次，运行消费者拒绝非 v3。运行确认不会写回计划。

单关系使用 `relation_spec`，连续路径使用 `path_spec`，最多两段。CALL_CHAIN 只为路径模式。谁调用是 CALLS/INCOMING，类型实现是 IMPLEMENTS/INCOMING，方法实现是 OVERRIDES/INCOMING。显式构造使用 CONSTRUCTS。普通解释、设计说明和一般“流程”不自动变成路径。

### 3.2 Workspace 与 metadata

Relation 保存真实物理 source/target、解析可靠性、源码位置及端点 Chunk 映射。反向遍历只体现在 ConfirmedPath.directions。关系本体按仓库、source、target、edge_type、源码位置去重，ownership 和 provenance 分开保存。

Symbol/Chunk/Relation/ownership/Probe 按需求和轮次可见，冻结后拒绝所有公开写入。新 Relation、新 requirement ownership 计进展；重复本体和新增 provenance 不计进展。

metadata hydration 按 Requirement 批量合并需要的边类型，不逐 Symbol/need 查询。SQL 同时限制 source 与 target 为已取得端点；不会取得邻居正文或给 EvidencePool 新增候选。相同已完成范围可复用，新增 Symbol 后补查。

Probe 保存 need 索引、Symbol 集合、边与方向过滤、查询范围、轮次、返回关系、截断和错误。NOT_QUERIED、COMPLETED、TIMEOUT、INDEX_UNAVAILABLE、FAILED、DEADLINE 独立保存；范围不匹配或截断的 Probe 不证明查询已完成。已有完整可靠关系不被无关失败撤销。

### 3.3 Coverage 与决策

所有模式使用 CombinedCoverage。来源不足或结构未通过的需求不进入语义模型；通过的需求保持一次批量语义检查。Fast 不再走旧快捷 Coverage 路径。

RELATION/PATH 必须拥有匹配的需求归属、已确认端点、可靠关系和所需正文。方法端点须为 METHOD Chunk，OVERRIDES 实现端须包含实现正文。反向边、错误类型、断链、其他需求的关系、候选及 hint 都不能补证。

无直接证据 MISSING；已有正文、完成所需范围但尚未证明关系或缺少端点 PARTIAL；已有正文但未查/超时/索引不可用/失败为 UNVERIFIED。SATISFIED 要求来源、结构、语义均通过，全部 CORE SATISFIED 才 READY。缺边只能解释为当前证据未证明，不能推断运行时关系不存在。

Planner 的工具选择必须属于当前需求候选集合；Graph key 必须在本步观察中已确认并属于当前需求。整步先校验再执行；本步新 key 下一步才能使用。首次策略/契约失败进入 deterministic fallback，再失败停止；被拒绝批次不计实际调用预算，也不计 Executor invalid-call。Executor 仍保留工具、参数、来源、confirmed-symbol 第二层校验。

完整批次和 Coverage 后再判断进展。取证前 Planner 失败为 RETRIEVAL_FAILED；完成查询但无正文为 EMPTY；已有正文的 Deadline/失败为 PARTIAL。Fixed 仍两轮，Agent 仍 3 steps / 8 calls / 每步 3 calls。

## 4. 补充约束落实检查

| 约束编号 | 落实方式 |
|---|---|
| 1–3 | 不可变计划、统一 v2→v3 入口、独立 RELATION/PATH 字段及物理边枚举 |
| 4–6 | 仅 CONFIRMED＋可靠索引可证明；物理方向不变；Probe 区分未查/失败/正常空结果 |
| 7–9 | 限定已有端点、按需求批量/按 need 过滤；Graph 开关只控制自动扩图，配置说明同步 |
| 10–11 | 本体/ownership/provenance 独立；需求级 confirmed key 权限 |
| 12–14 | DecisionPolicyValidator＋一次 fallback、独立策略指标；Executor 校验保留；无 step 内绑定 |
| 15–18 | source→structural→semantic；统一 CombinedCoverage；关系/路径与端点 METHOD；状态不推断运行时不存在 |
| 19–22 | 新关系/归属进展；失败包状态；预算保持；Writer/Citation 仍只消费 Chunk |
| 23–26 | 新 A/B/C、严格相对不退化、生产正负例硬门槛、单分支逻辑 commits |

## 5. 已执行验证

| 验证 | 实际结果 | 证据 |
|---|---|---|
| 全量 Python | 747 passed、11 skipped，跳过项依赖 PostgreSQL | `artifacts/v5/python-delivery.txt` |
| Java parser Maven | 6 tests，0 failures/errors | `java-parser/target/surefire-reports/TEST-*.xml` |
| PostgreSQL 专项实际尝试 | ConnectionTimeout；1 error，不视为通过 | `artifacts/v5/postgres-attempt.txt` |
| 生产模型第 1 轮 | 25/28；下游、构造、连续路径分类失败 | `artifacts/v5/live-01/production-planners.json` |
| 修改分类提示后的第 2 轮 | 27/28；一般概念负例 fallback | `artifacts/v5/live-02/production-planners.json` |
| 补充非结构问题计划契约后的第 3 轮 | **28/28**；20 正例、8 负例，全部 native v3 且不可变 | `artifacts/v5/live-03/production-planners.json` |
| semantic 新三组完整对照 | 尚未执行：数据库阻塞 | `artifacts/v5/live-03/external-blocker.json` |
| Oracle 新三组完整对照 | 尚未执行：数据库阻塞 | 同上 |
| Fast/Full 12 个回答场景 | 尚未执行：数据库阻塞 | 同上 |

生产使用当前 `deepseek-flash` 配置；Fast/Full 各十类正例、四类负例。保留前两轮失败，不拼接有利结果；第三轮重新运行完整 28 项。单次一般概念诊断不计入门槛。报告保存解析计划；第三轮还保存原始响应和拒绝原因。

新增确定性测试覆盖冻结、统一版本入口、边类型、需求隔离、轮次隔离、物理方向、断链、实现正文、批量端点查询、Probe 状态、语义零调用、策略提前拒绝、fallback/预算、进展和停止。既有测试保留；工作区原有无关改动未重置或纳入提交。

最终检查还修正了旧格式适配的一项过度推断：“定位令牌桶实现类和 Redis 数据结构”属于 CODE 正文需求，不能仅凭“实现类”生成 IMPLEMENTS，并错误地把 Redis 当作接口锚点。对应 V5 regression need 已修正，案例和独立正文 Gold 保留；显式接口/方法实现仍要求真实关系。

数据库 localhost:5432 无法连接。尝试启动已安装的 Docker Desktop 后，其 backend 日志显示 Secrets Engine 无法访问/重命名 `docker-secrets-engine/engine.sock`。未重置 Docker、删除数据卷或修改用户数据库来绕过此问题。

可追溯交付摘要与生产原始报告同时保存在 `benchmark/baselines/v5-delivery/`。历史产物只按 `v5-historical-manifest.json` 冻结归档，不与新基线百分比直接比较。

## 6. 新基线与剩余启用验收

A=Fixed＋structural metadata，B=AutoGraph＋structural metadata，C=ToolAgent＋structural metadata。semantic 为 16 案例×3 次×3 组=144 次；Oracle 为 24 案例×1 次×3 组=72 次，Agent 决策仍用真实模型。每个 paired run 三组使用同一个不可变 v3 plan，保存 plan hash，轮换运行顺序，保持 top-k、上下文与 Coverage 配置一致。

计划与独立 Gold 分离；Chunk 命中和 requirement-owned 物理关系共同评分，False READY 按独立 Gold 判断。报告含评分/语料/计划/模型配置哈希、工具与策略指标、结构证据、Probe、Token、阶段耗时。不同模型配置、语料或 top-k 的报告不能合并通过启用判定。

默认开启必须同时满足：C semantic CORE >90%、完整命中 >85%、False READY ≤5/48；C Oracle CORE ≥95%；semantic 和 Oracle 的 C CORE/完整命中均不低于 B、False READY 不高于 B；执行错误 0、Executor invalid-call <5%、平均实际调用 ≤4、P95 steps ≤3；结构/预算/停止/恢复测试、生产规划正负例、数据库测试和 Fast/Full 回归均通过。

当前只有生产规划与非数据库测试有通过证据，不能据此改默认值。恢复数据库与索引后，运行完整新一轮：

```powershell
.venv/Scripts/python.exe -m pytest -ra
$env:DEVCONTEXT_RUN_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest tests/test_storage_integration.py tests/test_code_graph_integration.py tests/test_tool_agent_integration.py tests/test_v5_postgres.py -ra
Remove-Item Env:DEVCONTEXT_RUN_INTEGRATION
mvn -q -f java-parser/pom.xml test
# checks.json 只写实际执行结论；数据库未通过不得写 true。
.venv/Scripts/python.exe scripts/run_v5_validation.py --output artifacts/v5/live-next --checks artifacts/v5/checks.json
```

`checks.json` 需要真实的 python/postgresql/java/structure/policy 布尔结果。当前交付文件的 postgresql=false，因此安全阻止开启。脚本不自动修改默认值；所有门槛通过后才可另作逻辑 commit 修改 Settings 和 `.env.example`，再进行启用冒烟。模型配置变化后重新运行整个配对评测，不扩大预算、不降低结构要求、不删除失败案例。
