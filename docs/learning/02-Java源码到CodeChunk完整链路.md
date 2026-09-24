# 02 · Java 源码 → CodeChunk 完整链路

> 学习路线位置：**Parsing 层**（第 2 篇）
> 重点源码：`java-parser/src/main/java/io/devcontext/parser/`（3 个类，共约 380 行）
> 前置：`01-Chunk数据模型与双检索文本.md`（需要知道 `Chunk` 的 17 个字段）
> 本篇要完整回答的问题：**`PurchaseTicketTxService.java` 是怎样最终产生 `doPurchaseInTransaction` 这个 METHOD Chunk 的？**

---

## 1. 这一模块解决什么问题

Java 侧的解析要解决一个看似简单、实际不能靠字符串处理的问题：

```text
给定一个 .java 文件，找出"方法"的边界，并给出它的精确起止行
```

为什么不能靠正则或固定长度切分？因为 Java 里"找方法边界"不是一个字符串问题：

| 障碍 | 例子 |
|---|---|
| 大括号嵌套 | 方法体里有 `if` / `for` / lambda / 匿名内部类，每个都带 `{}` |
| 字符串字面量里可以有任意字符 | `throw new ServiceException("请检查车次是否存在");` 里甚至可以有 `}` |
| 注释里可以有任意字符 | `// TODO: 这里少一个 }` |
| 泛型让括号不对称 | `Map<Integer, List<PurchaseTicketPassengerDetailDTO>>` |
| 注解让"看起来像声明"的地方未必是声明 | `@Transactional(...)` 在方法前，也可能在类前 |

真实证据就在语料里。`doPurchaseInTransaction` 的方法体里有：

```java
Map<Integer, List<PurchaseTicketPassengerDetailDTO>> seatTypeMap = requestParam.getPassengers().stream()
        .collect(Collectors.groupingBy(PurchaseTicketPassengerDetailDTO::getSeatType,
                LinkedHashMap::new, Collectors.toList()));
```

这一段里既有 `<>` 嵌套，又有跨行的方法链，还有 `::` 方法引用。用正则找"第一个 `{` 到配对的 `}`"在这里必然出错。

所以这一层选择了 **JavaParser**：一个真正解析 Java 语法的库，能给出语法级别的、精确的 `Range`（起止行列）。

**这一层的输出质量，决定了后面所有层的能力上限。** 如果 `start_line` 错了，Citation 就是错的；如果边界切错了，embedding 就是错的；如果 `signature` 取值错了，关键词检索的权重臂就打不中。这也是为什么本篇要把 `Range → content` 这一段讲得最细。

---

## 2. 在完整系统中的位置

Java 侧是一个**完全独立的 Maven CLI**，Python 只负责调用它。

```text
┌─────────────────────── Python 进程 ───────────────────────┐
│                                                            │
│  cli.py: ingest                                            │
│      ↓                                                     │
│  ingestion/pipeline.py: ingest(settings)                    │
│      ↓                                                     │
│  ingestion/java_parser_runner.py: JavaParserRunner.parse()  │
│      │                                                     │
│      │  ① self.build()      → subprocess: mvn -q package    │
│      │  ② subprocess: java -jar ... --code-root --output    │
│      │  ③ 读回 JSONL → Chunk.from_dict(...)                 │
│      ↓                                                     │
│  list[Chunk]  ─────────────→ 交给 pipeline 的后续步骤         │
└────────────────────────────────────────────────────────────┘
                    │ ② 进程边界
                    ↓
┌────────────────── Java 进程（独立 JVM） ───────────────────┐
│  Main.main(args)                                           │
│      ↓                                                     │
│  findJavaFiles(root)        → List<Path>                   │
│      ↓  for each file                                      │
│  JavaSourceParser.parse(root, file, repository)            │
│      │   ├─ 读源文件（UTF-8）                                │
│      │   ├─ parser.parse(source) → CompilationUnit          │
│      │   ├─ findAll(ClassOrInterfaceDeclaration) → typeRecord│
│      │   ├─ findAll(MethodDeclaration)      → callableRecord│
│      │   └─ findAll(ConstructorDeclaration) → callableRecord│
│      ↓                                                     │
│  ChunkRecord（17 字段的 record）                             │
│      ↓  Jackson + SNAKE_CASE                               │
│  写入 JSONL（每行一条）                                       │
└────────────────────────────────────────────────────────────┘
```

**两个进程通过文件交换数据**（不是 socket、不是 RPC）：Java 侧写 `artifacts/java-chunks.jsonl`，Python 侧读回来。这个设计在第 9 节解释。

---

## 3. 必须先知道的最少概念

### 3.1 JavaParser 里本项目真正用到的 5 个类型

**只讲这 5 个。** 其余（`FieldDeclaration` 之外的节点、`SymbolSolver`、`LexicalPreservingPrinter`）当前项目都没用。

| 类型 | 一句话 | 在本项目里 |
|---|---|---|
| `CompilationUnit` | 一个 `.java` 文件解析出来的根节点（对应"编译单元"） | `parser.parse(source)` 的返回值，用来取 `package` 声明 |
| `ClassOrInterfaceDeclaration` | 类或接口声明 | `findAll` 遍历，每个产出一条 `CLASS` 或 `INTERFACE` 记录 |
| `MethodDeclaration` | 方法声明 | `findAll` 遍历，每条产出一条 `METHOD` 记录 |
| `ConstructorDeclaration` | 构造方法声明 | `findAll` 遍历，每条产出一条 `CONSTRUCTOR` 记录 |
| `Range` | 一个节点的**起止位置**，`begin` / `end` 各是一个 `Position` | 是 `content` 切片与 `start_line`/`end_line` 的唯一来源 |

### 3.2 `Position` 与 `Range` 的三个约定（必须记准）

```text
Position = (line, column)
  · line   : 1-based   ← 第 1 行是 1，不是 0
  · column : 1-based   ← 第 1 列是 1，不是 0

Range = (begin, end)
  · 闭区间           ← end 那一列"也算内容"，不是"到这一列之前为止"
```

这两个约定直接决定了后面 `offset()` 里那个 `+1` 和 `-1`。记错任何一个，所有切片都会偏一格。

### 3.3 `findAll` 的语义

`unit.findAll(MethodDeclaration.class)` 返回**这棵树里所有**该方法类型的节点，包括：

- 嵌套类（inner class）里的方法
- 匿名内部类里的方法
- 枚举 / record 里的方法（**这是后面第 11.1 节要讲的一个缺陷**）

顺序是**深度优先的前序遍历顺序**，也就是源码顺序。这一点保证了同一次解析的输出是稳定的。

### 3.4 `getDeclarationAsString(...)` 与"原文"的区别

```java
method.getDeclarationAsString(true, true, true)
//                          ↑     ↑     ↑
//                     includeModifiers, includeDescriptor, includeThrows
```

它返回的是**重新生成的单行声明字符串**，不是源码原文。真实对比：

```text
getDeclarationAsString(...,true,true,true) 得到（一行）：
public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)

源码原文（多行，保留原来怎么换行就怎么换行）：
    public PurchaseReservationResult doPurchaseInTransaction(
            PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn,
            Map<String, PassengerActualRespDTO> passengersById) {
```

**同一条信息，两个形态：单行版进 `signature` 字段，原文版进 `content` 字段。** 记住这个区别，第 4 节会用到。

### 3.5 JSON 输出的命名策略

```java
ObjectMapper mapper = new ObjectMapper();
mapper.setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
```

Java 的 `sourceType` 会被序列化成 `source_type`。Python 侧的 `Chunk.from_dict` 直接按 `snake_case` 键取值。**如果这行被删掉，Python 侧会收到 `sourceType`，`from_dict` 会找不到键，该字段静默变成默认值（`None`）——不会报错，只会字段为空。** 这是跨语言契约里最典型的"静默失败"，第 10 节把它列为误解之一。

---

## 4. 关键源码入口

| 位置 | 内容 |
|---|---|
| `Main.java:23-64` | `main`：参数解析 → 扫描 → 逐文件解析 → 写 JSONL → 打印汇总 |
| `Main.java:66-78` | ★ `findJavaFiles`：三个过滤条件 |
| `JavaSourceParser.java:32-37` | 构造：`JAVA_21` + UTF-8 |
| `JavaSourceParser.java:39-74` | ★ `parse`：解析 + 三类节点遍历 |
| `JavaSourceParser.java:76-120` | ★ `typeRecord`：CLASS / INTERFACE 摘要 |
| `JavaSourceParser.java:122-157` | ★ `callableRecord`：METHOD / CONSTRUCTOR |
| `JavaSourceParser.java:173-194` | ★ `slice` / `offset`：**本篇最重点** |
| `JavaSourceParser.java:196-203` | `detectModule`：从路径推 module |
| `JavaSourceParser.java:205-229` | `typeDeclaration`：拼类声明字符串 |
| `ChunkRecord.java:5-24` | 输出 DTO（17 字段） |
| `java-parser/src/test/.../JavaSourceParserTest.java` | JUnit 测试（含 CRLF 用例，本篇会逐字用到） |

---

## 5. Input / Output

### 5.1 Java CLI 的 Input / Output

**Input**（命令行参数，`Main.Arguments.parse`）：

```text
--code-root  <path>    如 D:\Java-learning\12306Project\12306\my12306
--output     <path>    如 D:\Java-learning\DevContext\artifacts\java-chunks.jsonl
--repository <name>    如 my12306（默认值就是 my12306）
```

**Output**：一个 JSONL 文件 + 一行 stdout 汇总。

真实的 stdout【实测】：

```text
JAVA_PARSE_SUMMARY scanned=218 parsed=218 failed=0 chunks=538 output=D:\Java-learning\DevContext\artifacts\java-chunks.jsonl
```

真实的 JSONL 中的一行（`doPurchaseInTransaction`，为便于阅读做了换行，实际是一整行）：

```json
{"repository":"my12306","source_type":"CODE","chunk_type":"METHOD",
 "file_path":"services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java",
 "module":"ticket-services",
 "package_name":"edu.swu.fcj.my12306.biz.ticketservice.service.impl",
 "class_name":"PurchaseTicketTxService",
 "symbol_name":"doPurchaseInTransaction",
 "signature":"public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)",
 "annotations":["@Transactional(rollbackFor = Exception.class)"],
 "javadoc":"* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。",
 "title":null,
 "heading_path":[],
 "content":"@Transactional(rollbackFor = Exception.class)\n    public PurchaseReservationResult doPurchaseInTransaction(\n ...（6488 字符）...",
 "start_line":69,
 "end_line":172,
 "content_hash":"519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c"}
```

### 5.2 每一步的中间产物

| 步骤 | 中间产物 | 形态 |
|---|---|---|
| 扫描 | `List<Path> files` | 218 个绝对路径（真实语料） |
| 解析 | `ParseResult<CompilationUnit>` | JavaParser 的解析结果包装 |
| 类型遍历 | `List<ClassOrInterfaceDeclaration>` | 每个文件 1 个（真实语料实测：203 个类型 Chunk 分布在 203 个文件上） |
| 方法遍历 | `List<MethodDeclaration>` | 全语料 316 条 |
| 构造器遍历 | `List<ConstructorDeclaration>` | 全语料 19 条 |
| 切片 | `String content` | METHOD 平均 436 字符，最大 6488 字符 |
| 输出 | `ChunkRecord` | 17 字段，序列化成一行 JSON |

---

## 6. 主执行流程

```text
main(args)
 │
 ├─ Arguments.parse(args)
 │     · 缺 --code-root 或 --output → 抛 IllegalArgumentException
 │
 ├─ root = args.codeRoot().toAbsolutePath().normalize()
 ├─ Files.isDirectory(root) 为假 → 抛异常
 ├─ output 的父目录不存在 → Files.createDirectories(output.getParent())
 │
 ├─ mapper.setPropertyNamingStrategy(SNAKE_CASE)
 ├─ parser = new JavaSourceParser()        ← 配置 JAVA_21 + UTF-8
 ├─ files = findJavaFiles(root)            ← 见 7.1
 │
 └─ try (BufferedWriter writer = ...)      ← 打开输出文件（一次，不逐文件开关）
     │
     └─ for (Path file : files)            ← 逐文件循环
         │
         ├─ try:
         │    records = parser.parse(root, file, repository)
         │        ├─ source = Files.readString(file, UTF_8)
         │        ├─ result = parser.parse(source)
         │        ├─ unit = result.getResult().orElseThrow(...)
         │        ├─ if (!result.isSuccessful()) throw ...
         │        ├─ relativePath = root.relativize(file).toString().replace('\\','/')
         │        ├─ module = detectModule(root.relativize(file))
         │        ├─ packageName = unit.getPackageDeclaration()...
         │        ├─ for (type : unit.findAll(ClassOrInterfaceDeclaration)) → typeRecord(...)
         │        ├─ for (method : unit.findAll(MethodDeclaration))         → callableRecord(...)
         │        └─ for (ctor : unit.findAll(ConstructorDeclaration))      → callableRecord(...)
         │
         │    for (record : records) writer.write(json); writer.newLine()
         │    chunks += records.size(); parsed++
         │
         └─ catch (Exception):
              failed++
              System.err.printf("PARSE_ERROR file=%s message=%s%n", ...)
              ← 不 rethrow，循环继续（见 7.7）
 │
 ├─ System.out.printf("JAVA_PARSE_SUMMARY scanned=%d parsed=%d failed=%d chunks=%d ...")
 └─ if (!files.isEmpty() && parsed == 0) throw new IllegalStateException("All Java files failed to parse")
```

**注意一个容易忽略的顺序细节**：`writer` 在整个循环之外打开，逐文件追加写入。所以 JSONL 的**行顺序 = 文件顺序 = `files.sort(Comparator.naturalOrder())` 的字典序**。这就是为什么 `artifacts/java-chunks.jsonl` 的前若干行总是 `services/gateway-services/...` 开头——`g` 在字典序里排在 `o`(order)、`p`(pay)、`t`(ticket)、`u`(user) 之前。

---

## 7. 关键源码逐段解释

### 7.1 第一部分：文件扫描 `findJavaFiles`（`Main.java:66-78`）

```java
private static List<Path> findJavaFiles(Path root) throws IOException {
    List<Path> files = new ArrayList<>();
    try (var paths = Files.walk(root)) {
        paths.filter(Files::isRegularFile)
                .filter(path -> path.toString().endsWith(".java"))
                .filter(path -> path.toString().replace('\\', '/').contains("/src/main/java/"))
                .filter(path -> StreamSupport.stream(root.relativize(path).spliterator(), false)
                        .noneMatch(part -> EXCLUDED.contains(part.toString())))
                .forEach(files::add);
    }
    files.sort(Comparator.naturalOrder());
    return files;
}
```

**四个过滤条件，逐条解释。**

#### 条件 1：`Files::isRegularFile`

排除目录与符号链接。看起来是废话，但 `Files.walk` 会返回目录本身，不加这一条会把目录也当文件读。

#### 条件 2：`.endsWith(".java")`

只看 `.java`。注意它**不做大小写归一化**，所以 `.JAVA` 不会被收。当前语料没有这种文件。

#### 条件 3（最关键）：路径里必须含 `/src/main/java/`

先把反斜杠换成斜杠，再判断子串。为什么是它？

**原因一：排除 Maven 构建产物。** `target/generated-sources/...` 下会有 Java 文件（注解处理器生成的、或编译时复制的）。先看真实仓库结构：

```text
D:\Java-learning\12306Project\12306\my12306\
├── services\
│   ├── ticket-services\
│   │   ├── src\
│   │   │   ├── main\java\edu\swu\fcj\my12306\...   ← 要收
│   │   │   └── test\java\edu\swu\fcj\my12306\...   ← 不要
│   │   └── target\                                  ← 不要
│   │       ├── classes\...
│   │       └── generated-sources\...
│   └── order-services\ ... 同理
└── ...
```

完整路径换斜杠后是这样：

```text
D:/Java-learning/12306Project/12306/my12306/services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java
                                                                          ↑ 含 "/src/main/java/" ✅ 收
D:/.../services/ticket-services/target/generated-sources/annotations/...  ↑ 不含                      ❌ 弃
D:/.../services/ticket-services/src/test/java/...                        ↑ 而是 "/src/test/java/"     ❌ 弃
```

**原因二：`src/test/java` 天然被排除。** 这不是巧合，是白名单式过滤的副产品——只要不是 `/src/main/java/` 就不收。测试代码**不应该**进检索索引：它描述的是"怎么验证"，而不是"系统怎么工作"，混进来会污染 CODE 类问题的答案。

**原因三：这一条同时解释了 236 与 218 的差额。** 规划文档记录的真实 `.java` 文件数是 **236**（排除 `target/`）【文档依据】，而 CLI 实际扫描到的是 **218**【实测】。差额 18 个就是 `src/test/java` 下的测试文件——它们存在、但不是主源码，被条件 3 挡掉了。（**推断**：这是两条数据之间自洽的解释；由于 `my12306` 源码目录不在本次可访问范围内，无法逐文件核对这 18 个具体是哪些。）

**一个必须知道的副作用**：这个条件假设了 Maven 标准目录布局。如果将来索引一个 Gradle 项目（源码在 `src/main/java` 也成立，但 Kotlin/Groovy 可能不同）或非标准布局的工程，需要改这一行。

#### 条件 4：相对路径中任何一段不能是排除目录名

```java
private static final Set<String> EXCLUDED = Set.of("target", ".git", ".idea");
```

注意它是按**路径段**比较（`root.relativize(path)` 的每一段），不是子串匹配。所以：

```text
.../target/classes/Foo.java        → 段 "target" 命中 → 排除 ✅
.../src/main/java/.../myTarget.java → 段是 "myTarget.java" → 不命中 → 保留 ✅
```

### 7.2 第二部分：JavaParser 最少 AST 知识

#### 解析入口与两个失败分支（`JavaSourceParser.java:39-47`）

```java
String source = java.nio.file.Files.readString(file, StandardCharsets.UTF_8);
ParseResult<CompilationUnit> result = parser.parse(source);
CompilationUnit unit = result.getResult().orElseThrow(
        () -> new IllegalArgumentException("No compilation unit: " + result.getProblems())
);
if (!result.isSuccessful()) {
    throw new IllegalArgumentException("Parse problems: " + result.getProblems());
}
```

两个分支的区别很重要：

| 分支 | 触发条件 | 含义 |
|---|---|---|
| `result.getResult()` 为空 | 完全没有产出 AST | 文件根本读不成 Java（例如内容是别的语言） |
| `isSuccessful()` 为假 | 产出了 AST，但有 problem | 语法有错，但 JavaParser 用容错模式尽量恢复 |

**第二个分支被当作失败处理**（抛异常 → 该文件计入 `failed`）。这是一个偏严格的选择：JavaParser 默认是容错的，即使有语法错误也能产出部分 AST；这里选择"宁可判定失败，也不要半残的 AST"。理由很实在：**半残的 AST 会产出错误的 `Range`，而错误的行号会静默污染 Citation 与评测**。宁可少几个文件，不要错几个行号。

配置本身只有两行（`JavaSourceParser.java:32-37`）：

```java
ParserConfiguration configuration = new ParserConfiguration()
        .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
        .setCharacterEncoding(StandardCharsets.UTF_8);
```

`JAVA_21` 对应 `my12306` 的 Java 版本。**没有**调用 `setStoreTokens(false)`——这一点是硬要求，见 7.4 节。

#### 三类遍历（`JavaSourceParser.java:56-72`）

```java
for (ClassOrInterfaceDeclaration type : unit.findAll(ClassOrInterfaceDeclaration.class)) {
    records.add(typeRecord(repository, relativePath, module, packageName, type, source));
}
for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
    records.add(callableRecord(repository, relativePath, module, packageName, method,
            "METHOD", method.getNameAsString(),
            method.getDeclarationAsString(true, true, true), source));
}
for (ConstructorDeclaration constructor : unit.findAll(ConstructorDeclaration.class)) {
    records.add(callableRecord(repository, relativePath, module, packageName, constructor,
            "CONSTRUCTOR", constructor.getNameAsString(),
            constructor.getDeclarationAsString(true, true, true), source));
}
```

三个必须注意的点：

1. **三类是并列关系，不是嵌套关系。** 同一个方法会在两处出现：一次作为 `METHOD` Chunk（完整源码），一次作为它所在 `CLASS` 摘要里的一行方法签名。这是**有意**的（地图 vs 地点），但确实意味着 `CLASS` Chunk 与 `METHOD` Chunk 的 `signature` 信息有重叠。
2. **`INTERFACE` 与 `CLASS` 共用同一个遍历。** 区分发生在 `typeRecord` 内部：`type.isInterface() ? "INTERFACE" : "CLASS"`。
3. **`enum` / `record` / `@interface` 不在这个遍历里。** 这是一个真实缺陷，第 11.1 节给出实测证据。

### 7.3 第三部分：跟踪一个真实 Method

`typeRecord` 与 `callableRecord` 的差别在 `content` 的构造方式，其余字段的取法完全一致。先看 METHOD 的（`JavaSourceParser.java:122-157`）：

```java
private ChunkRecord callableRecord(
        String repository, String relativePath, String module, String packageName,
        Node callable, String chunkType, String symbolName, String signature, String source
) {
    Range range = callable.getRange().orElseThrow();
    String content = slice(source, range);                       // ← 原始切片
    String className = callable.findAncestor(TypeDeclaration.class)
            .map(type -> type.getNameAsString())
            .orElse(null);
    return new ChunkRecord(
            repository, "CODE", chunkType, relativePath, module, packageName,
            className, symbolName, signature,
            annotations(callable), javadoc(callable).orElse(null),
            null, List.of(), content,
            range.begin.line, range.end.line, sha256(content)
    );
}
```

把它逐字段对应到真实的 `doPurchaseInTransaction`：

| ChunkRecord 字段 | 取值来源 | 真实值 |
|---|---|---|
| `repository` | 命令行参数 | `my12306` |
| `sourceType` | 硬编码 | `"CODE"` |
| `chunkType` | 调用方传入 | `"METHOD"` |
| `filePath` | `root.relativize(file)` + 反斜杠替换 | `services/ticket-services/.../PurchaseTicketTxService.java` |
| `module` | `detectModule()` | `ticket-services` |
| `packageName` | `unit.getPackageDeclaration()` | `edu.swu.fcj.my12306.biz.ticketservice.service.impl` |
| `className` | `callable.findAncestor(TypeDeclaration.class)` | `PurchaseTicketTxService` |
| `symbolName` | `method.getNameAsString()` | `doPurchaseInTransaction` |
| `signature` | `method.getDeclarationAsString(true,true,true)` | `public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)` |
| `annotations` | `annotations(callable)` | `["@Transactional(rollbackFor = Exception.class)"]` |
| `javadoc` | `javadoc(callable).orElse(null)` | `* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。` |
| `title` / `headingPath` | **硬编码 `null` / `List.of()`** | `null` / `[]` |
| `content` | `slice(source, range)` | 6488 字符的源码原文 |
| `startLine` / `endLine` | `range.begin.line` / `range.end.line` | `69` / `172` |
| `contentHash` | `sha256(content)` | `519d2ebf...8039c` |

两个辅助函数的实现（`JavaSourceParser.java:159-171`）：

```java
private static List<String> annotations(Node node) {
    if (node instanceof NodeWithAnnotations<?> annotated) {
        return annotated.getAnnotations().stream().map(Node::toString).toList();
    }
    return List.of();
}

private static Optional<String> javadoc(Node node) {
    if (node instanceof NodeWithJavadoc<?> documented) {
        return documented.getJavadocComment().map(comment -> comment.getContent().strip());
    }
    return Optional.empty();
}
```

三点值得注意：

1. **注解字符串用 `Node::toString`**。对注解来说这是安全的——`@Transactional(rollbackFor = Exception.class)` 的重新格式化结果与原文一致（注解很短、没有复杂换行）。这里不存在第 7.4 节要讲的"toString 破坏行号自洽"问题，因为注解字符串**不参与行号对应**。
2. **`NodeWithAnnotations` / `NodeWithJavadoc` 是"能力接口"（mixin）**。所以 `annotations()` / `javadoc()` 能同时接受方法、构造器、类三种节点——这就是为什么用一个函数就能处理三类。
3. **`javadoc` 只取内容**，并用 `.strip()` 去掉首尾空白。保留了每行的 ` * ` 前缀（从真实值可以看到开头的 `*`）。**它不在 `content` 里**——这是一个重要事实，见 7.4 节末尾。

### 7.4 第四部分：`Range` → 原始源码（本篇最重点）

#### 三个函数的完整实现（`JavaSourceParser.java:173-194`）

```java
static String slice(String source, Range range) {
    int start = offset(source, range.begin);
    int end = Math.min(source.length(), offset(source, range.end) + 1);
    return source.substring(start, end);
}

private static int offset(String source, Position position) {
    int line = 1;
    int index = 0;
    while (line < position.line && index < source.length()) {
        char current = source.charAt(index++);
        if (current == '\r') {
            if (index < source.length() && source.charAt(index) == '\n') {
                index++;
            }
            line++;
        } else if (current == '\n') {
            line++;
        }
    }
    return Math.min(source.length(), index + position.column - 1);
}
```

#### 关系图

```text
Position(line=69, column=1)
        │
        │  offset(source, position)      把"行列"换算成"字符下标"
        ↓
start = 119（举例，见下文 CRLF 演算）     ← 0-based 字符下标
        │
        │  slice(source, range)          做两次 offset，加闭区间修正
        ↓
source.substring(start, end)            ← Python 的切片语义：含头不含尾
        │
        ↓
content（Java String，UTF-16 码元序列）
```

四个函数的分工，一句话各一个：

| 函数 | 输入 | 输出 | 职责 |
|---|---|---|---|
| `offset` | source + 一个 `Position` | 0-based 字符下标 | 行列 → 下标 |
| `slice` | source + 一个 `Range` | 字符串 | 两次 `offset` + 闭区间修正 + 边界保护 |
| `substring` | 起止下标 | 字符串 | JDK 自带，含头不含尾 |
| `Math.min` | 下标 + 长度 | 安全下标 | 防止越界 |

#### 用真实数据走一遍：`doPurchaseInTransaction`，`start_line=69`，`end_line=172`

对这条真实数据，`slice()` 的执行是：

```text
range.begin = Position(line = 69,  column = 1)     ← 方法第一个注解 @Transactional 所在列
range.end   = Position(line = 172, column = 5)     ← 方法闭合大括号 } 所在列

start = offset(source, begin)            = 从文件开头数到第 69 行第 1 列的字符下标
end   = min(len, offset(source, end) + 1) = 数到第 172 行第 5 列，再 +1

content = source.substring(start, end)   ← 178 行 × 平均 36 字符 ≈ 6488 字符（实测）
```

**这个切片与 `[start_line, end_line]` 天然自洽**，可以用一句话验证：

```text
content 的第一行  = "@Transactional(rollbackFor = Exception.class)"   ← 正是第 69 行的内容
content 的最后一行 = "    }"                                          ← 正是第 172 行的内容
```

这就是为什么第 01 篇里 `sha256(content) == content_hash` 能成立——`content_hash` 算的就是这段原文。

#### 细节 1：CRLF 为什么会导致行号/offset 错误

Windows 上的 Java 文件换行是 `\r\n`（两个字符），Linux/macOS 是 `\n`（一个字符）。如果一个实现用"数 `\n` 的个数"来推算行号，本身没问题；**但常见错误是"把 `\r\n` 当成两个换行"或"用 `source.split("\n")` 后再拼接"**：

```java
// 错误示范：split("\n") 会把 \r 留在行尾
String[] lines = source.split("\n");
// lines[0] = "package demo;\r"        ← 多了个 \r
// 按行 join 回去时，若用 "\n" 连接，\r 就丢失/错位
```

后果是**行号偏移**或者 `content` 与原文差一个 `\r`，进而 `content_hash` 与源文件实际内容不再对应。

**当前代码怎么处理？** `offset()` 手写逐字符扫描，对三种换行都正确：

```java
char current = source.charAt(index++);
if (current == '\r') {
    if (index < source.length() && source.charAt(index) == '\n') {
        index++;        // \r\n 一起吃掉，只算一次换行
    }
    line++;
} else if (current == '\n') {
    line++;             // 单独的 \n 也算一次换行
}
```

| 换行形式 | 处理 |
|---|---|
| `\r\n`（Windows） | 吃掉两个字符，`line++` 一次 ✅ |
| `\n`（Unix） | `line++` 一次 ✅ |
| `\r`（旧 Mac） | 走第一个分支，`index` 后面不是 `\n` 就不多吃，`line++` 一次 ✅ |

**一个必须知道的语义**：因为 `\r\n` 是两个字符，**同一行在两个平台上的字符下标是不同的**。所以 `offset()` 的结果是与具体文件绑定的，不能跨平台复用——但它每次都从同一个 `source` 现算，所以不存在不一致问题。

**这个行为有真实测试**：`JavaSourceParserTest` 构造了一个 CRLF 文件并断言 `content` 里包含 `"return \"ticket:\" + id;\r\n"` —— **注意断言里带 `\r\n`**，也就是要求切片保留原始 CRLF，而不是被规范化成 `\n`。

#### 细节 2：为什么是 `offset(end) + 1`

因为 **`Range` 是闭区间**：`end` 指向的列**属于**这个节点。

```text
方法体最后一行（第 172 行）：
    }        ← column 5 是 '}'
             ↑
             这一列是方法的一部分

若 end = offset(172, 5)            → substring 会在 '}' 之前停下 ❌ 少一个字符
    end = offset(172, 5) + 1       → 正好包含 '}' ✅
```

对比另一端的 `start`：它不需要 `+1`，因为 `substring` 是含头不含尾，`begin` 那一列本来就该被包含。

**两者配合的净效果**是：`[start, end]` 这个闭区间被精确地映射成 `substring(start, end_index + 1)`。这也是为什么 `Position` 的 1-based 约定必须记住——`offset()` 末尾的 `+ position.column - 1` 正是在做 1-based 列 → 0-based 下标的换算。

#### 细节 3：为什么不用 `node.toString()`

`Node.toString()` 走 JavaParser 的 `PrettyPrintVisitor`，会**重新格式化**：

```text
缩进可能被重排
空行可能被增删
行内注释位置可能被移动
```

语义不变，但**文本变了**。于是：

```text
content ≠ 源文件[start_line, end_line] 的真实文本
        ↓
用户按 Citation 里的行号去源文件里找，看到的东西与 content 不完全一样
        ↓
“检索结果是否命中 Ground Truth”无法用行号核验
        ↓
评测失去可验证性
```

所以本项目的规则可以总结成一句：

> **凡是要与行号自洽的地方，就用原始切片；凡是不需要对应行号的地方，才可以用 `toString()`。**

这条规则在两处各体现一次，形成对照：

| 位置 | 用什么 | 为什么 |
|---|---|---|
| METHOD / CONSTRUCTOR 的 `content` | **原始切片**（`slice`） | 要与 `start_line`/`end_line` 逐字对应，要支持 Citation 核验 |
| CLASS / INTERFACE 的 `content`（摘要）里的字段与声明 | **`Node.toString()`** | 摘要本来就不是原文，也没有行号要对应（见 7.5 节，这里还留下了一个可见的缩进痕迹） |
| `annotations` 列表 | `Node.toString()` | 注解很短，重新格式化后与原文一致，且不与行号对应 |
| `signature` | `getDeclarationAsString()` | 生成单行版本，专门给检索用，不与行号对应 |

（还有一个已排除的替代方案：`LexicalPreservingPrinter`。它能保留原始词法，但它是为"改 AST 后只重排版那一处、其余原样保留"这种**写场景**设计的。DevContext 是只读检索系统，引入它属于过度设计。）

#### 一个必须知道的事实：Javadoc 不在 `content` 里

用真实数据核对：

```text
content 第一行 = "@Transactional(rollbackFor = Exception.class)"
start_line     = 69
javadoc 字段   = "* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。"
```

也就是说 Javadoc 在**第 69 行之前**（大约 66–68 行），**不在 Range 内**，因此不在 `content` 里。它只存在于 `javadoc` 字段。

**这条事实有一个重要的推论**：第 01 篇说过 `javadoc` 会进 `embedding_text`。所以**中文 Javadoc 对向量检索的贡献，完全依赖 `embedding_text()` 显式地把 `javadoc` 字段拼进去**；如果哪天有人"简化"了 `embedding_text()` 只留 `content`，中文语义召回会明显下降。这是一个容易在重构时被误删的关键逻辑。

JUnit 测试也验证了这个边界（`startLine() == 5`，而 Javadoc 在第 4 行）：

```java
"package demo;\r\n"                                    // 1
+ "public class TicketService implements Handler {\r\n" // 2
+ "  public TicketService() {}\r\n"                    // 3
+ "  /** Buy one ticket. */\r\n"                       // 4  ← Javadoc 在这
+ "  @Override\r\n"                                    // 5  ← 方法 Range 从这里开始
+ "  public String purchaseTicket(String id) {\r\n"     // 6
+ "    return \"ticket:\" + id;\r\n"                    // 7
+ "  }\r\n"                                              // 8  ← 到这里结束
+ "}\r\n"                                                // 9
+ "interface Handler { String purchaseTicket(String id); }\r\n"  // 10

assertEquals(5, method.startLine());
assertEquals(8, method.endLine());
```

这段 fixture 同时证明了两件事：**① 注解在 Range 内（第 5 行是 `@Override`，startLine 就是 5）；② Javadoc 在 Range 外。**

### 7.5 第五部分：CLASS Summary

#### 真正的实现（`JavaSourceParser.java:76-120`）

```java
Range range = type.getRange().orElseThrow();
String declaration = typeDeclaration(type);
StringBuilder summary = new StringBuilder();
javadoc(type).ifPresent(value -> summary.append(value).append("\n"));
annotations(type).forEach(value -> summary.append(value).append("\n"));
summary.append(declaration).append(" {\n");
for (FieldDeclaration field : type.getFields()) {
    summary.append("  ").append(field).append("\n");
}
for (ConstructorDeclaration constructor : type.getConstructors()) {
    summary.append("  ").append(constructor.getDeclarationAsString(true, true, true)).append(";\n");
}
for (MethodDeclaration method : type.getMethods()) {
    summary.append("  ").append(method.getDeclarationAsString(true, true, true)).append(";\n");
}
summary.append('}');
String content = summary.toString();
```

**七段内容，顺序固定**：

```text
① Javadoc（如果存在）
② 注解（一行一个）
③ 类声明（含泛型/extends/implements/permits）+ " {"
④ 字段声明（每个前面加 "  "）
⑤ 构造方法签名（加 "  " 和 ";"）
⑥ 方法签名（加 "  " 和 ";"）
⑦ 一个 "}"
```

#### 真实输出：`TicketAvailabilityTokenBucket`

```text
* Redis 余票令牌桶只负责购票准入，不是库存事实源。              ← ① Javadoc
 * 异常时始终降级放行，最终是否能占座仍由 MySQL 条件更新决定。
@Slf4j                                                        ← ② 注解
@Component
@RequiredArgsConstructor
public class TicketAvailabilityTokenBucket {                  ← ③ 类声明（由 typeDeclaration 拼装）
  private static final long TAKE_SUCCESS = 1L;                 ← ④ 字段
  private static final long TAKE_REJECTED = 0L;
  private static final long BUCKET_MISSING = -1L;
  private static final DefaultRedisScript<Long> TAKE_TOKEN_SCRIPT = loadScript("lua/take_token_from_bucket.lua");
  private static final DefaultRedisScript<Long> RETURN_TOKEN_SCRIPT = loadScript("lua/return_token_to_bucket.lua");
  private final StringRedisTemplate stringRedisTemplate;
  private final SeatMapper seatMapper;
  private final TrainMapper trainMapper;
  private final RedisCacheHelper redisCacheHelper;
  private final MeterRegistry meterRegistry;
  @Value("${my12306.availability.token-bucket.enabled:true}")     ← ← 注意这里
private boolean enabled;                                          ← ← 缩进只到首行
  ...
  public void initializeBuckets();                             ← ⑥ 方法签名（无方法体）
  public TokenTakeResult takeToken(TokenTakeRequest request);
  public void returnToken(...);
  ...
}
```

**实测规模**：这个 CLASS Chunk 的 `content` 是 **1715 字符**，`start_line=37`、`end_line=214`。

**注意上面那处缩进**：`@Value(...)` 有两空格缩进，而紧随其后的 `private boolean enabled;` 没有。原因是 `field.toString()` 对带注解的字段返回**多行**字符串，而代码只在拼接处加了一次 `"  "`：

```java
summary.append("  ").append(field).append("\n");
//                ↑ 只作用于整个字符串的开头，不影响第二行
```

这是一个**纯外观问题**（不影响检索），但它是"用 `toString()` 渲染"留下的真实痕迹，也是 7.4 节那条规则的具体证明。真实产物中可以看到这个形态【实测】。

#### 为什么不保存整个类源码

三个理由，每一条都能独立成立：

**理由一：语义被平均掉。** 一个 200 行的类作为单个 Chunk，它的 embedding 是整个类的语义平均。用户问"这个类里怎么取令牌"，向量会给出一个"有点像但都不准"的结果——因为这个向量同时混着字段初始化、Redis 脚本加载、指标埋点、降级逻辑。

**理由二：与 METHOD Chunk 内容重叠。** 类源码包含类内每个方法的完整实现，而每个方法又各自是一个 METHOD Chunk。同一个方法体因此会有两个 Chunk 覆盖它，检索时同一段代码反复出现，挤占 Top-K。

**理由三：挤占 Context 预算。** `TicketAvailabilityTokenBucket` 这个类的 178 行原文远大于它的摘要（1715 字符）。举个可核对的数字：这个类里的单个方法 `loadBucket` 的 `content` 就有 **2573 字符**，比整个类的摘要还长。如果一个类的原文进 Context，几条结果就会占满预算。

**CLASS 与 METHOD 的分工**因此是明确的一句话：

```text
CLASS   = 地图   “这个类是什么角色（@Service / @FeignClient）、提供哪些能力”
METHOD  = 地点   “某个行为到底怎么实现的”
```

`type.getMethods()` 只返回**直接声明**在类里的方法（不含继承来的），这与"地图"的定位一致。

#### 一个必须知道的取舍：CLASS 的 `content` 与行号不自洽

`start_line` / `end_line` 来自 `type.getRange()`，覆盖**整个类体**；而 `content` 是摘要，**方法体被全部丢弃**。所以对 CLASS / INTERFACE：

```text
content ≠ source[start_line .. end_line]
```

用可核对的数据说明：`TicketAvailabilityTokenBucket` 的 `content` 是 1715 字符，而 37–214 行覆盖 178 行类体，其中仅 `loadBucket` 一个方法就占 2573 字符。**只要类里有任何一个带方法体的方法，摘要就必然短于原文。**

这不是 bug，是"CLASS 是地图"这个设计决定的取舍。但它有一个直接后果，必须写进认知：

> **Citation 不能对 CLASS Chunk 也用"行号范围"呈现。** 用户照着 37–214 行去看，看到的是一整个类，而不是 `content` 里那段摘要。对 CLASS/INTERFACE，Citation 更合适的形态是"文件路径 + 类型名"。

（对 METHOD / CONSTRUCTOR 则相反：`content` 与行号严格自洽，行号式 Citation 完全成立。）

### 7.6 补充：`detectModule` 与 `typeDeclaration`

**`detectModule`（`JavaSourceParser.java:196-203`）**：

```java
for (int index = 0; index < relativePath.getNameCount(); index++) {
    if ("src".equals(relativePath.getName(index).toString()) && index > 0) {
        return relativePath.getName(index - 1).toString();
    }
}
return relativePath.getNameCount() > 1 ? relativePath.getName(0).toString() : null;
```

逻辑是"找 `src`，取它前一段"。真实验证：

```text
services/ticket-services/src/main/java/...   → src 在索引 2 → module = "ticket-services"  ✅（与产物一致）
services/gateway-services/src/main/java/...  → module = "gateway-services"              ✅（与产物一致）
```

兜底分支（找不到 `src` 时取第一段）在当前语料上从未触发。**注意返回值只进 `module` 字段，不参与任何检索路径**（第 01 篇误解 4）。

**`typeDeclaration`（`JavaSourceParser.java:205-229`）**：手工拼装声明字符串，因为这个信息 `getDeclarationAsString` 在类上不可用（类没有"参数"）。它依次拼：

```text
修饰符（public / abstract / final ...）
+ "class " 或 "interface "
+ 类型名
+ 类型参数 <T, R>
+ " extends ..."（extendedTypes）
+ " implements ..."（implementedTypes）
+ " permits ..."（permittedTypes，Java 17+ 密封类）
```

真实输出：`public interface OrderItemMapper extends BaseMapper<OrderItemDO>`、`public class TicketAvailabilityTokenBucket`。

### 7.7 第六部分：错误隔离

#### 单文件失败隔离（`Main.java:41-54`）

```java
for (Path file : files) {
    try {
        List<ChunkRecord> records = parser.parse(root, file, arguments.repository());
        for (ChunkRecord record : records) {
            writer.write(mapper.writeValueAsString(record));
            writer.newLine();
            chunks++;
        }
        parsed++;
    } catch (Exception exception) {
        failed++;
        System.err.printf("PARSE_ERROR file=%s message=%s%n", root.relativize(file), exception.getMessage());
    }
}
```

为什么必须是"单文件失败 ≠ 整个 Repository 失败"：

| 如果没有隔离 | 后果 |
|---|---|
| 第一个坏文件直接抛异常 | 整个 `ingest` 失败，**你永远拿不到索引** |
| 218 个文件里只要 1 个解析不了 | 全量重建永远无法完成 |

在真实仓库里"总有几个文件解析不了"是常态而非例外（语法版本不匹配、生成代码、编码问题）。所以在 218 个文件的规模上，没有隔离就等于没有可用系统。

注意隔离的**粒度是"文件"**：`parser.parse()` 内部抛出的任何异常都被这一个文件的 catch 接住。而 `writer.write()` 也在 try 内——这意味着**如果某个文件写到一半失败，可能有一行残缺写进 JSONL**。当前实现没有对此做特殊处理，Python 侧读回来时会因为 `json.loads` 失败而报 `Invalid Java parser output at line N`（见第 11 节）。

#### `parsed == 0` 兜底（`Main.java:61-63`）

```java
if (!files.isEmpty() && parsed == 0) {
    throw new IllegalStateException("All Java files failed to parse");
}
```

为什么这条必须存在？因为**没有它，最糟糕的情况是静默的**：

```text
假设环境有问题（Java 版本不对 / jar 没打出来 / 路径写错）
  → 218 个文件全部解析失败
  → 每个文件都打一行 PARSE_ERROR 到 stderr
  → parsed=0, failed=218, chunks=0
  → 进程正常退出（exit code 0）
  → JSONL 被创建，但是空的
        ↓
Python 侧读回 0 个代码 Chunk
        ↓
pipeline.py 里：
   if not code_chunks or not document_chunks:
       raise RuntimeError("Both source types are required: code=0, docs=2363")
```

也就是说，**这条红线是"第一道"防线，Python 侧的条件检查是"第二道"**。两道都在，是为了避免"进程成功返回、索引却是空的"这种最难排查的状态。

注意条件是 `!files.isEmpty() && parsed == 0`：**扫描到 0 个文件不触发这条**（那种情况由 Python 侧拦），只有当"确实有文件、但一个都没成功"时才判定为环境级故障。

---

## 8. 用一个真实数据走完整流程

把 `PurchaseTicketTxService.java` 从磁盘到 JSONL 的全过程串一遍。

```text
① Scan
   Files.walk(D:\Java-learning\12306Project\12306\my12306)
     → 过滤 isRegularFile
     → 过滤 endsWith(".java")
     → 过滤 路径含 "/src/main/java/"
     → 过滤 路径段不含 target/.git/.idea
     → 排序
   得到这个文件：
   D:\...\my12306\services\ticket-services\src\main\java\edu\swu\fcj\my12306\biz\ticketservice\service\impl\PurchaseTicketTxService.java

② Read
   Files.readString(file, UTF_8)  →  source（一个 String，含 \r\n 或 \n）

③ Parse
   parser.parse(source)
     → ParseResult<CompilationUnit>
     → isSuccessful() == true
     → unit = CompilationUnit

④ Package + Module
   unit.getPackageDeclaration().getNameAsString()
     → "edu.swu.fcj.my12306.biz.ticketservice.service.impl"
   detectModule(root.relativize(file))
     → 找 "src"，取前一段 → "ticket-services"
   relativePath = root.relativize(file).toString().replace('\\','/')
     → "services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java"

⑤ findAll(ClassOrInterfaceDeclaration.class)  → 1 个
     typeRecord(...)
       → chunkType = "CLASS"（不是接口）
       → symbolName = className = "PurchaseTicketTxService"
       → signature  = typeDeclaration(type) = "public class PurchaseTicketTxService"
       → content    = 摘要（Javadoc + 注解 + 声明 + 字段 + 构造器签名 + 方法签名）
       → startLine / endLine = type.getRange() 的起止行

⑥ findAll(MethodDeclaration.class)  → N 个，其中一个是目标
     callableRecord(..., "METHOD", method.getNameAsString(),
                    method.getDeclarationAsString(true,true,true), source)
       → symbolName = "doPurchaseInTransaction"
       → signature  = "public PurchaseReservationResult doPurchaseInTransaction(...)"
       → annotations = ["@Transactional(rollbackFor = Exception.class)"]
       → javadoc    = "* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。"
       → className  = findAncestor(TypeDeclaration) = "PurchaseTicketTxService"
       → range      = Range(begin=(69,1), end=(172,5))
       → content    = slice(source, range)   ← 6488 字符原文
       → startLine  = 69, endLine = 172
       → contentHash= sha256(content) = "519d2ebf...8039c"

⑦ findAll(ConstructorDeclaration.class)  → 该文件的构造器（若有）

⑧ 序列化
   mapper.setPropertyNamingStrategy(SNAKE_CASE)
   mapper.writeValueAsString(record)  → 一行 JSON
   writer.write(...) + newLine()

⑨ 全部文件跑完
   stdout: JAVA_PARSE_SUMMARY scanned=218 parsed=218 failed=0 chunks=538 output=...
```

**Python 侧接手的部分**（`java_parser_runner.py:50-60`）：

```python
chunks: list[Chunk] = []
with output.open(encoding="utf-8") as handle:
    for line_number, line in enumerate(handle, start=1):
        if line.strip():
            try:
                chunks.append(Chunk.from_dict(json.loads(line)))
            except Exception as exception:
                raise ValueError(f"Invalid Java parser output at line {line_number}") from exception
return chunks
```

到这里，一个 Java 方法就变成了一个 Python `Chunk` 对象，可以进入第 01 篇讲的双文本派生流程。

---

## 9. 关键设计为什么这样做

### 9.1 为什么 Java 解析器是一个独立进程

不是"顺手"，有三个独立成立的理由：

**理由一：语言与栈的匹配。** JavaParser 是 Java 库。要让 Python 用它，只能靠 `jpype` 之类的桥接、或者自己重写解析器。独立 JVM 进程是最干净的方式。

**理由二：零依赖打包。** `javaparser-core` 自身**没有任何运行时依赖**（pom 里 `<dependencies>` 为空），配合 `maven-shade-plugin` 打成一个 fat jar，只带 Jackson 一个额外依赖。部署成本接近零。

**理由三：可独立测试。** Java 侧有自己的 JUnit 测试（`JavaSourceParserTest`），可以脱离 Python 环境、脱离数据库、脱离 Docker 单独运行。这一点在本项目里非常实际：**CRLF、Range、注解归属这些最容易出错的逻辑，全部在 Java 侧，而它们都能被一个不依赖任何外部服务的测试覆盖。**

代价也很明确：**多了一个进程边界，数据要通过文件交换**。所以 `java_parser_runner.py` 必须处理"mvn 构建失败""jar 不存在""java 不在 PATH""JSONL 某行坏了"这四类问题（对应 `build()` 与 `parse()` 里的显式检查）。

### 9.2 为什么用 JSONL 而不是 JSON 数组或数据库

```text
JSONL = 一行一条记录
```

三个好处：

1. **流式写入**。Java 侧边解析边写（`try (BufferedWriter writer = ...)` 包住整个循环），不需要在内存里攒完 538 条再统一序列化。
2. **流式读取 + 可定位的错误**。Python 侧逐行读、逐行 `json.loads`，失败时能精确报出 `line N`。
3. **可人工检查**。`artifacts/java-chunks.jsonl` 可以直接用编辑器打开看某一行，这对调试很有价值（第 12 节的实验全部依赖这一点）。

### 9.3 为什么 `content` 用切片而不是 `toString`

见 7.4 节细节 3。一句话总结：**`toString()` 会重新格式化，破坏 `content` 与 `[start_line, end_line]` 的自洽性，而自洽性是 Citation 与评测可验证性的前提。**

### 9.4 为什么构造器要独立成一类

不是"顺便多一个类型"，而是字段语义决定的：

| | METHOD | CONSTRUCTOR |
|---|---|---|
| 返回类型 | 有（`getType()` 可用） | **没有** |
| 名字 | 方法名 | **恰好等于类名** |

第二行是关键：如果构造器混进 METHOD，`symbol_name` 里会出现大量与 `class_name` 相同的值，直接污染"类名匹配"这条评分臂（第 06 篇）。而且 `cases.jsonl` 里 CODE 题的 Ground Truth 是 `symbol` —— 构造器与方法同名时判分会变得含糊。独立成类，问题消失。

真实语料里有 19 个 CONSTRUCTOR Chunk。

### 9.5 为什么 CLASS 用摘要、METHOD 用全文

这是整个数据模型里最核心的一次分工，第 7.5 节已从三个角度解释（语义平均、内容重叠、预算占用）。这里补一个正面视角：

**摘要的字段列表本身就是一种"类的能力清单"。** 当用户问"`TicketAvailabilityTokenBucket` 提供哪些能力"时，答案就在那串方法签名里——`initializeBuckets` / `takeToken` / `returnToken` …… 这不需要任何方法体。反过来，问 `loadBucket` 怎么实现，摘要完全没用，需要 `loadBucket` 自己的 METHOD Chunk。

所以两者不是冗余，而是**覆盖了两类不同的问题**：类级问题与方法级问题。

### 9.6 为什么 Javadoc 要与 content 分离存储

因为它**既有检索价值，又有位置特性**：

- 它有检索价值：中文 Javadoc 是自然语言，与中文提问的语义距离最近，是向量检索的重要入口。
- 它的位置在 Range 之外：所以它不可能出现在 `content` 里。

这两点合在一起，决定了它必须是一个独立字段，由 `embedding_text()` 显式拼接（而不是靠 `content` 顺带包含）。这也解释了为什么 `javadoc` 出现在 `Chunk` 字段里，而"类源码里的普通注释"没有——普通注释既不在 Range 内、又没有独立字段，等于**完全丢失**（见第 11 节）。

---

## 10. 常见误解

### 误解 1：`METHOD` Chunk 的 `content` 包含 Javadoc

**不包含。** Javadoc 在方法 Range 之外（Range 从注解/修饰符开始）。真实数据：

```text
start_line = 69
content 第一行 = "@Transactional(rollbackFor = Exception.class)"
javadoc 字段   = "* 在锁内原子完成选座..."（来自第 69 行之前）
```

`content` 里既没有 Javadoc，也没有 `/** */` 符号。Javadoc 只存在于 `javadoc` 字段里。

### 误解 2：`CLASS` Chunk 的 `content` 是类源码

**不是，是摘要**（`typeRecord` 手工拼装的 StringBuilder）。里面**没有方法体**，方法只以一行签名出现。所以：

```text
想在 CLASS Chunk 里找某个方法的具体实现 → 找不到
```

### 误解 3：`start_line` / `end_line` 对所有 Chunk 都表示"content 在源文件里的位置"

**只对 METHOD / CONSTRUCTOR 成立。**

| chunk_type | `content` 是什么 | 与行号自洽？ |
|---|---|---|
| METHOD / CONSTRUCTOR | 原始源码切片 | ✅ 严格自洽 |
| CLASS / INTERFACE | 摘要 | ❌ 不自洽（行号是声明范围，content 是摘要） |
| DOCUMENT_SECTION | 章节正文 | ✅（但行号来自 Markdown 解析器，不含标题行） |

### 误解 4：删掉 `SNAKE_CASE` 那行不会有影响

**会静默损坏字段。** 删掉之后 Java 输出 `sourceType`，Python 的 `Chunk.from_dict` 按 `source_type` 找不到键，**不报错**，该字段落回默认值。而 `source_type` 是 `Chunk` 的必填字段（无默认值）——它确实会抛 `TypeError`。但换成别的字段就未必了：

```text
startLine      → 找不到 start_line  → 落回默认值 None  → 行号全部丢失、不报错
```

这是跨语言契约里最典型的"静默失败"：**改变序列化命名策略不会让程序崩溃，只会让一批字段变空。**

### 误解 5：`mvn -q package` 每次都是浪费

`JavaParserRunner.parse()` 的第一行就是 `self.build()`，也就是每次 ingestion 都会跑一次 `mvn -q package`。

看起来多余，实际是有意的：**Python 侧不假设 jar 已经存在或是最新的**。如果只做 `java -jar`，一旦 jar 不存在，报错是 `Unable to access jarfile`，与"jar 存在但过期"混在一起难以区分。先构建再运行，把"构建失败"和"运行失败"分成了两类可区分的错误（`FileNotFoundError: Maven executable was not found` / `FileNotFoundError: Parser JAR was not created` / 进程非零退出）。

代价是每次 ingest 多几秒 Maven 启动时间——以 -q 静默模式运行，且 Maven 增量编译时很快。

### 误解 6：`findAll` 只找顶层类型

`findAll` 是**递归全树**搜索。所以嵌套类、匿名内部类里的方法都会被找到。

对匿名内部类里的方法，`findAncestor(TypeDeclaration.class)` 会找到**最近的命名类型**（也就是外层类），因此 `class_name` 会是外层类的名字——方法在语法上确实属于外层类的作用域，但读者需要知道这个 Chunk 实际来自方法体内的匿名类。

（真实语料实测：203 个类型级 Chunk 分布在 203 个文件上，**没有任何文件含 2 个以上类型声明**，所以当前语料里没有嵌套类引起的问题。）

### 误解 7：`content_hash` 是"源文件"的 hash

**是 `content` 的 hash，不是源文件的 hash。** 而且因为 CLASS 的 `content` 是摘要，它的 `content_hash` 与源文件任何一段文本的 hash 都不相等。对 METHOD，`sha256(content)` 等于源文件对应行的 hash（这点可以核对，见第 01 篇）。

---

## 11. 当前实现的限制/缺陷

### 11.1 `enum` / `record` / `@interface` 不产生类型级 Chunk（已定位到具体文件）

`parse()` 只遍历 `ClassOrInterfaceDeclaration`。因此：

```text
enum Foo { ... }        → 不产生 CLASS Chunk
record Bar(...) {...}   → 不产生 CLASS Chunk
@interface Baz { ... }  → 不产生 CLASS Chunk
```

但它们**内部的方法会被 `findAll(MethodDeclaration.class)` 抓到**（枚举方法、record 的显式方法都是 `MethodDeclaration`）。于是出现一个不对称的结果：`class_name` 有值，但库里没有对应的类型 Chunk。

**这不是推测，已经在产物中核实。** 我对 `artifacts/java-chunks.jsonl` 做了"按文件统计 chunk_type"，结果：

```text
206 个文件产出 Chunk
其中 203 个产出类型级 Chunk（CLASS / INTERFACE）
     3 个没有任何类型级 Chunk
203 个类型级 Chunk 恰好分布在 203 个不同文件上
```

那 3 个文件**全部是枚举**：

| 文件 | 收录到的 Chunk | 缺失的 |
|---|---|---|
| `payservice/common/enums/PayChannelEnum.java` | METHOD `findByName`（`class_name = PayChannelEnum`） | `PayChannelEnum` 枚举声明 |
| `ticketservice/common/enums/RegionStationQueryTypeEnum.java` | METHOD `findSpellsByType` | `RegionStationQueryTypeEnum` 枚举声明 |
| `ticketservice/common/enums/VehicleTypeEnum.java` | METHOD `findSeatTypesByCode` | `VehicleTypeEnum` 枚举声明 |

**影响可以精确描述**：枚举定义了哪些常量、承担什么分类职责，这一层信息**完全没有进索引**。因此"`PayChannelEnum` 支持哪些支付渠道"这类问题当前无法回答，只能命中 `findByName` 这个工具方法。

修复方向（不要现在改，只是记下来）：把遍历扩展为同时处理 `EnumDeclaration` / `RecordDeclaration` / `AnnotationDeclaration`，并在 `sql/001_schema.sql` 的 `chunk_type` CHECK 约束里加对应取值——注意 DDL 与 `ChunkRecord` 要一起动，因为 CHECK 是硬约束，写入未声明的取值会直接违反约束、导致整个事务失败。

### 11.2 普通注释完全丢失

`content` 是源码切片，所以**落在方法体内部的**行注释与块注释会保留（它们在同一 Range 内）。但：

- 方法**上方**的 Javadoc → 由独立字段保全（`javadoc`）
- 方法上方或类上方的**普通注释**（`// ...` 或 `/* ... */`，非 Javadoc）→ **既不在 Range 内，也没有独立字段，直接丢失**

这一点在设计讨论中"为什么用 javadoc 字段"时容易被忽略：它保的只是 Javadoc，不是所有注释。

### 11.3 CLASS / INTERFACE 的 `content` 与行号不自洽

见 7.5 节。后果是 **Citation 对 CLASS 不能用行号式呈现**。严重度中低——检索本身不受影响，影响的是下游 Context Builder / Citation 的设计。

### 11.4 摘要里的字段渲染有缩进瑕疵

见 7.5 节（`@Value(...)` 有两空格、紧邻的字段声明没有）。纯外观问题，不影响检索，但在"把 CLASS 摘要喂给 LLM"时会出现不一致的缩进。

### 11.5 类摘要与构造器 / 方法 Chunk 存在信息重叠

CLASS 摘要里包含构造器签名与方法签名列表，而这些信息同时又是独立的 CONSTRUCTOR / METHOD Chunk 的一部分。这是"地图 vs 地点"设计的必然代价，但它意味着：

```text
问“这个类有哪些方法” → CLASS Chunk 命中（好）
但同一个查询也可能命中若干 METHOD Chunk，因为签名行重复
```

### 11.6 `content` 首行会带原始缩进

`slice()` 从 `range.begin.column` 开始切，因此**首行保留了源码的缩进**。真实对照：

```text
doPurchaseInTransaction（注解在第 1 列）→ content 首行 = "@Transactional(rollbackFor = Exception.class)"
JUnit fixture 的方法（注解在第 3 列） → content 首行 = "  @Override"
```

不影响检索（`content` 在 `keyword_text` 里只参与 trigram 相似度），但在"按行对齐展示 content"时要注意首行可能有两个空格。

### 11.7 `offset()` 对每个切片都从头扫描文件

`offset()` 是 O(n) 扫描（n = 文件字符数），而每个 METHOD / CONSTRUCTOR 都要调用它两次（`begin` 与 `end`），每次**都从文件开头重新扫描**。

对一个 386 行的文件、20 个方法，就是 40 次全文件扫描。当前规模下完全可接受（Java 侧实测能秒级跑完 218 个文件），但如果将来索引更大的仓库，这是一个明确的优化点（预先算好行偏移表）。

### 11.8 单个文件写 JSONL 失败可能留下残行

`writer.write()` 在 try 块内，如果写到一半抛异常（磁盘满、编码问题），JSONL 里可能留下一个不完整的 JSON 行。当前实现没有对此做处理。Python 侧会在读取时以 `Invalid Java parser output at line N` 报错——**不会静默通过**，但错误的定位会指向"输出格式坏了"，而不是"磁盘满了"。

### 11.9 没有 SymbolSolver、没有调用图（现状，不是遗漏）

只引入了 `javaparser-core`。因此**当前无法回答**"谁调用了 `scanTimeoutOrder`"这类跨文件、跨方法的问题。AST 回答的是"代码长什么样"，"这个名字指向哪个声明"是 `SymbolSolver` 的职责，而它需要 TypeSolver 配置（JDK 反射、项目源码目录、依赖 jar……），对一个 5 模块的 Maven 工程成本很高。**当前没有实现，也不在本阶段范围内。**

### 11.10 超长方法尚未触发截断（实测）

`content` 长度实测：

```text
平均 436 字符
最大 6488 字符（就是 doPurchaseInTransaction）
> 6000 字符的 Chunk：1 个
> 8192 字符的 Chunk：0 个（共 538 个）
```

所以"超长方法 embedding 截断"这个已记录的风险**当前没有触发**。但 6488 已经接近上限，需要持续观察。

---

## 12. 建议亲自执行的实验

### 实验 A：手工写一个 20 行的 Java 文件，用当前 Parser 输出 JSONL

新建 `tmp-parser-lab/src/main/java/demo/TicketService.java`（注意必须放在 `src/main/java/` 下，否则条件 3 会把它过滤掉——这本身就是对 7.1 节的验证）：

```java
package demo;

import java.util.Optional;

public class TicketService implements Handler {

    /** Buy one ticket. */
    @Override
    public Optional<String> purchaseTicket(String id) {
        if (id == null) {
            throw new IllegalArgumentException("id is required");
        }
        return Optional.of("ticket:" + id);
    }

    public static class Inner {
        void nested() {
        }
    }
}

interface Handler {
    Optional<String> purchaseTicket(String id);
}
```

运行（在项目根目录）：

```powershell
mvn -q -f java-parser\pom.xml package
java -jar java-parser\target\devcontext-java-parser.jar `
     --code-root .\tmp-parser-lab `
     --output .\tmp-chunks.jsonl `
     --repository lab
```

预期输出：`scanned=1 parsed=1 failed=0 chunks=?`。

**人工核对清单**（这才是实验的重点）：

1. `chunks` 是几？逐个列出它们分别是什么 `chunk_type`、什么 `symbol_name`。（提示：本文件有 1 个接口 + 1 个类 + 1 个嵌套类）
2. `purchaseTicket` 有几条 METHOD？它们的 `class_name` 分别是什么？（提示：接口方法 + 类方法）
3. 嵌套类 `Inner` 有没有产生 CLASS Chunk？`nested()` 的 `class_name` 是什么？
4. `purchaseTicket`（类里那个）的 `start_line` 是几？——**注意它是 `@Override` 那一行，不是 `public Optional...` 那一行。** 这是"注解在 Range 内"的直接验证。
5. `javadoc` 字段有值吗？`content` 里有 `/** Buy one ticket. */` 吗？
6. 接口 `Handler` 的 Chunk 里，`signature` 是什么？`content` 里有方法体吗？

最后把 `tmp-parser-lab` 与 `tmp-chunks.jsonl` 删掉——**不要留在仓库里**。

### 实验 B：拿真实 `doPurchaseInTransaction` 逐字段与 IDE 对照

```powershell
# 过滤出目标记录（Windows PowerShell）
Select-String -Path artifacts\java-chunks.jsonl -Pattern '"symbol_name":"doPurchaseInTransaction"' |
    ForEach-Object { $_.Line } | ConvertFrom-Json | Format-List
```

然后**打开源文件**（`my12306` 里的 `PurchaseTicketTxService.java`），逐项核对：

| 核对项 | 怎么核对 |
|---|---|
| `start_line = 69` | 跳到第 69 行，应该正好是 `@Transactional(rollbackFor = Exception.class)` |
| `end_line = 172` | 跳到第 172 行，应该正好是方法的 `}` |
| `content` 首行 | 与第 69 行的文本逐字对比（含缩进） |
| `content` 末行 | 与第 172 行的文本逐字对比 |
| `javadoc` | 往上找 `/** ... */`，去掉 `/**` `*/` 与每行的 ` * ` 前缀后，应该与字段值一致 |
| `annotations` | 应该只有 `@Transactional(rollbackFor = Exception.class)` 一条 |
| `signature` | 是**单行**的；而 IDE 里看到的是多行——这正是 `getDeclarationAsString` 与原文的差别 |
| `content_hash` | 见下面的命令 |
| `module` | 路径里 `src` 前一段是 `ticket-services` |
| `class_name` | 方法所在类，不是返回值类型也不是参数类型 |

校对 `content_hash`：

```powershell
python -c "import json,hashlib;s=open(r'artifacts/java-chunks.jsonl',encoding='utf-8').read().splitlines();r=[json.loads(x) for x in s if x.strip()];c=[x for x in r if x['symbol_name']=='doPurchaseInTransaction'][0];print(c['content_hash']);print(hashlib.sha256(c['content'].encode()).hexdigest())"
```

两行输出必须完全相同。

### 实验 C：构造 CRLF 文件，验证 Range → 切片 的算术

这是本篇最值得亲手做的一次实验，因为**可以完全脱离 my12306，用手算验证**。

新建 `tmp-crlf/src/main/java/demo/CRLF.java`，**必须用 CRLF 保存**（VS Code 右下角切到 `CRLF`，或用下面的 Python 一次性生成）：

```python
# 生成 CRLF 文件
src = ("package demo;\r\n"
       "public class TicketService implements Handler {\r\n"
       "  public TicketService() {}\r\n"
       "  /** Buy one ticket. */\r\n"
       "  @Override\r\n"
       "  public String purchaseTicket(String id) {\r\n"
       "    return \"ticket:\" + id;\r\n"
       "  }\r\n"
       "}\r\n"
       "interface Handler { String purchaseTicket(String id); }\r\n")
open("tmp-crlf/src/main/java/demo/CRLF.java", "w", encoding="utf-8", newline="").write(src)
```

运行 Parser 后，预期得到（与仓库里 JUnit 测试的断言完全一致）：

```text
method.startLine() = 5          ← "@Override" 那一行
method.endLine()   = 8          ← "  }" 那一行
method.content()   = '  @Override\r\n  public String purchaseTicket(String id) {\r\n    return "ticket:" + id;\r\n  }'
```

**手算验证**（用 Python 复现 `offset()` 的逻辑）：

```python
src = open("tmp-crlf/src/main/java/demo/CRLF.java", encoding="utf-8", newline="").read()
lines = src.split("\r\n")

# 手动累加：每一行长度 + 2（一个 \r 一个 \n）
idx = 0
for n in range(1, 5):                 # 第 1~4 行
    idx += len(lines[n-1]) + 2
print("offset(line=5, col=1) =", idx)            # 预期 119

line8_start = idx + len(lines[4]) + 2            # 第 5 行之后
print("第 8 行 '  }' 的下标:", line8_start)
end_idx = line8_start + 3 - 1 + 1                # column=3 → +col-1 → 再 +1（闭区间）
print("slice = src[%d:%d] = %r" % (idx, end_idx, src[idx:end_idx]))
```

手算结果（已在本文档中验证过真实数值）：

```text
line1 'package demo;'                                len=13  +2 = 15  累计 15
line2 'public class TicketService implements Handler {' len=47 +2 = 49  累计 64
line3 '  public TicketService() {}'                  len=27  +2 = 29  累计 93
line4 '  /** Buy one ticket. */'                     len=24  +2 = 26  累计 119   ← ★ offset(line5, col1) = 119

第 8 行 '  }'，'}' 在第 3 列
line8_start = 205  →  offset(line8, col=3) = 205 + 3 - 1 = 207
slice = src[119 : 208]
      = '  @Override\r\n  public String purchaseTicket(String id) {\r\n    return "ticket:" + id;\r\n  }'
```

**三个由此亲手验证的结论**：

1. **CRLF 是两个字符**，所以每行要 `+2`；如果只 `+1`，第 5 行的 offset 会算成 115，切片整体偏移 4 个字符。
2. **闭区间**：`offset(207) + 1 = 208`，`substring(119, 208)` 才会包含第 172 行（此处是第 8 行）末尾的 `}`。
3. **首行缩进被保留**：切片以 `  @Override` 开头，因为 `range.begin.column = 3`。

做完请删除 `tmp-crlf/` 与 `tmp-chunks.jsonl`。

---

## 13. 学完后应该能够回答的问题

1. `findJavaFiles` 的四个过滤条件分别是什么？各自排除了什么？
2. 为什么 236 个 `.java` 文件只有 218 个被扫描到？
3. `src/test/java` 是怎么被排除的？（是显式排除还是副作用？）
4. `Position` 与 `Range` 的 1-based / 闭区间约定，分别对应代码里的哪个 `+1` 和 `-1`？
5. `offset()` 为什么必须手写逐字符扫描，而不是 `split("\n")`？
6. `slice()` 里 `Math.min(source.length(), ...)` 防的是什么？
7. 为什么 METHOD 的 `content` 用原始切片，而 CLASS 摘要里的字段用 `Node.toString()`？两者不矛盾吗？
8. METHOD Chunk 的 `content` 里有没有 Javadoc？Javadoc 在哪里被保留？
9. `signature` 与 `content` 首行的区别是什么？分别由哪个 API 产生？
10. CLASS 摘要包含哪七段内容？为什么不保存整个类源码（三个理由）？
11. CLASS Chunk 的 `content` 与 `[start_line, end_line]` 是否自洽？这对 Citation 意味着什么？
12. `parsed == 0` 这条检查防止的是哪一种最难排查的故障？
13. 当前有哪些 Java 语言结构**不会**产生类型级 Chunk？举出三个真实受影响的文件。
14. `detectModule()` 的推导规则是什么？真实值如何验证？
15. 删掉 `mapper.setPropertyNamingStrategy(SNAKE_CASE)` 会怎样？

---

## 本章源码阅读任务

### 第一遍

只看：

- `Main.java`（全文 106 行，重点是 `main` 与 `findJavaFiles`）
- `ChunkRecord.java`（全文 24 行，就是字段清单）

目标：理解整体流程——知道"扫哪些文件、每个文件经过什么、输出成什么形态"。

### 第二遍

重点看：

- `JavaSourceParser.java:39-74` 的 `parse()`（三类遍历）
- `JavaSourceParser.java:173-194` 的 `slice()` / `offset()`（**本篇最关键的两个函数**）
- `JavaSourceParser.java:122-157` 的 `callableRecord()`（字段逐一对应）

目标：理解**关键转换**——`Range` 如何变成 `content`，以及每个 ChunkRecord 字段分别从哪里来。

### 第三遍

带着问题阅读：

1. `offset()` 里 `while (line < position.line)` 这个条件，为什么是 `<` 而不是 `<=`？改错了会怎样？
2. 如果我把 `slice()` 换成 `callable.toString()`，哪些下游能力会失效？（结合第 01 篇与第 09 篇）
3. `typeRecord()` 里 `sha256(content)` 算的是摘要的 hash。这对"判断类是否变化"够用吗？
4. 如果我要支持 `enum`，需要在几个地方改动？`chunk_type` 的 CHECK 约束不加会怎样？
5. `findAll` 的顺序是源码顺序吗？如果不是，会有什么后果？（提示：JSONL 的可复现性）

---

## 调试观察点

Java 侧无处可打日志（它是 CLI），所以推荐的调试方式是**在 JUnit 测试里下断点**，而不是在 `main` 里。

**断点位置 1**：`JavaSourceParser.parse()` 的 `parser.parse(source)` 之后

| 变量 | 预期形态 |
|---|---|
| `source` | 一个 `String`，包含完整文件内容；CRLF 文件里含 `\r\n` |
| `result.isSuccessful()` | `true` |
| `unit` | `CompilationUnit`，非空 |
| `unit.getPackageDeclaration()` | `Optional`，非空（`package-info.java` 之类才可能为空） |
| `result.getProblems()` | 空列表（非空就意味着这个文件会被判为 failed） |

**断点位置 2**：`JavaSourceParser.slice()` 内部

| 变量 | 预期形态 |
|---|---|
| `range.begin` | `Position`，`line` 从 1 开始，`column` 从 1 开始 |
| `range.end` | `Position`，`line >= begin.line` |
| `start` | 0-based 下标，`0 <= start < source.length()` |
| `end` | `start < end <= source.length()` |
| `source.substring(start, end)` | 首行 = 注解或修饰符行；末行 = 闭合大括号行 |
| 关键自检 | `source.substring(start, end).equals(content)` 必须为 `true` |

**断点位置 3**：`Main.main()` 的 `catch (Exception exception)` 分支

| 变量 | 预期形态 |
|---|---|
| `failed` | 正常情况下恒为 `0`（真实语料 `failed=0`） |
| `exception.getMessage()` | 若是 `Parse problems: [...]`，说明是语法问题；若是其他，说明是 IO/环境问题 |
| `root.relativize(file)` | 用于定位是哪个文件失败——**这是最有用的一条信息** |

**断点位置 4**（推荐）：`JavaSourceParserTest.parsesJava21InterfaceMethodAndConstructorWithCrlf()`

| 变量 | 预期值 |
|---|---|
| `temporaryDirectory` | 一个临时目录（`@TempDir`） |
| `source`（Path） | `<tmp>/order/src/main/java/demo/TicketService.java` |
| `records.size()` | **5**（`TicketService` 类 + `TicketService` 构造器 + `purchaseTicket` 方法 + `Handler` 接口 + 接口的 `purchaseTicket` 抽象方法） |
| `method.startLine()` | `5` |
| `method.endLine()` | `8` |
| `method.content()` | 含 `return "ticket:" + id;\r\n`（**注意带 `\r\n`**） |
| `method.annotations()` | 含 `"@Override"` |
| `method.module()` | `"order"` |

**Python 侧观察点**（`java_parser_runner.py:50-60`，从 Java 交接给 Python 的地方）：

| 变量 | 预期形态 |
|---|---|
| `output` | `artifacts/java-chunks.jsonl` 的 `Path` |
| `line`（JSONL 循环里） | 一行完整 JSON，以 `{` 开头 `}` 结尾 |
| `chunks` | `list[Chunk]`，长度 **538** |
| `chunks[0].source_type` | `"CODE"` |
| `chunks[0].file_path` | 以 `services/gateway-services/` 开头（字典序第一） |
| `len(set(c.file_path for c in chunks))` | **206**（小于 538，因为一个文件有多个 Chunk） |

---

## 学习完成标准

学完后我应该能够：

1. **从源码指出 `MethodDeclaration` 是在哪里被遍历出来的**——`JavaSourceParser.java:61-66` 的 `unit.findAll(MethodDeclaration.class)`，并说明为什么 `CLASS` 与 `METHOD` 是两次独立遍历而不是嵌套遍历；
2. **解释 `Range` 与 `content` 的关系**，并写出 `slice()` 的三行逻辑（两次 `offset`、`+1` 闭区间修正、`Math.min` 边界保护）；
3. **手动判断某个方法的 start/end line**——给定一个 CRLF 文件与一个方法，能算出它的 `offset` 并推出切片范围（实验 C 的手算过程）；
4. **解释为什么 `Node.toString()` 不适合 METHOD 的 `content`**，同时说明它**为什么适合** CLASS 摘要里的字段渲染；
5. **添加一个新的 Metadata 字段并说全改动清单**——例如加 `is_static`（方法是否 static），应改：`ChunkRecord.java`（加字段）、`JavaSourceParser` 的 `callableRecord` 与 `typeRecord`（填值，Java 侧用 `method.isStatic()`）、`models.py` 的 `Chunk`（加字段）、`sql/001_schema.sql`（加列）、`storage.replace_repository`（INSERT 列表加一项），并决定它要不要进 `keyword_text()` / `embedding_text()`；
6. **指出当前实现的至少三个真实缺陷**（`enum` 无类型 Chunk、CLASS 的 `content` 与行号不自洽、普通注释丢失），并说明每个缺陷会影响哪一类问题；
7. **说明错误隔离的两道防线**（Java 侧 `parsed == 0` 抛异常、Python 侧 `if not code_chunks` 抛 `RuntimeError`），以及各自防的是哪种故障。
