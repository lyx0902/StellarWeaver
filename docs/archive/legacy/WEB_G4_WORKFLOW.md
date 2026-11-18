# G4 隔离工作台

> 历史记录。现行说明见[项目文档](../../README.md)。

G4 在已有 Compose 工作台上增加任务 worktree、线程追问和补丁验证。每个新线程从登记仓库的已提交 HEAD 创建一个私有 Git worktree。同线程追问继续使用该 worktree。已登记的原仓库采用只读挂载，未提交和未跟踪的文件不进入新任务。

## 启用

从 StellarWeaver 根目录运行。复用已有的工作台配置和端口时，先让正在运行的任务结束。Docker Desktop 需使用 Linux 容器。本地先准备可信的 Python runner 镜像。

```powershell
docker pull python:3.12-slim-bookworm
$env:SW_WORKBENCH_REPAIR = '1'
$dc = @('--env-file', '.env', '--env-file', '.stellarweaver/workbench.compose.env', '-p', 'stellarweaver', '-f', 'deploy/compose.yaml', '-f', 'deploy/compose.workbench.yaml', '-f', 'deploy/compose.g4.yaml')
docker compose @dc up -d --build --wait
docker compose @dc ps -a
```

使用 FakeModel 时可以去掉前一组 `'--env-file', '.env'` 参数。DeepSeek 使用原配置中的 `SW_WORKBENCH_PROVIDER=deepseek` 和宿主机代理设置。新增 PostgreSQL 迁移由 Compose 的 `migrate` 服务执行。打开原工作台并使用原令牌。旧运行可查看。旧版线程绑定原来的执行方式，启用 G4 后从新线程开始隔离任务。

## 项目验证命令

已有配置文件 `.stellarweaver/workbench.compose.env` 中可以填写：

```dotenv
SW_RUNNER_IMAGE=python:3.12-slim-bookworm
SW_VERIFY_COMMANDS='[["python","-m","unittest","-q"],["python","scripts/check_build.py"]]'
```

将 `scripts/check_build.py` 替换为项目实际存在的构建检查脚本。默认只运行 `python -m unittest -q`。每条命令使用 JSON argv，在任务根目录运行。依赖需事先安装在可信 Linux 镜像内，镜像需要 Python 3.12+ 且不得声明 `VOLUME`。pytest、npm 或构建工具可用自备 runner 镜像。变更命令后重新执行 Compose 启动命令。

Agent 发起验证仍需逐项审批。选定补丁发起验证时，工作台逐条执行配置中的所有命令。报告保留退出码、输出、验证文件快照和清理状态。文件在检查后变化时需要重新验证。自定义命令的零退出码以项目定义为准，unittest 发现零测试时标记为未确认。

## 浏览器中的操作

1. 从左侧选择已有线程，或点击「新任务」。输入框固定在对话底部。按 Enter 发送，按 Shift+Enter 换行。运行中可以先写下追问，待本轮结束再发送。
2. 编辑和验证的审批卡显示在当前轮次中。最终回复出来后，工具调用折叠到「执行过程」，可以展开查看，也可以通过「运行详情 → 日志」看完整事件。
3. 点击「查看改动」打开右侧审阅面板，可按文件看带行号的 Diff、选择改动块和下载补丁。点击「在独立工作区应用所选并验证」后，工作台按原基线创建审阅 worktree，并在「验证」页展示新一次运行的命令、退出码与输出。
4. 继续同一个线程时直接使用底部输入框。若上轮中断，先核对未确认操作，再选择带上已完成的对话或用新提示检查工作区。每个新操作重新授权。

输入框中的上下文表显示当前所查看轮次的最近一次模型请求输入量，分母默认是 100,000 tokens 的窗口配置。模型未返回用量时显示估算值，收到用量后更新。窗口包含预留给输出的空间。

## 执行范围

- 每条验证命令在一次性 Linux 容器内运行，接收该任务的文件快照。容器使用非 root 用户、只读根文件系统、独立的临时可写目录和关闭外部网络的配置。构建产物随容器销毁。Git 元数据、控制面凭据、Docker socket 和其他任务卷留在执行容器之外。[Docker 容器选项](https://docs.docker.com/reference/cli/docker/container/create/) · [Docker 的 none 网络](https://docs.docker.com/engine/network/drivers/none/)
- 本机可信 worker 通过 Docker socket 创建和清理容器。部署范围为本机可信控制面。仓库内容仍会按 Agent 配置发给模型，提交到仓库普通文件中的秘密应由仓库维护者处理。提交中的受保护文件名、符号链接和子模块会使工作区初始化失败。
- 单 worker 依次执行排队任务。任务 worktree 保存在原 `workbench_state` 卷，供追问和审阅复用。备份会话时同时保存 PostgreSQL 与该卷。
- 候选补丁支持文本改动。补丁应用和检查均发生在新审阅 worktree。原仓库的合并由维护者使用导出的补丁处理。
