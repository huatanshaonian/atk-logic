import binascii
import os

import numpy as np
import pytest

from atk_logic import protocol as P


def test_crc_table_matches_util_cpp():
    # first/last entries copied from pv/static/util.cpp
    assert P.CRC_TABLE[:4] == [0x00000000, 0x77073096, 0xEE0E612C, 0x990951BA]
    assert P.CRC_TABLE[-1] == 0x2D02EF8D


@pytest.mark.parametrize("data", [b"\x11\x0e", b"123456789", os.urandom(77), b"\x00" * 10])
def test_crc_equals_zlib_variant(data):
    # gCRC32: init 0, final xor -> equals zlib crc32 seeded with 0xFFFFFFFF
    assert P.crc32_atk(data) == binascii.crc32(data, 0xFFFFFFFF)


def test_crc_empty():
    assert P.crc32_atk(b"") == 0xFFFFFFFF


def test_interleave_roundtrip_and_layout():
    src = os.urandom(4096)
    dev = P.convert_to_device(src)
    assert P.convert_to_pc(dev) == src
    # u16 word k of a 2048 block goes to lane k%4 at index k//4 (usb_base.cpp)
    words = np.frombuffer(src[:2048], "<u2")
    lanes = np.frombuffer(dev[:2048], "<u2").reshape(4, 256)
    for k in (0, 1, 2, 3, 4, 1023):
        assert lanes[k % 4][k // 4] == words[k]


def test_interleave_rejects_bad_length():
    with pytest.raises(ValueError):
        P.convert_to_pc(b"\x00" * 100)


def test_normalize_in():
    assert P.normalize_in(b"\x0a\x81") == b"\x0a\x81"  # MCU reply untouched
    raw = os.urandom(5000)
    assert P.normalize_in(raw) == P.convert_to_pc(raw[:4096])


def test_fpga_command_layout():
    payload = bytes(range(13))
    raw = P.fpga_command(P.CMD_PARAMETER_SETTING, payload)
    assert len(raw) == 2048
    d = P.convert_to_pc(raw)
    body = bytes([0x11, 14]) + payload
    assert d[:8] == bytes(8) and d[8] == 0x0A
    assert d[9:9 + len(body)] == body
    assert d[9 + len(body)] == 0x0B
    assert int.from_bytes(d[10 + len(body):14 + len(body)], "little") == P.crc32_atk(body)
    assert d[14 + len(body):] == bytes(2048 - 14 - len(body))
    assert P.unframe_fpga_command(raw) == (0x11, payload)


def test_fpga_command_no_payload():
    assert P.unframe_fpga_command(P.fpga_command(P.CMD_STOP)) == (0x15, b"")


def test_mcu_command():
    b = P.mcu_command(P.MCU_SET_RESET_STATE, b"\x01")
    assert len(b) == 512 and b[:3] == b"\x0a\x87\x01" and not any(b[3:])


def pk(order, payload):
    return bytes([0x0A, order]) + len(payload).to_bytes(2, "little") + payload + b"\x00\x0b"


def test_parser_split_and_garbage():
    stream = b"\x00\x0a\x09junk" + pk(1, b"\x03\x00" + b"\xaa" * 300) + b"\x00\x00" + pk(5, b"\x00\x00\x01\x02\x03\x04\x05") + pk(6, b"\x00\x00\x00")
    for cut in (1, 7, 100, 305, 311, len(stream) - 1):
        p = P.PacketParser()
        got = p.feed(stream[:cut]) + p.feed(stream[cut:])
        assert [g.order for g in got] == [1, 5, 6], cut
        assert got[0].channel == 3 and got[0].payload[2:] == b"\xaa" * 300
        assert P.u40(got[1].payload[2:7]) == 0x0504030201


def test_parser_bytewise():
    stream = pk(4, b"\x00\x00\x15") * 3
    p = P.PacketParser()
    got = []
    for b in stream:
        got += p.feed(bytes([b]))
    assert [g.payload[2] for g in got] == [0x15] * 3
    assert p.pending == 0


def test_rle_expand():
    assert P.rle_expand(bytes([3, 0xAB, 1, 0x00, 0, 0x11, 2, 0xFF])) == b"\xab\xab\xab\x00\xff\xff"


def test_encode_settings_matches_gui():
    # buffer + RLE, 3.3V CMOS (1.6 V), 100 MHz (index 9), 1 s, trigger 5 %
    depth = P.sampling_depth(100_000_000, 1000)
    trig = P.trigger_depth(depth, 5)
    b = P.encode_settings(True, True, 1.6, 100e6, depth, trig)
    assert b[0] == 0xC0 and b[1] == 16 and b[2] == 10
    assert P.u40(b[3:8]) == 100_000_000 and P.u40(b[8:13]) == 5_000_000
    assert P.encode_settings(False, False, -2.5, 1e6, 1000, 0)[:3] == bytes([0, 0x80 + 25, 1])
    with pytest.raises(ValueError):
        P.encode_settings(True, False, 6, 1e6, 1000, 0)
    with pytest.raises(ValueError):
        P.rate_index(3_000_000)


def test_encode_trigger():
    t = P.encode_trigger([0, 1, 3], {0: "rising", 3: "low"}, instant=False)
    assert len(t) == 9
    assert t[0] == (0x80 | 0x10) | (0x08 | 0x07)  # ch0 rising, ch1 enabled/no trigger
    assert t[1] == 0x08  # ch2 disabled, ch3 low
    assert t[2:8] == bytes(6) and t[8] == 0
    assert P.encode_trigger([4], {4: "edge"}, False)[2] == 0x80 | 0x30
    assert P.encode_trigger([5], {5: "high"}, True)[2] == 0x08 | 0x04
    assert P.encode_trigger([0], {}, True)[-1] == 1
    with pytest.raises(ValueError):
        P.encode_trigger([0], {0: "sideways"}, False)


def test_encode_pwm():
    b = P.encode_pwm(1, 1000, 25)
    assert b[0] == 0x21
    assert int.from_bytes(b[1:5], "little") == 200000
    assert int.from_bytes(b[5:9], "little") == 50000
    assert P.encode_pwm(0, 3, 50)[0] == 0x11
    assert P.encode_pwm_stop(0) == b"\x10" and P.encode_pwm_stop(1) == b"\x20"
    with pytest.raises(ValueError):
        P.encode_pwm(0, 30e6, 50)


def test_allowed_rates():
    assert max(P.allowed_rates("stream", 3, False)) == 100e6
    assert max(P.allowed_rates("stream", 4, False)) == 50e6
    assert max(P.allowed_rates("stream", 16, False)) == 20e6
    assert max(P.allowed_rates("buffer", 16, False)) == 250e6
    assert max(P.allowed_rates("buffer", 16, True)) == 500e6
    assert max(P.allowed_rates("buffer", 8, True)) == 1e9


def test_read_transfer_count():
    assert P.read_transfer_count(1_000_000, 1, 0.1) == 1
    assert P.read_transfer_count(100_000_000, 16, 1) == 16


def test_parse_replies():
    r = bytearray(512)
    r[:9] = bytes([0x0A, 0x81, 1, 0x61, 1, 5, 2, 0, 1])
    v = P.parse_mcu_version(bytes(r))
    assert v.ok and v.mcu_version == 15 and v.hw_version == 2 and v.level == 1
    r[3] = 0x62
    assert P.parse_mcu_version(bytes(r)).bootloader
    fi = P.parse_device_info(bytes([0, 0, 1, 3, 0, 1, 16, 1, 0]) + b"DL16\x00\x00")
    assert fi.status_ok and fi.usb == "3.0" and fi.fpga_version == 116 and fi.name == "DL16"


def test_normalize_stream_drops_short_and_truncates():
    from atk_logic import protocol as P
    assert P.normalize_stream(b"\x0a" * 100) == b""
    block = bytes(range(256)) * 8  # 2048 bytes
    raw = P.convert_to_device(block) + b"\xff" * 10
    assert P.normalize_stream(raw) == block
