# 小型评测实际结果

本页保留历史测量及评分修订说明。原始报告、工作区与来源收据仅留在本地，不随 GitHub 源码公开；见[公开验证摘要](../docs/validation/README.md)。本页和摘要不能代替可重评分的原报告及运行工作区。

日期：2026-10-25。平台：Windows，Python 3.12.6。真实模型：DeepSeek `deepseek-flash`，`https://api.deepseek.com`，Chat Completions SSE，关闭thinking。没有估算货币费用。

## 完整20题真实运行

显式执行：

```powershell
uv run python -m stellarweaver.evals run --provider deepseek --env-file .env --output .validation/m5-live-20
```

20题全部实际运行，每题固定临时Git工作区、精确文件和unittest授权、最多12次模型/40次工具/180秒。仓库理解题只读；Key不进入模型提示、工作区或命令环境。

| 指标 | 实测 |
| --- | --- |
| 运行次数 | 每题1次，20/20，无跳过 |
| Agent运行状态 | 20 completed、0 blocked、0 cancelled |
| 初始独立评分 | 19/20，Q01因评分范围错误被判失败 |
| 修正范围后的零模型重评分 | **20/20** |
| 有效diff | 18/18需要修改的任务 |
| 实际执行指定验证 | 18/18需要验证的任务 |
| 工具错误 | 2/117；拒绝与取消不计入此分母 |
| 模型请求 / 工具调用 | 93 / 117 |
| API报告输入 / 输出token | 319974 / 16757 |
| usage_estimated / usage_incomplete | 所有任务均false / false |
| 整套耗时 | 219.110秒，含夹具准备与独立评分 |
| 单任务Agent耗时平均 / 中位数 | 9940.8 / 10328毫秒 |

保存了原始19/20报告（本地归档 `docs/validation/m5_live_report.json`）与零模型重评分回执（本地归档 `docs/validation/m5_live_rescore.json`），没有把原始失败记录改成成功。Q01要求验证config.py/resolve和配置优先级；初版评分器额外要求读取仅作re-export的target.py，超出了题目相关证据范围。模型确已读取config.py、报告正确SHA与顺序。评分器改为只要求config.py实际读取证据，增加对应回归后，对保存工作区和原工具结果重新评分；未产生额外模型请求。Q02仍要求其真实两文件链路证据。

| 类别 | 任务 | 重评分成功 |
| --- | --- | --- |
| Bug | B01–B08 | 8/8 |
| 小功能 | F01–F04 | 4/4 |
| 跨文件 | X01–X04 | 4/4 |
| 测试补充 | T01–T02 | 2/2，均捕获指定变异 |
| 仓库理解 | Q01–Q02 | 2/2，事实锚点和源码hash规则 |

这是固定小夹具集的一次测量。成功同时要求行为oracle、允许修改范围合格和Agent正常结束；不是模型“完成”文本自评。理解题采用明确事实锚点与读取hash规则，不宣称自然语言语义评分完备。结果不能外推到大型仓库或SWE-bench，也没有显著性结论。

## 前置试跑与离线验证

正式运行前的4题pilot为4个行为oracle通过、3个Agent追加未授权命令被blocked、1个正常完成。完整成功口径为1/4，不能称4/4闭环成功。后续明确提示唯一可执行argv、用内置git_diff查看变化，以及理解题只读；没有扩大命令授权来绕过失败。

离线20题报告（本地归档 `docs/validation/m5_offline_report.json`）为20/20参考动作脚本通过，18/18有效diff与验证执行。模型决策来自预设FakeModel，只检验真实工具、协议、持久化、oracle和指标链路，不作为模型编码成绩。默认pytest中也完整运行这20题与独立重评分、变异和评分绕过回归。

另外修复并保留回归：候选模块提前SystemExit(0)不能跳过oracle后冒充成功；oracle执行后重新检查越界变化；未知/空任务评分不返回0/0成功；拒绝/取消不稀释工具错误率。评分oracle在文件工具工作区外，但Host下可执行代码仍有本机权限，不是恶意代码隔离环境。

## 未运行与后续工作

- 没有重复20题多次采样，没有bash-only与结构化工具、关闭/开启repo-map或压缩的对比实验。
- 鲁棒性故障注入单独由pytest报告，不混入20题成功率，当前报告recovery_rate保持null并指向故障测试。
- 没有Linux/macOS实机结果；CI文件已建立但未提交、推送或触发GitHub运行。
- 更强 oracle 隔离与 Docker 执行后端仍待实现。后续验收修复已加入 Windows Job Object，并通过父进程先退出、脱离控制台及重复取消的真实进程测试；范围限于受管的 CreateProcess 后代，外部 broker 代启不在范围内，也不是文件系统沙箱。此修复未重新运行真实模型评测。
## 验收修复后的来源补齐

2026-10-25 使用保存工作区和原工具证据，以当前评分器分别执行旧规则与修正规则：旧规则19/20、当前规则20/20，新增模型调用均为0。新旧规则收据（本地归档 `docs/validation/audit_fixes_old_rules_v2.json`）与当前规则收据（本地归档 `docs/validation/audit_fixes_current_rules_v2.json`）记录原报告hash、实际catalog hash、评分器版本及按内容hash归档的评分源码。原始报告与旧重评分回执的字节保持不变。

旧catalog经过CRLF/JSON格式精确重建，SHA-256为`14a8d31d6ef372cb27e12943f0f00e8bd2cc90edead76a9c0e60d56e751ce6ef`，与原报告完全一致；当前规则hash为`092f952d8a83b17606a6082da01f16e6999990a80a1bf4d0f5bdb86d9c318df0`。旧评分Python当时未归档，新增收据只声称当前评分器加旧规则的复现，不声称找回旧实现。规则档案见[provenance](provenance/README.md)。这次没有重新运行真实模型，不将历史成绩当成修改后全栈的新实测。

来源捕获另有并发修改回归：实际任务从同一份catalog原始字节解析，身份捕获或归档间发生漂移即拒绝运行，不把旧任务对象标成新规则版本。v2收据记录此修复后的实际评分源码；先前收据继续保留。
