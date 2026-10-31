# RFC IPC-WIN-001 · Windows 本机控制命名管道

状态：**设计已冻结；本轮限定验证50 pure + 6 Windows私有pipe native通过，不构成开启授权**
验证范围与限制：仓库根 `reports/dev-20261031-r3-ipc.md`、`reports/dev-20261031-r3-qa/ipc-final-job-results/ipc-final.log`；真实跨用户/session/IL负向与整体daemon仍未验收。  
日期：2026-10-31 · Owner：client-seat · R3 IPC-WIN

## 1. 最小目标与非目标

为现有 `ControlClient.call(command, timeout=...)` / daemon control handler 增加 Windows transport；Linux/macOS Unix socket 原行为不变。只用 Python≥3.10 标准库 `ctypes` 调 Win32，无新依赖、TCP、pickle、共享口令或磁盘令牌。GUI/tray保持禁用。本轮不修通知库，不承诺四基线全部通过。

## 2. 明确安全边界

仅接受 **相同 TokenUser SID、TokenSessionId、TokenIntegrityLevel RID** 的双方；均须非 impersonation 身份的服务进程主 token。不同用户、不同登录 session、提升/降权完整性级别不同一律拒绝；不默认支持 LocalSystem 服务或提权代理。不防同SID/session/integrity的恶意进程（同信任主体）；不声称能证明二进制来源。token查询/API缺失/权限不足/ACL配置失败均 fail-closed。

管道名固定产品前缀 + canonical home、SID/session/integrity摘要（摘要只分隔命名空间，不是认证）。CreateNamedPipeW 使用 `PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE`、`PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS`、maxInstances=1。仅创建一次并 Disconnect/Connect 复用，保持句柄占有，避免每请求关闭后重建造成抢占窗口。名称已占用拒绝，不夺取、不删除其他实例、不退备用名字。

显式安全描述符：protected DACL，仅本用户SID允许必要读写，**不授予 Everyone/Authenticated Users**；加mandatory label禁止较低integrity的读写（查询identity仍是必需防线）。TokenUser DACL不在ACL层隔离相同用户的其他session；该类客户端可能先连接占资源，再被session核验拒绝，不声称跨session资源耗尽已在ACL层防住（单连接与10秒预算限制影响）。在CreateNamedPipe传 `SECURITY_ATTRIBUTES`，描述符内存LocalFree，句柄不可继承。精确SDDL为 `D:P(A;;0x0012019B;;;<SID>)S:(ML;;NRNW;;;<IL-SID>)`；客户端desired access同为 `0x0012019B`（READ_CONTROL|SYNCHRONIZE|READ_DATA|WRITE_DATA|READ_EA|WRITE_EA|READ_ATTRIBUTES|WRITE_ATTRIBUTES），明确排除 `0x4 FILE_CREATE_PIPE_INSTANCE/FILE_APPEND_DATA`、GENERIC_WRITE与GENERIC_ALL。IL仅接受低/中/高的已知RID 0x1000/0x2000/0x3000，对应S-1-16-RID；其他级别拒绝，DACL与mandatory label创建失败拒绝，不跳过安全描述符。

双向身份验证：
- 客户端打开管道后，在发送任何请求前调用 GetNamedPipeServerProcessId，OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)、OpenProcessToken(TOKEN_QUERY)、GetTokenInformation 三项，精确匹配自身身份。持有进程/token句柄直到连接结束，查询失败拒绝。
- 服务端连接后取 GetNamedPipeClientProcessId 同法核验客户端主token；请求读入后用 ImpersonateNamedPipeClient + OpenThreadToken(TOKEN_QUERY)核验连接实际token，必须try/finally RevertToSelf；有效token仍须精确相等，不能仅信任PID。同样保持句柄至请求完成，校验完成之前绝不调用业务handler。
- 本端每次入口先OpenThreadToken检查：**只有ERROR_NO_TOKEN**表示无预存impersonation；已有thread token或任何其他错误都拒绝。服务端完成有效token验证后，RevertToSelf必须成功才可handler；revert失败永久停止该服务线程dispatch，不在不确定身份下继续业务。
- 所有ctypes绑定明确argtypes/restype，HANDLE/ULONG_PTR按指针宽度，WinDLL(use_last_error=True)。
- 客户端 CreateFileW 使用 SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION，服务端只查身份，不获得可代执行权限。连接/身份认证没有自行发明的秘密握手。

## 3. 有界协议、超时与资源生命周期

保留JSON dict信封及命令字符串；Windows使用4字节LE长度前缀 + UTF-8 JSON；不会改变POSIX newline transport。请求≤64KiB、响应≤4MiB；拒绝非dict、非UTF8、无穷/NaN JSON、零长/超长/截断；读取精确长度，单连接单请求；状态机为Connect→双向身份→Request→认证/flag/deadline复核→Handler→Response→客户端完整收帧后固定0x06 ACK→Disconnect。错误响应同样ACK；ACK使用同一总deadline，禁止FlushFileBuffers无限等待。stop_event阻止新accept/dispatch，但已授权响应可有界完成；flag关闭或poison仍断开，不重放。禁pickle，错误只回既有错误信封结构、不含原始异常/请求/token。

单服务线程、单连接、无无界任务队列；Connect/Read/Write使用overlapped事件与单调时间deadline，分片≤64KiB；Stop与flag关闭每≤100ms检查。超时调用CancelIoEx，必须等该OVERLAPPED完成后再释放事件/缓冲区；不能为追求超时而释放仍被内核引用的内存。正常取消完成后回收；CancelIoEx本身不保证取消。取消完成等待额外≤1秒，若未确认完成或等待异常，保留完整buffer/OVERLAPPED/event/pipe引用到模块级quarantine，**每进程最多两个**，原子poison禁止所有新IPC操作/dispatch/新listener。采用固定inflight≤2的condition registry，互斥只用于预留/释放slot和poison状态，不跨Wait/Cancel/completion；每handle最多一个pending操作，每op结束释放slot，handler不持slot。同进程server+client必须可用，不能通过禁止同进程规避死锁。poison后其他pending也有限cancel，未确认各自保留，quarantine≤2；不能释放仍被内核引用的对象。隔离对象可能保留到进程退出（非完全回收），不得强杀常驻进程。必须如实记录状态，不声称绝对取消有界或零泄漏。客户端连接等待有相同总deadline，timeout范围(0,30]秒，服务端连接请求总预算10秒。

关闭顺序：当前I/O取消完成 → 客户端身份句柄 → Disconnect → 管道/Event句柄关闭。server线程每轮都有finally回收。只有本次实例自己创建的句柄可关闭；不操作其他daemon、服务或socket文件。

## 4. 治理、接线与启动失败

canonical `facts.FEATURE_FLAGS['client.ipc.windows.enabled']`：default=false；owner=client-seat；expire_at=2026-12-31（UTC，次日起强制关）；rollback_rule=`关闭Windows命名管道控制并显示不可用；不退TCP/不更改daemon运行偏好`。取值统一config.feature_flag，额外严格expiry检查。由factory注入enabled callable：每次调用重新load_config(paths)读非敏感本地配置、验证flag metadata（default=false、owner、expire_at与rollback_rule齐全）和UTC expiry，任何读取/解析失败关闭；不加watcher。连接前、每次dispatch前重新检查当前配置/expiry；关后停止接受控制，不改user_paused，不伪造控制响应。

新增 factory 在 Windows 选择命名管道，关闭或能力不支持抛现有 UsageError/NotRunningError的明确“Windows控制不可用”，**不得执行getuid/Unix路径fallback**。POSIX路径和接口保持原样。Windows daemon启动在修改运行状态/绑定镜像端口前完成transport配置检查；只对本次启动失败回收已创建control/listener，恢复本次写入前的runtime/state；不调用完整stop()导致其他工作副作用。

## 5. 验证、依赖与待裁

写域仅：新ipc_windows.py、daemon.py factory及本次启动失败清理、facts.py新IPC flag、新test_windows_ipc.py、新RFC及索引、新R3报告。无第三方依赖、无额外凭据需求。Lead额外批准server.py仅新增close_bound()用于未启动serve线程的listener关闭及bind异常清理；已运行时拒绝关闭，禁止用shutdown等待尚未启动的serve_forever。Windows先control.open再mirror.bind，失败只关本次句柄并恢复本次之前runtime/state。

QA由qa-runner统一Windows E:隔离harness：先纯mock API/身份/帧/超时/关闭/flag到期/长路径；再真实临时pipe harness（固定echo/status-like handler，不实例化常驻daemon、不用生产home），同进程client/server无死锁和两进程双向身份、重复实例拒绝、并发拒绝/有界等待、连接中断/超时/句柄回收、停用/expiry拒绝。记录Win build/Python/API能力，不把Win AF_UNIX符号存在当可用证据。真实跨用户/session/integrity若无隔离测试身份则列未验证，只报最小账号/权限需求，不索要凭据值。

**冻结需确认**：同SID+同session+同integrity作为首期边界；同信任主体恶意进程不在防御目标；长度前缀仅Windows内部transport；取消回收机制必须经真实harness验证，不满足则保持不支持与默认关。
