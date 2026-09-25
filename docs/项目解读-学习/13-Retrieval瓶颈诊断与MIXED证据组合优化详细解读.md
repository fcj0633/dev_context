# Retrieval 瓶颈诊断与 MIXED 证据组合优化详细解读

> 对应实现提交：`bc60bb3 feat: diagnose and improve mixed retrieval composition`
> 分支：`main`
> 正式验证时间：2026-09-25
> 本文定位：承接《09-评测框架 v2 实现与真实结果详细解读》和《12-QueryRouter 与 RetrievalPolicy 实现详细解读》，解释系统如何从真实 Benchmark 判断检索瓶颈，并完成第一次由失败数据驱动的最小检索优化。

---

# 一、先说结论：这次真正解决的是什么

这一阶段最重要的成果，不是把某个参数从 `0.6` 调成 `0.7`，也不是换一个更大的 Embedding 模型，而是回答了一个在检索系统里更基础的问题：

> 当前结果不够好，究竟是因为候选根本没有召回，还是已经召回但排得不够靠前，或者 CODE 和 DOCUMENT 分别找到了，却没有被正确组合进最终 Top-5？

优化前 routed 的核心指标是：

| 指标 | Before |
|---|---:|
| Recall@5 | 0.6250 |
| Recall@10 | 0.7917 |
| Recall@20 | 0.8333 |
| CODE Recall@5 | 0.7500 |
| DOC Recall@5 | 0.5833 |
| MIXED Recall@5 | 0.5417 |
| MIXED both_sources_hit@5 | 0.1667 |

只看 `Recall@5 = 0.625`，很容易得出一个过于笼统的结论：

```text
检索效果不好，需要继续优化召回。
```

但把观察深度扩展到 Top10、Top20，并检查 MIXED 的 CODE、DOCUMENT 独立候选后，真实情况变成：

```text
18 个 Recall@5 失败案例
├── 3 个 RETRIEVAL_MISS
├── 5 个 RANKING_MISS
└── 10 个 COMPOSITION_MISS
```

因此主要瓶颈并不是 Candidate Recall，而是：

```text
MIXED_EVIDENCE_COMPOSITION
```

这个结论直接决定了本轮不应该做什么：

- 不换 Embedding；
- 不重做 Chunk；
- 不修改 PostgreSQL 或 pgvector；
- 不修改 Keyword SQL；
- 不调整 RRF 权重；
- 不引入 Cross-Encoder；
- 不提前上 GraphRAG；
- 不为了让数字变好而修改 Benchmark 或 Ground Truth。

最终只做了一个很小的优化：

> 对 MIXED 查询的 DOCUMENT 独立候选池进行一次确定性的“标题锚点提升”，最多提升一个文档结果，然后继续沿用原来的 CODE-first 交错组合。

正式 After 结果为：

| 指标 | Before | After | 变化 |
|---|---:|---:|---:|
| Recall@5 | 0.6250 | 0.6667 | +0.0417 |
| Recall@10 | 0.7917 | 0.8194 | +0.0278 |
| Recall@20 | 0.8333 | 0.8611 | +0.0278 |
| MIXED Recall@5 | 0.5417 | 0.6667 | +0.1250 |
| MIXED both_sources_hit@5 | 0.1667 | 0.4167 | +0.2500 |
| MIXED both_sources_hit@10 | 0.5000 | 0.6667 | +0.1667 |

CODE、DOC 单源 Recall 没有下降。这说明优化确实作用在它应该解决的地方：**MIXED 双源证据组合**。

---

# 二、为什么只看 Recall@5 无法定位问题

## 2.1 Recall@5 只能告诉你“失败了”，不能告诉你“为什么失败”

当前 Benchmark 的 Recall 以 Ground Truth 目标组为分母。

CODE、DOC 问题通常只有一个目标组：

```text
Recall@5 = Top5 命中目标组 ? 1 : 0
```

MIXED 问题通常有两个目标组：

```text
一个 CODE 目标组
+
一个 DOCUMENT 目标组
```

如果 Top5 只命中 CODE，没有命中文档：

```text
Recall@5 = 1 / 2 = 0.5
```

这个 `0.5` 至少可能对应三种完全不同的情况。

### 情况 A：文档根本没有召回

```text
DOCUMENT Top20：没有 Ground Truth
```

这才是严格意义上的 Candidate Recall 问题。

可能原因包括：

- Query 和文档表达差异太大；
- Chunk 内容没有覆盖关键语义；
- Embedding 表达能力不足；
- 目标文档没有正确入库；
- Ground Truth 指向的标题层级与数据库实际数据不一致。

### 情况 B：文档已经在第 8 名

```text
DOCUMENT Ground Truth rank = 8
```

这是 Ranking 问题。候选已经找到了，继续扩展召回数量没有解决核心矛盾；真正需要考虑的是如何利用符号、标题、路径或其他元数据改善顺序。

### 情况 C：CODE、DOCUMENT 各自都找到了，但交错后被截断

假设两个独立候选列表是：

```text
CODE：C1 C2 C3 C4 C5 ...
DOC： D1 D2 D3 D4 D5 ...
```

CODE-first 交错后的 Top5 是：

```text
C1 D1 C2 D2 C3
```

如果正确 CODE 是 `C3`，正确 DOCUMENT 是 `D3`：

```text
CODE 最终 rank = 5
DOC  最终 rank = 6
```

两条证据分别都在源内 Top3，但五个位置不可能同时容纳它们。这不是基础召回失败，而是**组合容量和组合顺序问题**。

## 2.2 为什么增加 Recall@10 和 Recall@20

三个观察窗口分别回答不同问题：

| 指标 | 主要回答的问题 |
|---|---|
| Recall@5 | 最终交给 Context Builder 的小窗口够不够好 |
| Recall@10 | 目标是否就在第一层扩展候选里 |
| Recall@20 | 当前 Retriever 是否已经具备基本 Candidate Recall |

本次 Before 数据是：

```text
Recall@5  = 0.6250
Recall@10 = 0.7917
Recall@20 = 0.8333
```

从 Top5 到 Top20 增加了：

```text
0.8333 - 0.6250 = 0.2083
```

这是一个非常明显的信号：

> 很多正确证据并没有消失，而是排在了 Top5 之后。

如果 Top5、Top10、Top20 都很低，才更像基础召回问题；现在 Top20 已达到 `0.8333`，说明直接重做 Embedding 或 Chunk 并不是最低风险的下一步。

---

# 三、为什么还需要“源内候选排名”

## 3.1 最终排名会隐藏 Policy 内部的信息

MIXED Policy 本身执行两次来源过滤检索：

```text
Query
├── Vector Search(source_type=CODE)
└── Vector Search(source_type=DOCUMENT)
```

随后再交错：

```text
CODE-1, DOCUMENT-1, CODE-2, DOCUMENT-2, ...
```

如果只看最终 Top20，那么最多只能看到：

```text
CODE 源内前 10
DOCUMENT 源内前 10
```

源内第 11～20 名会因为交错截断而完全消失。

例如 MIXED-007：

```text
正确 CODE：源内第 1，最终第 1
正确 DOC： 源内第 12，最终 Top20 中消失
```

如果只看最终结果，会误判为：

```text
DOCUMENT 没有召回。
```

但真实情况是：

```text
DOCUMENT 已经进入独立来源 Top20，
只是没有进入交错结果允许的 DOCUMENT 前 10。
```

## 3.2 `SearchExecution.source_candidates`

为了保留这层诊断信息，`SearchExecution` 增加了一个默认空字典：

- [models.py](../../src/devcontext/models.py)

概念结构是：

```text
SearchExecution
├── results                最终给调用方的排序结果
├── timings                五段耗时
└── source_candidates      Policy 各来源独立候选
    ├── CODE
    └── DOCUMENT
```

这个字段有几个重要边界：

1. `search()` 的返回值仍是 `list[SearchResult]`；
2. `search_with_trace()` 的主要接口没有改变；
3. Context Builder 仍然只读取最终 `results`；
4. Answer Generator 完全不读取它；
5. 它主要服务于 Evaluation 和调试，不改变生产答案链路。

这样评测器可以同时输出：

```text
final_rank
source_candidate_rank
```

这两个排名的差异，正是识别 Composition Miss 的关键。

---

# 四、三类失败是如何定义的

核心实现位于：

- [_case_detail()](../../src/devcontext/evaluation/runner.py)
- [_bottleneck_analysis()](../../src/devcontext/evaluation/runner.py)

## 4.1 `RETRIEVAL_MISS`

定义：

> 至少一个必需的 Ground Truth，在对应来源的独立 Top20 候选中都不存在。

判断形式：

```text
source_candidate_rank is null
```

它表示当前候选生成阶段真的没有把目标取回来。

这类问题才适合进一步检查：

- Query Rewrite；
- Embedding；
- Chunk 内容；
- 文档是否入库；
- 符号和标题元数据；
- Keyword/Vector 候选生成方式。

Before 中只有 3 条：

```text
CODE-010
DOC-002
DOC-012
```

## 4.2 `RANKING_MISS`

定义：

> Ground Truth 已经进入最终 Top10 或 Top20，但没有进入 Top5。

典型形式：

```text
final_rank = 6 / 8 / 9 / 15
```

Before 中有 5 条：

```text
CODE-007
CODE-009
DOC-004
DOC-005
DOC-007
```

这类案例说明基础检索并不差，主要需要更准确的排序信号，例如：

- 精确 symbol/class/signature；
- 文档 heading/title；
- 文件路径；
- 查询里的业务实体；
- 后续轻量 Reranker。

## 4.3 `COMPOSITION_MISS`

定义：

> MIXED 问题的 CODE、DOCUMENT Ground Truth 都存在于各自来源的 Top20，但最终 Top5 没有同时包含两类正确证据。

Before 中有 10 条：

```text
MIXED-001 ～ MIXED-009
MIXED-012
```

这是数量最多的失败类型。

## 4.4 为什么 Composition 的判断优先于 Ranking

MIXED-003 的排名是：

```text
CODE：source rank 3 → final rank 5
DOC： source rank 3 → final rank 6
```

如果只看最终排名，DOC 在第 6，可以叫 Ranking Miss。

但它更本质的问题是：

```text
Top5 只有五个槽位；
CODE 和 DOC 都需要各自前三名才能命中；
保持源内顺序交错时，它们无法同时进入 Top5。
```

因此分类优先级固定为：

```text
1. Recall@5 已满 → 无失败
2. MIXED 两侧源内 Top20 均找到，但 Top5 不完整 → COMPOSITION_MISS
3. 非 MIXED 目标在最终 Top20、Top5 之后 → RANKING_MISS
4. 来源 Top20 仍找不到 → RETRIEVAL_MISS
```

这样三个分类互斥，每条失败只属于一种主要原因。

---

# 五、Before 的 18 个失败案例应该怎样阅读

排名写成：

```text
最终 routed 排名 / 来源内部排名
```

`—` 表示不在对应 Top20 中。

| Case | Ground Truth 摘要 | 实际排名 | 类型 | Top5 缺失 |
|---|---|---|---|---|
| CODE-007 | `OrderServiceImpl#createTicketOrder` | `9 / 9` | RANKING_MISS | CODE |
| CODE-009 | `TrainPurchaseTicketParamStockChainFilter#handler` | `6 / 6` | RANKING_MISS | CODE |
| CODE-010 | `UserRemoteService#listPassengerQueryByIds` | `— / —` | RETRIEVAL_MISS | CODE |
| DOC-002 | `Feign移出事务... / 11.3 实际事务与锁边界` | `— / —` | RETRIEVAL_MISS | DOCUMENT |
| DOC-004 | `P2支付与超时取消功能分析与设计` | `8 / 8` | RANKING_MISS | DOCUMENT |
| DOC-005 | `项目介绍与亮点 / 2.1 服务地图` | `15 / 15` | RANKING_MISS | DOCUMENT |
| DOC-007 | `订单与支付模块 / 4.2 订单状态机` | `9 / 9` | RANKING_MISS | DOCUMENT |
| DOC-012 | `订单与支付模块 / 10.4 工程层面` | `— / —` | RETRIEVAL_MISS | DOCUMENT |
| MIXED-001 | `purchaseTickets` + 责任链文档 | CODE `1/1`；DOC `—/15` | COMPOSITION_MISS | DOCUMENT |
| MIXED-002 | BloomFilter Bean + 注册设计 | CODE `1/1`；DOC `6/3` | COMPOSITION_MISS | DOCUMENT |
| MIXED-003 | `scanTimeoutOrder` + 双通道设计 | CODE `5/3`；DOC `6/3` | COMPOSITION_MISS | DOCUMENT |
| MIXED-004 | `notifyPayResult` + 支付通知设计 | CODE `3/2`；DOC `—/16` | COMPOSITION_MISS | DOCUMENT |
| MIXED-005 | `createTicketOrder` + `afterCommit` | CODE `—/19`；DOC `18/9` | COMPOSITION_MISS | CODE、DOCUMENT |
| MIXED-006 | `closeTimeoutOrder` + 双通道设计 | CODE `5/3`；DOC `8/4` | COMPOSITION_MISS | DOCUMENT |
| MIXED-007 | `payCallback` + 支付回调通知 | CODE `1/1`；DOC `—/12` | COMPOSITION_MISS | DOCUMENT |
| MIXED-008 | `PayNotifyCompensateJob` + 补偿任务文档 | CODE `3/2`；DOC `10/5` | COMPOSITION_MISS | DOCUMENT |
| MIXED-009 | `pageListTicketQuery` + 车票查询读链路 | CODE `3/2`；DOC `—/11` | COMPOSITION_MISS | DOCUMENT |
| MIXED-012 | Stock Chain `handler` + 责任链文档 | CODE `—/13`；DOC `2/1` | COMPOSITION_MISS | CODE |

从表中可以看到一个非常明显的结构：

```text
10 个 MIXED 失败里：
大部分 CODE 已经很靠前；
真正挤不进最终窗口的通常是 DOCUMENT。
```

这就是为什么本轮选择“提升一个最明确的 DOCUMENT 锚点”，而不是对 CODE 和 DOC 进行全面重排。

---

# 六、为什么没有直接把 DOC 改成 Hybrid

一个看起来很自然的方案是：

```text
DOC：Vector → Hybrid
MIXED 的两侧：Vector → Hybrid
```

项目已经有 Keyword、Vector 和 RRF，实现成本确实很低。

但真实只读实验表明，这个方案反而退化：

| 方案 | Recall@5 | DOC Recall@5 | MIXED both@5 |
|---|---:|---:|---:|
| 当前 Vector Policy | 0.6250 | 0.5833 | 0.1667 |
| DOC 使用 Hybrid | 0.5694 | 0.4167 | 0.1667 |
| MIXED 两侧都使用 Hybrid | 0.5556 | 0.4167 | 0.0833 |

为什么？

当前 Keyword 检索针对 Java 标识符有明显优势，但文档的自然语言标题、正文中存在大量重复词和相似章节。Keyword 加入 RRF 后，一些词面相似但标题层级错误的 Chunk 被推到前面，反而挤掉了正确文档。

这说明：

> “系统已经有 Hybrid”不等于“每个 Route 都应该使用 Hybrid”。策略必须服从真实评测，而不是服从实现上的统一感。

---

# 七、为什么没有改成按 Vector 分数直接合并

另一个低成本方案是：

```text
CODE Top20 + DOC Top20
→ 按 cosine score 全局排序
→ 强制至少保留一条 CODE 和一条 DOC
```

它看起来比固定交错更“动态”。

但只读实验结果同样退化：

```text
Recall@5：0.6250 → 0.5833
MIXED Recall@5：0.5417 → 0.4167
MIXED both@5：0.1667 → 0.0833
```

原因主要有两个：

1. CODE 和 DOCUMENT 的向量文本结构不同，绝对 cosine score 未必适合跨来源直接比较；
2. 强制各保留一条只保证“来源存在”，不保证保留的是 Ground Truth。

所以当前继续保留来源内排序和稳定交错，只在 DOCUMENT 一侧增加一个非常保守的锚点信号。

---

# 八、最终选择：MIXED 文档锚点提升

核心实现位于：

- [RetrievalPolicy](../../src/devcontext/retrieval/policy.py)
- `_metadata_terms()`
- `_document_metadata_overlap()`
- `_promote_document_anchor()`
- `_interleave_results()`

## 8.1 完整数据流

优化后的 MIXED 流程是：

```text
Query
  │
  ├── CODE Vector Search(top_k)
  │      └── 保持原顺序
  │
  └── DOCUMENT Vector Search(max(20, top_k))
         │
         ├── 读取 file_path
         ├── 读取 title
         ├── 读取 heading_path
         ├── 计算 query / metadata 词项重合度
         └── 最多提升一个最明确的 DOCUMENT anchor
  │
  ▼
CODE-first 稳定交错
  │
  ▼
最终 Top-K
```

## 8.2 为什么 DOCUMENT 候选池至少为 20

如果 CLI 请求：

```text
--top-k 5
```

而 Policy 只向数据库请求 DOCUMENT Top5，那么源内第 11、12、15 名永远没有机会被元数据识别。

所以候选池使用：

```python
document_pool_size = max(20, top_k)
```

注意这不是把最终 Context 扩成 20 条。最终返回仍然不超过用户请求的 `top_k`：

```text
数据库候选池：20
最终 SearchResult：5
Context Builder 输入：5
```

这就是经典的两阶段思想：

```text
先取稍大的候选池
再做一个很轻的本地选择
```

但这里没有引入完整 Reranker，只进行一次锚点提升。

## 8.3 元数据如何分词

先执行：

```python
unicodedata.normalize("NFKC", value).casefold()
```

目的包括：

- 统一全角/半角形式；
- 统一兼容字符；
- 忽略英文大小写；
- 让 `PayCallback` 和 `paycallback` 可比较。

英文和 Java 标识符按完整词提取，例如：

```text
PayNotifyCompensateJob
payCallback
orderSn
```

标准化后得到：

```text
paynotifycompensatejob
paycallback
ordersn
```

中文没有天然空格，所以使用连续双字片段。

例如：

```text
支付回调通知
```

得到：

```text
支付
付回
回调
调通
通知
```

双字片段比整句精确匹配更能容忍：

- “支付回调与下游通知”；
- “回调后的通知推进”；
- “支付通知补偿流程”。

## 8.4 为什么只看文档元数据，不看正文

参与匹配的只有：

```text
file_path + title + heading_path
```

没有使用正文 `content`。

原因是正文范围大、重复词多，容易把“提到某个主题”的 Chunk 推上来；标题层级更接近文档作者对这一节主题的明确描述。

例如 query 问：

```text
支付回调后状态推进和通知如何设计？
```

标题：

```text
五、支付模块：设计与实现
  > 5.3 支付回调与下游通知（详细流程）
```

比某个正文里偶然出现“支付”“通知”更值得成为 Context 锚点。

## 8.5 重合度如何计算

计算方式非常简单：

```text
metadata_overlap
= query 词项与 metadata 词项的交集数量
  ÷ query 词项数量
```

它只用于候选间比较，不覆盖原始向量分数。

## 8.6 为什么最多只提升一个结果

算法不是把 20 条文档全部重新排序，而是：

```text
1. 找到重合度最高的 DOCUMENT
2. 如果它严格高于当前第一名，移动到第一位
3. 其他 DOCUMENT 保持原始相对顺序
```

例如原始顺序：

```text
D1 D2 D3 D4 D5
```

如果 `D4` 是最强标题锚点：

```text
D4 D1 D2 D3 D5
```

不会变成：

```text
D4 D2 D5 D1 D3
```

这种设计刻意限制优化的影响面：

- 只移动一个结果；
- 不修改其他结果的相对顺序；
- 不修改 score；
- 重合度并列时保持原排名；
- 没有有效词项时完全不动；
- DOCUMENT 为空时不伪造结果。

这比引入一个全量重排公式更容易解释、测试和回退。

---

# 九、三个被修复的真实案例

优化后，18 个失败案例减少为 15 个，直接修复：

```text
MIXED-007
MIXED-008
MIXED-009
```

## 9.1 MIXED-007：支付回调与通知推进

问题同时需要：

```text
CODE：PayServiceImpl#payCallback
DOC： 支付回调与下游通知详细流程
```

Before：

```text
CODE source rank = 1
DOC  source rank = 12
DOC  final rank  = null
```

正确文档标题与 query 中的“支付回调”“状态推进”“通知”高度重合。锚点提升后，这一文档被移到 DOCUMENT 首位，再与 CODE 交错，因此双源都进入 Top5。

## 9.2 MIXED-008：支付通知补偿任务

目标是：

```text
CODE：PayNotifyCompensateJob
DOC： 5.4.2 PayNotifyCompensateJob —— 通知补偿任务
```

Before：

```text
CODE source rank = 2 → final rank = 3
DOC  source rank = 5 → final rank = 10
```

这个案例非常适合元数据锚点，因为类名直接出现在 Markdown 标题中。提升后不需要猜测正文语义，就能把最直接的设计/源码说明文档送进 Top5。

## 9.3 MIXED-009：车票查询读链路

目标是：

```text
CODE：TicketServiceImpl#pageListTicketQuery
DOC： 车票查询读链路 / 车次列表 + 票价 + 余票
```

Before：

```text
CODE source rank = 2 → final rank = 3
DOC  source rank = 11 → final rank = null
```

目标文档的文件名和标题层级与 query 中“车票查询”“列表”“票价”“余票”“读链路”等表达重合，因此成为明确锚点。

这三个案例的共同特征是：

> 正确 DOCUMENT 已经在源内 Top20，而且标题元数据比纯向量排名更明确。

因此本轮优化不是凭空创造召回，而是把已经召回、身份清晰的证据更合理地组合进最终小窗口。

---

# 十、Before / After 的完整指标应该怎样解释

## 10.1 全局指标

| 指标 | Before | After | Delta |
|---|---:|---:|---:|
| Recall@5 | 0.6250 | 0.6667 | +0.0417 |
| Recall@10 | 0.7917 | 0.8194 | +0.0278 |
| Recall@20 | 0.8333 | 0.8611 | +0.0278 |

三档 Recall 同时提升，说明优化没有简单地把一个正确结果从第 6 名换到第 4 名、同时把其他正确结果挤出 Top20。

## 10.2 CODE 指标

| 指标 | Before | After |
|---|---:|---:|
| CODE Recall@5 | 0.7500 | 0.7500 |
| CODE Recall@10 | 0.9167 | 0.9167 |
| CODE Recall@20 | 0.9167 | 0.9167 |

完全不变是符合设计预期的，因为 CODE Route 和 MIXED 的 CODE 候选顺序都没有修改。

## 10.3 DOC 指标

| 指标 | Before | After |
|---|---:|---:|
| DOC Recall@5 | 0.5833 | 0.5833 |
| DOC Recall@10 | 0.7500 | 0.7500 |
| DOC Recall@20 | 0.8333 | 0.8333 |

DOC 单源 Route 也完全不变。锚点逻辑只在 MIXED 生效，这是控制回归风险的重要边界。

## 10.4 MIXED 指标

| 指标 | Before | After | Delta |
|---|---:|---:|---:|
| MIXED Recall@5 | 0.5417 | 0.6667 | +0.1250 |
| MIXED Recall@10 | 0.7083 | 0.7917 | +0.0833 |
| MIXED Recall@20 | 0.7500 | 0.8333 | +0.0833 |
| both_sources_hit@5 | 0.1667 | 0.4167 | +0.2500 |
| both_sources_hit@10 | 0.5000 | 0.6667 | +0.1667 |

`both_sources_hit@5` 从：

```text
2 / 12
```

提升为：

```text
5 / 12
```

这正好对应新增修复的 3 条 MIXED 用例。

## 10.5 延迟为什么不能简单归因

正式两次运行观测到：

```text
Before average routed latency = 654.87 ms
After  average routed latency = 616.44 ms
```

不能据此说锚点提升让系统快了 38 ms。

原因是端到端时间主要受：

- 百炼 Embedding API 网络；
- PostgreSQL 当前负载；
- Docker Desktop 状态；
- TLS 和本地网络波动；
- 操作系统缓存。

锚点逻辑本身只做本地集合运算，但 DOCUMENT SQL 从 Top5 扩大到 Top20 也可能略微增加数据库开销。因此正确结论是：

> 本轮正式运行未观察到延迟回归，但不能把两次在线测试的差值直接当作算法加速效果。

---

# 十一、评测报告新增了什么

评测入口：

- [runner.py](../../src/devcontext/evaluation/runner.py)
- [cli.py](../../src/devcontext/cli.py)

## 11.1 单案例新增字段

每个案例现在包括：

```text
recall_at_10
recall_at_20
code_hit_at_10
doc_hit_at_10
both_sources_hit_at_10
target_ranks
missing_sources_at_5
failure_type
preliminary_reason
```

`target_ranks` 结构大致为：

```json
{
  "source_type": "DOCUMENT",
  "ground_truth": {
    "source_type": "DOCUMENT",
    "path_contains": "订单与支付模块.md",
    "heading_path": [
      "my12306 · 订单模块与支付模块 · 设计文档（自足版）",
      "五、支付模块：设计与实现",
      "5.3 支付回调与下游通知（详细流程）"
    ]
  },
  "final_rank": null,
  "source_candidate_rank": 12
}
```

看到这个结构，就能明确区分：

```text
不是没有召回；
而是源内第 12 被组合阶段截断。
```

## 11.2 聚合新增指标

每个 strategy 现在都输出：

```text
Recall@3
Recall@5
Recall@10
Recall@20
CODE hit@3/@5/@10
DOC hit@3/@5/@10
MIXED both-sources hit@3/@5/@10
```

## 11.3 `bottleneck_analysis`

顶层报告新增：

```json
{
  "bottleneck_analysis": {
    "strategy": "routed",
    "diagnostic_k": 20,
    "failure_count": 18,
    "failure_counts": {
      "RETRIEVAL_MISS": 3,
      "RANKING_MISS": 5,
      "COMPOSITION_MISS": 10
    },
    "primary_bottleneck": "MIXED_EVIDENCE_COMPOSITION",
    "cases": []
  }
}
```

CLI 只打印摘要，避免把 18 条 Top20 结果全部刷到终端；完整案例保存在 `artifacts/evaluation-*.json`。

---

# 十二、测试如何保证这个小优化不会悄悄扩大

测试位于：

- [test_evaluation.py](../../tests/test_evaluation.py)
- [test_retrieval_policy.py](../../tests/test_retrieval_policy.py)

## 12.1 Evaluation 测试

覆盖：

- Recall@10、Recall@20；
- CODE/DOC hit@10；
- both_sources_hit@10；
- `any_of` 在不同 K 下仍只算一个目标组；
- 三类失败的互斥分类；
- 最终排名和源内候选排名；
- MIXED 源内找到、最终被截断时优先判为 Composition Miss；
- 空结果的 Top10、Top20 诊断。

## 12.2 文档锚点测试

覆盖：

- NFKC 和大小写标准化；
- Java/英文标识符；
- 中文双字片段；
- 只提升重合度最高的一个文档；
- 并列时保持原顺序；
- 没有词项时保持原顺序；
- 不修改 `SearchResult.score`；
- MIXED `top_k=5` 时确实请求 DOCUMENT Top20；
- 最终结果仍不超过 Top5；
- 单侧缺失时不伪造另一来源；
- CODE、DOC 单源策略不改变。

## 12.3 完整验证结果

```text
Python tests：87 / 87 通过
PostgreSQL/pgvector integration：通过
Java Parser Maven verify：通过
Python compileall：通过
git diff --check：通过
MIXED context CLI：通过
```

---

# 十三、如何自己运行并阅读结果

## 13.1 确认环境

需要：

```text
Docker Desktop 已启动
PostgreSQL/pgvector 容器可连接
DASHSCOPE_API_KEY 已在用户环境变量中
数据库中已经完成 Java/Markdown ingest
```

## 13.2 运行评测

```powershell
uv run devcontext evaluate
```

控制台重点查看：

```text
output
benchmark_sha256
bottleneck_analysis.failure_counts
bottleneck_analysis.primary_bottleneck
routed.recall_at_5/10/20
routed.by_type
routed.both_sources_hit_at_5/10
```

## 13.3 打开详细产物

进入：

```text
artifacts/evaluation-时间戳.json
```

重点路径：

```text
strategies[routed].cases
bottleneck_analysis.cases
```

每个失败案例应检查：

```text
question
ground_truth
target_ranks
missing_sources_at_5
failure_type
preliminary_reason
top_results
```

## 13.4 人工查看 Context

例如：

```powershell
uv run devcontext context "支付回调方法的实现在哪里，回调后状态推进和通知是如何设计的？" --top-k 5 --max-chars 2000
```

应当能看到：

```text
Route: MIXED
[C1] CODE
[C2] DOCUMENT
[C3] CODE
[C4] DOCUMENT
...
```

然后检查：

- CODE 是否是正确接口或实现；
- DOCUMENT 是否是目标标题层级；
- 双源是否足以支撑回答；
- Context 是否因为字符预算截断；
- Citation 是否能够定位回源码和文档。

---

# 十四、这次优化没有解决什么

## 14.1 三个真实 Candidate Recall Miss

仍然存在：

```text
CODE-010
DOC-002
DOC-012
```

它们的目标在对应来源 Top20 中不存在，文档锚点提升无法创造一个数据库没有返回的候选。

下一阶段如果继续优化，应该先逐条检查：

### CODE-010

```text
UserRemoteService#listPassengerQueryByIds
```

重点检查：

- Feign 接口的方法 Chunk 是否正确入库；
- annotation、signature 是否进入 keyword_text；
- query 中“用户服务、乘车人 ID 列表、Feign”与代码标识符差距；
- Keyword 和 Vector 各自的原始排名。

### DOC-002

目标是 Feign 移出事务、数据库失败后的边界和补偿说明。重点检查：

- query 的“失败后怎么办”和标题“事务与锁边界”之间的语义跨度；
- `any_of` 的两个等价标题是否都存在于索引；
- 文档 Chunk 是否过长或主题混杂。

### DOC-012

目标是“工程层面的已知不足”。重点检查：

- query 中“工程化不足”与标题 `10.4 工程层面` 的向量相似度；
- 父标题“已知不足与后续改进方向”是否进入 embedding_text；
- 是否需要未来的 Parent Context 或标题权重。

## 14.2 五个 Ranking Miss

它们已经在 Top20 中：

```text
CODE-007
CODE-009
DOC-004
DOC-005
DOC-007
```

这些案例更适合后续研究：

- exact-symbol / class / signature boost；
- CODE 查询的标识符提取；
- DOC heading/title 轻量 rerank；
- 同名方法消歧。

但不应该和本次 MIXED 组合优化混在同一提交里，否则很难判断指标变化来自哪里。

## 14.3 七个剩余 Composition Miss

After 仍有 7 个 MIXED 组合失败：

```text
MIXED-001
MIXED-002
MIXED-003
MIXED-004
MIXED-005
MIXED-006
MIXED-012
```

其中部分问题不是“提升一个 DOCUMENT”能够解决的：

- 有的正确 CODE 在源内第 13 或第 19；
- 有的 CODE、DOC 都需要各自前三名，Top5 容量天然紧张；
- 有的标题词项并不足以唯一识别目标文档；
- 有的需要 Query Rewrite 才能把设计语义对齐到文档标题。

## 14.4 0.80 目标仍未达到

当前：

```text
MIXED both_sources_hit@5 = 0.4167
```

长期目标：

```text
>= 0.80
```

因此这次结果应该描述为：

```text
完成一次可验证、无单源回归的明显改善。
```

不能描述为：

```text
MIXED 检索问题已经解决。
```

---

# 十五、这次实现体现了哪些工程方法

## 15.1 先扩展观测，再修改算法

如果一开始就修改 Retriever，只能看到 Recall@5 变化，却不知道为什么变化。

正确顺序是：

```text
增加 Top10/Top20
→ 保存源内候选
→ 分类失败
→ 判断主瓶颈
→ 只改一个点
→ 同 Benchmark Before/After
```

## 15.2 让失败分类对应不同解决手段

```text
RETRIEVAL_MISS  → 候选生成、Embedding、Chunk、Rewrite
RANKING_MISS    → 元数据、精确匹配、Reranker
COMPOSITION_MISS → 来源配额、锚点、组合策略
```

没有这层分类，团队很容易用错误工具解决错误问题。

## 15.3 每次只改变一个主要变量

本轮没有同时加入：

```text
标题 boost
+ symbol boost
+ Query Rewrite
+ 新 Embedding
+ RRF 调参
```

只增加 MIXED 文档锚点，因此指标提升可以较可信地归因到这个变化。

## 15.4 评测数据不能为了通过而被修改

Benchmark SHA-256 在 Before、After 中完全一致：

```text
b4275a82b8e4d4c6f01d32453adcf141e2bd9a53947964b978880b227702a23b
```

这证明没有通过删除困难用例或放宽 Ground Truth 来制造提升。

## 15.5 相对改善和最终质量目标要分开

相对改善已经通过：

```text
both@5：0.1667 → 0.4167
```

最终绝对目标仍未通过：

```text
0.4167 < 0.80
```

工程报告必须同时保留这两个事实。

---

# 十六、下一步应该怎样继续，而不是立刻上复杂模型

后续建议仍然遵循“先分析失败，再选一个最小方案”。

## 优先级 1：处理 3 个 Candidate Recall Miss

逐条导出 Keyword、Vector 的 Top50 和原始分数，确认：

```text
是索引数据缺失？
是 query 与目标表达不一致？
是目标在 20 之后？
还是 Ground Truth 与当前数据库内容不一致？
```

只有确认确实属于语义召回不足，才考虑 Query Rewrite 或 Embedding/Chunk 调整。

## 优先级 2：处理 5 个 Ranking Miss

可以选择一个独立阶段研究：

```text
CODE exact-symbol metadata boost
```

或者：

```text
DOC heading/title conservative rerank
```

不要两者同时做，以免无法判断收益来源。

## 优先级 3：继续改善 MIXED 组合

剩余案例可以研究：

- 根据 Router 信号动态决定 `3 CODE + 2 DOC` 或 `2 CODE + 3 DOC`；
- 对 CODE、DOC 各选一个高置信锚点，再填充剩余结果；
- 对 query 中明确出现的类名/方法名做 CODE 锚点；
- 只对仍失败的 MIXED 做轻量 Query Rewrite。

在这些简单方案用尽之前，没有必要直接引入 Cross-Encoder 或 GraphRAG。

---

# 十七、最终知识点总结

读完这一阶段，应掌握以下结论。

## 1. Recall@5 不是根因诊断

它只表示最终小窗口是否命中，不能区分召回、排序和组合问题。

## 2. Top10/Top20 是低成本的瓶颈探针

如果 Recall 随 K 明显提高，说明大量 Ground Truth 已经存在于候选中。

## 3. MIXED 必须观察源内候选

最终交错结果会隐藏来源内部第 11～20 名，容易把 Composition Miss 误判成 Retrieval Miss。

## 4. Candidate Recall、Ranking、Composition 要用不同方案

```text
召回不到 ≠ 排名不高 ≠ 双源装不进 Top5
```

## 5. 最小优化应直接对应最大失败类别

10 个 Composition Miss 多于 3 个 Retrieval Miss，因此先做证据组合，而不是先换 Embedding。

## 6. 元数据可以作为低风险的第二信号

文件名、标题和 heading path 往往比大段正文更适合确定文档主题，但应采用保守提升，避免全面重排。

## 7. Before/After 必须使用相同 Benchmark

相同 SHA-256 是结果可比较的前提。

## 8. 一次改善不等于问题结束

`both_sources_hit@5` 从 `0.1667` 提升到 `0.4167` 很有价值，但距离 `0.80` 仍有明显差距。

最终可以用一句话总结这一阶段：

> DevContext 这次不是“尝试了一个看起来合理的检索技巧”，而是先用 Top20 与源内排名证明主要瓶颈是 MIXED Evidence Composition，再用一个只移动单条 DOCUMENT 的确定性锚点规则完成了可归因、无单源回归的真实提升。
