Markdown 解析比 Java 解析简单很多：它不使用 Markdown AST，而是一个“基于 ATX 标题的结构化行扫描器”。它先用 `#`～`######` 标题把文档划分成章节，再把超过 6000 个 Unicode 字符的章节按“段落 → 行 → 固定字符片段”逐级拆分，最终生成统一的 `DOCUMENT_SECTION` Chunk。

核心实现集中在 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:1)。

## 一、Markdown 解析在整体系统中的位置

整体链路是：

```text
Markdown 文档根目录
        │
        ▼
iter_markdown_files()
扫描所有 *.md，排除第三方目录
        │
        ▼
parse_markdown_file()
识别标题和章节层级
        │
        ▼
_Section
尚未超长拆分的逻辑章节
        │
        ▼
_split_section()
按 6000 字符上限拆分
        │
        ▼
Chunk(
    source_type="DOCUMENT",
    chunk_type="DOCUMENT_SECTION"
)
        │
        ▼
与 Java Code Chunk 合并
        │
        ▼
Embedding + PostgreSQL
```

Java Parser 通过独立 JVM 输出 JSONL，而 Markdown Parser 完全运行在 Python 进程内，直接返回 `list[Chunk]`。入口在 [pipeline.py](D:/Java-learning/DevContext/src/devcontext/ingestion/pipeline.py:22)：

```python
code_chunks = JavaParserRunner().parse(...)
document_chunks = parse_markdown_tree(...)
chunks = code_chunks + document_chunks
```

---

## 二、第一步：扫描 Markdown 文件

目录级入口是：

```python
parse_markdown_tree(root, repository, max_chars=6000)
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:164)。

它调用：

```python
iter_markdown_files(root)
```

扫描逻辑是：

```python
for path in sorted(root.rglob("*.md")):
    relative = path.relative_to(root)
    if not _is_excluded(relative):
        yield path
```

也就是说：

- 递归扫描文档根目录；
- 只查找 `*.md`；
- 文件按路径排序，保证每次处理顺序稳定；
- 数据库里保存相对文档根目录的路径；
- 不会把本机绝对路径存进 Chunk。

例如文档根目录是：

```text
D:\Java-learning\12306Project\docs
```

文件是：

```text
D:\Java-learning\12306Project\docs\5-后续开发规划\D4-设计分析.md
```

最终保存：

```text
file_path = 5-后续开发规划/D4-设计分析.md
```

路径分隔符通过 `as_posix()` 统一为 `/`。

### 排除哪些目录

如果相对路径任意一段满足以下条件，就不会解析：

```text
bower_components
node_modules
target
.git
.idea
```

或者目录名以：

```text
sbadmin2-
```

开头。

判断时会转成小写，因此目录大小写不影响过滤。

实现见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:23)。

这主要是为了排除项目中附带的第三方前端库 README，避免把 Bootstrap、SB Admin、Bower 包等第三方说明文档当成 `my12306` 的项目知识。

验证测试覆盖了：

- 正常项目 README 会进入索引；
- `bower_components` 下的 README 被排除；
- `sbadmin2-1.0.7` 下的 README 被排除。

测试见 [test_markdown_parser.py](D:/Java-learning/DevContext/tests/test_markdown_parser.py:35)。

---

## 三、第二步：读取 Markdown 文件

单文件入口是：

```python
parse_markdown_file(
    root,
    path,
    repository="my12306",
    max_chars=6000,
)
```

文件读取方式是：

```python
raw_lines = path.read_text(
    encoding="utf-8-sig",
    errors="replace",
).splitlines()
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:106)。

这里有三个行为需要注意。

### 1. 使用 `utf-8-sig`

`utf-8-sig` 可以自动去掉文件开头的 UTF-8 BOM。

否则第一行是标题时，BOM 可能出现在 `#` 前面，导致标题正则无法匹配。

### 2. 非法字符不会导致整个文档失败

使用了：

```python
errors="replace"
```

如果文件中存在非法 UTF-8 字节，会用 Unicode 替换字符 `�` 代替，而不是抛异常中止整个 ingestion。

好处是容错性强，代价是编码损坏可能被静默替换。

### 3. 换行符被统一处理

`splitlines()` 会处理：

- Windows `\r\n`
- Linux `\n`
- 旧式 `\r`

解析后只保留每一行的文本，不保留原来的换行符。后面重新拼接内容时统一使用 `\n`。

因此 Markdown Chunk 的内容不是“字节级原文”，但文本内容和行号仍然可以对应。

---

## 四、第三步：识别 Markdown 标题

标题正则定义为：

```python
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:11)。

能够识别：

```markdown
# 一级标题
## 二级标题
### 三级标题
#### 四级标题
##### 五级标题
###### 六级标题
```

标题必须满足：

- `#` 位于行首；
- `#` 数量是 1～6；
- `#` 后至少有一个空白字符；
- 标题后面存在内容。

例如：

```markdown
# 架构设计
```

可以识别。

但下面这些不会识别：

```markdown
###没有空格
    # 缩进标题
> # 引用块里的标题
标题
====
```

最后一个属于 Setext 风格标题，当前实现不支持。

### 标题行不会进入 `content`

当遇到标题行时，解析器会：

1. 结束前一个章节；
2. 更新标题层级栈；
3. 清空章节正文；
4. 把章节正文的起始行设置成“标题下一行”。

对应代码：

```python
finish()
level = len(match.group(1))
title = match.group(2).strip()
...
current_title = title
current_path = ...
current_lines = []
current_first_line = index + 1
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:126)。

所以对于：

```markdown
10: ## 订单模块
11: 订单模块负责创建订单。
12: 支付成功后更新订单状态。
```

生成的 Chunk 是：

```text
title       = 订单模块
heading_path= [...]
content     = 订单模块负责创建订单。
              支付成功后更新订单状态。
start_line  = 11
end_line    = 12
```

标题第 10 行不会进入 `content`，但会保存在 `title` 和 `heading_path` 中。

---

## 五、`heading_path` 是怎样维护的

解析器使用一个 `heading_stack` 保存当前标题层级：

```python
heading_stack: list[str] = []
```

遇到一个标题时，标题等级由 `#` 的数量决定：

```python
level = len(match.group(1))
```

然后执行：

```python
heading_stack[level - 1 :] = []

while len(heading_stack) < level - 1:
    heading_stack.append("")

heading_stack.append(title)
```

最后过滤掉空占位：

```python
current_path = [item for item in heading_stack if item]
```

### 正常层级下降

输入：

```markdown
# 系统设计
## 订单模块
### 事务边界
```

标题栈变化：

```text
# 系统设计
["系统设计"]

## 订单模块
["系统设计", "订单模块"]

### 事务边界
["系统设计", "订单模块", "事务边界"]
```

最后一个章节得到：

```python
heading_path = ["系统设计", "订单模块", "事务边界"]
title = "事务边界"
```

### 返回上级标题

输入：

```markdown
# 系统设计
## 订单模块
### 事务边界
## 支付模块
```

读到“支付模块”时，会删除二级及其以下的旧路径：

```text
原来：
["系统设计", "订单模块", "事务边界"]

现在：
["系统设计", "支付模块"]
```

因此不会错误地产生：

```text
["系统设计", "订单模块", "事务边界", "支付模块"]
```

### 标题跳级

输入：

```markdown
# 系统设计
### 事务边界
```

代码会为缺失的二级标题放一个空占位：

```text
["系统设计", "", "事务边界"]
```

然后过滤空值，最终得到：

```text
["系统设计", "事务边界"]
```

它不会拒绝不规范的标题层级，也不会在结果里保留“缺失了一级”这个事实。

### `title` 和 `heading_path` 的区别

对于：

```markdown
# 系统设计
## 订单模块
### 事务边界
```

“事务边界”章节：

```text
title = "事务边界"

heading_path = [
    "系统设计",
    "订单模块",
    "事务边界"
]
```

`title` 是当前叶子标题；`heading_path` 是完整上下文路径。

---

## 六、怎样形成一个 `_Section`

在正式生成 `Chunk` 前，解析器先生成内部数据对象：

```python
@dataclass(slots=True)
class _Section:
    title: str | None
    heading_path: list[str]
    lines: list[str]
    first_line: int
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:15)。

它代表“标题结构意义上的完整章节”，此时还没有考虑 6000 字符限制。

### 章节边界

任何新的 Markdown 标题都会结束当前章节，不论新标题是：

- 同级；
- 子级；
- 父级。

例如：

```markdown
# 订单系统
这里是订单系统概述。

## 创建订单
这里是创建订单的正文。

## 关闭订单
这里是关闭订单的正文。
```

会形成三个独立章节：

```text
章节 1
title = 订单系统
content = 这里是订单系统概述。

章节 2
title = 创建订单
content = 这里是创建订单的正文。

章节 3
title = 关闭订单
content = 这里是关闭订单的正文。
```

父章节不会递归包含子章节正文。也就是说，“订单系统”Chunk 不会重复包含“创建订单”和“关闭订单”的内容。

这种设计减少了父子章节之间的大面积内容重复。

### 空章节不产生 Chunk

`finish()` 只有在章节正文中至少存在一个非空白行时才会保存章节：

```python
if current_lines and any(line.strip() for line in current_lines):
    sections.append(...)
```

所以：

```markdown
# 订单模块
## 创建订单
创建订单正文
```

“订单模块”标题下面没有正文，不会产生独立 Chunk；只有“创建订单”产生 Chunk。

标题信息仍然可以通过子章节的 `heading_path` 保留下来：

```text
["订单模块", "创建订单"]
```

---

## 七、标题前的前言怎样处理

Markdown 文件可能在第一个标题前有正文：

```markdown
这是项目的总体介绍。

# 架构设计
架构正文。
```

解析器不会丢掉标题前的内容，而是把它保存成一个“前言章节”。

因为此时还没有标题：

```text
current_title = None
current_path  = []
current_first_line = 1
```

生成 Chunk 时：

```python
title = section.title or path.stem
```

所以如果文件名是：

```text
design.md
```

前言 Chunk 是：

```text
title        = design
heading_path = []
content      = 这是项目的总体介绍。
start_line   = 1
```

测试专门验证了这一行为，见 [test_markdown_parser.py](D:/Java-learning/DevContext/tests/test_markdown_parser.py:6)。

### 完全没有标题的文档

如果整个文件没有任何标题：

```markdown
这是一篇没有标题的说明文档。

第二段内容。
```

那么整个文件会被视为一个章节：

```text
title        = 文件名，不含 .md
heading_path = []
content      = 文档正文
```

如果正文超过 6000 字符，再进入超长拆分逻辑。

---

## 八、完整例子

假设 `design.md` 内容是：

```text
1  项目导言
2
3  # 架构设计
4  概述
5
6  ## 订单模块
7  事务边界说明
```

会先识别成三个 `_Section`。

### 第一个 Section：标题前言

```text
title        = None
heading_path = []
lines        = ["项目导言", ""]
first_line   = 1
```

生成 Chunk：

```text
title        = design
heading_path = []
content      = 项目导言
start_line   = 1
end_line     = 2
```

这里 `end_line=2` 是因为第 2 行空行被识别为段落结束行；虽然最终 `content.strip()` 去掉了尾部空白，但行号范围可能仍包含该空白分隔行。

### 第二个 Section：架构设计

```text
title        = 架构设计
heading_path = ["架构设计"]
lines        = ["概述", ""]
first_line   = 4
```

生成：

```text
title        = 架构设计
heading_path = ["架构设计"]
content      = 概述
start_line   = 4
end_line     = 5
```

### 第三个 Section：订单模块

```text
title        = 订单模块
heading_path = ["架构设计", "订单模块"]
lines        = ["事务边界说明"]
first_line   = 7
```

生成：

```text
title        = 订单模块
heading_path = ["架构设计", "订单模块"]
content      = 事务边界说明
start_line   = 7
end_line     = 7
```

这里可以看到：

- 标题行 3、6 不进入正文；
- 父标题通过 `heading_path` 继承；
- 正文行号仍然保留；
- 前言不会丢失。

---

## 九、超长章节怎样拆分

当前限制是：

```python
max_chars = 6000
```

架构决策明确是“最多 6000 个 Unicode 字符”，见 [architecture-decisions.md](D:/Java-learning/DevContext/docs/architecture-decisions.md:11)。

需要特别注意：

> 这里按 Python 字符串长度计算，不是按 Token，也不是按 UTF-8 字节数。

例如：

```text
"购票流程"
```

Python 中长度是 4；不会因为每个汉字的 UTF-8 编码占 3 字节而算成 12。

虽然早期规划文档提过 Token-aware Split，但当前代码实际实现的是字符数上限。

### 拆分优先级

```text
完整 Section
    │
    ├─ 不超过 6000
    │      └─ 一个 Chunk
    │
    └─ 超过 6000
           │
           ├─ 优先按段落边界组合
           │
           ├─ 单个段落仍过长：按行组合
           │
           └─ 单行仍过长：固定字符数硬切
```

实现见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:54)。

---

## 十、第一层拆分：按段落

段落由空白行分隔。

`_paragraph_ranges()` 逐行扫描，把遇到空白行时累积的内容保存为一个段落：

```python
if not line.strip():
    ranges.append((current, start, line_number))
    current = []
```

见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:37)。

例如：

```markdown
第一段第一行
第一段第二行

第二段第一行

第三段第一行
```

识别为：

```text
段落 1：第一段第一行 + 第一段第二行 + 后面的空白行
段落 2：第二段第一行 + 后面的空白行
段落 3：第三段第一行
```

拆分时并不是“一段一个 Chunk”，而是尽量把多个完整段落装进一个不超过 6000 字符的 Chunk。

例如三个段落长度分别是：

```text
2000
2500
3000
```

可能得到：

```text
Chunk 1 = 段落 1 + 段落 2，大约 4500 字符
Chunk 2 = 段落 3，大约 3000 字符
```

而不是固定生成三个 Chunk。

这样可以：

- 尽量保留段落完整性；
- 减少过碎的 Chunk；
- 避免在普通情况下切断一句话。

---

## 十一、第二层拆分：按行

如果单个段落本身已经超过 `max_chars`，就不能再保持整个段落。

这时解析器清空之前累积的普通段落，然后尝试按完整行组合：

```python
if line_buffer and len("\n".join(line_buffer + [line])) > max_chars:
    pieces.append(...)
    line_buffer = []
```

例如一个没有空行的大段落：

```text
第 1 行：2000 字符
第 2 行：2000 字符
第 3 行：2000 字符
第 4 行：2000 字符
```

在 6000 字符限制下，大致会形成：

```text
Chunk 1：第 1～2 行，或者第 1～3 行，取决于换行符后的实际长度
Chunk 2：剩余行
```

每个 Chunk 会保存自己的 `start_line` 和 `end_line`。

---

## 十二、第三层拆分：超长单行硬切

如果某一行自身就超过 6000 字符，例如：

```markdown
{"一个非常长、完全不换行的 JSON 或压缩内容": "..."}
```

解析器执行：

```python
while len(line) > max_chars:
    pieces.append((line[:max_chars], line_number, line_number))
    line = line[max_chars:]
```

也就是说，把同一行切成多个固定字符片段：

```text
第 1 片：字符 0～5999
第 2 片：字符 6000～11999
第 3 片：剩余字符
```

这些 Chunk 会拥有相同的：

```text
start_line = 原始行号
end_line   = 原始行号
```

例如一条 15000 字符的第 20 行，可能产生：

```text
Chunk A：start_line=20, end_line=20
Chunk B：start_line=20, end_line=20
Chunk C：start_line=20, end_line=20
```

这也是数据库不能对：

```text
repository + file_path + start_line + end_line
```

施加唯一约束的原因：多个合法 Chunk 可以指向同一源码行。相关决策见 [architecture-decisions.md](D:/Java-learning/DevContext/docs/architecture-decisions.md:15)。

### 没有重叠窗口

当前拆分没有 overlap：

```text
Chunk A 结束后
Chunk B 从下一个字符/段落直接开始
```

它不会把前一个 Chunk 的结尾重复一部分到后一个 Chunk 中。

优点是没有重复文本；缺点是如果语义刚好跨越拆分边界，两边的语义联系可能变弱。

---

## 十三、一个章节拆成多个 Chunk 后保留什么

假设：

```markdown
# 事务边界调整
```

下面正文太长，被拆成 3 个 Chunk。

三个 Chunk 都会共享：

```text
repository
source_type
chunk_type
file_path
title
heading_path
```

不同的是：

```text
content
start_line
end_line
content_hash
```

例如：

```text
Chunk 1
title        = 事务边界调整
heading_path = ["购票优化", "事务边界调整"]
start_line   = 120
end_line     = 168

Chunk 2
title        = 事务边界调整
heading_path = ["购票优化", "事务边界调整"]
start_line   = 169
end_line     = 221

Chunk 3
title        = 事务边界调整
heading_path = ["购票优化", "事务边界调整"]
start_line   = 222
end_line     = 245
```

当前没有额外保存：

```text
part_index
part_count
parent_section_id
```

所以只能通过相同的文件、标题路径和相邻行号判断它们来自同一个逻辑章节。

---

## 十四、最终生成哪些字段

每个 Markdown Chunk 都使用统一的 Python `Chunk` 模型，见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:8)。

Markdown Parser 主动设置：

| 字段 | 内容 |
|---|---|
| `repository` | 默认 `my12306`，由调用方传入 |
| `source_type` | 固定为 `DOCUMENT` |
| `chunk_type` | 固定为 `DOCUMENT_SECTION` |
| `file_path` | 相对文档根目录的路径 |
| `title` | 当前章节标题；前言/无标题文档使用文件名 |
| `heading_path` | 从顶级标题到当前标题的完整路径 |
| `content` | 当前章节或章节子片段正文 |
| `start_line` | 正文片段起始行，1-based |
| `end_line` | 正文片段结束行，1-based |

下面这些 Java 专属字段使用默认空值：

```text
module       = None
package_name = None
class_name   = None
symbol_name  = None
signature    = None
annotations  = []
javadoc      = None
```

`content_hash` 没有在 Markdown Parser 中显式填写，而是由 `Chunk.__post_init__()` 自动计算：

```python
sha256(content.encode("utf-8")).hexdigest()
```

实际构造代码见 [markdown_parser.py](D:/Java-learning/DevContext/src/devcontext/ingestion/markdown_parser.py:144)。

---

## 十五、Markdown 内容怎样参与检索

### 1. 关键词检索文本

`keyword_text()` 会拼接：

```text
title
heading_path
file_path
content
```

对 Markdown Chunk 来说，大致是：

```text
事务边界调整
购票优化 / 事务边界调整
5-后续开发规划/D4-设计分析.md
正文内容……
```

所以关键词检索能够命中：

- 当前标题；
- 父标题；
- 文件名或目录名；
- 正文关键词。

对应实现见 [models.py](D:/Java-learning/DevContext/src/devcontext/models.py:40)。

### 2. 向量检索文本

`embedding_text()` 的逻辑是：

```python
identity = signature or symbol_name or title or heading
values = [identity, javadoc, content]
```

对于 Markdown：

```text
signature   = None
symbol_name = None
title       = 一定有值
```

所以实际 Embedding 输入是：

```text
当前章节 title

正文 content
```

一个重要限制是：

> `heading_path` 的父标题当前不会进入 Embedding。

例如：

```text
heading_path = ["购票优化", "事务边界", "方案对比"]
title        = "方案对比"
```

Embedding 看到的是：

```text
方案对比

正文……
```

但看不到：

```text
购票优化 / 事务边界
```

完整 `heading_path` 只进入 `keyword_text`。

这会影响标题很泛的章节，例如很多文件都有：

```markdown
## 方案对比
## 实验结果
## 问题分析
```

只把“方案对比”送入 Embedding，区分能力不如把完整路径一起送进去。这是当前数据模型中一个明确的检索质量改进点。

---

## 十六、与 Java 解析最主要的区别

| 维度 | Java | Markdown |
|---|---|---|
| 解析方式 | JavaParser AST | 标题正则 + 行扫描 |
| 主要边界 | 类、接口、方法、构造器 | `#`～`######` 标题 |
| Chunk 类型 | CLASS、INTERFACE、METHOD、CONSTRUCTOR | DOCUMENT_SECTION |
| 长内容处理 | 当前一个方法一个 Chunk | 段落→行→字符三级拆分 |
| 标题/签名 | `signature` | `title` + `heading_path` |
| 原文保持 | 方法/构造器精确源码切片 | 换行统一，首尾空白会被 `strip()` |
| Citation | 文件 + 精确代码行 | 文件 + 标题路径 +正文行号 |
| 运行方式 | 独立 Java CLI，输出 JSONL | Python 进程内直接返回 Chunk |
| 语法完整性 | 真正 Java AST | 不是完整 CommonMark AST |

---

## 十七、当前实现的限制

### 1. 不是完整 Markdown Parser

当前只识别行首 ATX 标题：

```markdown
# 标题
```

不识别：

- Setext 标题；
- Markdown AST；
- HTML section；
- YAML front matter；
- 文档链接关系；
- 引用关系；
- 表格结构；
- 列表层级；
- admonition/callout；
- Mermaid 图；
- Markdown include。

除标题外，表格、列表、代码块等都只是普通正文字符串。

### 2. 不理解 fenced code block

这是当前实现最明显的结构风险。

例如：

````markdown
```markdown
# 这只是示例，不是真的文档标题
```
````

解析器没有维护“当前是否位于代码围栏内”的状态，所以代码块内部这一行：

```markdown
# 这只是示例，不是真的文档标题
```

仍可能被误识别为真实章节标题。

因此当前实现更准确地说是：

> 基于行首标题正则的 Heading-aware Splitter，而不是符合完整 CommonMark 规则的 Markdown Parser。

### 3. 标题本身不在 `content`

标题保存在 Metadata 中，但不进入正文。

这本身没问题，因为检索文本会另外拼接 `title`；不过如果后续某个组件只读取 `content`，不读取 Metadata，它会看不到章节标题。

### 4. 行号范围可能包含空白分隔行

段落遇到空行时，空行会被放进该段落的行号范围；最终正文通过 `.strip()` 去掉尾部空白。

因此可能出现：

```text
content 最后一个可见字符在第 20 行
end_line = 21
第 21 行其实是空白段落分隔行
```

这不会影响定位章节，但 Markdown 行号的“逐字精确性”不如 Java 方法切片严格。

### 5. 超长单行的多个 Chunk 行号相同

多个子片段都可能拥有：

```text
start_line = end_line = 同一行
```

当前没有字符列号或子片段序号，所以仅靠行号不能区分它们在该行中的具体位置。

### 6. 没有 Chunk overlap

跨边界的上下文可能被拆开，尤其是在：

- 一个超长无空行段落；
- 一行超长 JSON；
- 大型 Markdown 表格；
- 大型代码块。

### 7. 空文档和只有标题的文档不产生 Chunk

如果文件内容为空，或者只有标题、没有正文：

```markdown
# 标题
```

则不会产生任何 `DOCUMENT_SECTION`。

标题不会作为独立 Chunk 保存。

### 8. 编码错误被替换而不是显式报告

`errors="replace"` 保证 ingestion 不因单个错误字符失败，但也意味着不会形成编码错误报告。

---

## 十八、当前真实解析结果

当前验证报告记录的 Markdown 结果是：

```text
Markdown 文件：57
文档 Chunk：   2363
```

并且：

```text
bower_components 入库数：0
sbadmin2-* 入库数：      0
```

见 [verification.md](D:/Java-learning/DevContext/docs/verification.md:15)。

57 个文档产生 2363 个 Chunk，平均每个文件约 41 个 Chunk。数量较多的主要原因是：

- 每个标题章节独立成 Chunk；
- 长章节会继续按 6000 字符拆分；
- 项目设计文档本身标题层级较细；
- 同一个逻辑 Section 可能产生多个物理 Chunk。

---

## 总结

Markdown 的解析过程可以压缩成一句话：

> 递归扫描并排除第三方 Markdown，按 `#`～`######` 标题维护层级栈，把标题间正文形成 `_Section`，再按 6000 字符上限依次采用段落、行和固定字符切分，最后生成带 `title`、完整 `heading_path`、文件路径和正文行号的 `DOCUMENT_SECTION` Chunk。

它最核心的价值是保留文档结构：

```text
文件
└── 一级标题
    └── 二级标题
        └── 当前章节
```

而不是把整篇设计文档机械地每 1000 字符切一刀。

当前实现轻量、可解释、足以支撑 `my12306` 的设计文档检索；但它不是完整 Markdown AST 解析器，代码围栏内的伪标题、完整标题路径未进入 Embedding、没有 overlap 和字符列定位，是后续最值得关注的几个改进点。