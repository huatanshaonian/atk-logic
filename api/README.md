# atk-logic：ATK-Logic 逻辑分析仪跨平台 Python API / 命令行工具

适用于正点原子（ALIENTEK）DL16 / DL16 Plus 逻辑分析仪（USB `1a86:ffcc`）。
同一套代码在 **Windows 与 Linux** 上行为一致，底层通过 libusb（python-libusb1）直接与设备通信，不依赖官方上位机，也不依赖 libsigrokdecode。

功能：

- 设备枚举、连接握手、读取设备信息（型号、USB 2.0/3.0、MCU/FPGA/硬件版本、是否 Plus）
- 采集：Buffer（硬件缓存，支持触发）/ Stream（USB 实时流）两种模式，可选硬件 RLE 压缩
- PWM 输出（2 路，1 Hz–20 MHz，占空比 1–99 %）
- 读取官方上位机保存的工程文件 `.atkdl`
- 导出 CSV（与上位机格式一致的“变化点”行）、VCD（PulseView / sigrok-cli / GTKWave 可读）、无损边沿 JSON（`.atkjson`）、统计摘要 JSON
- 协议解码：用纯 Python 实现的 `sigrokdecode` 兼容层直接运行仓库 `runtime/decoders/` 下的 180 余个 sigrok 解码器（UART、I²C、SPI、CAN、SWD、Modbus、USB PD、WS2812……），支持堆叠（如 I²C → 24xx EEPROM）
- 所有命令输出 JSON，便于脚本或 Claude Code 等 AI 工具调用

> **重要说明**：协议实现逐行对照了官方 C++ 源码（见文末“实现对照”），并用软件模拟的设备做了端到端测试，`.atkdl` 读取与解码已用仓库自带的 12 个真实采集文件验证；**但开发环境中没有真实硬件，USB 实机采集尚未在真机上验证**。首次使用请先用 `info`、PWM 自环等简单操作确认。

## 1. 安装

需要 Python ≥ 3.8。

```bash
cd api
pip install .            # 或开发模式：pip install -e .
# 依赖：libusb1（python-libusb1）、numpy
```

### Windows

- python-libusb1 的 Windows wheel 已内置 `libusb-1.0.dll`，无需另装 libusb。
- 设备需要 WinUSB 驱动。安装过官方 ATK-Logic 上位机的电脑一般已具备；否则可用 [Zadig](https://zadig.akeo.ie/) 为 `VID 1A86 / PID FFCC` 绑定 WinUSB。

### Linux

```bash
sudo apt install libusb-1.0-0          # Debian/Ubuntu；其他发行版安装对应 libusb-1.0 包
# 非 root 访问需要 udev 规则（仓库 linux/99-atk-logic.rules）：
sudo cp ../linux/99-atk-logic.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# 然后重新插拔分析仪
```

### 协议解码器目录

解码器使用仓库中的 `runtime/decoders/`。以开发模式（`pip install -e .`）或直接在仓库内运行时会自动找到；
若把包安装到别处，请设置环境变量 `ATK_DECODERS_DIR=<仓库>/runtime/decoders`，或在命令中加 `--decoders-dir`。

## 2. 命令行（`atk-logic`，也可用 `python -m atk_logic`）

所有命令向 stdout 输出一个 JSON；加 `--pretty` 缩进。出错时输出 `{"error":..., "type":..., "hint":...}` 并返回非 0：
2 = 参数错误，3 = 未找到设备，4 = 权限/驱动/被占用，5 = 设备状态异常（如需升级固件），6 = 采集失败。

```bash
atk-logic list                                    # 枚举设备（不打开）
atk-logic info --pretty                           # 连接并读取设备信息
atk-logic limits -c 0-3 --plus                    # 离线查询：该通道数下允许的采样率/深度

# 采集 ch0-3，10 MHz，10 ms，3.3V CMOS 电平（阈值 1.6 V），立即开始，保存 VCD 和 CSV
atk-logic capture -c 0-3 -r 10M -d 10ms --threshold 3.3V -o cap.vcd -o cap.csv

# ch0 上升沿触发，触发前保留 10 %，最多等 5 s
atk-logic capture -c 0,1 -r 100M -n 1M -t 0=rising --trigger-pos 10 --timeout 5s -o trig.atkjson

# Stream 模式 + RLE，长时间采集（stream 模式下 采样率 × 通道数 ≤ 320 MHz，且 ≤ 100 MHz）
atk-logic capture -c 0,1 -r 20M -d 2s --mode stream --rle -o long.atkjson

# 采集并直接解码（UART RX 接 ch0，115200 8N1）
atk-logic capture -c 0 -r 10M -d 50ms -D uart -m rx=0 -O baudrate=115200 --ann rx-data

# 自环测试：PWM0 输出 1 MHz 50 % 接到 ch0，同时采集
atk-logic capture -c 0 -r 100M -d 1ms --pwm 0:1M:50 --edges

# PWM：启动后退出（输出保持）/ 保持 10 s 后关闭 / 关闭
atk-logic pwm 0 -f 1k --duty 25
atk-logic pwm 1 -f 2.5M --duty 50 --hold 10s
atk-logic pwm-stop            # 或 pwm-stop 0 / pwm-stop 1

# 离线：打开上位机保存的 .atkdl，输出统计并导出
atk-logic open ../runtime/test/uart_tx_115200.atkdl -o uart.vcd --pretty
atk-logic edges ../runtime/test/uart_tx_115200.atkdl --start 0.9ms --end 2ms   # 紧凑的电平持续时间文本

# 解码：不给 -D 时使用 .atkdl 里保存的解码配置
atk-logic decode ../runtime/test/eeprom_24c02_i2c.atkdl --rows ops
atk-logic decode ../runtime/test/uart_tx_115200.atkdl -D uart -m tx=0 -O baudrate=115200 -O format=ascii --ann tx-data
atk-logic decode cap.atkjson -D i2c,eeprom24xx -m scl=0,sda=1 --annotations --max-annotations 50
atk-logic decoders                 # 列出全部解码器
atk-logic decoders spi --pretty    # 查看某解码器的通道、选项、注释类别
```

常用参数说明：

| 参数 | 说明 |
|---|---|
| `-c 0,1,4-7` | 通道（0–15） |
| `-r 10M` | 采样率，可选 1/2/4/5/10/20/25/40/50/100/200/250/500 MHz、1 GHz（受模式/型号/通道数限制，见 `limits`） |
| `-d 10ms` / `-n 1M` | 采集时长 / 每通道采样点数 |
| `--threshold` | 阈值电压（-5…5 V）或预设 `5V`/`3.3V`/`3.0V`/`2.5V`/`1.8V`/`1.5V`/`1.2V`（对应上位机 CMOS 预设 2.5/1.6/1.5/1.2/0.9/0.7/0.6 V），默认 1.6 V |
| `-t 0=rising,3=high` | 触发：`rising` `falling` `high` `low` `edge`（双沿）；不给则立即采集；仅 buffer 模式 |
| `--trigger-pos 10` | 触发前数据占比（1–90 %） |
| `-D` / `-m` / `-O` | 解码器（可堆叠，低层在前）/ 通道映射 / 选项（有歧义时写 `decoder.option=value`） |
| `--ann` / `--rows` | 只输出指定注释类别 / 注释行 |

## 3. Python API

```python
from atk_logic import AtkLogic, list_devices, load, read_atkdl, decode

print(list_devices())
cap = load("../runtime/test/uart_tx_115200.atkdl")   # .atkdl（上位机工程）或 .atkjson（本包保存）

with AtkLogic.open() as la:                    # 连接握手与官方上位机相同
    print(la.info.as_dict())
    la.pwm(0, 1e6, 50)                         # 可选：PWM0 输出 1 MHz 方波
    cap = la.capture(channels=[0, 1], samplerate=100e6, duration=1e-3,
                     threshold="3.3V", mode="buffer",
                     trigger={0: "rising"}, trigger_position=10, timeout=5)
# 退出 with 时按上位机流程关闭（唤醒 FPGA → 关 PWM → Exit → 休眠）

print(cap.summary())                           # 每通道跳变数、占空比、频率、首末边沿……
print(cap.edges_text(max_edges=50))            # 适合直接阅读的电平持续时间文本
bits = cap.channel(0).bits(0, 1000)            # numpy 0/1 数组
edges = cap.channel(0).edges                   # 跳变位置（采样点序号）
cap.save("cap.vcd"); cap.save("cap.csv"); cap.save("cap.atkjson")

res = decode(cap, [{"id": "uart", "channels": {"rx": 1}, "options": {"baudrate": 115200}}])
print(res["rows"])                             # 按注释行汇总的文本
for a in res["annotations"][:10]:              # 带时间戳的注释
    print(a["t0"], a["ann"], a["text"])

old = read_atkdl("../runtime/test/spi_flash_w25q128.atkdl")
print(decode(old, old.meta["decoders"][0])["rows"]["spiflash.commands"])
```

主要异常：`DeviceNotFound`、`DeviceAccessError`（权限/驱动/被占用）、`DeviceStateError`（握手失败、需升级固件）、`CaptureError`；参数不合法时抛 `ValueError`，信息中写明允许范围。异常对象的 `hint` 属性给出处理建议。

## 4. 设计与实现对照

| 本包 | 官方源码 | 内容 |
|---|---|---|
| `protocol.fpga_command` | `pv/usb/usb_control.cpp` `Write()` / `SendToDevice()` | 8 字节 0 + `0x0A` + `[code, len+1, data]` + `0x0B` + CRC32，补齐 2048 字节后做 4 路交织 |
| `protocol.mcu_command` | `SendToMCU()` | 512 字节原始命令（版本查询 0x81、FPGA 唤醒/休眠 0x87…） |
| `protocol.convert_to_pc/device` | `pv/usb/usb_base.cpp` | 每 2048 字节块 4×512 字节交织 |
| `protocol.crc32_atk` | `pv/static/util.cpp` `gCRC32` | 反射 CRC-32 表，初值 0、末尾异或 0xFFFFFFFF |
| `protocol.PacketParser` | `pv/data/analysis.cpp` | `0x0A, order, u16 len, payload, 0x00, 0x0B`，跨传输残包拼接 |
| `protocol.encode_settings/encode_trigger/encode_pwm` | `pv/controller/session_controller.cpp` | 参数、触发、PWM 字节编码 |
| `AtkLogic.connect` | `pv/thread/connect.cpp` | 握手、版本检查、bootloader 检测 |
| `AtkLogic.capture` | `pv/data/session.cpp` `OrderStart`、`pv/thread/thread_work.cpp` `DeviceRecvThread` | 采集流程、order 1–6 数据包处理、RLE 展开、buffer 模式起始偏移裁剪、超容/超带宽标志 |
| `atkdl.read_atkdl` | `ThreadWork::LoadSourceFileThreadPara` | zip 内 `channel.ini` + 每通道 1 MiB 数据块 + `tog/value` 常量块 |
| `decode.py` | libsigrokdecode `type_decoder.c` / `instance.c` | `wait()` 条件语义（h/l/r/f/e/n/skip、OR/AND、`matched`、`samplenum`） |

解码器引擎不逐点扫描，而是在各通道的跳变列表上跳转，因此长时间空闲的数据几乎不耗时；对跳变极多的数据（例如 5 亿点、1500 万次跳变的 SWD 文件）纯 Python 解码需要数分钟，可用 `--start/--end` 截取窗口或用 `--limit` 限制注释数量提前结束。

## 5. 局限与注意事项

- **未在真实硬件上验证**（见开头说明）。协议细节与官方源码一致，但以下几点只能在真机上确认：
  - buffer 模式等待触发期间设备是否持续发送进度包（官方上位机约 2 s 无数据即判定“应答超时”，本包沿用该逻辑，可通过 `AtkLogic.silence_timeout_s` 调整）；
  - `pwm` 命令默认在释放 USB 后保持 PWM 输出（跳过关闭流程），设备在主机端断开后是否仍保持输出未经验证；如需确定行为请用 `--hold` 或在同一 Python 会话中完成 PWM + 采集；
  - Stream 模式在高带宽下纯 Python 是否来得及接收（已使用多路异步 libusb 传输；若出现“超出硬件容量”警告，请降低采样率或通道数，或改用 buffer 模式）。
- **官方上位机与本 API 不能同时占用设备**，使用前请关闭上位机。
- 不实现固件升级 / bootloader 相关命令；检测到 bootloader 状态或固件版本过低时会报错并提示用官方上位机升级。
- 仅实现官方上位机实际使用的“简单触发”；上位机中尚未完成的多级触发同样未实现。
- 采集数据保存在内存中（每通道每 8 个采样点 1 字节），超大深度时请注意内存。

## 6. 测试

```bash
pip install pytest
python -m pytest -q tests
```

测试内容：CRC/帧格式/交织往返、数据包解析（任意切分、垃圾字节）、RLE、参数编码；
用软件模拟设备（`tests/fake_device.py`）端到端测试连接、buffer/stream/RLE 采集（含非整字节的起始偏移裁剪）、触发超时、参数被拒、PWM 与关闭流程；
读取 `runtime/test/*.atkdl` 并用 UART、I²C+EEPROM、SPI+Flash、CAN 解码器校验结果；导出格式往返；CLI 冒烟测试。

## 许可

GPL-3.0-or-later（与 ATK-Logic 及 sigrok 解码器一致）。
