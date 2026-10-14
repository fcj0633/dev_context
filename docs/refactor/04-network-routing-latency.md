# GPT / DeepSeek 网络分流与耗时诊断

日期：2026-10-14（Asia/Shanghai）。基线：`2d824ba87dbd9f64fe01e8824c39109fb03eaea5`。

## 结论

本机原配置已经让 GPT 经 HTTP 代理、百炼嵌入直连。GPT 更慢的本次主要可观测等待集中在串行模型 API 调用，不能归因为嵌入误走代理。实际将嵌入请求强制送入代理会发生 TLS 握手失败，因此需要保持分流，但分流本身不能消除当前的模型决策等待。

一个定位问题的四次完整请求全部完成。关闭代理未在本次观察中缩短总耗时；不同调用的输出长度、推理用量、证据输入不同，不能将这四个单次样本写成稳定均值或网络因果结论。

已将两种网络路径改为客户端独立配置。本机最终保留 GPT 代理、Embedding 强制直连、DeepSeek 沿用原有代理环境，以及 continuous/Fast。未修改 VPN 全局开关、系统路由、索引、模型预算或提示词，也未开始第 5 阶段。

## 原实现和网络证据

GPT 与 DeepSeek 的同步和 SSE 请求均通过 curl。Embedding 的 curl、SDK 及自动回退原来都继承进程代理设置，没有独立配置。

本机 HTTP_PROXY / HTTPS_PROXY 指向 `http://127.0.0.1:12334`，该代理端口处于监听状态。原绕过代理列表覆盖百炼域名。实际 curl 的 `proxy_used`、远端地址和端口确认：GPT、DeepSeek 使用本地代理，Embedding 连接百炼远端 443，未使用 HTTP 代理。

下表为无认证 GET 的单次连通性探针。401/400 表示请求到达服务并收到拒绝响应，不能当作认证或模型生成成功。

| 服务 | 直连耗时 | 经代理耗时 | 结果 |
|---|---:|---:|---|
| GPT 兼容网关 apinebula.ai | 2.134 秒 | 0.473 秒 | 两侧 HTTP 401 |
| 原生 DeepSeek | 0.324 秒 | 0.345 秒 | 两侧 HTTP 401 |
| 百炼 Embedding | 0.242 秒 | 0.221 秒后失败 | 直连 HTTP 400；代理 HTTP 0 / curl exit 35 |

补充使用同一问题文本进行一次真实 Embedding POST / 路径：直连 HTTP 200、1024 维、0.343 秒；经代理 HTTP 0、curl exit 35、0.231 秒。后者是 TLS 连接失败，短耗时不能视为性能改善。

这些探针不是生成延迟测试。GPT 的模型后端排队、推理和网关处理没有独立指标，不能通过 GET 时间直接估算。

## 完整问答对照

问题固定为：**用户注册入口位于哪个类？请说明接口路径，以及它调用的服务方法。**

- 模型：`deepseek-flash` 与现有网关模型 `gpt-6.1-sol`。
- Fast + continuous；每次请求 240 秒总上限，仅用于实验。
- 执行顺序：DeepSeek 原配置、GPT 原配置、GPT 直连、DeepSeek 直连。
- 不预热、不自动重跑、不使用模型裁判、不重新索引。
- 原配置继承真实进程代理；直连样本使用独立子进程清除代理变量并设置 NO_PROXY=*。没有修改主进程或系统代理。
- 四份生成记录各包含 4 次模型调用、2 次真实 Embedding，均正常完成且取证状态为 READY。READY 不代表事实已认证。
- 实验完成后才接入独立代理配置，避免修改运行中的基线链路。复现脚本明确关闭新增显式配置，继续对比原继承路径和子进程直连路径。

| 样本 | 总耗时 | 模型首正文 | 取证阶段 | Writer 阶段 | Embedding 调用合计 | 模型调用合计 | 答案字符数 |
|---|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek 原配置（代理） | 34.88 秒 | 28.21 秒 | 25.88 秒 | 8.83 秒 | 0.82 秒 | 32.70 秒 | 2263 |
| GPT 原配置（代理） | 97.56 秒 | 81.62 秒 | 75.45 秒 | 21.89 秒 | 0.69 秒 | 95.62 秒 | 1533 |
| GPT 直连 | 144.34 秒 | 99.55 秒 | 78.27 秒 | 65.84 秒 | 0.76 秒 | 142.42 秒 | 2392 |
| DeepSeek 直连 | 52.04 秒 | 47.77 秒 | 31.02 秒 | 20.90 秒 | 0.76 秒 | 50.47 秒 | 2189 |

取证阶段包含其子调用，不再与模型、Embedding 合计相加。Writer 阶段按 Writer 开始到请求结束计算，包含本地收尾。模型首正文使用诊断中的 provider_first_body，是本地消费者观察到正文的时间；没有测量 SSE 发布、浏览器 DOM 或真实屏幕绘制。

原 GPT 样本的模型调用分解：

| 调用 | 耗时 |
|---|---:|
| Evidence Planner | 41.39 秒 |
| Agent Planner | 11.62 秒 |
| Coverage | 20.73 秒 |
| 连续 Writer | 21.88 秒 |

GPT 直连样本分别为 20.65、32.06、23.88、65.83 秒；并非每一个子调用都随网络切换同向改变。GPT Writer 输出用量也从代理样本 561 增到直连样本 1195。DeepSeek 直连样本的规划/写作推理用量明显增加，例如 Writer 的 reasoning_tokens 从 199 增至 2502。因此单次差异包含模型随机输出和后端波动，不能全部计入代理效应。

另一个需要核实的配置因素：DeepSeek 四个调用均实际发送 reasoning_effort=low；GPT 四个调用虽请求低推理档位，但因网关/模型能力尚未声明，工厂安全省略该参数，记录 omission_reason。不能将两者称为相同的低推理强度。后续应核实网关对档位参数的真实支持，再声明 LLM_SUPPORTED_REASONING_EFFORTS；本次没有盲目开启或修改参数，以免混入网络对照。

人工快速核查四份正文均定位到 UserInfoController、POST /api/user-service/register 和 UserLoginService.register。本次没有开展完整事实支持/教学质量评测。

最初两个样本的脚本 first_delta 回调匹配了错误事件名，原字段保留 null。后两个样本已修正为 answer.text_delta。所有样本原诊断快照都保留完整事件，可从快照恢复首次 delta 时间；analysis.json 保存该恢复字段。上表统一使用四份快照原生 provider_first_body，未把缺失指标伪装为零，也没有修改原始样本。

## 配置和代码变更

```dotenv
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-6.1-sol
OPENAI_PROXY_URL=http://127.0.0.1:12334
EMBEDDING_BYPASS_PROXY=true
ANSWER_SYNTHESIS_MODE=continuous
```

密钥及网关地址继续使用现有本机配置，不写入版本控制。代理 URL 在 Settings 内使用 SecretStr，公开输出及诊断不包含代理认证信息。

- OPENAI_PROXY_URL 可选。配置后，GPT 同步、JSON SSE 和正文 SSE 共用显式代理，覆盖当前客户端的 NO_PROXY，不改变其他客户端。
- EMBEDDING_BYPASS_PROXY=true 时，curl 设置空代理和 noproxy=*，SDK 使用 DefaultHttpxClient(trust_env=False)。检索、导入、smoke-api 均传递同一配置；本次没有执行导入或修改知识库。
- DEEPSEEK_BYPASS_PROXY 可选，默认 false。当前本机不启用，保留原环境代理。两个 DeepSeek 单次样本不能支持稳定优选结论。
- 配置未提供时保持原继承行为；.env.example 明确建议 Embedding 直连。
- 显式 curl 路径使用首参数 --disable，避免用户 curlrc 覆盖分流；代理及认证仅通过 stdin 配置，不加入进程命令行。
- 代理配置校验禁止非法协议、端口和控制字符。各客户端独立配置，没有请求中切换共享环境变量的并发风险。

这里的“直连”指绕过 HTTP/SOCKS 代理。操作系统 VPN 的 TUN、透明转发及全局路由仍可能影响流量；若 VPN 使用这些模式，需要在 VPN 软件自身配置百炼域名直连。本次只验证 HTTP 代理路径，没有宣称绕过所有 VPN 网络层。

架构依据：[OpenAI 官方 Python 库 HTTP 客户端配置](https://developers.openai.com/api/reference/python)将代理等网络行为交给独立客户端。实际运行环境 openai==3.17.0，已确认提供 DefaultHttpxClient；本项目 GPT 主传输为 curl，因此采用同样的客户端隔离原则，并未改换模型 API 或照搬新版 SDK 类名。

## 复现、验证与产物

执行独立网络对照，使用新的产物目录（脚本拒绝覆盖/重跑已有目录）：

```powershell
.venv/Scripts/python.exe scripts/diagnose_network_latency.py --output artifacts/network-latency-new
```

代理探针默认使用本机已验证端口，可通过 --proxy 指定。该参数仅控制显式连接探针；current 完整样本仍继承真实进程代理，不会假称切到了参数指定的代理。实际测量时须确认父进程代理与探针参数一致。该脚本最多 4 个完整问答，请勿把它当作无费用的健康检查。

完整证据、实际模型输入、回答原文、时间线及网络原始指标位于忽略目录 `artifacts/network-latency-20261014/`。其中 manifest.json、ambient-routes.json、embedding-route-probes.json 和 analysis.json 为摘要；*.snapshot.json、*.summary.json、*.answer.md 为每次请求材料。

测试范围：显式代理覆盖环境绕过、Embedding SDK/curl 直连、同步和流式路由一致、代理凭据不进入命令行、非法代理校验、客户端隔离，以及既有回答、检索、诊断和 Web 后端回归。前端未改动，不新增前端测试或质量裁判。

验证结果：针对性网络、模型传输、Embedding 与交付测试 98 项通过，其中新增网络测试 14 项；全套 Python 测试 967 项通过、11 项跳过，收集总数 978；git diff --check 通过。全套包含 Web 后端回归，前端没有变更，因此没有重新构建前端或扩大浏览器测试。

首次全套运行受本机切换后的 GPT 配置影响，部分旧 CLI 测试不能命中其 DeepSeek 替身，已中止。随后仅在测试进程中设 LLM_PROVIDER=deepseek 和 EMBEDDING_BYPASS_PROXY=false，定位并补齐 RetrievalService 测试替身缺失的新配置属性；修复后完整通过。该测试环境隔离没有覆盖本机 Web 的 GPT 设置，新路由测试单独明确验证 GPT 专属代理和 Embedding 强制直连。

另做配置后的真实 Embedding 检查：在独立进程中将 HTTP_PROXY/HTTPS_PROXY 指向不可达的 localhost:9，并清空 NO_PROXY；新客户端设置 bypass_proxy=true 后仍成功返回 1024 维，耗时 0.696 秒。结果保存在 configured-embedding-check.json，证明新配置无需依赖原绕过域名列表。

提交范围为独立路由实现、三类 Embedding 入口、模型传输/组装、配置示例、14 项新测试和测试替身、复现脚本、README 及本报告。本机 .env、实验原文、历史未跟踪文档、trace.json 和原有纯换行状态不纳入提交。提交哈希通过 git log 检查本报告所在提交；报告不写入自身提交哈希，避免自引用。

## 后续方向

保持已验证的分流，优先推进此前规划的受控取证循环，减少 Evidence Planner / Agent Planner / Coverage 的重复串行理解。本次定位题也要在 Writer 前等待三次模型调用，是首正文等待的明确组成部分。

需要证明代理或模型后端的稳定性能差异时，应另行制定交替、多次、固定提示输入和输出上限的组件实验，记录生成首字、输出速率、网关响应头和排队指标。本次不扩大真实模型采样，也不能把通过第三方兼容网关的 GPT 结果直接外推为官方 OpenAI API 性能。
