我已经按“当前有效文档 → 项目背景文档 → JavaParser 调研与学习文档 → 当前实现代码 → 实际解析产物”的顺序阅读了一遍。

结论先说：这里的 JavaParser 模块不是要构建完整的 Java 语义模型，而是一个“面向检索的语法结构提取器”。它把 Java 21 源码解析成 AST，然后只选择类、接口、方法和构造方法，转换成扁平的 Code Chunk，保留符号名、签名、注解、Javadoc、所属类、文件路径以及精确行号，供后续关键词检索、向量检索和 Citation 使用。

## 一、项目背景与当前定位

DevContext-Java 的知识来源是已经存在的 `my12306` 项目：

- Java 源码包含真正的实现，例如购票、令牌桶、订单、支付和网关逻辑。
- Markdown 文档包含设计原因，例如为什么使用令牌桶、为什么调整事务边界。
- 实验文档包含性能数据和设计证据。

同一个问题往往需要同时查代码和文档。例如：

```text
为什么把 Feign 调用移出购票事务？具体涉及哪些代码？
```

这个问题同时需要：

```text
设计文档中的 Why
+
Java 源码中的 How
```

所以项目整体链路是：

```text
Java Source ──JavaParser──→ Code Chunk ─┐
                                        ├─→ Embedding / PostgreSQL
Markdown ──Heading Parser──→ Doc Chunk ─┘
                                              │
                                              ▼
                                  Keyword / Vector / RRF
                                              │
                                              ▼
                              可定位到文件与行号的检索结果
```

原始规划曾设想继续做 DOC/CODE/MIXED Router、LangGraph、上下文评估和答案生成，但当前有效文档已经将范围收紧为“可验证的检索闭环”：当前版本不生成答案、不修改源项目，也不实现 SymbolSolver、调用图和增量索引。这一优先级在 [docs/README.md](D:/Java-learning/DevContext/docs/README.md:3)、[README.md](D:/Java-learning/DevContext/README.md:1) 和 [implementation-plan.md](D:/Java-learning/DevContext/docs/implementation-plan.md:1) 中已经明确。

因此，目前 JavaParser 的职责只有三件事：

1. 准确识别 Java 语法结构。
2. 在不破坏源码边界的前提下生成检索 Chunk。
3. 提供可以回到原始文件核验的路径和行号。

它不是编译器，也不是调用关系分析器。

---

## 二、Java 解析模块在系统中的位置

JavaParser 被做成了一个独立的 Java 21 Maven CLI，而主系统是 Python。

入口链路如下：

```text
devcontext ingest
    │
    ▼
Python: ingestion/pipeline.py
    │
    ▼
JavaParserRunner.parse()
    │
    ├─ mvn -q package
    ├─ java -jar devcontext-java-parser.jar
    └─ 读取 artifacts/java-chunks.jsonl
             │
             ▼
       list[Python Chunk]
             │
             ├─ 构造 embedding_text
             ├─ 生成 1024 维向量
             └─ 写入 PostgreSQL knowledge_chunk
```

Python 侧的启动逻辑在 [java_parser_runner.py](D:/Java-learning/DevContext/src/devcontext/ingestion/java_parser_runner.py:12)：

- 每次解析前执行 `mvn -q package`。
- Maven Shade Plugin 把 JavaParser、Jackson 和本项目代码打成可执行 fat JAR。
- 执行：

```text
java -jar devcontext-java-parser.jar
    --code-root <my12306>
    --output <artifacts/java-chunks.jsonl>
    --repository my12306
```

- Java 侧输出 JSONL，一行一个 Chunk。
- Python 逐行执行 `json.loads()`，再通过 `Chunk.from_dict()` 转成统一的 Python 数据模型。
- JSONL 某一行损坏时，错误会精确报告到输出文件的行号。

独立进程的好处是：

- JavaParser 原生运行在 Java 环境里，不需要 Python/JVM 桥接。
- Java 解析部分可以脱离 Python、数据库和 Docker 单独测试。
- CRLF、AST Range、注解边界等问题可以直接通过 JUnit 验证。
- Java 与 Python 之间只通过稳定的 JSONL 数据契约通信。

对应 Maven 配置见 [pom.xml](D:/Java-learning/DevContext/java-parser/pom.xml:12)。

---

## 三、Java 文件是怎样被发现的

Java CLI 入口是 [Main.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/Main.java:23)。

### 1. 解析命令行参数

支持三个参数：

```text
--code-root    Java 项目根目录，必填
--output       JSONL 输出文件，必填
--repository   仓库逻辑名称，默认 my12306
```

代码根目录会转换成绝对、规范化路径；如果目录不存在，立即失败。

### 2. 扫描文件

`findJavaFiles()` 使用 `Files.walk(root)` 递归扫描，但只接受同时满足以下条件的文件：

- 是普通文件；
- 文件名以 `.java` 结尾；
- 路径中包含 `/src/main/java/`；
- 相对路径的任何一段都不是 `target`、`.git` 或 `.idea`。

代码见 [Main.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/Main.java:66)。

这意味着：

- 生产源码会被扫描。
- `src/test/java` 不会被扫描。
- Maven 的 `target` 构建产物不会重复索引。
- 不会扫描任意位置的 Java 示例文件。
- `package-info.java`、枚举文件等只要位于 `src/main/java` 仍会被解析，但不一定能产生受支持的 Chunk。

所有文件按路径自然排序，因此输出顺序是稳定的。

---

## 四、单个 Java 文件的完整解析过程

核心实现在 [JavaSourceParser.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java:29)。

### 第一步：创建并复用 JavaParser

构造器创建一个 JavaParser 实例：

```java
ParserConfiguration configuration = new ParserConfiguration()
        .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
        .setCharacterEncoding(StandardCharsets.UTF_8);
this.parser = new JavaParser(configuration);
```

主要配置是：

- Java 语言级别：Java 21；
- 字符集：UTF-8；
- 没有配置 SymbolSolver；
- 同一个 Parser 实例在整个仓库扫描期间顺序复用。

由于没有 SymbolSolver，解析器只需要源码文本，不需要先编译整个 Maven 多模块工程，也不需要完整加载所有依赖 JAR。

### 第二步：读取源码并生成 CompilationUnit

每个文件先被完整读取成 UTF-8 字符串：

```java
String source = Files.readString(file, StandardCharsets.UTF_8);
ParseResult<CompilationUnit> result = parser.parse(source);
```

`CompilationUnit` 可以理解成一个 Java 文件的 AST 根节点，下面包含：

- package；
- import；
- 类型声明；
- 字段；
- 方法；
- 构造方法；
- 语句和表达式等。

但本项目不会把整棵 AST 序列化出去，只会选择其中一部分节点转换成 Chunk。

### 第三步：严格检查解析结果

代码先检查是否存在 `CompilationUnit`，然后检查 `result.isSuccessful()`：

```java
CompilationUnit unit = result.getResult().orElseThrow(...);

if (!result.isSuccessful()) {
    throw new IllegalArgumentException("Parse problems: " + result.getProblems());
}
```

这里采取的是偏严格策略：

- 完全没有 AST：文件失败。
- JavaParser 虽然恢复出了部分 AST，但存在语法 Problem：仍然判定失败。
- 不使用存在错误的半残 AST 继续生成 Chunk。

原因是半残 AST 最危险的地方不只是内容缺失，而是可能产生错误的 Range；错误 Range 会进一步污染源码切片、行号 Citation 和评测 Ground Truth。

### 第四步：提取文件级 Metadata

解析成功后，先得到：

- `relativePath`：相对代码根目录的路径，统一使用 `/`；
- `module`：取路径中 `src` 前一段；
- `packageName`：从 `CompilationUnit.getPackageDeclaration()` 读取。

例如：

```text
services/ticket-services/src/main/java/edu/.../TicketServiceImpl.java
```

得到：

```text
file_path   = services/ticket-services/src/main/java/edu/.../TicketServiceImpl.java
module      = ticket-services
package_name= edu....
```

`module` 是基于目录结构的启发式推导，不是读取 Maven `artifactId`。实现见 [JavaSourceParser.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java:196)。

### 第五步：递归遍历 AST

项目分别执行三次 `findAll`：

```java
unit.findAll(ClassOrInterfaceDeclaration.class)
unit.findAll(MethodDeclaration.class)
unit.findAll(ConstructorDeclaration.class)
```

见 [JavaSourceParser.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java:56)。

`findAll` 是递归的前序遍历，不只检查文件最外层节点。因此：

- 顶层类会找到；
- 顶层接口会找到；
- 命名内部类和内部接口也会找到；
- 内部类的方法会找到；
- 接口方法和抽象方法没有方法体，但仍然是 `MethodDeclaration`，也会找到；
- 方法重载会分别产生不同的 METHOD Chunk；
- 一个文件可以产生多个类型 Chunk、多个方法 Chunk和多个构造器 Chunk。

单文件输出顺序是：

```text
所有 CLASS / INTERFACE
→ 所有 METHOD
→ 所有 CONSTRUCTOR
```

因为代码使用了三段独立循环。

---

## 五、不同 AST 节点怎样变成 Chunk

当前一共输出四种代码 Chunk：

| Chunk 类型 | AST 节点 | `content` 形态 | 主要用途 |
|---|---|---|---|
| `CLASS` | 非接口的 `ClassOrInterfaceDeclaration` | 人工构造的类摘要 | 回答“这个类是什么、有哪些能力” |
| `INTERFACE` | 接口 `ClassOrInterfaceDeclaration` | 人工构造的接口摘要 | 回答“接口提供哪些能力” |
| `METHOD` | `MethodDeclaration` | 从原文件精确切出的完整声明/方法体 | 主要代码检索单位 |
| `CONSTRUCTOR` | `ConstructorDeclaration` | 从原文件精确切出的完整构造方法 | 单独表达对象初始化逻辑 |

### 1. CLASS / INTERFACE：生成摘要，不保存整类源码

类型处理函数是 `typeRecord()`，见 [JavaSourceParser.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java:76)。

类型摘要按固定顺序拼接：

```text
Javadoc
注解
类型声明 {
  字段声明
  构造方法签名;
  方法签名;
}
```

类似：

```java
@Service
public class TicketServiceImpl implements TicketService {
  private final OrderRemoteService orderRemoteService;
  public TicketServiceImpl(OrderRemoteService orderRemoteService);
  public TicketPurchaseRespDTO purchaseTicket(TicketPurchaseReqDTO requestParam);
  private void doPurchaseInTransaction(...);
}
```

类声明由本项目手工组装，会保留：

- 修饰符；
- `class` 或 `interface`；
- 类型名称；
- 泛型参数；
- `extends`；
- `implements`；
- `permits`。

字段会进入类摘要，但字段本身不会产生独立 Chunk。

之所以不保存整个类源码，是因为整个类可能包含几十个方法：

- 向量会把大量不同职责平均在一起；
- 一个类 Chunk 会和全部方法 Chunk 大面积内容重叠；
- 检索结果容易被同一个大类占满；
- 会浪费上下文预算。

因此这里采用“地图 + 地点”的设计：

```text
CLASS/INTERFACE 摘要 = 地图，告诉你这个类型有哪些成员
METHOD Chunk         = 地点，告诉你某个能力怎么实现
```

### 2. METHOD：保存完整原始方法源码

方法记录包含：

- 方法所属的最近一层命名类型；
- 方法名；
- 可读签名；
- 注解；
- Javadoc；
- 完整方法源码；
- 起止行号。

签名通过：

```java
method.getDeclarationAsString(true, true, true)
```

生成，大致包含：

- 可见性和部分修饰符；
- 返回类型；
- 方法名；
- 参数类型和参数名；
- `throws` 声明。

例如：

```text
public Mono<Void> filter(
    ServerWebExchange exchange,
    GatewayFilterChain chain
)
```

它不包含方法体；方法体放在 `content` 中。

对于接口方法或抽象方法，即使没有方法体，仍然会生成 METHOD Chunk，`content` 是对应的声明源码，例如：

```java
String purchaseTicket(String id);
```

### 3. CONSTRUCTOR：与方法分开保存

构造方法同样使用原始源码切片，但 `chunk_type` 是 `CONSTRUCTOR`。

这是必要的，因为构造方法：

- 没有返回类型；
- 名称等于类名；
- 语义上表达依赖注入、字段初始化、前置校验等对象构造行为。

如果把构造器混入 METHOD，会让“按类名精确匹配”和“按方法名匹配”变得含糊。

### 4. 所属类怎样确定

方法和构造器通过：

```java
callable.findAncestor(TypeDeclaration.class)
```

向上寻找最近的类型声明。

因此对嵌套类：

```java
class Outer {
    class Inner {
        void execute() {}
    }
}
```

`execute` 的 `class_name` 是 `Inner`，不是 `Outer.Inner`。当前模型没有保存完整嵌套类路径。

---

## 六、怎样从 AST Range 恢复原始源码

这是解析实现中最关键的部分。

JavaParser 的 AST 节点具有：

```text
Range
├─ begin: Position(line, column)
└─ end:   Position(line, column)
```

它的约定是：

- 行号从 1 开始；
- 列号从 1 开始；
- Range 的结尾是闭区间，即 `end` 指向的字符也属于节点。

本项目没有使用 `node.toString()` 作为方法内容，因为 `toString()` 会经过 Pretty Printer 重新格式化，可能改变：

- 缩进；
- 空行；
- 换行格式；
- 注释布局；
- 多行声明格式。

如果内容被重新格式化，而 `start_line`、`end_line` 仍指向原文件，就无法保证 Citation 与展示内容一致。

所以方法和构造器采用原始字符串切片，见 [JavaSourceParser.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/JavaSourceParser.java:173)：

```java
static String slice(String source, Range range) {
    int start = offset(source, range.begin);
    int end = Math.min(source.length(), offset(source, range.end) + 1);
    return source.substring(start, end);
}
```

过程是：

```text
AST Position(line, column)
        │
        ▼
offset(source, position)
        │
        ▼
原始字符串字符下标
        │
        ▼
source.substring(start, end + 1)
```

`+1` 是因为：

- JavaParser 的 `Range.end` 是包含在节点内的最后一个字符；
- Java `substring(start, end)` 的 `end` 是不包含的。

### CRLF 处理

`offset()` 同时处理三种换行：

- `\n`
- `\r\n`
- `\r`

当遇到 `\r\n` 时，两字符被识别成一次换行，防止 Windows 文件的行号和字符偏移错位。

JUnit 测试专门使用 CRLF 文件验证：

- 方法 `start_line` 和 `end_line`；
- 注解是否进入源码范围；
- 方法体是否保持 `\r\n`；
- module 是否正确识别。

测试见 [JavaSourceParserTest.java](D:/Java-learning/DevContext/java-parser/src/test/java/io/devcontext/parser/JavaSourceParserTest.java:18)。

### 注解与 Javadoc 的边界

对于一个方法：

```java
/** Buy one ticket. */
@Override
public String purchaseTicket(String id) {
    return "ticket:" + id;
}
```

当前结果是：

```text
start_line = @Override 所在行
content    = @Override + 方法声明 + 方法体
javadoc    = "Buy one ticket..."，单独字段保存
```

也就是说：

- 注解通常属于方法 AST Range，所以进入 `content`。
- Javadoc 通常不属于方法节点 Range，所以不进入 `content`。
- Javadoc 通过 `getJavadocComment()` 单独提取。
- 方法体内部的 `//` 和 `/* ... */` 注释会随原始切片保留。
- 方法或类上方的非 Javadoc 普通注释既不在 Range 内，也没有独立字段，因此会丢失。

---

## 七、最终解析出了哪些信息

Java 输出对象是 [ChunkRecord.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/ChunkRecord.java:5)，一共 17 个字段。

| 字段 | 代码 Chunk 中的含义 | 来源 |
|---|---|---|
| `repository` | 仓库逻辑名称，如 `my12306` | CLI 参数 |
| `source_type` | 固定为 `CODE` | Java 解析器常量 |
| `chunk_type` | `CLASS`、`INTERFACE`、`METHOD` 或 `CONSTRUCTOR` | AST 节点类型 |
| `file_path` | 相对 code root 的源码路径，分隔符统一为 `/` | `root.relativize(file)` |
| `module` | `src` 前一段路径，如 `ticket-services` | 路径启发式 |
| `package_name` | Java package 全名 | `CompilationUnit` |
| `class_name` | 类型名；方法/构造器取最近祖先类型 | 类型节点或 `findAncestor` |
| `symbol_name` | 类型名、方法名或构造器名 | AST 节点名称 |
| `signature` | 类型声明或可调用成员签名 | 手工类声明 / `getDeclarationAsString` |
| `annotations` | 注解字符串数组，如 `["@Override"]` | `NodeWithAnnotations` |
| `javadoc` | Javadoc 原始内容，去掉首尾空白 | `NodeWithJavadoc` |
| `title` | 代码 Chunk 固定为 `null` | 为文档模型预留 |
| `heading_path` | 代码 Chunk 固定为空数组 | 为文档模型预留 |
| `content` | 方法/构造器原始源码，或类型摘要 | Range 切片 / 摘要拼接 |
| `start_line` | AST 节点起始行，1-based | `range.begin.line` |
| `end_line` | AST 节点结束行，1-based | `range.end.line` |
| `content_hash` | `content` 的 SHA-256 | Java 解析器计算 |

这些字段通过 Jackson 的 `SNAKE_CASE` 策略输出，所以 Java 的：

```text
sourceType
chunkType
filePath
packageName
```

会变成：

```text
source_type
chunk_type
file_path
package_name
```

见 [Main.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/Main.java:32)。

Python 的 `Chunk` 使用同样的 snake_case 字段，见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:8)。

需要注意一个当前代码事实：如果删掉 Jackson 的 `SNAKE_CASE` 配置，`source_type`、`chunk_type` 等必填字段会缺失，当前 `Chunk.from_dict()` 会在构造 `Chunk` 时失败，随后被包装成“Invalid Java parser output”，并不是无声接受。

---

## 八、哪些信息只是包含在文本中，哪些是真正结构化出来的

这是理解当前解析能力的关键。

### 已结构化成独立字段的信息

- 仓库；
- 相对文件路径；
- 模块；
- package；
- 最近所属类；
- 符号名；
- Chunk 类型；
- 方法/构造器/类型签名；
- 注解；
- Javadoc；
- 起止行号；
- 内容哈希。

这些字段可以直接用于筛选、精确匹配、展示和评测。

### 存在于 `content` 或 `signature`，但没有拆成独立字段的信息

- 方法返回类型；
- 参数类型和参数名；
- `throws`；
- 字段声明；
- `extends`、`implements`、`permits`；
- 方法体中的局部变量；
- 方法调用表达式；
- `if`、`for`、`try` 等控制流；
- 字符串常量；
- 方法体内部注释。

这些内容能被关键词或向量检索命中，但不能直接写类似：

```sql
WHERE return_type = 'TicketPurchaseRespDTO'
```

因为当前数据库没有 `return_type` 这样的结构化列。

### AST 里存在，但当前完全没有输出的信息

- imports；
- package-info 的文档语义；
- 每一个参数的独立结构；
- 每一个字段的独立 Chunk；
- initializer block；
- lambda 的独立 Chunk；
- 方法调用关系；
- 引用目标；
- 跨文件继承关系；
- 接口实现绑定；
- 重写/重载关系图；
- 控制流图；
- 数据流图。

所以虽然 JavaParser 生成的 AST 比最终 JSONL 丰富得多，但本项目刻意只投影出检索闭环需要的最小子集。

---

## 九、解析结果怎样进入检索系统

Java 侧生成 JSONL 后，Python 会转成统一的 `Chunk`。

### 1. 关键词检索文本

`keyword_text()` 会拼接：

```text
symbol_name
class_name
signature
title
heading_path
file_path
javadoc
content
```

见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:40)。

因此像下面这些查询比较适合关键词检索：

```text
purchaseTicket
TicketServiceImpl
@Transactional
RDelayedQueue
```

数据库还对符号名、类名和签名设置了不同的精确匹配权重，见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:95)。

### 2. 向量检索文本

`embedding_text()` 只拼接：

```text
signature / symbol_name / title
Javadoc
content
```

不把路径、行号、Hash 等工程噪声放进 Embedding。

代码见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:53)。

### 3. 入库

代码 Chunk 与 Markdown Chunk 合并后：

```text
Code Chunks + Document Chunks
→ embedding_text()
→ 1024 维 embedding
→ knowledge_chunk
```

入口见 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:22)，表结构见 [001_schema.sql](D:/Java-learning/DevContext/sql/001_schema.sql:4)。

`knowledge_chunk` 同时保存：

- 解析 Metadata；
- 原始/摘要内容；
- 关键词检索文本；
- 1024 维向量；
- Embedding 模型；
- 行号和 Hash。

---

## 十、解析失败怎样处理

每个文件都有独立的 `try/catch`：

```text
文件 A 成功 → 写入它的全部 Chunk
文件 B 失败 → stderr 输出 PARSE_ERROR，继续文件 C
文件 C 成功 → 正常写入
```

失败日志包含相对文件路径和错误消息。

全部结束后输出：

```text
JAVA_PARSE_SUMMARY
scanned=...
parsed=...
failed=...
chunks=...
output=...
```

见 [Main.java](D:/Java-learning/DevContext/java-parser/src/main/java/io/devcontext/parser/Main.java:40)。

如果扫描到了文件，但所有文件都解析失败，Java CLI 会抛异常并以失败退出；Python 的 `subprocess.run(check=True)` 随后中止 ingestion。

如果只是部分文件失败，当前 Java CLI 仍会正常退出，Python 会继续处理已经生成的 Chunk。也就是说，部分失败主要依靠 stderr 和最终 summary 暴露，目前没有单独生成结构化的失败报告文件。

---

## 十一、当前真实产物

项目自带的验证报告记录了 2026-09-22 的真实运行结果，见 [verification.md](D:/Java-learning/DevContext/docs/verification.md:15)：

```text
扫描 Java 文件：218
解析成功：       218
解析失败：       0
代码 Chunk：     538
```

我又核对了当前 [java-chunks.jsonl](D:/Java-learning/DevContext/artifacts/java-chunks.jsonl:1)，538 条代码记录的类型分布是：

| Chunk 类型 | 数量 |
|---|---:|
| `CLASS` | 165 |
| `INTERFACE` | 38 |
| `METHOD` | 316 |
| `CONSTRUCTOR` | 19 |
| 合计 | 538 |

另外：

- 538 条记录分布在 206 个源码文件中；
- 这说明“文件被成功解析”和“文件产生受支持的 Chunk”不是同一件事；
- 218 个解析成功文件中，有 12 个没有命中当前支持的类/接口/方法/构造器节点组合；
- 当前产物中所有 Chunk 都有 `class_name` 和 `package_name`；
- 288 个 Chunk 有 Javadoc；
- 286 个 Chunk 至少有一个注解。

---

## 十二、当前解析没有做什么

### 1. 没有 SymbolSolver

当前只依赖：

```text
javaparser-core
```

没有：

```text
javaparser-symbol-solver-core
```

因此它知道：

```java
orderService.createOrder(request);
```

是一个方法调用表达式，但不知道：

- `orderService` 最终对应哪个具体类型；
- `createOrder` 对应哪个源文件中的声明；
- 这个调用是接口方法还是实现类方法；
- 哪些方法反过来调用了当前方法。

所以当前不能可靠回答：

```text
谁调用了 scanTimeoutOrder？
purchaseTicket 最终调用链是什么？
某个接口有哪些实现类？
```

这些需要 Symbol Resolution、Reference Resolution 或 Call Graph，是后续阶段能力。

### 2. enum、record、`@interface` 没有类型级 Chunk

类型遍历只处理：

```java
ClassOrInterfaceDeclaration
```

所以：

```java
enum PayChannelEnum { ... }
record OrderResult(...) { ... }
@interface InternalApi { ... }
```

不会产生类型级 Chunk。

但其中显式声明的普通方法仍可能被 `findAll(MethodDeclaration.class)` 找到，于是会出现：

```text
有 PayChannelEnum.findByName 的 METHOD Chunk
但没有 PayChannelEnum 自身的 ENUM/CLASS Chunk
```

这会丢失枚举常量列表和类型级职责说明。

如果后续修复，需要同时扩展：

- AST 节点处理；
- `chunk_type` 枚举；
- PostgreSQL CHECK 约束；
- 类摘要构造逻辑。

### 3. 不解析测试源码

因为扫描条件强制要求 `/src/main/java/`，所以：

```text
src/test/java
```

完全不会进入索引。

这符合当前“面向生产实现检索”的范围，但意味着测试用例、测试意图和测试中的使用示例当前不可检索。

### 4. CLASS/INTERFACE 摘要与行号不完全自洽

这是一个重要限制。

类型记录的：

```text
start_line / end_line
```

来自原始类声明的完整 AST Range，覆盖整个类。

但它的：

```text
content
```

是人工拼出来的摘要，不是该行号范围的原始源码。

因此：

- METHOD/CONSTRUCTOR 的 `content` 和行号可以精确对应；
- CLASS/INTERFACE 的行号表示“原类型在文件中的范围”；
- CLASS/INTERFACE 的 `content` 不是这段范围的逐字原文。

所以严格 Citation 时，方法可以展示：

```text
TicketServiceImpl.java:69-172
```

并逐字核验；类型摘要则应标注为“类摘要”，不能声称是这段行号的原文。

### 5. 没有超长方法二次切分

当前一个方法始终对应一个 METHOD Chunk。

没有：

- 按语句切分；
- 按 Token 切分；
- 超长标记；
- Embedding 前局部截断逻辑。

现有语料里最大代码 Chunk 大约 6488 字符，暂时没有超过实际 Embedding 能力，但这仍是更大仓库下需要处理的问题。

### 6. 方法上方普通注释可能丢失

例如：

```java
// 这里必须先扣库存
public void purchaseTicket() {
}
```

如果这个普通注释不属于方法 AST Range，则：

- 不在 `content`；
- 也不是 Javadoc；
- 没有独立字段；
- 最终丢失。

但方法体内部注释会随原始源码切片保留。

### 7. module 是目录推断，不是 Maven 模型解析

当前：

```text
.../ticket-services/src/main/java/...
```

会得到：

```text
module = ticket-services
```

但解析器没有读取：

- 根 `pom.xml`；
- Maven reactor；
- `<modules>`；
- `<artifactId>`；
- Maven profile。

对于标准目录结构足够，但对非标准布局可能不准确。

---

## 十三、如何准确理解当前 JavaParser 能力

可以把它概括成三层：

```text
第一层：JavaParser
完整识别 Java 21 语法，构建 AST 和节点 Range

第二层：JavaSourceParser
只选择 Class / Interface / Method / Constructor
提取检索需要的 Metadata 和源码切片

第三层：Chunk / knowledge_chunk
保存扁平记录，用于 Keyword、Vector 和 RRF 检索
```

最重要的边界是：

> 当前实现解析的是“代码的语法结构和源码位置”，不是“代码中每一个名字最终指向谁”。

它能可靠回答：

```text
purchaseTicket 方法在哪里？
TicketService 接口声明了哪些方法？
某个方法有哪些注解？
某个类有哪些字段和方法签名？
某个方法的原始源码是什么？
方法位于文件的第几行？
```

但不能仅依靠当前结构化数据可靠回答：

```text
purchaseTicket 被哪些方法调用？
某次 Feign 调用最终绑定到哪个实现？
接口方法的全部实现类有哪些？
一个 DTO 字段在项目中经过了哪些数据流？
完整跨模块调用链是什么？
```

一句话总结：

> DevContext 当前的 Java 解析是“AST-aware、位置精确、面向检索的结构切片”，它保留了类型和方法的身份、声明、注解、Javadoc、原始代码与行号；但没有做符号求解、引用绑定和调用图分析。