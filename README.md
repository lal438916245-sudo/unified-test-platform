# 统一游戏测试平台 MVP

> **仓库名：`unified-test-platform`** · 分支 `main` · 仓库根 = 本目录（`platform/`）。
> 中文产品名「统一游戏测试平台」表达**产品定位**；仓库名刻意不带 `game`，
> 因为平台的受控登记层对任意被测系统通用（已登记 3 个 SUT，其中 `GameAutoTest-Pro` 本身不是游戏）。

一个**三引擎测试薄壳**：只管 **测试计划编排 / 独立子进程执行 / 统一报告 / 环境与资源配置**，
本机单机工具，FastAPI 后端 + 单页前端。

**不做**（产品边界）：用例编辑器、脚本在线编辑、鉴权与多租户、分布式 Worker、专项大屏。
**不重写**被测引擎：pytest、Locust、`match_eval.py` 原样调用，只做受控参数注入与结果归档。

核心模型：`Plan → PlanRun → Job → Report`。
`Plan` 可编辑；`PlanRun` 固化 `plan_revision` + `plan_snapshot`；`Job` 固化脱敏 `config_snapshot`。
**历史记录与原始 `report.json` 不会被后续的计划/配置编辑改写。**

---

## 1. 当前支持的能力

| 能力 | 说明 |
|---|---|
| 三引擎 | `pytest`（功能/API）、`locust`（压测）、`matcheval`（UI 视觉识别评估） |
| 计划编辑器 | 新建/编辑/复制、步骤增删与上下移、三引擎专用表单、**无副作用预检**、一键执行；不提供删除计划 |
| 版本快照 | 编辑计划 `revision+1`；执行时固化计划快照与脱敏配置快照，历史不漂移 |
| 任务控制 | 串行执行、`fail_fast`、`queued/running/cancelling/success/failed/cancelled/timedout/skipped`、取消、超时看门狗、重试、Windows 进程树回收、Executor 独占锁 |
| 报告中心 | 按 job 维度索引（筛选/分页/趋势/两次报告对比），计划名取执行时快照；对比仅允许同引擎 + 同 schema 主版本 + 同环境等条件的报告 |
| 配置中心 | **Environment**（协议/host/port + secret 引用名）、**Executor**（受控解释器引用 + 受控工作目录根 + 允许的 Runner）、**AssetSource**（受控 dataset / 场景 key）；增改/停启/低风险预检；密钥只存**引用名**，前端、日志、报告均脱敏 |

### 已登记的被测系统

| 被测系统 | 使用的引擎 | 说明 |
|---|---|---|
| **PrivaHigh**《私立高中校长》 | pytest + matcheval | Unity 6 客户端 + 自带 FastAPI 后端（随机端口，游戏启动时自动拉起）；后端 `tests/` 49 条；实机采集 5 场景 / 23 模板 |
| **GameAutoTest-Pro** | pytest | 纯 API 冒烟测试；**需要被测服务端在跑**，否则诚实归档为 `failed` |
| **Colyseus-Storm** | locust | Colyseus 联机压测脚本；**需要 `localhost:2567` 服务端** |

> ⚠️ **口径硬约束**：`PrivaHigh` 与 `ColyseusTechDemo-MMO` 是**两个不同的被测系统**，
> 不得表述为「同一产品的真实三引擎验收」。两者各自建立独立计划；
> 只有在拿到**同一个被测系统**的三类资产（功能 + 压测 + 视觉）时，才可在一份计划里同时跑三引擎。

---

## 2. 从启动平台到完成一次测试（最短操作说明）

> 完全照着做即可完成一次演示，全程**不需要**任何外部服务或游戏。

**Step 0 · 启动**

```powershell
# 前置：使用含 fastapi / uvicorn 的 Python（本机为 F:\Anaconda3\python.exe）
F:\Anaconda3\python.exe backend\run.py            # 默认 127.0.0.1:8000
# 其他方式：start.bat ／ powershell -File start.ps1
# 可选：--port 8123 --no-browser ；隔离数据目录 --data-dir <dir> 或环境变量 PLATFORM_DATA_DIR
```

启动后浏览器打开 <http://127.0.0.1:8000>。首次启动会自动建库并预置配置实体与演示计划（幂等，已有则跳过）。

**Step 1 · 看「资源与环境」→ 引擎就绪度**（右上角导航第三个入口）

先确认三个引擎的状态是「可运行」。若显示「缺少配置」，下方直接给出**缺失项**与**下一步**，
例如「为对应 Runner 安装缺失依赖（locust / opencv-python + numpy），或改用已装好依赖的解释器引用」。

**Step 2 · （可选）认识三类配置**

同一页面下方依次是 **Environment** / **Executor** / **AssetSource** 三块：新增、编辑、停用/启用、预检。
所有路径与密钥类字段都是**受控下拉或引用名**，界面无法填写任意路径。

**Step 3 · 回到「测试计划」，点某个计划的「预检」**

预检**零副作用**（不建执行记录、不拉子进程）。结果包含：每步的四格就绪标记
（引擎已注册 / 参数合法 / 执行机允许 / 依赖就绪）、三类配置就绪度、锁与串行提示、
`fail_fast` 预期行为、**缺失项**与**下一步**、以及非敏感指纹。

**Step 4 · 点「一键执行」（或编辑器里的「保存并执行」）**

**Step 5 · 执行详情页看结果**

每步一张 Job 行：状态、尝试次数、退出码、**日志**、取消、重试、报告链接（有报告时）。
页面上的计划名与版本取自执行时的不可变快照。

**Step 6 · 点某 Job 的「报告」看报告详情**

指标卡 + 产物下载（junit.xml / CSV / 热力图等）+ locust / matcheval 专项面板 + 原始 `report.json`。
页面右上角的 `执行 #N` 可跳回该报告所属的运行详情。

**Step 7 · 「报告中心」看历史与对比**

按计划/引擎/状态/环境/时间筛选与分页；每行有「所属执行」可跳回对应 PlanRun。
选两份报告作**基线**与**候选**即可进入对比视图（若不兼容，页面会列出具体原因）。

### 演示计划与适用条件

预置计划共 **12 个**（Locust / MatchEval 相关计划**仅当对应的受控解释器可用时**才会预置）：

| 计划 | 需要什么 | 预期结果 |
|---|---|---|
| 平台自检 (green demo) | 无外部依赖 | `success` |
| 冒烟+Locust(fixture 压测) | 平台自身在跑即可（Locust fixture 打平台自己的 API） | `success` |
| MatchEval(小样本 fixture 评估) | 无外部依赖（隔离 fixture 素材） | `success` |
| 三引擎组合(pytest+Locust+MatchEval) | 无外部依赖（三引擎 fixture 版） | `success` |
| GameAutoTest API 冒烟 (需服务端) | **需要 GameAutoTest-Pro 服务端** | 服务不可达 → `failed`（诚实归档） |
| Locust-Colyseus(需服务端 2567) | **需要 `localhost:2567`** | 服务不可达 → `failed`（诚实归档） |
| CTRL 成功 | 无 | `success` |
| CTRL 失败 | 无 | **`failed`（故意失败，用于验证失败归档）** |
| CTRL 长跑(可取消) | 无 | 运行中点「取消」→ `cancelling → cancelled` |
| CTRL 超时 | 无 | `timedout`（4s 看门狗强杀进程树，**故意设置**） |
| CTRL 重试 | 无 | 失败后点「重试」→ 新 Job `success` |
| CTRL 互斥 | 无 | 两个独占步骤串行，锁不泄漏 |

> 名字里写「需服务端」的计划**跑失败是正常的**——那是被测服务没起，不是平台坏了。

---

## 3. 已验收能力（有实际证据）

| 能力 | 证据 |
|---|---|
| **pytest 平台执行链路** | PlanRun #20：`Job#24` pytest 真实子进程执行，junit + `report.json` 归档，`exit_code=1` 如实记录；`fail_fast` 正确把后续步骤置 `skipped` |
| **PrivaHigh pytest 真实功能测试** | `PrivaHigh\backend\tests\` **49 passed / 0 failed / 0 errors / 0 skipped**（1.62s，本地直跑，2026-09-29 修复后） |
| **PrivaHigh MatchEval 真实资产链路** | PlanRun #21 / `Job#26`：`success`、`exit_code=0`、30s；**TP=23 / FP=0 / FN=0 / TN=92，Precision=Recall=F1=1.00**，与旁路冻结基线**零差异**；CSV 690 行、热力图 23 张、产物 114 个 |
| **Locust 真实执行链路** | `Job#29` / `Job#30`（基于 **Colyseus-Storm 真实服务**）：每次 **6 局 complete games**、**0 个 `WinError 10053`**、**0 个 Locust failures**。用于验证 **Locust → WebSocket → Colyseus 游戏服务 → 游戏完成 → 测试结果回传** 这一**真实执行链路**；**不作为容量压测 / 性能基准（benchmark）** |
| **报告生成** | `report.json` 版本化 schema，通过 `validate_report()`；`data/reports/run_{id}/job_{id}/` 归档 |
| **Report Center** | `report_index` 派生索引：筛选、分页、趋势、两次报告对比；计划名取执行时快照 |
| **配置中心** | 三类实体 CRUD + 预检 + 启停；`privahigh-real` 资产源预检 6/6 通过；执行机预检含依赖探测 |
| **Precheck** | `POST /api/plans/precheck`（草稿）与 `/api/plans/{pid}/precheck`（已保存），`no_side_effects=true`，缺失项与下一步可行动 |
| **Plan / PlanRun / Job / 快照** | 计划编辑 `revision` 递增；执行固化 `plan_snapshot` + `config_snapshot`；历史 `plan_runs#1–19` / `jobs#1–23` 与执行前备份**逐行一致**（未被改写） |
| **隔离验证** | `backend/tests` **94 条单测**（隔离临时库 / 独立数据目录，含前端步骤往返契约）；另有 7 个开发期自验收脚本 `verify*.py`，**不随仓库发布** |

---

## 4. 未完成真实验收（不要当成已完成）

- **Locust 容量压测 / 性能基准尚未做**：Locust 的**真实执行链路已完成验收**（见 §3，`Job#29` / `Job#30`：基于
  **Colyseus-Storm 真实服务**，每次 **6 局 complete games**、**0 个 `WinError 10053`**、**0 个 Locust failures**）。
  但请严格区分口径：该验收**只验证** `Locust → WebSocket → Colyseus 游戏服务 → 游戏完成 → 测试结果回传`
  这条**真实执行链路**是否打通，**不作为容量压测结论，也不作为性能基准（benchmark）**。
- **Locust WS 地址仍需改造**：`Colyseus-Storm/locustfile.py` 第 84 行的 WebSocket 地址**硬编码**为
  `ws://localhost:2567`（平台的 `--host` 覆盖不到 WS 段，脚本也不读任何环境变量）→
  **若真实服务不在本机 2567，必须先授权改造为读取受控配置，否则结果不可信**。
- **尚未完成同一被测系统的 pytest + Locust + MatchEval 三引擎闭环**：
  当前三类资产分属两个被测系统（PrivaHigh 有功能 + 视觉、Colyseus 有联机脚本），
  PrivaHigh 缺「压测场景」这一环。
- **真实数据上的报告对比 / 趋势尚未验证**：对比功能的代码与隔离验证都在，但**没有在两个真实运行之间做过 A/B**。
- **修复后的 PrivaHigh pytest「49 passed」尚未经平台链路留档**：平台侧最新记录仍是 PlanRun #20 的 46 passed / 3 failed。
- **平台 verify 基线自白名单登记（2026-09-29）后尚未复跑**。

---

## 5. 已知限制 / 技术债

| 项 | 说明 |
|---|---|
| 运行历史入口 | 「最近执行记录」默认 8 条、可点「加载更多」逐批展开；无筛选。**没有报告的运行**（skipped/cancelled）只能在执行记录里看到 |
| Locust 真实服务 | 真实执行链路**已验收**（§3）；WS 地址仍硬编码 `ws://localhost:2567`，容量 / 性能基准未做（见 §4） |
| verify 脚本不随仓库发布 | 7 个 `verify*.py` 是**开发期自验收脚本，不在本仓库内**（依赖本机真实服务/资产，不宜作为产品交付物）：其中 `verify.py` / `verify_control.py` 假定 **:8000 已有服务**，`verify_locust.py` / `verify_matcheval.py` 假定**非 8000 端口的隔离实例**已运行，其余 3 个自包含 |
| 环境依赖 | 平台默认解释器与真实项目根是本机路径，可通过环境变量覆盖（见下表） |
| 解释器依赖缺口 | 无 `pytest-html`（跑 GameAutoTest-Pro 用例必须加 `-o addopts=`，预置选择器已带）；无 `pymongo`（其账号清理夹具会静默跳过） |
| AssetSource 语义 | 资产源**不决定执行内容**：只在步骤未填 `dataset` / `locustfile` 时兜底注入，且 kind 与引擎不匹配时静默跳过 —— **步骤参数优先** |
| 动态端口目标 | 目标是「被拉起的进程用随机端口」时（如 PrivaHigh 由游戏自动拉起），平台没有端口发现机制，只能**固定端口手动启动**后再测 |
| 路径限制 | 参数里的路径必须在受控目录根之下；绝对路径、盘符、`..`、shell 元字符一律 400 拒绝 |
| 已知平台缺陷（未修） | 预检 `run_in_progress` 会因历史 `timedout` 记录误报（仅显示层，不拦执行）；Job#24 曾出现约 69s 退出延迟（根因未确证，未复现） |
| 未纳入 MVP | UI 交互自动化（Airtest poco 未被平台接管）、分布式 Worker、鉴权、多租户 |

### 环境变量（可覆盖本机路径）

| 变量 | 作用 |
|---|---|
| `PLATFORM_DATA_DIR` | 数据目录（db / logs / reports），用于隔离实例 |
| `PLATFORM_DB` | 直接指定 SQLite 文件 |
| `PLATFORM_DEFAULT_PYTHON` | 覆盖默认解释器（`config_center.DEFAULT_PYTHON`） |
| `PLATFORM_SDET_PROJ` | 覆盖真实项目根（`config_center.SDET_PROJ`） |
| `PLATFORM_PYTHON` / `PLATFORM_LOCUST_PYTHON` / `PLATFORM_MATCHEVAL_PYTHON` / `PLATFORM_MATCHEVAL_SCRIPT` / `PLATFORM_AIRTEXT_DIR` | **已弃用**（过渡兼容层），命中时 `/config/catalog` 会提示迁移到配置实体 |

---

## 6. 依赖安装

```powershell
F:\Anaconda3\python.exe -m pip install -r requirements.txt
```

`requirements.txt` 只声明**平台自身**必需的包；Locust 与 cv2/numpy 属**特定 Runner 的按需依赖**，
已在文件内以注释标注（建议装在独立解释器中，并在配置中心登记为 Executor）。

---

## 7. 常用接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config/catalog` | 受控 key 目录（白名单枚举，前端下拉数据源） |
| GET | `/api/config/readiness` | 三引擎就绪度（可用执行机 / 缺失项 / 下一步） |
| POST | `/api/plans/precheck` | 草稿预检（零副作用） |
| POST | `/api/plans/{id}/precheck` | 已保存计划预检 |
| POST | `/api/plans/{id}/run` | 触发计划 → 返回 `run_id` |
| GET | `/api/plans` · `/api/runs?limit=&offset=` | 计划列表 · 运行记录（倒序，支持分页） |
| GET | `/api/runs/{id}` | PlanRun + Jobs |
| GET | `/api/jobs/{id}/logs` | Job 日志 |
| POST | `/api/jobs/{id}/cancel` · `/retry` | 取消（排队→终态；运行中→终止子进程）· 重试（新 job_id） |
| GET | `/api/reports/{job_id}` | report.json（落库） |
| GET | `/api/artifacts/{job_id}/{filename}` | 产物下载（junit.xml / CSV / 热力图等） |
| GET | `/api/report-center` · `POST /api/report-center/compare` | 报告索引（筛选/分页）· 两次报告对比 |

## 8. 数据结构

- SQLite：`data/db.sqlite`（`environments / executors / asset_sources / plans / plan_runs / jobs / reports / report_index / executor_locks`）
- 报告归档：`data/reports/run_{id}/job_{id}/report.json`（+ 引擎产物）
- 日志：`data/logs/job_{id}.log`

## 9. 验证

```powershell
# 1) 平台单测（自包含，隔离临时库，不需要服务）—— 仓库内的标准回归
F:\Anaconda3\python.exe -m pytest backend\tests -o addopts= -q          # 94 passed

# 2) 隔离回归测试台（自包含，不依赖外部服务与真实资产）
F:\Anaconda3\python.exe _v0.3_baseline\e2e_isolated.py                  # 隔离实例端到端 39 项
node _v0.3_baseline\fe_roundtrip_test.js                                # 前端步骤往返 54 项（已并入 pytest）
```

> ⚠️ 开发期另有一组自验收脚本 `verify*.py`（`verify.py`、`verify_config_center.py`、`verify_control.py`、
> `verify_locust.py`、`verify_matcheval.py`、`verify_plan_editor.py`、`verify_report_center.py`）：
> 它们依赖本机真实服务 / 真实资产 / 真实项目根，**按设计不随仓库发布**，
> 因此上述命令在本仓库内不可直接执行（对应证据已归档在平台报告里，不在本仓库）。

## 10. CI（GitHub Actions）· **CI Phase 1 已完成**

```powershell
# 与 CI 完全相同的本地命令
F:\Anaconda3\python.exe -m pytest backend\tests -q -o addopts=   # L0：平台代码级测试（94 passed）
F:\Anaconda3\python.exe _ci\l1_smoke.py                          # L1：真实启动 + Demo Plan 冒烟（本机 42 passed）
```

**状态：CI Phase 1 已通过真实 GitHub Actions 执行验收。**

| 项 | 值 |
|---|---|
| Run / commit | **#8** · **`caf85bf`** |
| Runner | `lenovo-win11-dev` · Windows self-hosted |
| 最终结论 | **Success**（L0 Job 8/8、L1 Job 9/9 步骤全绿） |
| **L0** | `pytest backend/tests` → **94 passed / 0 failed** |
| **L1（CI 实际）** | **36 passed / 0 failed** |
| **L1（本机完整）** | **42 passed / 0 failed** |
| 产物 | `l1-smoke-logs` 上传成功 |

**L0（平台代码级测试）**：`pytest backend/tests` → 94 passed。

**L1（平台真实启动 + Demo Plan 冒烟）**：Windows self-hosted runner → **动态端口** → **隔离临时数据库**
→ 启动**真实 FastAPI** → `GET /health` → **Demo Plan** → **PlanRun** → **Job** → **Report**
→ **清理进程** → **释放端口** → **清理临时目录**。

- 定义：`.github/workflows/platform-ci.yml`，**仅 `workflow_dispatch`**（当前触发方式），
  `runs-on: [self-hosted, Windows]`（不使用 `ubuntu-22.04`），workflow 级 `concurrency` 串行化。
- 探测端点：`GET /health` → `{"status": "ok"}`（零副作用，仅表示 HTTP 服务已就绪）。
- 隔离保证：L1 每次执行都自证**真实库 md5/mtime/文件数/`plan_runs` 计数未变**、无残留进程。详见 [`_ci/README.md`](_ci/README.md)。

> ⚠️ **口径**：`data/` 被 `.gitignore` 排除，不进入 CI workspace → CI 中那 6 项"真实库不变"断言走 `[跳过]`，
> 所以 **CI 的 L1 是 36 passed，本机完整跑是 42 passed**。两者口径不同但都对，**不可混写**。

**为什么暂不把 PrivaHigh / Locust 加为 PR Gate**：真实 PrivaHigh pytest/MatchEval（L2）与 Locust 打 `:2567`（L3）
**依赖真实游戏服务、真实资产与本地环境**（游戏客户端、`localhost:2567` 的 Colyseus 服务端、实机采集的视觉素材等），
在 CI 上无法稳定复现，因此**不作为 CI 第一阶段自动门禁**——它们仍可在本机通过平台按需执行（验收证据见 §3）。
