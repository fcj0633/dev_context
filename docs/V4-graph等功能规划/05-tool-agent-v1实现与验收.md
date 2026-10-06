# Tool-driven Retrieval Agent V1 实现与验收

开发分支：main。基础提交：73d56bf（Fast/Full + Symbol Graph V1）。

## 实现边界

新增 ToolDrivenRetrievalController、RetrievalEngine 协议与 tool_agent 包。固定两轮 RetrievalController 保留，开关关闭时行为不变。EvidencePlanner 定义待证明事实，Agent 选择取证工具，独立 CoverageChecker 判断覆盖，EvidenceWorkspace 保存真实证据，Fast/Full 继续消费 EvidencePackage。

七个工具：search_code、search_docs、find_symbol、find_callers、find_callees、find_implementations、find_hierarchy。search_code 复用 stage-aware 策略；DOCUMENT 搜索和全部证据归类复用现有策略。find_callees 分别返回 CALLS 和 CONSTRUCTS。关系方向保存真实 source/target；incoming 查询不会颠倒实际边。Graph 缺边只表示当前静态索引没有可靠关系，不证明不存在运行时调用。

不加入文件读取、Shell、执行、编辑、Git、长期 Memory、多 Agent、LangGraph、动态工具或 Agent SQL。

## 六项约束的落地

1. 新流程和三组 A/B/C 都使用标准独立 CoverageChecker，不引入另一套 Fast Coverage 设计。配对评测通过同一 factory 创建相同配置的新 Checker；semantic 统一 max_attempts=1，oracle 统一 Ground Truth。
2. SymbolObservation 明确 CONFIRMED/CANDIDATE。只有唯一可靠定位、实际检索 Chunk 映射或真实关系发现的 Symbol 可以确认；find_symbol 歧义时没有 evidence_results，也不增加 confirmed 集合。Graph Executor 必须验证 key 出现在本步 Observation 的 confirmed_keys 中。
3. 完整 ActionStep 的工具执行、证据入库和一次 Coverage 结束后才判断 NO_PROGRESS。一个工具返回重复证据，不影响同一步其他工具继续执行。进展包括新 Chunk、新 requirement–Chunk 归属、新确认 Symbol、覆盖改善。总 deadline 到达可立即中断批次。
4. package_state 增加可选 ToolExecutionSummary（attempted/completed/failed）。Agent 以全部工具结果判定失败；有证据时继续根据覆盖得出 PARTIAL/READY。Search 失败、Graph/Symbol 成功不会错误地得到 RETRIEVAL_FAILED。固定检索不传摘要时兼容原行为。
5. paired run 在构造三组控制器之前生成一次不可变 EvidencePlan，经 FixedEvidencePlanner 原样返回；每组检查对象身份和 plan hash。三组交换执行顺序，记录语料 hash，语料变化则拒绝对比。
6. 原 neighbors() 保持原代码与去重语义。新增 tool_relations()，每节点限制不同邻居数量，保留所选邻居上的不同 edge_type/direction，同一种关系的重复调用位置采用最早位置。不会把整个 frontier 放进一个全局 LIMIT。

## 运行和恢复

固定上限：MAX_STEPS=3、MAX_TOOL_CALLS=8、MAX_CALLS_PER_STEP=3；Graph 每节点最多五邻居，hierarchy 最多两跳，其余关系一跳。工具参数不能控制预算或 SQL。依赖本步新 Symbol 的工具应安排到下一步，同步批次内的未确认 key 会被拒绝。

Planner 使用严格 JSON；Observation 保存需求、缺口、最多八个优先相关 confirmed Symbol/需求、歧义候选、三步摘要和预算，输入限制 8,000 估计 token。Memory 保存 metadata，不复制证据正文。ToolResult 进入 Memory/ActionStep 时剥离正文，保留 ID、Observation、错误和新增计数。

首次 Planner JSON/契约失败或可恢复传输错误采用确定性 fallback；再次失败停止。客户端配置错误、认证错误、AnalysisContractError 和程序不变量错误不吞成普通工具 EMPTY。查询 embedding 的传输失败有独立错误类型；不可恢复配置/维度错误仍抛出。

一整步无进展且包含可恢复错误时，全请求最多一次换工具机会，仍受三步/八次限制；同一调用不能原样重复。歧义、工具超时和无索引可以换工具；重复成功结果且无其他进展立即停止。Deadline 保留已取得证据。

没有 Symbol 索引时返回 TOOL_UNAVAILABLE；有索引但名称未匹配为 SYMBOL_NOT_FOUND/EMPTY。缺失关系属于数据缺口，内部分析契约违反属于致命错误。

## 配置与诊断

- TOOL_AGENT_ENABLED=false：默认固定检索；true 时 Fast/Full 都选择工具检索。
- TOOL_AGENT_PLANNER_TIMEOUT_SECONDS=60：Planner 单次超时，受显式请求 deadline 约束。
- SYMBOL_GRAPH_ENABLED：仅固定检索自动扩图开关，Agent 模式不调用自动 expander。
- SYMBOL_GRAPH_QUERY_TIMEOUT_SECONDS=2：图查询安全超时；100ms 为 P95 目标，不是提前跳过门槛。

Trace 在 Agent 模式新增 agent_retrieval；固定模式不输出空字段。记录步骤、工具参数、选择理由、状态、真实关系、新证据、新 Symbol、覆盖变化和停止原因。StageUsage 只计父阶段，步骤和工具耗时为子诊断，不重复累加。--debug 提供逐步可读输出。

## 验证与评测口径

benchmark/symbol-graph-business-v1.jsonl 冻结 12 个实际业务关系场景，核对了源码路径、锚点和目标，涵盖 Controller→Service、接口实现、caller、callee、两跳接口链、继承和构造关系。

benchmark/tool-agent-v1.jsonl 包含 16 个场景，增加精确定位效率、跨模块同名 BaseDO 歧义、缺失 Symbol 停止。纯 fixture 测试另外模拟临时服务失败和整步重复结果，避免以线上偶然故障作为测试前提。

evaluate-tool-agent 三组共享每次配对的冻结 EvidencePlan、同一语料、搜索 top-k 和最终 Context 预算。semantic 使用真实搜索/Planner/Coverage；oracle 用确定的覆盖判断，固定组 Query 冻结，Agent Planner 保持真实 LLM。oracle 不能用于声称真实 Coverage 判断准确。

Full-case Success 为全部可满足证据组命中；没有可满足组的 negative case 须实际包状态匹配预期。False READY 对独立 Ground Truth 和完整 Workspace 检查，不能仅与实际 Checker 的判断自比较。工具选择用允许的有效动作序列，失败/非法动作不计为有效序列。未发生的歧义恢复和缺失 usage 显示 null。

受控 Oracle-seeded 对比额外固定锚点候选和工具序列，验证工具、关系映射、恢复和边界。它不能证明真实模型会选择正确工具，不能与真实检索 A/B 混为一谈。

默认启用候选门槛：CORE 覆盖不低于自动 Graph 组，False READY 不增加，Graph-sensitive Full-case Success 至少增加 10 个百分点，非法调用率 <5%，平均调用数 ≤4，P95 步数 ≤3，停止测试通过。性能目标只观测，不改为硬截止。首版默认不开启。

## 回答兼容修复

端到端回归发现 Full 蓝图允许 critical_distinctions 缺省 claim_ids，但证据组织器直接读取导致 KeyError。契约层现在将可选引用数组归一化为空数组；没有支持性 claim 的 distinction 不进入 Writer 包。此修复不改变 Fast/Full 的回答架构或流式协议。

已发布部分章节之后的协议故障仍返回 partial，不在同一次请求里重放已发布正文。回答回归结果同时检查 generation status，不能仅根据 Python 没有抛异常声称成功。

## 实测结果

真实 semantic A/B/C：16 个问题各三次 paired run，总计 144 次检索。三组 Checker 配置完全一致，max_attempts=1。CORE 覆盖按可满足 CORE 统计；False READY 用独立 Ground Truth 对完整 Workspace 检查。

| 指标 | 固定检索 A | 自动 Graph B | Tool Agent C |
|---|---:|---:|---:|
| CORE Coverage | 77.78% | 93.33% | 75.56% |
| Evidence Recall | 88.10% | 96.43% | 86.90% |
| Full-case Success | 72.92% | 87.50% | 70.83% |
| False READY | 8 | 3 | 11 |
| 执行错误 | 0 | 0 | 0 |
| 检索 P95 | 21.22s | 11.02s | 13.24s |

Agent 平均 2.10 次工具调用、P95 调用数 3、P95 步数 2；非法调用率 0.99%，允许有效工具序列命中率 31.25%。48 次 Agent 请求共四次有效显式 Graph 调用，Graph 工具 P95 55.06ms；发生歧义时确认恢复率 62.5%。停止案例正确停止。

这说明工具基础设施和预算有效，但真实模型经常通过 Symbol/搜索取证后由 Coverage 过早判为充分；目标 Chunk 尚未进入 Workspace 时也会产生 READY。Graph 使用次数少，三步调用链能力尚未充分展示。不能把低非法调用率或快速停止当成证据充分性认证。

24 个 L1.5+regression paired run 使用完全相同 Oracle Checker，固定组冻结 Query、Agent 保留真实 Planner，共 72 次检索。A/B CORE Coverage 均为 100%，C 为 84.62%；A/B Full-case Success 83.33%，C 为 62.50%，三组 False READY/执行错误均为零。四个可满足 CORE 缺口主要出现在后续关键词搜索仍使用泛业务表达，以及跨来源证据补齐不足。该结果是取证回归，不证明语义 Coverage 的准确性。

受控 Oracle-seeded 16 场景：Agent CORE Coverage、Evidence Recall、Full-case Success 和脚本序列命中率均为 100%，无 False READY、无非法调用；平均 1.875 次调用，Graph P95 53.10ms；歧义候选重新确认和 NO_PROGRESS 停止通过。这只证明给定正确动作时工具链可执行，不能抵消真实 Agent 的退化。

因此未达到默认开启门槛，TOOL_AGENT_ENABLED 继续 false。后续应优先改善取证缺口描述、Planner 对真实 Symbol 的复用及独立 Coverage 判断，再按同一 paired 协议复测，不能扩大循环或以受控结果替换真实结果。

原始明细：artifacts/tool-agent-live-abc.json、artifacts/tool-agent-regression-abc.json、artifacts/tool-agent-controlled.json。scoring_version=2 统一 negative case 成功口径、有效动作序列和独立 False READY 检查；对保存的相同证据离线重评分，没有追加或替换模型响应。语料 hash 保持一致。

最终全量 Python 测试含 PostgreSQL 集成：694 passed。Graph 查询、逐节点限制、多 Edge 类型、实际歧义候选、CONSTRUCTS、Deadline、来源权限、BOTH、UNVERIFIED、整步进展、三步/八次预算、冻结计划配对和 embedding 错误分类均有验证。

回答回归覆盖定位、纯文档、代码+文档、接口实现、调用链、部分证据六场景。首轮 Fast 六场景均 complete；Full 首轮四场景 complete，一场景触发预存 claim_ids KeyError，一场景在已发布正文后发生章节协议错误并正确返回 partial。修复引用字段归一化后，Full 六场景重新运行均 complete、没有非法引用，仍保持部分证据场景的 PARTIAL 包状态。原始失败未删除，分别保存在 artifacts/tool-agent-answer-regression.json 与 artifacts/tool-agent-full-after-fix.json。这是端到端兼容回归，没有独立回答质量 Judge；不能据此声称事实完全正确。
