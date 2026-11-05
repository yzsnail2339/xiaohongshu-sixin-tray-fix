# 小红书私信通：关闭窗口后无法从托盘恢复的本地修复

针对 Windows 版 **小红书私信通 1.0.11** 的社区修复工具。关闭主窗口后，程序仍在后台，但点击托盘图标无法恢复；本补丁补全托盘单击、双击和右键菜单的窗口恢复行为。

**非官方项目，与小红书无隶属关系。** 仓库只提供修补工具、最小差异和测试，不提供完整客户端、安装包或修改后的二进制文件。请从[私信通官网](https://sxt.xiaohongshu.com/)获取客户端。

English: A narrowly scoped, reversible local patch for the Windows Xiaohongshu Sixin Tong 1.0.11 tray-window restore bug. Only the exact verified official build is supported. No client binaries or account data are distributed.

## 已确认的问题

1. 点右上角 **×** 时，客户端阻止真正关闭，隐藏窗口，并从任务栏移除。
2. 原版托盘单击仅停止图标闪烁，没有恢复窗口；也没有双击恢复处理。
3. 托盘窗口名称同时带子菜单与父级点击回调，没有明确的“显示窗口”子项。

第 1、2 项已由源码检查及原版回归测试确认。第 3 项中的父级回调在不同系统上的实际表现未单独验证；修复改为明确的菜单入口，不依赖它。

2026-11-06，从官网链接下载的 1.0.11 安装包中，`app.asar` 和主程序 EXE 的 SHA-256 与最初复现故障的安装文件完全一致。详见[缺陷报告与来源核验](docs/BUG_REPORT.md)。这不是对所有版本、所有安装来源的泛化结论。

## 修复后的操作

- 单击或双击托盘图标：恢复当前客户端实例下已有的主窗口，包括各账号窗口。
- 恢复时还原最小化状态、任务栏图标，并把窗口带到前台。
- 托盘右键 → 窗口名称 → **显示窗口**：只恢复选中的窗口。
- 点 **×** 仍然隐藏到后台；使用“退出”才结束客户端。

只修改资源包中的 `main/windows.js` 和 `main/tray/tray.js`。不修改登录、私信收发、会话分区、更新机制或主程序 EXE，也不添加单实例锁。恢复窗口不会新建账号窗口。

## 使用方法

需要 Python 3.9 或更高版本；安装与回退仅支持 Windows。只使用 Python 标准库，无需 `pip install`。

先下载或克隆本仓库，在仓库目录打开终端。将下面示例路径改为你的实际安装位置；**不要照抄不存在的路径**。

### 1. 只读检查

```powershell
python patcher.py check --asar "C:\Apps\小红书私信通\resources\app.asar"
```

省略 `check` 也只进行检查，不修改文件。工具按文件哈希识别已验证的原版和本工具生成的修复版，未知文件会被拒绝；仅版本号相同还不够。

### 2. 完全退出客户端，再应用

保存未发送的内容，并通过托盘菜单退出私信通所有实例。点右上角 × 只会隐藏窗口，不算退出。重启客户端可能需要重新登录。

```powershell
python patcher.py apply --asar "C:\Apps\小红书私信通\resources\app.asar"
```

工具会确认主程序匹配、未运行且未启用不兼容的包完整性校验，备份原文件、验证补丁，再原子替换。它不会结束进程、提权、清理缓存或修改账号数据。目录没有写入权限时，请使用你有权限的终端；不要修改系统安全设置。

默认主程序为 `resources` 上级的 `小红书私信通.exe`；非标准目录可显式传入 `--exe "完整路径\小红书私信通.exe"`。

备份保存在原文件旁边：`app.asar.tray-restore.original`。再次应用不会覆盖不同内容的旧备份。

### 3. 重新打开并验证

连续做两次：打开窗口 → 点 × → 单击托盘图标。窗口和任务栏图标应恢复；再试最小化后恢复，以及右键菜单的“显示窗口”。

### 回退

完全退出私信通，然后执行：

```powershell
python patcher.py restore --asar "C:\Apps\小红书私信通\resources\app.asar"
```

工具只把匹配的修复版恢复为已验证的原始备份，拒绝用旧包覆盖未知的新版本。备份会保留。

### 只构建，不安装

```powershell
python patcher.py build --asar "C:\Apps\小红书私信通\resources\app.asar" --output "C:\Temp\app.asar.patched"
```

`build` 可跨平台运行，只输出到新文件，不替换原文件。生成的客户端包仅供本地使用，不要提交到本仓库。

## 测试与边界

无需客户端即可运行合成 ASAR 的工具测试：

```powershell
python -m unittest discover -s tests -v
```

有已验证原始 `app.asar` 和 Node.js 时，可在系统临时目录里提取最少必需源码，运行真实模块的行为回归测试：

```powershell
python tests/run_local_client_tests.py --asar "C:\Apps\小红书私信通\resources\app.asar"
```

若已经应用补丁，请把上面的参数指向 `app.asar.tray-restore.original`。测试不会启动客户端或连接账号，结束后清理临时文件。覆盖原版故障复现、连续关闭恢复、任务栏、最小化、多窗口、重复点击、旧菜单引用及正常退出。

最初的本地修复通过了 10 项真实模块回归测试，并由用户在 Windows 托盘实际确认“关闭后能正常恢复”。自动测试使用模拟 Electron 对象，不代表所有 Windows 环境的 UI 验证。

客户端更新可能覆盖本地补丁。**不要把旧版资源包复制到新版客户端**；未知哈希需要重新检查和测试。工具不会关闭或绕过程序签名、ASAR 完整性校验、杀毒软件等安全机制。

## 向官方反馈或报告其他环境

[正式缺陷报告](docs/BUG_REPORT.md)可以复制到私信通产品内的官方反馈入口。请附版本、复现步骤和本仓库链接，不要上传 Cookie、验证码、账号数据库、私信截图或完整客户端包。

2026-11-06 已通过产品内反馈提交，暂未收到官方答复：[上游反馈跟踪 #1](https://github.com/yzsnail2339/xiaohongshu-sixin-tray-fix/issues/1)。

本仓库的 Issue 用于记录复现和修复反馈，不是小红书的官方客服渠道。发布 Issue 或链接一个组织不等于官方开发团队已收到通知。

## 许可

本仓库原创修补工具和测试采用 [MIT License](LICENSE)。最小补丁中的原客户端上下文仍归原权利人所有；本仓库的许可不授予对官方客户端的重新分发许可。名称仅用于识别受影响的软件。
