# CI 第一阶段（L0 + L1）

本目录只放 **CI 专用**脚本。仓库：**`unified-test-platform`**。
GitHub Actions 定义在 `.github/workflows/platform-ci.yml`。

## 目标（本阶段只做这些）

```
GitHub Actions
    ↓
Windows self-hosted runner
    ↓
统一测试平台
    ↓
L0 平台代码级测试
    ↓
L1 平台真实启动 + Demo Plan 执行
    ↓
Report / PlanRun 结果
    ↓
CI Pass / Fail
```

**明确不做**：L2（真实 PrivaHigh pytest / MatchEval）、L3（Locust 打 :2567）、
Colyseus / MongoDB 启动、Worker、队列、CI Dashboard、RBAC、参数化矩阵、
Suite 执行、`get_conn` 技术债修复。

## 验收结果（CI Phase 1 · 2026-10-06 收口）

| 项 | 值 |
|---|---|
| Run | **#8**（`workflow_dispatch`，手动触发） |
| commit | **`caf85bf`** |
| Runner | **`lenovo-win11-dev`** · Windows self-hosted（标签 `self-hosted, Windows, X64`） |
| workflow 结论 | **Success** |
| L0 Job | 8/8 steps success |
| L1 Job | 9/9 steps success |
| **L0 结果** | **94 passed / 0 failed** |
| **L1 结果（CI 实际）** | **36 passed / 0 failed** |
| **L1 结果（本机完整）** | **42 passed / 0 failed** |
| 产物 | `l1-smoke-logs` 上传成功 |
| Runner 清理 | 无残留 `run.py --data-dir` 进程 · 无残留 CI 临时目录 |
| 真实库 | 全程未变：md5 `9c191da86caaad20bac5d621a0142cae` · 278528 B · mtime `2026-10-01 20:10:44` · plans=15 / plan_runs=34 / jobs=41 |

### 为什么 CI 是 36、本机是 42

差的 **6 项**是**针对开发机真实 `data/db.sqlite` 的保护性断言**：
执行前后必须不变的有——真实库 **md5**、**mtime**、**数据目录文件数**、**`plan_runs` 计数**等。

`data/` 被 `.gitignore` 排除，**不进入 CI workspace** → CI 侧**没有真实库基线可比**，
这 6 项走脚本里的 `[跳过]` 分支（打印 `[跳过] 全新签出…`，不计为失败，也不算通过）。

因此：

- **本机（有真实库，完整比对）= 42 passed / 0 failed**
- **CI（全新签出、无真实库）= 36 passed / 0 failed**

两者都正确，只是**口径不同**。**禁止为了让数字统一而把 CI 的 36 写成 42。**

> 同理，workflow 里那两步「校验 L0/L1 未写真实库」在 CI 里比的是 `ABSENT vs ABSENT`，
> 属**平凡成立**；"真实库确实没被写"这点的真实守卫在**本机**（就是上表最后一行）。
> 这点必须如实记录，不可含糊。

## L0 与 L1 的分工

| 层 | 内容 | 外部依赖 | 入口 |
|---|---|---|---|
| **L0** | `pytest backend/tests`（约 94 条，进程内 `TestClient`） | 无（不需要游戏/服务端/MongoDB/真实素材/真实库/外网） | workflow 直接调 pytest |
| **L1** | 真实启动 `backend/run.py` → 跑一个自包含 Demo Plan → 断言 PlanRun/Job/report.json/Report Center | 无（只跑 `backend/demo/test_platform_demo.py`） | `_ci/l1_smoke.py` |

L0 证明"代码是对的"；L1 证明"把平台真的跑起来、真的执行一个计划、真的产出报告"。

## 本地手动运行（与 CI 完全相同的命令）

```powershell
# L0
python -m pytest backend/tests -q -o addopts=

# L1（会自动选空闲端口、建临时数据目录、跑完清理）
python _ci/l1_smoke.py

# L1 排障：保留临时数据目录，便于看现场
python _ci/l1_smoke.py --keep
```

环境变量（可选）：

| 变量 | 用途 |
|---|---|
| `PLATFORM_CI_PYTHON` | 指定解释器；不设则用当前 `sys.executable` |
| `PLATFORM_NODE` | 指定 node（前端往返测试台用）；不设则查 PATH |
| `NO_PROXY=127.0.0.1,localhost` | **建议设置**：本机有 `http_proxy`，否则 127.0.0.1 会被送去代理（502） |

## self-hosted runner 要求

1. **Windows**，标签需同时含 `self-hosted` 与 `Windows`。
2. `python` 在 PATH 上，且装有：`fastapi` `uvicorn` `pydantic` `pytest` `httpx`
   （L0 的环境自检步骤会显式验证，缺任何一个都直接失败）。
   也可用仓库变量 `PLATFORM_CI_PYTHON` 指向别的解释器。
3. **无需** MongoDB / Colyseus / 游戏 / 真实游戏素材 —— L0/L1 都不依赖。
4. node 可选；缺失时前端往返测试台会自动 skip（不计为失败）。
5. 不需要 `pwsh`（PowerShell 7）：workflow 全部步骤用 `shell: powershell`（5.1，Windows 自带）。

## 本机 runner 实况（2026-10-05 注册 · 2026-10-06 Run #8 实跑通过）

| 项 | 值 |
|---|---|
| 位置 / 版本 | `F:\actions-runner` · runner **2.337.0**（win-x64 官方包） |
| 名称 | `lenovo-win11-dev`（`agentId=2`） |
| 标签 | `self-hosted, Windows, X64` —— 即默认标签集，命中 `runs-on: [self-hosted, Windows]` |
| 服务 | `actions.runner.lal438916245-sudo-unified-test-platform.lenovo-win11-dev` · `Auto` 启动 · 账户 `NT AUTHORITY\NETWORK SERVICE` |
| 状态 | `Running`；`_diag` 日志显示 `Listening for Jobs` |
| 实跑验证 | **Run #8（`caf85bf`）Success** —— L0 8/8、L1 9/9 步骤全绿（见上「验收结果」） |

安装命令（管理员权限一次性完成注册 + 装服务，本版本已无 `svc.cmd`，改用 `--runasservice`）：

```powershell
F:\actions-runner\bin\Runner.Listener.exe configure --unattended `
  --url https://github.com/lal438916245-sudo/unified-test-platform `
  --token <注册token> --name lenovo-win11-dev `
  --labels self-hosted,Windows,X64 --work _work --replace --runasservice
```

要点 / 踩坑：

- **服务账户是 `NETWORK SERVICE`，只取机器级 PATH**。本机机器 PATH 含 `F:\Anaconda3` →
  workflow 默认的 `PYTHON_EXE=python` 能解析到 `F:\Anaconda3\python.exe`（fastapi/uvicorn/pydantic/pytest/httpx 齐全）。
  若换机器且解释器只挂在**用户级** PATH 上，服务会找不到 → 需设仓库变量 `PLATFORM_CI_PYTHON` 为绝对路径。
- **改配置要先 remove**：runner 拒绝在「已配置」状态下重复 `configure`
  （报 `Cannot configure the runner because it is already configured`）→ 先 `bin\Runner.Listener.exe remove --local` 再配置。
- 生成物 `.runner` / `.credentials` / `.credentials_rsaparams` / `_diag` / `_work` 均在 `F:\actions-runner` 下，
  **不在仓库内**，不受 `.gitignore` 影响；`_diag\Runner_*.log` 不会记录注册 token（已实测）。
- ⚠️ **该仓库是公开仓库**，而 GitHub 官方不建议在公开仓库上挂 self-hosted runner（分叉 PR 可在你机器上执行代码）。
  当前 workflow 只有 `workflow_dispatch`（需写权限才能触发），风险可控；**一旦以后加 `push` / `pull_request` 触发器，须先评估**。

## 隔离契约（L1 每次执行都会自证）

L1 存在的首要理由是**防止再次发生"把遗留实例误认成自己启动的实例"**
（2026-10-01 曾因此把 9 条 PlanRun 写进了真实库）。因此它必须能证明：

1. **Port** = `socket.bind(0)` 动态取到的空闲端口（启动前断言不可达），**绝不是 8000**；
2. **Data Dir** = 本次新建的临时目录，**绝不是 `platform/data`**；
3. **PID** = 本次 `Popen` 出来的进程（OS 级 `OpenProcess` 确认，不用会静默返回空的 `tasklist`）；
4. 子进程**自己 stdout 声明**了它绑定的端口 / 数据库 / 数据目录（读它的原话，不靠推测）；
5. 执行完成后：临时库有本次记录，而**真实库 md5 + mtime + 文件数 + plan_runs 计数全部不变**；
6. 收尾强杀进程、按唯一标记扫描残留、确认端口释放、删除临时目录。

## 产物

- `_ci_out/<时间戳>/`（已 gitignore）：平台 stdout、Job 的 report 目录、`ci-meta.json`。
  CI 里由 `actions/upload-artifact` 上传为 `l1-smoke-logs`。
- 退出码：`0` = 全部通过；`1` = 任一断言失败（CI 据此判红，**绝不用 `continue-on-error` 掩盖**）。
