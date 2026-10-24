import json
import os
import subprocess
import sys

import numpy as np
import pytest

from atk_logic import Capture, decode, read_atkdl
from conftest import DECODERS, TEST_FILES

pytestmark = pytest.mark.skipif(not os.path.isdir(TEST_FILES), reason="runtime/test not present")


def f(name):
    return os.path.join(TEST_FILES, name)


def rows(res, key):
    return res["rows"][key]["texts"]


def test_uart_file_basic():
    c = read_atkdl(f("uart_tx_115200.atkdl"))
    assert c.samplerate == 10e6 and c.n_samples == 1_000_000 and list(c.channels) == [0]
    st = c.summary()["channels"][0]
    assert 110e3 < st["shortest_pulse_hz"] < 120e3  # ~115200 baud
    assert c.meta["decoders"][0]["stack"][0]["id"] == "uart"


def test_pwm_file_stats():
    st = read_atkdl(f("pwm_10M_30_25.atkdl")).summary()["channels"][0]
    assert st["frequency_hz"] == pytest.approx(10e6) and st["duty_percent"] == pytest.approx(30, abs=0.01)


def test_offset_channels():
    c = read_atkdl(f("eeprom_24c02_i2c.atkdl"))
    assert sorted(c.channels) == [6, 7] and c.n_samples == 100_000_000


def test_uart_decode():
    c = read_atkdl(f("uart_tx_115200.atkdl"))
    r = decode(c, [{"id": "uart", "channels": {"tx": 0},
                    "options": {"baudrate": 115200, "format": "ascii"}}], DECODERS)
    data = [a["text"] for a in r["annotations"] if a["ann"] == "tx-data"]
    assert "".join(data).startswith("ALIENTEK") and len(data) == 56
    assert not r["errors"]


def test_i2c_eeprom_stack_from_file():
    c = read_atkdl(f("eeprom_24c02_i2c.atkdl"))
    r = decode(c, c.meta["decoders"][0], DECODERS)
    ops = rows(r, "eeprom24xx.ops")
    assert ops[0] == "Byte write (addr=00, 1 byte): 53"
    assert any(o.startswith("Random access read") for o in ops)


def test_spi_flash_stack():
    c = read_atkdl(f("spi_flash_w25q128.atkdl"))
    r = decode(c, c.meta["decoders"][0]["stack"], DECODERS)
    assert "WarShipSTM32 SPI TEST" in rows(r, "spiflash.commands")[0]


def test_can():
    c = read_atkdl(f("can_500K.atkdl"))
    r = decode(c, [{"id": "can", "channels": {"can_rx": 0}, "options": {"nominal_bitrate": 500000}}], DECODERS)
    assert "Identifier: 18 (0x12)" in rows(r, "can.fields")


def test_bad_mapping_errors():
    c = read_atkdl(f("uart_tx_115200.atkdl"))
    with pytest.raises(ValueError, match="not in the capture"):
        decode(c, [{"id": "uart", "channels": {"rx": 5}}], DECODERS)
    with pytest.raises(ValueError, match="unknown option"):
        decode(c, [{"id": "uart", "channels": {"rx": 0}, "options": {"baud": 9600}}], DECODERS)
    with pytest.raises(ValueError, match="required channel"):
        decode(c, [{"id": "i2c", "channels": {"scl": 0}}], DECODERS)


# ---------------------------------------------------------------- synthetic
def uart_bits(text, baud, rate, idle=200):
    spb = int(rate / baud)
    bits = [1] * idle
    for ch in text.encode():
        frame = [0] + [(ch >> i) & 1 for i in range(8)] + [1]
        for b in frame:
            bits += [b] * spb
    return np.array(bits + [1] * idle, np.uint8)


def test_synthetic_uart_roundtrip_and_exports(tmp_path):
    c = Capture.from_bits(1e6, {0: uart_bits("Hello, 流体!", 9600, 1e6)})
    r = decode(c, [{"id": "uart", "channels": {"rx": 0}, "options": {"baudrate": 9600, "format": "hex"}}],
               DECODERS)
    data = bytes(int(a["text"], 16) for a in r["annotations"] if a["ann"] == "rx-data")
    assert data.decode() == "Hello, 流体!"
    # lossless edge JSON round trip
    p = tmp_path / "c.atkjson"
    c.save(str(p))
    c2 = Capture.from_atkjson(str(p))
    np.testing.assert_array_equal(c2.channel(0).bits(), c.channel(0).bits())
    # VCD / CSV are written and contain the edges
    c.save(str(tmp_path / "c.vcd"))
    c.save(str(tmp_path / "c.csv"))
    vcd = (tmp_path / "c.vcd").read_text()
    assert "$timescale 1000000 ps $end" in vcd and vcd.count("\n0!") == c.channel(0).edges.size // 2
    csv = (tmp_path / "c.csv").read_text().splitlines()
    assert csv[6].startswith("SystemTime, Time(s), Channel 0")
    assert len(csv) == 8 + len(c.channel(0).edges)


def test_wait_semantics():
    # h after a match at the same sample returns the same sample; skip counts from current
    from atk_logic.decode import DecodeSession
    import sigrokdecode as srd  # shim installed by DecodeSession / load
    bits = np.array([0, 0, 1, 1, 1, 0, 0, 1, 0, 0], np.uint8)
    c = Capture.from_bits(1e6, {0: bits})
    log = []

    class D(srd.Decoder):
        api_version = 3
        id = "t"
        channels = ({"id": "d", "name": "D", "desc": ""},)
        options = ()
        annotations = ()

        def decode(self):
            self.wait({0: "h"}); log.append(self.samplenum)       # 2
            self.wait({0: "h"}); log.append(self.samplenum)       # 2 again
            self.wait({0: "f"}); log.append(self.samplenum)       # 5
            self.wait({"skip": 2}); log.append(self.samplenum)    # 7
            self.wait([{0: "f"}, {"skip": 5}]); log.append((self.samplenum, self.matched))  # 8, (T,F)
            self.wait(); log.append(self.samplenum)               # 9
            self.wait({0: "r"})                                    # no more -> ends

    import types, sys
    mod = types.ModuleType("t")
    mod.Decoder = D
    sys.modules["t"] = mod
    DecodeSession(c, [{"id": "t", "channels": {"d": 0}}], DECODERS).run()
    assert log == [2, 2, 5, 7, (8, (True, False)), 9]


def test_cli_offline(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-m", "atk_logic", "decode", f("uart_tx_115200.atkdl"),
                          "--ann", "tx-data"], capture_output=True, text=True, env=env, check=True)
    d = json.loads(out.stdout)
    assert "".join(d["rows"]["uart.tx-data"]["texts"]).startswith("ALIENTEKALIENTEK")
    out = subprocess.run([sys.executable, "-m", "atk_logic", "open", f("pwm_10M_30_25.atkdl"),
                          "-o", str(tmp_path / "p.vcd")], capture_output=True, text=True, env=env, check=True)
    assert json.loads(out.stdout)["capture"]["channels"][0]["duty_percent"] == pytest.approx(30, abs=0.01)
    out = subprocess.run([sys.executable, "-m", "atk_logic", "capture", "-c", "0-3", "-r", "100M",
                          "-d", "1ms", "--mode", "stream"], capture_output=True, text=True, env=env)
    assert out.returncode != 0 and "error" in json.loads(out.stdout)
