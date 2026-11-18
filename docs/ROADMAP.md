# 实施阶段与 Harness 差距

StellarWeaver 的 CLI 阶段建立了模型、工具、策略和会话核心。Web 阶段在共享运行时上增加持久队列、隔离工作区和审阅界面。当前使用情况见[项目概览](README.md)，测试和历史结果见[验收文档](VALIDATION.md)。

## 实施阶段

| 阶段 | 原目标 | 交付与验收 |
| --- | --- | --- |
| M0 | 工程基线与 CLI 入口 | 建立 Python `src` 布局、uv、配置、`--help`、`version` 与离线诊断。 |
| M1 | 工具调用与修复闭环 | 领域类型、FakeModel、八个内置工具和 Agent 主循环。临时 Git 仓库复现读取、编辑、测试失败及修复。 |
| M2 | 真实模型及持续交互 | 接入 DeepSeek Chat Completions，完成文本与工具调用聚合、CLI 流式、持续聊天及真实修复任务。 |
| M3 | 上下文与状态恢复 | SQLite schema 2、检查点、回放和恢复。上下文保留完整工具回合，支持仓库指令与有界 repo map。后续将默认窗口配置调至 100,000。 |
| M4 | 扩展与改动管理 | Skills、本地 stdio MCP、Git 启动基线、`/diff` 与经核对的单步 `/undo`。 |
| M5 | 评测与分发 | 20 题固定任务集、独立 oracle、重评分及来源收据，完成离线参考动作、单次真实评测和 Python 包检查。 |
| G0–G1 | 共享 Agent 装配与只读工作台 | React、FastAPI 和 PostgreSQL 保存运行与事件。刷新后按 SSE 游标继续查看。 |
| G2 | 可审阅的修复闭环 | 浏览器逐项审批、取消、差异和测试报告。测试证据绑定对应内容快照。 |
| G3 | 部署及持久队列 | Compose、迁移服务、独立 worker，事务入队、领取归属和中断标记。 |
| G4 | 任务隔离和线程续接 | 私有 Git worktree、一次性验证容器、同线程追问、按改动块导出和复验。 |
| G5 | 项目授权与团队运行 | 角色、OIDC、并发、配额和审计，专用执行器收敛 Docker 权限。验收覆盖两个账号及执行器中断。实际组织身份源与完整恢复仍待接入验证。 |
| 本机工作台增量 | 降低登录和多项目使用成本 | 账号密码注册、历史继承、多项目、普通对话、归档与恢复。数据库升级至 `g6_local_accounts`，沿用现有 Compose 入口及数据卷。 |

最初的实施要求保存在 [CLI 规范](archive/IMPLEMENTATION_SPEC.md)和 [GUI 方案](archive/GUI_PLATFORM_IMPLEMENTATION_PLAN.md)。按次记录的实施、测试和故障修复见[开发历史](archive/PROGRESS.md)。

## Harness 能力对照

按用户可执行的工作流比较。外部能力以对应产品的官方文档或源码为依据，StellarWeaver 以当前实现和已有验收为依据。比较覆盖功能、操作边界与交付方式。

| 场景 | StellarWeaver 当前实现 | 对照与实际差距 |
| --- | --- | --- |
| 线程与项目环境 | 项目线程复用 worktree，支持追加轮次、日志补读和归档。普通对话单独分组。 | Codex App Server 提供线程分叉接口，Codex 的工作树作为独立执行环境。StellarWeaver 仍需支持从指定历史轮次创建分支线程、保存分支基线和处理运行中补充要求。 [Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) · [Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees) |
| 代码审阅与最终交付 | 按块选择、导出 Diff、在新独立 worktree 应用并复验。最终合并由维护者执行。 | Claude Code Desktop 提供差异批注和 PR 状态跟踪。StellarWeaver 需要受控地将选中补丁交付至目标分支，预览冲突，保留目标仓库的既有改动。 [Claude Code Desktop](https://code.claude.com/docs/en/desktop#review-changes-with-diff-view) |
| 应用验证和桌面开发 | 工作台显示编辑与命令结果、退出码、快照和清理状态。 | Claude Code Desktop 可管理开发服务器，在浏览器面板验证应用并查看 DOM 或截图。StellarWeaver 的验证仍以配置的批处理命令为主，交互终端、开发服务与浏览器产物需要接入。 [Claude Code 应用预览](https://code.claude.com/docs/en/desktop#preview-your-app) |
| 扩展体系与客户端接入 | CLI 有 Skills 和本地 stdio MCP，Web 具备自身 REST 与 SSE 接口。 | DeepSeek Harness 将插件作为主要架构组成，并提供多种运行入口。StellarWeaver 需要明确 Web 扩展的权限、生命周期与部署方式，形成稳定的外部客户端接口。 [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness) · [官方 CLI 参考](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md) |
| 长期记忆、委派和定时 | 保存原始历史、压缩上下文和仓库说明，单 Agent 执行任务。worker 可以运行多个不同账号的任务。 | Hermes Agent 提供跨会话记忆、定时任务和子 Agent 委派。StellarWeaver 需要明确跨线程知识的保存、授权、删除和检索范围；任务并发与同一任务下的委派属于不同机制。 [Hermes Agent 功能说明](https://hermes-agent.nousresearch.com/docs/getting-started/learning-path/) |
| 团队运行与运维 | 本机账户与团队 OIDC 配置、租户与项目访问控制、额度、审计和私有执行器。 | 当前主要验证单机 Compose。真实组织身份、HTTPS、备份与恢复、远程执行和跨设备使用需要相应环境和验收。 [本项目数据库](DATABASE.md) · [工作台验收](VALIDATION.md#工作台与部署验证) |

## 后续推进顺序

| 优先级 | 工作 | 验收标准 |
| --- | --- | --- |
| 1 | 安全完成补丁交付，并完善重启恢复。 | 对同一选中补丁展示应用前检查、成功路径和冲突详情。目标分支已有改动可核对，失败保留文件状态与操作记录。演练恢复 PostgreSQL 和状态卷后，可打开原线程并继续独立 worktree。 |
| 2 | 完善线程分支与面向应用的验证。 | 从规定轮次建立新线程并保留可比较的基线。隔离环境启动开发服务、记录状态和日志，至少完成一个 Web 功能的页面行为验收。异常时能关闭服务并收集结束证据。 |
| 3 | 统一 CLI 与 Web 的扩展访问，提供稳定集成接口。 | 同一许可的 Skills 或 MCP 场景分别经 CLI 与 Web 验证。UI 能查看配置状态，连接失败和取消均有可追踪记录。外部接口明确版本和授权。 |
| 4 | 扩大多用户与跨平台使用，再评估持久记忆、定时任务和子 Agent。 | 真实身份服务、部署恢复和平台矩阵具备验收记录。新机制以可测使用场景确定范围，分别记录权限、生命周期和资源限制。 |

功能验证和真实模型复跑分批执行。评测基线、消融及跨平台计划见[实验计划](VALIDATION.md#后续实验)。
