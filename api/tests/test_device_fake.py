import numpy as np
import pytest

from atk_logic import protocol as P
from atk_logic.device import AtkLogic, CaptureError, DeviceStateError
from fake_device import FakeDevice


def square(period, duty=0.5, phase=0):
    def gen(n):
        k = (np.arange(n) + phase) % period
        return (k < period * duty).astype(np.uint8)
    return gen


def noise(seed):
    def gen(n):
        return np.random.default_rng(seed).integers(0, 2, n).astype(np.uint8)
    return gen


def make(fake):
    la = AtkLogic(fake, connect=False)
    la._sleep = lambda s: None
    la.silence_timeout_s = 0.2
    la.connect()
    return la


def test_connect_sequence():
    fake = FakeDevice({}, level=1, name="DL16")
    la = make(fake)
    i = la.info
    assert (i.name, i.usb, i.mcu_version, i.fpga_version, i.hw_version, i.plus) == ("DL16", "3.0", 15, 120, 2, True)
    # GUI order: version query, reset 0 then 1, FPGA info, close PWMs, reset 0
    mcu = [(e[1], e[2][:1]) for e in fake.log if e[0] == "mcu"]
    assert mcu[:2] == [(0x81, b"\x00"), (0x81, b"\x00")]
    assert (0x87, b"\x00") in mcu and (0x87, b"\x01") in mcu
    fpga = [(e[1], e[2]) for e in fake.log if e[0] == "fpga"]
    assert fpga == [(0x10, b""), (0x17, b"\x20"), (0x17, b"\x10")]
    assert fake.log[-1] == ("mcu", 0x87, b"\x00\x00")


def test_bootloader_refused():
    with pytest.raises(DeviceStateError) as e:
        make(FakeDevice({}, bootloader=True))
    assert "official ATK-Logic GUI" in e.value.hint


@pytest.mark.parametrize("rle", [False, True])
def test_buffer_capture_offset_trim(rle):
    sig = {0: noise(1), 3: square(40, 0.25), 15: square(1000, 0.5, 13)}
    fake = FakeDevice(sig, junk_bits=37, packet_bytes=1500)
    la = make(fake)
    cap = la.capture(channels=[0, 3, 15], samplerate=10e6, samples=100_000, rle=rle,
                     trigger={3: "rising"}, trigger_position=10)
    assert fake.settings == {"buffer": True, "rle": rle, "threshold": 16, "rate_code": 5,
                             "depth": 100_000, "trig_depth": 10_000}
    assert cap.n_samples == 100_000
    assert cap.trigger_sample == 10_000
    for ch in (0, 3, 15):
        np.testing.assert_array_equal(cap.channel(ch).bits(), fake.expected[ch])
    assert cap.meta["state"] == "done" and not cap.meta["warnings"]
    # Stop is always sent after the capture (DeviceRecvThread)
    assert ("fpga", P.CMD_STOP, b"") in fake.log
    s = cap.summary()["channels"][1]
    assert abs(s["frequency_hz"] - 250e3) < 1 and abs(s["duty_percent"] - 25) < 0.1


def test_trigger_bytes_and_wake():
    fake = FakeDevice({1: square(10)})
    la = make(fake)
    la.capture(channels=[0, 1], samplerate=1e6, samples=2000, trigger={1: "falling"}, threshold="1.8V")
    assert fake.trigger_bytes == bytes([0x80 | 0x70 | 0x08 | 0x02]) + bytes(7) + b"\x00"
    assert fake.settings["threshold"] == 9
    idx = [k for k, e in enumerate(fake.log) if e[:2] == ("fpga", 0x11)][0]
    assert fake.log[idx - 1][:2] == ("mcu", 0x87) or ("mcu", 0x87, b"\x01\x00") in fake.log[:idx]


def test_stream_capture_rle():
    fake = FakeDevice({2: square(7, 3 / 7)})
    la = make(fake)
    cap = la.capture(channels=[2], samplerate=100e6, samples=50_000, mode="stream", rle=True)
    assert fake.settings["buffer"] is False and fake.settings["rle"] is True
    np.testing.assert_array_equal(cap.channel(2).bits(), fake.expected[2])
    assert cap.trigger_sample is None


def test_stream_overflow_warning():
    fake = FakeDevice({0: square(8)}, stream_overflow=True)
    la = make(fake)
    cap = la.capture(channels=[0], samplerate=10e6, samples=10_000, mode="stream")
    assert any("capacity" in w for w in cap.meta["warnings"])


def test_rle_capacity_flag():
    fake = FakeDevice({0: square(8)}, rle_flags=1)
    la = make(fake)
    cap = la.capture(channels=[0], samplerate=10e6, samples=10_000, rle=True)
    assert any("capacity" in w for w in cap.meta["warnings"])


def test_trigger_timeout():
    fake = FakeDevice({0: square(8)}, never_trigger=True)
    la = make(fake)
    la.silence_timeout_s = 0.3
    with pytest.raises(CaptureError):
        la.capture(channels=[0], samplerate=10e6, samples=10_000, trigger={0: "rising"}, timeout=0.2)
    assert ("fpga", P.CMD_STOP, b"") in fake.log


def test_rejected_parameters():
    fake = FakeDevice({0: square(8)}, reject_params=True)
    la = make(fake)
    with pytest.raises(CaptureError) as e:
        la.capture(channels=[0], samplerate=10e6, samples=10_000)
    assert "rejected" in str(e.value)


@pytest.mark.parametrize("kw,msg", [
    (dict(channels=[0, 1, 2, 3], samplerate=100e6, samples=1000, mode="stream"), "320 MHz"),
    (dict(channels=[0], samplerate=1e9, samples=10_000), "Plus"),
    (dict(channels=list(range(9)), samplerate=1e9, samples=10_000), "8 channels"),
    (dict(channels=[0], samplerate=3e6, samples=10_000), "unsupported sample rate"),
    (dict(channels=[16], samplerate=1e6, samples=10_000), "0..15"),
    (dict(channels=[0], samplerate=1e6, samples=10), "at least 1000"),
    (dict(channels=[0], samplerate=1e6, samples=10_000, trigger={1: "rising"}), "not an enabled"),
    (dict(channels=[0], samplerate=1e6, samples=10_000, mode="stream", trigger={0: "rising"}), "buffer mode"),
    (dict(channels=list(range(16)), samplerate=250e6, samples=100_000_000), "buffer too small"),
    (dict(channels=[0], samplerate=1e6, samples=10_000, threshold=7), "threshold"),
])
def test_validation(kw, msg):
    fake = FakeDevice({}, level=0)
    la = make(fake)
    with pytest.raises(ValueError) as e:
        la.capture(**kw)
    assert msg in str(e.value)


def test_pwm_and_close():
    fake = FakeDevice({}, level=0)
    la = make(fake)
    res = la.pwm(1, 1e6, 30)
    assert res["actual_hz"] == 1e6 and res["duty_percent"] == 30
    assert fake.log[-1] == ("fpga", 0x17, bytes([0x21]) + (200).to_bytes(4, "little") + (60).to_bytes(4, "little"))
    la.pwm_stop(1)
    assert fake.log[-2] == ("fpga", 0x17, b"\x20") and fake.log[-1][:2] == ("mcu", 0x87)
    n = len(fake.log)
    la.close()
    seq = [e[:2] for e in fake.log[n:]]
    assert seq == [("mcu", 0x87), ("fpga", 0x17), ("fpga", 0x17), ("fpga", 0x18), ("mcu", 0x87)]
    assert fake.closed


def test_close_keep_pwm():
    fake = FakeDevice({}, level=0)
    la = make(fake)
    la.pwm(0, 1000, 50)
    n = len(fake.log)
    la.close(keep_pwm=True)
    assert len(fake.log) == n and fake.closed
