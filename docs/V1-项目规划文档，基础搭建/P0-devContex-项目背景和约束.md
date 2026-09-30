# DevContext-Java P0 项目定义

> 项目阶段：P0 · Project Definition
> 项目性质：个人学习项目 / 简历项目 / AI 应用工程实践
> 预计核心开发周期：10～15 天
> 第一实验对象：`my12306` Java 微服务项目
> 工作名称：DevContext-Java
> 英文定位：Agentic Context Retrieval for Java Projects

---

# 1. 文档目的

本文不是具体功能开发文档，而是 DevContext-Java 项目的最高层项目定义。

后续所有 AI Agent 在参与以下工作前，应优先阅读本文：

* 项目架构设计；
* 数据库设计；
* 模块划分；
* API 设计；
* JavaParser 代码解析模块开发；
* RAG / Retrieval 开发；
* LangGraph 工作流设计；
* Evaluation 测试；
* README 和面试材料生成；
* 项目优化与技术选型讨论。

后续开发如果出现“增加新技术”“增加新功能”“修改架构”等行为，必须首先判断：

1. 是否符合本文定义的项目核心目标；
2. 是否能在 10～15 天项目周期内完成；
3. 是否提高 Retrieval / Context Engineering / Evaluation 的学习价值；
4. 是否只是为了增加技术栈而增加复杂度。

如果不满足，应默认拒绝加入 MVP。

---

# 2. 项目背景

## 2.1 前置项目背景

开发者已经完成或正在完成一个简化版 12306 项目 `my12306`。

该项目采用 Java 21、Spring Boot、Spring Cloud、MySQL、Redis、Redisson、ShardingSphere 等技术，包含：

* 用户服务；
* 票务服务；
* 订单服务；
* 支付服务；
* API Gateway；
* 注册登录；
* 车票查询；
* 购票占座；
* 下单；
* 支付；
* 超时关单；
* 分布式锁；
* Redis + Lua；
* 分库分表；
* 状态机；
* 缓存；
* 性能压测。

第一项目主要用于学习：

**Java Backend Engineering / Distributed System Engineering。**

随着项目深入，项目内部逐渐产生三类信息：

### 第一类：Implementation

存在于 Java / Lua / 配置代码中。

例如：

* `purchaseTicket()` 在哪里；
* Token Bucket 如何调用；
* 延迟队列如何工作；
* Feign 调用了什么服务。

### 第二类：Design Decision

存在于 Markdown 技术设计文档中。

例如：

* 为什么使用 Token Bucket；
* 为什么没有采用 Seata；
* 为什么不使用 Canal；
* 为什么调整事务边界。

### 第三类：Experiment / Evidence

存在于测试与性能分析文档中。

例如：

* 修改前后 P99；
* SQL 数量变化；
* 座位售出率；
* 不同设计方案 A/B/C 的测试结果。

因此，同一个技术问题往往不能只查看代码，也不能只查看文档。

例如：

> “为什么需要把 Feign 调用移出购票事务？”

完整回答至少需要：

```text
Java implementation
        +
Design documentation
        +
Performance experiment
```

这构成 DevContext-Java 的直接项目背景。

---

# 3. 项目产生原因

DevContext-Java 不以“替代 Cursor、Codex、Claude Code 等成熟 Coding Agent”为目标。

当前成熟 Coding Agent 已经能够：

* 搜索代码；
* 读取文件；
* 理解项目；
* 修改代码；
* 调用终端；
* 自动执行多步任务。

因此，本项目不存在以下假设：

> “现有 AI 无法理解代码，所以需要重新开发一个代码助手。”

本项目开发原因是：

> **为了学习和实现 Coding Agent 背后的 Context Engineering 与 Retrieval 核心机制。**

LLM 本身并不知道一个本地 Java 项目。

真正进入模型 Context Window 的信息必须经历：

```text
Repository

→ Parse

→ Chunk

→ Index

→ Retrieve

→ Rank

→ Select Context

→ LLM
```

成熟 Agent 已经封装了这个过程。

DevContext-Java 的学习目标是将这个过程部分拆开、亲手实现并进行量化评测。

---

# 4. 项目核心定位

DevContext-Java 定义为：

> 一个面向 Java 项目的代码与设计知识混合 Context Retrieval 系统。

系统通过对：

```text
Java Source Code
+
Markdown Project Documents
```

建立不同的结构化索引，使 AI 能够根据问题性质选择：

```text
Document Retrieval

Code Retrieval

Mixed Retrieval
```

最终回答：

### What

“什么东西在哪里？”

例如：

> 购票入口代码在哪里？

主要依赖：

```text
CODE
```

### Why

“为什么这样设计？”

例如：

> 为什么购票之前使用 Token Bucket？

主要依赖：

```text
DOCUMENT
```

### Why + How

“为什么这样设计，并且如何实现？”

例如：

> 为什么 Feign 调用要移出事务？涉及哪些代码？

依赖：

```text
DOCUMENT
+
CODE
```

这类问题称为：

```text
MIXED
```

MIXED Retrieval 是项目最重要的应用场景。

---

# 5. 项目核心问题

整个项目只围绕一个核心问题：

> **如何为 LLM 找到正确、完整、可验证的项目 Context？**

进一步分成五个子问题。

## 5.1 Parsing

不同数据如何解析？

```text
Markdown
→ Heading-aware Parsing

Java
→ AST-aware Parsing
```

---

## 5.2 Chunking

如何避免破坏原始语义结构？

Markdown 应尽可能保留：

```text
Title
Section
Subsection
Paragraph
```

Java 应尽可能保留：

```text
Package
Class
Interface
Method
Annotation
```

不能简单把 Java 源码每 1000 字符切割一次。

---

## 5.3 Retrieval

不同查询适合不同检索方式。

例如：

```text
RDelayedQueue
```

属于非常明确的 Symbol。

Keyword Search 很有效。

而：

> “系统如何实现订单自动关闭？”

则更加适合 Semantic Vector Search。

因此系统采用：

```text
Keyword Retrieval
+
Vector Retrieval
```

---

## 5.4 Context Fusion

来自：

```text
Document Retriever
Code Retriever
Keyword Retriever
Vector Retriever
```

的结果如何组合？

MVP 使用：

```text
RRF
Reciprocal Rank Fusion
```

而不是直接比较不同 Retriever 的原始 Score。

---

## 5.5 Evaluation

如何证明 Retrieval 优化有效？

不能使用：

> “感觉回答更准确了。”

必须使用：

```text
Recall@K
MRR
Latency
Citation Accuracy
```

进行实验。

---

# 6. 项目核心学习目标

完成 DevContext-Java 后，开发者应能够真正解释：

### RAG

不仅知道：

```text
Embedding
→ Vector Search
→ LLM
```

还理解：

```text
Parsing
Chunking
Metadata
Index
Keyword Retrieval
Vector Retrieval
Hybrid Retrieval
Rank Fusion
Rerank
Context Selection
Citation
Evaluation
```

### LangChain

掌握其作为：

```text
Model
Embedding
Prompt
Retriever
Output Parser
```

等 AI Application Building Block 的角色。

LangChain 不是项目架构中心。

---

### LangGraph

掌握：

```text
State
Node
Edge
Conditional Edge
Cycle
Retry
```

以及为什么：

```text
固定 Pipeline
```

可以使用 LCEL，

但：

```text
Routing
+
State
+
Retry
+
Loop
```

更适合 LangGraph。

---

### Context Engineering

理解：

> AI Agent 效果不仅取决于模型能力，也高度取决于进入模型上下文的信息质量。

DevContext-Java 最终真正学习的主题是：

**Context Engineering。**

---

# 7. MVP 系统架构

第一版本整体流程：

```text
                    Java Repository

                         │

            ┌────────────┴────────────┐

            │                         │

      Markdown Docs               Java Source

            │                         │

     Markdown Parser              JavaParser

            │                         │

 Heading-aware Chunk          AST-aware Chunk

            │                 Class / Method

            │                         │

            └──────────┬──────────────┘

                       │

                   Embedding

                       │

               PostgreSQL

                 pgvector

                       +

                Full Text Search


================================================


                    User Query

                        │

                        ▼

                 LangGraph Router

                 /      |      \

               DOC     CODE    MIXED

                │       │        │

         Doc Retrieval │   Doc + Code

                        │

                 Code Retrieval

                \       │       /

                  Context Results

                        │

                Vector + Keyword

                        │

                       RRF

                        │

                [Optional Rerank]

                        │

                 Context Builder

                        │

               Context Evaluation

                   /          \

                Enough     Insufficient

                  │             │

                  │        Query Rewrite

                  │             │

                  └──────◄──────┘

                        │

                       LLM

                        │

                        ▼

              Answer + Citations
```

---

# 8. 数据源定义

P0 阶段只支持两个主要数据源。

## 8.1 Markdown

扩展名：

```text
.md
```

用于：

* README；
* 系统设计；
* 技术方案；
* 性能分析；
* Bug 分析；
* 开发规划。

---

## 8.2 Java Source

扩展名：

```text
.java
```

用于：

* Controller；
* Service；
* Mapper；
* DTO；
* Handler；
* Filter；
* Configuration；
* Utility；
* Domain Model。

---

第一版本暂不重点支持：

```text
PDF
Word
HTML
Python
Go
C++
JavaScript
```

如果项目需要，可以保留 `.yml`、`.xml`、`.lua` 的简单文本检索扩展能力，但不得影响 MVP 主线。

---

# 9. Markdown Chunk 设计

Markdown 不使用完全固定字符切割。

优先根据：

```text
# Heading
## Heading
### Heading
```

识别文档结构。

每一个 Document Chunk 至少保存：

```text
repository
file_path
document_title
heading_path
content
start_line
end_line
chunk_type
```

其中：

```text
chunk_type = DOCUMENT_SECTION
```

如果 Section 超过 Token 限制，可以再次进行 Token-aware Split。

---

# 10. Java Chunk 设计

Java 使用 JavaParser 构建 AST。

第一版本重点识别：

```text
CompilationUnit

Package

Class

Interface

Method

Constructor

Annotation
```

核心 Chunk 粒度：

```text
CLASS
METHOD
```

重点优先 METHOD。

每个 Code Chunk 至少保留：

```text
repository

module

file_path

package_name

class_name

method_name

chunk_type

annotations

content

start_line

end_line
```

例如：

```text
repository:
my12306

module:
ticket-service

package:
com.xxx.ticket.service

class:
TicketServiceImpl

method:
purchaseTicket

chunk_type:
METHOD

annotations:
@Transactional

file:
...

start_line:
120

end_line:
188
```

---

# 11. 第一版明确不实现完整代码语义图

第一版本不建立完整：

```text
Call Graph

Inheritance Graph

Reference Graph
```

原因：

10～15 天开发周期中，这些功能的实现和调试成本过高。

项目后续可演进：

```text
V1
AST Chunk

↓

V2
Symbol Resolution

↓

V3
Reference

↓

V4
Call Graph
```

第一版本只需要证明：

> Structure-aware Code Chunk 能够比普通 Text Chunk 更合理地组织代码知识。

---

# 12. 数据存储

MVP 统一使用：

```text
PostgreSQL
+
pgvector
```

主要原因：

一套数据库即可承载：

```text
Metadata

Vector

Keyword Search

Evaluation Data
```

避免在 10～15 天内同时维护：

```text
MySQL
+
Milvus
+
Elasticsearch
+
Redis
```

等多个基础设施。

---

# 13. 核心数据表初步设计

P0 只定义概念，不要求立即确定全部 SQL。

核心实体：

## repository

表示一个已经索引的项目。

主要字段：

```text
id
name
root_path
language
status
created_at
updated_at
```

---

## knowledge_chunk

统一存储 Doc Chunk 与 Code Chunk。

主要字段：

```text
id

repository_id

source_type

chunk_type

file_path

module

package_name

class_name

method_name

heading_path

content

content_hash

start_line

end_line

embedding

created_at
updated_at
```

`source_type`：

```text
DOCUMENT
CODE
```

`chunk_type`：

```text
DOCUMENT_SECTION
CLASS
METHOD
```

---

## evaluation_case

用于 Benchmark。

```text
id

question

query_type

expected_sources

expected_chunks

tags
```

---

## evaluation_result

用于保存实验结果。

```text
case_id

strategy

top_k

retrieved_chunks

latency

hit

reciprocal_rank
```

---

# 14. Retrieval Strategy

第一版本必须支持三种策略。

## Vector Only

```text
Query
→ Embedding
→ Vector Search
```

用途：

建立 Baseline。

---

## Keyword Only

基于 PostgreSQL Full Text Search 或基础 Keyword Search。

用途：

测试：

```text
class name
method name
framework symbol
constant name
```

等查询。

---

## Hybrid

```text
Vector Ranking
+
Keyword Ranking
        ↓
       RRF
        ↓
      Final Ranking
```

Hybrid 是最终默认方案。

---

# 15. 为什么使用 RRF

Vector Search 与 Keyword Search 的原始 Score 不在统一评分空间。

例如：

```text
Vector similarity = 0.86

BM25 score = 7.2
```

二者不能直接比较。

RRF 通过：

```text
Rank
```

而不是原始 Score 融合多个 Retriever。

因此能够低成本实现：

```text
Semantic Retrieval
+
Exact Retrieval
```

结果融合。

RRF 是 DevContext-Java 第一版必须掌握并实现的算法之一。

---

# 16. Query Type

系统定义：

```text
DOC

CODE

MIXED
```

### DOC

主要询问：

```text
Why
Design
Architecture
Trade-off
Experiment
```

例如：

> 为什么没有使用 Seata？

---

### CODE

主要询问：

```text
Where
Implementation
Class
Method
API
```

例如：

> Token Bucket 在哪里调用？

---

### MIXED

同时涉及：

```text
Why
+
How
```

例如：

> 为什么把 Feign 移出事务？具体改了哪些代码？

MIXED 是项目主要研究对象。

---

# 17. LangGraph 初始工作流

第一版本采用：

```text
START

  ↓

analyze_query

  ↓

DOC / CODE / MIXED

  ↓

retrieve

  ↓

evaluate_context

 /              \

ENOUGH        INSUFFICIENT

  │                │

  │          rewrite_query

  │                │

  │             retrieve

  │                │

  └───────◄────────┘

          │

   generate_answer

          │

         END
```

---

# 18. LangGraph State 初步定义

State 只保存跨 Node 需要共享的数据。

初步包含：

```text
question

rewritten_query

query_type

doc_results

code_results

merged_results

context

context_quality

retry_count

answer

citations
```

不得将所有局部临时变量写入 State。

---

# 19. Retry 限制

Agentic Retrieval 不允许无限循环。

第一版设置：

```text
MAX_RETRY = 2
```

如果两次重写后仍无法获得足够 Context：

系统应明确返回：

> 当前索引内容不足以可靠回答该问题。

而不是继续让模型推测。

---

# 20. Citation

回答必须尽可能携带来源。

Java Citation：

```text
TicketService.java
Lines 120-188
```

Document Citation：

```text
D4-设计分析.md
Section: 事务边界调整
```

Citation 的目标：

1. 用户能够验证答案；
2. Evaluation 可以分析 Retrieval；
3. 减少无法验证的模型生成。

---

# 21. Evaluation 是强制模块

本项目不得在没有 Evaluation Dataset 的情况下宣称：

```text
Hybrid Search improves accuracy
```

或者：

```text
Rerank improves retrieval
```

所有优化结论必须来自实验。

---

# 22. Evaluation Dataset

目标构建：

```text
30～50 questions
```

建议：

```text
DOC      10～15

CODE     10～15

MIXED    15～20
```

其中 MIXED 应占较高比例。

---

# 23. Evaluation 指标

核心：

```text
Recall@3

Recall@5

MRR
```

工程指标：

```text
Retrieval Latency

Total Latency
```

可补充：

```text
Citation Accuracy
```

回答质量可以人工评测，不要求第一版实现复杂 LLM-as-Judge。

---

# 24. 核心 A/B 实验

至少执行三个实验组：

```text
A:
Vector Only

B:
Keyword Only

C:
Vector + Keyword + RRF
```

如果开发周期允许：

```text
D:
Hybrid + Rerank
```

最终生成类似：

| Strategy        | Recall@3 | Recall@5 | MRR | P95 |
| --------------- | -------: | -------: | --: | --: |
| Vector          |       实测 |       实测 |  实测 |  实测 |
| Keyword         |       实测 |       实测 |  实测 |  实测 |
| Hybrid          |       实测 |       实测 |  实测 |  实测 |
| Hybrid + Rerank |       实测 |       实测 |  实测 |  实测 |

不得提前编造实验结果。

---

# 25. 核心技术栈

## Python

负责：

```text
FastAPI

LangChain

LangGraph

Embedding

Retrieval

RRF

Evaluation

LLM integration
```

---

## Java

负责：

```text
JavaParser

AST Extraction
```

Java Parser 可以是：

```text
独立 CLI
```

输入：

```text
repository path
```

输出：

```text
JSON chunks
```

Python Ingestion 模块读取 JSON 后入库。

该设计可以显著降低 Java 与 Python 系统集成成本。

---

## PostgreSQL

负责：

```text
Metadata Storage

Vector Storage

Full Text Search

Evaluation Storage
```

---

# 26. 推荐项目目录初稿

```text
devcontext-java/

├── README.md

├── docs/
│   ├── 01-project-definition.md
│   ├── 02-architecture.md
│   ├── 03-retrieval-design.md
│   ├── 04-evaluation-design.md
│   └── 05-development-log.md

├── java-parser/
│   ├── pom.xml
│   └── src/

├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── graph/
│   │   ├── ingestion/
│   │   ├── retrieval/
│   │   ├── ranking/
│   │   ├── generation/
│   │   ├── evaluation/
│   │   ├── models/
│   │   └── db/
│   │
│   └── tests/

├── evaluation/
│   ├── dataset.json
│   └── results/

├── scripts/

├── docker-compose.yml

└── .env.example
```

目录后续可以调整，但模块职责不得混乱。

---

# 27. P0 核心功能范围

MVP 必须完成：

1. 导入一个 Java Repository；
2. Markdown Heading Chunk；
3. Java AST Class / Method Chunk；
4. Embedding；
5. PostgreSQL + pgvector；
6. Vector Retrieval；
7. Keyword Retrieval；
8. RRF；
9. DOC / CODE / MIXED Router；
10. LangGraph Workflow；
11. Context Evaluation；
12. Query Rewrite + Retry；
13. Answer Citation；
14. Evaluation Dataset；
15. Recall@K / MRR；
16. Docker Compose 基础启动。

---

# 28. P1 可选功能

只有 MVP 完成后才能考虑：

```text
Cross Encoder Rerank

Git Incremental Index

Simple Web UI

Streaming Response

Tracing
```

优先级建议：

```text
Rerank

>

Incremental Index

>

UI / Streaming
```

---

# 29. Non-goals

P0 / V1 明确不做：

```text
Automatic Code Editing

Automatic Shell Execution

Git Commit

GitHub PR Agent

Multi-Agent

MCP

GraphRAG

Complete Call Graph

Multi-language Support

Kubernetes

Microservices

Complex RBAC

Fine-tuning
```

后续 AI 不应主动添加这些功能。

---

# 30. 为什么暂时不做 Coding Agent

Coding Agent 通常需要：

```text
read

search

edit

terminal

test

debug

git

planning
```

这会使 10～15 天项目完全偏离：

```text
Retrieval
+
Context Engineering
```

核心目标。

因此 DevContext-Java 的能力边界为：

```text
Search

Understand

Explain

Trace
```

而不是：

```text
Edit

Run

Fix

Commit
```

---

# 31. 项目最重要的三个技术亮点

最终简历和面试主要讲三点。

## Highlight 1：Structure-aware Indexing

Markdown：

```text
Heading-aware Chunk
```

Java：

```text
AST Class / Method Chunk
```

解决：

> 固定字符 Chunk 破坏结构语义的问题。

---

## Highlight 2：Hybrid Context Retrieval

实现：

```text
Keyword
+
Vector
+
RRF
```

解决：

> Symbol 精确检索和自然语言语义检索各有所长的问题。

---

## Highlight 3：Evaluation-driven Agentic Retrieval

使用 LangGraph 实现：

```text
Route

Retrieve

Evaluate

Rewrite

Retry
```

并使用：

```text
Recall@K

MRR

Latency
```

评测 Retrieval。

解决：

> RAG 优化仅靠主观感觉的问题。

---

# 32. 10～15 天开发节奏

核心阶段：

```text
Day 1
P0设计 + Evaluation题目初稿

Day 2
Markdown Ingestion

Day 3
JavaParser AST

Day 4
Code Index + Vector Search

Day 5
Keyword Search

Day 6
RRF Hybrid Retrieval

Day 7
LangGraph Router

Day 8
Mixed Retrieval + Citation

Day 9
Context Evaluation + Rewrite + Retry

Day 10
Evaluation

Day 11
根据数据完成一次真实优化

Day 12
README / Docker / Architecture / 面试文档
```

扩展：

```text
Day 13
Rerank

Day 14
Incremental Index

Day 15
部署 / Demo / 面试准备
```

---

# 33. 开发原则

后续所有 AI Agent 必须遵循：

### 原则一

优先保证：

```text
完整主链路
```

而不是增加功能数量。

### 原则二

每一个引入的技术必须回答：

> 它解决什么实际问题？

不能因为：

> “大厂项目用了。”

而添加。

### 原则三

所有 Retrieval 优化尽可能通过 Evaluation 验证。

### 原则四

允许使用 AI 大量生成：

```text
CRUD

DTO

配置

脚本

测试框架

基础代码
```

但是核心以下模块必须确保开发者能够解释：

```text
AST Chunk

Embedding

Keyword Search

Vector Search

RRF

Router

State

Retry

Evaluation
```

### 原则五

遇到功能范围冲突时：

```text
Retrieval Quality
>
Evaluation
>
Agent Workflow
>
Engineering Completeness
>
UI
```

---

# 34. 第一阶段验收标准

项目至少满足以下条件，才认为 V1 可以作为简历项目：

### Functional

能够导入 `my12306`。

能够回答：

```text
DOC
CODE
MIXED
```

三类问题。

能够返回：

```text
Answer
+
Source Citation
```

---

### Retrieval

拥有：

```text
Vector
Keyword
Hybrid
```

三种检索策略。

---

### Agent

拥有：

```text
Route
Retrieve
Evaluate
Rewrite
Retry
```

工作流。

---

### Evaluation

拥有至少：

```text
30条测试问题
```

以及：

```text
Recall@3
Recall@5
MRR
Latency
```

对照数据。

---

### Engineering

能够通过：

```text
Docker Compose
```

启动主要依赖。

拥有清晰 README 与架构文档。

---

# 35. 项目成功标准

项目成功并不意味着：

> 做出了比 Cursor 更强的工具。

项目成功意味着开发者能够通过这个系统解释：

1. Coding Agent 为什么需要 Context Retrieval；
2. 代码为什么不能按照普通文本随意 Chunk；
3. Vector Search 和 Keyword Search 分别擅长什么；
4. 为什么 Hybrid Retrieval 有价值；
5. 为什么使用 RRF；
6. 什么情况下 LangGraph 比 LCEL 更适合；
7. Query Rewrite 为什么可能提高召回；
8. Agentic Retrieval 为什么必须限制重试；
9. 如何评价一次 Retrieval 是否成功；
10. 如何使用实验数据证明一个 RAG 优化有效。

如果上述问题能够通过实际代码和实验回答，则 DevContext-Java 已经达到本项目主要学习目标。

---

# 36. 与 my12306 的关系

最终两个项目分别回答：

```text
my12306

How to BUILD
a complex backend system
```

以及：

```text
DevContext-Java

How AI UNDERSTANDS
a complex backend system
```

第一项目重点：

```text
Java

Spring

MySQL

Redis

JUC

Distributed Systems

Performance
```

第二项目重点：

```text
Python

RAG

LangChain

LangGraph

Retrieval

Context Engineering

Evaluation
```

两个项目互相补充，而不是技术重复。

---

# 37. P0 当前结论

当前正式确定：

**项目工作名**

`DevContext-Java`

**项目类型**

Java Project Context Retrieval / AI Application Engineering

**核心对象**

`my12306`

**核心数据**

```text
Java Source
+
Markdown Documentation
```

**核心技术主题**

```text
Structure-aware Indexing

Hybrid Retrieval

Agentic Retrieval

Evaluation

Context Engineering
```

**核心开发时间**

```text
12 Days MVP

+ 3 Days Optional Enhancement
```

**核心衡量标准**

不是：

```text
功能数量
```

而是：

```text
Retrieval Quality

Engineering Reasoning

Evaluation Evidence

Explainability
```

本文档作为后续 DevContext-Java 开发的 P0 上层约束。后续设计如果与本文存在冲突，应优先遵循本文确定的项目定位、Scope 和学习目标。
