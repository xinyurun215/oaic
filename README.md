# OAIC · GOSIM Factory26 决赛公开包

本仓库是 **GOSIM Factory26 智能体软件工厂黑客松决赛** 作品的公开整理版：可运行的本地 Racer（工程执行器）源码、赛题总档案，以及最终打分提交的 Agent 包。

## 决赛提交包（Final score package）

- 压缩包：[`artifacts/final-score-package/final-score-package.zip`](artifacts/final-score-package/final-score-package.zip)
- SHA-256：`06f93649d597c61bf59be7ce9fb161d599769a5c57e7705e46ea22ae8fca5186`（见同目录 `.sha256`）
- 已解压源码树：[`final-score-package/`](final-score-package/)（含 `main.py`、`arcbench-agent-runtime/`、`skills/`、`template/`、`PACKAGE_MANIFEST.json` 等）

该 zip 即为平台打分使用的 Agent 交付物。

## 如何运行 Racer（本地确定性执行器）

需要 CPython 3.12+，仅标准库。

```text
python -m racer run --job <job.json> --state-dir <directory>
python -m racer resume --job <job.json> --state-dir <directory>
python -m racer reset --job <job.json> --state-dir <directory>
python -m racer trace --job <job.json> --state-dir <directory>
```

`run` / `resume` 可加 `--interrupt-before <stage-id>` 做确定性中断模拟。成功结果只写 stdout，失败只写 stderr。退出码：`0` 成功，`2` 输入，`3` 状态/检查点，`4` 阶段失败，`70` 内部错误，`130` 模拟中断。

`trace` 只读：校验 job 与已有 checkpoint，不创建或改写状态。

## 仓库地图

- 赛题总档案：[`docs/competition/GOSIM-Factory26-赛题总档案.md`](docs/competition/GOSIM-Factory26-赛题总档案.md)
- Racer 源码：[`racer/`](racer/)
- Racer 测试：[`tests/`](tests/)
- 决赛 Agent 包：[`final-score-package/`](final-score-package/) 与上述 zip

## Racer 能力边界

Racer Core Phase 1 是本地确定性 runner：fixture stages、持久 checkpoint、生命周期恢复、只读 trace。它不提供比赛平台适配器、网络访问、LLM 调用、ARC 集成或分布式多写安全。Phase 1 为单写者；revision 检查能发现期望版本冲突，但不是完整的多进程锁。

## Authority boundary

**research != authoritative specification**

权威赛题事实、证据等级与待确认项只维护在 `docs/competition/` 下。
