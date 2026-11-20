# StellarWeaver Harness 能力与差距审计

核对日期为 2026-11-20。依据当前仓库源码、迁移、部署配置和已保存的验收记录。阶段交付见[开发进度](PROGRESS.md)，后续优先级见[路线图](../ROADMAP.md)。

## 当前实现与证据

| 能力 | 源码中的实现 | 已有验证与边界 |
| --- | --- | --- |
| 任务隔离与命令执行 | 项目线程从已提交 Git 基线创建私有 worktree，原仓库在本机 Compose 中只读挂载。专用执行服务将项目验证委派给临时、断网的 Linux 容器，限制命令、资源和环境。CLI 的宿主执行使用进程生命周期管理。 | G4 验收覆盖同仓库双任务、凭据隔离及超时清理。验证容器限制其命令进程，CLI 宿主执行仍使用当前操作系统账户权限。 [执行环境](../ARCHITECTURE.md#web-执行环境) · [工作台验收](../EVAL.md#工作台与部署验证) |
| 会话与恢复 | 项目线程支持流式续问、复用 worktree、SSE 游标补读和归档恢复。普通对话有独立分组。API 和 worker 使用持久队列，未确认的执行结果留待核对。 | 已有浏览器刷新续接、任务取消、执行器中断及补读验收。单次运行中的未确认命令不会自动重放。 [普通对话与恢复](../ARCHITECTURE.md#普通对话与恢复) |
| 验证与交付 | 逐项审批、按文件或改动块反馈、选块复验、当前 checkout 冲突预检及 IDE 补丁交接。验证结果绑定实际快照。 | 已有本机账号链路、CRLF 工作区和冲突回归。IDE 中应用补丁及应用后的检查由维护者完成。 [交付与复验](../ARCHITECTURE.md#web-修复与补丁复验) · [审核预检源码](../../src/stellarweaver/web/review.py) |
| 模型与扩展 | 100,000 tokens 默认窗口配置，支持证据压缩、适用目录的 `AGENTS.md` 和 Python AST repo map。DeepSeek 默认 Thinking High。`ds_search` 在支持的 DeepSeek 配置中按需调用。CLI 可加载 Skills 和本地 stdio MCP，Web Skills 库提供只读浏览。 | 已保存真实 Thinking 流式／非流式和工具续接结果。窗口量以保守估算控制，实际模型用量单独记录。 [模型与上下文](../ARCHITECTURE.md#模型上下文与扩展) · [Thinking 验收](../EVAL.md#deepseek-thinking-接入) |
| 身份与运维 | 本机账号使用 Cookie，会话按账号、工作区及成员权限查询。团队配置支持 OIDC、成员角色、项目授权、配额和审计。用量视图按北京时间统计今日请求，每 30 秒刷新。工作区别名由 `g8_workspace_alias` 迁移引入。 | 双账号和签名 OIDC 夹具已有记录。实际组织身份服务、生产 HTTPS 和数据库与状态卷的联合恢复仍待验收。 [数据库与迁移](../DATABASE.md) · [部署验证](../EVAL.md#工作台与部署验证) |

## 对照与剩余差距

| 工作流 | 外部能力参考 | StellarWeaver 的下一步 |
| --- | --- | --- |
| 线程分支 | Codex App Server 可通过 `thread/fork` 建立分支线程，并保留分叉边界的设置。 [Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) | 从指定历史轮次建立独立线程和对应工作区基线，定义运行中补充要求的处理边界。 |
| 集成交付 | Claude Code Desktop 提供逐行批注和 PR 检查状态。 [Claude Code Desktop 审阅](https://code.claude.com/docs/en/desktop#review-changes-with-diff-view) · [PR 状态](https://code.claude.com/docs/en/desktop#monitor-pull-request-status) | 现有审阅反馈按文件或改动块提交。可进一步补齐逐行审阅、应用后验证回传和 PR 状态联动。 |
| 应用验证 | Claude Code Desktop 提供应用预览、浏览器和终端面板。 [Claude 应用预览](https://code.claude.com/docs/en/desktop#preview-your-app) · [终端](https://code.claude.com/docs/en/desktop#run-commands-in-the-terminal) | 增加隔离环境中的开发服务管理、交互终端和页面验证证据。 |
| 扩展与集成 | DeepSeek Harness 以 Cordis 插件架构组织组件，提供 Web 启动入口。 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 打通 Web 运行时的 Skills 与 MCP 装配，明确扩展授权、配置、生命周期和对外接口版本。 |
| 持久记忆与调度 | Hermes Agent 提供跨会话记忆及定时任务。 [Hermes Agent 功能说明](https://hermes-agent.nousresearch.com/docs/getting-started/learning-path) | 当前任务使用单 Agent。跨线程项目记忆、定时任务和子 Agent 委派需要独立设计权限、预算和恢复语义。 |

## SWE-bench Lite 证据

批量适配器冻结数据集修订、题目和预算，分开保存公开任务输入和独立评分资料。已有候选补丁可以用于重新导出预测、重新评分和汇总。Web「实验记录」读取部署指定的本地 JSON。按实验查询、跨尝试关联和两组轨迹对照属于后续实验管理。 [批量适配器](../../src/stellarweaver/evals/swebench.py) · [实验报告路由](../../src/stellarweaver/web/app.py) · [评分与流程](../EVAL.md#swe-bench-lite-批量评测)

| 证据 | 状态 |
| --- | --- |
| 三题官方评分样本 | Marshmallow 和 Pydicom 的保存补丁通过独立评分。Astroid 的旧尝试触及 16 次模型请求上限，候选为空，记为未评分。 |
| 完整 `dev` 准备 | 已冻结 23 题清单，本地有 3 个题目的执行配方。新批量清单默认取消模型请求次数上限，工具、时长和累计 token 预算仍生效。 |
| 尚待验证 | 23 题完整跑通、300 题 `test` 基线、重复采样和 Harness 消融。对照实验需固定模型、任务版本、总预算和评分器。 |

历史 20 题内部评测为单次测量，初评 19/20，规则修订后以原有候选重评分为 20/20。内部评测与 SWE-bench Lite 使用各自的题集和判定口径。 [内部评测原稿](00_Sample_Run.md) · [三题试跑明细](../EVAL.md#phase-2-多仓库试跑记录)
