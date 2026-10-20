# Poco RPC 超时原因核查（2026-10-13）

## 结论

已复现的链路是：平台 UI worker 完成 pytest 后没有显式关闭 Poco RPC → 子进程退出时 Unity 收到 TCP 连接重置 → 客户端 `TcpServer.HandleDatagramReceived()` 调用 `Debug.LogException()` → Console 开启 Error Pause，Editor 自动暂停 → `PocoManager.Update()` 不再处理 RPC → 新 TCP 连接仍可由网络线程接受，但 `GetSDKVersion` / `Dump` 无响应。

这不是仅由缺少初始 BeginBtn 引起。诊断用例只读取控件树，没有填写或点击，页面一直停留在初始 ServerSetup，仍复现了异常和后续 RPC 超时。用户确认未手动暂停。

## 证据

| 对照 | 结果 |
|---|---|
| 原始只读 RPC 查询后退出，再连接 | 正常；没有页面变化 |
| 同样的只读操作经真实 pytest worker 执行，未显式关闭 Poco | pytest 1 passed；随后 Unity 记录连接重置，暂停按钮选中；下一连接连 SDK 版本都读不到 |
| 只取消暂停，未停止 Play、未重启 Poco、未重置页面 | SDK 版本 6 与 Dump 再次正常响应 |
| 在诊断 worker 的取证结束后加 `poco.agent.rpc.close()` | 1 passed，输出 `[rpc]closed`；下一连接正常 |
| 恢复原 Error Pause 设置后，重复上述显式关闭对照 | 1 passed，下一连接 Dump 0.027 秒返回，BeginBtn 仍存在 |

只读获取到 `kMonoEditorConsoleFlagsV2_h2706168672=7815`，`7815 & 4 != 0`，Error Pause 开启。Unity 官方 C# 参考源码将 `ConsoleFlags.ErrorPause` 定义为 `1 << 2`；临时关闭时值为 7811，诊断结束恢复为 7815。

Unity 日志中的异常：`SocketException: 远程主机强迫关闭了一个现有的连接`，以及 `IOException: Unable to read data from the transport connection`。新的请求仍有 `Client ... --> 109` 日志，但暂停后没有 RPC 响应；只取消暂停即可恢复，排除了必须重启服务或必须换页的解释。

## 代码位置

- 平台原缺陷：`backend/app/executors/ui_worker.py` 的 `main()` 在最终取证、落盘后直接返回，模块入口 `sys.exit(main())`；缺少覆盖正常完成/用例失败/准备失败的 RPC 关闭处理。此遗漏已于 2026-10-14 修复，当前 `main()` 使用 `finally` 回收 RPC。
- 客户端：`Assets/Plugins/TcpServer.cs:307` 将 `EndRead` 的断开异常记录为 `Debug.LogException`。
- 客户端：`Assets/Plugins/PocoManager.cs:211` 在 Unity 主线程 `Update()` 中解析请求并发送响应。
- 已安装 pocoui：`poco.agent.rpc.close()` → `TcpClient.close()` → `Client.close()`，底层调用 socket `shutdown(SHUT_RDWR)` 再 `close()`，完成主动断开。

## 修复位置与边界

平台修复已接入生产 Runner 的 UI worker：生命周期 `finally` 中显式关闭 Poco RPC，取证在关闭前完成。`connection_cleanup` 记录回收结果，关闭失败写入独立 `cleanup_errors` 和堆栈，报告页面显示告警，不覆盖原测试结果。正常完成、用例失败、连接建立后的准备失败、Python 异常退出与幂等回收已通过隔离回归。

2026-10-14 本机未发现目标 Unity 窗口，本次生产改动尚未进行真实客户端连续重连复验；此前保留 Error Pause 的显式关闭诊断对照已通过。`_ci/ui_worker_lifecycle_probe.py production` 现直接使用生产关闭路径，`abrupt` 仅供明确需要的历史缺陷复现。

客户端维护项：正常断开和可预期的连接重置应按连接关闭处理，避免以错误级别触发 Error Pause。取消/超时可能强杀子进程，无法保证 Python finally 一定执行，因此仅补平台正常退出的关闭还不足以覆盖强杀场景。客户端文件本轮只读，未修改。

Error Pause 是有用的调试设置，本轮临时切换仅用于对照，已恢复原设置；不将永久关闭全部错误暂停当作正式修复。

## 产物

产物在 `_ci_out/ui_real_20261013/`（隔离目录，未修改真实运行库）：

- `rpc-abrupt-before.json`、`rpc-after-abrupt.json`：简单 RPC 对照。
- `worker-read-abrupt-rpc/`、`rpc-after-pytest.json`：只读 pytest 后的暂停与超时。
- `rpc-resumed-no-reset.json`：不复位页面、不重启服务，仅恢复运行后的成功响应。
- `worker-graceful-error-pause-on/`、`rpc-after-graceful-error-pause-on.json`：保留原 Error Pause 配置的主动关闭对照。
- `worker-read-abrupt/` 为 Console 浮窗使 GameView 查找失败的无效对照，不用于归因；早期 `rpc-after-unpause.json` 在 UI 恢复动作被阻止时产生，也不用于“恢复后成功”的结论。

来源：[Unity Console 手册](https://docs.unity.com/en-us/engine/6000.5/manual/unity-editor/editor-windows-views-reference/console)、[Unity ConsoleWindow 源码](https://github.com/Unity-Technologies/UnityCsReference/blob/master/Editor/Mono/ConsoleWindow.cs)。
