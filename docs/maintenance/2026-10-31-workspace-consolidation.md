# 本机 Git 工作区整理与统一目录说明

整理日期：2026-10-31（Asia/Shanghai）。代码基线：`1fe8be1debf3fcaa29ff8e18ddcbc8b11b6f7444`。

## 整理结果

日常开发目录统一为 `D:\Java-learning\DevContext`，当前检出 `main`，已包含 Agent State 与 Working Memory 重构。整理时核对了 Gitee、GitHub 两端的 `main`，均与上述代码基线一致。

四个已完成任务的附加 Git worktree 已正式移除，现在 Git 只登记一个工作区。开发分支引用和提交历史保留，移除工作区没有删除历史提交。

本次没有修改业务代码、模型配置、数据库结构或检索规则。本说明及文档索引以单独的文档提交记录；不会将本机配置、会话数据和归档产物加入提交。

## 为什么此前有多个项目目录

这些目录属于同一个 Git 仓库，共享对象和提交历史，但分别检出不同分支，拥有各自的工作文件及本机产物。此前分阶段开发、验证和合并时使用独立工作区，以避免将原目录中的资料和未提交状态混入功能提交。

各阶段工作结束并合入主分支后，附加工作区尚未清理，导致目录数量增加。远端主分支前进也不会自动更新其他工作区的文件，因此原日常目录一度停留在较早版本。

| 整理前的目录 | 整理前分支及提交 | 本次处理 |
|---|---|---|
| `DevContext` | `codex/stage4-continuous-answer`，`44cc833` | 保留原目录，切换到 `main @ 1fe8be1` |
| `DevContext-agent-run-state` | `codex/agent-run-state`，`1fe8be1` | 保存独有产物后移除工作区 |
| `DevContext-main-v5-20261008` | `main`，`1fe8be1` | 释放 main 分支占用后移除工作区 |
| `DevContext-merge-main-20261006` | `codex/merge-fast-full-main`，`346a047` | 保存本机文件后移除工作区 |
| `DevContext-web-ui-v1` | `codex/web-ui-v1`，`2276c3e` | 保存配置、会话库和产物后移除工作区 |

上述目录原先均位于 `D:\Java-learning`。删除前已确认四个附加工作区的 HEAD 都是 main 的祖先或与 main 相同，没有遗漏尚未合入 main 的工作区提交，也没有项目服务正在使用这些目录。

## 资料保留与归档

归档位于本机 `D:\Java-learning\DevContext\artifacts\workspace-consolidation-20261031`，包含：

| 归档子目录或文件 | 内容 |
|---|---|
| `original-local-files/` | 原目录 36 个受保护文件的原始备份：26 个测试文件、7 个未跟踪文件、本机 `.env` 和原有会话数据 |
| `DevContext-agent-run-state/` | 10 个前端构建及浏览器测试产物 |
| `DevContext-merge-main-20261006/` | 1 个 `benchmark/cases.jsonl` 原始文件 |
| `DevContext-web-ui-v1/` | 14 个文件，包括本机 `.env`、两份 SQLite 会话库及锁文件、测试报告和前端构建产物 |
| `manifest.json` | 原目录、分支、提交、逐文件 SHA-256、缓存排除分类和整理结果 |
| `README.md` | 本机归档内容与整理过程说明 |

原目录的受保护文件仍然保留在原位置。归档前后逐文件校验，确认 36 个原文件和 25 个附加工作区归档文件一致。

旧 Web 工作区的会话库独立保存，没有覆盖或合入原日常目录的会话库。本机原 `.env`、虚拟环境、Python 运行时、缓存、依赖和已有 artifacts 均保留。

归档可能包含本机配置及会话内容，继续受现有 `artifacts/*` 忽略规则保护，不上传远端。本记录只描述其位置与保存方式；新克隆仓库不会自动获得这些本机备份。

## 状态清理与安全处理

原目录的 26 个测试文件虽然显示为已修改，但逐一计算 Git 规范化内容哈希后均与 HEAD 一致。确认没有实际代码修改后，刷新其索引状态；没有改写工作文件，也没有生成测试逻辑变更。

旧合并工作区的 `benchmark/cases.jsonl` 同样没有 Git 内容差异，仍保存原始文件作为备份。

State 重构工作区的 `.venv` 是指向原日常目录虚拟环境的 Windows junction。清理时先核对链接目标，再仅移除链接本身，随后通过 `git worktree remove` 移除工作区，主目录的虚拟环境未被删除。

附加工作区中可重新生成的依赖、Python/测试缓存、旧解析器构建产物随工作区清理。所有移除目标均核对为 `D:\Java-learning` 下明确命名的附加工作区。

原有 7 个未跟踪文件保持未跟踪，未被删除、移动或擅自纳入提交。它们包括历史规划、Visio、回答对比文本及 `trace.json`，与本次目录整理文档分开处理。

## 整理后的验证

| 检查 | 结果 |
|---|---|
| `D:\Java-learning` 下 DevContext 项目目录 | 仅保留 `DevContext` |
| `git worktree list` | 仅登记主目录，检出 `main` |
| 代码版本 | `1fe8be1`，与整理时两端远端 main 一致 |
| 新版 State 导入 | 从主目录的 `src/devcontext/tool_agent/state.py` 成功导入 |
| State、Tool Agent、V5 及确定性等价回归 | 94 项通过 |
| 原文件及归档文件 | SHA-256 校验通过 |
| 已跟踪文件 | 整理完成时无未提交修改；随后仅新增本说明及索引更新 |

94 项回归的执行范围如下，测试进程使用 `LLM_PROVIDER=deepseek`、`EMBEDDING_BYPASS_PROXY=false`，没有修改本机 `.env`：

```powershell
Set-Location D:\Java-learning\DevContext
.venv/Scripts/python.exe -m pytest tests/test_agent_state.py tests/test_agent_state_equivalence.py tests/test_tool_agent.py tests/test_v5_runtime.py tests/test_v5_structure.py tests/test_v5_semantic_alignment.py -o addopts= -q -ra
```

这是目录整理后的针对性验证，不代表在此次整理中重新执行了全套测试、数据库集成测试或真实模型问答。Agent State 重构的完整验收范围及限制见[重构架构与验收说明](../refactor/05-agent-run-state.md)。

## 后续日常使用

编辑器、终端、运行配置和服务启动目录统一使用 `D:\Java-learning\DevContext`。此前指向四个附加目录的外部快捷方式或手工启动配置应改用该目录；本次没有扫描或重写系统其他位置的快捷方式。

需要隔离开发时仍可创建临时 worktree，但任务完成后应先确认提交已合入目标分支、保存本机独有文件并检查服务占用，再通过 Git 移除工作区。不要将共享虚拟环境链接当成独立依赖目录递归删除。
