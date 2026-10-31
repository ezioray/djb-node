# R4 Windows 通知文件安全基础库：阶段一 RFC

状态：设计已获lead/reviewer批准，阶段一实现已冻结；同版Windows pure181通过、两条限定native通过并有独立ACL回读。公共安全root正例因E祖先ACL不满足仍BLOCKED。本阶段未修改 notifications.py、原通知测试、util、IPC 或既有用户目录权限。

## 1. 范围与非目标

新增 `djb_node/file_security.py`，仅标准库，公开两个同步API：

```python
def create_private_directory(path: str) -> None: ...
def assert_private_path(path: str, directory: bool = False) -> None: ...
```

- create：只创建指定的一个新目录；父目录必须存在且安全。创建时即传入私有安全描述符；已存在只验证，失败不修复。绝不递归mkdir、chmod、SetSecurityInfo、SetNamedSecurityInfo或改变owner。
- assert：只读验证当前对象安全性，成功None，失败抛固定类别 `FileSecurityError`；不修改对象，不打开SQLite。
- Windows阶段一：其它平台调用明确unsupported，不在本票重写POSIX权限策略；模块在非Windows可import且不加载WinDLL。
- 不新增运行时依赖、flag、环境启用路径、接受宽ACL选项或可由调用者指定的可信SID。
- 本阶段测试只证明安全对象创建/验证；**原通知1fail仍未修复**，通知接线另需批准。

## 2. 身份和精确目标DACL

使用独立最小ctypes实现，不导入IPC注册表、pipe权限或修改冻结IPC文件。

1. `OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, True, ...)`：只有ERROR_NO_TOKEN(1008)可继续；若有任何线程token（包括匿名/impersonation）立即拒绝并关闭；其它错误也拒绝。
2. `OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY)`、`GetTokenInformation(TokenUser)`获取当前运行账户SID。SID只保留运行时内存，不写日志、异常或报告。
3. 所有TokenUser缓冲区生命周期覆盖其SID指针使用；句柄finally关闭。字符串SID仅内部构造SD，转换分配用LocalFree释放。

目标所有者必须EqualSid(current SID)。DACL必须存在且非NULL、非空，`SE_DACL_PROTECTED`已设置；拒绝额外ACE、deny/object/callback/未知ACE，不做不完整复杂ACL语义推断。

| 目标 | 精确ACE |
| --- | --- |
| 文件 | 一个ACCESS_ALLOWED，flags=0，mask=`0x0013019F`，SID=current |
| 目录 | 一个ACCESS_ALLOWED，flags=0，mask=`0x001301FF`，SID=current；一个ACCESS_ALLOWED，flags=`OBJECT_INHERIT_ACE|INHERIT_ONLY_ACE = 0x09`，mask=`0x0013019F`，SID=current |

SDDL符号模板（CURRENT不是实际SID）：

- 文件：`O:<CURRENT>D:P(A;;0x0013019f;;;<CURRENT>)`
- 目录：`O:<CURRENT>D:P(A;;0x001301ff;;;<CURRENT>)(A;OIIO;0x0013019f;;;<CURRENT>)`

文件掩码是FILE_GENERIC_READ|FILE_GENERIC_WRITE|DELETE，目录增加遍历和FILE_DELETE_CHILD。不显式授予WRITE_DAC/WRITE_OWNER/FULL_CONTROL，不加SYSTEM或Administrators。owner本身仍有Windows固有权限管理能力；本方案不防同SID进程或本机特权管理员。

目录只提供直接子文件继承；不承诺递归目录继承。创建子目录必须再次用create显式安全SD。

## 3. 路径、祖先及句柄边界

### 3.1 明确支持面

只接受字符串形式本地固定磁盘的绝对盘符路径（如 `E:\approved-job\private`），盘符可规范大写但不通过路径重写消除危险输入。拒绝：

- 相对路径、盘符相对路径、UNC、设备命名空间、extended path、ADS（盘符以外冒号）；
- NUL、空组件、`.` / `..`、正斜杠混用、组件尾点/空格、保留DOS设备名、通配符；
- 卷根本身作为create/目标对象；祖先缺失；不支持的卷类型/无持久ACL能力。

不要先realpath解析链接再信任解析结果。`GetDriveTypeW`须固定盘；`GetVolumeInformationW`须同时报告NTFS和FILE_PERSISTENT_ACLS，初版拒FAT/exFAT/ReFS/映射盘及未知类型；这只是明确缩小已验证支持面，不宣称其它文件系统一定不安全。这些只确认能力，不代替真实DACL验证。

### 3.2 不跳过E盘根检查

从盘根开始逐级开现有祖先目录，使用 `CreateFileW(READ_CONTROL|FILE_READ_ATTRIBUTES, FILE_SHARE_READ|FILE_SHARE_WRITE, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT)`：**不设置FILE_SHARE_DELETE**。持有已打开祖先句柄直到本次操作结束，不能开完即关再访问下一级。

每个祖先必须是目录且无reparse；从同一句柄查询属性与安全描述符，并以GetFinalPathNameByHandle规范名字核对拒绝短名/别名解析，不只用路径lstat。**未进行跨handle的卷serial/file index比较，不声称整个mount epoch的对象身份不变**；保护依赖逐级已验证祖先句柄的无delete-sharing及明确特权信任边界。目标文件拒硬链接计数不等于1；目录链接计数不套用POSIX规则。

所有祖先（**包括E盘根，不存在默许盘根的例外**）采用下述单独策略：

- owner仅允许current SID、LocalSystem(S-1-5-18)、Builtin Administrators(S-1-5-32-544)。后两者仅是已有祖先的OS特权信任边界；目标依然仅current SID。
- DACL须存在非NULL。逐条识别普通ALLOW ACE；未知/复杂/deny初版保守拒绝，不尝试假定deny会抵消allow。
- 对不属于三个可信主体的ACE，不得允许危险权限：目录创建子项、删除子项、删除本目录、写属性/EA、改DACL/owner、generic write/all/maximum-allowed等。即使是inherit-only危险ACE，首版也保守拒绝以免下一级继承放宽；只读/遍历ACE可存在。
- mask检查针对文件目录权位，不用pipe掩码；GENERIC_READ/WRITE/EXECUTE/ALL先按FILE_GENERIC_*显式展开（或MapGenericMask），MAXIMUM_ALLOWED及未识别权限位保守拒绝。外部SID的危险mask至少 `0x000D0156`：FILE_WRITE_DATA/ADD_FILE(0x2)、FILE_APPEND_DATA/ADD_SUBDIRECTORY(0x4)、FILE_WRITE_EA(0x10)、FILE_DELETE_CHILD(0x40)、FILE_WRITE_ATTRIBUTES(0x100)、DELETE(0x10000)、WRITE_DAC(0x40000)、WRITE_OWNER(0x80000)。READ_CONTROL/READ_DATA/READ_EA/READ_ATTRIBUTES/EXECUTE/SYNCHRONIZE可按只读遍历处理。
- 对ADD_SUBDIRECTORY单独拒绝是首版**保守能力限制**，不宣称该位单独必然能劫持已pin的既有子对象，更不自造必要充分的effective-access定理。若因此阻断E根，是可用性阻断，不是假装攻击已实证。
- 不为测试改祖先ACL；E job父链若存在普通Users创建/删除/控制能力则**报告blocked**，不降级、不默认豁免E根。盘根自身通常不能被rename不等于子项安全，根上的DELETE_CHILD等必须检查。

不防SYSTEM/管理员、同SID进程主动改DACL/对象或离线磁盘；不证明ACL已有泄露的历史保密性。ACL在句柄持有期间仍可被这些受信主体变更，这也是明确边界。

### 3.3 create的原子性与失败处理

- 先pin并验证所有现有祖先，再构建受保护SD并 `CreateDirectoryW(path, SECURITY_ATTRIBUTES)` 一次创建；不先宽mkdir再补权限。
- 成功后打开新目录句柄，保留祖先pins，回读owner/DACL/属性精确匹配才返回。
- ERROR_ALREADY_EXISTS只读验证目标；不存在“帮已有目录收紧ACL”分支；竞争创建的对象也必须满足同样验证。
- 拒reparse/父目录被替换/对象类型不符/安全描述符查询失败。创建或回读失败抛固定错误；不自行删除可能属于其他人的竞争对象，不递归清理；新建空目录可能留存，报告应真实说明，不声称事务回滚。

### 3.4 返回None的真实承诺

公开两API返回前释放全部句柄，因此只证明**本次调用期间及最后验证点**的状态，不给未来使用者对象租约，不保证下一次SQLite按路径打开与刚验证的是同一个对象。

实现可使用内部 `_pinned_ancestors(path)` context manager，复用安全检查与句柄管理；这不是“通知使用期保护已完成”。下一阶段须单独设计context-manager lease：先pin并验证父链，在lease内打开SQLite、验证DB/WAL/SHM，并保持必要父句柄贯穿使用期。不得把当前assert调用后单独sqlite.connect称为TOCTOU闭环。

## 4. 预留private inherited文件验证（非当前公开放宽项）

`SE_DACL_PROTECTED`不是所有安全文件的必要条件。SQLite新建WAL/SHM通常从安全父目录继承当前SID ACE，子文件可能未protected。

阶段一公开assert严格验证上述protected对象；内部设计将ACL分类拆成：

- `_assert_explicit_private_target(...)`：当前公开API使用；
- 将来 `_assert_private_inherited_file(..., pinned_parent)`：**仅在已持有且验证安全的直接父句柄上下文内**，验证非NULL DACL、current owner、精确currentSID文件掩码、预期INHERITED_ACE且无额外权限/主体，检查真实继承关系/父对象身份。

后者当前只保留设计，不提供allow_insecure参数或未验证的成功实现。继承ACE旗标转换、owner/default DACL、parent pin贯穿SQLite生命周期，须真实Windows QA后才可接通知；不能仅凭文件protected缺失说不安全，也不能仅凭SID一样说已安全。

## 5. ctypes与错误模型

按需加载 `ctypes.WinDLL(..., use_last_error=True)`，每个函数设置argtypes/restype。HANDLE、LPVOID、ULONG_PTR按位宽；SECURITY_ATTRIBUTES/BY_HANDLE_FILE_INFORMATION/ACL/ACE头布局有独立测试。GetTokenInformation先问尺寸再分配，GetLastError只在对应失败后立即取。

只使用创建和读取相关API：GetCurrentThread/GetCurrentProcess、OpenThreadToken/OpenProcessToken、GetTokenInformation、EqualSid/转换SID、ConvertStringSecurityDescriptorToSecurityDescriptorW、GetSecurityInfo/GetSecurityDescriptorDacl/GetSecurityDescriptorControl/GetAclInformation/GetAce、CreateFileW/CreateDirectoryW、GetFileInformationByHandle、卷查询、CloseHandle/LocalFree。**生产代码不引入修改已有ACL API**。

`FileSecurityError`仅公开稳定原因枚举（如unsupported_platform/unsafe_path/untrusted_ancestor/impersonation/unsafe_owner/unsafe_dacl/native_error）；不包含实际SID、SDDL、用户名、token或通知内容，也不回显原始底层异常。无成功默认值或except-pass。清理失败不能被当成安全成功，应可被测试并给固定错误。

## 6. 写域、实施步骤及验收门

唯一当前授权：

1. 新 `repos/djb-node/djb_node/file_security.py`；
2. 新 `repos/djb-node/tests/test_file_security.py`；
3. 本 `repos/djb-node/docs/rfc/windows-notification-acl.md`；
4. `reports/dev-20261031-r4-acl.md`。

单写者cloud-security。IPC冻结不动，不改notification现有源码/测试，不改util或dependencies。

实施顺序：设计review → 实现ctypes后端/精确ACL解析/纯策略与故障测试 → 冻结hash交QA → Windows E盘只读父链评估 → 符合条件才在限定合成子目录native测试 → 独立review。祖先不满足则报告native阻断，不能只凭API符号存在宣布成功。

验收矩阵（下面是设计目标，不是全部已验声明；实际阶段一证据为pure181、公共unsafe祖先拒零创建、底层新空目录SD机制及独立回读；真实安全父链正向、真实rename拒绝、真实异用户访问、通知侧文件与lease未验）：

- 纯测试：路径拒绝矩阵、精确owner/ACE/mask/flags、其它SID负控、NULL/空/额外ACE、祖先危险权限、impersonation/error-code区分、句柄及LocalFree每个故障路径释放、create顺序与already-exists零权限修改。
- native：E授权合成目录首次创建SD即时生效，存在复验不改ACL；公开assert检查人工合成的protected私有文件；坏ACL/owner/reparse/hardlink负控，全部对象限定沙箱。不创建真实用户、读真实home或调用通知daemon。
- 文件fixture的私有SD创建/宽ACL负控由专用QA测试代码在合成对象上完成，不借生产模块隐藏SetSecurityInfo能力。不对任何现有用户目录设权限。
- 独立Win32回读实际SD验证（不只复用被测assert），日志SID仅映射CURRENT/OTHER；synthetic SID可用明确测试值，不是真用户凭据。
- 祖先pins期间由测试进程尝试rename应被Windows共享模式拒绝，退出后可按预期释放；此测试不声称防同SID主动ACL修改。
- 不承诺跨账户实测：没有真实其它账户token时，只报告真实SD回读/合成SID负控，不能称异用户CreateFile已验证。
- 结果含精确suite/exit/count、哈希、原始日志、匿名化DACL证据、失败负控、清理；本阶段不将原通知1fail标关闭。
