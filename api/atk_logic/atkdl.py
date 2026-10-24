"""Reader for ATK-Logic project files (*.atkdl).

Format (ThreadWork::SaveSourceFileThreadPara / LoadSourceFileThreadPara):
  zip archive containing
    channel.ini           SessionName=, SamplingFrequency=<kHz>, SamplingDepth=,
                          TriggerSamplingDepth=, Decodes=<id,id..>, GlitchRemoval=
    set.ini               GUI session settings (QSettings ini, JSON values)
    vernier.ini / measure.ini
    <decodeId>            JSON describing a decoder stack configured in the GUI
    <ch>/channel.ini      line1 name, line2 start offset (bits), line3 max sample
                          (bits, offset included), then one "tog,value" line
                          per node (node = 64 blocks of 1 MiB)
    <ch>/<node>-<blk>.bin 1 MiB raw block (LSB-first packed samples)
  A block whose bit is set in `tog` is constant (= corresponding bit of `value`);
  a block without .bin file and without tog bit reads as 0.
"""

from __future__ import annotations

import configparser
import json
import re
import zipfile
from typing import Dict, List, Optional

import numpy as np

from .capture import Capture, Channel, shift_packed

BLOCK_BYTES = 1024 * 1024
NODE_BLOCKS = 64


def _lines(raw: bytes) -> List[str]:
    return raw.decode("utf-8", "replace").replace("\r", "").split("\n")


def _ini(raw: bytes) -> Dict[str, str]:
    d = {}
    for ln in _lines(raw):
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def read_atkdl(path: str, channels: Optional[List[int]] = None) -> Capture:
    """Load an .atkdl project into a Capture (only channels that hold data)."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        top = _ini(z.read("channel.ini")) if "channel.ini" in names else {}
        freq_khz = int(top.get("SamplingFrequency", "0") or 0)
        if freq_khz <= 0:
            raise ValueError("%s: missing SamplingFrequency" % path)
        depth = int(top.get("SamplingDepth", "0") or 0)
        trig = int(top.get("TriggerSamplingDepth", "0") or 0)

        settings = _read_settings(z, names)
        decoders = []
        for did in (top.get("Decodes") or "").split(","):
            did = did.strip()
            if did and did in names:
                try:
                    decoders.append(_decoder_stack(json.loads(z.read(did).decode("utf-8"))))
                except Exception as e:  # keep loading the samples
                    decoders.append({"error": str(e), "id": did})

        bins_by_ch: Dict[int, Dict[int, str]] = {}
        for n in names:
            m = re.match(r"^(\d+)/(\d+)-(\d+)\.bin$", n)
            if m:
                ch, node, blk = (int(x) for x in m.groups())
                bins_by_ch.setdefault(ch, {})[node * NODE_BLOCKS + blk] = n

        chans: Dict[int, Channel] = {}
        lengths = []
        ch_ids = sorted(int(n.split("/")[0]) for n in names
                        if re.match(r"^\d+/channel\.ini$", n))
        for ch in ch_ids:
            if channels is not None and ch not in channels:
                continue
            ls = _lines(z.read("%d/channel.ini" % ch))
            name = ls[0] if ls else "Channel %d" % ch
            offset = int(ls[1] or 0) if len(ls) > 1 else 0
            max_sample = int(ls[2] or 0) if len(ls) > 2 else 0
            nodes = []
            for ln in ls[3:]:
                if "," in ln:
                    t, v = ln.split(",", 1)
                    nodes.append((int(t), int(v)))
            has_tog = any(t for t, _ in nodes)
            if max_sample <= offset or not (bins_by_ch.get(ch) or has_tog):
                continue
            n_bits = max_sample - offset
            if depth:
                n_bits = min(n_bits, depth)
            need_bytes = (offset + n_bits + 7) // 8
            packed = np.zeros(need_bytes, np.uint8)
            n_blocks = (need_bytes + BLOCK_BYTES - 1) // BLOCK_BYTES
            bins = bins_by_ch.get(ch, {})
            for b in range(n_blocks):
                node, blk = divmod(b, NODE_BLOCKS)
                lo = b * BLOCK_BYTES
                hi = min(need_bytes, lo + BLOCK_BYTES)
                t, v = nodes[node] if node < len(nodes) else (0, 0)
                if (t >> blk) & 1:
                    if (v >> blk) & 1:
                        packed[lo:hi] = 0xFF
                elif b in bins:
                    raw = np.frombuffer(z.read(bins[b]), np.uint8)
                    packed[lo:hi] = raw[:hi - lo]
            packed = shift_packed(packed, offset)
            chans[ch] = Channel(ch, packed, n_bits, name)
            lengths.append(n_bits)

    n = min(lengths) if lengths else 0
    rate = freq_khz * 1000.0
    meta = {
        "source": "atkdl",
        "file": path,
        "session_name": top.get("SessionName"),
        "sampling_depth": depth,
        "settings": settings,
        "decoders": decoders,
    }
    return Capture(rate, chans, n, trig if trig < n else None, meta)


def _read_settings(z: zipfile.ZipFile, names) -> dict:
    if "set.ini" not in names:
        return {}
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(z.read("set.ini").decode("utf-8", "replace"))
    except configparser.Error:
        return {}
    out = {}
    for sec in cp.sections():
        for k, v in cp.items(sec):
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        out["device_section"] = sec
    s = out.get("settingData") or {}
    if isinstance(s, dict):
        out["mode"] = "buffer" if s.get("isBuffer") else "stream"
    return out


def _decoder_stack(d: dict) -> dict:
    """Convert the GUI's decoder JSON into the decode.py stack format:
    {"stack":[{"id","channels":{id:ch},"options":{id:value}}...]} lowest first."""
    layers = []
    for key, v in d.items():
        if key == "main" or not isinstance(v, dict) or "main" not in v:
            continue
        chans = {}
        for c in list(v.get("channels") or []) + list(v.get("opt_channels") or []):
            val = str(c.get("value", "-"))
            if val not in ("-", "", "None") and val.lstrip("-").isdigit() and int(val) >= 0:
                chans[c["id"]] = int(val)
        opts = {o["id"]: o.get("value") for o in (v.get("options") or [])}
        layers.append({"id": v["main"]["id"], "name": v["main"].get("name", key),
                       "channels": chans, "options": opts})
    # the decoder with logic channels is the bottom of the stack
    layers.sort(key=lambda L: 0 if L["channels"] else 1)
    return {"stack": layers}
