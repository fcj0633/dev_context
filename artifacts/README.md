# 真实回答分析样本

这些文件是已完成测试的原始产物，按用户要求提交供远程分析。本次只整理历史文件，没有调用模型生成新回答、没有重跑测试问题，也没有新增质量评测。

## Teaching Answer V2 与 Single-Stream V1

优先阅读答案，再结合章节计划、引用映射、性能与可读性数据分析。V2 的两次真实请求均为 teach / single_stream / detailed / top-k 12，每题一次，Planner 与 Writer reasoning effort 为 high。

| 问题 / 版本 | 原始答案 | 完成状态 | 计划与原始 trace | 性能 |
|---|---|---|---|---|
| Q1 占座一致性，旧 multi_pass | [答案](single-stream-experiment/q1-multi_pass.answer.md) | 旧路径完成，Composer回退拼接 | [trace](single-stream-experiment/q1-multi_pass.trace.json) | [性能](single-stream-experiment/q1-multi_pass.json) |
| Q1 占座一致性，Single-Stream V1 | [答案](single-stream-experiment/q1-single_stream.answer.md) | complete，11/11 | [trace](single-stream-experiment/q1-single_stream.trace.json) | [性能](single-stream-experiment/q1-single_stream.json) |
| Q1 占座一致性，Teaching V2 | [答案](teaching-answer-v2/q1-v2.answer.md) | complete，8/8 | [trace](teaching-answer-v2/q1-v2.trace.json) | [性能](teaching-answer-v2/q1-v2.json) |
| Q3 订单支付幂等，旧 multi_pass | [失败提示](single-stream-experiment/q3-multi_pass.answer.md) | HTTP 402余额不足，无有效答案 | [trace](single-stream-experiment/q3-multi_pass.trace.json) | [性能](single-stream-experiment/q3-multi_pass.json) |
| Q3 订单支付幂等，Single-Stream V1 | [五节答案](single-stream-experiment/q3-single_stream.answer.md) | partial，5/10，缺S6～S10 | [trace](single-stream-experiment/q3-single_stream.trace.json) | [性能](single-stream-experiment/q3-single_stream.json) |
| Q3 订单支付幂等，Teaching V2 | [答案](teaching-answer-v2/q3-v2.answer.md) | complete，9/9 | [trace](teaching-answer-v2/q3-v2.trace.json) | [性能](teaching-answer-v2/q3-v2.json) |

V1 的独立 `*.trace.json` 从对应 `.out` 的原始 Trace JSON提取；原始日志未改动。V2 trace由已有运行入口保存。`.out` 为完整CLI输出，`.err` 为错误日志；空错误文件也保留。没有把Provider的reasoning正文持久化到这些答案，reasoning token使用量保留在性能数据中。

V2目录的 `q1-v1.answer.md` 与 `q3-v1.answer.md` 是历史答案的对照副本，不代表额外采样。对应V1/V2的 `*.readability.json` 使用同一确定性Inspector计算；V1术语计划未知时为null。

- [V1配置与索引快照](single-stream-experiment/manifest.json)、[V1摘要](single-stream-experiment/summary.json)。
- [V2配置与索引快照](teaching-answer-v2/manifest.json)、[V2摘要](teaching-answer-v2/summary.json)、[测试记录](teaching-answer-v2/validation.json)。
- [V1实验报告](../docs/performance/05-single-stream-teaching-experiment.md)、[V2对照报告](../docs/performance/06-teaching-answer-v2-comparison.md)。

V2每题只有一次真实样本，V1是历史样本，检索返回未冻结。Q3 V1只可作五节的局部表达参考。标签校验不代表全部事实和推导已经验证；表达是否容易理解由人工判断。

## 较早的全链路回答

以下历史结果保持原样。版本、问题、配置和失败情况以对应原始日志与性能JSON为准，不作为新增V2样本，也不据不同版本的文件名推断可直接比较。

| 历史目录 | Q1 | Q2 | Q3 | 配套文件 |
|---|---|---|---|---|
| perf-fullchain | [答案](perf-fullchain/q1.answer.txt) | [答案](perf-fullchain/q2.answer.txt) | [答案](perf-fullchain/q3.answer.txt) | 同名 `.out`、`.err`、`.json` |
| perf-fullchain2 | [正文](perf-fullchain2/q1.answerbody.txt) | [正文](perf-fullchain2/q2.answerbody.txt) | [正文](perf-fullchain2/q3.answerbody.txt) | 同名 `.answer.txt`、`.out`、`.err`、`.json` |

## 令牌桶与默认教学历史样本

下面的txt文件包含当时CLI输出或诊断，应结合文件中的问题、Sources、Trace和错误阅读；失败记录保留，不当作成功答案。

- [令牌桶 teach](answer-quality-v2-baseline/token-bucket.teach.txt)、[teach-p3](answer-quality-v2-baseline/token-bucket.teach-p3.txt)、[teach-p4](answer-quality-v2-baseline/token-bucket.teach-p4.txt)。
- [令牌桶 explain](answer-quality-v2-baseline/token-bucket.explain.txt)、[explain-current](answer-quality-v2-baseline/token-bucket.explain-current.txt)、[legacy](answer-quality-v2-baseline/token-bucket.legacy.txt)。
- [令牌桶代理失败记录](answer-quality-v2-baseline/PROXY-FAILURE-token-bucket.explain.txt)。
- [默认教学 smoke 1](smoke-default-teach.txt)、[smoke 2](smoke-default-teach2.txt)、[smoke 3](smoke-default-teach3.txt)。

## 文件管理

本次显式提交现有答案及分析配套文件。仓库的artifacts默认忽略规则继续保留，新产生的其他文件仍需明确选择后提交。嵌入缓存、索引导出、无回答正文的检索benchmark结果和本地临时报告脚本未纳入这批分析样本。

已检查这些文件是否包含当前配置的API凭据和常见凭据模式，未发现匹配；没有提交 `.env` 或数据库连接凭据。原始答案保持原样，没有为使指标更好而删节或重写。
