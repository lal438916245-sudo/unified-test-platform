# CI 第一阶段（L0 + L1）

本目录只放 **CI 专用**脚本。GitHub Actions 定义在 `.github/workflows/platform-ci.yml`。

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
