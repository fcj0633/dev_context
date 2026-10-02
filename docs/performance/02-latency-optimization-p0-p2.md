# P0–P2 回答耗时优化实施与验收报告

日期：2026-10-02

## 结论

P0–P2 的实现、单元测试、PostgreSQL 集成测试、Embedding smoke、Micro 性能样本、并发对照、L2 小样本 A/B 与 Retrieval benchmark 已完成。

代码层目标已经实现；性能层多数门禁达到，但候选配置尚不满足最终合并门禁：

- `locate-01` p50 为 14.8 秒，达到 20 秒目标；`locate-02` p50 为 24.9 秒，未达到目标。
- ExplanationPlanner 经 schema slimming 后平均 9.85 秒；修正 Writer 预算后的 detailed 三次样本平均 13.07 秒，达到 15 秒目标。
- corrected concurrency 3 的 teaching draft p50 为 37.7 秒，相对旧基线 174.7 秒下降 78.4%。
- detailed 总耗时 p50 从 353.2 秒降到 212.4 秒，下降 39.9%。
- section/revision Writer 的 `finish_reason=length` 在预算修正后的三次样本中为 0，失败章节为 0。
- L2 两个有效盲评样本中 teach 1 胜、1 负，胜或平率为 50%，低于 90% 门禁；主要问题是正文仍偏长、历史材料削弱了当前实现主线。

因此生产默认值保持安全配置：

```text
EXPLANATION_DEPTH_POLICY=legacy
TEACHING_SECTION_CONCURRENCY=1
```

候选实现仍可显式启用：

```text
EXPLANATION_DEPTH_POLICY=deterministic
TEACHING_SECTION_CONCURRENCY=3
```

没有因为部分性能目标达标而绕过 Locate 和质量门禁。

## 实施内容

### P0：并发安全 Trace V2

- LLM 调用改为 begin/finish 生命周期，在请求开始时由加锁 Recorder 原子分配 call ID。
- 正常、截断、HTTP/curl/timeout、无效 JSON、空正文与业务丢弃均能结束或精确标记同一条调用。
- 并发路径使用精确 `mark_call_wasted(call_id, reason)`，不再依赖“最后一次调用”。
- Trace 增加调用与章节 offset、parallel group、attempt、section call IDs、work/wall/span、峰值并发、parallelism ratio、逻辑串行深度和调度 waves。
- reasoning detail 为 optional：合法时记录 Provider 真值；缺失或异常时 `reasoning_tokens=null`，visible 回退为 completion total，请求不失败。
- 旧 27 样本共 315 次调用全部标为 detail unavailable，没有推算或伪造 reasoning token。

### P1：确定性 Depth、Section Budget、Locate 与输出预算

- Depth 优先级固定为：显式 depth → Locate → 详细提示 → standard；deep 仅允许显式选择。
- brief / standard / detailed / deep 的 preferred/hard max 固定为 1/2、3/4、5/6、7/8。
- evidence-backed capacity、target section、gap code/reason 与 unsupported IDs 全由程序计算。
- Planner 必须严格输出 target 数量且不超过 hard max；重复意图、重复 key points、无证据章节、无法反向绑定 Requirement、PROJECT_FACT 越界引用均被拒绝。
- Planner 验证失败不再次调用模型，精确标记原调用 wasted，并使用同一 target 的确定性 fallback。
- capacity=0 时不调用 ExplanationPlanner 或 Writer。
- Locate fast path 只绑定定位 Requirement 对应证据；无相关 Requirement 时不借用无关 CORE 证据。
- Planner 改为 slim schema：模型只决定教学结构，claim、confidence、depends_on、teaching device、gap 与 conflict 由程序从冻结 EvidencePackage 派生；旧完整 schema 仍兼容。
- Planner 上限保持 completion-total 20,224。该值来自旧成功样本 completion p95 16,144 × 1.25 后向上取 256 整数倍，旧样本无 length finish。
- Writer client 实际接收计算后的 `max_tokens`；`target_tokens` 同时传入 Prompt，明确是可见正文软目标。
- 首轮 64 个真实 high-effort section/revision 调用的 reasoning-token nearest-rank p95 为 4,135，向上取整为 4,352，作为 Writer reasoning reserve。修正后的真实 Writer 上限为 4,864～5,632，均记录在 LLM call trace。

### P2：Writer 并发安全、章节与 Revision 有界并行

- `write()` / `write_section()` 始终通过局部 `client` 发请求。
- fan-out worker 固定 `remember_last_client=False`；usage、call ID、finish reason 与错误不通过共享 `last_client` 传递。
- 每个 task 单独 `copy_context()`，Recorder 是唯一共享收集器且所有写入加锁。
- worker 只返回结果，主线程按原 section index 合并，Future 完成顺序不影响答案顺序。
- timeout、429、5xx、curl/连接错误最多重试一次；Citation、业务验证和配置错误不重试。
- 单节失败不取消其他节；partial answer 明确记录 failed IDs 并跳过 Reviewer/Revision；全部失败走 no-draft fallback。
- Revision 仅并发需要重写且 Context 有效的章节；失败保留原稿；stage 固定为 `teaching_revision`。
- sentinel `last_client`、交叉完成、独立 client、Context 传播、retry、partial 与 revision failure 均有自动化覆盖。

## 测试结果

### 自动化与真实依赖

- `uv run pytest`（`DEVCONTEXT_RUN_INTEGRATION=1`）：479 passed，0 failed，PostgreSQL integration 已实际执行。
- `uv run devcontext smoke-api`：成功；Embedding model=`text-embedding-v4`、dimensions=1024。
- 真实 Embedding 前只追加 `.aliyuncs.com,dashscope.aliyuncs.com` 等条目到当前进程 `NO_PROXY`，未覆盖用户原值。
- 真实 `devcontext search`：成功。

### Locate

| Case | 旧三次耗时 | 新三次耗时 | 新 p50 | 最大逻辑深度 | 结论 |
|---|---:|---:|---:|---:|---|
| locate-01 | 24.5 / 26.3 / 31.4s | 14.8 / 13.9 / 21.7s | 14.8s | 4 | 通过 |
| locate-02 | 45.1 / 65.5 / 36.1s | 19.8 / 24.9 / 36.8s | 24.9s | 6 | 未通过 20s 门禁 |

两个 case 都命中 deterministic `LOCATION_ONLY`，ExplanationPlanner 耗时为 0，最终只有一个定位章节。`locate-02` 第三次进入额外 Retrieval/Coverage round，瓶颈位于 EvidencePlanner、SearchAction/Coverage，而非 Locate presentation fast path；本轮明确不修改 Retrieval Round 1，因此没有越界优化。

### Planner schema slimming

`why-02` 在 deterministic、concurrency 3 下的同题三次结果：

| 版本 | Planner 三次 | 平均 | p50 | fallback |
|---|---:|---:|---:|---:|
| slimming 前 | 17.13 / 28.42 / 29.77s | 25.11s | 28.42s | 1/3 |
| slimming 后 | 9.39 / 12.59 / 7.58s | 9.85s | 9.39s | 0/3 |

平均耗时下降 60.8%，且没有新增 schema、section budget 或 fallback 错误。既然 high effort 已达到 15 秒门禁，本轮没有再把 Planner 切到 low，避免无必要的质量变量。

### Section 数稳定性

| Case | section count | 极差 | hard max 违规 |
|---|---:|---:|---:|
| why-02 | 3 / 3 / 3 | 0 | 0 |
| teach-token-bucket-01（预算修正后） | 5 / 5 / 4 | 1 | 0 |
| flow-01 | 5 / 5 / 4 | 1 | 0 |

`4` 节样本均有程序生成的 `INSUFFICIENT_EVIDENCE_REQUIREMENTS` 原因，而不是 Planner 自报或用空章节凑数。

### Writer 预算修正

首轮 live perf 暴露出 5 次 draft 和 1 次 revision 的 `finish_reason=length`。原因是 4,096 reasoning reserve 略低，且模型没有收到 section 的 visible target。

修正后对 `teach-token-bucket-01` 运行三次：

| 总耗时 | Planner | Teaching draft | Review | Revision | Sections | Failed IDs | length |
|---:|---:|---:|---:|---:|---:|---|---:|
| 116.4s | 10.0s | 36.2s | 34.8s | 0 | 5 | 无 | 0 |
| 212.4s | 17.2s | 44.2s | 54.7s | 41.3s | 5 | 无 | 0 |
| 234.3s | 12.0s | 37.7s | 71.1s | 40.3s | 4 | 无 | 0 |

17 个真实 section/revision Writer 调用全部正常 stop，Writer budget 确实传入 Provider；最终答案字符数从首轮约 8,083～10,771 降到约 5,981～6,647。

### 并发 1/2/3/4

下面的 c1/c2/c4 是同题单次 controlled 样本；c3 为预算修正后三次样本的 p50。由于 Reviewer/Revision 是否触发会显著影响 total，draft 与 section wall 更能反映 fan-out 本身。

| 并发 | Total | Teaching draft | Section wall | 失败章节 | Peak | 结论 |
|---:|---:|---:|---:|---|---:|---|
| 1 | 208.8s | 110.6s | 81.1s | 0 | 1 | 回归/debug 基线 |
| 2 | 107.8s | 55.6s | 55.6s | S5（旧预算 length） | 2 | 样本不稳定 |
| 3 | 212.4s p50 | 37.7s p50 | 33.8s p50 | 0/3 次运行 | 3 | 最佳候选 |
| 4 | 220.0s | 58.1s | 54.1s | 0 | 4 | 未比 c3 快 15%，且无性价比优势 |

c3 相对 c1 的 teaching draft p50 下降 65.9%，超过 40% 目标。c4 没有比 c3 再降低 15%，因此不会成为默认候选。c2 的失败发生在预算修正前，但现有样本仍不能证明它优于 c3。

### Detailed / Multi-pass 总延迟

| Case | 旧 total p50 | 新 total p50 | 下降 | 旧 draft p50 | 新 draft p50 | 下降 |
|---|---:|---:|---:|---:|---:|---:|
| teach-token-bucket-01 | 353.2s | 212.4s | 39.9% | 174.7s | 37.7s | 78.4% |
| flow-01 | 435.9s | 121.9s | 72.0% | 142.1s | 43.2s | 69.6% |

两题均超过 total 25% 与 draft 40% 的目标。旧流程全部串行，最大一次有 23 个 LLM calls；新 Trace 按 fan-out group 与 retry 计算的最大逻辑串行深度为 11。

### reasoning detail

- 旧 27 样本：0/315 可用，全部明确标记 unavailable。
- 本轮 `artifacts/perf-p0-p2`：224/224 可用，可用率 100%。
- Provider 返回合法 detail 时记录真实 reasoning 与 visible；缺失、null、异常或大于 completion total 时的回退行为均有自动化测试。

### L2 小样本质量

- `why-02`：盲评 winner=`teach`，teach 无 invalid citation、无 zero-valid-citation。
- `flow-01` 首次 baseline arm 遇到临时 DNS 失败，该结果不计入质量结论；网络恢复后单独复测，winner=`explain`。
- 两个有效盲评合计 teach 1 胜、1 负，胜或平率 50%，未达到 90%。
- `flow-01` 的 teach 教学评分为 4.714/5，但被判冗长、历史材料过多，并且“已有测试能证明什么”的回答不如 explain 直接。
- 两个 teach arm 都没有 invalid citation 或 zero-valid-citation；复测未发现新增 must-not-claim 违规。

因为小样本质量门禁已经失败，没有继续运行昂贵的 Natural 9×3 和完整 L2，也没有据此提升默认开关。

### Retrieval benchmark

36 case Retrieval benchmark 与既有 baseline 可比：

- hybrid recall@5：0.4722 → 0.4722，无回归。
- hybrid MRR：0.3764 → 0.3845。
- hybrid 平均耗时：956.7ms → 875.1ms。
- router accuracy：1.0；policy improvement checks 全部通过。

报告中两个历史绝对阈值（legacy recall@5=1.0、mixed both-sources@5=0.8）仍未通过，但 baseline 本身同样未通过；本轮没有修改 benchmark、acceptance threshold、检索策略或索引实现。

## 最终门禁状态

| 门禁 | 状态 | 说明 |
|---|---|---|
| 全部 pytest | 通过 | 479 passed，含 PostgreSQL integration |
| Embedding smoke | 通过 | 已按 NO_PROXY 约束运行 |
| Section 稳定性 | 通过 | 三组自然样本极差均 ≤ 1 |
| Planner 平均 ≤ 15s | 通过 | slimming 后 9.85s；corrected detailed 13.07s |
| Locate p50 ≤ 20s | 未完全通过 | locate-01 通过，locate-02 为 24.9s |
| Draft wall 降低 ≥ 40% | 通过 | 65.9%～78.4% |
| Detailed total 降低 ≥ 25% | 通过 | 39.9%～72.0% |
| Writer 无 length / failed section | 通过 | 修正后 17/17 stop，0 failed |
| Citation / Retrieval 无回归 | 通过（已测范围） | invalid citation 0；hybrid recall@5 持平 |
| L2 胜或平 ≥ 90% | 未通过 | 1 胜 1 负，50% |
| 默认值提升 | 未执行 | 合并门禁未全部通过 |

## 当前主要瓶颈与后续边界

P2 后新的主要瓶颈是 Reviewer/Revision：corrected detailed 样本中 Review 达 34.8～71.1 秒，触发 Revision 时再增加约 40～41 秒。其次是 Retrieval/Coverage 的额外 round，它直接导致 `locate-02` 第三次达到 36.8 秒。

本轮按约束没有修改：Composer 默认策略、Reviewer effort/token/timeout、Retrieval Round 1、pgvector/BM25/Fusion/Embedding cache、默认模型、benchmark 或 acceptance threshold。后续若进入 P3/P4，应优先解决正文收敛、历史材料选择、Reviewer/Revision 成本，再重跑完整 L2 与 Natural 9×3；Locate 24.9 秒问题需要单独授权 Retrieval 优化后处理。

## 产物

- 基线聚合：`docs/performance/data/baseline-v1-summary.json`
- 本轮聚合：`docs/performance/data/p0-p2-validation-summary.json`
- 性能原始数据：`artifacts/perf-p0-p2/*.json`
- L2：`artifacts/l2-p0-p2-smoke.json`、`artifacts/l2-p0-p2-flow01-retry.json`
- Retrieval：`artifacts/evaluation-20261002-194752.json`
