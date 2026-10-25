# V1验收核对（2026-10-25）

对应原始 `IMPLEMENTATION_SPEC.md` 第20项的25条。原规范和根目录副本不修改勾选，以本页记录当前实现、实际证据与边界。当前是Windows11/Python3.12.6本机验收；远端CI、Linux/macOS运行、重复真实评测及消融仍未完成，不能称全平台验证。

| # | 验收行为 | 当前证据与边界 |
| --- | --- | --- |
| 1 | chat与非交互exec | CLI、持续聊天、真实公开runtime测试；最小入口可运行 |
| 2 | 真实provider与缺Key离线 | DeepSeek历史完整20题；当前默认测试/Fake无网络。此次修复没有重新请求模型 |
| 3 | 八工具可发现和调用 | registry/Agent闭环与完整离线20题 |
| 4 | 定位、编辑、失败后修复再通过 | 临时Git真实文件和unittest；`python -m stellarweaver.demo` |
| 5 | 完整流式参数后执行 | streaming分片、坏JSON、缺finish、断流回归 |
| 6 | 参数/未知工具/权限结构化结果 | registry、policy与Agent完整工具回合 |
| 7 | 工具、模型和总任务预算 | 共享恢复/基线/扩展/执行deadline，清理后保存outcome；同步阶段返回后记账，CLI初始化与provider最终关闭另计 |
| 8 | 程序级只读 | schema与dispatch双层限制，命令和写入拒绝 |
| 9 | 非交互授权不足不挂起 | 无TTY明确blocked、退出3，精确策略授权 |
| 10 | 超时/取消回收受管进程 | Windows Job Object真实PID测试，父先退出/detach/双取消；MCP SDK关闭。搜索/Git及基线不吞unknown；范围限受管创建树，非OS沙箱 |
| 11 | 修改版本冲突保护 | 完整SHA-256、唯一替换、授权后路径/hash/权限复检 |
| 12 | diff区分用户与Agent变化 | 脏Git/index不变；基线路径删除/还原clean/rename/类型变化回归 |
| 13 | undo只撤可证明的内置修改 | before/after、权限及快照hash匹配；冲突拒绝，逆序单步 |
| 14 | SQLite消息/工具/授权/制品 | schema2事务、外键、单写者锁与UI持久化顺序测试 |
| 15 | resume不重放未知副作用 | 真os._exit后命令counter保持1，文件after状态只确认，连续恢复不放行unknown |
| 16 | replay无模型/工具执行 | 只读快照、无原DB sidecar、provider未构造 |
| 17 | 压缩完整工具回合/证据 | context/M3压力及100k大源码回归；UTF-8 bytes保守估算，非精确tokenizer |
| 18 | AGENTS范围可解释 | 根到操作祖先目录；授权后新增/修改/删除返回INSTRUCTIONS_UPDATED且0副作用；最终检查后外部并发竞争仍有限制 |
| 19 | Skills发现/加载/记录 | 本地技能、来源/hash、按需加载事件与演示 |
| 20 | MCP真实stdio发现/调用/关闭 | 官方SDK，本地真服务器分页/刷新/超时/取消/关闭测试和演示 |
| 21 | JSONL有效、stdout无UI噪声 | CLI/公开runtime/预算超时都解析连续seq的实际JSONL |
| 22 | 默认测试无需网络或Key | 全量pytest离线；API需显式命令，不因.env存在自动联网 |
| 23 | 20任务fixture和独立评分 | 8bug/4feature/4cross-file/2test/2understanding；oracle及变异回归 |
| 24 | 区分真实成绩与未运行项 | 保留原19/20、旧回执与新来源收据；旧规则19/20、新规则20/20，0新模型调用 |
| 25 | README声明一致 | 同步预算/Windows/diff/指令/分发/评分范围，详见最新PROGRESS检查记录 |

全量离线结果：471 passed、4 skipped（206.27秒）；Ruff/格式/mypy通过。4项跳过为当前账户符号链接权限3项和POSIX权限1项。完整记录见[进度](PROGRESS.md)。

新增回归入口：

- `tests/integration/test_total_wall_budget.py`：慢基线/真实慢MCP/同步恢复、共享deadline、清理后结果、chat初始化和用户等待。
- `tests/unit/test_diff_baseline_edges.py`：所有观察前后比较Git index及工作区字节。
- `tests/integration/test_instruction_approval.py` 和 `tests/unit/test_registry_authorization_hooks.py`：授权窗口根/子目录指令变化，读写与命令及回调恢复。
- `tests/unit/test_win32_job.py`：Job创建、后代回收、breakaway、取消和失败边界。
- `tests/unit/test_command_wrappers.py`、`tests/integration/test_command_wrapper_cleanup.py` 和 `tests/unit/test_changes_observation_failures.py`：内部查询保留终态与证据，未知观察不折无Git继续。
- `tests/integration/test_eval_provenance.py`：来源字节校验、规则差异、拒覆盖、漂移与篡改检测。
- `tests/integration/test_packaging.py`：合成私有状态的sdist与直接wheel内容检查。

分发检查包含源码包和wheel实际构建、资源清单扫描及隔离加载smoke。sdist保留源码/测试/文档/评测夹具/示例/模板，排除IDE、真实环境文件、数据库、私钥与本机验证收据；完整收据仅留在本地的 `docs/validation`，不随 GitHub 源码或 sdist 提供；公开范围见[验证摘要](validation/README.md)。

应用层授权、工作区锁、Job Object和oracle位置不能替代容器/低权限OS隔离。POSIX进程组不能保证回收主动setsid逃组的子进程，Windows外部broker代启也不属于Job继承树。精确tokenizer、DockerBackend、其他平台实跑、CI和评测比较是明确后续工作，不写成已经完成。
