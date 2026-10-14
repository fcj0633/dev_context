下面正式进入第一讲：**Symbol Graph 是怎样从一个 Java 仓库里构建出来的。**

这一讲先不碰 PostgreSQL，也不碰 Agent。只看最底层的构图过程：

```text
Java Repository
      ↓
发现源码根目录
      ↓
JavaParser + SymbolSolver
      ↓
CompilationUnit / AST
      ↓
Chunk
      ↓
Symbol
      ↓
EXTENDS / IMPLEMENTS
CALLS / CONSTRUCTS / OVERRIDES
      ↓
Symbol Graph 原始数据
```

核心代码集中在：

```text
java-parser/
├── pom.xml
├── RepositoryJavaAnalyzer.java   ← 本讲核心
├── JavaSourceParser.java
├── SymbolRecord.java
└── SymbolEdgeRecord.java
```

---

# 一、先理解这次重构解决了什么问题

在 Symbol Graph 出现以前，Java Parser 主要做一件事：

```text
Java 源码
   ↓
AST
   ↓
CLASS / METHOD / CONSTRUCTOR Chunk
```

例如：

```java
public void closeTimeoutOrder(String orderSn) {
    closePayOrder(orderSn);
    rollbackTicketSeats(...);
}
```

原来的系统能够得到：

```text
METHOD Chunk

symbolName:
closeTimeoutOrder

content:
public void closeTimeoutOrder(...) {
    closePayOrder(...);
    rollbackTicketSeats(...);
}
```

这对 RAG 已经有价值。

但是系统并不知道：

```text
closeTimeoutOrder
        |
        +-- CALLS --> closePayOrder

        +-- CALLS --> rollbackTicketSeats
```

因为：

> **Chunk 描述“这段代码是什么”；Symbol Graph 描述“代码之间是什么关系”。**

所以新的分析器必须同时产出两类东西：

```text
文本世界：
Chunk

程序结构世界：
Symbol + Edge
```

---

# 二、为什么新增 `javaparser-symbol-solver-core`

看当前 `pom.xml`，真正重要的依赖变成了：

```xml
javaparser-symbol-solver-core
```

以前如果只有 JavaParser Core：

```text
JavaParser
```

主要解决：

> 源码的语法结构是什么？

例如看到：

```java
orderService.createTicketOrder(request);
```

JavaParser AST 能告诉你：

```text
这是 MethodCallExpr

method name:
createTicketOrder

scope:
orderService

argument:
request
```

但它并不能仅靠 AST 稳定回答：

> 这个 `createTicketOrder` 究竟是哪一个类的哪个重载？

因为仓库里可能有：

```java
OrderService.createTicketOrder(RequestA)

OrderService.createTicketOrder(RequestB)

OrderServiceImpl.createTicketOrder(...)

MockOrderService.createTicketOrder(...)
```

所以需要第二层：

```text
SymbolSolver
```

它解决的是：

> **一个名字在 Java 类型系统中究竟引用哪个声明。**

可以先把二者理解成：

```text
JavaParser
= 看懂语法

SymbolSolver
= 解析身份
```

---

# 三、入口 `analyze()` 第一件事：扫描整个 Repository

核心入口：

```java
public Result analyze(Path root, String repository)
```

最终返回：

```java
Result(
    chunks,
    symbols,
    edges,
    diagnostics
)
```

也就是说一次仓库分析会同时得到：

```text
1. Chunk
2. Symbol
3. Edge
4. Diagnostics
```

这里已经能看出来它与以前单文件 Parser 的区别：

```text
以前：
file → chunks

现在：
repository → chunks + graph
```

---

## 3.1 只扫描 `src/main/java`

当前逻辑：

```java
files = paths
    .filter(... endsWith(".java"))
    .filter(... contains("/src/main/java/"))
```

所以当前 V1 明确只处理：

```text
src/main/java
```

不会处理：

```text
src/test/java
target/
.git/
.idea/
```

这是有意限制。

因为 Symbol Graph 当前目标是：

> 当前生产代码关系。

而不是：

> 测试调用图 + Generated Source + 所有 Java 文件。

---

# 四、为什么需要找到每一个 `src/main/java`

这是多模块 Java 项目里非常重要的一个细节。

my12306 类似：

```text
repository
├── services
│   ├── order-services
│   │   └── src/main/java
│   ├── pay-services
│   │   └── src/main/java
│   └── user-services
│       └── src/main/java
```

分析器会针对每个 Java 文件向上找：

```text
src/main/java
```

最后得到：

```java
Set<Path> roots
```

例如：

```text
order-services/src/main/java

pay-services/src/main/java

user-services/src/main/java
```

然后：

```java
CombinedTypeSolver solver = new CombinedTypeSolver();
```

每个源码根加入：

```java
solver.add(
    new JavaParserTypeSolver(sourceRoot)
);
```

最后还加入：

```java
ReflectionTypeSolver
```

---

# 五、为什么 `JavaParserTypeSolver` 输入必须是 `src/main/java`

这个细节很重要。

假设：

```java
package edu.swu.order.service;
```

文件位置：

```text
order-services/
src/main/java/
edu/swu/order/service/OrderService.java
```

Java 类型解析的根应该是：

```text
src/main/java
```

这样：

```text
edu.swu.order.service.OrderService
```

才能对应：

```text
src/main/java
  /edu
    /swu
      /order
        /service
          /OrderService.java
```

如果把：

```text
repository root
```

直接给 `JavaParserTypeSolver`，

包路径与源码路径就对不上。

所以当前实现先专门做：

```text
Source Root Discovery
```

再创建 TypeSolver。

---

# 六、`CombinedTypeSolver` 到底是什么

现在的结构：

```text
CombinedTypeSolver
    |
    +-- JavaParserTypeSolver(order-services/src/main/java)
    |
    +-- JavaParserTypeSolver(pay-services/src/main/java)
    |
    +-- JavaParserTypeSolver(user-services/src/main/java)
    |
    +-- ReflectionTypeSolver
```

可以把它理解成：

> 一个“类型去哪找”的总调度器。

例如 SymbolSolver 遇到：

```java
OrderService
```

可以去源码目录找。

遇到：

```java
String
List
Object
```

则可以通过：

```text
ReflectionTypeSolver
```

解析 JDK 类型。

---

# 七、为什么当前没有自动加载 Maven 依赖

注意当前并没有：

```text
JarTypeSolver
Maven dependency resolver
完整 classpath
```

所以：

```text
Spring
MyBatis Plus
RocketMQ
第三方库
Generated class
```

很多关系可能无法 resolve。

这也就是你真实项目出现大量：

```text
call_unresolved
```

的根本原因之一。

但这是 V1 有意边界。

当前原则不是：

> 尽最大可能猜出来。

而是：

> **解析不出来就记录 gap，不建假边。**

后面你会看到这个设计贯穿整个 Analyzer。

---

# 八、配置共享 SymbolResolver

构建完 Solver 后：

```java
sharedConfiguration.setSymbolResolver(
    new JavaSymbolSolver(solver)
);
```

然后创建：

```java
JavaParser parser =
    new JavaParser(sharedConfiguration);
```

所以之后这个 Parser 生成出来的 AST Node 都拥有：

```text
resolve()
```

能力。

例如：

```java
MethodCallExpr call
```

可以：

```java
call.resolve()
```

得到：

```text
ResolvedMethodDeclaration
```

这就是 Graph 边能够建立的关键。

---

# 九、接下来进入第一个阶段：解析全部 CompilationUnit

每个源码文件：

```text
source
   ↓
parser.parse(source)
   ↓
CompilationUnit
```

`CompilationUnit` 可以简单理解为：

> 一个 Java 文件完整 AST 的根节点。

例如：

```java
package xxx;

class OrderServiceImpl {

    void foo() {
        bar();
    }
}
```

大致 AST：

```text
CompilationUnit
│
├── PackageDeclaration
│
└── ClassOrInterfaceDeclaration
    │
    └── MethodDeclaration foo
        │
        └── MethodCallExpr bar
```

后面所有：

```text
Class
Method
Constructor
Call
new
extends
implements
```

都是从这棵 AST 找出来的。

---

# 十、一个很重要的设计：Chunk 和 Symbol 同时生成，但不是同一个东西

在解析一个文件后：

```java
List<ChunkRecord> fileChunks =
    chunkParser.chunks(...);
```

这仍然复用旧的：

```text
JavaSourceParser
```

也就是说：

> Symbol Graph 没有重新发明 Chunk 系统。

仍然生成：

```text
CLASS
INTERFACE
METHOD
CONSTRUCTOR
```

Chunk。

---

## Chunk 是给谁用的？

给：

```text
Embedding
Keyword Search
LLM Context
Citation
```

---

## Symbol 是给谁用的？

给：

```text
Graph
Call relation
Inheritance
Implementation
Graph Tool
```

---

所以二者职责不同：

```text
Chunk
= 可阅读证据

Symbol
= 程序实体身份
```

这是理解整个架构非常关键的一点。

---

# 十一、为什么 Chunk 不直接拿 AST Node 当 Graph Node

假设有：

```text
Chunk id = 123
```

重新 ingestion：

```text
Chunk id 可能变 581
```

所以不能把数据库 ID 当程序实体的稳定身份。

而且：

```text
Chunk
```

本质是检索存储对象，

并不是 Java 语言实体的自然身份。

所以项目额外引入：

```text
SymbolRecord
```

---

# 十二、`SymbolRecord` 实际保存什么

实际结构：

```java
record SymbolRecord(
    String repository,
    String symbolKey,
    String symbolKind,
    String simpleName,
    String qualifiedName,
    String canonicalSignature,
    String ownerSymbolKey,
    int chunkRef,
    String filePath,
    int startLine,
    int endLine
)
```

这里最重要的其实只有三个概念：

```text
symbolKey

ownerSymbolKey

chunkRef
```

---

# 十三、`symbolKey`：程序实体的稳定身份

对于类型：

```java
OrderService
```

可能：

```text
T:edu.xxx.OrderService
```

---

对于方法：

```java
createTicketOrder(TicketOrderCreateReqDTO)
```

可能：

```text
M:edu.xxx.OrderService#createTicketOrder(
    edu.xxx.TicketOrderCreateReqDTO
)
```

---

对于构造器：

```text
C:edu.xxx.ServiceException#<init>(
    java.lang.String
)
```

这里：

```text
T:
M:
C:
```

分别表示：

```text
Type
Method
Constructor
```

---

# 十四、为什么不能只使用方法名？

因为：

```java
void save(String id)

void save(Long id)
```

都是：

```text
save
```

如果 key：

```text
M:UserService#save
```

就冲突了。

所以真实身份必须包含：

```text
Owner Type
+
Method Name
+
Parameter Types
```

即：

```text
UserService#save(String)

UserService#save(Long)
```

---

# 十五、`callableKey()` 是 Symbol Identity 的核心实现

实际代码：

```java
static String callableKey(
    ResolvedMethodLikeDeclaration declaration,
    boolean constructor
)
```

关键部分：

```java
declaration.formalParameterTypes()
    .stream()
    .map(t -> t.erasure().describe())
```

注意两个关键词：

```text
formalParameterTypes

erasure
```

---

## formalParameterTypes

使用的是：

> 方法声明参数。

例如：

```java
void foo(Number value)
```

调用：

```java
foo(1);
```

不能因为调用时传：

```text
Integer
```

就创建：

```text
foo(Integer)
```

真实声明还是：

```text
foo(Number)
```

所以 Symbol 身份必须依赖：

```text
Declaration
```

而不是：

```text
Call-site argument。
```

---

# 十六、为什么要 `erasure()`

例如：

```java
void foo(List<String> value)
```

Java 方法签名层面最终会进行类型擦除。

所以 canonical key 使用：

```text
List
```

而不是把：

```text
List<String>
```

直接当方法身份的一部分。

可以把它理解成：

```text
Source generic type
       ↓
Java method identity normalization
       ↓
Erased type
```

这是为了：

> 调用声明和方法声明生成相同 Symbol Key。

---

# 十七、`ownerSymbolKey` 是干什么的

例如：

```java
class OrderServiceImpl {

    void createOrder() {}
}
```

那么：

```text
T:xxx.OrderServiceImpl
```

是 Type Symbol。

方法：

```text
M:xxx.OrderServiceImpl#createOrder()
```

它的：

```text
ownerSymbolKey
```

就是：

```text
T:xxx.OrderServiceImpl
```

形成：

```text
Type
└── Method
```

这样的 ownership。

注意：

> 这不是 Graph Edge。

当前并没有：

```text
DECLARES
```

边。

它只是 Symbol 元数据里的所属关系。

---

# 十八、`chunkRef` 是一个很聪明的中间映射

注意这里暂时没有数据库 ID。

分析 Java 时：

```text
Chunk 还没有写进 PostgreSQL。
```

所以 Symbol 不可能直接保存：

```text
knowledge_chunk.id
```

因此：

```java
chunkRef
```

保存的是：

> 当前分析结果中 Chunk 的位置。

例如：

```text
chunks[0]
chunks[1]
chunks[2]
```

假设：

```text
Symbol A
chunkRef = 17
```

表示：

```text
这个 Symbol 对应本次输出的第 17 个 Chunk。
```

以后数据库写入：

```text
chunkRef
    ↓
真实 chunk_id
```

再做转换。

这就把：

```text
Java Analyzer
```

和：

```text
PostgreSQL DB ID
```

解耦了。

---

# 十九、为什么有一段 Chunk/declaration 数量校验

代码里：

```java
if (declarations.size() != fileChunks.size())
    throw new AnalysisContractException(...)
```

这里非常重要。

Analyzer 创建：

```text
declarations
=
ClassOrInterface
+
Method
+
Constructor
```

JavaSourceParser 也应该产生对应：

```text
CLASS / INTERFACE
METHOD
CONSTRUCTOR
```

如果：

```text
declaration count = 20

chunk count = 19
```

说明：

> Symbol→Chunk 的一一对应假设已经坏了。

这不是：

```text
某个 SymbolSolver 解析失败
```

这种正常数据缺口。

而是：

> **系统内部契约发生错误。**

所以直接：

```text
fatal
```

而不是 diagnostics 后继续。

这个设计很重要。

---

# 二十、第一阶段 Register Symbol

接下来：

```java
attempt(
    node,
    "symbol_unresolved",
    () -> register(...)
);
```

这里已经体现当前容错哲学。

对于每个 declaration：

```text
尝试生成 Symbol。
```

成功：

```text
进入 Symbol Table。
```

失败：

```text
Chunk 仍然存在

Symbol 跳过

记录：
symbol_unresolved
```

---

# 二十一、这意味着 Chunk 和 Symbol 的关系不是双向强制

不是：

```text
每一个 CODE Chunk
必须有 Symbol
```

而是：

```text
每一个 Symbol
必须有 Chunk
```

即：

```text
Chunk → Symbol
可能没有

Symbol → Chunk
必须存在
```

这就是之前我们总结的：

> **Chunk 保底，Graph 严格。**

---

# 二十二、哪些声明会被跳过？

当前 `register()` 真正支持：

```text
Class
Interface
Method
Constructor
```

但是例如：

```text
enum
record
local class
anonymous class
```

并没有完整纳入 V1 Symbol 模型。

特别是：

```java
type.getFullyQualifiedName()
```

如果是局部类没有稳定 qualified name：

```text
UnsupportedOperationException
```

于是：

```text
Chunk 保留
Symbol skip
diagnostic
```

这就是为什么真实项目：

```text
601 CODE Chunk
567 Symbol
```

不是完全相等。

---

# 二十三、第一阶段结束时，真正得到了一张“符号表”

可以把：

```java
Map<String, SymbolRecord> symbols
```

理解成：

```text
Repository Symbol Table
```

例如：

```text
T:OrderService
→ SymbolRecord

M:OrderService#createTicketOrder(...)
→ SymbolRecord

T:OrderServiceImpl
→ SymbolRecord

M:OrderServiceImpl#createTicketOrder(...)
→ SymbolRecord
```

还有：

```java
Map<Node, String> nodeKeys
```

作用是：

```text
AST Node
→ Symbol Key
```

以后看到某个：

```text
MethodDeclaration AST Node
```

可以立即知道：

```text
它是哪个 Symbol。
```

---

# 二十四、为什么一定要先完成这一阶段，再构建 Edge

现在进入 Symbol Graph 设计最重要的一点：

> **Two-Pass Analysis。**

第一轮：

```text
Parse all files
+
Register all symbols
```

第二轮：

```text
Resolve relations
```

为什么？

假设：

```java
// Controller.java
orderService.createTicketOrder(...);
```

而：

```java
// OrderService.java
void createTicketOrder(...);
```

如果：

```text
一边读 Controller
一边立刻建 CALLS
```

此时 OrderService Symbol 可能还没有登记。

虽然 SymbolSolver 可能知道其声明，

但你的：

```text
repository graph symbol table
```

里还没有 target。

最终不知道：

```text
这个 target 应不应该进入 graph。
```

现在两阶段后：

```text
第一遍：
全部 Symbol 已知

第二遍：
CALL target → 查 symbol table
```

就能确定：

```text
target 是 repository internal
且已被 Graph 建模。
```

---

# 二十五、第二阶段首先构建 EXTENDS / IMPLEMENTS

例如：

```java
class OrderDO extends BaseDO
```

AST：

```text
ClassOrInterfaceDeclaration

extendedTypes:
BaseDO
```

代码：

```java
parent.resolve()
```

得到：

```text
BaseDO 的真实 Qualified Name
```

然后：

```text
OrderDO
 --EXTENDS-->
 BaseDO
```

---

类似：

```java
class MockPayChannelHandler
    implements PayChannelHandler
```

得到：

```text
MockPayChannelHandler
 --IMPLEMENTS-->
 PayChannelHandler
```

---

# 二十六、为什么 relation 最后还要调用 `addEdge()`

即使 SymbolSolver：

```text
resolve 成功
```

也不代表一定加入 Graph。

`addEdge()` 会检查：

```java
symbols.containsKey(target)
```

只有 target 真正在当前 repository Symbol Table 中，

才创建 Edge。

否则分两种。

---

## 情况一：Target 是仓库类型，但没有索引

```text
repositoryTypes.contains(targetType)
```

那么：

```text
internal_target_not_indexed
```

例如：

```text
record method
enum method
```

SymbolSolver 能知道：

> 它属于当前 repository。

但 V1 没给它建立 Symbol。

---

## 情况二：Target 是外部类型

例如：

```text
String
List
RedisTemplate
RocketMQTemplate
```

记：

```text
resolved_external
```

并且：

> 不进入 Graph。

---

# 二十七、CALLS 是如何生成的

对于所有：

```java
MethodCallExpr
```

例如：

```java
closePayOrder(orderSn);
```

首先：

```java
Node owner = callableOwner(call);
```

也就是：

> 这个调用属于哪个方法/构造器？

如果找到：

```text
closeTimeoutOrder()
```

则：

```text
source =
M:...#closeTimeoutOrder(...)
```

然后：

```java
ResolvedMethodDeclaration target =
    call.resolve();
```

得到真实目标声明。

再：

```java
callableKey(target, false)
```

得到：

```text
M:...#closePayOrder(String)
```

于是：

```text
closeTimeoutOrder
       |
       | CALLS
       ▼
closePayOrder
```

---

# 二十八、`callableOwner()` 是一个很值得理解的小实现

不是简单：

```text
call.findAncestor(MethodDeclaration)
```

而是手动向上找。

原因在于嵌套结构。

例如：

```java
void foo() {

    list.forEach(x -> {
        bar();
    });
}
```

当前设计：

```text
bar()
```

归属：

```text
foo()
```

因为 Lambda 并不会阻断 owner 查找。

这表示：

> `bar()` 出现在 foo 的源码执行结构中。

---

但是：

```java
void foo() {

    Runnable r = new Runnable() {
        public void run() {
            bar();
        }
    };
}
```

`bar()` 不能算：

```text
foo CALLS bar
```

因为它其实属于：

```text
anonymous class.run()
```

而当前 Anonymous Method 又没有稳定入图。

所以 `callableOwner()` 遇到：

```text
anonymous ObjectCreationExpr
```

会返回：

```text
null
```

宁可不建这条边。

这是非常典型的：

> **Precision First。**

---

# 二十九、CONSTRUCTS 与 CALLS 很相似

对于：

```java
new ServiceException("...");
```

AST：

```text
ObjectCreationExpr
```

resolve 后得到：

```text
ServiceException#<init>(String)
```

于是：

```text
closePayOrder
      |
      | CONSTRUCTS
      ▼
ServiceException#<init>(String)
```

为什么不也叫 CALLS？

因为构造关系单独保留后：

```text
普通方法调用
```

与：

```text
对象创建
```

语义不会混在一起。

这也就是 Tool Agent 的：

```text
find_callees
```

为什么 Observation 最后要把：

```text
calls
constructs
```

分开。

---

# 三十、最后讲本讲最复杂的一条边：OVERRIDES

假设：

```java
interface OrderService {
    void createTicketOrder(Request req);
}
```

实现：

```java
class OrderServiceImpl
    implements OrderService {

    @Override
    public void createTicketOrder(Request req) {}
}
```

需要：

```text
OrderServiceImpl#createTicketOrder
       |
       | OVERRIDES
       ▼
OrderService#createTicketOrder
```

---

# 三十一、为什么不能只看 `@Override`

因为：

```java
@Override
```

只是源码 annotation。

而：

```text
Graph
```

需要表达：

> Java 类型系统中真实成立的覆盖关系。

有些代码：

```text
可能没写 @Override
但依然 override。
```

而且：

```text
static method
private method
```

不能算正常 override。

所以当前并不依赖 annotation。

---

# 三十二、为什么也不能只判断“同名 + 同参数数目”

例如：

```java
interface Api<T> {
    T find(T id);
}
```

实现：

```java
class StringApi
    implements Api<String> {

    public String find(String id) {}
}
```

父方法看起来：

```text
find(T)
```

子方法：

```text
find(String)
```

如果字符串比较：

```text
T != String
```

就会错误认为没有 Override。

所以需要：

```text
Generic Substitution
```

---

# 三十三、当前 OVERRIDES 是怎么做的

先拿：

```java
ResolvedMethodDeclaration child
```

然后：

```java
child.declaringType()
    .getAncestors(true)
```

得到所有祖先类型。

例如：

```text
OrderServiceImpl
      ↓
OrderService
      ↓
BaseService
...
```

然后对祖先方法：

```text
getDeclaredMethods()
```

逐个比较。

---

# 三十四、先排除明确不可能的情况

当前会跳过：

```text
static
private
```

还会处理：

```text
package-private
```

跨 package 时不可覆盖。

也就是说不是单纯：

```text
method name == child name
```

---

# 三十五、泛型替换是怎么完成的

这里利用：

```java
ancestor.typeParametersMap()
```

例如：

```text
Api<T>
```

在：

```text
Api<String>
```

这个祖先上下文里，

就把：

```text
T
```

替换成：

```text
String
```

然后：

```text
find(T)
```

变成：

```text
find(String)
```

再进行比较。

这是 SymbolSolver 真正带来的高级能力之一。

---

# 三十六、最后通过 `isSubSignature()`

代码核心思想：

```text
child signature
是不是
ancestor method usage 的合法 subsignature
```

再检查：

```text
return type compatible
```

成立才建立：

```text
OVERRIDES
```

所以当前这条边并不是：

> 文本推测。

而是：

> 基于 Java 类型解析和继承关系推导出的确定关系。

因此 `resolutionKind`：

```text
DERIVED_EXACT
```

而 CALLS：

```text
SYMBOL_SOLVER_EXACT
```

这个差异也很合理。

---

# 三十七、为什么 OVERRIDES 对后面的 RAG 特别重要

假设 Controller：

```java
private OrderService orderService;

orderService.createTicketOrder(req);
```

CALLS 静态解析：

```text
Controller#createTicketOrder
          |
          | CALLS
          ▼
OrderService#createTicketOrder
```

它不能直接说：

```text
OrderServiceImpl
```

因为静态声明就是：

```text
OrderService
```

但是再查询：

```text
incoming OVERRIDES
```

可以找到：

```text
OrderServiceImpl#createTicketOrder
          |
          | OVERRIDES
          ▼
OrderService#createTicketOrder
```

于是完整静态关系：

```text
Controller
    ↓ CALLS
OrderService interface method
    ↑ OVERRIDES
OrderServiceImpl implementation method
```

这正是现在：

```text
find_callees
+
find_implementations
```

能够组成两步 Tool 链的基础。

---

# 三十八、`attempt()` 是整个 Analyzer 的容错边界

实际逻辑：

```java
try {
    operation
}
catch (AnalysisContractException) {
    throw
}
catch (
    UnsolvedSymbolException |
    UnsupportedOperationException |
    IllegalArgumentException
) {
    diagnostic
    continue
}
```

这其实非常值得记。

---

## 可以容忍

```text
SymbolSolver 找不到某第三方类型

某 local type 没稳定身份

某方法 resolve 失败
```

这些属于：

```text
现实源码并不完全可解析。
```

于是：

```text
gap + continue
```

---

## 不能容忍

```text
duplicate symbol identity

Chunk/declaration 数量不一致

Symbol owner 不存在

Source location 不存在
```

属于：

> **系统自身数据契约坏了。**

直接：

```text
AnalysisContractException
```

终止。

---

# 三十九、所以这个 Analyzer 最核心的设计不是“尽量构更多边”

而是：

```text
可靠事实
→ Graph

无法确认
→ Diagnostics

内部不变量错误
→ Fail
```

可以浓缩成：

> **Resolve, don't guess.**

这是整个 Symbol Graph 的设计核心。

---

# 四十、本讲最终形成的完整流程

把这一讲所有内容连接起来：

```text
Repository
   ↓
扫描 src/main/java
   ↓
发现多个 Source Root
   ↓
CombinedTypeSolver
   ├── JavaParserTypeSolver
   └── ReflectionTypeSolver
   ↓
JavaSymbolSolver
   ↓
Parse all CompilationUnit
   ↓
┌──────────────────────────────┐
│ Pass 1                       │
│                              │
│ JavaSourceParser → Chunk     │
│ Declaration → SymbolRecord   │
│ Node → SymbolKey             │
│ Repository Symbol Table      │
└──────────────────────────────┘
   ↓
全部 Symbol 已登记
   ↓
┌──────────────────────────────┐
│ Pass 2                       │
│                              │
│ extends → EXTENDS            │
│ implements → IMPLEMENTS      │
│ MethodCall → CALLS           │
│ new → CONSTRUCTS             │
│ type hierarchy → OVERRIDES   │
└──────────────────────────────┘
   ↓
addEdge()
   │
   ├── internal + indexed
   │      → 写 Edge
   │
   ├── internal + not indexed
   │      → diagnostic
   │
   └── external
          → skip
   ↓
Result
├── chunks
├── symbols
├── edges
└── diagnostics
```

---

# 四十一、这一讲最重要的 6 个知识点

如果你以后面试只需要复述核心，记住这 6 个：

1. **JavaParser 负责 AST，SymbolSolver 负责把名称解析成真实声明。**

2. **采用 Repository 级 Two-Pass：第一阶段先建立完整 Symbol Table，第二阶段再构建跨文件关系。**

3. **Symbol 使用稳定自然身份：`类型 + 方法名 + 擦除后的声明参数类型`，而不是数据库 ID 或简单方法名。**

4. **CALLS 指向静态解析声明；接口到实现通过 OVERRIDES/IMPLEMENTS 单独建模，不猜 Spring Runtime Bean。**

5. **每个 Symbol 必须映射回 CODE Chunk，因此 Graph 只负责导航，最终 RAG Evidence 仍然是原来的 Chunk。**

6. **解析失败可以降级并记录 Diagnostics，但重复身份、非法引用等内部契约错误必须 Fail Fast。**

---

下一步最适合继续进入第二讲：**这些 `SymbolRecord / SymbolEdgeRecord` 如何变成 PostgreSQL 中的 `code_symbol / code_symbol_edge`，为什么要做原子 Repository Snapshot，以及 `CodeGraphStore` 如何实现 callers/callees/邻居查询。**