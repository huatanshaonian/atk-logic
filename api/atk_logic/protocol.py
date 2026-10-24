"""Wire protocol of the ATK-Logic (ALIENTEK DL16) logic analyzer.

Pure functions only (no USB access), ported 1:1 from the official C++ sources:
  pv/usb/usb_base.cpp      -> convert_to_pc / convert_to_device
  pv/usb/usb_control.cpp   -> fpga_command / mcu_command
  pv/data/analysis.cpp     -> PacketParser
  pv/static/util.cpp       -> crc32_atk
  pv/controller/session_controller.cpp -> encode_settings / encode_trigger / encode_pwm
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

# ---- USB ids / endpoints (usb_base.h, util.h, usb_control.cpp) ----
VENDOR_ID = 0x1A86
PRODUCT_ID = 0xFFCC
INTERFACE = 0
EP_OUT = 0x02  # TO_MCU_EP
EP_IN = 0x81  # TO_PC_EP

BLOCK = 2048  # interleave block size
READ_TRANSFER_SIZE = 16384  # READ_TRANSFER_BUFFER_SIZE
READ_TRANSFER_COUNT = 40  # READ_TRANSFER_LENGTH
CHANNEL_COUNT = 16

# Version gates (ATK-Logic.pro)
APP_VERSION_NUM = 1066
FPGA_MIN_VERSION_NUM = 115

# ---- FPGA command codes (usb_control.cpp) ----
CMD_GET_DEVICE_DATA = 0x10
CMD_PARAMETER_SETTING = 0x11
CMD_SIMPLE_TRIGGER = 0x12
CMD_STOP = 0x15
CMD_PWM = 0x17
CMD_EXIT = 0x18

# ---- MCU command codes (raw 512-byte buffers) ----
MCU_GET_VERSION = 0x81
MCU_RESTART = 0x84
MCU_SET_RESET_STATE = 0x87
MCU_GET_RESET_STATE = 0x88

# ---- packet orders (thread_work.cpp DeviceRecvThread) ----
ORDER_SAMPLES = 1
ORDER_DEVICE_INFO = 2
ORDER_OFFSET = 3
ORDER_REPLY = 4
ORDER_PROGRESS = 5
ORDER_END = 6

# Sample-rate table. hardware code = index + 1 (SetContent.qml hzComboBox).
SAMPLE_RATES = [
    1_000_000, 2_000_000, 4_000_000, 5_000_000,
    10_000_000, 20_000_000, 25_000_000, 40_000_000, 50_000_000,
    100_000_000, 200_000_000, 250_000_000, 500_000_000, 1_000_000_000,
]
STREAM_MAX_RATE = 100_000_000
STREAM_MAX_BANDWIDTH = 320_000_000  # rate * enabled channels (resetHzComboBox)
BUFFER_MAX_RATE_STD = 250_000_000
BUFFER_MAX_RATE_PLUS = 500_000_000
GHZ_MAX_CHANNELS = 8
# Buffer depth limits from resetTimeComboBox (samples * channels).
BUFFER_MAX_BITS_STD = 1_000_000_000
BUFFER_MAX_BITS_PLUS = 3_500_000_000
RLE_DEPTH_FACTOR = 10

THRESHOLD_MIN_V = -5.0
THRESHOLD_MAX_V = 5.0
PWM_BASE_HZ = 200_000_000
PWM_MIN_HZ = 1
PWM_MAX_HZ = 20_000_000

TRIGGER_TYPES = ("none", "rising", "falling", "high", "low", "edge")


# ------------------------------------------------------------------ CRC32
def _make_table() -> List[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    return table


CRC_TABLE = _make_table()


def crc32_atk(data: bytes) -> int:
    """gCRC32 from util.cpp: reflected CRC-32 table, init 0, final xor 0xFFFFFFFF."""
    if len(data) < 1:
        return 0xFFFFFFFF
    crc = 0
    for b in data:
        crc = CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# ------------------------------------------------------- 2048-byte interleave
def _check_len(n: int) -> None:
    if n % BLOCK:
        raise ValueError("length must be a multiple of 2048, got %d" % n)


def convert_to_pc(data: bytes) -> bytes:
    """logic_analyzer_convert_to_pc: 4 lanes of 512 bytes -> interleaved u16 words."""
    _check_len(len(data))
    a = np.frombuffer(data, dtype=np.uint8).reshape(-1, 4, 256, 2)
    return a.transpose(0, 2, 1, 3).tobytes()


def convert_to_device(data: bytes) -> bytes:
    """logic_analyzer_convert_to_device: inverse of convert_to_pc."""
    _check_len(len(data))
    a = np.frombuffer(data, dtype=np.uint8).reshape(-1, 256, 4, 2)
    return a.transpose(0, 2, 1, 3).tobytes()


def normalize_in(data: bytes) -> bytes:
    """What USBControl does with every IN buffer: >=2048 bytes -> truncate to a
    multiple of 2048 and de-interleave; shorter buffers (MCU replies) are raw."""
    if len(data) >= BLOCK:
        n = len(data) // BLOCK * BLOCK
        return convert_to_pc(bytes(data[:n]))
    return bytes(data)


def normalize_stream(data: bytes) -> bytes:
    """What transfer_read_cb does with an async IN transfer during capture:
    truncate to a multiple of 2048 and de-interleave; shorter transfers are dropped."""
    n = len(data) // BLOCK * BLOCK
    return convert_to_pc(bytes(data[:n])) if n else b""


# ------------------------------------------------------------- OUT framing
def fpga_command(code: int, payload: bytes = b"") -> bytes:
    """USBControl::Write(code, data) + Write(data) + SendToDevice."""
    body = bytes([code & 0xFF, (len(payload) + 1) & 0xFF]) + bytes(payload)
    crc = crc32_atk(body)
    frame = bytes(8) + b"\x0a" + body + b"\x0b" + crc.to_bytes(4, "little") + b"\x00"
    padded = frame + bytes((-len(frame)) % BLOCK)
    return convert_to_device(padded)


def unframe_fpga_command(raw: bytes):
    """Inverse of fpga_command (used by tests / the fake device). Returns (code, payload)."""
    d = convert_to_pc(raw)
    if d[:8] != bytes(8) or d[8] != 0x0A:
        raise ValueError("bad frame header")
    code, ln = d[9], d[10]
    plen = ln - 1
    body = d[9:11 + plen]
    if d[11 + plen] != 0x0B:
        raise ValueError("bad frame tail")
    crc = int.from_bytes(d[12 + plen:16 + plen], "little")
    if crc != crc32_atk(body):
        raise ValueError("bad crc")
    return code, bytes(d[11:11 + plen])


def mcu_command(cmd: int, payload: bytes = b"") -> bytes:
    """USBControl::SendToMCU: raw 512-byte buffer [0x0a, cmd, payload...]."""
    buf = bytearray(512)
    buf[0] = 0x0A
    buf[1] = cmd
    buf[2:2 + len(payload)] = payload
    return bytes(buf)


# --------------------------------------------------------------- IN parser
@dataclass
class Packet:
    order: int
    payload: bytes  # payload[0] = channel id (low byte of u16), data from payload[2:]

    @property
    def channel(self) -> int:
        return self.payload[0] if self.payload else -1


MAX_PACKET = 4 + 0xFFFF + 2


class PacketParser:
    """Streaming version of the Analysis class (analysis.cpp).

    Frame: 0x0A, order(1..6), u16 len (LE), payload[len], 0x00, 0x0B.
    Bytes after the last complete frame are kept and prepended to the next
    feed() call (Analysis::getLastData).
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> List[Packet]:
        buf = self._buf + bytes(data)
        out: List[Packet] = []
        i, n, last = 0, len(buf), 0
        find = buf.find
        while True:
            i = find(b"\x0a", i)
            if i < 0 or i + 4 > n:
                break
            if 0 < buf[i + 1] < 7:
                ln = buf[i + 2] | (buf[i + 3] << 8)
                end = i + 4 + ln  # index of the 0x00 trailer
                if ln > 0 and end + 1 < n and buf[end] == 0x00 and buf[end + 1] == 0x0B:
                    out.append(Packet(buf[i + 1], buf[i + 4:end]))
                    i = end + 2
                    last = i
                    continue
            i += 1
        # keep everything after the last complete frame (it may be a partial frame)
        tail = buf[last:]
        # never keep more than one maximum-size frame of garbage
        if len(tail) > 2 * MAX_PACKET:
            tail = tail[-MAX_PACKET:]
        self._buf = tail
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


# ------------------------------------------------------------ data helpers
def rle_expand(data: bytes) -> bytes:
    """RLE payload = (count, byte) pairs; each byte holds 8 samples."""
    a = np.frombuffer(data, dtype=np.uint8)
    if len(a) % 2:
        a = a[:-1]
    return np.repeat(a[1::2], a[0::2]).tobytes()


def u40(b: bytes) -> int:
    return int.from_bytes(bytes(b[:5]).ljust(5, b"\x00"), "little")


# ------------------------------------------------------- parameter encoding
def rate_index(rate_hz: float) -> int:
    r = int(round(rate_hz))
    if r not in SAMPLE_RATES:
        raise ValueError(
            "unsupported sample rate %s Hz; allowed: %s"
            % (rate_hz, ", ".join(fmt_hz(x) for x in SAMPLE_RATES)))
    return SAMPLE_RATES.index(r)


def fmt_hz(hz: float) -> str:
    for unit, div in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= div:
            return ("%g " % (hz / div)) + unit
    return "%g Hz" % hz


def allowed_rates(mode: str, n_channels: int, plus: bool) -> List[int]:
    """Replicates resetHzComboBox() in SetContent.qml."""
    out = []
    if mode == "stream":
        for r in SAMPLE_RATES:
            if r > STREAM_MAX_RATE or r * n_channels > STREAM_MAX_BANDWIDTH:
                break
            out.append(r)
        return out
    for r in SAMPLE_RATES:
        if r == 1_000_000_000:
            if plus and n_channels <= GHZ_MAX_CHANNELS:
                out.append(r)
            continue
        if r > (BUFFER_MAX_RATE_PLUS if plus else BUFFER_MAX_RATE_STD):
            continue
        out.append(r)
    return out


def encode_threshold(volts: float) -> int:
    if not (THRESHOLD_MIN_V <= volts <= THRESHOLD_MAX_V):
        raise ValueError("threshold must be within %.0f..%.0f V" % (THRESHOLD_MIN_V, THRESHOLD_MAX_V))
    b = 0x80 if volts < 0 else 0
    # qAbs(qRound(v*10))
    return b + abs(_qround(volts * 10))


def encode_settings(buffer_mode: bool, rle: bool, threshold_v: float, rate_hz: float,
                    depth: int, trigger_depth: int) -> bytes:
    """setArray of SessionController::start (13 bytes)."""
    b0 = (0x80 if buffer_mode else 0) | (0x40 if rle else 0)
    out = bytes([b0, encode_threshold(threshold_v), rate_index(rate_hz) + 1])
    out += int(depth).to_bytes(5, "little") + int(trigger_depth).to_bytes(5, "little")
    return out


_TRIG_BITS = {  # (even-channel bits, odd-channel bits)
    "none": (0x10 | 0x20 | 0x40, 0x01 | 0x02 | 0x04),  # ChannelTriggerButton::Null
    "rising": (0x10, 0x01),
    "high": (0x40, 0x04),
    "falling": (0x20, 0x02),
    "low": (0x00, 0x00),
    "edge": (0x10 | 0x20, 0x01 | 0x02),  # DoubleEdge
}


def normalize_trigger(t: str) -> str:
    t = (t or "none").strip().lower()
    alias = {"r": "rising", "f": "falling", "h": "high", "l": "low", "1": "high", "0": "low",
             "e": "edge", "c": "edge", "both": "edge", "double": "edge", "x": "none",
             "any": "none", "": "none"}
    t = alias.get(t, t)
    if t not in _TRIG_BITS:
        raise ValueError("unknown trigger type %r; use one of %s" % (t, ", ".join(TRIGGER_TYPES)))
    return t


def encode_trigger(channels: Sequence[int], triggers: Dict[int, str], instant: bool,
                   n_channels: int = CHANNEL_COUNT) -> bytes:
    """dataBytes of SessionController::start, simple-trigger mode (type 0)."""
    out = bytearray()
    for i in range(0, n_channels, 2):
        b = 0
        for ii in range(2):
            ch = i + ii
            if ch not in channels:
                continue
            even, odd = (0x80, 0x08)
            b |= even if ii == 0 else odd
            bits = _TRIG_BITS[normalize_trigger(triggers.get(ch, "none"))]
            b |= bits[ii]
        out.append(b)
    out.append(1 if instant else 0)
    return bytes(out)


def encode_pwm(index: int, freq_hz: float, duty_percent: float) -> bytes:
    """SessionController::pwmStart payload."""
    if index not in (0, 1):
        raise ValueError("PWM index must be 0 or 1")
    if not (PWM_MIN_HZ <= freq_hz <= PWM_MAX_HZ):
        raise ValueError("PWM frequency must be within 1 Hz .. 20 MHz (GUI limits)")
    if not (1 <= duty_percent <= 99):
        raise ValueError("PWM duty must be within 1..99 %")
    period = _qround(PWM_BASE_HZ / float(freq_hz))
    high = _qround(period * (float(duty_percent) / 100.0))
    return bytes([0x21 if index else 0x11]) + period.to_bytes(4, "little") + high.to_bytes(4, "little")


def encode_pwm_stop(index: int) -> bytes:
    return bytes([0x20 if index else 0x10])


def _qround(x: float) -> int:
    """Qt5 qRound(double): equals floor(x + 0.5) for both signs (-2.5 -> -2)."""
    return int(math.floor(x + 0.5))


def sampling_depth(rate_hz: int, time_ms: float) -> int:
    """GUI formula: (quint64)setHz/1000*setTime (integer division first)."""
    return int((int(rate_hz) // 1000) * time_ms)


def trigger_depth(depth: int, position_percent: float) -> int:
    """GUI formula: (quint64)(samplingDepth/100 * triggerPosition)."""
    return int((int(depth) // 100) * float(position_percent))


def read_transfer_count(depth: int, n_channels: int, seconds: float) -> int:
    """maxTransferSize in DeviceRecvThread."""
    n = 16
    depth_s = depth * n_channels / max(1, math.ceil(seconds))
    if depth_s < 250_000_000:
        n = math.ceil(depth_s / 250_000_000 * READ_TRANSFER_COUNT)
    return max(1, min(READ_TRANSFER_COUNT, n))


@dataclass
class McuVersion:
    ok: bool
    bootloader: bool
    mcu_version: int = 0
    hw_version: int = 0
    level: int = 0


def parse_mcu_version(reply: bytes) -> Optional[McuVersion]:
    """Reply to MCU_GET_VERSION (connect.cpp)."""
    if len(reply) < 9 or reply[0] != 0x0A or reply[1] != 0x81 or reply[2] != 0x01:
        return None
    if reply[3] == 0x61:
        return McuVersion(True, False, reply[4] * 10 + reply[5], reply[6], reply[8])
    if reply[3] == 0x62:
        return McuVersion(False, True, 0, reply[6], 0)
    return None


@dataclass
class FpgaInfo:
    status_ok: bool
    usb: str
    fpga_version: int
    min_app_version: int
    name: str


def parse_device_info(payload: bytes) -> FpgaInfo:
    """Order-2 packet payload (connect.cpp)."""
    p = payload
    usb = {2: "2.0", 3: "3.0"}.get(p[3], "NULL") if len(p) > 3 else "NULL"
    fpga = p[5] * 100 + p[6] if len(p) > 6 else 0
    minv = p[7] * 100 + p[8] if len(p) > 8 else 0
    raw = bytes(p[9:]).split(b"\x00", 1)[0]
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        name = raw.decode("gbk", "replace")
    return FpgaInfo(len(p) > 2 and p[2] == 1, usb, fpga, minv, name.strip())
