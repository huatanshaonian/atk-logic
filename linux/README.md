# ATK-Logic Linux 版构建说明

本目录提供在 Linux 上编译、安装 ATK-Logic 上位机所需的脚本。源码本身大部分已为 Linux 预留了
`Q_OS_LINUX` 分支（USB 通过 libusb 访问，热插拔用 libusb hotplug），移植时主要补齐了构建配置
和少量平台相关问题，详见文末“移植改动”。

## 测试环境

| 项目 | 版本 |
| --- | --- |
| 系统 | Ubuntu 24.04 (x86_64) |
| Qt | 5.15.13（发行版自带） |
| Python（解码器用） | 3.12（系统自带） |
| libusb | 1.0.27 |

已验证：程序可启动、界面正常显示、打开 `test/*.atkdl` 工程文件、加载 182 个协议解码器，
并能在界面中完成 UART 解码（`uart_tx_115200.atkdl`，位宽 8.7 µs）。
**尚未在 Linux 上接入真实的逻辑分析仪硬件测试**，USB 采集流程与 Windows 版共用同一套 libusb 代码，
理论上可用，但请以实际测试为准。

## 一键构建

```bash
# 1. 安装依赖（Debian/Ubuntu）
linux/build.sh --deps

# 或者依赖已装好时直接构建
linux/build.sh
```

脚本会：

1. 下载 [atk_libsigrokdecode](https://github.com/alientek-openedv/atk_libsigrokdecode)
   （固定在已验证的提交，可用 `SRD_REV=` 覆盖，或用 `ATK_SRD_DIR=/本地/源码` 指定已有源码）；
2. 用 `linux/decoder/CMakeLists.txt` 编译出 `libatk_decoder.so`；
3. 用 qmake 编译 `ATK-Logic.pro`，产物位于 `build/linux-release/`：

```
build/linux-release/
├── ATK-Logic                 # 主程序（rpath 指向 ./lib）
├── lib/libatk_decoder.so*    # 协议解码库
├── decoders/                 # Python 协议解码脚本（来自 runtime/）
├── test/                     # 示例工程文件
└── ATK-Logic_*.pdf           # 使用手册
```

直接运行：`build/linux-release/ATK-Logic [工程文件.atkdl]`

其他发行版：依赖为 Qt5 (base / declarative / quickcontrols2 / x11extras / graphicaleffects /
labs-platform / labs-settings)、libusb-1.0、libX11、glib2、python3 开发包、cmake、pkg-config，
包名请按发行版自行对应。

## 安装与 USB 权限

```bash
sudo linux/install.sh            # 安装到 /opt/ATK-Logic，创建菜单项、atk-logic-gui 命令和 udev 规则
sudo linux/install.sh --remove   # 卸载
```

如只需 USB 权限（例如只使用 `api/` 下的 Python 接口），执行：

```bash
sudo linux/install-udev-rules.sh
```

规则文件 `99-atk-logic.rules` 匹配 VID `1a86` / PID `ffcc`，给当前登录用户（uaccess）及
`plugdev` 组读写权限。未安装时打开设备会出现 `LIBUSB_ERROR_ACCESS`（错误码 -3）。

## 已知限制

- 软件自动更新依赖 Windows 更新程序（`update_x64.exe`），Linux 下该功能不起作用，请重新拉取源码编译。
- 固件升级走的是与 Windows 相同的 libusb 代码，但未在 Linux 上用真实硬件验证过；升级中断有可能使设备
  无法启动，建议固件升级仍在 Windows 上用官方软件完成。
- Wayland：无边框窗口拖动依赖 X11，程序在未设置 `QT_QPA_PLATFORM` 时默认使用 `xcb`（XWayland）。
- 解码器脚本是为 Python 3.7 编写的，Python 3.12 启动时会打印若干 `SyntaxWarning`（正则转义），
  不影响使用；如需屏蔽可设置环境变量 `PYTHONWARNINGS=ignore`。

## 移植改动

| 文件 | 改动 |
| --- | --- |
| `ATK-Logic.pro` | 新增 `unix:!macx` 段：pkg-config 链接 libusb-1.0/X11、`QT += x11extras`、链接 `libatk_decoder`、`$ORIGIN/lib` rpath、构建后复制 `runtime/`；补上 `QtLockedFile` 的平台实现源文件（`_unix.cpp` / `_win.cpp`）；QBreakpad 仅在 MSVC 下启用 |
| `main.cpp` | QBreakpad 头文件改为条件包含；Linux 下默认 `QT_QPA_PLATFORM=xcb`；日志中 CPU 架构不再固定写 x86 |
| `pv/draw/frameless_window.cpp` | 打开帮助 PDF 改用 `QDesktopServices`（原来固定调用 `evince` 且会阻塞界面）；非 X11 平台下窗口拖动不再访问空的 X11 Display |
| `linux/decoder/CMakeLists.txt` | 上游仅有 autotools 且不含 `atk_decoder.c` 的构建规则，这里直接编译；同时修正 `atk_decoder_init()` 无条件设置 `PYTHONHOME` 导致系统 Python 无法初始化的问题（仅当程序目录自带 Python 标准库时才设置） |
