这两个功能最好一起理解，因为它们解决的是同一个问题的两个层面：

> **Query Router V1 决定“这个问题属于哪一类”；Retrieval Policy V1 决定“这一类问题应该怎么查”。**

现在你的系统是：

```text
Query
→ 固定 Hybrid Retrieval
→ Context Builder
→ LLM
```

这意味着不管用户问什么：

```text
“某个方法在哪？”
“为什么这么设计？”
“为什么这样设计，代码在哪里？”
```

都走同一套检索。

而你前面的评测已经说明，这三类问题的检索需求明显不同，所以现在才有必要加 Router 和 Policy。

---

# 一、Query Router V1 是干什么的

它的职责非常简单：

> **判断用户问题更偏 CODE、DOC，还是 MIXED。**

输出只有三类：

```text
CODE
DOC
MIXED
```

例如：

```text
OrderServiceImpl.createTicketOrder 在哪里实现？
→ CODE
```

```text
为什么订单超时关闭要设计双通道？
→ DOC
```

```text
为什么订单关闭这样设计，对应代码在哪里？
→ MIXED
```

所以 Router 本质是在做：

```text
Query
↓
理解用户想要哪类证据
↓
QueryType
```

---

# 二、为什么要做 Router

因为现在固定 Hybrid 有一个明显问题：

> Hybrid 只是在混合 Keyword 和 Vector，它并不知道“用户到底需要代码、文档，还是两者都要”。

例如 MIXED 问题：

```text
为什么这样设计？代码怎么实现？
```

它真正需要的是：

```text
至少一个 CODE
+
至少一个 DOCUMENT
```

但固定 Hybrid 可能返回：

```text
CODE
CODE
CODE
CODE
CODE
```

或者：

```text
DOC
DOC
DOC
DOC
DOC
```

数学上的 Top5 可能没问题，但业务上证据不完整。

所以 Router 的业务价值是：

> **先识别用户的证据需求。**

---

# 三、Router V1 不应该做什么

Router 不应该自己：

```text
检索数据库
调用 Context Builder
调用 LLM 回答
重写 Query
重新排序 Chunk
```

它只负责一个判断：

```python
route(query) -> QueryType
```

这样职责才干净。

---

# 四、Router V1 为什么以规则为主，而不是 LLM 为主

第一版不应该为每一次分类都调一次大模型。

因为 CODE/DOC/MIXED 很多特征都比较明显。

例如 CODE：

```text
类名
方法名
接口名
xxx()
@Transactional
“在哪里实现”
“哪个类”
“代码位置”
```

DOC：

```text
为什么
设计原因
流程
架构
部署
原理
考虑
目的
```

MIXED：

```text
为什么 + 代码
设计 + 实现
原因 + 哪个类
流程 + 对应源码
```

所以 V1 用规则已经足够。

而且更容易测试：

```text
输入固定 Query
→ 输出稳定
```

不会因为 LLM 波动导致路由不稳定。

## 实现修正：规则为主 + LLM 兜底

> 本节的原始结论是“V1 只用规则，不引入 LLM”。**实现时做了一处扩展**，原因和边界如下。

规则能覆盖绝大多数问题，但**总有规则完全无法判断的模糊问题**：

```text
帮我看看这个功能目前到底怎么样
```

这句话既没有代码标识符，也没有设计类词汇，规则无从下手。如果直接给一个保守默认值，等于放弃了分类。

因此实现的策略是：

```text
规则命中        → 直接使用规则结果（不调用 LLM）
规则完全无信号  → 才调用一次 LLM 做分类
LLM 失败或非法  → 安全降级为 MIXED
```

关键约束：**LLM 只是"规则没覆盖时"的兜底，不是主路径。** 在 36 条 Benchmark 上的实际分布是：

```text
rules:    36
llm:       0
fallback:  0
```

也就是说，标定集里一次 LLM 请求都没发生。这次调用只出现在真正的歧义问题上。

### 实现修正：Router 的 max_tokens 必须给够

计划最初给 Router 设的是 `max_tokens=16`——逻辑上很合理，回答只需要一个词。

**但真实请求持续返回 `finish_reason=length`。** 原因与答案生成层遇到的是同一个：DeepSeek 的思考模式会先生成内部推理内容，再输出可见回答，**两者共享同一个 token 预算**。预算太小，推理还没结束就被截断，没有可见输出。

实测过程：

```text
16   → 持续 finish_reason=length
64   → 仍被截断
128  → 仍被截断
256  → 仍被截断
512  → 不稳定
1024 → 稳定
```

最终 Router 使用 **`max_tokens=1024`**。

**注意这里放宽的只是“允许模型想多久”，没有放宽“允许模型答什么”。** 输出解析依然严格——只接受完整的单个大写标签，任何多余内容都降级。

---

# 五、Router 实际采用的数据结构

> 本节按最终实现给出。比原计划多了两个字段，原因见下。

三个类型（`src/devcontext/routing/router.py`）：

```python
class QueryType(str, Enum):
    CODE = "CODE"
    DOC = "DOC"
    MIXED = "MIXED"


class DecisionSource(str, Enum):
    RULES = "rules"        # 规则命中
    LLM = "llm"            # 规则无信号，LLM 分类成功
    FALLBACK = "fallback"  # LLM 不可用或输出非法，兜底


@dataclass(frozen=True, slots=True)
class RouteDecision:
    query_type: QueryType
    decision_source: DecisionSource
    reason: str
    code_signals: tuple[str, ...] = ()
    doc_signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        ...
```

例如：

```text
Query:
“为什么 createTicketOrder 要在事务提交后投递消息？”

Route:
MIXED

Decision source:
rules

Reason:
matched both CODE and DOC rule signals

Code signals:
("qualified_java_symbol", "multi_word_camel_case")

Doc signals:
("why_or_reason", "design")
```

## 三处与原计划的差异及理由

**差异一：枚举值改为大写，并继承 `str`。**

原计划写的是 `CODE = "code"`。实现改为 `CODE = "CODE"` 并继承 `str`，理由是与数据库中 `source_type` 的取值风格保持一致（那里存的就是 `"CODE"` / `"DOCUMENT"`），减少大小写转换的心智负担；继承 `str` 则让它可以直接参与 JSON 序列化。

**差异二：新增 `decision_source`（重要）。**

原计划只有 `query_type` 和 `reason`。这会导致一个问题：**看到 `Route: DOC` 时无法知道这个判断是谁做的。**

```text
rules    → 确定性、可复现、可解释
llm      → 依赖模型、可能波动
fallback → 其实没判断出来，只是给了个默认值
```

三者的可信度完全不同。CLI 会把来源显示出来：

```text
Route: DOC (rules)
Route: DOC (llm)
Route: MIXED (fallback)
```

评测报告也会统计三者的分布，用于判断"规则覆盖了多少"。

**差异三：新增 `code_signals` / `doc_signals`。**

记录**命中了哪些具名信号**（而不是分数）。这是排查路由误判最直接的工具——你能一眼看出"为什么它被判成 CODE"。

另外 `frozen=True` 让两个判断结果可以直接用 `==` 比较，从而支持"相同输入 → 完全相同输出"的可复现性断言。

`reason` 保留下来，仍然很适合调试和面试演示。

---

# 六、判定逻辑：具名信号，而不是分数

> 原计划建议用 `code_score` / `doc_score` 两个计数器。**实现改为“信号是否存在”，不做打分。** 理由见下。

第一组：**CODE 信号**（9 条）。

```text
qualified_java_symbol             Java 成员表达式：OrderServiceImpl.createTicketOrder
leading_lower_camel_identifier    位于问题开头的 lowerCamelCase：purchaseTickets 在哪里
multi_word_camel_case             多单词 CamelCase 类名：AuthGlobalFilter
java_annotation                   注解：@Transactional
java_file                         .java
code_or_source                    “代码”“源码”
implementation_location           “如何实现”“具体实现”“实现在哪里”
code_locator                      “在哪里”“哪个类”“由哪个类执行”“在哪生成”
code_construct                    “业务方法”“接口方法”“责任链 handler”
```

第二组：**DOC 信号**（6 条）。

```text
why_or_reason                     “为什么”“为何”“原因”
design                            “设计”“依据”“架构”“原理”“目的”“机制”“取舍”
flow_or_structure                 “文档”“流程”“链路”“状态机”“职责”“边界”“数据归属”
explanation_operation             “如何区分”“如何防护”“如何补偿”“如何推进”
configuration                     “关键配置”“配置开关”
known_gap                         “已知不足”“工程化方面”
```

判定：

```text
命中 CODE 信号 && 命中 DOC 信号   → MIXED
只命中 CODE 信号                  → CODE
只命中 DOC 信号                   → DOC
一条都没命中                      → 交给 LLM 兜底（见第四节）
```

## 为什么不用分数

打分需要阈值，而阈值需要标定数据：

```text
code_score = 2 算 CODE 吗？
“为什么 AuthGlobalFilter 这样设计” → code=3, doc=2
    按分数 → CODE
    按需求 → MIXED（用户既要设计原因，也要这个类）
```

**“是否出现”是客观的，“多少分”是主观的。** 选择前者换来三个好处：

```text
1. 确定性   相同输入永远相同输出
2. 可解释   code_signals 直接告诉你为什么
3. 可测试   不需要调参，不需要标定
```

代价是有时会**过度触发 MIXED**。但这个偏差方向是安全的：MIXED 会同时检索两类来源，最多是多余，不会缺失。

## 两个必须注意的误判边界

规则路由最容易出错的地方是**领域术语被误当成代码标识符**。实现里有两个针对性约束，都是为了修这个问题。

**边界一：`orderSn` 不应该触发 CODE。**

```text
Ticket 为什么预生成 orderSn，Order 为什么仍需要唯一索引？
```

这是在问设计原因，属于 DOC。之所以没被误判，是因为两条 CamelCase 正则都有额外约束：

```python
# 要求 lowerCamelCase 出现在问题开头（注意 ^ 和结尾的 \s+）
leading_lower_camel_identifier = re.compile(r"^\s*[a-z_$][A-Za-z0-9_$]*[A-Z][A-Za-z0-9_$]*\s+")

# 要求至少两个“首字母大写的词”（注意末尾的 +）
multi_word_camel_case = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")
```

逐个检查：

```text
orderSn  → 不在问题开头，不匹配第 1 条
         → 以小写开头，不匹配第 2 条
Ticket   → 单个首字母大写的词，第 2 条要求后面还有 [A-Z]，不匹配
Order    → 同上，不匹配
```

最终 `code_signals == ()`，只剩两个 DOC 信号 → **DOC**。

**这里的经验是：正则里的锚点（`^`）和重复次数要求（`+`）本身就是在表达业务判断，不是可有可无的修饰。**

**边界二：`AuthGlobalFilter` 应该触发 CODE。**

单个词，但是多单词 CamelCase（`Auth` + `Global` + `Filter`），第 2 条正则会命中。

**这也说明为什么两条 CamelCase 正则都要保留**：

```text
leading_lower_camel_identifier  → 认方法式命名：purchaseTickets
multi_word_camel_case           → 认类式命名：  AuthGlobalFilter
```

单靠任何一条都会漏掉一半。

V1 不做复杂 NLP。

---

# 七、Retrieval Policy V1 又是什么

Router 只是说：

```text
“这是 CODE”
```

但接下来还需要有人决定：

> **CODE 问题到底怎么查？**

这就是 Retrieval Policy。

也就是：

```text
QueryType
↓
选择 Retrieval Strategy
↓
SearchResult[]
```

所以二者关系是：

```text
Query
↓
Router
↓
CODE / DOC / MIXED
↓
Retrieval Policy
↓
具体检索方式
```

---

# 八、为什么不能只有 Router，没有 Policy

假设 Router 判断：

```text
CODE
```

但系统后面依旧无脑：

```text
search("hybrid")
```

那 Router 实际上没有改变任何行为。

所以真正产生价值的是：

```text
CODE → 一种检索策略
DOC → 一种检索策略
MIXED → 一种检索策略
```

这就是 Policy。

---

# 九、Retrieval Policy V1 实际采用的策略

> 本节按最终实现给出。与计划的主要差别在 MIXED 那一档（用 Vector 而非 Hybrid），理由见下。

```text
CODE   → hybrid  + 只检索 CODE
DOC    → vector  + 只检索 DOCUMENT
MIXED  → 两次 vector（分别只检索 CODE / DOCUMENT）→ 交错合并
```

这里最值得优先改的是 MIXED。

因为你已经有明确数据表明：

```text
both_sources_hit@5 = 0
```

所以 Policy V1 最应该解决：

> **MIXED 查询必须尽量拿到两种来源。**

## 为什么三档是合理的

**CODE → Hybrid + 只取代码。** 代码类问题有两种典型形式，正好对应 Hybrid 的两路：标识符精确匹配（`orderSn 在哪里生成？`）依赖 Keyword，语义描述（`订单创建的事务流程是什么`）依赖 Vector。所以 CODE 保留 Hybrid，但限定只搜代码。

**DOC → Vector + 只取文档。** 文档类问题几乎都是自然语言描述（`为什么支付事实和通知进度要分开保存？`），最怕的是文档里压根没有"分开保存"这几个字。Vector 正是解决"用词不同但意思相近"的。

**MIXED → 两次 Vector 分头检索。** 核心改动，见第十节与第十一节。

---

# 十、MIXED Policy 的最小实现

可以这样：

```text
MIXED Query
   ↓
现有 Hybrid Retrieval 取一批候选
   ↓
从候选里挑：
最高排名 CODE
+
最高排名 DOCUMENT
   ↓
再补剩余高排名结果
   ↓
SearchResult[]
```

但你当前 Context Builder 已经有“保留双源锚点”的逻辑。

如果 Retriever Top10 本身就有两种来源，这种方案已经够。

问题在于：

```text
有时 Top10 全是 DOC
```

那 Context Builder 也没法救。

所以更进一步的最小 Policy 可以是：

```text
MIXED
↓
跑一次 CODE-oriented retrieval
+
跑一次 DOC-oriented retrieval
↓
合并
↓
去重
↓
交给 Context Builder
```

这才真正能提高双来源覆盖。

## 实现选择

**实现采用了后一种（两次独立检索 + 合并）。** 因为第一种方案无法解决"Top10 全是 DOC"这个关键情形——而那正是 `both_sources_hit@5 = 0` 的直接原因。

具体用的是 `vector` 而非 `hybrid`，以及"过滤下推到 SQL""交错而非拼接"等细节，见第十一节。

---

# 十一、CODE-oriented retrieval 实际是怎么做的

不需要新写一个搜索引擎，复用现有能力加一个**来源过滤**即可。

## 关键修正：过滤必须下推到 SQL

原计划写的是：

```text
Hybrid Retrieval top20
↓
只保留 source_type = CODE
↓
取 top3
```

**这个做法有一个隐藏缺陷**：如果 Top-20 里只有 1 条是代码，那么"取 top3"根本凑不出 3 条——而且这种缺失是**静默的**，很难察觉。

实现改为把过滤条件下推到数据库：

```sql
... AND source_type = %s
```

```text
正确做法：SQL 里带 WHERE source_type = 'CODE' → 数据库直接返回 Top-3 代码
```

这样无论文档的匹配度多高，代码那一侧都能拿到属于自己的 Top-N。

实现要点：

- 过滤条件是**参数化**的（`%s` 占位符），不是字符串拼接，避免 SQL 注入
- 另有一层白名单校验：`source_type` 只能是 `None` / `"CODE"` / `"DOCUMENT"`
- `source_type` 是**关键字参数**（`*` 之后），强制调用方写出参数名，避免位置参数传错

## 两路怎么合并：交错，不是拼接

```text
CODE-1
DOCUMENT-1
CODE-2
DOCUMENT-2
CODE-3
...
```

即 `code[0], doc[0], code[1], doc[1], ...` 依次取出，**先 CODE 后 DOCUMENT**。按 `id` 去重。

三个必须明确的规则：

**缺来源时不伪造。** 如果数据库里没有 DOCUMENT，就只返回 CODE。绝不补查、绝不生成虚假结果。

**`top_k=1` 时返回 CODE。** 因为取出顺序是先 CODE。

**重复 ID 会被跳过。** 两路来自同一张表，跨来源出现同 ID 时会自动去重。

## 为什么 MIXED 用 Vector 而不是 Hybrid

原计划建议两路都用 Hybrid。实现改成了 `vector`，理由是**成本**：

```text
两次 Hybrid = 2 次 Keyword + 2 次 Vector + 2 次 RRF
两次 Vector = 2 次 Vector
```

关键在于：**一旦按来源拆开检索，RRF 融合的意义就被削弱了**——RRF 的价值在于融合不同信号，而当两路已经明确对应两类来源时，直接交错更简单、更快。

而且来源过滤已经下推到 SQL，所以"只搜代码"这件事在数据库层就完成了，不需要先取 20 条再在应用层筛掉一半。

当然更高级的可以：

```text
CODE → Keyword-heavy
DOC → Vector-heavy
```

但这属于 V1 之后的调优。

---

# 十二、为什么 Router 和 Policy 要分开

这是很重要的设计思想。

Router 负责：

> **“用户需要什么？”**

Policy 负责：

> **“系统怎么拿到它？”**

比如：

```text
Router:
这是 MIXED
```

Policy：

```text
那我分别检索 CODE 和 DOCUMENT
```

以后你想换策略：

```text
MIXED
→ 两路 Hybrid
```

改成：

```text
MIXED
→ Keyword CODE + Vector DOC
```

Router 完全不用改。

这就是解耦。

---

# 十三、它们在完整流程里的位置

现在：

```text
Query
  ↓
固定 Hybrid
  ↓
Context Builder
  ↓
LLM
```

加完后：

```text
Query
  ↓
Query Router
  ↓
CODE / DOC / MIXED
  ↓
Retrieval Policy
  ↓
SearchResult[]
  ↓
Context Builder
  ↓
LLM
```

这时系统第一次有了：

> **根据问题动态改变检索行为**

这已经开始接近 Agent 的“决策能力”。

---

# 十四、举三个完整例子

## 例 1：CODE

```text
Query:
OrderServiceImpl.createTicketOrder 在哪里？
```

Router：

```text
CODE
```

Policy：

```text
偏代码检索
```

结果：

```text
OrderServiceImpl#createTicketOrder
OrderRemoteService#createTicketOrder
...
```

Context Builder：

```text
主要保留 CODE
```

---

## 例 2：DOC

```text
Query:
为什么订单超时关闭要使用延迟队列和定时任务双通道？
```

Router：

```text
DOC
```

Policy：

```text
偏文档检索
```

结果：

```text
订单设计文档
超时关单章节
幂等说明
```

不需要强塞大量 Java。

---

## 例 3：MIXED

```text
Query:
订单超时关闭为什么这样设计，代码在哪里？
```

Router：

```text
MIXED
```

Policy：

```text
必须同时找 CODE + DOCUMENT
```

最终：

```text
CODE:
OrderServiceImpl.closeOrder

DOC:
订单与支付模块 > 超时关单双通道
```

这才是符合用户需求的 Context。

---

# 十五、Router V1 的测试重点

你现在已有 Benchmark：

```text
12 CODE
12 DOC
12 MIXED
合计 36（其中 12 条为 legacy）
```

直接就能用。

测试：

```text
Benchmark.type
vs
Router预测类型
```

得到：

```text
Route Accuracy
```

并且会额外输出：

```text
by_type            分类型准确率
decision_sources   rules / llm / fallback 各多少条
confusion_matrix   期望类型 × 预测类型 的次数矩阵
cases              逐条明细（含命中的 signals 与 reason）
```

实际结果（`artifacts/evaluation-20260924-191240.json`）：

```text
Router Accuracy: 1.0000
  CODE  Accuracy: 1.0000
  DOC   Accuracy: 1.0000
  MIXED Accuracy: 1.0000

Decision sources:
  rules:    36
  llm:       0
  fallback:  0
```

混淆矩阵完全对角——36 条全部由确定性规则正确处理，没有误判，也没有一次需要 LLM。

第一版不需要复杂 F1。

## 一个必须知道的评测细节：评测不传 LLM 工厂

构造 Router 时用的是**裸构造**：

```python
router = QueryRouter()          # llm_client_factory = None
```

这意味着评测里**不会调用任何 LLM**：

```text
规则命中     → 用规则结果
规则没命中   → 直接 fallback → MIXED (fallback)
```

三个好处：

```text
1. 评测完全确定性、可复现（不依赖模型版本）
2. 不需要 API Key 就能跑评测
3. 不花钱
```

但也要清楚它的**局限**：

> **`router_accuracy = 1.0` 衡量的是“规则层”的准确率，不包含 LLM fallback 的准确率。**

在 36 条上规则命中率是 100%（`llm: 0`），所以 fallback 路径在这个 Benchmark 上**从未被触发**，其准确率是未知的。

另外有一个测试专门保证这个前提始终成立：

```python
def test_checked_in_benchmark_is_classified_by_rules() -> None:
    cases = load_cases(PROJECT_ROOT / "benchmark" / "cases.jsonl")
    decisions = [QueryRouter().route(case["question"]) for case in cases]

    assert all(d.decision_source is DecisionSource.RULES for d in decisions)
    assert [d.query_type.value for d in decisions] == [case["type"] for case in cases]
```

如果哪天有人往 Benchmark 里加了一条规则处理不了的问题，这个测试会立刻失败——而不是等到跑完整评测才发现 `decision_sources` 里混进了 `fallback`。

---

# 十六、Retrieval Policy 的测试重点

Policy 不能只看 Router 分类正确率。

真正要看：

```text
用了 Router + Policy 之后
检索质量有没有提升
```

评测因此从 3 种策略扩展为 4 种：

```text
keyword
vector
hybrid     ← 原来的基线
routed     ← Router + Policy（本轮新增）
```

重点是同一份报告里 **Hybrid 与 Routed 的直接对比**：

```text
CODE  Recall@5
DOC   Recall@5
MIXED Recall@5
both_sources_hit@5
average_latency_ms
```

尤其：

```text
both_sources_hit@5
```

这是最核心的。

---

# 十七、怎样判断这一步做成功了

不是：

```text
Router Accuracy = 100%
```

就算成功。

而是：

```text
Router 能较稳定分类
+
Policy 让对应类型的检索结果更符合需求
```

尤其应该看到：

```text
MIXED both_sources_hit@5
从 0
明显提升
```

同时不要把：

```text
CODE Recall
DOC Recall
```

大幅拉低。

## 实现做法：两组独立门禁

实现把上面这几条落成了两组**判据完全不同**的门禁。

**第一组：`policy_acceptance`——这次改动有没有让东西变好**

```text
router_accuracy                                    >= 0.9
routed_CODE_recall_at_5   >= hybrid_CODE_recall_at_5
routed_DOC_recall_at_5    >= hybrid_DOC_recall_at_5
routed_MIXED_recall_at_5  >= hybrid_MIXED_recall_at_5
routed_both_sources_hit_at_5  >  hybrid_both_sources_hit_at_5
```

输出 `policy_improvement_passed`。

**第二组：`acceptance`——系统达到目标了没有**

```text
case_count                         == 36
balanced_case_distribution         {CODE:12, DOC:12, MIXED:12}
legacy_case_count                  == 12
legacy_hybrid_recall_at_5          >= 1.0
mixed_hybrid_both_sources_hit_at_5 >= 0.8
hybrid_recall_at_5_vs_baseline     >= baseline
```

输出 `quality_passed`。

## 两组门禁的差别

| | policy_acceptance | acceptance |
|---|---|---|
| 问的问题 | 这次改动**有没有让东西变好** | 系统**达到目标了没有** |
| 判据形式 | **相对**（after ≥ before） | **绝对**（≥ 0.8、≥ 1.0） |
| 会因存量问题失败吗 | 不会 | 会 |

**为什么必须分开？** 如果只报前者，很容易造成"项目已经做好了"的错觉。前者证明"这一步有效"，后者说明"离目标还有多远"，两个问题都值得回答。

## 一个细节：最后一条门禁用 `>` 而不是 `>=`

```text
routed_both_sources_hit_at_5  >  hybrid_both_sources_hit_at_5
```

因为 `both_sources_hit@5` 的基线是 `0.0`。如果只要求"不低于"，那么"还是 0"也会通过——**这等于什么都没证明**。必须严格大于，才能说明这个功能真的起了作用。

**门禁的严格程度要跟着指标的语义走，不能机械地统一用 `>=`。**

## 另一个细节：绝对目标锚定在基线策略上

`mixed_hybrid_both_sources_hit_at_5` 检查的是 **`hybrid`** 的值，不是 `routed` 的。

这是有意的——**引入新策略不会让绝对质量目标自动达标**。本轮的 `routed` 把双源命中率提升到了 `0.1667`，但这个门禁看的是 `hybrid` 的 `0.0`，所以它**仍然是 FAIL**。这正是下面第十八节要说明的情况。

---

# 十八、这一轮不做什么

原计划明确列出本轮不要同时加：

```text
LLM Router
Query Rewrite
Retry
Context Sufficiency Judge
LangGraph
Reranker
复杂权重调参
```

否则很难知道：

> 最后提升到底是哪一步带来的。

这轮只验证一个假设：

> **不同类型 Query 使用不同 Retrieval Policy，是否比固定 Hybrid 更好。**

## 实现修正：加的是“兜底”，不是“LLM Router”

本轮**确实引入了 LLM 分类**，但必须是“兜底”而不是“主路径”，否则会破坏上面的归因原则。两者的区别：

| | LLM Router（原计划排除） | LLM Fallback（实际实现） |
|---|---|---|
| LLM 何时被调用 | 每个问题都调，或与规则并行 | **只在规则零信号时**调用 |
| 对评测的影响 | 评测结果依赖模型，不可复现 | 评测中一次都没触发，结果完全确定 |
| 失败时 | 需要额外处理 | 降级 MIXED，不阻塞检索 |

实际数据支持这一点——36 条 Benchmark 的决策来源分布：

```text
rules:    36
llm:       0
fallback:  0
```

**LLM 一次都没被调用。** 所以这一步的评测结论仍然完全归因于"Router 规则 + Retrieval Policy"，没有被模型的不确定性污染。

需要诚实记录的边界：**这也意味着 LLM fallback 本身的准确率在这个 Benchmark 上是未被测量的**。要衡量它，需要专门构造一批"无规则信号"的问题作为独立测试集。

另外，`Query Rewrite`、`Retry`、`Context Sufficiency Judge`、`LangGraph`、`Reranker`、`复杂权重调参` **本轮均未实现**，与原计划一致。

## 实现修正：延迟统计与一个已知代价

MIXED 跑两次检索，耗时统计需要合并。实现把两段的分项耗时相加得到 `stage_total`，再与整段墙钟时间 `measured_total` 取 `max`：

```text
stage_total    = 各段相加      → 可能低估（漏掉阶段之间的开销）
measured_total = 真实墙钟
max(...)       = 不低估的那个
```

取 `max` 保证上报耗时不会低于真实值。

**一个已知代价**：MIXED 会用同一条 query 调两次 embedding，产生两个完全相同的查询向量。测试里把这个行为固定下来了：

```python
assert execution.timings.query_embedding_ms == 4.0        # 2.0 + 2.0
```

这是当前明确的 TODO。之所以没有立刻修，是因为它需要把"查询向量"作为参数在 `RetrievalService` 接口里传递、会改变现有方法签名——而本轮的目标是**先测出收益，再优化实现**。

---

# 十九、可以把这两个功能理解成 Agent 的第一层“决策”

现在系统：

```text
所有问题
→ 同一路径
```

做完后：

```text
先判断问题
→ 再决定检索方式
```

这已经不是单纯 RAG 了。

它开始有：

```text
感知问题
→ 做决策
→ 执行动作
```

虽然还没有循环和 Retry，但已经是 Agent 化的第一步。

---

# 二十、一句话记忆

你可以这样记：

```text
Query Router
= 判断用户“需要什么类型的证据”
```

```text
Retrieval Policy
= 决定“这种证据应该怎么找”
```

两个组合起来就是：

> **先理解问题，再选择检索策略。**

而你当前项目下一阶段最重要的目标，就是用这两个功能解决已经被评测明确暴露出来的：

> **固定 Hybrid 对 CODE / DOC / MIXED 一视同仁，尤其 MIXED 无法稳定同时覆盖代码和文档证据。**

---

# 二十一、实测结果（本节为事后补充）

> 以下数据来自实际评测产物 `artifacts/evaluation-20260924-191240.json`（本地忽略文件，未进入 Git）。本节记录"假设是否被验证"，不属于原计划的预测内容。

## 21.1 路由准确率

```text
Router Accuracy: 1.0000
  CODE  Accuracy: 1.0000
  DOC   Accuracy: 1.0000
  MIXED Accuracy: 1.0000

Decision sources:
  rules:    36
  llm:       0
  fallback:  0
```

混淆矩阵完全对角。

## 21.2 四种策略完整对比

| Strategy | Recall@3 | Recall@5 | MRR | 平均延迟 |
|---|---:|---:|---:|---:|
| keyword | 0.2222 | 0.2222 | 0.2037 | 409.1 ms |
| vector | 0.3056 | 0.4722 | 0.3730 | 560.5 ms |
| hybrid | 0.2917 | 0.4722 | 0.3764 | 952.8 ms |
| **routed** | **0.4583** | **0.6250** | **0.4564** | **759.3 ms** |

**`routed` 在三个质量指标上全面领先，而且比 `hybrid` 更快。**

这里有一个值得单独留意的观察：

```text
hybrid 的 Recall@5 = 0.4722，与 vector 完全相同
hybrid 却多花了 392 ms（952.8 vs 560.5）
```

也就是说，**在这个语料上 Hybrid 相对纯 Vector 没有带来 Recall 收益，只带来了延迟成本**。"混合检索一定更好"本身是一个需要验证的假设。

## 21.3 Hybrid vs Routed 前后对比

| 指标 | 原 Hybrid | Router + Policy | 差值 |
|---|---:|---:|---:|
| Overall Recall@5 | 0.4722 | 0.6250 | **+0.1528** |
| CODE Recall@5 | 0.6667 | 0.7500 | +0.0833 |
| DOC Recall@5 | 0.4167 | 0.5833 | +0.1667 |
| MIXED Recall@5 | 0.3333 | 0.5417 | **+0.2083** |
| **MIXED both_sources_hit@5** | **0.0000** | **0.1667** | **+0.1667** |
| 平均检索延迟 | 952.8 ms | 759.3 ms | **−193.5 ms** |

三点解读：

**MIXED Recall@5 提升最大（+0.2083）。** 符合设计预期——这正是判断最需要改的那一类。

**`both_sources_hit@5` 从 0 变成 0.1667。** 从"一条都没有"变成"12 条里有 2 条"。

**延迟反而降低 193.5 ms。** 看起来反常识——MIXED 明明跑两次检索。原因是：Hybrid 本身要跑 Keyword + Vector + RRF 三件事，而 Routed 里 CODE 只搜代码、DOC 只做单次 Vector、MIXED 只是两次 Vector；加上来源过滤把候选集缩小了，SQL 扫描量下降，净效果是变快。

## 21.4 五道 Policy 门禁全部通过

```text
PASS  router_accuracy                                    current=1.0      target=0.9
PASS  routed_code_recall_at_5_not_below_hybrid           current=0.75     target=0.6667
PASS  routed_doc_recall_at_5_not_below_hybrid            current=0.5833   target=0.4167
PASS  routed_mixed_recall_at_5_not_below_hybrid          current=0.5417   target=0.3333
PASS  routed_both_sources_hit_at_5_improves_hybrid       current=0.1667   target=> 0.0

policy_improvement_passed = true
```

**本轮假设被验证**：

> 不同类型 Query 使用不同 Retrieval Policy，确实比固定 Hybrid 更好。

## 21.5 但 `quality_passed` 仍然是 false

真实产物中的绝对质量门禁：

```text
PASS  case_count                              current=36                          target=36
PASS  balanced_case_distribution              current={CODE:12,DOC:12,MIXED:12}   target=同
PASS  legacy_case_count                       current=12                          target=12
FAIL  legacy_hybrid_recall_at_5               current=0.5833                      target=1.0
FAIL  mixed_hybrid_both_sources_hit_at_5      current=0.0                         target=0.8
PASS  hybrid_recall_at_5_vs_baseline          current=0.4722                      target=0.4722

quality_passed = false
```

两个失败项**都不是本轮引入的**：

```text
legacy_hybrid_recall_at_5
  → 衡量旧 hybrid 策略在 legacy 12 条上的表现，目标 1.0，实际 0.5833（历史遗留）

mixed_hybrid_both_sources_hit_at_5
  → 衡量旧 hybrid 策略在 MIXED 上的双源命中率，目标 0.8，实际 0.0
  → 这正是本轮要解决的问题，但本轮改的是 routed 策略，没有动 hybrid
```

而且即使看新指标：

```text
routed both_sources_hit@5 = 0.1667
理想目标                  = 0.8
```

从 `0` 到 `0.1667` 是**方向正确的改善**，但**距离目标还差 4.8 倍**。

**没有做的事**（这一点比数字本身更重要）：

```text
没有修改 Benchmark
没有放宽 ground truth
没有调整基础检索权重
```

**没有为了让 `quality_passed` 变成 true 而动任何手脚。** 一个诚实的 false 准确指出了下一步该做什么，比一个注水的 true 有价值得多。

## 21.6 本轮结论

```text
policy_improvement_passed = true
  → "这次改动有效吗？"     → 有，五个维度全部改善

quality_passed = false
  → "系统达到目标了吗？"   → 没有，历史目标仍未达成
```

两个问题的答案都要如实报告。**证明了"这一步有效"，不等于"项目已经做好"。**