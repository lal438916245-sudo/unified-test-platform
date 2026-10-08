# 统一游戏测试平台：项目记忆

仓库名：**`unified-test-platform`**（分支 `main`）· 最后更新：2026-10-08

## 已确认的产品边界

平台是三引擎测试的薄壳：只管理 **测试计划编排、独立子进程执行、统一报告、环境与资源配置**。不重写 pytest、Locust 或 `match_eval.py`，不做平台内用例编辑器、复杂权限、分布式调度或营销式大屏。

核心模型是 `Plan -> PlanRun -> Job -> Report`。Plan 可编辑；PlanRun 固化 `plan_revision` 和 `plan_snapshot`；Job 固化脱敏 `config_snapshot`。历史记录与原始 `report.json` 不得被后续计划/配置编辑改写。

## 已完成能力（均已隔离验证）

- 三个 Runner：`pytest_runner`、`locust_runner`、`matcheval_runner`；均通过独立子进程执行。
- 任务控制：串行、fail-fast、`queued/running/cancelling/success/failed/cancelled/timedout/skipped`、取消、超时、重试链、Windows 进程树回收、Executor 锁。
- `report.json` 采用版本化 schema；保留 `engine_data_schema` 与引擎专项数据；产物使用带名称、类型、路径、大小的对象。
- 报告中心：SQLite 派生 `report_index`，筛选、分页、趋势、两次报告对比；只允许相同引擎、schema 主版本、环境和关键输入的报告进行有效对比。
- 环境与资源配置中心：Environment、Executor、AssetSource；只提交受控 ID/key；敏感值仅可使用环境变量名/外部 secret 引用，前端、日志、报告均脱敏；支持低风险预检。
- 测试计划编辑器：新建、编辑、复制、步骤排序、三引擎专用表单、无副作用预检、一键执行。当前不提供删除计划。

相关验证曾全绿：`verify.py`、`verify_control.py`、`verify_locust.py`、`verify_matcheval.py`、`verify_report_center.py`、`verify_config_center.py`、`verify_plan_editor.py` 与后端单测。

## 真实资产与验收边界

- 原先工作区 PNG 是工具/Web 截图残留，不能作为真实游戏识别资产。
- 已采集 PrivaHigh 的真实视觉资产：5 个 1920x1080 场景（开始页、学校总览、招生与班级、校园与师资、设置弹窗）及 23 个模板，命名遵循 `tpl_{id}_{scene}.png`；`baseline_tpl.csv` 有 690 条明细。
- 原始 `match_eval.py` 对这些资产的实测结果：阈值 0.80、0.90、0.95 均为 TP=23、FP=0、FN=0、TN=92、Precision/Recall/F1=1.00。真实平台首轮建议 `algorithm=["tpl"]`、`threshold=0.8`。
- 设置弹窗必须只使用模态框区域作场景，不能使用整屏截图；透明弹窗会暴露背景，导致背景模板出现歧义 FP。
- 不采纳跨场景共享导航、标题等元素，也不采纳会作为另一 UI 文本子串出现的元素，避免真值歧义。
- 当前高分主要证明冻结基线的内部一致性。后续应采集独立验证集（不同存档/状态/时间）验证泛化；不得自动用日常截图覆盖基线。

## 真实验收进展（2026-09-29）

- 白名单登记已完成（`privahigh-backend` 工作目录、真实 tpl/scenes/dataset key、`matcheval_runner._DATASETS` 同步、Airtest 默认目录修正），配置预检全绿。
- **PlanRun #20**（pytest + matcheval，fail_fast）：`failed`。pytest 46 passed / 3 failed；3 条失败**同源** —— 测试把事件池取模除数写死为 3，而事件池已由 3 条扩到 10 条（业务代码无 Bug，`resolve_event` 的拒绝是正确防御）。matcheval 被 `fail_fast` 正确置为 `skipped`。
- **该 3 条失败已修复**（仅改 `PrivaHigh\backend\tests\test_simulation.py`：改为按 `dismiss_teacher` 语义沿真实结算路径定位事件，不依赖事件 index / 池长度 / 特定 seed），本地实测 **49 passed / 0 failed / 0 errors / 0 skipped（1.62s）**。**尚未经平台链路留档**（平台侧最新记录仍是 #20 的 46 passed / 3 failed）。
- **PlanRun #21**（单步 matcheval）：`success`，30s。`Job#26` 读取真实 PrivaHigh 资产（`template_count=23`、`scene_count=5`、CSV 690 行、热力图 23 张、产物 114 个），**TP=23 / FP=0 / FN=0 / TN=92，Precision=Recall=F1=1.00**，与旁路冻结基线零差异；`report.json` 通过 `validate_report()`，报告中心索引 `job_id=26`。
- 历史不变性：`plan_runs#1–19` / `jobs#1–23` 与执行前备份**逐行一致**；真实资产未被修改。
- **尚未做**：同一被测系统的三引擎闭环；真实数据上的报告 A/B 对比。（Locust 的**真实执行链路**已于 2026-10-01 验收，见下文「关键架构决策」。）

## 当前被测系统（3 个）

| 被测系统 | 使用的引擎 | 状态 |
|---|---|---|
| PrivaHigh | pytest + matcheval | 两类真实链路均已跑通（见上） |
| GameAutoTest-Pro | pytest | 需被测服务端；服务不可达时诚实归档为 `failed` |
| Colyseus-Storm | locust | 需 Colyseus 服务端（预设 `Environment` 指向 `localhost:2567`，实际地址随配置）；真实**执行链路已验收**（`Job#29` / `#30`） |

## MVP 状态（2026-09-29 收尾盘点）

主流程 8 步：6 已具备 / 2 部分具备，**无缺失步骤、无架构级缺口**，结论为「只需少量收尾修复」。
本轮已完成 4 项最小收尾：① 文档（`README.md` 重写 + 本文件同步）；② 前端「执行详情日志被自动清空」与「报告 → PlanRun 跳转」修复；③ 运行历史分页/入口 + 报告中心「所属执行」反链；④ 可复现性整理（`requirements.txt`、Locust 场景单一来源、环境常量集中 + 环境变量覆盖）。

## 关键架构决策与待确认事项

- 平台默认不负责启动被测服务；服务由用户/部署环境管理，平台只预检。明确登记为平台托管的受控启动配置后才可启动/停止。
- UI 自动化可由 Executor 启动专用测试客户端并在任务结束时回收；人工调试窗口仍由用户管理。首次真实截图采集或启动图形程序必须有明确授权。
- PrivaHigh 与 ColyseusTechDemo 是不同被测系统：
  - PrivaHigh 适合功能/UI 与视觉识别评估；
  - ColyseusTechDemo 适合现有 `Colyseus-Storm` Locust 压测。
  它们不得被表述为同一产品的真实三引擎验收。当前应分别建立真实计划，除非获得同一被测系统的三类资产。
- ✅ **`Colyseus-Storm/locustfile.py` 的 WebSocket 地址已改造完毕（2026-10-08）**：原先硬编码为
  `ws://localhost:2567`，而 locust 的 `--host`（平台由 `Environment(host, port)` 注入）覆盖不到 WS 段 ——
  服务端不在本机时会「REST 打对了、WS 连错地方」。现由 locust **实际生效的 host** 推导
  （`http→ws` / `https→wss`，见脚本内新增的 `_ws_base()`），脚本不再需要读取环境变量，平台也无需新增配置项。
  依据：locust `runners.py` 执行 `user_class.host = environment.host`，故 `self.host` 即受控配置值。
  ⚠️ 该脚本位于**独立私有仓库** `Colyseus-Storm`（分支 `master`），**不由本仓库版本化**；
  平台只在 `config_center.LOCUST_SCENARIOS` 登记其**路径**，因此「地址随配置走」取决于那份脚本的版本。
- 真实 Locust 压测仍缺：可达的 Colyseus 服务端（`Environment` 指向即可）、保守并发授权。
  真实 MatchEval 数据集与 Airtest 目录已登记并实测通过（见上文真实验收进展）。

## 使用原则

- fixture 仅验证平台链路，不能写成真实业务验收或与真实数据集作有效指标比较。
- 执行真实测试或压测前必须显示并由用户确认目标、pytest 选择器、Locust 并发/爬升/时长、MatchEval 算法/阈值/数据集及 fail-fast 行为。
- 不保存或输出明文凭据；路径与敏感配置保持脱敏。
