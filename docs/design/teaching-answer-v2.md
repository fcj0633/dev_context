# Teaching Answer V2 设计

DevContext 的教学回答优先帮助用户建立正确且可复述的心智模型。关键事实、条件与边界仍需充分解释；篇幅和术语数量本身不代表教学质量。

四个原则：先理解再展开；先现象再术语；先为什么再代码；一次只学一个核心东西。

## 计划与职责

正常 single_stream 链路仍是 EvidencePackage → Planner 一次 → Streaming Writer 一次 → 章节引用校验 → Style Inspector → 章节回调及聚合答案。

Planner 同时选择 WHAT/HOW/WHY/COMPARE/DEBUG/LOCATE 类型并安排认知顺序。默认读者是有 Java 基础、首次阅读本项目的 intermediate 读者，显式回答深度优先。DEBUG 无证据的原因和修复必须标为待验证假设。

全局字段：question_kind、direct_answer（最多120字符）、core_mental_model、reader_assumption、likely_misconceptions（最多3条）、answer_depth、sections。章节字段：id、title、reader_takeaway（最多160字符）、new_terms（0～2条且只在首次引入时声明）、key_points、evidence_labels、target_chars。名称继续沿用 MicroSectionPlan、MicroSection 和 MicroExplanationPlanner，trace 版本为 teaching_v2。

章节范围 brief 2～3、standard 3～5、detailed 6～9、deep 8～12。每节正文目标100～250字符，排除标题、引用和marker。detailed 1800～3500字符是宽松阅读参考，允许更短，不设最低字数、不填充或截断。Provider 按深度的总安全预算保留；detailed 为11000 token（受模型能力限制），不向模型显示小节 token 配额。

Writer 先说明现象与问题，再解释办法、命名术语，最后用项目代码证明。标题优先写成用户会问的问题。例子明确为假设；引用放在对应解释段末尾。会改变结论的缺口立即说明，其他缺口放到计划末尾已有章节。词表只提供表达指导，不做正文字符串替换。

required_section_ids 明确要求每节完整输出，不合并或跳过。达到字数不能提前结束。Provider 正常 stop 不等于计划完成；缺失章节仍为 partial，不恢复、不重播、不自动切回旧路径。

## 可读性统计

指标为确定性启发式，不是质量裁判，不修改正文，不触发模型。章节校验通过后、回调前检查；回调失败不把未发布章节计入汇总。统计异常仅记录 inspector_errors，保持发布流程。

- 正文字符数剔除标题、marker、引用和 Markdown 格式符号，不计空白。代码块另计 code_block_chars；其标识符进入代码统计，不进入正文句长。
- 句子按中文句末标点、问号、感叹号及空行切分，不按逗号或 Java 点号切分。平均句长按全部句子加权计算。
- planned_new_terms 是计划声明数量；observed_new_terms 是共享词表正文首次出现数量，只能识别词表覆盖的概念。V1 未声明计划术语时记录 null，不伪造为0。
- 代码名通过行内代码、类名、方法名、常量及限定名规则统计；代码块另计标识符。抽象词按共享词表最长匹配。引用密度为每100个正文字符的引用标记次数。
- warning：章节>320字符、单句>80字符、观察到新术语>2、连续代码标识符>4。计划 new_terms>2 在规划解析时硬拒绝；风格 warning 不重试。
- 全文汇总只包含发布章节，保留 complete/partial/failed 状态，不生成综合易懂性分数。

## 验证与兼容

默认仍为 multi_pass，其旧 ExplanationPlan、检索、Coverage、模型及 reasoning effort 不变。AnswerResult 和 ValidatedSection 回调结构不变。已有流式传输、Parser、marker、allowlist 和发布前最多一次重试继续复用，无新增在线 Reviewer。

真实验证复用历史 V1 Q1（完整）与 Q3（partial），仅新增 V2 Q1/Q3 各一次全链路请求，无预热、重跑、Judge 或多阶段消融。余额检查只请求 `/user/balance`，持久化可用标志而非账户金额；不足时不执行真实生成。Q1 中途收到402时停止剩余真实请求。

先分别验收实现、章节完整性、指标变化与人工阅读；指标降低不自动等于质量提高。Q3 V1 的五节仅作为局部表达参考，不能做完整答案对照。默认模式不自动推广，真实阅读体验由用户判断。
