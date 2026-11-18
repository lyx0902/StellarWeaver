# G2 本地审批与修复

> 历史记录。现行说明见[项目文档](../../README.md)。

工作台默认提交只读任务。服务端使用 `--enable-repair` 启动后，浏览器可以显式选择修复模式。每次创建或编辑文件会显示完整的待审批 diff，运行测试会显示完整 argv、工作目录和超时。审批 90 秒内有效，每次只批准一项操作，刷新后可以继续处理同一项。测试仍以本机 Python 进程运行，请只登记自己信任的仓库。

数据库和访问令牌按 [G1 启动说明](WEB_G1_QUICKSTART.md) 配置。升级已有数据库时，先停掉旧 API，保留现有数据库卷，按原步骤使用迁移角色执行 `uv run --locked --extra web python -m stellarweaver.web migrate`。迁移完成后仍使用应用角色运行服务。

## 离线运行一次修复

从项目根目录在 PowerShell 创建新的临时 Git 测试仓库，然后启动 API。

```powershell
$demo = (New-Item -ItemType Directory -Path (Join-Path ([System.IO.Path]::GetTempPath()) ("stellarweaver-g2-" + [guid]::NewGuid().ToString("N")))).FullName
git init -q $demo
Set-Content -LiteralPath (Join-Path $demo 'calc.py') -Value @'
def add(a, b):
    return a - b
'@
Set-Content -LiteralPath (Join-Path $demo 'test_calc.py') -Value @'
import unittest
from calc import add

class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
'@
uv run --locked --extra web python -m stellarweaver.web serve --provider fake --enable-repair --workspace "$demo"
```

另开一个终端，在前端目录运行 `npm.cmd run dev`。打开 [本地工作台](http://127.0.0.1:5173)，使用启动后端时配置的令牌连接。选择「修复（逐项审批）」，填入「修复 calc.py 并验证」并提交。依次批准两次编辑和两次测试，可以在报告中看到退出码 1 和 0。点击「下载 JSON 报告」保存证据。重新演示请创建新的临时仓库。

使用已配置的 DeepSeek 时，把启动命令的 `--provider fake` 改为 `--provider deepseek`。默认从项目根目录的 `.env` 加载服务端 API Key。修复模式支持创建和编辑文件，测试命令只允许使用工作区根目录下的 `<服务端 Python> -B -m unittest -q`，每次仍须批准。

## 如何读报告

- 工具时间线显示实际读取内容和执行结果。报告列出已执行的补丁、可观测文件变更、测试退出码和有界输出；读取内容和报告输出都有长度上限。
- 测试证据绑定命令前后的文件快照和编辑序号。同一次运行中的后续编辑或可观测文件变化使旧结果标为 `stale`，仍可查看原始退出码。退出码 0 且确认至少运行一项测试，才会记录为通过。快照覆盖有界的非忽略普通文件，排除受保护路径和 Git 元数据。报告描述运行结束时的证据。
- 取消时待审批操作失效，已启动的测试按本机进程清理流程终止。已完成的文件编辑保留在磁盘上，报告会记录它们。
