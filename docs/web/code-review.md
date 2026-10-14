# Web 接入前代码审查

基线为 `2bcd21f8aed87f8bbebf9d37155497d201b349d7`：远端 main、本地 main 和独立工作树初始 HEAD 一致。原工作区已有测试修改与文档不纳入本次实现。

## 真实入口与装配

`cli.main` 解析策略后选择 plan-only、no-plan 或 planned。正常路径 `_planned_workflow` 构造 `EvidenceDrivenWorkflow`，教学调用 `TeachingExplanationWorkflow`。`_retrieval_controller` 根据配置构造 Tool Agent 或 Fixed/Graph 控制器，main 默认 Tool Agent。

本次将装配逻辑迁至 `application.assembly.WorkflowFactory`，用显式依赖映射保留 CLI 工厂替换点；请求配置解析迁至 `resolve_request`。CLI 保留格式化、重复执行与性能报告，实际 Ask 进入 `AskService`，Application 不导入 CLI。

## 策略与结果

`resolve_policy` 在检索前决定 profile、effort、读者假设和 deadline。Fast 使用 Single Stream；Full 使用 V3，已有 planner/writer 修复与 buffered direct fallback。本次不改变检索、Coverage、教学合同和恢复算法。

`AgenticAnswerResult` 实际包含 `answer_result,context_bundle,trace` 及可选 `evidence_catalog`。回答状态在 `trace.teaching.completion_status`，取 complete/partial/failed；检索状态在 `evidence_package_state`，取 READY/PARTIAL/EMPTY/RETRIEVAL_FAILED。二者独立，不能以返回值存在或答案非空标成功。

## Observer 与章节

`RetrievalObserver` 只有 `on_action_completed(action,execution)` 和 `on_context_built(round_index,bundle)`。`SearchAction` 来源字段是 `source_scope`。检索工具和控制器使用既有回调；规划、Coverage、Graph 细节仅在最终摘要显示。

Fast/V3 Writer 在章节边界解析及标题、正文、引用检查后发布 `ValidatedSection(section_id,markdown,citations)`。Full 的 direct fallback 也使用这个接口。回调失败是既有非重试错误。本次在实际证据包形成处增加请求独立、只读发布钩子，覆盖 Fast、Full、fallback；原 CLI 无钩子时行为不变。

证据来自真实 ContextItem；最终用 Catalog 校对。局部检索 Context 的 C 标签不能替代运行稳定 E 标签，完整检索包也不能替代回答实际使用的包。

## 失败、隐私与取消

Full Trace 的 planner_response、attempts.response、rejected_protocol_buffer、拒绝章节和内部规划不适合直接返回。Web 使用逐字段白名单、安全错误码和配置密钥脱敏，不保存这些原始诊断。

原有 deadline/fatal request scope 使用 ContextVar，但没有统一取消接口。本次增加请求独立 token 和 scope，在已有阶段检查、传输入口及流事件边界退出。取消异常继承 BaseException，避免被算法的 except Exception 转成重试或 fallback；未取消请求保持原算法。

同步调用无法安全强杀，取消立即持久化，名额在线程实际退出后释放。SSE 断开与 Agent 执行生命周期独立。
