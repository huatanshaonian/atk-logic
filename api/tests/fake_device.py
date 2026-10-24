"""A software model of the analyzer's USB side, used to test the host logic
(connect / capture / pwm / close) without hardware.

It speaks the same wire format the host expects: MCU replies are raw 512-byte
buffers; FPGA packets are framed (0x0A, order, len, payload, 0x00, 0x0B),
padded to 2048 bytes and sent through convert_to_device (the host de-interleaves
them with convert_to_pc).
"""

from collections import deque
from typing import Callable, Dict, List, Optional

import numpy as np

from atk_logic import protocol as P
from atk_logic.device import Transport


def packet(order: int, payload: bytes) -> bytes:
    return bytes([0x0A, order]) + len(payload).to_bytes(2, "little") + payload + b"\x00\x0b"


def to_wire(stream: bytes, chunk: int = P.READ_TRANSFER_SIZE) -> List[bytes]:
    stream = stream + bytes((-len(stream)) % P.BLOCK)
    out = []
    for i in range(0, len(stream), chunk):
        out.append(P.convert_to_device(stream[i:i + chunk]))
    return out


def rle_encode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        v = data[i]
        j = i
        while j < len(data) and data[j] == v and j - i < 255:
            j += 1
        out += bytes([j - i, v])
        i = j
    return bytes(out)


class FakeDevice(Transport):
    def __init__(self, signals: Dict[int, Callable[[int], np.ndarray]], level: int = 1,
                 name: str = "DL16", junk_bits: int = 37, packet_bytes: int = 3000,
                 rle_flags: int = 0, never_trigger: bool = False, bootloader: bool = False,
                 stream_overflow: bool = False, reject_params: bool = False):
        self.signals = signals
        self.level = level
        self.name = name
        self.junk_bits = junk_bits
        self.packet_bytes = packet_bytes
        self.rle_flags = rle_flags
        self.never_trigger = never_trigger
        self.bootloader = bootloader
        self.stream_overflow = stream_overflow
        self.reject_params = reject_params
        self.sync_q: deque = deque()
        self.chunks: deque = deque()
        self.log: List[tuple] = []
        self.settings: Optional[dict] = None
        self.trigger_bytes: Optional[bytes] = None
        self.expected: Dict[int, np.ndarray] = {}
        self.closed = False
        self.streaming = False

    # ------------------------------------------------------------ host -> dev
    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        data = bytes(data)
        if len(data) == 512 and data[0] == 0x0A:
            cmd = data[1]
            self.log.append(("mcu", cmd, data[2:4]))
            if cmd == P.MCU_GET_VERSION:
                r = bytearray(512)
                r[0:4] = b"\x0a\x81\x01" + (b"\x62" if self.bootloader else b"\x61")
                r[4], r[5], r[6], r[8] = 1, 5, 2, self.level  # mcu 15, hw 2
                self.sync_q.append(bytes(r))
            return
        assert len(data) % P.BLOCK == 0, "FPGA frames must be 2048-byte multiples"
        code, payload = P.unframe_fpga_command(data)
        self.log.append(("fpga", code, payload))
        if code == P.CMD_GET_DEVICE_DATA:
            pl = bytes([0, 0, 1, 3, 0, 1, 20, 1, 0]) + self.name.encode() + b"\x00"
            self.sync_q.extend(to_wire(packet(P.ORDER_DEVICE_INFO, pl), P.BLOCK))
        elif code == P.CMD_PARAMETER_SETTING:
            self.settings = {
                "buffer": bool(payload[0] & 0x80), "rle": bool(payload[0] & 0x40),
                "threshold": payload[1], "rate_code": payload[2],
                "depth": P.u40(payload[3:8]), "trig_depth": P.u40(payload[8:13]),
            }
        elif code == P.CMD_SIMPLE_TRIGGER:
            self.trigger_bytes = payload
            self._generate()
        elif code == P.CMD_STOP:
            self.sync_q.extend(to_wire(packet(P.ORDER_REPLY, bytes([0, 0, P.CMD_STOP])), P.BLOCK))

    # ------------------------------------------------------------ capture data
    def _generate(self) -> None:
        s = self.settings
        depth, trig = s["depth"], s["trig_depth"]
        enabled = []
        for i in range(P.CHANNEL_COUNT):
            b = self.trigger_bytes[i // 2]
            if b & (0x80 if i % 2 == 0 else 0x08):
                enabled.append(i)
        st = bytearray()
        if self.reject_params:
            st += packet(P.ORDER_REPLY, bytes([0, 0, P.CMD_SIMPLE_TRIGGER, 1]))
            self.chunks.extend(to_wire(bytes(st)))
            return
        if s["buffer"]:
            # trigger wait progress
            st += packet(P.ORDER_PROGRESS, bytes([0, 0]) + (trig // 2).to_bytes(5, "little"))
            if self.never_trigger:
                self.chunks.extend(to_wire(bytes(st)))
                return
            st += packet(P.ORDER_PROGRESS, bytes([0, 0]) + (trig + 10).to_bytes(5, "little"))
        rng = np.random.default_rng(1)
        per_ch = {}
        for ch in enabled:
            sig = np.asarray(self.signals.get(ch, lambda n: np.zeros(n, np.uint8))(depth), np.uint8) & 1
            self.expected[ch] = sig
            if s["buffer"]:
                junk = rng.integers(0, 2, self.junk_bits).astype(np.uint8)
                bits = np.concatenate([junk, sig])
            else:
                bits = sig
            per_ch[ch] = np.packbits(bits, bitorder="little").tobytes()
        if s["buffer"]:
            counts = [len(per_ch.get(i, b"")) for i in range(P.CHANNEL_COUNT)]
            cnt_bits = max(counts) * 8
            trigger_pos = cnt_bits - trig - self.junk_bits  # makes offset == junk_bits
            pl = bytes([0, 0]) + trigger_pos.to_bytes(5, "little")
            pl += b"".join(c.to_bytes(5, "little") for c in counts)
            if s["rle"]:
                pl += bytes([self.rle_flags])
            st += packet(P.ORDER_OFFSET, pl)
        # data packets, channels interleaved
        pos = {ch: 0 for ch in per_ch}
        while any(pos[ch] < len(per_ch[ch]) for ch in per_ch):
            for ch in per_ch:
                d = per_ch[ch][pos[ch]:pos[ch] + self.packet_bytes]
                if not d:
                    continue
                pos[ch] += len(d)
                body = rle_encode(d) if s["rle"] else d
                st += packet(P.ORDER_SAMPLES, bytes([ch, 0]) + body)
                if not s["buffer"]:
                    st += packet(P.ORDER_PROGRESS, bytes([0, 0]) + (1).to_bytes(5, "little"))
        st += packet(P.ORDER_END, bytes([0, 0, 1 if self.stream_overflow else 0]))
        self.chunks.extend(to_wire(bytes(st)))

    # ------------------------------------------------------------ dev -> host
    def read(self, length: int, timeout_ms: int) -> bytes:
        if not self.sync_q:
            return b""
        item = self.sync_q.popleft()
        if len(item) > length:
            self.sync_q.appendleft(item[length:])
            item = item[:length]
        return item

    def stream_start(self, n_transfers: int, size: int, timeout_ms: int) -> None:
        self.streaming = True
        self.n_transfers = n_transfers

    def stream_get(self, timeout_s: float) -> Optional[bytes]:
        if self.chunks:
            return self.chunks.popleft()
        return None

    def stream_stop(self) -> None:
        self.streaming = False

    def close(self) -> None:
        self.closed = True
