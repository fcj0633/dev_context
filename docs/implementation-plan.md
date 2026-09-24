# DevContext-Java 精简实施计划

## 目标

建立 Java/Markdown 解析、百炼向量化、PostgreSQL 存储、关键词/向量/RRF 混合检索和小规模评测闭环。

## 阶段

1. 建立 uv/Python 3.13、Maven 和 Docker 项目骨架。
2. 使用 JavaParser 3.28.2 解析 Java 21 类、接口、方法和构造方法。
3. 按 Markdown 标题结构切片，并排除第三方依赖文档。
4. 使用 `text-embedding-v4` 生成 1024 维向量并全量入库。
5. 实现 `pg_trgm` 关键词检索、精确向量检索和 RRF 融合。
6. 用 12 条问题计算 Recall@3、Recall@5、MRR 和耗时。

## 完成条件

- Java 与文档均产生非零切片。
- 全量重建具有幂等性，失败时不破坏上一版索引。
- 三种检索可通过统一 CLI 调用，结果可定位到源文件和行号。
- 单元测试、Java 测试、数据库集成和真实数据烟雾测试通过。
- 混合检索 Recall@5 目标为 0.75；不以无限调参阻塞 MVP 收口。

## 暂缓

LLM 答案生成、Agent、LangChain/LangGraph、Web API/UI、SymbolSolver、调用图、增量索引、Reranker 和 HNSW 均不进入本阶段。

