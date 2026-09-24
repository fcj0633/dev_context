这一步可以理解为：**把你已经准备好的 `ContextBundle` 真正交给大模型，让它“只根据这些证据回答”，并把答案和证据引用绑定起来。**

它在整个流程里的位置是：

```text
用户问题
   ↓
Retrieval
   ↓
Context Builder
   ↓
ContextBundle
   ↓
LLM Answer + Citation   ← 现在这一步
   ↓
最终回答
```

核心不是“调用一次大模型 API”，而是设计一层 **受证据约束的回答生成层**。

---

## 一、这一步为什么需要单独设计

你现在已经有：

```text
ContextBundle
├── C1 Java代码
├── C2 文档
├── C3 Java代码
└── rendered_text
```

例如：

```text
[C1] CODE
File: PurchaseTicketTxService.java
Symbol: doPurchaseInTransaction
Lines: 69-118

<代码>

[C2] DOCUMENT
File: 事务设计.md
Heading: 购票流程 > 事务边界

<设计说明>
```

但这还只是“资料”。

真正用户想要的是：

> “所以购票事务到底是怎么实现的？”

因此需要下一层：

```text
资料
↓
理解
↓
组织
↓
生成自然语言回答
```

这就是 Answer Generator。

---

# 二、这一层最重要的设计思想：Grounded Generation

不要把它理解成：

> “让 LLM 根据自己的知识回答。”

而应该理解成：

> **让 LLM 只根据当前项目检索出来的 Context 回答。**

例如用户问：

```text
购票事务在哪里执行，为什么这样设计？
```

模型看到：

```text
[C1] 事务方法源码
[C2] 事务设计文档
```

那么回答应该类似：

```text
购票核心事务由 PurchaseTicketTxService#doPurchaseInTransaction 执行 [C1]。

从设计文档来看，将核心事务集中在这里主要是为了缩小事务边界，
避免远程调用等操作长时间占用数据库事务 [C2]。
```

这里所有关键结论都有来源。

这叫：

> **Grounded Answer / Grounded Generation**

---

# 三、为什么 Citation 必须和 Answer 一起做

你现在已经有：

```text
[C1]
[C2]
[C3]
```

如果 LLM 只返回：

```text
事务由 PurchaseTicketTxService 执行。
```

虽然可能是对的，但用户不知道：

> “你依据什么说的？”

所以要求模型输出：

```text
事务由 PurchaseTicketTxService#doPurchaseInTransaction 执行 [C1]。
```

这样：

```text
[C1]
```

就可以映射回：

```text
src/.../PurchaseTicketTxService.java
Lines 69-118
```

所以 Citation 的作用是：

> **让回答中的结论可以追溯到项目真实代码或文档。**

这也是你这个项目区别于普通 ChatBot 的重要地方。

---

# 四、这一步真正需要实现哪些功能

我建议 V1 只实现 5 个核心能力。

## 1. Answer Generator

新增一个专门负责生成答案的组件。

例如：

```python
generator.generate(
    query,
    context_bundle
)
```

输入：

```text
用户问题
+
ContextBundle
```

输出：

```text
AnswerResult
```

---

## 2. Prompt 构造

Prompt 需要告诉模型：

```text
你是项目代码分析助手。

只能根据提供的 Context 回答。

不要使用 Context 中没有出现的项目事实。

关键结论必须标注 [C1] [C2] 等引用。

如果 Context 不足以支持回答，
明确说明信息不足，不要猜测。
```

然后把：

```text
Question
ContextBundle.rendered_text
```

一起发送给模型。

这一部分其实比“调 API”更重要。

---

# 五、为什么要禁止模型自由发挥

例如当前 Context 只有：

```text
[C1] 文档：订单关闭设计
```

没有任何代码。

用户问：

```text
订单关闭代码在哪里实现？
```

如果没有约束，模型可能凭经验猜：

```text
可能由 OrderServiceImpl.closeOrder() 实现。
```

这就是幻觉。

正确行为应该是：

```text
当前上下文提供了订单关闭的设计说明 [C1]，
但没有包含对应 Java 实现，因此无法可靠指出具体实现类和方法。
```

所以这一步需要一个非常关键的能力：

> **证据不足时不编答案。**

---

# 六、建议设计一个 `AnswerResult`

例如：

```python
@dataclass
class AnswerResult:
    answer: str
    used_citations: list[str]
```

例如：

```text
answer:
购票事务由 ... 执行 [C1]，设计原因是 ... [C2]。

used_citations:
["C1", "C2"]
```

以后可以再扩展：

```text
model
latency
token_usage
```

但 V1 先不要。

---

# 七、还需要一个 LLM Client 抽象

不要让业务代码直接写死：

```text
DeepSeek
OpenAI
百炼
```

可以简单设计：

```python
class LLMClient:
    def generate(self, messages) -> str:
        ...
```

然后：

```text
AnswerGenerator
      ↓
LLMClient
      ↓
具体模型
```

作用是：

> 以后换模型，不需要改 Answer Generator。

但这个抽象不要设计太复杂，一层就够。

---

# 八、Citation 怎么校验

V1 不需要做复杂语义验证。

只需要做最基础的一层：

> 模型引用的 `[C1] [C2]` 必须真实存在于当前 ContextBundle。

例如 Bundle 只有：

```text
C1
C2
C3
```

模型却输出：

```text
根据 [C7] ...
```

那就是非法 Citation。

你可以解析回答中的：

```text
[C数字]
```

检查是否属于当前 Bundle。

这叫：

> **Citation label validation**

它不是判断“C1 是否真的支持这句话”，只是先保证引用不会凭空出现。

---

# 九、空 Context 怎么处理

如果：

```python
bundle.items == []
```

那就不应该继续调用 LLM。

直接返回：

```text
当前没有检索到足够的项目上下文，无法可靠回答该问题。
```

这样：

```text
Retriever没有结果
```

不会变成：

```text
LLM凭通用知识乱回答
```

这是非常重要的边界。

---

# 十、CLI 需要增加 `ask`

你现在有：

```bash
devcontext context "..."
```

下一步增加：

```bash
devcontext ask "购票事务是如何实现的？"
```

流程：

```text
ask
 ↓
Hybrid Retrieval
 ↓
ContextBuilder
 ↓
AnswerGenerator
 ↓
LLM
 ↓
打印 Answer
```

例如：

```text
Question:
购票事务是如何实现的？

Answer:
购票核心事务由 PurchaseTicketTxService#doPurchaseInTransaction 执行 [C1]。
该设计将核心数据库操作集中在事务方法中 [C2]。

Sources:
[C1] PurchaseTicketTxService.java:69-118
[C2] docs/事务设计.md > 事务边界
```

到这里，你的项目才第一次形成真正的：

> **Question → Retrieval → Context → Answer**

完整闭环。

---

# 十一、这一层不要负责什么

为了保持职责清晰，Answer Generator V1 不应该负责：

```text
❌ 判断 CODE / DOC / MIXED
❌ 重新检索
❌ Query Rewrite
❌ Rerank
❌ Context 去重
❌ Token Budget
❌ LangGraph
```

这些都不是它的职责。

它只负责：

```text
Question + Context
→ Grounded Answer
```

---

# 十二、如何理解和 Context Builder 的边界

非常简单：

### Context Builder

回答：

> “给模型看哪些资料？”

### Answer Generator

回答：

> “根据这些资料怎么回答？”

所以：

```text
Retriever
= 找资料

Context Builder
= 整理资料

Answer Generator
= 根据资料回答
```

---

# 十三、这一阶段建议的最小验收标准

V1 做到下面这些就结束：

```text
1. 可以把 ContextBundle 传给 LLM

2. Prompt 明确要求只根据 Context 回答

3. 回答支持 [C1][C2] 引用

4. 非法 Citation 能检测

5. 空 Context 不调用 LLM

6. CLI：
   devcontext ask "..."
   能完整跑通
```

测试时不要每个单元测试都真实调在线模型，可以用 Fake LLM。

---

# 十四、最终完成后的项目效果

你现在是：

```text
用户问题
→ 搜资料
→ 整理资料
```

完成这一层后会变成：

```text
用户问题
→ 搜资料
→ 整理资料
→ 基于资料回答
→ 告诉用户证据在哪里
```

这一步的真正价值不是“接了一个 LLM API”，而是：

> **把 Retrieval 和 Context Engineering 的成果真正消费起来，形成一个有证据约束、可引用、可追溯的回答系统。**

所以 **LLM Answer + Citation V1 的核心设计目标不是“回答得更聪明”，而是“回答必须有项目证据支撑”。**