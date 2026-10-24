"""USB access and device control (Windows + Linux, via libusb / python-libusb1).

The command sequences replicate the official GUI:
  connect  -> ConnectDevice::CheckDeviceCreanInfo   (pv/thread/connect.cpp)
  capture  -> Session::OrderStart + ThreadWork::DeviceRecvThread
  pwm      -> SessionController::pwmStart / pwmStop
  close    -> FramelessWindow shutdown (SetResetState(1), CloseAllPWM, Exit, SetResetState(0))
Firmware update / bootloader commands are intentionally NOT implemented.
"""

from __future__ import annotations

import datetime
import math
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Union

import numpy as np

from . import protocol as P
from .capture import Capture, Channel, shift_packed

THRESHOLD_PRESETS = {  # SetContent.qml thresholdLevelComboBox
    "5.0v": 2.5, "5v": 2.5, "3.3v": 1.6, "3v3": 1.6, "3.0v": 1.5, "3v": 1.5,
    "2.5v": 1.2, "1.8v": 0.9, "1.5v": 0.7, "1.2v": 0.6,
}
DEFAULT_THRESHOLD_V = 1.6  # GUI default: "3.3V CMOS"


class AtkError(Exception):
    """Base error. `hint` is a human-readable suggestion."""

    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


class DeviceNotFound(AtkError):
    pass


class DeviceAccessError(AtkError):
    pass


class DeviceStateError(AtkError):
    pass


class CaptureError(AtkError):
    pass


# =================================================================== transport
class Transport:
    """Minimal interface the device logic needs (real USB or a test fake)."""

    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        raise NotImplementedError

    def read(self, length: int, timeout_ms: int) -> bytes:
        """Bulk IN; return what arrived (possibly b'' on timeout)."""
        raise NotImplementedError

    def stream_start(self, n_transfers: int, size: int, timeout_ms: int) -> None:
        raise NotImplementedError

    def stream_get(self, timeout_s: float) -> Optional[bytes]:
        """Next completed IN transfer (raw bytes) or None."""
        raise NotImplementedError

    def stream_stop(self) -> None:
        raise NotImplementedError

    @property
    def no_device(self) -> bool:
        return False

    def close(self) -> None:
        pass


def _usb1():
    try:
        import usb1  # python-libusb1; Windows wheels bundle libusb-1.0.dll
    except ImportError as e:  # pragma: no cover
        raise AtkError("python package 'libusb1' is not installed",
                       "pip install libusb1  (Linux also needs the system libusb-1.0 library)") from e
    return usb1


def _access_hint() -> str:
    import sys
    if sys.platform.startswith("linux"):
        return ("Linux: install the udev rule (sudo cp linux/99-atk-logic.rules /etc/udev/rules.d/ && "
                "sudo udevadm control --reload && sudo udevadm trigger), then re-plug the analyzer; "
                "also make sure the ATK-Logic GUI is closed.")
    if sys.platform == "win32":
        return ("Windows: the device needs a WinUSB/libusb driver (installed by the official ATK-Logic "
                "installer, or bind WinUSB with Zadig to VID 1A86 PID FFCC); close the ATK-Logic GUI.")
    return "Check USB permissions and close the ATK-Logic GUI."


def list_devices() -> List[dict]:
    """Enumerate connected analyzers without opening them."""
    usb1 = _usb1()
    out = []
    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            try:
                if dev.getVendorID() != P.VENDOR_ID or dev.getProductID() != P.PRODUCT_ID:
                    continue
                ports = []
                try:
                    ports = list(dev.getPortNumberList() or [])
                except Exception:  # noqa: BLE001
                    pass
                out.append({
                    "index": len(out),
                    "bus": dev.getBusNumber(),
                    "address": dev.getDeviceAddress(),
                    "port": dev.getPortNumber(),
                    "port_path": ".".join(str(p) for p in ports),
                    "speed": {1: "low", 2: "full", 3: "high(USB2.0)", 4: "super(USB3.0)",
                              5: "super+(USB3.1)"}.get(dev.getDeviceSpeed(), "unknown"),
                    "vid": "%04x" % P.VENDOR_ID, "pid": "%04x" % P.PRODUCT_ID,
                })
            finally:
                dev.close()
    return out


class UsbTransport(Transport):
    def __init__(self, index: int = 0, bus: Optional[int] = None, address: Optional[int] = None):
        usb1 = self._u = _usb1()
        self._ctx = usb1.USBContext()
        self._ctx.open()
        self._handle = None
        self._claimed = False
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._pending = 0
        self._lock = threading.Lock()
        self._transfers = []
        self._no_device = False
        self.stream_error: Optional[str] = None
        try:
            devs = [d for d in self._ctx.getDeviceIterator(skip_on_error=True)
                    if d.getVendorID() == P.VENDOR_ID and d.getProductID() == P.PRODUCT_ID]
            if bus is not None and address is not None:
                devs = [d for d in devs if d.getBusNumber() == bus and d.getDeviceAddress() == address]
            if not devs:
                raise DeviceNotFound(
                    "no ATK-Logic analyzer (USB %04x:%04x) found" % (P.VENDOR_ID, P.PRODUCT_ID),
                    "Plug in the analyzer and check the cable/port (Linux: `lsusb | grep 1a86:ffcc`; "
                    "Windows: Device Manager). Close the ATK-Logic GUI if it is running.")
            if index >= len(devs):
                raise DeviceNotFound("device index %d out of range (%d found)" % (index, len(devs)))
            dev = devs[index]
            self.bus, self.address, self.port = dev.getBusNumber(), dev.getDeviceAddress(), dev.getPortNumber()
            try:
                self._handle = dev.open()
            except usb1.USBErrorAccess as e:
                raise DeviceAccessError("permission denied opening the analyzer (%s)" % e, _access_hint())
            except (usb1.USBErrorNotSupported, usb1.USBErrorNotFound) as e:
                raise DeviceAccessError("no usable driver for the analyzer (%s)" % e, _access_hint())
            except usb1.USBError as e:
                raise DeviceAccessError("cannot open the analyzer (%s)" % e, _access_hint())
            try:
                self._handle.setAutoDetachKernelDriver(True)
            except Exception:  # noqa: BLE001 - not supported on Windows
                pass
            try:
                self._handle.claimInterface(P.INTERFACE)
                self._claimed = True
            except usb1.USBErrorBusy as e:
                raise DeviceAccessError("the analyzer is in use by another program (%s)" % e,
                                        "Close the ATK-Logic GUI or other scripts using the device.")
            except usb1.USBErrorAccess as e:
                raise DeviceAccessError("cannot claim the USB interface (%s)" % e, _access_hint())
        except Exception:
            self.close()
            raise

    # -- sync ---------------------------------------------------------
    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        u = self._u
        try:
            self._handle.bulkWrite(P.EP_OUT, bytes(data), timeout_ms)
        except u.USBErrorNoDevice:
            self._no_device = True
            raise DeviceStateError("device disconnected", "Re-plug the analyzer.")
        except u.USBErrorTimeout:
            raise DeviceStateError("USB write timed out",
                                   "Re-plug the analyzer; if it persists, update firmware with the official GUI.")

    def read(self, length: int, timeout_ms: int) -> bytes:
        u = self._u
        try:
            return bytes(self._handle.bulkRead(P.EP_IN, length, timeout_ms))
        except u.USBErrorTimeout as e:
            return bytes(getattr(e, "received", b"") or b"")
        except u.USBErrorNoDevice:
            self._no_device = True
            return b""
        except u.USBErrorOverflow:
            return b""

    # -- async streaming ----------------------------------------------
    def _cb(self, t):
        u = self._u
        st = t.getStatus()
        if st in (u.TRANSFER_COMPLETED, u.TRANSFER_TIMED_OUT):
            n = t.getActualLength()
            if n > 0:
                self._q.put(bytes(t.getBuffer()[:n]))
            if self._running:
                try:
                    t.submit()
                    return
                except Exception as e:  # noqa: BLE001
                    self.stream_error = "resubmit failed: %s" % e
        elif st == u.TRANSFER_NO_DEVICE:
            self._no_device = True
        elif st != u.TRANSFER_CANCELLED:
            self.stream_error = "transfer status %s" % st
        with self._lock:
            self._pending -= 1

    def _loop(self):
        while True:
            with self._lock:
                pending = self._pending
            if not self._running and pending <= 0:
                break
            try:
                self._ctx.handleEventsTimeout(tv=0.05)
            except Exception as e:  # noqa: BLE001
                self.stream_error = str(e)
                break

    def stream_start(self, n_transfers: int, size: int, timeout_ms: int) -> None:
        self._q = queue.Queue()
        self._running = True
        self.stream_error = None
        self._transfers = []
        for _ in range(n_transfers):
            t = self._handle.getTransfer()
            t.setBulk(P.EP_IN, size, callback=self._cb, timeout=timeout_ms)
            self._transfers.append(t)
        for t in self._transfers:
            t.submit()
            with self._lock:
                self._pending += 1
        self._thread = threading.Thread(target=self._loop, name="atk-usb-events", daemon=True)
        self._thread.start()

    def stream_get(self, timeout_s: float) -> Optional[bytes]:
        try:
            return self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def stream_stop(self) -> None:
        self._running = False
        for t in self._transfers:
            try:
                if t.isSubmitted():
                    t.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        for t in self._transfers:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        self._transfers = []

    @property
    def no_device(self) -> bool:
        return self._no_device

    def close(self) -> None:
        if self._running:
            self.stream_stop()
        if self._handle is not None:
            if self._claimed:
                try:
                    self._handle.releaseInterface(P.INTERFACE)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None


# ====================================================================== device
@dataclass
class DeviceInfo:
    name: str
    usb: str
    mcu_version: int
    fpga_version: int
    hw_version: int
    level: int
    channels: int = P.CHANNEL_COUNT
    bus: Optional[int] = None
    address: Optional[int] = None

    @property
    def plus(self) -> bool:
        return self.level == 1

    def as_dict(self) -> dict:
        d = asdict(self)
        d["plus"] = self.plus
        d["model"] = self.name + (" Plus" if self.plus and "plus" not in self.name.lower() else "")
        d["max_buffer_rate_hz"] = P.BUFFER_MAX_RATE_PLUS if self.plus else P.BUFFER_MAX_RATE_STD
        if self.plus:
            d["max_buffer_rate_hz_8ch"] = 1_000_000_000
        d["stream_bandwidth_hz"] = P.STREAM_MAX_BANDWIDTH
        return d


@dataclass
class CaptureConfig:
    channels: List[int]
    samplerate: int
    depth: int
    threshold_v: float = DEFAULT_THRESHOLD_V
    mode: str = "buffer"
    rle: bool = False
    triggers: Dict[int, str] = field(default_factory=dict)
    trigger_position: float = 1.0
    timeout_s: Optional[float] = None


def parse_threshold(v: Union[str, float, None]) -> float:
    if v is None:
        return DEFAULT_THRESHOLD_V
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(" ", "").replace("cmos", "")
    if s in THRESHOLD_PRESETS:
        return THRESHOLD_PRESETS[s]
    return float(s.rstrip("v"))


class AtkLogic:
    """One opened analyzer.  Use as a context manager::

        with AtkLogic.open() as la:
            print(la.info)
            cap = la.capture(channels=[0, 1], samplerate=10e6, duration=0.01)
    """

    def __init__(self, transport: Transport, connect: bool = True, log=None):
        self.t = transport
        self.info: Optional[DeviceInfo] = None
        self._log = log or (lambda msg: None)
        self._pwm_on = [False, False]
        self._fpga_awake = False
        self._sleep = time.sleep
        self.silence_timeout_s = 2.5  # GUI: ~20 x 100 ms without packets -> "应答超时"
        if connect:
            try:
                self.connect()
            except Exception:
                self.t.close()
                raise

    @classmethod
    def open(cls, index: int = 0, bus: Optional[int] = None, address: Optional[int] = None,
             log=None) -> "AtkLogic":
        return cls(UsbTransport(index, bus, address), log=log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------- low level
    def _fpga(self, code: int, payload: bytes = b"") -> None:
        self.t.write(P.fpga_command(code, payload))

    def _mcu(self, cmd: int, payload: bytes = b"") -> None:
        self.t.write(P.mcu_command(cmd, payload))

    def _read_sync(self, length: int = 512, timeout_ms: int = 50):
        """USBControl::ReadSynchronous -> (ok, normalized data)."""
        raw = self.t.read(length, timeout_ms)
        return len(raw) > 0, P.normalize_in(raw)

    def _drain(self, limit: int = 1000) -> int:
        n = 0
        while n < limit:
            ok, _ = self._read_sync()
            if not ok:
                break
            n += 1
        return n

    def set_reset_state(self, state: int) -> None:
        """MCU 0x87: 1 = wake FPGA, 0 = put FPGA to sleep."""
        self._mcu(P.MCU_SET_RESET_STATE, bytes([state & 0xFF]))
        self._fpga_awake = bool(state)

    def _fpga_active(self) -> None:
        self.set_reset_state(1)
        self._read_sync()

    # ------------------------------------------------------------ connect
    def connect(self, max_attempts: int = 5) -> DeviceInfo:
        """Handshake (ConnectDevice::CheckDeviceCreanInfo)."""
        self._sleep(0.3)
        name, usb, mcu, fpga, hw, level = "", "NULL", 0, 0, 0, 0
        count = 0
        while True:
            # flush stale data, detect a device stuck in bootloader mode
            self._mcu(P.MCU_GET_VERSION)
            garbage = other = 0
            while True:
                ok, d = self._read_sync()
                if not ok:
                    break
                if len(d) >= 512 and d[0] > 200 and d[511] > 200:
                    garbage += 1
                    self._sleep(0.01)
                    if garbage > 120:
                        raise DeviceStateError(
                            "the analyzer looks like it is in bootloader / firmware-update mode",
                            "Open the official ATK-Logic GUI and let it update the firmware; "
                            "this API never flashes firmware.")
                else:
                    other += 1
                    if other == 100:
                        self._fpga(P.CMD_GET_DEVICE_DATA)
                    if other > 900:
                        self._sleep(0.01)
                    if other > 1100:
                        break
            self._log("connect attempt %d" % (count + 1))
            active = False
            if mcu == 0:
                self._mcu(P.MCU_GET_VERSION)
                ok, d = self._read_sync()
                v = P.parse_mcu_version(d) if ok else None
                if v is not None and v.bootloader:
                    raise DeviceStateError(
                        "the analyzer reports bootloader mode (firmware update pending)",
                        "Open the official ATK-Logic GUI to finish the firmware update.")
                if v is not None and v.ok:
                    level, mcu, hw = v.level, v.mcu_version, v.hw_version
                    self.set_reset_state(0)
                    self.set_reset_state(1)
                    self._drain(2000)
                    active = True
            else:
                active = True
            if active:
                self._fpga(P.CMD_GET_DEVICE_DATA)
                raw = self.t.read(2048, 50)
                if raw:
                    for pkt in P.PacketParser().feed(P.normalize_in(raw)):
                        if pkt.order != P.ORDER_DEVICE_INFO:
                            continue
                        fi = P.parse_device_info(pkt.payload)
                        if not fi.status_ok:
                            raise DeviceStateError("FPGA status error reported by the analyzer",
                                                   "Re-plug the analyzer; if it persists use the official GUI.")
                        usb, fpga = fi.usb, fi.fpga_version
                        if P.FPGA_MIN_VERSION_NUM > fpga:
                            raise DeviceStateError(
                                "FPGA firmware %d is older than required %d" % (fpga, P.FPGA_MIN_VERSION_NUM),
                                "Update the firmware with the official ATK-Logic GUI.")
                        if P.APP_VERSION_NUM < fi.min_app_version:
                            raise DeviceStateError(
                                "firmware requires host software >= %d (this API implements %d)"
                                % (fi.min_app_version, P.APP_VERSION_NUM),
                                "The protocol may have changed; check for an updated API.")
                        name = fi.name
                        break
            self._sleep(0.3)
            count += 1
            if count > max_attempts or self.t.no_device:
                break
            if name and usb != "NULL" and mcu and fpga and hw:
                break
        if self.t.no_device:
            raise DeviceStateError("device disconnected during connect", "Re-plug the analyzer.")
        if not (name and usb != "NULL" and mcu and fpga and hw):
            raise DeviceStateError(
                "device handshake failed (mcu=%d fpga=%d hw=%d name=%r usb=%s)" % (mcu, fpga, hw, name, usb),
                "Re-plug the analyzer and close other programs using it; update firmware with the official GUI.")
        self.close_all_pwm()
        self._sleep(0.03)
        self.set_reset_state(0)
        self._read_sync()
        self.info = DeviceInfo(name, usb, mcu, fpga, hw, level,
                               bus=getattr(self.t, "bus", None), address=getattr(self.t, "address", None))
        return self.info

    # ---------------------------------------------------------------- PWM
    def pwm(self, index: int, freq_hz: float, duty_percent: float = 50) -> dict:
        """Start PWM output `index` (0/1): 1 Hz..20 MHz, duty 1..99 %."""
        payload = P.encode_pwm(index, freq_hz, duty_percent)
        self._fpga_active()
        self._fpga(P.CMD_PWM, payload)
        self._pwm_on[index] = True
        period = int.from_bytes(payload[1:5], "little")
        high = int.from_bytes(payload[5:9], "little")
        return {"index": index, "requested_hz": freq_hz, "actual_hz": P.PWM_BASE_HZ / period,
                "duty_percent": 100.0 * high / period, "period_ticks": period, "high_ticks": high}

    def pwm_stop(self, index: int) -> None:
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(index))
        self._pwm_on[index] = False
        if not any(self._pwm_on):
            self.set_reset_state(0)

    def close_all_pwm(self) -> None:
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(1))
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(0))
        self._pwm_on = [False, False]

    # ------------------------------------------------------------ capture
    def build_config(self, channels: Iterable[int], samplerate: float,
                     samples: Optional[int] = None, duration: Optional[float] = None,
                     threshold: Union[str, float, None] = None, mode: str = "buffer",
                     rle: bool = False, trigger: Optional[Dict[int, str]] = None,
                     trigger_position: float = 1.0, timeout: Optional[float] = None) -> CaptureConfig:
        """Validate parameters like the GUI does; raises ValueError with a clear message."""
        chans = sorted(set(int(c) for c in channels))
        if not chans:
            raise ValueError("enable at least one channel")
        bad = [c for c in chans if not 0 <= c < P.CHANNEL_COUNT]
        if bad:
            raise ValueError("channel numbers must be 0..15, got %s" % bad)
        mode = mode.lower()
        if mode not in ("buffer", "stream"):
            raise ValueError("mode must be 'buffer' or 'stream'")
        plus = bool(self.info and self.info.plus)
        rate = int(round(float(samplerate)))
        P.rate_index(rate)
        allowed = P.allowed_rates(mode, len(chans), plus)
        if rate not in allowed:
            why = ""
            if mode == "stream":
                why = (" (stream mode: max 100 MHz and rate x channels <= 320 MHz;"
                       " use fewer channels or buffer mode)")
            elif rate == 1_000_000_000:
                why = " (1 GHz needs a Plus model and at most 8 channels)"
            else:
                why = " (500 MHz needs a Plus model)" if rate == 500_000_000 else ""
            raise ValueError("sample rate %s not allowed with %d channel(s) in %s mode%s; allowed: %s"
                             % (P.fmt_hz(rate), len(chans), mode, why,
                                ", ".join(P.fmt_hz(r) for r in allowed)))
        if samples is None and duration is None:
            raise ValueError("give samples or duration")
        depth = int(samples) if samples is not None else int(math.ceil(float(duration) * rate))
        if depth < 1000:
            raise ValueError("capture at least 1000 samples (got %d)" % depth)
        if mode == "buffer":
            cap = P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD
            if rle:
                cap *= P.RLE_DEPTH_FACTOR
            if depth * len(chans) > cap:
                raise ValueError(
                    "buffer too small: %d samples x %d channels > %d (%s); reduce duration/rate/channels%s"
                    % (depth, len(chans), cap, "Plus" if plus else "standard",
                       "" if rle else " or enable RLE (compressible signals only)"))
        if depth >= 1 << 40:
            raise ValueError("too many samples")
        trig = {}
        for k, v in (trigger or {}).items():
            k = int(k)
            if k not in chans:
                raise ValueError("trigger channel %d is not an enabled channel" % k)
            t = P.normalize_trigger(v)
            if t != "none":
                trig[k] = t
        if trig and mode == "stream":
            raise ValueError("triggers are only available in buffer mode (the GUI disables them in stream mode)")
        if not 1 <= float(trigger_position) <= 90:
            raise ValueError("trigger_position must be 1..90 %")
        thr = parse_threshold(threshold)
        P.encode_threshold(thr)
        return CaptureConfig(chans, rate, depth, thr, mode, bool(rle), trig,
                             float(trigger_position), timeout)

    def capture(self, channels: Iterable[int], samplerate: float, samples: Optional[int] = None,
                duration: Optional[float] = None, threshold: Union[str, float, None] = None,
                mode: str = "buffer", rle: bool = False, trigger: Optional[Dict[int, str]] = None,
                trigger_position: float = 1.0, timeout: Optional[float] = None,
                progress=None) -> Capture:
        """Acquire samples.

        channels:   iterable of channel numbers 0..15
        samplerate: Hz, one of protocol.SAMPLE_RATES
        samples / duration: capture length (per channel)
        threshold:  switching threshold in volts (-5..5) or preset "3.3V", "1.8V"...
        mode:       'buffer' (device memory, triggers, highest rates) or
                    'stream' (continuous USB streaming, <=100 MHz, rate*channels<=320 MHz)
        rle:        hardware run-length compression
        trigger:    {channel: 'rising'|'falling'|'high'|'low'|'edge'}; empty = start immediately
        trigger_position: percent of the buffer before the trigger (1..90)
        timeout:    seconds to wait for the trigger/data (None = wait for ever; Ctrl+C to abort)
        """
        cfg = self.build_config(channels, samplerate, samples, duration, threshold, mode, rle,
                                trigger, trigger_position, timeout)
        return self.capture_config(cfg, progress)

    def capture_config(self, cfg: CaptureConfig, progress=None) -> Capture:
        is_buffer = cfg.mode == "buffer"
        depth = cfg.depth
        trig_depth = P.trigger_depth(depth, cfg.trigger_position) if is_buffer else 0
        settings = P.encode_settings(is_buffer, cfg.rle, cfg.threshold_v, cfg.samplerate, depth, trig_depth)
        instant = not cfg.triggers
        trig_bytes = P.encode_trigger(cfg.channels, cfg.triggers, instant)
        seconds = depth / cfg.samplerate
        n_xfer = P.read_transfer_count(depth, len(cfg.channels), seconds)
        warnings: List[str] = []

        # --- Session::OrderStart
        self._fpga_active()
        self._drain(1000)
        self._fpga(P.CMD_PARAMETER_SETTING, settings)
        self._sleep(0.03)  # FPGA needs time to settle the threshold voltage
        start_time = datetime.datetime.now()
        self._fpga(P.CMD_SIMPLE_TRIGGER, trig_bytes)

        # --- ThreadWork::DeviceRecvThread
        parser = P.PacketParser()
        n_ch = P.CHANNEL_COUNT
        data: Dict[int, List[bytes]] = {c: [] for c in cfg.channels}
        got = {c: 0 for c in range(n_ch)}  # bytes appended per channel
        offsets = [0] * n_ch  # start offset (bits) still to drop
        trigger_pos = 0
        offset_seen = False
        exceed_capacity = exceed_bandwidth = False
        state = "running"  # running | done | stopped | timeout | error | nodevice
        error = None
        t0 = time.monotonic()
        last_packet = time.monotonic()
        silence_limit = self.silence_timeout_s
        triggered = not is_buffer or instant
        last_prog = -1

        self.t.stream_start(n_xfer, P.READ_TRANSFER_SIZE, 1200)
        try:
            while state == "running":
                chunk = self.t.stream_get(0.1)
                now = time.monotonic()
                if chunk is None:
                    if self.t.no_device:
                        state, error = "nodevice", "device disconnected during capture"
                        break
                    waiting_trigger = is_buffer and not instant and not triggered
                    if cfg.timeout_s is not None and now - t0 > cfg.timeout_s:
                        state = "timeout"
                        error = ("no trigger within %.1f s" % cfg.timeout_s) if waiting_trigger \
                            else ("capture did not finish within %.1f s" % cfg.timeout_s)
                        break
                    if now - last_packet > silence_limit:  # GUI: "应答超时"
                        state, error = "timeout", "device stopped answering (communication timeout)"
                        break
                    continue
                for pkt in parser.feed(P.normalize_stream(chunk)):
                    last_packet = now
                    o, p = pkt.order, pkt.payload
                    if o == P.ORDER_SAMPLES:
                        ch = p[0]
                        if ch >= n_ch:
                            raise CaptureError("data packet for invalid channel %d" % ch,
                                               "Re-plug the analyzer and try again.")
                        d = P.rle_expand(p[2:]) if cfg.rle else bytes(p[2:])
                        off = offsets[ch]
                        if off > 7:  # drop whole bytes now, remaining bits at the end
                            rb = off // 8
                            if len(d) > rb:
                                d = d[rb:]
                                offsets[ch] = off & 7
                            else:
                                offsets[ch] = off - len(d) * 8
                                d = b""
                        if d:
                            if ch in data:
                                data[ch].append(d)
                            got[ch] += len(d)
                        if progress and cfg.channels and ch == cfg.channels[0]:
                            pr = min(99, int(got[ch] * 8 * 100 / depth))
                            if pr != last_prog:
                                last_prog = pr
                                progress("transfer", pr)
                    elif o == P.ORDER_OFFSET:
                        if offset_seen:
                            raise CaptureError("unexpected second offset packet",
                                               "Re-plug the analyzer and try again.")
                        offset_seen = True
                        if len(p) < 7:
                            raise CaptureError("malformed offset packet")
                        trigger_pos = P.u40(p[2:7])
                        rest = p[7:]
                        if is_buffer:
                            if len(rest) < n_ch * 5:
                                raise CaptureError("malformed offset packet (channel counts)")
                            counts = [P.u40(rest[i * 5:i * 5 + 5]) for i in range(n_ch)]
                            for i, cnt in enumerate(counts):
                                if cnt and cnt * 8 > depth:
                                    offsets[i] = max(0, cnt * 8 - trig_depth - trigger_pos)
                                else:
                                    offsets[i] = 0
                            rest = rest[n_ch * 5:]
                            if cfg.rle:
                                if len(rest) < 1:
                                    raise CaptureError("malformed offset packet (flags)")
                                exceed_capacity = bool(rest[0] & 1)
                                exceed_bandwidth = bool(rest[0] & 2)
                                if exceed_capacity:
                                    nz = [counts[i] for i in cfg.channels if counts[i]]
                                    mn = min(nz) if nz else 0
                                    for i in range(n_ch):
                                        offsets[i] = (counts[i] - mn) * 8 if (counts[i] and i in cfg.channels) else 0
                                    state = "stopped"
                                if exceed_bandwidth:
                                    state = "done"
                        triggered = True
                    elif o == P.ORDER_REPLY:
                        if len(p) >= 3:
                            code = p[2]
                            if code == P.CMD_STOP:
                                state = "done"
                            elif code == P.CMD_SIMPLE_TRIGGER and len(p) >= 4 and p[3] != 3:
                                raise CaptureError("the analyzer rejected the capture parameters",
                                                   "Choose another sample rate / depth / channel set.")
                    elif o == P.ORDER_END:
                        state = "done"
                        if not is_buffer and len(p) >= 3 and p[2] == 1:
                            exceed_capacity = True
                    elif o == P.ORDER_PROGRESS:
                        if len(p) < 7:
                            raise CaptureError("malformed progress packet")
                        val = P.u40(p[2:7])
                        if is_buffer:
                            if val > trig_depth:
                                triggered = True
                            if progress:
                                pr = min(100, int(val * 100 / depth)) if depth else 0
                                progress("triggered" if val > trig_depth else "pre-trigger", pr)
                        elif progress:
                            progress("stream", 0)
                    if state != "running":
                        break
        except KeyboardInterrupt:
            state, error = "stopped", "interrupted by user"
        except CaptureError as e:
            state, error = "error", str(e)
        finally:
            # end of DeviceRecvThread: stop reader, send Stop, flush the MCU
            self.t.stream_stop()
            try:
                self._fpga(P.CMD_STOP)
            except AtkError:
                pass
            self._drain(1000)

        if exceed_bandwidth:
            warnings.append("data exceeded the hardware bandwidth; data is invalid")
        if exceed_capacity:
            warnings.append("data exceeded the hardware capacity; only the last valid part is kept")
        if error:
            warnings.append(error)

        chans: Dict[int, Channel] = {}
        avail = []
        for c in cfg.channels:
            raw = b"".join(data[c])
            packed = np.frombuffer(raw, np.uint8) if raw else np.zeros(0, np.uint8)
            packed = shift_packed(packed, offsets[c])  # remaining start offset (bits)
            nbits = len(packed) * 8
            avail.append(nbits)
            chans[c] = Channel(c, packed, min(nbits, depth))
        n = min([depth] + avail) if avail else 0
        if n == 0 and state in ("error", "timeout", "nodevice") and error:
            raise CaptureError(error, "Check wiring/trigger settings; re-plug the analyzer if it persists.")
        if n < depth and state == "done" and not exceed_capacity:
            warnings.append("received %d of %d samples" % (n, depth))
        trig_sample = trig_depth if is_buffer else (trigger_pos or None)
        if trig_sample is not None and trig_sample >= n:
            trig_sample = None
        meta = {
            "source": "device",
            "device": self.info.as_dict() if self.info else None,
            "mode": cfg.mode, "rle": cfg.rle, "threshold_v": cfg.threshold_v,
            "requested_samples": depth,
            "triggers": {str(k): v for k, v in cfg.triggers.items()},
            "trigger_position_percent": cfg.trigger_position if is_buffer else None,
            "state": state,
            "warnings": warnings,
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
        return Capture(cfg.samplerate, chans, n, trig_sample, meta, start_time)

    # -------------------------------------------------------------- close
    def close(self, keep_pwm: bool = False) -> None:
        """Shutdown like the GUI on exit; keep_pwm=True leaves PWM outputs running
        (skips CloseAllPWM / Exit / sleep) and only releases the USB interface."""
        try:
            if not keep_pwm and self.info is not None and not self.t.no_device:
                self.set_reset_state(1)
                self.close_all_pwm()
                self._fpga(P.CMD_EXIT)
                self.set_reset_state(0)
        except AtkError:
            pass
        finally:
            self.t.close()
