# 轻量工程记忆 / 失败指纹去重交付报告

## changed files

- `main.py`
- `tests/test_engineering_memory.py`
- 本地模拟证据：`evidence/engineering-memory-simulation/receipt.json`、`evidence/engineering-memory-simulation/repair-replay-3-receipt.json`

候选目录没有提交 Git，也没有调用平台或真实模型。

## 最小数据结构与读写位置

记忆文件是输出目录下可选的 `.arc/engineering/memory.json`，结构为 `{version: 1, entries: [...]}`。每条 entry 保留阶段/模块、`error_fingerprint`、错误类型/关键消息/来源阶段、需求 ID 与输入摘要、源码摘要、根因、修复、验证证据、适用和不适用范围、`run_id` 与状态 `candidate|verified|stale|rejected`。`run_id` 只用于同一运行去重；源码摘要和模块必须同时匹配。

- 编码模型请求前：`engineering_memory_hints()` 读取当前源码摘要和模块匹配的最多 3 条 candidate/verified 提示；提示是非权威上下文，当前需求和可信 checkpoint 优先。
- 模型/观察修复失败后：生成稳定指纹，写入 candidate；同一运行再次遇到相同源码摘要、模块和指纹时跳过重复模型请求，记录 `duplicate root cause already known; model request skipped`。
- 修复后冻结复验通过：现有修复回合把同一条 candidate 升级为 verified，并写入根因、修复说明及 before/after 验证证据。
- 缺失、损坏或写入失败：读作空记忆或忽略写错，不成为前置门禁。普通 generation report 和 summary 只记录路径、状态、条目数与动作。

## 新增测试与结果

- `python -B -m unittest tests/test_engineering_memory.py -q`：5/5 通过。
- `python -B -m unittest discover -s tests -p 'test_first_batch_v6.py' -q`：21/21 通过。
- `python -B -m compileall -q main.py engineering.py tests arcbench-agent-runtime skills`：通过。
- `python -B -m py_compile main.py tests/test_engineering_memory.py`：通过。

相关回归集合曾完成 77 通过；其中 2 个 `test_engineering_handoff.py` 夹具失败在本轮改动前已存在，未触碰其 checkpoint/handoff 逻辑。

用户指定的 v6(7) 57/57 与 compileall 基线证据保留为基线口径；本轮新增代码没有放松 checkpoint、preflight、identity、manifest 或 integrity-stop。

## 本地模拟

输入是候选测试夹具中的依赖完整 ROOT 两模块需求（REQ-1、REQ-2），模型、构建和启动均使用现有离线替身，没有阶段 23 的缺失平台依赖。

- 完整成功流：2 次模型线程，generation `complete`，delivery `ready`，acceptance `passed`，功能链 4 passed / 0 failed / 0 unverified，记忆文件为空。
- 重复失败流：2 次模型线程；REQ-2 首次错误为 `Connection reset by peer`，第二次相同源码摘要和指纹被跳过。generation `blocked`、delivery `partial`、acceptance `unverified`，并保留 1 条 candidate 指纹；既有 preflight integrity-stop 仍生效。
- 修复复验流：3 次模型线程，最终 4 passed / 0 failed / 0 unverified；REQ-1 的 candidate 升为 verified，根因为 `Assertion failed`，修复为 bounded observation repair，证据包含 before/after 路径。

收据：`evidence/engineering-memory-simulation/receipt.json`、`evidence/engineering-memory-simulation/repair-replay-3-receipt.json`。

## 外部阻塞、未验证项与下一步

阶段 23 的 `REQ-1-1-2` 缺失是平台输入/阶段依赖阻塞，已单独保留为外部阻塞；没有改依赖校验、添加兜底、猜测或伪造节点。没有平台提交、真实模型调用或平台分数提升证据；本地通过数不代表平台分数。全目录 compileall 若把本地模拟生成的 recovery snapshot 一并纳入，会遇到快照内 pycache 写入错误；源代码范围 compileall 已通过。下一步仅在获得新输入或明确授权后再做平台操作。

## 产物摘要

- `main.py` SHA-256：`56D49F547A9F0492B6E43213C408156832FD6E383444EF52B5111A9D90DCB4A2`
- `tests/test_engineering_memory.py` SHA-256：`489C0F4B5FE9EC97748A4F84B10245667A32563EC9015B19FBADC44079C18448`
- 本报告 SHA-256：未填（自包含摘要会改变自身哈希）


## Final package

- ZIP: `D:/oaic/artifacts/V8/V8.zip`
- SHA-256: see `package-manifest.json` and final delivery message (kept outside the self-referential report)\n- ZIP contains 72 candidate files plus `PACKAGE_MANIFEST.json`; local simulation evidence and caches are excluded.



