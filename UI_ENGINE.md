# UI 交互引擎（Airtest + Poco）

新增 `engine=airtest`，pytest 组织 Python 用例，Airtest 连接设备并截图，Poco 操作控件。入口为 `backend/app/executors/airtest_runner.py`、`ui_worker.py` 和 `ui_config.py`。

## 首版范围

- Windows Unity + Poco SDK；单 Job、单设备、强制执行机独占。
- `unity-smoke`：点击一个登记的安全导航按钮，断言新面板出现。
- `colyseus-ui-smoke`：填写 Environment 服务地址和端口，点击 `BeginBtn`，断言 `text="Sign Up"` 出现；停在注册入口，不注册账号、不加载外部账号清理 fixture。该计划应选择 `127.0.0.1:2567` 的 Colyseus Environment。
- `gameautotest-ui`：只读调用外部项目已有的两个 UI 用例文件。
- 用例库、计划/资产表单、静态预检、实时日志、取消、超时、重试、统一报告、专项证据面板、对比与趋势。
- `.air`、Android、多设备、录像及自动客户端复位尚未支持。`reset=none`，用例自行验证初始状态；取消不恢复游戏界面。
- 隔离回归使用模拟设备，验证平台执行链路。真实 Unity 的 Begin → 注册入口冒烟已通过，完整业务流程仍待验收，详见下方记录。

## 部署和设备登记

UI 依赖建议安装到单独环境。`requirements-ui.txt` 固定本机已有版本，真实设备兼容性仍待验收。从 `platform/` 目录运行：

```powershell
python -m venv .venv-ui
.\.venv-ui\Scripts\python.exe -m pip install -r requirements-ui.txt
```

也可设置 `PLATFORM_UI_PYTHON` 引用部署侧已安装依赖的 Python。只有该解释器可用且依赖导入成功，才增量预置 UI 执行机、资产和冒烟计划；不覆盖已有配置。没有 UI 环境时，引擎显示缺少可用执行机，也可手动在界面选择已装好依赖的受控解释器，允许其运行 `airtest`。

### 网页登记设备（2026-10-15）

首次升级后按原方式重启平台一次，启动入口会增量创建 `ui_devices` 表。此后的设备新增、编辑和启停即时生效，无需环境变量或重启平台。配置保存在当前实例的 SQLite 数据库；隔离实例与主实例配置互不影响。

1. 进入「资源与环境」，编辑或新增 Executor，选择已安装 pytest、airtest、poco 的解释器，勾选允许 `airtest`。本机 Anaconda 已具备依赖，可直接选择「Anaconda 主解释器」，无需创建 `.venv-ui`。
2. 在同页「UI 设备」点「新增」，输入设备名称，选择关联执行机。点击「查看本机窗口」，对 Unity 行点「采用此窗口」。Editor 模式会取项目标题前缀作为包含规则，并填入进程名与类名；仍应确认规则唯一。
3. Colyseus MMO 推荐：标题包含 `ColyseusTechDemo-MMO`，进程 `Unity.exe`，Editor GameView 模式开启，Poco `127.0.0.1:5001`。专用 `colyseus-ui-smoke` 不需要填写通用冒烟控件；通用 `unity-smoke` 则需登记两个不同的点击/目标控件。
4. 保存后先「静态预检」，再「探测窗口」或「Unity / Poco 实时探测」。窗口探测只枚举窗口；实时探测会持锁完成 SDK 握手和层级读取，不点击或聚焦。静态就绪与窗口匹配均不等于 Poco SDK 已就绪。
5. 在测试用例或计划的 `airtest` 步骤选择该设备，并选择它关联的执行机。保存和启动时均拒绝执行机不匹配、设备停用或关联执行机未允许 airtest 的组合。

「查看本机窗口」前必须先选择关联执行机。未选择时网页直接提示，不发送探测请求；后端也拒绝 0、负数、非整数及不存在的执行机 ID，返回 400。此处原先对 0 漏校验导致 500，已补前后端校验，相关 28 项测试通过。

窗口标题支持字面包含或完全一致，可额外限定进程文件名和窗口类名，不接受任意命令、路径或正则。执行子进程在准备阶段重新解析当前句柄，仅允许一个匹配；零匹配/多匹配均归为 `prepare / infrastructure` 失败。句柄不会写成长期设备配置，也不参与内容指纹；实际匹配窗口写入报告的 `target_window`。

游戏须已启动、集成 Poco SDK 并处于可操作状态；平台不会打开游戏或进入 Unity Play 模式。`PLATFORM_UI_DEVICE_URI` 等旧环境变量仅保留为旧设备 `unity-windows` 的兼容入口，网页创建的 `ui-device-N` 始终使用数据库配置，环境变量不会覆盖。

### 本轮验证（2026-10-15）

- 完整隔离回归 **143 passed**；预检提示文案调整后前端往返测试 **1 passed**。
- 浏览器在独立验收库完成 Executor 允许 airtest、设备新增/保存、刷新读回、窗口枚举/选择、唯一窗口探测、计划设备选择及预检。真实 Unity 唯一匹配到 HWND `331388`，不同于此前诊断的 `923030`。
- 生产 worker 从该数据库读取设备规则，动态解析窗口，真实只读层级用例 **1 passed**；`connection_cleanup.state=closed`。结束后下一连接正常读取 SDK 版本 6，Dump 0.030 秒返回。此项只读验证没有填写或点击控件。
- 完整 Begin → Sign Up 点击验收在服务端 `localhost:2567` 连通性预检处未通过；没有将准备/依赖就绪冒充完整业务验收。隔离计划 #13 保存在验收库，可在服务启动后执行。
- 主实例已备份并重启，新增 `ui_devices` 表；登记 `Colyseus MMO Unity`（`ui-device-1`）并关联 `anaconda-python-ui`，其允许 Runner 为原有 pytest 加 airtest。主库原有 59 条 PlanRun、71 条 Job、69 条 Report 逐行对比保持一致。

证据：`_ci_out/ui_devices_web_acceptance/` 保存隔离库、真实 worker 产物及前后 RPC 查询；`_ci_out/ui_devices_deploy_20261015/verification.json` 保存核验摘要，`db-before.sqlite` 是升级前一致性备份。只读生产复验命令可使用：

```powershell
python -B _ci/ui_worker_lifecycle_probe.py production --db data/db.sqlite --device ui-device-1 --output _ci_out/ui-device-readonly-new
```

在界面中新增 UI 用例，选择套件、已登记用例及设备。`kind=airtest` 的 AssetSource 沿用 `scenario_ref` 登记套件，避免新增数据库列；计划如选择了该资产，它必须与 UI 步骤的套件一致。不同套件可在未指定计划级资产的计划中逐步明确指定。

## 真实验收步骤

1. 启动已集成 Poco SDK 的 Unity 客户端，确认目标窗口、端口和初始界面。
2. 通过网页登记设备并探测窗口。Colyseus 验收选择「Colyseus MMO：Begin → 注册入口（不注册）」/ `test_colyseus_ui.py`；通用冒烟则登记两个不同的安全导航按钮与目标面板。
3. 新建计划，选择联机 Environment `127.0.0.1:2567`、设备关联的 Executor，添加相应 airtest 用例步骤。预检并执行，检查真实 Begin → Sign Up 链路。点击前若目标面板已存在，用例会失败，要求恢复初始界面。
4. 检查真实断言与 JUnit；登记一个错误目标控件，再验证失败截图、层级和堆栈。
5. 验证取消、超时后停止继续操作，恢复初始状态后重试。

`gameautotest-ui` 会访问真实服务、创建测试账号；其现有 session fixture 按测试账号前缀清理 MongoDB，不是无副作用演示。执行前确认专用测试环境和该项目账号/数据库配置。Runner 注入服务地址与 Poco 参数并只读调用被测项目；项目自身的适配需单独部署。不把 PrivaHigh 截图素材当作它的 UI 用例。

2026-10-15 按用户要求适配了本工作区 `GameAutoTest-Pro/conftest.py` 的 `init_game_poco`：业务用例仍使用原 fixture 名称；作用域改为 function。平台运行时通过 `request.getfixturevalue("platform_poco")` 复用平台连接，不重新连接或关闭借用连接；只有该 fixture 不存在时才走独立 pytest 的自建分支，平台 fixture 的初始化错误或缺失依赖必须原样失败。自建分支在 `finally` 中回收 RPC，关闭失败仅告警。

适配不改变账号清理逻辑，不自动复位页面。独立运行时现在每个用例拥有自己的连接；平台运行的最终取证与关闭仍由 worker 统一负责。12 项隔离测试通过，包括真实生产 worker 配模拟设备的成功/失败取证集成，不调用实际注册或 Mongo 清理。此适配属于本机被测项目的改动，不会随单独的 platform 仓库自动部署到其他机器，部署该项目时须同步适配后的 conftest。旧 Job 的内容指纹会变化，应从计划新建执行验证。原文件备份在 `_ci_out/gameautotest_fixture_adapter_20261015/conftest.before.py`。

## 约束与结果语义

- UI 套件绑定 `engine=airtest`。TestCase 保存时先规范化 `asset_kind=airtest` 和独占，再计算定义指纹；计划覆盖不能取消独占，调度器也强制加锁。
- 普通 pytest 保存/启动时检查显式选择器及执行机默认工作目录，收集插件阻止通过宽泛目录发现已登记的 UI 文件。混合项目内明确选择的 API 文件仍可运行。新增 UI 文件必须先登记；任意未登记 Python 代码不属于路由保护的识别范围，这不是 Python 沙箱。
- UI 设备静态预检不连接设备。计划预检现默认检查公共运行要求并只读探测 Unity/Poco；执行前重新检查。配置复用、MongoDB 检查与接口边界见 [RUNTIME_REQUIREMENTS.md](RUNTIME_REQUIREMENTS.md)。
- 声明使用服务的 `colyseus-ui-smoke` / `gameautotest-ui` 要求联机 Environment；服务可达性是预检必需项。通用 Unity 控件冒烟不要求服务端。

- 不新增状态机状态。准备失败：`failed` + `phase=prepare` + `failure_kind=infrastructure`；用例/fixture 失败：`run`；收尾失败：`teardown`。双重失败保留原始失败，收尾事件仍记录在用例事件里。
- 零用例、全部跳过、缺少结果不判成功。取证失败记入 `evidence_errors`，不覆盖原始错误。`ui-result.json` 按阶段及时落盘，取消/超时保留最后可见阶段。
- 每个 Job 独立归档 JUnit、截图、堆栈、UI 层级和 Airtest 日志；嵌套日志可下载。私有执行输入不可下载且结束后删除；已知运行路径在 UI 文本日志及证据中替换为 `[runtime]`。
- 正常结束也采集最终截图和层级；外部项目自带的 hook 可能仍生成自己的 `reports/`，平台不改写这些业务 hook，平台自己的证据独立归档。
- 内容指纹独立于定义指纹，包括套件 Python/模板资源和设备配置。重试时发现变化就拒绝执行，要求新建运行；不声称冻结整个外部项目或解释器环境。报告记录实际依赖版本。
- UI 设备配置按运行固化到 `Job.config_snapshot.ui_device`，历史快照不随编辑变化。标题/进程/类名、关联执行机、Poco、Editor 模式、冒烟控件或启停变化会影响内容指纹；名称、备注和动态窗口句柄不影响执行语义。排队后配置变化也会在 Runner 启动前被检查。
- 对比/趋势限定相同套件、用例、设备、等待/复位策略、内容指纹和协议主版本；缺失比较元数据不作有效对比。

## 当前真实验收状态（2026-10-13）

最初因客户端未就绪暂停。用户随后回复“已就绪”，本机 Colyseus 2567、MongoDB 27017、Poco 5001 均可连接，确认唯一的 ColyseusTechDemo-MMO Unity Editor 窗口。设备用明确 HWND 连接，GameView 校准在该窗口内完成，不重新搜索其他 Editor。

真实成功链路：通过 TestCase → Plan → PlanRun → Job → Orchestrator → Airtest 子进程 → Report，在独立验收库中 `Run #1 / Job #1` 得到 **1 passed / 0 failed / 0 skipped**，用例耗时 2.299 秒。填写 `127.0.0.1:2567`，点击 Begin 后确认 `text="Sign Up"` 出现；最终截图与层级已归档，执行机锁释放。没有注册账号，没有调用外部项目 MongoDB 清理 fixture。

后续负例验收未完成：`Run #2 / Job #2` 与 `Run #3 / Job #3` 都在准备阶段失败（0 用例），没有到达预期的初始状态断言。RPC 建连成功但 `dumpHierarchy()` 没有响应；只读重连诊断确认是 `RpcTimeoutError`，连接地址 `tcp://127.0.0.1:5001`，Poco 单次调用内部等待 30 秒。聚焦 GameView 未解决，因此不归因于焦点。两次平台报告正确标记 `phase=prepare / failure_kind=infrastructure` 并释放锁；不能算作“用例失败截图验收通过”。完整重复运行稳定性与真实用例失败取证仍待客户端 Poco 链路恢复后验证。

### 复位对照复跑（2026-10-13）

后续专项诊断已定位 RPC 超时的具体链路：worker 未显式关闭连接，连接重置异常触发 Unity Error Pause，导致主线程暂停。保持初始页面的只读 pytest 同样复现；仅取消暂停即可恢复。开发期诊断报告不随精简仓库交付。显式关闭连接的诊断对照通过，生产 Runner 已于 2026-10-14 接入关闭处理。

保留原 Job #2/#3 的参数和配置快照，以新的重试 Job 验证：

| 条件 | 原任务 / 新重试 | 结果 |
|---|---|---|
| 初始页有 BeginBtn，Sign Up 不存在，Poco 层级可读 | #2 → #4 | success，1 passed，2.293 秒 |
| 紧接上次成功，不复位，停留在 Sign Up | #3 → #5 | failed，prepare/infrastructure，0 用例，dumpHierarchy RPC 超时 |
| 用户恢复初始页后，层级再次可读 | #3 → #6 | success，1 passed，2.258 秒 |

三个重试均释放执行机锁，原 #2/#3 的失败报告不改写。重试证据为 `retry-job-4.json`、`retry-job-5.json`、`retry-job-6.json` 以及对应的 Job 报告。脚本 `_ci/ui_retry_probe.py` 只创建重试、沿用原快照，不自动复位客户端。

结果证明复位后两项任务都能成功，也复现了未复位时的准备超时。不能仅据此认定“缺少 BeginBtn 导致 RPC 超时”：首次成功结束时同一连接能读取 Sign Up 层级；用户复位方式未明确，页面复位与 Poco 重启可能同时发生。自动复位与可重复执行的稳定性仍待完善，真实用例失败截图验收仍未完成。

证据在 `_ci_out/ui_real_20261013/`：`acceptance.json`、`data/reports/run_1/job_1/report.json`、JUnit、最终截图、层级和 Airtest 操作日志。该目录是隔离验收数据，不是原有 `platform/data/`；其中编号不代表原有运行库编号。

脚本入口：`python -B _ci/ui_real_acceptance.py inspect`（只读检查），`run`（需 ServerSetup 初始界面），`negative`（需已经到 Sign Up 的界面，用初始状态失败验证取证）。各次验收使用新的 `--output` 目录，或 `--label` 指定额外用例名后缀。脚本不重置客户端、不覆盖既有同名用例，也不自动安装依赖。负例必须实际到达用例阶段才算验收通过，准备失败不算。

这次只证明 Begin → 注册入口的真实 UI 冒烟链路；完整注册/登录、其他客户端、真实取消/超时后的界面复位仍未验收。

## 生产连接回收修复（2026-10-14）

生产 `ui_worker.main()` 在 `finally` 中调用 `poco.agent.rpc.close()`，最终截图和层级采集在关闭前完成。正常结束、用例失败、连接建立后的准备失败和 Python 异常退出均执行回收；每个 worker 只关闭自己持有的 RPC，重复回收不会再次关闭。

`engine_data.connection_cleanup` 记录 `attempted`、`closed` 和 `state`（`not_connected` / `closed` / `failed`）。关闭异常写入 `cleanup_errors` 与独立 `cleanup-trace.txt`，报告页面显示告警；原测试状态、阶段、错误和 JUnit 保留。取消/超时强杀子进程仍沿用原调度行为，不能保证执行 Python `finally`，客户端断开异常处理仍需客户端维护方完善。

UI 专项隔离回归 **30 passed**，后端完整回归 **124 passed**，覆盖上述退出路径和关闭失败不覆盖测试结果。2026-10-14 本机未发现目标 Colyseus Unity 窗口，因此本次没有执行修改后的生产代码真实重连验收；2026-10-13 的真实显式关闭对照证据继续保留。只读复验可运行 `_ci/ui_worker_lifecycle_probe.py production --output <新的隔离目录>`，随后用 `_ci/ui_rpc_lifecycle_probe.py graceful --output <结果文件>` 确认下一连接可响应；先确认诊断脚本中的设备 URI 与当前窗口一致。

## 隔离验证

```powershell
python -B -m pytest -p no:cacheprovider --basetemp .pytest-tmp backend/tests
```

新增测试覆盖路由绕过、绑定/指纹、静态缺失设备、子进程成功/失败/准备失败/收尾失败/零用例/跳过、取证降级、取消、调度超时与锁释放、重试保留历史、内容变化、嵌套证据下载和私有输入保护；前端往返加入 UI 参数，验证不凭空注入默认值。测试不使用真实运行库、不操作真实客户端。
