# Web UI V1 测试报告

执行日期：2026-10-12（Asia/Shanghai）。分支 `codex/web-ui-v1`，基线 `2bcd21f`。原工作区保留原样；本次全部实现和验证位于独立工作树。

## 自动化结果

| 检查 | 结果 |
|---|---|
| 干净 main 原有测试基线 | 全部非集成测试通过，11 项数据库集成测试默认跳过 |
| 最终 Python 全套，启用 PostgreSQL 集成测试 | **787 passed，0 failed，0 skipped**，24.91 秒 |
| PostgreSQL 集成测试单独执行 | **11 passed**，8.25 秒 |
| 新增 Application / Web / 持久化 / 并发测试 | **18 项**，包含在 Python 全套内 |
| Vue / TypeScript 类型检查及生产构建 | **通过** |
| Vitest + Vue Test Utils | **7 passed** |
| Playwright Chromium | **3 passed**，19.8 秒 |
| npm 依赖扫描 | **0 vulnerabilities** |
| 构建后 FastAPI 静态页面检查 | 健康检查 200、浏览器运行错误 0、390px 布局无横向溢出 |

Python 原始报告为 `artifacts/web-v1-python.xml` 和 `artifacts/web-v1-postgres.xml`。覆盖 CLI 兼容、Fast/Full 配置隔离、真实 Observer 注入、验证章节幂等、事件序号和重放、历史恢复、重复请求并发、取消/完成竞争、会话与全局名额、SQLite 回滚、存储失败后的安全恢复以及第二服务实例拒绝启动。

前端测试覆盖章节更新、旧事件去重、证据快照替换、安全 Markdown、Java/SQL 高亮、中文输入法/Shift+Enter、网络重试复用 ID、快照恢复。浏览器测试使用真实 FastAPI、SQLite 和 SSE，以及明确的确定性工作流替身，覆盖 Fast/Full、引用查看、刷新、停止、失败、部分回答、断网继续执行和 390px 移动布局。

浏览器断网验收发现并修复了新会话更新 URL 时重置当前会话的竞态。修复后完整浏览器用例通过。依赖扫描发现旧 Vitest 依赖问题，更新到兼容版本后扫描为零。

## 真实链路结果

使用现有模型配置、PostgreSQL、知识索引、Tool Agent 和教学工作流；通过 Web API 创建执行，没有替换 AskService。问题为：

> OrderServiceImpl.createTicketOrder 方法在哪个文件，主要负责什么？

| 项目 | Fast | Full |
|---|---|---|
| 终态 | completed | completed |
| 总耗时 | 55.56 秒 | 118.14 秒 |
| 验证章节 | 2 | 3 |
| 实际证据 / 使用引用 | 4 / 4 | 5 / 5 |
| 所有章节引用对应真实证据 | 通过 | 通过 |
| 章节拼接与最终答案一致 | 通过 | 通过 |
| 会话刷新答案恢复 | 通过 | 通过 |
| SSE 完整事件重放 | 通过 | 通过 |

原始报告为 `artifacts/web-v1-live.json`；实际会话保存在 `data/web/live-acceptance.sqlite3`。Fast 本次超过原有 45 秒延迟目标，功能完成；未为达成延迟目标修改检索或教学算法。耗时是本次单次样本，不构成持续延迟指标。

生产构建已使用上述真实 Full 会话检查，截图为 [真实回答](previews/live-answer.png)、[真实代码证据](previews/live-evidence.png)、[移动端](previews/mobile.png)和[新会话](previews/welcome.png)。这些截图使用真实验收内容，浏览器自动化中的 Order.java fixture 与此区分。

## 验收范围与复现

全部 V1 功能、真实 Fast/Full 问答和本地 PostgreSQL 集成测试均通过，无外部阻塞项。浏览器异常场景使用可控工作流替身，以确保断网、取消、LLM/检索失败和部分回答可重复验证；真实模型验收仅覆盖本次两条问题，未声明整个问答基准集通过。

命令、前置条件和测试/真实验收区分见 [启动与接口说明](README.md)。V1 单进程，本机单用户；停止等待在途调用退出，不强杀 Python 线程。原项目 Java 解析器代码未改，本轮未重新运行 Maven 测试。
