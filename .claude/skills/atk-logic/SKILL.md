---
name: atk-logic
description: Control the ALIENTEK ATK-Logic (DL16 / DL16 Plus) USB logic analyzer and analyze its captures on Windows or Linux through the `atk-logic` CLI / Python API. Use when the user wants to capture digital signals, measure frequency/duty/timing, decode protocols (UART, I2C, SPI, CAN, 1-Wire, WS2812, SWD, USB-PD, Modbus... 180+ sigrok decoders), drive the analyzer's PWM outputs, or inspect/convert .atkdl project files saved by the ATK-Logic GUI. Triggers: 逻辑分析仪, 正点原子, ATK-Logic, DL16, 抓波形, 采集, 协议解码, 串口/I2C/SPI 波形, .atkdl.
---

# ATK-Logic 逻辑分析仪控制

`atk-logic` 是本仓库 `api/` 目录下的跨平台（Windows / Linux）命令行工具与 Python 库，直接通过
libusb 与逻辑分析仪通讯（协议移植自官方上位机 C++ 源码），所有命令都向 stdout 输出 JSON。

## 0. 环境检查（每个会话先做一次）

```bash
atk-logic --version || python -m atk_logic --version
```

若命令不存在，从本技能所在仓库安装（本文件位于 `<仓库>/.claude/skills/atk-logic/`）：

```bash
python -m pip install -e "<仓库>/api"
```

- 若本技能被复制到了 `~/.claude/skills/`，`<仓库>` 指用户本地的 atk-logic 源码目录，不确定时询问用户。
- 需要 Python ≥ 3.8；依赖 `libusb1`、`numpy`（Windows 的 libusb1 wheel 已自带 libusb-1.0.dll）。
- 若 `atk-logic` 不在 PATH 中，一律改用 `python -m atk_logic ...`，参数相同。
- 如果把包装在仓库以外，需设置环境变量 `ATK_DECODERS_DIR=<仓库>/runtime/decoders` 才能做协议解码。

然后 `atk-logic list` 看设备是否在线。

## 1. 安全规则（必须遵守）

1. **PWM 会在引脚上输出信号**：开启前向用户说明会输出到哪个 PWM 口、频率和占空比，确认没有接到
   不能被驱动的线路后再执行；用完 `atk-logic pwm-stop`。
2. **绝不尝试固件升级**；遇到 bootloader / 固件版本错误，让用户用官方 ATK-Logic 软件处理。
3. **GUI 与 API 不能同时占用设备**：报 busy / access 错误时先请用户关闭 ATK-Logic 上位机。
4. 阈值电压要与被测电平匹配（默认 1.6 V 对应 3.3 V CMOS；1.8 V 系统用 `--threshold 1.8V`，
   5 V 系统用 `--threshold 5V`）。不确定被测电平时先问用户，不要猜。
5. 长时间或大深度采集（> 1e8 样本）前先告诉用户预计耗时与数据量。

## 2. 常用流程

### 2.1 设备信息与参数范围
```bash
atk-logic info --pretty                 # 型号、USB2/3、固件版本、是否 Plus
atk-logic limits -c 0-3 [--plus]        # 离线查询该通道组合允许的采样率/深度
```
采样率只能取：1/2/4/5/10/20/25/40/50/100/200/250/500 MHz、1 GHz。
- buffer 模式（默认，存入设备内存后上传，可触发）：标准版 ≤250 MHz，Plus ≤500 MHz，
  Plus 且通道 ≤8 时可 1 GHz；总量 样本数×通道数 ≤ 1e9（Plus 3.5e9），开 `--rle` 再×10（仅适合变化少的信号）。
- stream 模式（`--mode stream`，USB 连续传输，可长时间）：≤100 MHz 且 采样率×通道数 ≤ 320 MHz，不支持触发。
- 采样率至少取被测信号最高频率的 4~10 倍；需要精确测时序时取更高。

### 2.2 采集
```bash
# 立即采集 4 个通道 10 ms，保存为 VCD（PulseView/GTKWave 可打开）和 atkjson（供后续解码）
atk-logic capture -c 0-3 -r 10M -d 10ms -o cap.vcd -o cap.atkjson

# 通道 0 上升沿触发，10% 预触发，5 s 内没有触发就放弃
atk-logic capture -c 0,1 -r 100M -n 1M -t 0=rising --trigger-pos 10 --timeout 5s -o trig.atkjson

# 长时间流式采集
atk-logic capture -c 0,1 -r 20M -d 2s --mode stream --rle -o long.atkjson

# 采集同时直接解码
atk-logic capture -c 0 -r 10M -d 50ms -D uart -m rx=0 -O baudrate=115200 --ann rx-data
```
输出 JSON 的 `capture.channels[]` 已给出每个通道的跳变数、占空比、估计频率、最短脉宽
（`shortest_pulse_hz` 可用来估计 UART 波特率）等；`meta.warnings` 非空时要向用户说明。
有触发时务必加 `--timeout`，否则会一直等待。

### 2.3 查看波形细节
```bash
atk-logic edges cap.atkjson --start 0.9ms --end 2ms --channels 0   # 紧凑的电平/持续时间文本
atk-logic open FILE.atkdl --pretty                                 # 官方软件保存的工程文件 → 摘要
atk-logic open FILE.atkdl -o out.vcd -o out.csv                    # 格式转换
```
大文件先用摘要和 `--start/--end` 缩小窗口，不要一次性输出全部边沿。

### 2.4 协议解码（sigrok 解码器，180+ 种）
```bash
atk-logic decoders                       # 列出解码器 id
atk-logic decoders spi --pretty          # 某个解码器的通道名、选项、注释类别
atk-logic decode FILE.atkdl              # 使用 .atkdl 中保存的解码配置
atk-logic decode cap.atkjson -D uart -m tx=0 -O baudrate=115200 -O format=ascii --ann tx-data
atk-logic decode cap.atkjson -D i2c,eeprom24xx -m scl=0,sda=1 --rows ops
atk-logic decode cap.atkjson -D spi,spiflash -m clk=0,mosi=1,miso=2,cs=3
atk-logic decode --capture -c 0,1 -r 10M -d 20ms -D i2c -m scl=0,sda=1 -o cap.atkjson
```
- `-D` 用逗号写解码器栈（底层在前）；`-m` 把解码器通道名映射到分析仪通道号；`-O` 设置选项，
  栈中有重名选项时写成 `uart.parity=even`。通道名/选项名不确定时先运行 `atk-logic decoders ID`。
- `--rows`/`--ann` 过滤输出；`--annotations --max-annotations 50` 输出带时间戳的注释；
  默认最多 100000 条注释（`--limit`），大文件请配合 `--start/--end`。
- 波特率未知时：先看 `shortest_pulse_hz`（≈ 波特率），再取最接近的标准波特率解码。

### 2.5 PWM 输出（先征得用户同意）
```bash
atk-logic pwm 0 -f 1k --duty 25        # PWM0 输出 1 kHz / 25%，命令返回后继续输出
atk-logic pwm 1 -f 2.5M --hold 10s     # 输出 10 s 后自动停止
atk-logic pwm-stop                     # 全部停止；或 pwm-stop 0 / 1
# 自检：PWM0 用杜邦线接到通道 0，边输出边采集
atk-logic capture -c 0 -r 100M -d 1ms --pwm 0:1M:50
```
范围 1 Hz ~ 20 MHz，占空比 1~99%；实际频率 = 200 MHz / round(200 MHz / f)，以返回的 `actual_hz` 为准。

## 3. Python API（复杂分析时使用）

```python
from atk_logic import AtkLogic, load, decode

with AtkLogic.open() as la:                      # 多台设备时 AtkLogic.open(index=1)
    print(la.info)
    cap = la.capture(channels=[0, 1], samplerate=10e6, duration=0.01,
                     threshold="3.3V", trigger={0: "falling"}, trigger_position=10, timeout=5)

cap = load("FILE.atkdl")                         # 也可读取 .atkjson
print(cap.summary())                             # 与 CLI 相同的统计
print(cap.edges_text(channels=[0], start=0, end=int(0.002 * cap.samplerate)))  # 起止为样本序号
cap.save("x.vcd")                                # .vcd / .csv / .atkjson / .json
res = decode(cap, [{"id": "uart", "channels": {"rx": 0}, "options": {"baudrate": 115200}}])
```
需要逐样本处理时用 `ch = cap.channel(0)`：`ch.bits(start, end)` 返回 numpy 0/1 数组，
`ch.edges` 为跳变位置（样本序号），`ch.value_at(i)` 取某一样本；详见 `<仓库>/api/README.md`。

## 4. 错误处理

失败时输出 `{"error": ..., "type": ..., "hint": ...}`，按 `hint` 处理并转告用户。退出码：
2 参数错误，3 未找到设备，4 权限/驱动/被占用，5 设备状态异常（固件/bootloader），6 采集失败。

| 现象 | 处理 |
| --- | --- |
| 未找到设备 (3) | 检查连线；Linux `lsusb \| grep 1a86:ffcc`，Windows 设备管理器 |
| Linux 权限不足 (4) | `sudo <仓库>/linux/install-udev-rules.sh` 后重新插拔 |
| Windows 驱动 (4) | 安装官方 ATK-Logic 软件（自带 WinUSB 驱动），或用 Zadig 给 1A86:FFCC 装 WinUSB |
| busy (4) | 关闭 ATK-Logic 上位机或其他占用设备的脚本 |
| 通讯超时 / 无触发 (6) | 检查触发条件与阈值、加 `--timeout`；仍失败则重新插拔 |
| 固件 / bootloader (5) | 让用户用官方软件升级固件，本工具不刷固件 |

## 5. 汇报结果

向用户汇报时给出：采集参数（通道、采样率、时长、阈值、触发）、关键测量值（频率、占空比、脉宽，
注明分辨率 = 1/采样率）、解码出的数据（较长时给摘要和前若干条）、`meta.warnings` 中的告警，
以及保存的文件路径。测量结论受采样率限制时要说明误差范围。
