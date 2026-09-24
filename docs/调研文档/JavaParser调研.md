# JavaParser 调研

> 本文是《开源项目调研.md》中 **JavaParser 部分（第 6 章）** 的独立交付物。
> 调研对象：`javaparser/javaparser`，仓库快照位于 `参考项目/javaparser-master`（`pom.xml` version `3.29.0-SNAPSHOT`，master 分支；readme 中的依赖示例版本为 `3.28.2`）。
> 调研范围：**仅用于验证 JavaParser 是否足以实现 DevContext 的 AST-aware Code Chunk**。不研究代码生成、不研究完整类型推断。

---

## 0. 阅读约定与本次调研的一个前提变更

**引用格式**：`相对仓库根目录的路径 : 行号`。行号来自本次快照实际读取的内容，可回查。

**关于最小验证 Demo**：调研需求第十三节原本要求"设计或实现一个最小实验，输入一个真实 my12306 Java 文件，输出 JSON"以确认方案可行。**该项已被明确取消，本次不实现 Demo。**

因此本文对"方案是否可行"的论证改为以下三类可回查的证据，并会在涉及处标明证据级别：

| 证据级别 | 来源 | 说明 |
| --- | --- | --- |
| A | 生产源码 | `javaparser-core/src/main/java/...`，即实际运行的代码 |
| B | 官方测试 | `javaparser-core-testing/src/test/java/...`，官方对自身行为的断言 |
| C | 官方文档 | `readme.md`、`LICENSE.*`，以及 readme 中给出的官方站点链接 |

> **需要诚实说明的边界**：本次调研结论均为源码级/测试级论证，**没有实际编译运行 JavaParser**。因此"在 my12306 真实代码上能跑通"这一点属于**推断**而非实测。本文第 19 节列出了这一项。

**与调研需求第六节建议章节的对应关系**（便于后续合并进《开源项目调研.md》）：

| 调研需求章节 | 本文位置 |
| --- | --- |
| 6.1 为什么选择 JavaParser | 第 1 节 |
| 6.2 AST 基本模型 | 第 2 节 |
| 6.3 CompilationUnit | 第 3 节 |
| 6.4 Class / Interface | 第 4 节 |
| 6.5 Method | 第 5 节 |
| 6.6 Annotation | 第 6 节 |
| 6.7 Range 与 Citation | 第 7 节（+ 第 9 节补充"取完整源码"） |
| 6.8 CodeChunk Schema 建议 | 第 8 节 |
| 6.9 SymbolSolver 未来演进 | 第 11 节（+ 第 12 节"V1 不实现"） |
| 6.10 V1 不实现内容 | 第 12 节 |
| （调研需求第十四节：设计问题判断） | 第 13 节 |
| （调研需求第九节：六段式结论） | 第 14 / 15 / 16 / 17 节 |
| （调研需求第三十一/三十二节：明确决定与 Decision Table） | 第 18 节 |

---

## 1. 为什么选择 JavaParser（6.1）

### 1.1 Problem

DevContext 的 Java Chunk 要求是：**按 Package / Class / Interface / Method / Constructor / Annotation 的语法边界切分，并保留 class、method、annotation、startLine、endLine 等元数据**（定义文档第 10 节）。

用正则或固定长度切分做不到这一点：Java 的大括号可以嵌套、字符串字面量和注释里可以包含任意字符、泛型和注解会让"找方法边界"变成一个必须真正解析语法的问题。

### 1.2 候选方案的取舍

上一轮 Continue 调研得到的关键事实是：**Continue 用 tree-sitter**，因为它要支持 20+ 语言（`core/util/treeSitter.ts:38-...` 的 `supportedLanguages` 表，`core/indexing/chunk/code.ts` 里满是 `node.type` 字符串判断）。tree-sitter 的代价是：

- 语法节点是**弱类型字符串**，没有编译期检查；
- 每种语言要单独写 `.scm` 查询文件；
- 要加载 wasm parser。

DevContext 是 **Java Only**。语言范围收窄后，通用解析器的复杂度完全没有必要承担。JavaParser 提供的是**强类型 AST**：`MethodDeclaration`、`ClassOrInterfaceDeclaration`、`AnnotationExpr` 都是真正的 Java 类，有 getter 和类型检查。

### 1.3 JavaParser 的实际情况（证据级别 C/A）

| 项 | 值 | 来源 |
| --- | --- | --- |
| 语言支持范围 | Java 1.0 – Java 25 | `readme.md:17` |
| 许可证 | LGPL-3 **或** Apache-2.0（使用者自行选择） | `readme.md:137`；`LICENSE.LGPL` / `LICENSE.APACHE` |
| 核心模块 | `javaparser-core` | `javaparser-core/pom.xml:10` |
| **核心模块运行时依赖** | **无**（`<dependencies>` 段为空） | `javaparser-core/pom.xml` |
| 符号求解模块 | `javaparser-symbol-solver-core`，依赖 javassist / guava / checker-qual | `javaparser-symbol-solver-core/pom.xml` |
| 官方站点 | http://javaparser.org | `readme.md:19` |
| 官方 Wiki | https://github.com/javaparser/javaparser/wiki | `readme.md:133` |

**`javaparser-core` 零运行时依赖**这一条对 DevContext 的"独立 CLI"设计很关键：`java-parser/` 模块只需要一个 jar，不需要传递任何第三方依赖，打包和部署成本接近零。

`readme.md:63` 明确给出按需依赖的建议：

> Using the dependency above will add both JavaParser and JavaSymbolSolver to your project. **If you only need the core functionality of parsing Java source code in order to traverse and manipulate the generated AST**, you can reduce your projects boilerplate by only including JavaParser to your project

这正是 DevContext V1 的定位——只要 AST，不要符号求解。

### 1.4 结论

**采用 JavaParser（`javaparser-core` only）。** 理由不是"JavaParser 更专业"，而是三条可验证的事实：Java-only 场景不需要多语言解析器；`javaparser-core` 零依赖；强类型 AST 让 chunk 提取代码可以用 getter 而不是字符串匹配。

**`javaparser-symbol-solver-core` V1 不引入**，理由见第 11 节。

---

## 2. AST 基本模型（6.2）

### 2.1 两条并行的体系：节点继承链 + 能力接口（mixin）

JavaParser 的 AST 设计是"**类继承链管结构，接口管能力**"。

**继承链**（以方法为例）：

```text
Node
 └─ BodyDeclaration<T>            (ast/body/BodyDeclaration.java:45)
     ├─ CallableDeclaration<T>    (ast/body/CallableDeclaration.java:49)
     │   ├─ MethodDeclaration          (ast/body/MethodDeclaration.java:60)
     │   └─ ConstructorDeclaration     (ast/body/ConstructorDeclaration.java:53)
     └─ CompactConstructorDeclaration  (ast/body/CompactConstructorDeclaration.java:68)
                                       ↑ record 紧凑构造器，直接继承 BodyDeclaration，不走 CallableDeclaration
```

（`CompactConstructorDeclaration` 不走 `CallableDeclaration` 这条支线这一点是实际读源码发现的，见 `CompactConstructorDeclaration.java:68`。V1 可忽略 record。）

`TypeDeclaration<T>` 是另一条支线，下挂 `ClassOrInterfaceDeclaration` / `EnumDeclaration` / `RecordDeclaration` / `AnnotationDeclaration`。

**能力接口**位于 `ast/nodeTypes/`（共 31 个），例如：

```text
NodeWithAnnotations    有注解            BodyDeclaration 实现
NodeWithJavadoc        有 Javadoc        CallableDeclaration / TypeDeclaration 实现
NodeWithSimpleName     有名字            CallableDeclaration 实现
NodeWithParameters     有参数列表        CallableDeclaration 实现
NodeWithAbstractModifier / NodeWithStaticModifier / NodeWithFinalModifier ...
NodeWithRange          有源码位置        Node 实现（所有节点都有）
```

`CallableDeclaration` 的 implements 列表（`ast/body/CallableDeclaration.java:49-60`）可以直接读出"一个可调用声明具备哪些能力"：

```java
public abstract class CallableDeclaration<T extends CallableDeclaration<?>> extends BodyDeclaration<T>
        implements NodeWithAccessModifiers<T>, NodeWithDeclaration, NodeWithSimpleName<T>,
                   NodeWithParameters<T>, NodeWithThrownExceptions<T>, NodeWithTypeParameters<T>,
                   NodeWithJavadoc<T>, NodeWithAbstractModifier<T>, NodeWithStaticModifier<T>,
                   NodeWithFinalModifier<T>, NodeWithStrictfpModifier<T> { ... }
```

**这个设计对 DevContext 的意义**：抽取 chunk 时不需要对每种节点写一遍 `getAnnotations()`，因为 `NodeWithAnnotations` 定义在基类 `BodyDeclaration` 上——`getAnnotations()` 对所有 body declaration（方法、构造器、字段、类）统一可用。

`ast/body/BodyDeclaration.java:45`

```java
public abstract class BodyDeclaration<T extends BodyDeclaration<?>> extends Node implements NodeWithAnnotations<T> {
```

`ast/body/BodyDeclaration.java:73`

```java
public NodeList<AnnotationExpr> getAnnotations() { return annotations; }
```

### 2.2 遍历 API

`ast/Node.java` 提供三种粒度的遍历（`:907-1005`）：

```java
public Stream<Node> stream()                                   // 深度优先，PREORDER
public <T extends Node> void walk(Class<T> nodeType, Consumer<T> consumer)
public <T extends Node> List<T> findAll(Class<T> nodeType)
public <T extends Node> List<T> findAll(Class<T> nodeType, Predicate<T> predicate)
public <N extends Node> Optional<N> findFirst(Class<N> nodeType)
```

向上查找由 `HasParentNode` 提供（`HasParentNode.java:66-97`）：

```java
default <N> Optional<N> findAncestor(Class<N>... types)
default <N> Optional<N> findAncestor(Predicate<N> predicate, Class<N>... types)
```

**DevContext 的 chunk 抽取骨架因此非常短**：

```java
CompilationUnit cu = ...;
for (MethodDeclaration m : cu.findAll(MethodDeclaration.class)) { ... }
// 需要包名 / 类名时向上找：
Optional<TypeDeclaration> owner = m.findAncestor(TypeDeclaration.class);
```

### 2.3 设计含义

- **强类型 + mixin** 让"按能力遍历"成为可能：想处理"所有带注解的节点"，目标是 `NodeWithAnnotations`；想处理"所有有名字的节点"，目标是 `NodeWithSimpleName`。
- 代价是节点类很多（`ast/` 下按 `body/`、`expr/`、`stmt/`、`type/`、`modules/` 分了十几个子包），首次阅读有一定学习曲线。

---

## 3. CompilationUnit：解析入口（6.3）

### 3.1 两套解析 API

JavaParser 有两套入口，**行为不同，必须区分**。

**旧 API：`StaticJavaParser`（`StaticJavaParser.java:285`）**

```java
public static CompilationUnit parse(@NotNull String code)
```

失败时抛 `ParseProblemException`。

**新 API：`JavaParser`（`JavaParser.java:297`）**

```java
public ParseResult<CompilationUnit> parse(String code)
```

**新 API 是 DevContext 应该用的**，理由是它对"解析不完整"的处理更合理。`ParseResult`（`ParseResult.java:67-124`）提供：

```java
public boolean isSuccessful()
public List<Problem> getProblems()
public Problem getProblem(int i)
public Optional<T> getResult()
public Optional<CommentsCollection> getCommentsCollection()
public Optional<Path> getSourcePath()
```

注意 `getResult()` 返回 `Optional<T>`——**即使 `isSuccessful()` 为 false，`getResult()` 仍可能返回一个部分 AST**。这对 DevContext 很重要：仓库里必然存在解析失败的 Java 文件（旧语法、生成代码、故意写坏的测试文件），此时应记录 problem 但尽量保留能抽到的 chunk，而不是整个文件丢弃。

> 此处的"部分 AST"行为是从 `ParseResult` 的 API 形状（`isSuccessful` 与 `getResult` 相互独立）推断的，本次未运行验证。使用前建议自行确认。

### 3.2 JavaParser 实例 vs 静态方法

`JavaParser.java:78-82` 的注释：

> Instantiate the parser with default configuration. Note that parsing can also be done with the static methods `{@link StaticJavaParser}`. **Creating an instance will reduce setup time between parsing files.**

DevContext 的 Java 解析 CLI 要遍历整个 repository（成千上万个文件），应**复用一个 `JavaParser` 实例**，而不是每次调用静态方法。

### 3.3 CompilationUnit 能取到什么

`ast/CompilationUnit.java` 的相关 getter：

| 方法 | 行号 | 用途 |
| --- | --- | --- |
| `getPackageDeclaration()` | `:229` | 返回 `Optional<PackageDeclaration>` |
| `getTypes()` | `:243` | 顶层类型列表 `NodeList<TypeDeclaration<?>>` |
| `getImports()` | `:213` | import 列表 |
| `getComments()` | `:183` | 属于本 CompilationUnit 的注释 |
| `getAllComments()` | `:200` | 全部注释 |
| `getPrimaryType()` | `:618` | 主类型（用于"一个文件一个主类"的常见情形） |
| `getClassByName(String)` / `getInterfaceByName(String)` | `:559` / `:584` | 按名查找 |

包名获取：

```java
cu.getPackageDeclaration()
  .map(PackageDeclaration::getNameAsString)   // NodeWithName:48
  .orElse(null)
```

`PackageDeclaration.getName()` 返回 `Name`（`PackageDeclaration.java:104`），`getNameAsString()` 来自 `NodeWithName` 的 default 方法（`nodeTypes/NodeWithName.java:48`）。

**注意**：`getPackageDeclaration()` 返回 `Optional`——默认包（default package）的文件没有 package 声明。DevContext 必须处理这种情况，不能直接 `.get()`。

---

## 4. Class / Interface（6.4）

### 4.1 ClassOrInterfaceDeclaration

`ast/body/ClassOrInterfaceDeclaration.java` 的关键 getter：

| 方法 | 行号 |
| --- | --- |
| `getName()` | 继承自 `TypeDeclaration`（`:168`） |
| `getNameAsString()` | 来自 `NodeWithSimpleName` |
| `isInterface()` | `:298` |
| `getMembers()` | 继承自 `TypeDeclaration`（`:110`） |
| `getExtendedTypes()` | `:278` |
| `getImplementedTypes()` | `:283` |
| `getPermittedTypes()` | `:289` |
| `getTypeParameters()` | `:295` |
| `getFullyQualifiedName()` | `:405`（覆盖） |
| `isNestedType()` | 继承自 `TypeDeclaration`（`:235`） |
| `isTopLevelType()` | 继承自 `TypeDeclaration`（`:196`） |

**`getFullyQualifiedName()` 是 DevContext 最该用的一个方法。** 它的实现（`ast/body/TypeDeclaration.java:219-231`）已经正确处理了嵌套类：

```java
public Optional<String> getFullyQualifiedName() {
    if (isTopLevelType()) {
        return findCompilationUnit().map(cu -> cu.getPackageDeclaration()
                .map(pd -> pd.getNameAsString())
                .map(pkg -> pkg + "." + getNameAsString())
                .orElseGet(() -> getNameAsString()));
    }
    return findAncestor(TypeDeclaration.class)
            .map(td -> (TypeDeclaration<?>) td)
            .flatMap(td -> td.getFullyQualifiedName().map(fqn -> fqn + "." + getNameAsString()));
}
```

即：顶层类型 → `package + "." + ClassName`；嵌套类型 → `外层全限定名 + "." + 内层名`。

而 `ClassOrInterfaceDeclaration.getFullyQualifiedName()` 只是加了一个特例（`:405-409`）：**局部类（LocalClassDeclarationStmt 里的类）返回 `Optional.empty()`**。

对 DevContext 的意义：`package_name` 与 `class_name` 可以用 `getFullyQualifiedName()` 一次拿到，**但必须接受 `Optional.empty()`**（局部类）。此时应回退到 `package_name` + `getNameAsString()`。

> DevContext 建议把 `package_name` 与 `class_name` 分开存两列（定义文档第 13 节的 knowledge_chunk 就是这么设计的），因为 keyword 检索时 `class_name` 的权重应高于 `package_name`。用 `getFullyQualifiedName()` 校验，用 `getNameAsString()` + `PackageDeclaration.getNameAsString()` 分别存。

### 4.2 是否需要 Class 级别的信息

`TypeDeclaration` 还有一个方法值得一提（`:203`）：

```java
public List<CallableDeclaration<?>> getCallablesWithSignature(CallableDeclaration.Signature signature)
```

这是按签名查找重载/覆写的方法。V1 用不到，但说明 JavaParser 在类型层面已经内置了"同签名方法检索"的能力，属于 P2 候选。

### 4.3 一个 DevContext 需要注意的点

Continue 的 AST chunker 会在类过大时把类头保留、内部函数折叠（`core/indexing/chunk/code.ts:110-123`）。JavaParser 这边**没有内置这种折叠**——`ClassOrInterfaceDeclaration.toString()` 会输出整个类的完整源码。

DevContext 若要做 Class chunk，需要自己实现"类头 + 成员签名列表"的摘要生成，或者干脆用 `getMembers()` 逐个处理。这属于第 13 节要讨论的设计问题。

---

## 5. Method / Constructor（6.5）

### 5.1 MethodDeclaration

`ast/body/MethodDeclaration.java`：

| 方法 | 行号 | 返回 |
| --- | --- | --- |
| `getName()` | 继承 `CallableDeclaration:136` | `SimpleName` |
| `getNameAsString()` | `NodeWithSimpleName` | `String` |
| `getType()` | `:228` | `Type`（返回类型） |
| `getParameters()` | 继承 `CallableDeclaration:155` | `NodeList<Parameter>` |
| `getThrownExceptions()` | 继承 `CallableDeclaration:174` | `NodeList<ReferenceType>` |
| `getTypeParameters()` | 继承 `CallableDeclaration:193` | `NodeList<TypeParameter>` |
| `getModifiers()` | 继承 `CallableDeclaration:117` | `NodeList<Modifier>` |
| `getAnnotations()` | 继承 `BodyDeclaration:73` | `NodeList<AnnotationExpr>` |
| `getBody()` | `:205` | **`Optional<BlockStmt>`** |
| `isAbstract()` / `isNative()` / `isSynchronized()` / `isDefault()` | `:366` / `:378` / `:382` / `:386` | `boolean` |
| `isPublic()` | `:350` | `boolean` |
| `getDeclarationAsString()` | `NodeWithDeclaration:37` | `String` |
| `getSignature()` | 继承 `CallableDeclaration:318` | `Signature` |
| `toDescriptor()` | `:333` | `String`（JVM 方法描述符） |

**`getBody()` 返回 `Optional` 是自然且必须的**：接口方法和抽象方法没有方法体。源码注释（`:198-203`）明确了这点。

`Modifier` 是可枚举的（`ast/Modifier.java:100` `enum Keyword`，含 `PUBLIC` / `ABSTRACT` / `STATIC` 等）。

### 5.2 签名（signature）的三种获取方式

DevContext 的 chunk 需要 `signature` 字段。JavaParser 提供三种，用途不同：

**方式一：`getDeclarationAsString()` → 人类可读签名**

`NodeWithDeclaration.java:37-38`

```java
default String getDeclarationAsString() {
    return getDeclarationAsString(true, true, true);
}
```

`MethodDeclaration.java:280` 的实现返回这种形态（注释在 `:275-277`）：

```text
[accessSpecifier] [static] [abstract] [final] [native] [synchronized]
returnType methodName ([paramType [paramName]]) [throws exceptionsList]
```

例如 `public void purchaseTicket(TicketPurchaseReqDTO req)`。

**这是 DevContext 的 `signature` 字段应该用的方式**——它就是给人看的、可以进 keyword 索引的字符串。三个布尔参数分别控制"是否含修饰符 / 是否含 throws / 参数是否带参数名"，flexible。

**方式二：`getSignature()` → 结构化签名**

`CallableDeclaration.java:318-325`

```java
public Signature getSignature() {
    return new Signature(
            getName().getIdentifier(),
            getParameters().stream()
                    .map(this::getTypeWithVarargsAsArray)
                    .map(this::stripGenerics)
                    .map(this::stripAnnotations)
                    .collect(toList()));
}
```

返回 `Signature(name, List<Type>)`。注意它**主动剥掉了泛型和参数注解**（`:327-345`），并把 varargs 的 `...` 转成数组类型。这是为"重载匹配"设计的，**不适合直接展示给用户**。

**方式三：`toDescriptor()` → JVM 描述符**

`MethodDeclaration.java:333` 返回 `(IDLjava/lang/Thread;)Ljava/lang/Object;` 这种形式。V1 用不到，P2 若要精确到"重载级别的符号引用"再用。

### 5.3 ConstructorDeclaration

`ast/body/ConstructorDeclaration.java:53`：

```java
public class ConstructorDeclaration extends CallableDeclaration<ConstructorDeclaration>
        implements NodeWithBlockStmt<ConstructorDeclaration>, NodeWithAccessModifiers<ConstructorDeclaration>,
                   NodeWithJavadoc<ConstructorDeclaration>, NodeWithSimpleName<ConstructorDeclaration>,
                   NodeWithParameters<ConstructorDeclaration>, NodeWithThrownExceptions<ConstructorDeclaration>,
                   NodeWithTypeParameters<ConstructorDeclaration>, Resolvable<ResolvedConstructorDeclaration> {
```

与 `MethodDeclaration` 的差异：**没有 `getType()`**（构造器没有返回类型），方法体是 `getBody()`（来自 `NodeWithBlockStmt`，非 Optional）。

DevContext 若要把构造器作为 Chunk，`method_name` 字段直接放 `getNameAsString()`（构造器名 = 类名），另用一个 `chunk_type = CONSTRUCTOR` 或 `method_kind` 字段区分。定义文档第 10 节把 `Constructor` 列为要识别的节点，所以建议单独记录，不要和 METHOD 混。

还有 `CompactConstructorDeclaration`（record 的紧凑构造器），P0 可忽略。

---

## 6. Annotation（6.6）

### 6.1 注解的类层次

三个子类（`ast/expr/`）：

```text
AnnotationExpr  (abstract, AnnotationExpr.java:45)
 ├─ MarkerAnnotationExpr          无参数：@Override        (MarkerAnnotationExpr.java:42)
 ├─ SingleMemberAnnotationExpr    单值：@GetMapping("/tickets")  (SingleMemberAnnotationExpr.java:43)
 └─ NormalAnnotationExpr         多值：@FeignClient(name="x", url="y")  (NormalAnnotationExpr.java:43)
```

`AnnotationExpr` 继承链上的位置（`AnnotationExpr.java:45-46`）：

```java
public abstract class AnnotationExpr extends Expression
        implements NodeWithName<AnnotationExpr>, Resolvable<ResolvedAnnotationDeclaration> {
```

源码注释（`:38-40`）："A base class for the different types of annotations."

### 6.2 取注解名

因为 `AnnotationExpr implements NodeWithName<AnnotationExpr>`，且 `NodeWithName` 提供了 default 方法（`nodeTypes/NodeWithName.java:48`）：

```java
default String getNameAsString()
```

所以**对任何注解，不分类型，`getAnnotations()` 遍历后取 `getNameAsString()` 即可**。这正是 DevContext 需要的：

```java
List<String> annotations = method.getAnnotations().stream()
        .map(AnnotationExpr::getNameAsString)
        .collect(toList());
```

对照调研需求第十二节第 3 点要验证的四个注解：

| 注解 | 形态 | 类 | `getNameAsString()` |
| --- | --- | --- | --- |
| `@Service` | 标记注解 | `MarkerAnnotationExpr` | `"Service"` |
| `@Transactional` | 标记注解（无参数时） | `MarkerAnnotationExpr` | `"Transactional"` |
| `@Transactional(readOnly = true)` | 普通注解 | `NormalAnnotationExpr` | `"Transactional"` |
| `@GetMapping("/tickets")` | 单值注解 | `SingleMemberAnnotationExpr` | `"GetMapping"` |
| `@FeignClient(name = "ticket-service")` | 普通注解 | `NormalAnnotationExpr` | `"FeignClient"` |

**四种形态都能覆盖，结论：可行。**

### 6.3 直接判断注解是否存在

`NodeWithAnnotations` 还提供现成的判断方法（`nodeTypes/NodeWithAnnotations.java`）：

| 方法 | 行号 |
| --- | --- |
| `isAnnotationPresent(String annotationName)` | `:189` |
| `isAnnotationPresent(Class<? extends Annotation>)` | `:200` |
| `getAnnotationByName(String annotationName)` → `Optional<AnnotationExpr>` | `:209` |
| `getAnnotationByClass(Class)` → `Optional<AnnotationExpr>` | `:220` |

DevContext 若要识别"这是一个事务方法"，可以直接：

```java
if (method.isAnnotationPresent("Transactional")) { ... }
```

### 6.4 两个必须注意的坑

**坑一：`getNameAsString()` 返回的是源码字面，不是解析后的全限定名。**

如果源码写 `@org.springframework.transaction.annotation.Transactional`，`getNameAsString()` 返回的是那个全限定字符串，不是 `"Transactional"`。同理，如果源码 import 了 `org.springframework.web.bind.annotation.GetMapping` 但注解写作 `@GetMapping`，JavaParser **不会**把它还原成全限定名——**那需要 SymbolSolver**。

对 DevContext 的影响：V1 用注解做过滤/加权时，**应做后缀匹配**（`name.endsWith("Transactional")`）而不是精确相等，否则会漏掉全限定写法。这是一个必须写进实现的细节。

**坑二：注解在层级上的归属。**

- 方法上的注解 → `MethodDeclaration.getAnnotations()`
- 类上的注解 → `ClassOrInterfaceDeclaration.getAnnotations()`（继承自 BodyDeclaration）
- 字段上的注解 → `FieldDeclaration.getAnnotations()`（同上）

三者都是 `BodyDeclaration` 子类，API 一致。但**注解不属于它所标注的方法的 Range**——这一点在第 7 节说明，直接影响 citation 的准确性。

---

## 7. Range 与 Citation（6.7）

这一节是本次调研最关键的部分，因为它决定 DevContext 的 citation `TicketService.java Lines 120-188` 能否落地。

### 7.1 Position 是 1-based

`Position.java:29`

> `A position in a source file. Lines and columns start counting at 1.`

并在字段上再次强调（`:31-40`）：

```java
public final int line;      // The first line -- note that it is 1-indexed
public final int column;    // The first column -- note that it is 1-indexed
public static final int FIRST_LINE = 1;
public static final int FIRST_COLUMN = 1;
```

**这对 DevContext 是好事**：Continue 内部用 0-based、展示时 +1（`core/context/retrieval/retrieval.ts` 的 `r.startLine + 1`），而 JavaParser 直接就是 1-based。DevContext 的 Java 侧 chunk 存 1-based 行号即可，展示时不需要做 ±1 换算，减少一类 off-by-one bug。

但要留意：如果 DevContext 的 Python 侧某处用了 0-based 约定，两者交界处必须有明确约定。建议统一为 **1-based 存储，1-based 展示**。

### 7.2 Range 是闭区间

`Range.java:26`

> `A range of characters in a source file, from "begin" to "end", **including the characters at "begin" and "end"**.`

构造器会自动纠正反序（`Range.java:42-52`）：若 begin 晚于 end，两者交换。

`Range.getLineCount()`（`:251`）：

```java
public int getLineCount() { return end.line - begin.line + 1; }
```

**闭区间的含义**：`begin.line..end.line` 就是完整的行号范围。DevContext 生成 `Lines 120-188` 时，直接用 `range.begin.line` 和 `range.end.line`，**不需要任何 ±1**。

### 7.3 从节点取 Range

Range 定义在 `NodeWithRange` 接口上（`ast/nodeTypes/NodeWithRange.java`），注意注释：

> `A node that has a Range, which is **every** Node.`

```java
Optional<Range> getRange();
default Optional<Position> getBegin() { return getRange().map(r -> r.begin); }
default Optional<Position> getEnd()   { return getRange().map(r -> r.end); }
default boolean hasRange()            { return getRange().isPresent(); }
```

`Node` 实现该接口（`Node.java:123-124`）：

```java
public abstract class Node
        implements Cloneable, HasParentNode<Node>, Visitable, NodeWithRange<Node>, NodeWithTokenRange<Node> {
```

`Node.getRange()` 的实现（`Node.java:259-261`）：

```java
public Optional<Range> getRange() {
    return Optional.ofNullable(range);
}
```

### 7.4 Range 从哪里来（重要）

Range **不是解析时直接记录的，而是从 tokenRange 推导出来的**。`Node.java:267-286`：

```java
public Node setTokenRange(TokenRange tokenRange) {
    this.tokenRange = tokenRange;
    if (tokenRange == null
            || !(tokenRange.getBegin().hasRange() && tokenRange.getEnd().hasRange())) {
        range = null;                        // ← 拿不到 token range，range 就丢失
    } else {
        range = new Range(
                tokenRange.getBegin().getRange().get().begin,
                tokenRange.getEnd().getRange().get().end);
    }
    return this;
}
```

**这意味着：**
1. Range = 从**第一个 token 的起点**到**最后一个 token 的终点**。
2. Range 依赖 token 信息 → 依赖 `ParserConfiguration.isStoreTokens()`。

### 7.5 一个必须避开的配置陷阱

`ParserConfiguration.java:268-270` 的默认值：

```java
private boolean storeTokens = true;
private boolean attributeComments = true;
```

看起来没问题——默认都开着。但 `setStoreTokens` 的实现（`:435-441`）有一个连带效果：

```java
public ParserConfiguration setStoreTokens(boolean storeTokens) {
    this.storeTokens = storeTokens;
    if (!storeTokens) {
        setAttributeComments(false);      // ← 连带关闭注释归属
    }
    return this;
}
```

**结论（DevContext 必须遵守）**：

> **不要为了省内存而调用 `setStoreTokens(false)`。** 它会同时 (a) 让所有 `getRange()` 返回 `Optional.empty()`，citation 彻底失效；(b) 关闭注释归属，Javadoc 也拿不到。

配置上应该保持默认（`storeTokens = true`, `attributeComments = true`），或者显式设置：

```java
ParserConfiguration config = new ParserConfiguration()
        .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)  // 按 my12306 实际版本
        .setStoreTokens(true)
        .setAttributeComments(true);
```

（`setLanguageLevel` 在 `ParserConfiguration.java:493`，`getLanguageLevel` 在 `:498`；默认语言级别字段在 `:284`，值为 `POPULAR`。）

### 7.6 Range 是否可靠：官方测试作为证据（证据级别 B）

**测试一：`NodePositionTest`**（`javaparser-core-testing/src/test/java/com/github/javaparser/ast/NodePositionTest.java`）

该测试对"类/接口/枚举/注解/字段/方法/构造器"等各种代码片段调用 `ensureAllNodesHaveValidBeginPosition`，其断言（`:86-98`）为：

```java
getAllNodes(cu).forEach(n -> {
    assertNotNull(
            n.getRange(),
            String.format("There should be no node without a range: %s (class: %s)",
                    n, n.getClass().getCanonicalName()));
    if (n.getBegin().get().line == 0 && !n.toString().isEmpty()) {
        throw new IllegalArgumentException("There should be no node at line 0: " + n + " (class: "
                + n.getClass().getCanonicalName() + ")");
    }
});
```

**这个断言的含义**：JavaParser 官方对"解析出来的 AST 中，每个节点都必须有 Range，且行号不能是 0"做了强制测试。上面覆盖的片段包含 `public class A { void foo() {} }` 和 `public class A { A() {} }`——**正是 DevContext 关心的 Method 与 Constructor**。

**测试二：`JavaParserTest`** 中的精确 Range 断言（`javaparser-core-testing/.../JavaParserTest.java`）：

```java
assertTrue(memberDeclaration.hasRange());
assertEquals(new Range(new Position(1, 17), new Position(1, 29)), memberDeclaration.getRange().get());
```

以及 `:205`、`:221` 等处对 `type.getRange()`、`castExpr.getRange()` 的精确断言。

**证据级别说明**：这两处是官方测试代码（级别 B），不是我在 my12306 上的实测。但它们足以支持"**对能正常解析的 Java 源码，Method 的 startLine / endLine 可以稳定获得**"这一结论。

### 7.7 一个会影响 citation 准确性的细节：注解是否在 Range 内

**结论：是，注解在 MethodDeclaration 的 Range 内。** 这一点可以从 JavaCC 语法定义直接确认（证据级别 A，语法文件本身）。

`javaparser-core/src/main/javacc/java.jj:2254-2290` 是 `MethodDeclaration` 的产生式，关键三行：

```java
MethodDeclaration MethodDeclaration(ModifierHolder modifier):
{
    JavaToken begin = modifier.begin;                      // ← 起点继承自调用方传进来的 ModifierHolder
}
{
    // Modifiers already matched in the caller!
    [ typeParameters = TypeParameters() { begin = orIfInvalid(begin, typeParameters.range.getBegin()); } ]
    annotations = Annotations() { modifier.annotations.addAll(annotations); begin = orIfInvalid(begin, nodeListBegin(annotations)); }
    ...
    return new MethodDeclaration(range(begin, token()), modifier.modifiers, modifier.annotations, ...);
}
```

而 `modifier.begin` 来自调用方的 `Modifiers()`（`java.jj:1355-1401`），该产生式**同时匹配修饰符和注解**，并把两者的最早位置记为 `begin`（`:1396`）：

```java
ann = Annotation() { annotations = add(annotations, ann); begin = orIfInvalid(begin, ann); }
```

所以 `MethodDeclaration.getRange().begin` = **源码中最先出现的「修饰符或注解」的位置**。

这对 DevContext 是**好事**：`TicketService.java Lines 120-188` 会自然覆盖 `@Transactional` 这些注解行，用户能看到注解。这与调研需求第十节"Annotation 对代码理解非常重要"一致。

**两个必须注意的推论：**

1. **Range 的终点是最后一个被消费的 token**（`range(begin, token())`）。对有方法体的方法，终点是右大括号 `}`；对抽象方法或接口方法（以 `;` 结尾），**终点是那个分号**——也就是说抽象方法的 chunk 会包含结尾分号。

2. **`getRange()` 不包含前置注释 / Javadoc。** Javadoc 存在节点的 `comment` 属性里（`Node.getComment()`，`Node.java:251`），**不是子节点**，因此不在 token range 内，也就不在 Range 内。DevContext 若希望 citation 与 content 都包含 Javadoc，需要主动把 comment 的起始位置并入 Range，或者干脆在 chunk 里分开存 `javadoc` 字段（本文推荐后者，见第 9.4 与 13.4 节）。

### 7.8 其它可能影响稳定性的点

| 风险 | 说明 | 处理建议 |
| --- | --- | --- |
| 节点是手工构造的（非解析产物） | `range` 字段为 null，`getRange()` 为空 | DevContext 只处理解析产物，不受影响；但仍应 `ifPresent` |
| `setStoreTokens(false)` | Range 全部丢失（见 7.5） | 保持默认 true |
| 解析失败的文件 | 部分 AST 可能有节点缺 Range | 用 `getRange().ifPresent(...)` 守卫，缺 Range 的 chunk 跳过而非崩溃 |
| tab 缩进 | column 的语义受 `ParserConfiguration.setTabSize`（`:455`）影响 | DevContext 只用 line，不用 column 生成 citation，规避此问题 |
| 换行符 CRLF/LF | `setDetectOriginalLineSeparator`（`:518`）可检测原始行分隔符 | 行号不受影响；但若要按 Range 切原始文本（见第 9 节），需注意 |

`Range` 还提供区间关系判断，V1 用不到但 P2 有用：`contains(Range)`（`:141`）、`contains(Position)`（`:155`）、`strictlyContains(Range)`（`:164`）、`overlapsWith(Range)`（`:192`）。

### 7.9 结论

**JavaParser 可以为 DevContext 提供稳定的 file + lines citation，可行。** 前提是三条实现约束：保持 `storeTokens = true`；对 `getRange()` 的 `Optional` 做守卫；只依赖 line 不依赖 column。

---

## 8. Code Chunk Schema 建议（6.8）

基于以上 API，DevContext 的 `CodeChunk V1 Schema` 与 JavaParser API 的逐字段映射：

| DevContext 字段 | JavaParser 获取方式 | 备注 |
| --- | --- | --- |
| `repository` | 外部传入（CLI 参数） | 不由 JavaParser 提供 |
| `module` | 从文件路径推导 | JavaParser 不感知 Maven 模块 |
| `file_path` | 外部传入 | 相对仓库根目录 |
| `package_name` | `cu.getPackageDeclaration().map(PackageDeclaration::getNameAsString)` | 可能为空（默认包） |
| `class_name` | `m.findAncestor(TypeDeclaration.class).map(TypeDeclaration::getNameAsString)` | 方法所属类 |
| `method_name` | `m.getNameAsString()` | 构造器则为类名，需用 `chunk_type` 区分 |
| `chunk_type` | 由节点类型判定 | `METHOD` / `CONSTRUCTOR` / `CLASS` |
| `annotations` | `m.getAnnotations().stream().map(AnnotationExpr::getNameAsString)` | 源码字面，建议后缀匹配 |
| `signature` | `m.getDeclarationAsString()` | 含修饰符 / throws / 参数名 |
| `modifiers` | `m.getModifiers().stream().map(Modifier::getKeyword)` | 可选，`signature` 里已含 |
| `return_type` | `m.getType().asString()` | 仅 METHOD；CONSTRUCTOR 无 |
| `content` | **按 Range 从原始文件行切片**（见第 9 节） | 不用 `toString()` |
| `start_line` | `m.getRange().get().begin.line` | 1-based，含注解行 |
| `end_line` | `m.getRange().get().end.line` | 1-based，闭区间 |
| `javadoc` | `m.getJavadocComment().map(JavadocComment::getContent)` | 可选字段 |
| `content_hash` | 外部计算（文件级 sha256） | 复用 Continue 的设计 |
| `is_abstract` 等 | `m.isAbstract()` / `m.isDefault()` | 可选，用于过滤 |

**三个设计判断：**

1. **`signature` 用 `getDeclarationAsString()`**，不要用 `getSignature()`（后者剥掉泛型，是给重载匹配用的）。`signature` 进入 keyword 索引价值很高——查 `purchaseTicket` 时能同时命中方法名与参数类型。

2. **`annotations` 存字符串数组而非布尔标记**。因为 DevContext 之后可能想按注解做加权（如"带 `@Transactional` 的方法提权"），存字符串更灵活。

3. **`content` 用原始切片而不是 `toString()`**，理由见下一节。

---

## 9. 取"完整方法源码"的正确方式（6.7 补充 / 调研需求第十二节第 4 点）

调研需求要求验证"是否可以获得完整 Method Source，例如 `@Transactional public void purchaseTicket(...) { ... }`"。答案是可以，但**有两种方式，行为不同，必须选对**。

### 9.1 方式一：`Node.toString()` —— 会重新格式化

`Node.java:338-341`

```java
/**
 * @return pretty printed source code for this node and its children.
 */
@Override
public final String toString() {
    Printer printer = getPrinter();
    ...
    return printer.print(this);
}
```

`toString()` 走的是 `PrettyPrintVisitor`。以方法为例（`printer/PrettyPrintVisitor.java:1092-1120`）：

```java
public void visit(final MethodDeclaration n, final Void arg) {
    printOrphanCommentsBeforeThisChildNode(n);
    printComment(n.getComment(), arg);              // ← 注释/Javadoc 会被打印
    printMemberAnnotations(n.getAnnotations(), arg); // ← 注解会被打印
    printModifiers(n.getModifiers());
    printTypeParameters(n.getTypeParameters(), arg);
    ...
}
```

**能得到什么**：完整的方法源码，**含注解、含注释/Javadoc、含修饰符**——形式上完全满足调研需求举的例子。

**问题**：它是**重新格式化**的结果，不是原始文本。缩进、空行、换行位置、行内注释的位置都可能与源文件不同。

对 DevContext 这是一个**实质性缺陷**：chunk 的 `content` 如果与 `start_line`/`end_line` 指向的原始行不一致，那么：
- citation 里的行号范围与用户实际看到的代码对不上；
- Evaluation 里"检索到的 chunk 是否命中 ground truth"无法用行号核验。

### 9.2 方式二：`LexicalPreservingPrinter` —— 保留原始格式

`printer/lexicalpreservation/LexicalPreservingPrinter.java` 提供：

| 方法 | 行号 |
| --- | --- |
| `setup(N node)` | `:110` |
| `isAvailableOn(Node node)` | `:128` |
| `print(Node node)` | `:724` |

`setup()` 会为节点建立 `NodeText` 数据（`NODE_TEXT_DATA` 定义在 `:89`），之后 `print()` 能输出保留原始词法（含注释、空格、换行）的文本。

这是 JavaParser 对"修改 AST 后如何只改动那一处、其余原样保留"这一问题的标准解法（用于代码重构工具）。它是**功能性的**，但为 DevContext 的只读场景引入它有点重。

### 9.3 方式三（推荐）：按 Range 从原始文件行切片

因为 Range 已经精确给出了 `begin.line` / `end.line`，而 DevContext 本来就持有文件全文（它要算 content_hash），所以最直接的做法是：

```java
String[] lines = fileContent.split("\n", -1);
Range r = method.getRange().orElseThrow();
String content = String.join("\n",
        Arrays.copyOfRange(lines, r.begin.line - 1, r.end.line));   // 1-based → 0-based 索引
```

**推荐理由：**

1. **零额外依赖、零额外状态**，几行代码；
2. **`content` 与 `start_line`/`end_line` 天然一致**，citation 可被机器核验；
3. **不引入重新格式化的风险**；
4. 与 Continue 的做法一致——Continue 的 chunk 也是 `content` + `startLine` + `endLine` 三元组，它不做任何重新格式化。

**代价**：如果要把 Javadoc 也放进 chunk，需要额外处理（Javadoc 不在 Range 内，见 7.7）。

### 9.4 结论

| 方式 | 保真度 | 含注解 | 含 Javadoc | 复杂度 | DevContext 结论 |
| --- | --- | --- | --- | --- | --- |
| `toString()` | 格式化后 | 是 | 是 | 低 | **不用**（与行号不一致） |
| `LexicalPreservingPrinter` | 原始 | 是 | 是 | 高 | 不用（只读场景过重） |
| **Range 切片** | **原始** | **是**（注解在 Range 内） | 否 | **最低** | **采用** |

Javadoc 作为**独立字段**存储，不混入 `content`。这样既保证 citation 精确，又保留了文档信息。

---

## 10. Javadoc / Comment（6.10 前置）

### 10.1 注释的挂载方式

`Node.getComment()`（`Node.java:251-253`）：

```java
public Optional<Comment> getComment() {
    return Optional.ofNullable(comment);
}
```

**注释挂在节点上**，而不是作为子节点。归属规则由 `CommentsInserter` 决定（`CommentsInserter.java:85`）：

> `If they preceed a child they are assigned to it, otherwise they remain "orphans"`

无法归属的注释通过 `Node.getOrphanComments()`（`Node.java:435`）和 `getAllContainedComments()`（`:446`）获取。

### 10.2 注释的类型

`ast/comments/` 下有：

```text
Comment (abstract)     getContent() :75, isOrphan() :132, getCommentedNode() :106
 ├─ LineComment
 ├─ BlockComment
 ├─ JavadocComment
 └─ MarkdownComment / TraditionalJavadocComment
```

`Comment.getContent()`（`:75`）返回注释内容（不含 `//` 或 `/* */` 定界符）。

### 10.3 结构化的 Javadoc

`NodeWithJavadoc`（`ast/nodeTypes/NodeWithJavadoc.java`）提供：

```java
default Optional<JavadocComment> getJavadocComment()   // :46
default Optional<Javadoc> getJavadoc()                 // :56  → getJavadocComment().map(JavadocComment::parse)
```

`Javadoc`（`javadoc/Javadoc.java`）是把 Javadoc 注释**解析成结构**的结果：

| 方法 | 行号 | 返回 |
| --- | --- | --- |
| `getDescription()` | `:143` | `JavadocDescription` |
| `getBlockTags()` | `:150` | `List<JavadocBlockTag>` |
| `toText()` | `:89` | 纯文本 |
| `toComment()` | `:108` | 转回注释 |

也就是说，如果 DevContext 将来想让 LLM 理解"这个方法的 `@param`/`@return`/`@throws` 说了什么"，可以用 `getJavadoc().getBlockTags()` 拿到结构化数据，而不必把整段注释塞进 context。

### 10.4 默认行为

`attributeComments` 默认为 `true`（`ParserConfiguration.java:270`），所以注释归属是**默认开启**的，不需要额外配置——**只要不去调用 `setStoreTokens(false)`**（见 7.5）。

### 10.5 DevContext 建议（对应调研需求第十四节）

见第 13.4 节。

---

## 11. SymbolSolver 与未来演进（6.9）

调研需求第十五节要求：不深入实现，只需了解这些能力未来是否可以通过 JavaParser / SymbolSolver 演进，并放入 P1 / P2。

### 11.1 它是什么

`readme.md:60-61`

> While JavaParser generates an Abstract Syntax Tree, **JavaSymbolSolver analyzes that AST and is able to find the relation between an element and its declaration** (e.g. for a variable name it could be a parameter of a method, providing information about its type, position in the AST, etc).

即：AST 回答"代码长什么样"，SymbolSolver 回答"这个名字指向哪个声明"。

### 11.2 入口

`JavaSymbolSolver`（`javaparser-symbol-solver-core/.../JavaSymbolSolver.java`）：

| 方法 | 行号 |
| --- | --- |
| `JavaSymbolSolver(TypeSolver)` | `:88` |
| `inject(CompilationUnit)` | `:96` |
| `resolveDeclaration(Node, Class<T>)` | `:101` |
| `toResolvedType(Type, Class<T>)` | `:489` |
| `calculateType(Expression)` | `:499` |

另有 `JavaParserFacade` 作为门面类。

### 11.3 成本：TypeSolver 体系

SymbolSolver **必须**配置 TypeSolver 才能工作（`symbolsolver/resolution/typesolvers/`）：

```text
ReflectionTypeSolver    JDK / classpath 反射
JavaParserTypeSolver    指定源码目录     (JavaParserTypeSolver.java:71-91 有 6 个构造器)
JarTypeSolver           jar 包
AarTypeSolver           Android aar
MemoryTypeSolver        内存中的类型
ClassLoaderTypeSolver   classloader
CombinedTypeSolver      组合，按顺序尝试 (CombinedTypeSolver.java:60)
```

典型用法（`javaparser-symbol-solver-testing/.../Issue113Test.java:49-51`）：

```java
typeSolver = new CombinedTypeSolver(
        new ReflectionTypeSolver(),
        new JavaParserTypeSolver(adaptPath("src/test/resources/issue113"), new LeanParserConfiguration()));
```

**这就是核心成本**：对一个 Maven 多模块项目，要正确解析 `TicketServiceImpl` 里的 `FeignClient` 指向哪个服务、某个 import 的类来自哪个 jar，需要把**所有模块的源码目录 + 所有依赖 jar** 都喂给 TypeSolver。这与 DevContext"10～15 天完成 MVP"的约束直接冲突。

**额外依赖成本**：`javaparser-symbol-solver-core` 依赖 javassist、guava、checker-qual（`javaparser-symbol-solver-core/pom.xml`），而 `javaparser-core` 是零依赖的。

**额外风险**：解析失败时抛 `UnsolvedSymbolException`（`AnnotationExpr.java` 已经 import 了它）。在"仓库里总有一些文件解析不全"的现实下，需要大量 try/catch 兜底。

### 11.4 演进路径

对照 DevContext 定义文档第 11 节给出的 V1→V4 路线：

```text
V1  AST Chunk                ← 本次调研覆盖，JavaParser core 足够
V2  Symbol Resolution        ← 引入 SymbolSolver + TypeSolver
V3  Reference                 ← 基于 V2，找"谁引用了这个符号"
V4  Call Graph                ← 基于 V3，构图
```

**结论：V1 用 `javaparser-core` 足够；SymbolSolver 属于 P2。** 判断依据不是"SymbolSolver 不好"，而是"它的前置条件（完整 TypeSolver 配置）在 10～15 天内无法可靠完成，且 V1 的检索质量并不依赖它"。

一个折中选项（P1 可选）：用 `ReflectionTypeSolver` 单配（只解析 JDK 类型，不解析项目内类型），成本低，能解决"`List` / `Optional` 这些 JDK 类型"的部分问题。但这仍不是 V1 必需项。

---

## 12. V1 不实现内容（6.9 补充）

| 能力 | 归属 | 理由 |
| --- | --- | --- |
| SymbolSolver / TypeSolver | P2 | 前置配置成本高，V1 检索质量不依赖 |
| Reference Resolution（谁引用了这个符号） | P2 | 依赖 SymbolSolver |
| Call Graph | P2/P3 | 依赖 Reference |
| Inheritance Graph | P2 | 可用 AST 的 `getExtendedTypes()` / `getImplementedTypes()` 做"本文件内的浅层继承"，但跨文件继承需要 SymbolSolver（P2） |
| 完整 Java 类型推断 | 不做 | 需要完整 classpath，MVP 阶段无收益 |
| 代码生成 / AST 改写 | 不做 | DevContext 是只读检索系统（定义文档第 29 节） |
| `LexicalPreservingPrinter` | 不做 | 只读场景不需要（见 9.2） |
| `javaparser-core-serialization`（AST → JSON） | 可选 | 见下方说明 |

**关于 `javaparser-core-serialization`**：`readme.md:81` 提到自 3.6.17 起 AST 可序列化为 JSON，有独立模块。但 **DevContext 不应直接用它**——它输出的是完整 AST 的 JSON（节点树），而 DevContext 需要的是**扁平的 chunk 列表**（每个 chunk 带 metadata）。两者结构不同，用它会引入一次冗余转换。DevContext 应自己从 AST 抽取字段后序列化为自己的 chunk JSON。

---

## 13. 需要给出判断的设计问题（调研需求第十四节）

### 13.1 Method 是否永远等于一个 Chunk？

**判断：V1 是；但必须有超长降级路径。**

调研需求给出的候选策略是：

```text
Method <= limit  → 整体 Chunk
Method > limit   → Method 内部二次切分
```

**支持"以 Method 为 Chunk 单位"的源码依据**：

- `MethodDeclaration.getRange()` 给出完整的 `begin.line` / `end.line`，天然就是一个 chunk 的边界；
- `getDeclarationAsString()` 给出人类可读签名，可作为 chunk 的 `signature`；
- 官方测试（`NodePositionTest`）保证 Method 节点有 Range（证据级别 B）。

**超长方法怎么办**（Java 中确实存在，例如一个巨大的 `purchaseTicket()` 或生成代码）：

Continue 的处理方式是"结构保真折叠"（`core/indexing/chunk/code.ts:125-171`）——保留签名、把函数体折叠成 `{ ... }`，若仍超限再逐步降级。**这个思路在 JavaParser 上不能直接复用**，因为 JavaParser 不提供"只打印到函数体之前"的 API。

DevContext 的建议（**按成本从低到高，V1 用第一种**）：

1. **V1：整块 + 标记超长。** 若 `tokenCount(content) > limit`，仍然作为**一个** chunk 入库，但加一个 `is_oversized = true` 标记，并在 embedding 前按 token 上限截断（保留方法签名部分 + 前 N token 的 body）。理由：截断只损失召回质量，不破坏 citation 的准确性；而"按 token 切分"会产生多个指向同一方法、行号范围重叠的 chunk，使 RRF 融合和 Evaluation 都变复杂。

2. **P1：按语句块二次切分。** `MethodDeclaration.getBody()` 返回 `Optional<BlockStmt>`，`BlockStmt` 的 `getStatements()` 给出语句列表，每条语句都有自己的 Range。可以按语句边界累积切分，**每个子 chunk 保留相同的 class/method/signature 元数据，但用不同的 start_line/end_line 和子 chunk 序号区分**。调研需求第十四节明确要求这种降级"必须保留 class、method、file、line metadata"——语句级切分天然满足。

3. 不建议按固定字符数盲目切分（这正好是 DevContext 要避免的做法）。

**另一个必须处理的情况：抽象方法与接口方法。** `getBody()` 返回 `Optional.empty()`（`MethodDeclaration.java:205`）。此时 chunk 的 `content` 就是签名行（Range 仍然完整），不应跳过——接口方法恰恰是"这个服务提供什么能力"的关键信息。

### 13.2 Class Chunk 是否有必要？

**判断：V1 采用「METHOD 为主 + CLASS 辅助」，但 CLASS chunk 必须是"摘要形态"，不是"完整源码"。**

**理由是三点：**

1. **纯 METHOD 会丢失"类是什么"这一层信息。** 问题"票务服务有哪些职责"无法通过检索单个方法回答。而类级注解（`@Service`、`@RestController`、`@FeignClient`）、继承关系（`getExtendedTypes()` / `getImplementedTypes()`）都只在类上。

2. **但完整的 Class chunk 会污染检索。** 一个 500 行的 `TicketServiceImpl` 作为一个 chunk，其 embedding 是全类语义的平均，对任何具体问题都"有点像但都不准"；而且它会和类内每个方法 chunk 内容重叠，导致检索结果里同一段代码出现多次。Continue 的做法（类头保留 + 内部函数折叠，`code.ts:110-123`）正是为了缓解这个问题。

3. **MIXED 场景需要类级"桥"。** 问"为什么 Feign 调用要移出购票事务"时，设计文档提到的是 `TicketServiceImpl` 这个类名，而答案可能在某个具体方法里。有类级 chunk 才能在"类名 → 类 → 成员方法"之间建立一跳。

**具体做法建议**：

```text
CLASS chunk 的 content = 类签名 + 注解 + 成员方法签名列表
```

例如：

```java
@Service
public class TicketServiceImpl implements TicketService {
    public TicketPurchaseRespDTO purchaseTicket(TicketPurchaseReqDTO req);
    public boolean cancelTicket(Long orderId);
    ...
}
```

这可以用 `getAnnotations()` + `getDeclarationAsString()`（类也有 `getDeclarationAsString()`）+ `getMembers()` 中的 `BodyDeclaration.isCallableDeclaration()` 过滤后逐个取签名来拼装。**注意这不是 JavaParser 的内置能力，需要自己实现约 20 行代码。**

顺带说明：这类"符号摘要"正是 Continue 用独立的 `codeSnippets` 索引承载的东西（`core/indexing/CodeSnippetsIndex.ts` 的 `title` + `signature`）。DevContext 不需要单独建一个索引，把这些信息放在 CLASS chunk 的 content 里就够——这算是相对 Continue 的一处简化。

### 13.3 Class Chunk 与 Method Chunk 的关系

建议在 `knowledge_chunk` 里用 `chunk_type` 区分（`CLASS` / `METHOD` / `CONSTRUCTOR`），并加一个可选的 `parent_chunk_id` 或至少让两者共享 `class_name` 字段，这样检索到 METHOD 时可以顺带把所属 CLASS chunk 一并取出作为上下文。

不过按 DevContext 定义文档第 18 节"State 只保存跨 Node 需要共享的数据"的原则，这个"类上下文补全"逻辑应放在 **Context Builder** 里，不要塞进 Retriever。

### 13.4 Javadoc / Comment 是否保留？

**判断：V1 保留，但分层处理。**

| 内容 | V1 处理 | 理由 |
| --- | --- | --- |
| **注解** | 已在 `content` 内（在 Range 内） | 语义价值高（`@Transactional` 直接说明行为） |
| **Javadoc** | **独立字段** `javadoc`，不并入 `content` | (a) 不在 Range 内，并入会破坏 citation 与 content 的一致性（见 7.7）；(b) Javadoc 常含 `@author`、`@since` 等噪声 |
| **行内注释** | 已在 `content` 内（在 Range 内） | 保留原始代码可读性 |
| **孤立注释** | 丢弃 | V1 不值得处理归属歧义 |

**为什么不把 Javadoc 并入 content**：这会引入一个隐蔽的不一致——`content` 包含 Javadoc 行，但 `start_line` 指向注解行。任何用行号核验 chunk 的机制（Evaluation 的 ground truth 比对、UI 跳转）都会错位。分字段存储没有这个风险，且 Python 侧组装 context 时可以自由决定是否拼接。

**Javadoc 的存储建议**：V1 只存 `getJavadocComment().map(JavadocComment::getContent)` 的原始文本（`Comment.getContent()`，`Comment.java:75`）。**不要**在 V1 用 `Javadoc.parse()` 做结构化（`NodeWithJavadoc.java:56`）——结构化在 P1 有明确用途时再做（例如单独检索 `@param` 描述）。

**一个实际的加分点**：my12306 这类项目的 Service 方法往往有中文 Javadoc 说明业务含义。Javadoc 对**语义检索**的帮助可能很大（自然语言描述 + 自然语言问题，embedding 匹配度高于纯代码）。这值得在 Evaluation 里作为一个实验变量：`含 Javadoc vs 不含 Javadoc`。

### 13.5 包的归属问题

`m.findAncestor(TypeDeclaration.class)` 取所属类，但**对于嵌套类中的方法，只会取到最内层的类**。DevContext 若需要外层类信息，需要 `findAncestor` 多次或遍历全部祖先。

V1 建议：`class_name` 只存最内层类名，`package_name` 存包名，不在 metadata 层做完整的嵌套路径。若将来需要，加一个 `class_path` 字段（形如 `Outer.Inner`）。

---

## 14. DevContext 借鉴什么

**1. 强类型 AST 节点 + 能力接口（mixin）的分层**
`getAnnotations()` 定义在 `BodyDeclaration` 上，所有 body declaration 统一可用；`getRange()` 定义在 `Node` 上，所有节点统一可用。这让 chunk 抽取器可以针对"能力"编程，而不是针对"节点类型"编程。DevContext 的 `CodeChunker` 应模仿这个结构：一个统一的 `extractMetadata(Node)` 处理通用能力，再按节点类型补差异。

**2. `CompilationUnit` 作为文件级根对象**
`getPackageDeclaration()` / `getTypes()` / `getImports()` 三个入口就把一个文件的结构说清了。DevContext 的 Java 解析 CLI 的入口就应该是 `JavaParser.parse(File) → ParseResult<CompilationUnit>`。

**3. `ParseResult` 而非异常：解析失败是常态**
`isSuccessful()` + `getProblems()` + `getResult()` 的组合鼓励"记录问题但继续"。对一个真实 repository，这比"抛异常就整文件跳过"务实得多。DevContext 的 ingestion 应该记录 `parse_problems` 并继续。

**4. `getFullyQualifiedName()` 处理嵌套类的成熟实现**
`TypeDeclaration.java:219-231` 已经正确处理了顶层/嵌套两种情形。DevContext 不必自己实现包名拼接。

**5. `getDeclarationAsString()` 直接给出可展示的签名**
这正是 chunk 的 `signature` 字段需要的形态，不需要自己拼参数列表。

**6. Position 1-based + Range 闭区间**
减少 off-by-one。DevContext 可全链路统一为 1-based。

**7. 官方测试作为行为契约**
`NodePositionTest` 断言"每个节点都必须有 Range"，这是 JavaParser 对 citation 场景的隐含承诺。DevContext 在写自己的 parser 测试时，可以照搬这个断言形式（对 my12306 的每个文件，断言所有 MethodDeclaration 都有 Range）。

**8. 注解判断的现成 API**
`isAnnotationPresent(String)` / `getAnnotationByName(String)`，不用自己写遍历。

**9. 只依赖 `javaparser-core` 的零依赖打包**
与 DevContext"Java Parser 作为独立 CLI，输出 JSON chunks"（定义文档第 25 节）的设计完全契合。

---

## 15. DevContext 不借鉴什么

**1. 不引入 `javaparser-symbol-solver-core`。**
不是因为它不好，而是它的前置条件（完整的 TypeSolver 配置，需要所有模块源码目录 + 依赖 jar）在 10～15 天内无法可靠完成，且引入 javassist/guava 依赖与 `UnsolvedSymbolException` 处理成本。V1 用 `javaparser-core` 足够。

**2. 不用 `toString()` 作为 chunk 的 content。**
它是 `PrettyPrintVisitor` 重新格式化的结果，与 `start_line`/`end_line` 指向的原始行不一致。用 Range 切片。

**3. 不用 `LexicalPreservingPrinter`。**
它解决的是"改 AST 后保留其余格式"的写场景问题。DevContext 是只读检索系统，用它是过度设计。

**4. 不用 `javaparser-core-serialization` 直接输出 JSON。**
它序列化的是 AST 节点树，而 DevContext 需要扁平的 chunk 列表。结构不匹配，直接用它只是把"抽字段"这一步推后，没有省事。

**5. 不用 `getSignature()` 作为展示用签名。**
它会 `stripGenerics` 和 `stripAnnotations`（`CallableDeclaration.java:327-345`），语义上丢了信息。用 `getDeclarationAsString()`。

**6. 不做 AST 改写 / 代码生成。**
DevContext 是 Search / Understand / Explain / Trace，不是 Edit / Run / Fix / Commit（定义文档第 30 节）。

**7. 不引入 tree-sitter 作为补充解析器。**
Java-only 场景下，JavaParser 的强类型 AST 严格优于 tree-sitter 的字符串节点。Continue 用 tree-sitter 是它支持多语言的必然代价，不构成 DevContext 的理由。

**8. 不实现 Continue 式的"AST 折叠"降级链。**
Continue 的 `collapseChildren`（`code.ts:26-99`）依赖 tree-sitter 可以按语法节点任意切片。JavaParser 没有"打印节点的一部分"的 API，硬做需要自己遍历 tokens，成本远高于收益。DevContext 的超长方法处理改用"截断 + 标记"（13.1），成本更低且不破坏 citation。

---

## 16. DevContext 如何简化实现

| JavaParser 能力 | 复杂度 | DevContext V1 决策 |
| --- | --- | --- |
| `StaticJavaParser`（静态、抛异常） | 低 | **不用**——用 `JavaParser` 实例 + `ParseResult` |
| `JavaParser` 实例（可复用、返回 ParseResult） | 低 | **采用**，全 repository 遍历复用一个实例 |
| `cu.findAll(MethodDeclaration.class)` | 极低 | **采用**——chunk 抽取主循环 |
| `findAncestor(TypeDeclaration.class)` | 极低 | **采用**——取所属类 |
| `getFullyQualifiedName()` | 极低 | **采用**（但仍存 package/class 两列，便于分别加权） |
| `getDeclarationAsString()` | 极低 | **采用**——`signature` 字段 |
| `getAnnotations()` + `getNameAsString()` | 极低 | **采用**——后缀匹配，防全限定名 |
| `getRange()` | 极低 | **采用**——`start_line` / `end_line`；`Optional` 守卫 |
| Range 切片取 content | 低（几行） | **采用** |
| `getJavadocComment()` | 低 | **采用**——独立字段 |
| Class 摘要 chunk（类签名 + 成员签名列表） | 中（约 20 行自实现） | **采用**（见 13.2） |
| `LexicalPreservingPrinter` | 高 | 不用 |
| `javaparser-core-serialization` | 中 | 不用，自己拼 chunk JSON |
| `SymbolSolver` + `TypeSolver` | 很高 | P2 |
| `getSignature()` / `toDescriptor()` | 低 | 不用 |
| `CompilationUnit.getImports()` | 低 | V1 可存可不存；若要"这个类依赖了谁"再存 |

**一句话总结简化方向**：JavaParser V1 只需要 `javaparser-core` 的约 **7 个 API**（`JavaParser.parse` / `findAll` / `findAncestor` / `getRange` / `getAnnotations` / `getDeclarationAsString` / `getJavadocComment`），加上一段自己写的 Range 切片。其余全部是 P1/P2。

---

## 17. 哪些功能放 P1 / P2

**P1（成本低、与检索质量直接相关）**

1. **超长方法的语句级二次切分**。`MethodDeclaration.getBody()` → `BlockStmt.getStatements()`，按语句 Range 累积切分，保留全部 class/method/line 元数据（13.1 方案 2）。
2. **Javadoc 结构化**。`getJavadoc().getBlockTags()`（`Javadoc.java:150`），可用于单独检索 `@param` 描述，或把 Javadoc 文本作为独立的可检索字段（13.4）。
3. **CLASS 摘要 chunk 的完善**。加入继承关系（`getExtendedTypes()` / `getImplementedTypes()`）与字段列表。

**P2（有价值但前置成本高）**

4. **SymbolSolver + `ReflectionTypeSolver` 单配**。只解析 JDK 类型，成本相对低，适合先验证价值。
5. **SymbolSolver + 完整 TypeSolver**（`CombinedTypeSolver` + 多模块 `JavaParserTypeSolver` + `JarTypeSolver`）。这是 V2 Symbol Resolution 的前提。
6. **Reference Resolution**（V3）。基于 5，回答"`purchaseTicket()` 在哪些地方被调用"。
7. **Call Graph**（V4）。基于 6。
8. **浅层继承图**。仅靠 AST 的 `getExtendedTypes()` / `getImplementedTypes()` 可做"本文件内"的继承关系；跨文件需要 5。
9. **`getCallablesWithSignature(Signature)`**（`TypeDeclaration.java:203`）——按签名找重载/覆写，用于"这个接口方法的实现类在哪"。

**明确不做**

- 完整类型推断、AST 改写、代码生成、tree-sitter 补充解析。

---

## 18. 结论速览

| DevContext 问题 | JavaParser 的答案 | 证据 | 决定 |
| --- | --- | --- | --- |
| 是否足以实现 AST-aware Code Chunk | 是，`javaparser-core` 足够 | A（源码）+ B（测试） | P0 采用 |
| 用什么依赖 | `javaparser-core`（**零运行时依赖**） | A（`javaparser-core/pom.xml`） | 只引入 core |
| 如何解析文件 | `new JavaParser(config).parse(file)` → `ParseResult<CompilationUnit>` | A（`JavaParser.java:179`） | 复用实例，不用 StaticJavaParser |
| 解析失败怎么办 | `isSuccessful()` / `getProblems()` / `getResult()` 分离 | A（`ParseResult.java:67-117`） | 记录 problem，尽力保留 chunk |
| 如何拿 package | `cu.getPackageDeclaration().map(PackageDeclaration::getNameAsString)` | A（`CompilationUnit.java:229`） | `Optional` 守卫（默认包） |
| 如何拿 className | `m.findAncestor(TypeDeclaration.class).map(TypeDeclaration::getNameAsString)`；或 `getFullyQualifiedName()` | A（`TypeDeclaration.java:168,219-231`） | 存 package / class 两列 |
| 如何拿 methodName | `m.getNameAsString()` | A（`CallableDeclaration.java:136` + `NodeWithName.java:48`） | `METHOD` / `CONSTRUCTOR` 分类型 |
| 如何拿 annotations | `m.getAnnotations().stream().map(AnnotationExpr::getNameAsString)` | A（`BodyDeclaration.java:73`） | 后缀匹配，防全限定名 |
| `@Transactional`/`@Service`/`@GetMapping`/`@FeignClient` 能否取得 | 能，四种注解形态全覆盖 | A（`AnnotationExpr` 三个子类） | 可行 |
| Method startLine / endLine 是否稳定 | 稳定；官方测试断言每个节点必须有 Range | A（`Node.java:259`）+ B（`NodePositionTest.java:86-98`） | 可行，须保持 `storeTokens=true` |
| Range 的语义 | Position 1-based；Range 闭区间 | A（`Position.java:29`、`Range.java:26`） | 直接用，不做 ±1 |
| 能否取完整 Method Source | 能，但 `toString()` 会重新格式化 | A（`Node.java:338`、`PrettyPrintVisitor.java:1092`） | **改用 Range 切片** |
| Annotation 是否在 Method Range 内 | 是（注解是方法的首个 token） | A（`setTokenRange` 逻辑 + `printMemberAnnotations`） | citation 自然覆盖注解行 |
| Javadoc 是否保留 | 保留，独立字段 | A（`Node.getComment()`、`NodeWithJavadoc.java:46`） | 分字段，结构化留 P1 |
| 超长 Method 如何切分 | JavaParser 无内置折叠能力 | A（无对应 API） | V1 截断 + 标记；P1 语句级切分 |
| Class Chunk 是否有必要 | 有必要，但须为摘要形态 | 判断 | METHOD 为主 + CLASS 摘要辅助 |
| SymbolSolver V1 是否做 | **不做** | A（TypeSolver 体系成本） | P2 |

**关键源码索引（后续开发回查用）**

```text
javaparser-core/src/main/java/com/github/javaparser/
  JavaParser.java                    解析入口（新 API，返回 ParseResult）
  StaticJavaParser.java              解析入口（旧 API，抛异常）—— 不用
  ParseResult.java                   isSuccessful / getProblems / getResult
  ParserConfiguration.java           storeTokens(:268) / attributeComments(:270) / languageLevel(:284)
  Position.java                      1-based 行/列（:29-40）
  Range.java                         闭区间（:26）；getLineCount(:251)；contains(:141)
  ast/Node.java                      getRange(:259) / setTokenRange(:266) / getComment(:251)
                                     findAll(:944) / walk(:926) / getOrphanComments(:435)
  ast/CompilationUnit.java           getPackageDeclaration(:229) / getTypes(:243) / getImports(:213)
  ast/PackageDeclaration.java        getName(:104)
  ast/Modifier.java                  enum Keyword(:100)
  ast/body/BodyDeclaration.java      getAnnotations(:73)
  ast/body/CallableDeclaration.java  getName(:136) / getParameters(:155) / getSignature(:318)
  ast/body/TypeDeclaration.java      getName(:168) / getMembers(:110) / getFullyQualifiedName(:219)
  ast/body/ClassOrInterfaceDeclaration.java  isInterface(:298) / getExtendedTypes(:278)
  ast/body/MethodDeclaration.java    getType(:228) / getBody(:205) / getDeclarationAsString(:280) / toDescriptor(:333)
  ast/body/ConstructorDeclaration.java
  ast/expr/AnnotationExpr.java       注解基类
  ast/expr/MarkerAnnotationExpr.java          @Override 形态
  ast/expr/SingleMemberAnnotationExpr.java    @GetMapping("/x") 形态
  ast/expr/NormalAnnotationExpr.java          @Transactional(readOnly=true) 形态
  ast/comments/Comment.java          getContent(:75) / isOrphan(:132)
  javadoc/Javadoc.java               getDescription(:143) / getBlockTags(:150) / toText(:89)
  printer/PrettyPrintVisitor.java    visit(MethodDeclaration)(:1092) —— 说明 toString 行为
  printer/lexicalpreservation/LexicalPreservingPrinter.java  setup(:110) / print(:724) —— 不用
  ast/nodeTypes/NodeWithAnnotations.java     isAnnotationPresent(:189) / getAnnotationByName(:209)
  ast/nodeTypes/NodeWithName.java            getNameAsString(:48)
  ast/nodeTypes/NodeWithDeclaration.java     getDeclarationAsString(:37)
  ast/nodeTypes/NodeWithRange.java           getRange / getBegin / getEnd / hasRange
  ast/nodeTypes/NodeWithJavadoc.java         getJavadocComment(:46) / getJavadoc(:56)
  utils/PositionUtils.java                   sortByBeginPosition(:44)

javaparser-core/src/main/javacc/java.jj    JavaCC 语法（AST 节点 Range 的真正来源）
  MethodDeclaration 产生式(:2254-2290)      begin = modifier.begin；注解在 Range 内
  Modifiers() 产生式(:1355-1401)            修饰符与注解统一记 begin(:1396)

javaparser-symbol-solver-core/src/main/java/com/github/javaparser/symbolsolver/
  JavaSymbolSolver.java              resolveDeclaration(:101) / calculateType(:499)
  resolution/typesolvers/            Reflection / JavaParser / Jar / Combined 等 —— P2

javaparser-core-testing/src/test/java/com/github/javaparser/
  ast/NodePositionTest.java          "There should be no node without a range"(:86-98)  ← 关键行为契约
  JavaParserTest.java                精确 Range 断言(:60-106, :205, :221)
  RangeTest.java                     Range 代数性质
```

**官方文档 URL（均来自仓库 readme 自身给出的链接，非二手来源）**

```text
http://javaparser.org
https://javaparser.org
https://github.com/javaparser/javaparser/wiki
https://github.com/javaparser/javaparser/wiki/Migration-Guide
https://github.com/javaparser/javaparser-maven-sample
https://github.com/javaparser/javasymbolsolver-maven-sample
```

---

## 19. 当前调研无法确认的事项

按调研需求第三十四节要求，以下无法从源码/官方资料确定，**不做推测**：

1. **在 my12306 真实代码上能否跑通。** 本次调研为静态源码分析，**未编译运行 JavaParser**（最小验证 Demo 已按要求取消）。所有结论属于"源码级论证 + 官方测试证据"，不是实测结论。**执行编码前建议先补一次真实文件验证。**

2. **`ParseResult` 在解析失败时是否仍返回可用的部分 AST。** 这是从 `isSuccessful()` 与 `getResult()` 相互独立的 API 形状推断的，源码中未找到明确语义说明，也未验证。

3. **`getRange()` 在真实 Maven 多模块项目（含 Lombok 生成代码、Java 21 特性）上的稳定性。** 官方测试覆盖的是小型代码片段，不覆盖 my12306 的具体语法组合。

4. **Java 21 特性的完整支持度。** `readme.md:20` 声称支持 Java 1.0–Java 25，但本次未逐项验证 record、sealed、pattern matching 等特性在 3.29.0-SNAPSHOT 上的解析表现。

5. **`ParserConfiguration.LanguageLevel.POPULAR` 具体对应哪个 Java 版本。** 默认值是 `POPULAR`（`ParserConfiguration.java:284`），但本次未查证其定义。若 my12306 用 Java 21，建议显式设置 `LanguageLevel.JAVA_21` 而非依赖默认值。

6. **Range 切片方式对 CRLF 换行文件的处理。** 源码中有 `LineSeparator` / `LineEndingProcessingProvider` / `setDetectOriginalLineSeparator`（`ParserConfiguration.java:518`）等机制，说明换行符处理是被认真对待的，但按 `"\n"` 切分是否会与 JavaParser 的行号计算产生偏差，本次未验证。建议使用 `detectOriginalLineSeparator(true)` 或读取时统一换行符。
