# JavaParser 调研解析：从 Java 源码到 DevContext Code Chunk

## 0. 先明确：我们为什么研究 JavaParser？

JavaParser 并不是 DevContext 的核心目的。

DevContext 真正研究的是：

```text
如何为 LLM 找到正确、完整、可验证的项目 Context？
```

JavaParser 只负责其中很早的一步：

```text
Java Repository
      ↓
读取 .java
      ↓
理解 Java 代码结构
      ↓
生成 Code Chunk
      ↓
建立 Keyword / Vector Index
      ↓
Retrieval
```

也就是说：

```text
JavaParser
=
Java Source → Structured Code Chunk
```

而不是：

```text
JavaParser
=
整个 DevContext
```

原始调研给 JavaParser 规定的范围也非常明确：

> 只验证 JavaParser 是否足够支持 DevContext 的 AST-aware Code Chunk，不研究代码生成，也不深入完整类型推断。

所以学习 JavaParser 时，最容易犯的错误就是：

```text
Node 有哪些子类？
Visitor 怎么实现？
SymbolSolver 有哪些接口？
AST 怎么修改？
JavaParser 能不能生成代码？
```

一路把 JavaParser 学成一门新课程。

这对 DevContext V1 没有必要。

我们真正只想解决一个问题：

> **怎样把一个 Java 文件可靠地拆成适合 RAG 检索的知识单元？**

---

# 1. 为什么不能继续使用普通文本 Chunk？

你已经学过基础 RAG，所以最自然的方案是：

```text
读取 Java 文件
↓
每 500 / 1000 token 切一块
↓
Embedding
↓
Vector DB
```

对于普通文章，这个方案很多时候可以工作。

但是 Java 是一种高度结构化语言。

例如：

```java
@Service
public class TicketServiceImpl {

    @Transactional
    public TicketPurchaseRespDTO purchaseTicket(
            TicketPurchaseReqDTO request) {

        checkTokenBucket();
        lockSeat();
        createOrder();

        return result;
    }
}
```

从人的角度看，这并不是几十行互不相关的文本。

它拥有非常明显的结构：

```text
Class
TicketServiceImpl

Method
purchaseTicket

Annotation
@Transactional

Parameter
TicketPurchaseReqDTO request

Method Body
checkTokenBucket()
lockSeat()
createOrder()
```

如果按照固定字符切：

```text
Chunk 1

@Transactional
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request) {
    checkTokenBucket();

----------------

Chunk 2

    lockSeat();
    createOrder();
    return result;
}
```

第二个 Chunk 就出现问题了。

它已经不知道自己：

```text
属于哪个类？
属于哪个方法？
有没有 @Transactional？
方法叫什么？
文件在哪里？
```

于是你会发现：

> **Java Chunking 首先不是“把文本切小”，而是“确定代码语义边界”。**

这就是为什么 DevContext 要从普通：

```text
Text-aware Chunk
```

升级成：

```text
AST-aware Chunk
```

---

# 2. JavaParser 在这里到底做什么？

可以把 JavaParser 理解成：

> **把程序员看到的 Java 文本，转换成程序能够理解的 Java 结构。**

例如源码：

```java
@Transactional
public void purchaseTicket(Request req) {
    lockSeat();
}
```

JavaParser 解析之后，程序看到的就不再只是字符串，而类似于：

```text
MethodDeclaration

├── Name
│   └── purchaseTicket
│
├── Annotation
│   └── Transactional
│
├── Parameter
│   └── Request req
│
├── Return Type
│   └── void
│
├── Body
│   └── lockSeat()
│
└── Range
    ├── startLine
    └── endLine
```

这就是：

```text
AST
Abstract Syntax Tree
抽象语法树
```

这里不用把“树”想得特别复杂。

你现在只需要建立一个直觉：

> AST 就是把“代码文本”变成“代码结构”。

---

# 3. 为什么 AST 对 DevContext 特别重要？

假设用户问：

```text
purchaseTicket 方法在哪里？
```

如果数据库中只有：

```text
content
```

那么系统只能尝试在正文中搜索 `purchaseTicket`。

但如果 JavaParser 已经帮我们生成：

```text
file_path
class_name
method_name
annotations
signature
start_line
end_line
content
```

那么一个 CodeChunk 就可能变成：

```text
file_path:
ticket-service/.../TicketServiceImpl.java

package_name:
com.xxx.ticket.service

class_name:
TicketServiceImpl

method_name:
purchaseTicket

annotations:
@Transactional

signature:
public TicketPurchaseRespDTO purchaseTicket(
    TicketPurchaseReqDTO request
)

start_line:
120

end_line:
188

content:
完整原始方法源码
```

这时候 CodeChunk 已经不是：

> 一段字符串。

而是：

> **一份带有代码语义的结构化知识。**

这正是 JavaParser 的真正价值。

原始调研中最后建议的 CodeChunk Schema，也正是把 `package_name / class_name / method_name / annotations / signature / start_line / end_line / content` 映射到 JavaParser 提供的 AST 信息。

---

# 4. JavaParser 为什么比 tree-sitter 更适合当前 DevContext？

前面 Continue 调研里，你已经接触到了 tree-sitter。

Continue 选择 tree-sitter，是因为它要支持很多语言：

```text
Java
Python
JavaScript
Rust
Go
...
```

所以它需要：

```text
通用 Parser
+
每种语言不同规则
```

这是 Continue 的业务需求。

但 DevContext 当前非常明确：

```text
Java Only
```

因此问题发生变化了。

JavaParser 可以直接提供：

```java
MethodDeclaration
ClassOrInterfaceDeclaration
ConstructorDeclaration
AnnotationExpr
```

这些是强类型 Java 对象。

例如你想找所有 Method：

```java
cu.findAll(MethodDeclaration.class)
```

而不是：

```text
判断 node.type 是否等于 "method_declaration"
```

所以这里不是说：

> JavaParser 永远比 tree-sitter 好。

而是：

> **DevContext 已经明确限制为 Java，所以使用 Java 专用 AST 工具更简单。**

原调研因此建议 V1 只引入 `javaparser-core`，而不承担 Continue 多语言解析所需要的复杂度。JavaParser core 本身还可以只用于 AST 解析，不必连 SymbolSolver 一起引入。

这体现了你做项目时一个很重要的思维：

```text
不要问：
哪个技术更强？

应该问：
我的问题需要什么？
```

---

# 5. 理解 JavaParser，只需要先理解三层结构

不用一开始研究几十种 AST Node。

你可以先把 JavaParser 看成三层。

第一层：

```text
CompilationUnit
```

代表：

```text
一个完整 Java 文件
```

例如：

```text
TicketServiceImpl.java
```

第二层：

```text
TypeDeclaration
```

代表：

```text
Class
Interface
Enum
Record
...
```

第三层：

```text
BodyDeclaration
```

里面包括：

```text
Method
Constructor
Field
...
```

所以：

```text
TicketServiceImpl.java
        ↓
CompilationUnit
        ↓
TicketServiceImpl
        ↓
ClassOrInterfaceDeclaration
        ↓
purchaseTicket()
        ↓
MethodDeclaration
```

这就已经足够理解 DevContext 的 Parser。

---

# 6. CompilationUnit：一个 Java 文件的入口

假设文件是：

```java
package com.xxx.ticket.service;

import ...

@Service
public class TicketServiceImpl {

    public void purchaseTicket() {
    }
}
```

解析以后：

```text
CompilationUnit
```

可以告诉你：

```text
package 是什么？
import 有什么？
有哪些 class？
有哪些 interface？
```

例如：

```text
package:
com.xxx.ticket.service

types:
TicketServiceImpl
```

于是 DevContext 处理一个 Java 文件时，可以理解成：

```text
File
↓
JavaParser
↓
CompilationUnit
↓
开始抽取 Method / Class Chunk
```

原调研特别建议使用 `JavaParser` 实例返回的 `ParseResult<CompilationUnit>`，而不是简单使用失败就抛异常的静态 API；同时遍历整个 Repository 时复用同一个 Parser 实例。

---

# 7. 为什么 ParseResult 这个设计值得理解？

这个点虽然看起来只是 JavaParser API 细节，但其实是一个很好的工程设计。

一种非常简单的 Parser 写法是：

```text
解析成功
→ 返回 AST

解析失败
→ 抛异常
```

但真实 Repository 不会这么干净。

可能存在：

```text
生成代码
旧代码
临时文件
测试代码
不完整 Java 文件
某些新版本 Java 语法
```

如果：

```text
一个文件失败
→ 整个 Repository indexing 失败
```

系统会非常脆弱。

所以 JavaParser 提供：

```text
ParseResult

├── isSuccessful()
├── getProblems()
└── getResult()
```

这个设计传递了一个重要工程思想：

> **Parsing 是一个可能部分失败的过程，而不是非黑即白。**

映射到 DevContext：

```text
1000 个 Java 文件

其中：
995 个成功
5 个失败
```

正确策略不应该是：

```text
Index Failed
```

而应该：

```text
995 个继续生成 Chunk

5 个记录：
parse_problems
```

然后让整个 ingestion 继续。

这和前面 Continue 调研中“单个 Index 失败不要让整个索引流程崩溃”的思想其实是一致的。

---

# 8. MethodDeclaration：DevContext 最核心的 JavaParser 节点

JavaParser 有很多 AST Node。

但如果只允许你学一个：

> **MethodDeclaration。**

因为 DevContext V1 最重要的 Code Chunk 就是：

```text
METHOD Chunk
```

例如：

```java
@Transactional
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request) {

    ...
}
```

JavaParser 能从这个节点直接得到：

```text
methodName
parameters
returnType
annotations
modifiers
body
signature
range
```

原调研确认 `MethodDeclaration` 可以直接取得方法名、返回类型、参数、异常、修饰符、注解、方法体和声明字符串；接口方法和抽象方法没有 body，因此 `getBody()` 本身就是 Optional。

这里很重要的一点是：

> **没有方法体 ≠ 没有价值。**

例如：

```java
public interface TicketService {

    TicketResp purchaseTicket(TicketReq req);

}
```

虽然只有：

```text
方法声明
```

但对于：

```text
TicketService 提供哪些能力？
```

这种问题，Interface 本身非常重要。

所以 DevContext 不能写成：

```text
if method body == null
    skip
```

而应该：

```text
只要是 MethodDeclaration
就可以生成 Chunk
```

---

# 9. 为什么 Method 是天然的 Chunk 单位？

现在可以重新理解：

```text
AST-aware Chunking
```

普通文本 Chunk 依靠：

```text
字符数
Token 数
```

判断边界。

AST-aware Chunk 则依靠：

```text
程序结构
```

判断边界。

Method 有：

```text
完整开始位置
完整结束位置
方法名
方法签名
所属 Class
Annotations
```

因此：

```text
一个 Method
≈
一个完整语义单元
```

例如：

```text
purchaseTicket()
```

本身通常描述：

```text
一个具体业务行为
```

于是 DevContext V1 可以采用：

```text
Method
↓
Method Chunk
```

原调研最终也是这个判断：

```text
V1：
Method = 一个 Chunk
```

但同时保留超长方法的未来降级策略。

---

# 10. 一个 Method Chunk 最终应该长什么样？

以 my12306 为例。

假设：

```java
@Transactional
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request) {

    tokenBucket.tryAcquire();
    seatAllocator.lockSeat();
    orderService.createOrder();
}
```

DevContext 不应该只保存：

```text
@Transactional
public TicketPurchaseRespDTO ...
```

而应该保存：

```text
chunk_type:
METHOD

file_path:
ticket-service/src/main/java/.../TicketServiceImpl.java

package_name:
com.xxx.ticket.service.impl

class_name:
TicketServiceImpl

method_name:
purchaseTicket

annotations:
[
  "Transactional"
]

signature:
public TicketPurchaseRespDTO purchaseTicket(
    TicketPurchaseReqDTO request
)

start_line:
120

end_line:
168

content:
@Transactional
public TicketPurchaseRespDTO purchaseTicket(...) {
    ...
}

javadoc:
...
```

你可以发现：

```text
content
```

只是其中一个字段。

而真正让这份知识变得有用的是：

```text
Metadata + Content
```

---

# 11. 为什么 signature 很重要？

比如用户搜索：

```text
PurchaseTicketReqDTO
```

如果 Chunk 只有方法体：

```java
tokenBucket.tryAcquire();
seatAllocator.lockSeat();
```

可能根本没有出现：

```text
PurchaseTicketReqDTO
```

但方法签名：

```java
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request)
```

包含了非常强的检索信息。

因此：

```text
signature
```

不只是给人看的。

它还可以进入：

```text
Keyword Search
```

成为非常重要的 Symbol 信息。

JavaParser 给出了几种 signature 表达方式，但 DevContext 调研最终建议：

```text
getDeclarationAsString()
```

因为它得到的是适合人阅读和搜索的 Java 方法声明；而 `getSignature()` 更偏向程序内部的重载匹配，会主动去掉一部分泛型、注解等信息。

这里可以建立一个重要认知：

```text
结构化 Parser
不是只帮助“切 Chunk”

它还帮助生成：
Keyword-friendly Metadata
```

---

# 12. Annotation 为什么值得单独保存？

例如：

```java
@Transactional
public void purchaseTicket() {}
```

和：

```java
public void queryTicket() {}
```

虽然两个都是方法，但：

```text
@Transactional
```

本身携带非常强的语义。

它告诉你：

```text
这个方法与事务有关
```

类似的还有：

```text
@RestController
@Service
@FeignClient
@GetMapping
@PostMapping
@Async
@Cacheable
```

因此当用户问：

```text
购票事务在哪里开启？
```

如果 Retriever 能对：

```text
@Transactional
```

做 Keyword 匹配或者 Metadata 加权，会非常有帮助。

JavaParser 将注解区分为无参数、单参数、多参数等不同 AST 形式，但它们最终都可以通过统一的 `AnnotationExpr` API 得到注解名称。原调研已经验证 `@Service`、`@Transactional`、`@GetMapping`、`@FeignClient` 等常见形式都可以获取。

所以在 DevContext 中建议存：

```text
annotations:
[
    "Transactional",
    "Override"
]
```

而不是：

```text
isTransactional = true
isService = false
isController = false
```

因为前一种设计更通用。

以后突然要支持：

```text
@Async
@Cacheable
@Scheduled
```

不需要改 Schema。

---

# 13. Annotation 的一个小坑：不要把它理解成类型解析

JavaParser core 看到：

```java
@Transactional
```

得到：

```text
Transactional
```

看到：

```java
@org.springframework.transaction.annotation.Transactional
```

可能得到完整写法。

但它不会自动告诉你：

```text
这一定就是
org.springframework.transaction.annotation.Transactional
```

因为这已经涉及：

```text
Symbol Resolution
```

而不是单纯 AST Parsing。

所以 V1 可以做一个很简单的判断：

```text
annotationName.endsWith("Transactional")
```

而不要过早追求：

```text
完整 Class Resolution
```

这一点非常能体现：

```text
Parsing
≠
Semantic Resolution
```

JavaParser core 解决：

```text
代码长什么样？
```

SymbolSolver 才解决：

```text
这个名字到底指向谁？
```

---

# 14. Range：JavaParser 对 DevContext 最重要的能力之一

这一部分其实比很多 JavaParser API 都重要。

因为 DevContext 希望最终回答：

```text
TicketServiceImpl.java
Lines 120-188
```

那么系统必须知道：

```text
一个 Method 在原始文件的哪几行？
```

JavaParser 的每个 AST Node 都可以有：

```text
Range
```

例如：

```text
purchaseTicket Method

begin.line = 120
end.line   = 188
```

于是可以直接生成：

```text
Lines 120-188
```

JavaParser 中 Position 是 **1-based**，Range 又是闭区间，所以 DevContext 可以直接把：

```text
begin.line
end.line
```

存入数据库，不需要展示时再做 `+1/-1` 转换。

这看起来只是个小细节，但实际能减少大量：

```text
off-by-one
```

错误。

所以建议整个 DevContext 都统一：

```text
start_line:
1-based

end_line:
1-based
```

---

# 15. 为什么 Citation 不只是“展示功能”？

Citation 容易被理解成：

> 回答最后附一下文件名，看起来专业一点。

其实不是。

它至少有三个作用。

第一：

```text
用户验证
```

AI 说：

```text
purchaseTicket 使用 @Transactional。
```

用户可以直接打开：

```text
TicketServiceImpl.java
Lines 120-188
```

检查。

第二：

```text
Evaluation
```

假设 Benchmark 规定：

```text
正确答案应该命中
TicketServiceImpl.java
120-188
```

那么 Retriever 找到的 Chunk：

```text
start_line = 120
end_line = 188
```

就可以自动判断：

```text
Hit
```

第三：

```text
减少模型胡编
```

模型得到的 Context 本身就包含明确来源：

```text
file
class
method
lines
```

回答更容易绑定真实证据。

所以：

> Range 实际上连接了 Parsing、Retrieval、Evaluation 和最终回答。

这也是原调研把 Range 称为最关键部分之一的原因。

---

# 16. 为什么不能为了省事关闭 storeTokens？

这是原调研里一个非常值得保留的工程细节。

JavaParser 的 Range 来源于：

```text
Token Range
```

如果把：

```java
setStoreTokens(false)
```

关掉，

Range 就可能无法建立。

同时 JavaParser 还会关闭一部分 comment attribution。

于是 DevContext 最重要的两个能力：

```text
Citation
Javadoc / Comment
```

都会受到影响。

所以 DevContext 的配置应该明确保持：

```java
storeTokens = true
attributeComments = true
```

并显式设置 Java 语言级别，例如 Java 21。

这个细节现在不用死记代码，但需要记住：

> **Range 是 DevContext 的核心数据，因此任何影响 Range 的 Parser 配置都不能随便动。**

---

# 17. 如何取得 Method 的原始源码？

现在有：

```text
startLine
endLine
```

还要得到：

```text
content
```

最简单似乎是：

```java
method.toString()
```

但这里存在一个问题。

JavaParser 的：

```java
Node.toString()
```

不是简单返回源文件原始 substring。

它会使用 Pretty Printer 重新输出代码。

也就是说原代码：

```java
public void test( )
{

      foo( );

}
```

可能重新变成：

```java
public void test() {
    foo();
}
```

语义没变。

但是：

```text
content
```

已经不再与：

```text
startLine / endLine
```

对应的原文件文本完全相同。

这会影响：

```text
Citation
Evaluation
源码展示
```

原调研因此比较了 `toString()`、LexicalPreservingPrinter 和 Range 切片，最终选择最简单的第三种：**根据 Range 从原始文件内容直接切出对应行。**

可以理解成：

```text
JavaParser负责告诉我：
120 → 188

DevContext自己读取：
原始文件第120～188行

得到：
content
```

这是一个非常好的职责划分。

---

# 18. 为什么 Range 切片特别适合 DevContext？

因为 DevContext 不是：

```text
Java Code Formatter
```

也不是：

```text
Java Refactoring Tool
```

它只是要：

```text
读取
索引
检索
引用
```

所以完全没有必要为了“保留源码格式”再引入：

```text
LexicalPreservingPrinter
```

直接：

```text
Range
+
原始 File Content
```

已经足够。

而且：

```text
content
=
真正的 120-188 行

citation
=
Lines 120-188
```

两者天然一致。

这会让后面的 Evaluation 简单很多。

---

# 19. Javadoc 应该怎么办？

这一点很有意思。

例如：

```java
/**
 * 执行购票流程。
 *
 * 先进行令牌桶校验，
 * 再完成座位锁定与订单创建。
 */
@Transactional
public void purchaseTicket(...) {
}
```

对于 Vector Search 来说：

```text
执行购票流程
令牌桶校验
座位锁定
订单创建
```

这些自然语言信息非常有价值。

甚至有时候比方法体：

```java
bucket.tryAcquire();
seatService.lock();
```

更容易和用户自然语言问题匹配。

所以：

```text
Javadoc
```

值得保留。

但是原调研发现：

```text
Method Range
```

通常不会自动包含前面的 Javadoc。

如果强行把 Javadoc 加进：

```text
content
```

就会出现：

```text
content:
从 115 行开始

start_line:
120
```

二者不一致。

因此比较干净的设计是：

```text
content
=
真正 Method Range

javadoc
=
单独字段
```

这样后面 Context Builder 可以选择：

```text
Javadoc
+
Method Content
```

一起交给 LLM，

但底层 Citation 仍然保持准确。

原调研因此明确建议 V1 保留 Javadoc，但作为独立字段；行内注释自然跟随原始代码保留，孤立注释暂不处理。

---

# 20. Method 永远等于一个 Chunk 吗？

这是一个非常关键的设计问题。

例如：

```text
普通方法：
40 行

purchaseTicket：
150 行

极端大方法：
1000 行
```

前两个通常可以：

```text
Method
=
Chunk
```

但 1000 行方法可能超过 Embedding 模型单次输入上限。

一种简单解决方法是：

```text
Method
↓
固定 Token 切成 4 块
```

但这样又回到了我们最初的问题：

```text
语义边界被破坏
```

原调研最终给出的 V1 方案比较克制：

```text
V1：

Method 仍然作为一个逻辑 Chunk
↓
如果太长
↓
标记 is_oversized
↓
Embedding 输入截断
```

也就是说：

```text
数据库中的知识边界
仍然保持 Method

Embedding 时
允许暂时损失一部分内容
```

这样不会破坏：

```text
Citation
RRF
Evaluation
```

之后到了 P1，再改成：

```text
Method
↓
BlockStmt
↓
Statement
↓
按照语句边界二次 Chunk
```

这样就比固定字符切割合理很多。

这里有一个非常重要的设计原则：

> **为了让 V1 简单，可以先牺牲少量 Retrieval Quality，但不要破坏整个数据模型。**

---

# 21. 为什么除了 Method，还需要 Class Chunk？

最开始你可能觉得：

```text
所有 Method 都已经存了

为什么还存 Class？
```

考虑这个问题：

```text
TicketServiceImpl 主要负责什么？
```

这不是某一个 Method 能回答的。

你需要知道：

```text
@Service

TicketServiceImpl
implements TicketService

Methods:
purchaseTicket(...)
cancelTicket(...)
queryTicket(...)
refundTicket(...)
```

这才像“这个类是什么”。

所以需要：

```text
CLASS Chunk
```

但是又出现另一个问题。

如果直接把：

```text
整个 800 行 TicketServiceImpl
```

作为一个 Chunk，

那么 Vector Embedding 会变成：

```text
购票
退票
订单
缓存
锁
限流
查询
...
```

各种语义混在一起。

这个 Vector：

> 什么都包含一点，但什么都不特别准确。

所以 Class Chunk 不应该保存整个 Class 源码。

更合理的是：

```text
CLASS SUMMARY Chunk

@Service
public class TicketServiceImpl implements TicketService {

    purchaseTicket(...);
    queryTicket(...);
    cancelTicket(...);
    refundTicket(...);

}
```

也就是：

```text
类签名
+
Class Annotation
+
继承关系
+
成员 Method Signature
```

原调研最终正是选择：

```text
METHOD 为主
+
CLASS 摘要辅助
```

而不是“完整 Class + Method 重复存储”。

---

# 22. Class Chunk 与 Method Chunk 分别解决什么问题？

可以这样理解。

METHOD Chunk 擅长回答：

```text
purchaseTicket 具体怎么实现？
Token Bucket 在哪里调用？
@Transactional 在哪个方法上？
锁座代码在哪里？
```

CLASS Chunk 擅长回答：

```text
TicketServiceImpl 是干什么的？
这个类提供哪些主要能力？
这个 Service 实现了哪个接口？
有哪些核心方法？
```

所以：

```text
CLASS
=
地图

METHOD
=
具体地点
```

而且在 DevContext 的 MIXED 场景中：

```text
设计文档
提到了 TicketServiceImpl

↓

CLASS Chunk
知道这个类有哪些 Method

↓

METHOD Chunk
找到具体 purchaseTicket
```

Class 就可以成为：

```text
Document
→ Class
→ Method
```

之间的一个桥梁。

---

# 23. 最终 knowledge_chunk 中应该有哪些 Java 数据？

结合 Continue 和 JavaParser 两轮调研，V1 可以形成一个比较清晰的模型：

```text
knowledge_chunk

id

repository_id

source_type
CODE

chunk_type
METHOD / CLASS / CONSTRUCTOR

file_path

module

package_name

class_name

method_name

signature

annotations

javadoc

content

start_line

end_line

content_hash

embedding
```

注意：

```text
JavaParser
```

并不会生成所有这些东西。

它负责：

```text
package
class
method
signature
annotations
range
javadoc
```

而：

```text
repository_id
module
file_path
content_hash
embedding
```

属于 DevContext 自己负责。

原调研也明确做了这种字段归属区分，例如 repository 和 file_path 是外部传入，module 从路径推导，content_hash 由外部计算。

这个区分很重要。

不要形成：

> JavaParser 应该提供所有东西。

它只是 Parser。

---

# 24. JavaParser 与后面的 Keyword Search 有什么关系？

这才是 JavaParser 和 RAG 真正连接起来的地方。

假设原始源码：

```java
@Transactional
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request)
```

JavaParser 帮你拆成：

```text
class_name:
TicketServiceImpl

method_name:
purchaseTicket

annotations:
Transactional

signature:
TicketPurchaseRespDTO purchaseTicket(
    TicketPurchaseReqDTO request)
```

以后 PostgreSQL FTS 可以给这些字段：

```text
method_name
class_name
signature
```

更高权重。

于是用户搜索：

```text
purchaseTicket
```

几乎直接命中。

搜索：

```text
PurchaseTicketReqDTO
```

也可以命中 signature。

搜索：

```text
Transactional purchase
```

Annotation 也可以参与。

所以：

> JavaParser 本身不是 Retriever，但它提高了 Retriever 可以使用的知识质量。

---

# 25. JavaParser 与 Vector Search 又是什么关系？

Vector Search 不太关心：

```text
字段是不是精确字符串
```

它更关心语义。

因此可以把：

```text
signature
+
Javadoc
+
content
```

组成适合 Embedding 的文本。

例如：

```text
Class: TicketServiceImpl
Method: purchaseTicket

Description:
执行购票流程，在进入锁座逻辑之前进行令牌桶校验。

Code:
@Transactional
public ...
```

比单纯：

```java
bucket.tryAcquire();
seat.lock();
```

更加容易和：

```text
系统是如何防止大量购票请求直接进入库存系统的？
```

匹配。

这就是为什么：

```text
Metadata
```

不只是用于 SQL 查询。

它也可以成为：

```text
Embedding Input
```

的一部分。

---

# 26. Parsing → Chunking → Retrieval 的真正关系

经过 JavaParser 调研后，应该把原来的：

```text
代码
↓
Embedding
```

升级成：

```text
Java Source

↓

JavaParser

↓

AST

↓

Code Structure

↓

Method / Class Chunk

↓

Metadata

↓

┌────────────────┬─────────────────┐
│                │                 │
Keyword Index    Vector Index
│                │
Symbol Search    Semantic Search
```

这正是 JavaParser 与 Continue 调研真正接上的位置。

Continue 告诉我们：

```text
Repository
不能直接变成 Embedding
```

JavaParser进一步告诉我们：

```text
Java Repository
应该先变成 AST-aware Knowledge Chunk
```

---

# 27. SymbolSolver 是什么？

JavaParser core 能告诉你：

```java
ticketService.purchaseTicket();
```

这里存在：

```text
一个方法调用：
purchaseTicket
```

但它不一定知道：

```text
purchaseTicket
最终指向哪个 Class 中的哪个 Method？
```

例如：

```java
TicketService ticketService;
ticketService.purchaseTicket();
```

你可能想进一步回答：

```text
这个调用最终指向
TicketServiceImpl.purchaseTicket
```

这已经不是简单 AST Parsing。

而是：

```text
Symbol Resolution
```

需要：

```text
JavaSymbolSolver
```

简单区别：

```text
JavaParser AST

回答：
代码长什么样？
```

```text
SymbolSolver

回答：
这个名字真正指向谁？
```

原调研中也是这样定义二者边界的。

---

# 28. 为什么现在不要做 SymbolSolver？

因为 Symbol Resolution 很快就会引出：

```text
这个类型在哪？

↓

项目哪个模块？

↓

依赖哪个 jar？

↓

Maven dependency 是什么？

↓

有没有多个同名 Class？

↓

这个 Interface 的实现是谁？

↓

这个 Method 调用具体绑定谁？
```

于是需要：

```text
ReflectionTypeSolver
JavaParserTypeSolver
JarTypeSolver
CombinedTypeSolver
...
```

对于 my12306 这种多模块 Spring 项目，还需要正确提供：

```text
各模块 source path
+
依赖 jar
+
JDK classes
```

这已经是另一层复杂度。

而 DevContext V1 当前只需要证明：

```text
AST-aware Chunk
比普通 Text Chunk 更适合代码检索
```

所以：

```text
V1
JavaParser Core
```

足够。

原调研将 SymbolSolver、Reference Resolution 和 Call Graph 明确后移到 P2/P3，而不是塞进 10～15 天 MVP。

---

# 29. Call Graph 为什么也不该现在做？

你以后当然可能希望回答：

```text
purchaseTicket 调用了哪些方法？
```

甚至：

```text
用户请求从 Controller
经过哪些 Service
最终调用哪些 Mapper？
```

这属于：

```text
Call Graph
```

很有价值。

但它依赖：

```text
Symbol Resolution
```

否则看到：

```java
service.execute();
```

你不一定知道 `service` 到底是什么实现类。

所以合理演进路径是：

```text
V1
AST Chunk

↓

V2
Symbol Resolution

↓

V3
Reference Resolution

↓

V4
Call Graph
```

而不是 Day 1 就：

```text
JavaParser
+
SymbolSolver
+
Call Graph
+
Knowledge Graph
+
RAG
+
Agent
```

否则项目会迅速失控。

---

# 30. JavaParser V1 其实只需要很少 API

原调研虽然分析了大量源码，但最终 V1 真正使用的能力很少。

核心流程本质上就是：

```text
parse()

↓

CompilationUnit

↓

findAll(MethodDeclaration)

↓

findAncestor(TypeDeclaration)

↓

getNameAsString()

↓

getAnnotations()

↓

getDeclarationAsString()

↓

getRange()

↓

getJavadocComment()

↓

生成 CodeChunk
```

所以不要因为原始调研有 1000 多行，就产生：

> JavaParser 好复杂，我是不是得先学两周？

不需要。

那 1000 多行更多是在：

```text
证明这些选择为什么可靠
```

而真正开发可能只使用 JavaParser 很小的一部分 API。

---

# 31. JavaParser V1 的完整数据流

真正开发时，可以把整个 Parser 模块理解成：

```text
my12306 repository

↓

扫描 *.java

↓

读取原始 file content

↓

JavaParser.parse(file)

↓

ParseResult<CompilationUnit>

↓

解析 package

↓

findAll MethodDeclaration

↓

对每个 Method：

    找所属 Class

    取 Method Name

    取 Annotation

    取 Signature

    取 Range

    按 Range 从原始文件切 content

    取 Javadoc

↓

CodeChunk

↓

JSON / DTO

↓

DevContext Python / Backend

↓

PostgreSQL knowledge_chunk
```

这就是 JavaParser 在整个系统中的全部位置。

---

# 32. 用一个完整 my12306 例子串起来

假设源码：

```java
/**
 * 执行购票流程。
 * 请求进入后首先进行 Token Bucket 校验。
 */
@Transactional
public TicketPurchaseRespDTO purchaseTicket(
        TicketPurchaseReqDTO request) {

    tokenBucket.tryAcquire(request);
    seatAllocator.selectSeat(request);
    orderService.createOrder(request);
}
```

JavaParser 解析以后：

```text
MethodDeclaration
```

告诉系统：

```text
methodName:
purchaseTicket

annotations:
Transactional

signature:
public TicketPurchaseRespDTO purchaseTicket(
    TicketPurchaseReqDTO request
)

Range:
120 - 128

Javadoc:
执行购票流程……
```

DevContext 再加上：

```text
file:
TicketServiceImpl.java

class:
TicketServiceImpl

module:
ticket-service
```

最终：

```text
CodeChunk #328

type:
METHOD

class:
TicketServiceImpl

method:
purchaseTicket

annotation:
Transactional

signature:
public TicketPurchaseRespDTO purchaseTicket(
    TicketPurchaseReqDTO request
)

javadoc:
执行购票流程……

content:
@Transactional
public TicketPurchaseRespDTO purchaseTicket(...) {
    tokenBucket.tryAcquire(request);
    ...
}

file:
TicketServiceImpl.java

lines:
120-128
```

之后 Keyword Retriever 可以搜索：

```text
purchaseTicket
Transactional
TicketPurchaseReqDTO
TicketServiceImpl
```

Vector Retriever 可以理解：

```text
购票
令牌桶
限流
座位
订单
```

最后回答：

```text
购票入口位于 TicketServiceImpl.purchaseTicket，
该方法带有 @Transactional，
并在 TicketServiceImpl.java Lines 120-128
中调用 Token Bucket。
```

现在你应该能看到：

> JavaParser 根本不是和 RAG 无关的另一个技术栈。

它是在决定：

```text
RAG 到底检索什么。
```

---

# 33. 这次 JavaParser 调研真正验证了什么？

真正有价值的不是：

```text
JavaParser 有多少个 AST Class
```

而是确认了几个 DevContext 架构假设。

第一个：

```text
Java Code
可以可靠按 Method / Class
形成结构化 Chunk。
```

第二个：

```text
Method 可以获得准确 Range，
所以 Citation 可行。
```

第三个：

```text
可以获得：
class
method
annotation
signature
javadoc
等 Metadata。
```

第四个：

```text
不需要 SymbolSolver
也能完成 V1。
```

第五个：

```text
不需要 tree-sitter
也能完成 Java-only Code Parser。
```

第六个：

```text
Parser 可以保持非常轻量，
复杂语义分析以后再做。
```

这些才是“调研结果”。

---

# 34. DevContext V1 应该采用什么？

结合原调研，现在可以把决策压缩成：

```text
Parser：
javaparser-core

Java version：
显式 Java 21

File root：
CompilationUnit

Primary Chunk：
METHOD

Secondary Chunk：
CLASS SUMMARY

Other Chunk：
CONSTRUCTOR

Metadata：
package
class
method
annotation
signature
file
lines
javadoc

Content：
Range 原始源码切片

Citation：
1-based
[start_line, end_line]

Parsing Error：
记录问题
不要让整个 indexing 失败

Oversized Method：
V1 标记 + Embedding 截断

SymbolSolver：
不做

Call Graph：
不做
```

这就是足够开始编码的版本。

---

# 35. 哪些东西现在不要学？

现在看到下面内容时，只需要知道名字：

```text
JavaSymbolSolver
TypeSolver
ReflectionTypeSolver
JarTypeSolver
CombinedTypeSolver
Reference Resolution
Call Graph
LexicalPreservingPrinter
AST Serialization
AST Modification
Code Generation
```

不要因为：

```text
JavaParser 还能做这些
```

就认为：

```text
DevContext 也应该做这些。
```

这是两个完全不同的问题。

你现在应该不断问：

> **这项能力能不能直接提高 DevContext V1 的 Context Retrieval 学习价值？**

如果不能：

```text
P1
P2
或者不做
```

---

# 36. 原始 JavaParser 调研应该怎么使用？

和 Continue 一样，原来的 JavaParser 调研不要删除。

它非常适合作为：

```text
源码级 Reference
```

例如以后你开发：

```text
startLine
```

发现不确定 Range 是否 1-based。

回来查：

```text
JavaParser调研
→ Range
```

开发：

```text
annotations
```

发现：

```text
@GetMapping("/tickets")
```

怎么解析？

回来查：

```text
Annotation
```

开发：

```text
signature
```

不知道：

```text
getSignature()
vs
getDeclarationAsString()
```

回来查源码结论。

也就是说最好形成：

```text
学习理解文档
      ↓
理解“为什么”

源码调研文档
      ↓
开发时确认“具体怎么做”
```

而不是一开始逐行啃源码报告。

---

# 37. 你现在应该真正掌握的 8 个问题

学完 JavaParser 调研后，如果下面的问题可以自己解释，就已经足够进入开发。

### 问题 1

为什么 Java 不能固定长度 Chunk？

答案核心应该是：

```text
Java 有 Method / Class 等天然语义边界，
固定长度切分可能破坏代码结构。
```

### 问题 2

JavaParser 在 DevContext 中负责什么？

```text
Source Code
→ AST
→ Code Structure
→ CodeChunk Metadata
```

### 问题 3

为什么 Method 适合作为主要 Chunk？

```text
Method 通常对应完整业务行为，
并且拥有明确 name / signature / annotation / range。
```

### 问题 4

为什么还需要 Class Chunk？

```text
Class 提供类职责、注解、接口和成员方法整体信息，
但应该采用摘要而非完整源码。
```

### 问题 5

Range 有什么用？

```text
生成准确源码 Content
+
Citation
+
Evaluation Ground Truth
```

### 问题 6

为什么 content 不直接用 `toString()`？

```text
toString 会重新格式化，
可能与原始文件行号不一致。
```

### 问题 7

为什么 V1 不使用 SymbolSolver？

```text
AST Chunk 不依赖 Symbol Resolution，
SymbolSolver 需要复杂 TypeSolver / classpath 配置，
不符合 MVP 成本。
```

### 问题 8

JavaParser 如何影响 Retrieval？

```text
它生成的 method_name / class_name / signature /
annotation / javadoc / content
分别提高 Keyword 和 Vector Retrieval 的质量。
```

如果这八个问题能解释清楚：

> JavaParser 调研已经完成。

---

# 38. 把 Continue 与 JavaParser 两轮调研真正连起来

现在你已经完成两个层次的认识。

Continue 告诉你：

```text
不要：

Repository
→ Embedding

而应该：

Repository
→ Chunk
→ Index
→ Retrieval
```

JavaParser告诉你：

```text
对于 Java：

Repository
↓
Java Source
↓
AST
↓
Method / Class
↓
Structured Code Chunk
```

组合起来就是：

```text
Java Repository

↓

JavaParser

↓

AST

↓

Method / Class Chunk

↓

knowledge_chunk

↓

┌──────────────────────────────┐
│                              │
Full Text Index           Vector Index
│                              │
Keyword Retrieval         Semantic Retrieval
│                              │
└──────────────┬───────────────┘
               ↓
              RRF
               ↓
           Top-K Context
               ↓
              LLM
```

所以：

```text
Continue
```

是在回答：

> **代码知识检索系统应该怎样分层？**

而：

```text
JavaParser
```

是在回答：

> **其中 Java Code Chunk 到底怎样生成？**

后面的 RAGFlow 调研，才会继续回答：

> **这些 Chunk 建好以后，如何更好地 Retrieve / Hybrid / Rerank / Evaluate？**

---

# 39. JavaParser 在整个 DevContext 中的位置

最终可以用一句非常重要的话概括：

> **JavaParser 不负责让 LLM 更聪明，它负责让进入 LLM 之前的 Java 知识结构更正确。**

以前你的 RAG 认知可能是：

```text
好的模型
+
好的 Embedding
=
好的 RAG
```

经过 Continue 和 JavaParser 两轮调研以后，应该逐渐变成：

```text
好的 Parsing
+
好的 Chunking
+
好的 Metadata
+
好的 Index
+
好的 Retrieval
+
好的 Ranking
+
好的 Context Selection

↓

才更可能得到好的 LLM Answer
```

这就是 DevContext 真正想让你学习的：

```text
Context Engineering
```

---

# 40. 最后的开发判断

基于这份调研，目前没有必要继续系统学习 JavaParser。

下一步真正有学习价值的事情应该是直接做一个很小的 Parser：

```text
输入：

TicketServiceImpl.java

↓

输出：

[
  {
    "chunkType": "METHOD",
    "className": "TicketServiceImpl",
    "methodName": "purchaseTicket",
    "annotations": ["Transactional"],
    "signature": "...",
    "startLine": 120,
    "endLine": 188,
    "content": "...",
    "javadoc": "..."
  }
]
```

当这个 JSON 真正从 `my12306` 的源代码中跑出来时：

```text
JavaParser
→ AST
→ Code Chunk
```

这条链才真正从“调研知识”变成了你的工程理解。

原调研也明确承认，目前结论主要来自源码和官方测试，并没有在真实 `my12306` 文件上完成实际编译运行验证，因此真正编码前做一次真实文件验证仍然非常有价值。

从学习角度看，这一步的价值远高于继续阅读更多 JavaParser 源码。
