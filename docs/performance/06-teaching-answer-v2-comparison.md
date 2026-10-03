# Teaching Answer V2：历史答案对照与两题验证

日期：2026-10-03（Asia/Shanghai）。V1 使用历史样本，V2 每题一次，没有预热、重复采样、质量 Judge 或质量 A/B。检索结果未冻结，质量由用户人工阅读判断。

## 1. 验收结论

实现验收通过：584 项测试通过，包括583项普通测试与1项 PostgreSQL 集成检查。正常教学阶段仍为一次 Micro Planner、一次 Streaming Writer，没有重新引入 Composer、Reviewer 或 Revision。默认继续为 `multi_pass`。

本次真实验证Q1完整发布8/8节、Q3完整发布9/9节，均只有一次Writer、没有重试或非法引用。Q3在本次样本中解决了历史缺节问题。Q1平均句长与超长句减少，但全链路时间只下降0.94%；表达改善仍需人工认可，指标下降不直接等于教学质量提升。

实验方向具备继续人工审阅的依据，尚未完成用户阅读验收，不推广默认模式。

## 2. 版本、配置与实现

从已推送的 `codex/single-stream-teaching` 基线 `4a3a3f2` 创建 `codex/teaching-answer-v2`。真实 V2 采样提交为 `ce91061af460b68d3cd2ac61880ddd0c3074b3a5`；V1 历史采样提交为 `2ddb45a0fa69656a188af33fa59a3c065e69f1c7`。报告与使用说明在采样后补充提交。

沿用 `deepseek-flash`、教学 reasoning effort `high`、`text-embedding-v4`（1024维）、`teach`、`detailed`、top-k 12。Provider 窗口131072、能力上限32768，Writer detailed安全预算11000 token，包含 reasoning；章节数变化不扩大该预算。

索引为538个 CODE chunk、2574个 DOCUMENT chunk，采样前后指纹一致（index_unchanged=true），且与历史 V1 一致（index_matches_v1=true）。检索、Coverage 和 EvidenceWorkspace 代码未修改，但每次模型规划与检索返回仍可能不同，不构成冻结证据的因果实验。完整配置与索引检查见 [manifest](../../artifacts/teaching-answer-v2/manifest.json)。

链路保持 EvidencePackage → Micro Planner → Streaming Writer → 章节引用校验 → 发布。V2 在其中增加认知路径规划和发布前 Style Inspector：

- Planner 在同一次调用中判断 WHAT/HOW/WHY/COMPARE/DEBUG/LOCATE，选择读者假设与讲解顺序；trace 使用 `plan_schema_version=teaching_v2`。
- 每节由 `reader_takeaway` 聚焦一件事，`new_terms` 最多两个且只在首次引入时声明，正文 `target_chars` 为100～250。detailed规划6～9节；全文1800～3500字符是软参考，允许更短。
- Writer 先说现象和办法，再命名术语并引用代码；明确列出全部章节ID，不允许合并或遗漏。`stop` 仍必须通过 Parser 的完整性检查。
- Style Inspector 只统计已经通过引用校验的章节，不改写、不阻止有效章节、不重试。异常只进入诊断，partial只汇总已发布章节。

完整原则、字段及口径见 [V2 设计](../design/teaching-answer-v2.md)。原 `AnswerResult`、`ValidatedSection` 回调结构、marker、allowlist、reasoning隔离、首节前最多一次重试及发布后partial行为保留。

## 3. 真实样本与性能

执行顺序仅为 V2 Q1 → V2 Q3：

1. Q1：详细解释当前购票占座的数据一致性是如何保证的。
2. Q3：详细解释当前订单和支付链路是如何保证幂等的。

执行前只请求一次账户查询，`is_available=true`。该标志用于确认调用条件；不保存余额金额，不调用生成模型预热。接口依据 [DeepSeek 官方账户余额文档](https://api-docs.deepseek.com/api/get-user-balance/)。

| 样本 | 总耗时秒 | 教学生成秒 | Writer秒 | 全链路 / 生成调用 | 最终答案字符 | 发布 / 计划 | 状态 |
|---|---:|---:|---:|---|---:|---|---|
| Q1 V1 | 111.35 | 89.11 | 61.50 | 5 / 2 | 3217 | 11 / 11 | complete |
| Q1 V2 | 110.30 | 36.05 | 13.79 | 7 / 2 | 1698 | 8 / 8 | complete |
| Q3 V1（历史partial） | 101.10 | 47.28 | 18.56 | 7 / 2 | 2998 | 5 / 10 | partial |
| Q3 V2 | 124.76 | 53.61 | 21.94 | 7 / 2 | 2511 | 9 / 9 | complete |

| 样本 | TTFT秒 | TTFS秒 | Writer相对TTFT秒 | Writer相对TTFS秒 | 首节提前量秒 |
|---|---:|---:|---:|---:|---:|
| Q1 V1 | 102.37 | 103.42 | 52.52 | 53.57 | 7.93 |
| Q1 V2 | 106.69 | 107.12 | 10.18 | 10.61 | 3.18 |
| Q3 V1（历史partial） | 87.32 | 88.88 | 4.79 | 6.34 | 12.22 |
| Q3 V2 | 120.50 | 120.93 | 17.68 | 18.10 | 3.83 |

Q1总耗时下降 **0.94%**，教学生成下降 **59.54%**，Writer下降 **77.58%**。这是一次历史对照观察，不是稳定收益；Q1没有达到上一轮30%的全链路观察目标。Q3历史结果不完整，不计算完整回答的提速比例。

教学生成时间为 Explanation Planning、Draft、Review、Revision阶段之和，章节诊断和 LLM 调用属于这些阶段，不重复相加。TTFT/TTFS以请求开始为原点，另列相对Writer开始的耗时。首节提前量为最终请求时间减TTFS；CLI仍在结束后打印聚合答案，未实现页面逐节显示。

Q1 V2 的教学生成调用保持2次，全链路调用从5次变成7次：现有Coverage追加了一轮检索与检查。该变化来自未冻结的证据获取流程，没有增加教学评审或风格改写调用。必须结合阶段耗时解释全链路结果，不能把Writer缩短直接表述为总请求等比例提速。

| 样本 | 全链路输入 / 输出 / reasoning | Writer输入 / 输出 / reasoning / 可见输出 |
|---|---|---|
| Q1 V1 | 30558 / 19921 / 14667 | 7897 / 9655 / 7791 / 1864 |
| Q1 V2 | 45944 / 24306 / 19330 | 9904 / 3390 / 2246 / 1144 |
| Q3 V1（历史partial） | 36277 / 21644 / 14514 | 8254 / 4039 / 927 / 3112 |
| Q3 V2 | 36840 / 26490 / 21412 | 8274 / 5674 / 4156 / 1518 |

输入、输出和reasoning均取Provider实际usage。输出token包含reasoning，可见Writer输出为两者差值；缺失usage按null处理，不用字符估算替代。Q3历史V1为partial，耗时与token仅保留原始记录，不能据此计算完整回答的性能提升。

## 4. 可读性指标与阅读观察

V1两份答案用当前同一Inspector、同一共享术语词表离线计算。正文字符数剔除标题、引用、marker、Markdown格式与空白；代码块单独统计，不进入正文句长。句子按中文句末标点、问号、感叹号及空行切分，不按逗号或Java点号切分。

| 样本 | 正文字符 | 平均 / 最长章节 | 平均 / 最长句 | 超长句 / 总句 | 超长句率 | 代码名 | 抽象词 | 引用 / 密度 | warning数 |
|---|---:|---|---|---|---:|---:|---:|---|---:|
| Q1 V1 | 2645 | 240.45 / 288 | 50.86 / 108 | 5 / 51 | 9.80% | 47 | 12 | 46 / 1.74 | 5 |
| Q1 V2 | 1275 | 159.38 / 193 | 36.50 / 88 | 1 / 34 | 2.94% | 11 | 6 | 36 / 2.82 | 1 |
| Q3 V1（历史partial） | 2552 | 510.40 / 591 | 66.16 / 179 | 14 / 38 | 36.84% | 50 | 11 | 30 / 1.18 | 11 |
| Q3 V2 | 1900 | 211.11 / 282 | 38.58 / 106 | 2 / 48 | 4.17% | 30 | 14 | 61 / 3.21 | 3 |

Q1 V2 正文1275字符低于1800～3500的宽松参考；按计划允许更短，没有填充或截断。Q3 V2正文1900字符；第5/6节分别275/282字符，超过小节250字符目标但未超过320警告阈值，保留实际偏差。Q3第一节观察到3个新术语，产生NEW_TERM_LOAD警告；另有两句超过80字符。这些结果没有触发重写。

计划新术语与观察到的首次术语不同：前者来自Planner，后者只能识别共享词表，不能证明覆盖所有专业概念。V1未声明计划术语，记录null。引用密度为每100正文字符的标记次数；正文缩短时密度可能上升，不能单独判断更好或更差。

每节的字符数、目标偏差、句长、新术语、标识符、引用密度和warning都在对应readability JSON中，没有综合“易懂性分数”。warning阈值为章节>320、单句>80、观察到新术语>2、连续代码名>4；本轮没有用重写修补偏离。

Q1 V2 标题更接近读者会问的问题，例如“占座到底在改哪些数据？”、“座位和车票记录怎么一起成功或一起失败？”。第3节使用明确标为“假设”的并发场景，第6节把座位释放与令牌归还分开解释。相比历史V1密集列出方法名，这些形式有利于逐步阅读，但全文更短，不能仅凭这些形式确认关键事实全部保留。

仍有需要人工核对的措辞：Q1首节将用户锁描述为“防止同一用户重复提交”，第7节又说明占座去重过滤器未实现。锁的串行执行与请求幂等并不等同，首节措辞可能让读者形成过强理解。回答保留原样，引用标签校验不能消除这种语义风险。

请结合完整答案检查：第一次是否能理解，是否反复读同一句，能否说出各机制解决什么问题，能否复述主线，代码是否帮助理解，事实与失败边界是否保留。**人工认可尚未取得，不给两个样本套用胜率门禁。**

## 5. 完整性、引用与失败记录

Q1 V2规划、闭合和发布8/8节；Q3 V2规划、闭合和发布9/9节。两次均为complete，失败章节0，missing_sections为空，Writer调用各1次、重试0，Style统计异常0。已发布正文的非法引用0；答案未保留内部marker或reasoning字段。

V1 Q3的S6～S10缺失记录保留。本次V2提供了完整回答，确认这一样本的完整性问题改善；两个单次样本不能证明未来都不会缺节，也没有恢复历史失败请求。

已发布章节均通过存在性、本节allowlist、实际输入证据及至少一个有效引用检查；这只保证引用协议，不代表已经验证全部事实与推导。没有把reasoning_content写入答案，Provider reasoning仅记录usage计数。发布后的失败继续保留有效正文与missing_sections，不恢复、不重播、不fallback。

原始trace、逐次尝试、缺失章节及错误均保存，不以partial的较短耗时宣称成功。

## 6. 产物、验证与后续

| 样本 | 完整答案入口 | 原始性能 | 可读性 |
|---|---|---|---|
| Q1 V1（历史） | [答案](../../artifacts/teaching-answer-v2/q1-v1.answer.md) | [历史性能](../../artifacts/single-stream-experiment/q1-single_stream.json) | [指标](../../artifacts/teaching-answer-v2/q1-v1.readability.json) |
| Q1 V2 | [答案](../../artifacts/teaching-answer-v2/q1-v2.answer.md) | [性能](../../artifacts/teaching-answer-v2/q1-v2.json) | [指标](../../artifacts/teaching-answer-v2/q1-v2.readability.json) |
| Q3 V1（历史partial） | [五节答案](../../artifacts/teaching-answer-v2/q3-v1.answer.md) | [历史性能](../../artifacts/single-stream-experiment/q3-single_stream.json) | [指标](../../artifacts/teaching-answer-v2/q3-v1.readability.json) |
| Q3 V2 | [答案](../../artifacts/teaching-answer-v2/q3-v2.answer.md) | [性能](../../artifacts/teaching-answer-v2/q3-v2.json) | [指标](../../artifacts/teaching-answer-v2/q3-v2.readability.json) |

V2独立trace为 [Q1](../../artifacts/teaching-answer-v2/q1-v2.trace.json)、[Q3](../../artifacts/teaching-answer-v2/q3-v2.trace.json)，对应`.out`和`.err`保留完整运行日志。配置在manifest，简表在summary.json，检查记录在validation.json。原始产物在初次实现提交时仅保留本地；后续按用户要求已显式纳入版本控制，方便远程分析。统一入口见 [真实回答分析样本](../../artifacts/README.md)，V1目录未覆盖。

测试命令（PowerShell）：

```powershell
$env:DEVCONTEXT_RUN_INTEGRATION='1'
.venv\Scripts\python.exe -m pytest -o addopts='' -q
```

实际结果584 passed / 7.23s；基线531项，新增与更新的覆盖包括schema、深度、预算、引用与marker、正常stop缺节、风格统计、统计异常不丢输出、无额外调用、两题运行顺序与余额不足停止。`git diff --check`通过。

本轮保持默认multi_pass；显式single_stream即可使用V2，切回multi_pass即可回退，无数据迁移。没有修改模型、reasoning effort或索引，也没有自动推广。后续须先取得两题的人工认可，再在独立任务扩展WHAT/HOW/WHY/COMPARE样本；本轮不追加验证请求。
