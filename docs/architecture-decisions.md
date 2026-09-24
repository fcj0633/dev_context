# 架构决策

## 双根目录

代码和文档由两个独立根目录配置，数据库只保存相对路径。`my12306` 及其文档保持只读。

## Java 结构解析

使用独立的 Java 21 Maven CLI 和 JavaParser 3.28.2。类切片保存摘要，方法和构造方法保存完整原始源码。第一版不使用 SymbolSolver。

## Markdown 切片

按标题章节切分并保存完整标题路径。片段上限为 6000 个 Unicode 字符，超限先按段落、再按行拆分。

## 存储

PostgreSQL 18 + pgvector 0.8.6。核心数据放在单张 `knowledge_chunk` 表中，向量固定 1024 维。MVP 使用事务化全量重建。

不对“文件 + 行号”施加唯一约束，因为一个超长单行章节可以合法拆成多个片段。幂等性由同一事务内按仓库删除旧记录并写入新记录保证。

## 检索

- 关键词：精确标识符、子串和 `pg_trgm`。
- 向量：`text-embedding-v4` 与精确 cosine distance。
- 混合：两路各取 20 条，使用 `k=60` 的 RRF，默认返回 10 条。

OpenAI 兼容 Embedding 接口支持 `dimensions=1024`，但不支持 DashScope 原生接口独有的 `text_type`；实现不会向兼容接口发送该无效参数。

当前 Windows 环境的 Python/OpenSSL 与百炼公共域名可能出现 TLS 提前关闭，而系统 curl 可正常连接。Embedding 客户端支持 OpenAI SDK、自动回退和 curl 三种传输模式。API Key 通过 curl 配置的标准输入传递，不进入命令行、日志或临时文件。

本机默认设置 `EMBEDDING_TRANSPORT=curl`，避免每次先等待一次已知会失败的 TLS 尝试；在其他环境中可改为 `auto` 或 `openai`。curl 只把非敏感请求正文写入短期临时文件，密钥仍不落盘。

## 安全

API Key 只从环境变量读取。`.env`、数据库数据、向量产物和解析中间文件不提交。
