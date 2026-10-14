继续第二章。

# 第 2 章：Symbol Graph 到底如何抽象一个 Java 程序

上一章解决的是：

> 为什么普通 RAG 之外还需要 Symbol Graph？

这一章解决的是另一个更基础的问题：

> **如果要把一个 Java 项目变成“图”，到底什么东西应该成为节点，什么关系应该成为边？**

这一步非常关键。因为图设计得不好，后面的 JavaParser、数据库、Graph Query 再复杂也没意义。

当前项目没有试图把 Java 程序中的所有东西都塞进图里，而是做了一个非常克制的 V1：

```text
节点：
CLASS
INTERFACE
METHOD
CONSTRUCTOR

关系：
EXTENDS
IMPLEMENTS
CALLS
CONSTRUCTS
OVERRIDES
```

理解这一套抽象以后，你才真正理解 Symbol Graph 的数据模型。

---

## 一、先看一个完整的小例子

假设项目里有下面几段代码。

```java
public abstract class BaseService {
    protected void validate() {
    }
}
```

接口：

```java
public interface OrderService {
    void createOrder(String orderSn);
}
```

实现类：

```java
public class OrderServiceImpl extends BaseService
        implements OrderService {

    @Override
    public void createOrder(String orderSn) {
        validate();
        saveOrder(orderSn);
    }

    private void saveOrder(String orderSn) {
        if (orderSn == null) {
            throw new ServiceException("invalid order");
        }
    }
}
```

Controller：

```java
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    public void create(String orderSn) {
        orderService.createOrder(orderSn);
    }
}
```

异常：

```java
public class ServiceException extends RuntimeException {

    public ServiceException(String message) {
        super(message);
    }
}
```

先完全不考虑代码如何解析。

如果让我们人工把它变成一张 Symbol Graph，大概应该是：

```text
T:BaseService

T:OrderService

T:OrderServiceImpl
    ├── EXTENDS ──────> T:BaseService
    └── IMPLEMENTS ───> T:OrderService

T:OrderController

T:ServiceException


M:OrderController#create(String)
    └── CALLS ────────> M:OrderService#createOrder(String)


M:OrderServiceImpl#createOrder(String)
    ├── OVERRIDES ────> M:OrderService#createOrder(String)
    ├── CALLS ────────> M:BaseService#validate()
    └── CALLS ────────> M:OrderServiceImpl#saveOrder(String)


M:OrderServiceImpl#saveOrder(String)
    └── CONSTRUCTS ───> C:ServiceException#<init>(String)
```

这张图已经能回答很多代码问题：

```text
OrderService 谁实现？
OrderServiceImpl 继承谁？
Controller 调用了哪个接口方法？
接口方法对应哪个实现方法？
createOrder 内部调用了什么？
saveOrder 创建了什么异常？
```

所以这一章真正要理解的，就是：

> **为什么恰好选择这些节点和边。**

---

# 二、为什么节点不是“文件”，而是 Symbol

一种最直觉的图可能是：

```text
OrderController.java
       ↓
OrderService.java
       ↓
OrderServiceImpl.java
```

但这种图太粗。

因为用户问的通常不是：

> 哪个文件依赖哪个文件？

而是：

> 哪个方法调用哪个方法？

例如一个 Java 文件里可能有：

```java
createOrder()
cancelOrder()
queryOrder()
closeOrder()
```

如果节点只是：

```text
OrderServiceImpl.java
```

那么：

```text
Controller#create
```

到底调用：

```text
createOrder
```

还是：

```text
queryOrder
```

就无法表达。

所以当前项目选择的基本单位不是：

```text
File
```

而是：

```text
Symbol
```

可以把 Symbol 理解成：

> **Java 程序里一个可以稳定指认的程序实体。**

---

# 三、为什么首先需要 CLASS 和 INTERFACE

最容易理解的是：

```text
CLASS
INTERFACE
```

因为 Java 大量结构关系发生在类型之间。

例如：

```java
class OrderServiceImpl extends BaseService
```

表达：

```text
OrderServiceImpl
    ↓ EXTENDS
BaseService
```

再例如：

```java
class OrderServiceImpl implements OrderService
```

表达：

```text
OrderServiceImpl
    ↓ IMPLEMENTS
OrderService
```

如果图里没有 Type Node，那么：

```text
继承
实现
```

这种关系根本没有落脚点。

所以：

```text
CLASS / INTERFACE
```

主要承担：

> **类型结构建模。**

---

# 四、为什么还必须有 METHOD

如果只有：

```text
Class
Interface
```

那么只能回答：

```text
谁继承谁？
谁实现谁？
```

却无法回答最重要的：

```text
谁调用谁？
```

因此必须把：

```java
createOrder()
saveOrder()
validate()
```

这些方法变成独立节点。

于是程序执行关系才能表达成：

```text
Method A
   ↓ CALLS
Method B
```

这也是当前 Symbol Graph 最核心的一类节点。

从 Repository Understanding 的角度看，可以这样理解：

```text
Type Node
负责“静态结构”

Method Node
负责“执行结构”
```

---

# 五、为什么 Constructor 也单独成为节点

构造器其实也属于：

```text
callable
```

但项目没有把它和普通 METHOD 混成一种。

原因是：

```java
foo();
```

和：

```java
new Foo();
```

语义不同。

前者：

```text
CALLS
```

后者：

```text
CONSTRUCTS
```

例如：

```java
throw new ServiceException("error");
```

如果只记录：

```text
saveOrder CALLS ServiceException
```

语义很奇怪。

因为：

```text
ServiceException
```

是类型，不是被调用的方法。

更准确的图是：

```text
saveOrder
     ↓ CONSTRUCTS
ServiceException#<init>(String)
```

所以构造器独立成：

```text
CONSTRUCTOR
```

之后就可以明确区分：

```text
方法调用
对象创建
```

---

# 六、为什么当前没有 FIELD 节点

这一点很值得理解。

Java 项目里当然有：

```java
private OrderService orderService;
```

也可以设计：

```text
FIELD: OrderController.orderService
```

甚至加入：

```text
READS
WRITES
REFERENCES
```

但 V1 没这么做。

这不是能力不足，而是范围控制。

如果加入 FIELD，马上会出现大量关系：

```text
Method READS Field
Method WRITES Field
Field TYPE_OF Class
Field ANNOTATED_BY ...
```

图规模和噪声都会明显增加。

而当前最核心的 Repository Q&A 问题主要是：

```text
调用链
接口实现
继承
对象创建
```

四种 Node 已经足够支撑。

所以当前设计遵循：

> **先建最有信息密度、最容易可靠解析的实体。**

而不是：

> Java AST 有什么就全部建图。

---

# 七、为什么没有直接加入“文件节点”和“模块节点”

同理。

完全可以未来做：

```text
MODULE
FILE
PACKAGE
```

然后：

```text
File CONTAINS Class
Module CONTAINS File
Package CONTAINS Class
```

但是目前没有必要。

原因在于现有 Chunk 已经保存：

```text
file_path
module
package_name
```

也就是说这些信息已经可以作为：

> 元数据

存在，

不一定需要升级成 Graph Node。

这里体现一个很重要的数据建模思想：

> **不是所有信息都必须成为节点。**

只有当某种实体需要：

```text
被独立查询
被关系连接
参与图遍历
```

时，才值得升格为 Node。

---

# 八、接下来理解五种 Edge

节点决定：

> 图里有什么东西。

Edge 决定：

> 这些东西之间可以表达什么事实。

当前的五种：

```text
EXTENDS
IMPLEMENTS
CALLS
CONSTRUCTS
OVERRIDES
```

其实可以分成两大类：

```text
类型结构关系

EXTENDS
IMPLEMENTS


执行/行为关系

CALLS
CONSTRUCTS
OVERRIDES
```

其中 `OVERRIDES` 比较特殊，它既和类型结构有关，也服务于执行路径恢复。

---

# 九、EXTENDS 表达的到底是什么

例子：

```java
class OrderServiceImpl extends BaseService
```

关系：

```text
OrderServiceImpl
    ↓ EXTENDS
BaseService
```

这条边非常直接。

它表达的是：

> **Java 类型继承事实。**

注意不是：

```text
OrderServiceImpl uses BaseService
```

也不是：

```text
OrderServiceImpl depends on BaseService
```

因为：

```text
EXTENDS
```

信息更精确。

以后用户问：

> `OrderDO` 的父类是什么？

Graph 可以直接查询：

```text
OrderDO
↓ EXTENDS
BaseDO
```

不需要依赖文本检索。

---

# 十、IMPLEMENTS 和 EXTENDS 为什么要分开

假设：

```java
class OrderServiceImpl
        extends BaseService
        implements OrderService
```

如果统一成：

```text
OrderServiceImpl
    ↓ INHERITS
BaseService

OrderServiceImpl
    ↓ INHERITS
OrderService
```

虽然都表示某种类型关系，但会丢失 Java 语义。

因为：

```text
BaseService
```

是 superclass，

而：

```text
OrderService
```

是 interface。

用户问：

> 哪个类实现 OrderService？

需要：

```text
incoming IMPLEMENTS
```

而不是把：

```text
所有 INHERITS
```

都查一遍。

所以 Edge Type 越精确：

> 上层 Tool 和 Retrieval Policy 越简单。

---

# 十一、CALLS 是整个图里最重要的一条边

例子：

```java
public void create(String orderSn) {
    orderService.createOrder(orderSn);
}
```

图：

```text
OrderController#create
        ↓ CALLS
OrderService#createOrder
```

这里有一个必须牢记的设计原则：

> **CALLS 表达静态可证明的调用目标。**

不是：

> 最终运行时一定执行哪个具体方法。

---

# 十二、为什么 Controller 不能直接 CALLS OrderServiceImpl

假设：

```java
private OrderService orderService;
```

调用：

```java
orderService.createOrder(orderSn);
```

源码层能可靠确认的是：

```text
变量静态类型 = OrderService
```

所以：

```text
CALLS
→ OrderService#createOrder
```

但运行时可能：

```text
Spring 注入 OrderServiceImpl
```

甚至以后出现：

```text
MockOrderServiceImpl
NewOrderServiceImpl
```

单靠静态源码里的这个调用表达式，不能证明具体实现。

如果 Graph 直接构造：

```text
Controller
↓ CALLS
OrderServiceImpl
```

那就是把：

```text
运行时推测
```

伪装成：

```text
静态事实。
```

当前项目明确避免这种做法。

---

# 十三、那怎么从接口方法找到真正实现？

这就是：

```text
OVERRIDES
```

存在的重要原因。

假设：

```text
OrderServiceImpl#createOrder
      ↓ OVERRIDES
OrderService#createOrder
```

那么用户如果问：

> Controller 调接口之后实际有哪些实现？

系统可以做：

```text
Controller#create
        ↓ CALLS
OrderService#createOrder
        ↑
        │ reverse OVERRIDES
        │
OrderServiceImpl#createOrder
```

也就是说：

> `CALLS` 和 `OVERRIDES` 两条关系组合后，才形成完整的接口调用理解。

这比直接猜实现要可靠得多。

---

# 十四、OVERRIDES 为什么不是 IMPLEMENTS

这是很多人第一次看这个图时容易混淆的地方。

类型之间：

```text
OrderServiceImpl
    ↓ IMPLEMENTS
OrderService
```

方法之间：

```text
OrderServiceImpl#createOrder
    ↓ OVERRIDES
OrderService#createOrder
```

两者表达不同层级。

可以记成：

```text
IMPLEMENTS
= Type → Type

OVERRIDES
= Method → Method
```

所以用户问：

> 哪个类实现了 OrderService？

用：

```text
IMPLEMENTS
```

用户问：

> `OrderService.createOrder` 的具体实现方法是什么？

用：

```text
OVERRIDES
```

---

# 十五、为什么需要同时保留这两个层级

如果只有：

```text
IMPLEMENTS
```

那么你知道：

```text
OrderServiceImpl implements OrderService
```

但是一个接口可能有：

```java
createOrder()
cancelOrder()
queryOrder()
```

你并不知道：

```text
哪个具体方法
```

对应：

```text
哪个具体 override。
```

而方法级：

```text
OVERRIDES
```

可以精确到：

```text
createOrder(String)
```

这对于代码问答非常关键。

---

# 十六、CONSTRUCTS 为什么不直接归入 CALLS

例子：

```java
throw new ServiceException("error");
```

技术上构造函数也是一种调用。

完全可以理论上表示：

```text
saveOrder
↓ CALLS
ServiceException#<init>
```

但是项目把它单独设计成：

```text
CONSTRUCTS
```

原因是：

> 对象创建本身有独立语义价值。

例如用户问：

> `closePayOrder` 在异常分支构造了哪个本地异常？

直接查：

```text
CONSTRUCTS
```

非常自然。

而且未来也容易扩展：

```text
Factory
DTO
Command
Exception
```

这些对象创建关系。

---

# 十七、为什么没有 USES / DEPENDS_ON

这也是一个很关键的设计选择。

很多 Code Graph 会加入：

```text
USES
DEPENDS_ON
REFERENCES
IMPORTS
READS
WRITES
```

看起来图更丰富，

但问题是这些关系非常容易产生大量噪声。

例如：

```java
List<OrderDO>
```

是不是：

```text
OrderService USES List
```

？

当然可以。

但这种关系对：

> “订单关闭如何释放座位？”

几乎没有帮助。

反而会让每个 Symbol 都连出几十上百个邻居。

所以当前 V1 优先保留：

> **对 Repository 行为理解价值最高的关系。**

这就是为什么只有 5 种 Edge。

---

# 十八、可以把当前 Graph 看成“两张叠在一起的图”

这是一个很有帮助的理解方式。

第一张：

## 类型图

```text
OrderServiceImpl
   ├── EXTENDS ───> BaseService
   └── IMPLEMENTS → OrderService
```

主要解决：

```text
继承
实现
类型层级
```

---

第二张：

## 调用图

```text
Controller#create
    ↓ CALLS
OrderService#createOrder

OrderServiceImpl#createOrder
    ↓ CALLS
BaseService#validate

OrderServiceImpl#createOrder
    ↓ CALLS
OrderServiceImpl#saveOrder
```

再用：

```text
OVERRIDES
```

把两张图连接起来：

```text
Type relation
     │
     ▼
Method implementation relation
```

所以整个 Symbol Graph 实际是：

> **Type Graph + Call Graph + Override Bridge。**

这个理解比单纯记五种 Edge 更有用。

---

# 十九、再看一个完整的 Graph

把前面的例子全部画出来：

```text
                       T:BaseService
                              ▲
                              │ EXTENDS
                              │
                    T:OrderServiceImpl
                       │              │
                       │ IMPLEMENTS   │ owns
                       ▼              ▼
                  T:OrderService   M:createOrder
                       ▲              │
                       │              ├── CALLS ──> M:validate
                       │              │
                       │              └── CALLS ──> M:saveOrder
                       │
                       │ OVERRIDES
                       │
                M:OrderService#createOrder


T:OrderController
       │
       └── M:create
             │
             │ CALLS
             ▼
      M:OrderService#createOrder


M:saveOrder
    │
    │ CONSTRUCTS
    ▼
C:ServiceException#<init>
```

注意：

```text
owns
```

这里只是帮助你理解 Type 和 Method 的所属关系。

当前项目没有把：

```text
OWNS
```

正式建成 Graph Edge，

而是保存在：

```text
ownerSymbolKey / owner_symbol_id
```

里。

这又体现：

> 不是什么关系都必须变成遍历 Edge。

---

# 二十、为什么 Owner 不做 Edge？

因为：

```text
Method 属于哪个 Class
```

主要是 Symbol 身份和组织信息。

它几乎每次都存在：

```text
每个 Method 都属于 Type
```

如果也放进 Graph adjacency：

```text
每个 Method
↔ Class
```

Graph Traversal 时会增加大量无意义的邻居。

当前做法：

```text
owner_symbol_id
```

作为属性引用，

需要时可查，

但不参与普通 CALLS/IMPLEMENTS 等邻接扩展。

这是一个很好的：

> **Metadata relationship vs Traversal relationship**

区分。

---

# 二十一、Graph 的设计还必须考虑“方向”

每条 Edge 都是有方向的。

例如：

```text
OrderServiceImpl
   ↓ IMPLEMENTS
OrderService
```

方向是：

```text
实现类 → 接口
```

但用户问：

> 谁实现了 OrderService？

查询方向却是：

```text
从 OrderService
查 incoming IMPLEMENTS
```

类似：

```text
A CALLS B
```

用户问：

> A 调谁？

查：

```text
outgoing CALLS
```

用户问：

> 谁调用 B？

查：

```text
incoming CALLS
```

所以：

> **Graph 数据的真实方向和查询方向是两个概念。**

后面 `find_callers` / `find_callees` 就建立在这个基础上。

---

# 二十二、这也是为什么 Edge 必须是有类型、有方向的

如果 Graph 只是：

```text
A --- B
```

那么根本无法回答：

```text
A 调 B
还是 B 调 A？

A 实现 B
还是 B 实现 A？
```

所以当前的：

```text
source_symbol
target_symbol
edge_type
```

三者缺一不可。

---

# 二十三、当前 Symbol Graph 的能力边界

到这里已经可以清楚看到：

当前 V1 擅长：

```text
静态调用关系
接口实现
方法 override
继承关系
构造关系
```

但它不擅长直接回答：

```text
Spring 最终注入哪个 Bean
MQ producer 对应哪个 consumer
哪个接口对应哪个 HTTP endpoint
哪个 Mapper 操作哪个表
哪个配置项控制哪个功能
```

这些需要未来更高一层：

```text
Repository Graph
```

---

# 二十四、Symbol Graph 和未来 Repository Graph 的关系

可以这样理解：

```text
Symbol Graph
=
Java 编译期程序结构


Repository Graph
=
框架 / 配置 / 基础设施语义
```

未来可能加：

```text
Controller Method
   ↓ EXPOSES
HTTP Endpoint

Producer
   ↓ PUBLISHES
MQ Topic

Consumer
   ↓ CONSUMES
MQ Topic

Mapper
   ↓ ACCESSES
DB Table
```

那么整个项目才会从：

```text
Java Symbol Graph
```

升级成真正：

```text
Repository Graph
```

所以当前 V1 是底座，而不是终点。

---

# 二十五、为什么 V1 不应该直接一步做到 Repository Graph

因为两类关系的可靠性来源不同。

例如：

```text
CALLS
```

可以用 Java 类型系统严格解析。

而：

```text
Producer publishes TOPIC_X
```

可能需要：

```text
Annotation
constant propagation
Spring configuration
RocketMQ API semantics
```

再如：

```text
Feign interface → remote controller
```

甚至可能需要跨服务配置推导。

如果 V1 一开始把这些混在一起，

你很难区分：

```text
确定的 Java 事实
```

和：

```text
框架语义推理。
```

所以当前先把：

```text
静态、确定、可靠
```

的 Symbol Graph 做扎实，是正确路线。

---

# 二十六、这一章最重要的设计思想

这章如果压缩成几个核心思想，可以记成：

> **第一，节点不是文件，而是具有稳定程序身份的 Symbol。**

> **第二，Type 负责结构，Method/Constructor 负责行为。**

> **第三，不同程序语义必须使用不同 Edge，而不是统一成模糊的 `RELATED_TO`。**

> **第四，CALLS 只表达静态可证明调用，运行时实现通过 OVERRIDES/IMPLEMENTS 分开建模。**

> **第五，只有对代码理解真正有价值、并且能够可靠获取的实体和关系才进入 V1。**

---

# 二十七、再往前一步理解：为什么 Graph 设计会影响后面的 Agent

现在你已经能看到：

后面 Tool Agent 的七个 Tool，并不是随便设计出来的。

例如：

```text
find_callers
```

之所以存在，

是因为 Graph 有：

```text
incoming CALLS
```

---

```text
find_callees
```

来源于：

```text
outgoing CALLS
+
CONSTRUCTS
```

---

```text
find_implementations
```

来源于：

```text
incoming IMPLEMENTS
或者
incoming OVERRIDES
```

---

```text
find_hierarchy
```

来源于：

```text
EXTENDS
IMPLEMENTS
```

所以：

> **Tool Design 实际上是 Graph Data Model 向 Agent 能力层的投影。**

如果 Graph 的 Edge 设计很模糊，

后面的 Tool 也一定很模糊。

---

# 下一章

下一步进入第 3 章：

# **Java 仓库如何被转换成这张 Symbol Graph**

这时才开始引入三个实现概念：

```text
JavaParser
SymbolSolver
Repository-level Two-Pass Analysis
```

但仍然会以设计思想为主。

重点回答：

> 为什么只解析 AST 不够？

> SymbolSolver 到底补充了什么？

> 为什么必须先扫描整个 repository？

> 为什么采用“第一遍建立 Symbol，第二遍建立 Edge”？

> 多模块 `src/main/java` 为什么会影响类型解析？

只有在解释这些问题时，才会穿插少量当前真实代码。