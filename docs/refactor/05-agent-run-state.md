# Agent Runtime State 与 Working Memory 重构

日期：2026-10-21（Asia/Shanghai）。行为基线：`44cc833a9d7a2ce9a9972cf7fe7f52f5eaf6bee9`。

## 目标与结果

一次请求通过 AgentRunState 组织任务进度、工作记忆、执行历史、控制信息和证据资源。Runtime 负责调度，state_updates 负责状态变更，Planner 只读取不可变上下文。没有重新实现检索算法，也没有引入状态持久化、LangGraph、跨请求记忆或额外模型调用。

旧 AgentMemory 和 outcome.memory 已一次迁移并移除，不提供兼容对象。对外 RetrievalOutcome、EvidencePackage、agent_retrieval Trace、搜索历史、CoverageRound 和阶段统计维持原协议。

## 最终结构与权威来源

```text
AgentRunState
├─ query / plan                 本次问题 / 不可变 EvidencePlan v3
├─ task.coverage                当前需求覆盖结果
├─ memory
│  ├─ available_symbols         规划用的确认符号索引
│  ├─ symbol_requirements       需求级符号归属索引
│  └─ candidates               尚未确认的符号线索
├─ history
│  ├─ steps / tool_history      Step 与工具调用记录
│  ├─ coverage_rounds          每轮覆盖快照
│  ├─ search_history           代码、文档搜索动作
│  └─ stages                   调用阶段与使用量
├─ control
│  ├─ planner_failures / planner_diagnostics
│  ├─ policy_violations / policy_fallbacks
│  └─ recovery_used / stop_reason
├─ workspace                   同一个 EvidenceWorkspace 引用
└─ pool                        同一个 EvidencePool 引用
```

| 数据 | 权威来源与更新规则 |
|---|---|
| 需求、成功标准、优先级 | 不可变 EvidencePlan；Planner 不得改写 |
| 当前覆盖 | TaskProgress；完整 Checker 返回值统一替换 |
| 正文、引用、符号身份、物理关系及归属 | EvidenceWorkspace；仍由现有 ingest 和结构验证接口维护 |
| 可供后续规划使用的符号 | WorkingMemory；工具确认后更新，Coverage hydration 后再次同步 |
| 工具与 Step 事实 | ExecutionHistory；工具记录不持有正文 |
| 失败预算、诊断、恢复、停止 | RuntimeControl；仅通过状态更新入口修改 |
| 最终 Context 的排序候选 | EvidencePool；保留原有选择逻辑，不作为结构事实来源 |

WorkingMemory 是规划索引，不代替 Workspace。每个确认符号及其需求归属先经 Workspace 校验，再进入工作记忆。Candidate 不产生 Graph 权限；工具确认会按原行为移除同 key 的候选。Coverage 后同步保持原候选处理时机及符号插入顺序。

Pool 继续持有既有 EvidenceCandidate/SearchResult 引用，这是现有 Context 选择协议的必要结构。本次不将其替换为新的索引，也不深拷贝完整 Workspace。

## 字段迁移与接口

| 旧位置 | 新位置 |
|---|---|
| AgentMemory.coverage | state.task.coverage |
| available_symbols / symbol_requirements / candidates | state.memory 对应字段 |
| steps / tool_history | state.history 对应字段 |
| Runtime 局部 rounds / history / stages | state.history.coverage_rounds / search_history / stages |
| 失败计数、诊断、违规、恢复标志、停止原因 | state.control 对应字段 |
| AgentMemory.evidence_ids | 删除；通过 workspace.for_requirement() 派生 |
| AgentMemory.errors | 删除可变副本；history.errors 为派生 tuple |
| AgentMemory.structural_metadata | 删除；Trace 从最终 Workspace 派生 |

AgentRuntime.run(query, plan, workspace, pool) 保留入口形式，并创建全新的请求状态。RuntimeOutcome 仅保存 state 和总耗时；coverage_rounds、search_history、stages 属性从同一个 History 派生 tuple，不维护第二份可变集合。

build_observation(state, registry) 保留原 JSON 字段、符号选择顺序、约 8000 token 预算和裁剪策略。Observation 是本轮模型可见的能力边界，不是 State 的完整序列化。

AgentPlanner.plan(view, context, step) 只接收 PlannerContext。该 frozen 数据类包含 Plan、Coverage、符号与需求归属的只读副本，以及工具历史快照。映射使用 MappingProxyType，归属集合使用 frozenset，历史调用参数递归冻结，包括 Executor 曾拒绝的嵌套非法参数；fingerprint 转回等价 JSON 形状后比较。Context 不含 Workspace、RuntimeControl 或资源客户端。

PolicyValidator 接收本轮 Observation 和不可变工具历史。Executor 的契约及参数校验不变。State 内存在某个符号，不代表该需求本轮的 Observation 暴露了它；同轮发现也不能授予本轮 Graph 权限。

## 请求生命周期与修改边界

1. Controller 创建 Plan、Workspace 和 Pool。create_run_state() 初始化每项需求为 MISSING，所有容器独立分配。
2. begin_step() 保存当前 Coverage、确认符号 key 和 Workspace 的不可变进展标识集合；不读取正文副本。
3. Runtime 构造 Observation、只读 PlannerContext，调用 Planner 并校验整批决策。
4. Executor 执行工具。apply_tool_result() 依次完成 SourcePolicy 分类、Pool 更新、记录旧归属、正文 ingest、结构 ingest、增量计算、WorkingMemory 更新和元数据历史追加。
5. 每次应用工具结果后，保留原有 record_progress 调用，使用当时尚未更新的 Coverage；搜索动作随后进入搜索历史。工具 Deadline 的结果仍先入库及记录，再结束该批次。
6. 整批工具结束后最多调用一次 CombinedCoverage，保留 source → hydrate/structural → semantic → endpoint alignment。完整返回后才替换 TaskProgress；抛出 Deadline 则保留旧 Coverage 和已取得证据。
7. 同步 Workspace 当前轮可见的符号，包括 Checker/hydrator 补充的符号。finish_step() 记录 CoverageRound 和不可变 ActionStep。
8. Runtime 按原优先级决定继续、恢复或停止。Controller 仍按原顺序构建 Context、通知 Observer、判定 package state、冻结同一个 Workspace 并封装结果。

state_updates 不增加事务回滚或错误吞并。契约错误、编程错误、不可重试模型错误及配置错误继续按原行为传播。

### 规划失败预算

Planner 将可恢复的响应或服务失败转换为 RecoverablePlanningFailure，保存原错误文本和响应；不写入共享 State。Runtime 记录诊断、标记浪费的模型调用并增加共享失败计数，第一次使用原 deterministic fallback，超过一次失败则停止为 PLANNER_FAILED。

Policy 违规使用同一个失败计数，policy_fallbacks 仅统计原先的策略 fallback。fallback 再次违规时记录 source=fallback 并终止，不重复增加失败计数。无模型 factory 的正常 fallback 不记为失败。直接抛出的 PlannerFailed、非重试异常和工厂配置异常仍保持原分支。

### Progress 与终止

Workspace.progress_snapshot() 提供正文 ID、正文归属、关系身份和关系归属的 frozenset。Runtime 不再读取 Workspace 的私有关系字段。

新正文、新确认符号、新正文归属、新关系、新关系归属或 Coverage 等级提升均算进展。MISSING/UNVERIFIED=0、PARTIAL=1、SATISFIED=2。只增加 provenance、Probe 或重复结果不算进展。正文和符号输出保持原插入顺序，正文归属增量排序。

终止顺序保持：已有终止原因 → CORE 全满足 → 调用预算耗尽 → 无进展与恢复判断 → 轮数耗尽。3 Steps、8 ToolCalls、每步最多 3 次调用、一次无进展恢复，以及共享规划失败预算保持不变。

## 行为等价证据与验证

在修改生产代码前，使用原实现运行 62 项受控测试，保存其中 16 个测试、17 次 Runtime 执行的完整确定性记录。另从仍保持基线的旧工作区源码生成 8 个 Controller 场景，覆盖 legacy/explain/teach、连续两次模型失败、模型与 Policy 交叉失败、Coverage Deadline 和 NO_USEFUL_TOOL。

固定期望位于 tests/fixtures/agent-state-44cc833.json 和 agent-state-controller-44cc833.json。测试只读取基线，不提供覆盖生成开关。比较 Observation、模型消息、调用顺序、Coverage、Progress、停止原因、正文及归属、结构证据、Context/EvidencePackage、Trace 和阶段统计。仅归一化 latency_ms、total_agent_ms、elapsed_ms 数值，保留数值类型标记；token、调用计数和字段结构完整比较。

新增状态测试覆盖可变容器隔离、不可变 PlannerContext、非法嵌套参数、候选与跨需求权限、token 裁剪、元数据历史、六类进展、provenance 不算进展、Coverage 超时及 hydration 同步、逐工具诊断、冻结限制、轮数上限及 READY 优先于调用预算耗尽。

| 验证 | 结果 |
|---|---|
| 全套非数据库 Python 回归 | 999 项通过，包含 24 项新状态测试、8 项新 Controller 等价测试，以及既有测试中的 17 次 Runtime 基线比较 |
| 独立 PostgreSQL 图、工具及 Runtime 集成 | 11 项通过；其中新增 1 项真实符号定位 → 图查询 → State/Coverage 贯通用例 |
| 前端单元测试 | 3 个文件、11 项通过 |
| 前端类型检查与生产构建 | 通过 |
| 浏览器端到端测试 | 16 项通过，51.5 秒；独立 8001 测试接口 |
| 本次差异格式检查 | git diff --check 通过 |

数据库验证使用新建的独立临时容器 devcontext-agent-state-test-20261021，镜像 pgvector/pgvector:0.8.6-pg18-bookworm、独立数据库 devcontext_agent_state_test、随机分配的本机端口。没有使用或改写用户知识库，测试无需真实 LLM 或 Embedding API。测试完成后停止该临时容器，--rm 清理其临时数据。

test_storage_integration.py 的既有就绪检查要求预先索引的实际业务仓库及指定业务方法，独立测试库没有该资料。本次没有导入用户业务数据或制造替代业务记录，故该项未验证，不计为通过。非数据库全套运行中其余数据库测试跳过，随后在临时库单独执行上表的 11 项。

确定性场景没有观察到业务行为变化；未使用真实模型随机输出来宣称逐字等价，也没有新增在线模型调用、提示词、token 预算或工具预算。

### 复现

在项目根目录设置 PYTHONPATH=src;tests、LLM_PROVIDER=deepseek、EMBEDDING_BYPASS_PROXY=false 后执行：

```powershell
.venv/Scripts/python.exe -m pytest -o addopts= -q -ra -m 'not integration'
.venv/Scripts/python.exe -m pytest tests/test_agent_state.py tests/test_agent_state_equivalence.py tests/test_tool_agent.py tests/test_v5_runtime.py tests/test_v5_structure.py tests/test_v5_semantic_alignment.py
npm test --prefix frontend
npm run build --prefix frontend
$env:PLAYWRIGHT_API_PORT = '8001'
npm run test:e2e --prefix frontend
```

数据库复现前新建隔离 pgvector 容器或确认独立测试库，设置 DATABASE_URL 指向它、DEVCONTEXT_RUN_INTEGRATION=1，再运行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_code_graph_integration.py tests/test_tool_agent_integration.py tests/test_v5_postgres.py -o addopts= -q -ra
```

## 后续 Checkpoint 边界

本次 State 是内存聚合对象，不承诺可直接 pickle 或作为完整 JSON 保存。未来 Checkpoint 需要单独定义版本化 DTO，导出 Plan、任务进度、控制信息、元数据历史与 Workspace 的证据/归属引用；恢复时重建可变索引、只读 PlannerContext 和资源对象。

Executor、客户端、Registry、Checker 和数据库连接仍由应用组装层创建。保存时机应限定在状态更新完成或完整 Step 结束，另行设计运行中工具调用、使用量与请求 Deadline 的恢复语义。本次没有实现这些功能。

提交仅包含重构代码、迁移与新增测试、固定基线和本说明。原工作区的历史未跟踪资料、trace.json、测试文件换行状态及本机配置均未加入。
