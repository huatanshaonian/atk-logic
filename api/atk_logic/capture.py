"""Capture data model and exports (CSV / VCD / JSON / edge text).

Samples are kept bit-packed (LSB first, 8 samples per byte, exactly like the
device and the GUI's Segment class). Transitions are computed lazily and are
the basis of summaries, exports and the protocol decoders, so captures with
10^8..10^9 samples stay manageable.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

_CHUNK_BYTES = 8 * 1024 * 1024  # 64 Mi samples per chunk


def shift_packed(packed: np.ndarray, offset_bits: int) -> np.ndarray:
    """Drop the first `offset_bits` samples of a packed LSB-first bit array."""
    packed = np.asarray(packed, dtype=np.uint8)
    if offset_bits <= 0:
        return packed
    b, r = divmod(int(offset_bits), 8)
    a = packed[b:]
    if r == 0 or len(a) == 0:
        return a.copy() if b else a
    nxt = np.empty_like(a)
    nxt[:-1] = a[1:]
    nxt[-1] = 0
    return ((a >> r) | (nxt << (8 - r))).astype(np.uint8)


class Channel:
    """One logic channel: packed bits + lazily computed transitions."""

    def __init__(self, index: int, packed, n_samples: int, name: Optional[str] = None):
        self.index = int(index)
        self.name = name or "Channel %d" % index
        self.packed = np.asarray(packed, dtype=np.uint8)
        need = (n_samples + 7) // 8
        if len(self.packed) < need:  # pad (should not normally happen)
            self.packed = np.concatenate([self.packed, np.zeros(need - len(self.packed), np.uint8)])
        self.n = int(n_samples)
        self._edges: Optional[np.ndarray] = None

    # -- basic access ---------------------------------------------------
    @property
    def initial(self) -> int:
        return int(self.packed[0] & 1) if self.n else 0

    def bits(self, start: int = 0, end: Optional[int] = None) -> np.ndarray:
        """Unpacked 0/1 uint8 array for samples [start, end)."""
        end = self.n if end is None else min(end, self.n)
        start = max(0, start)
        if end <= start:
            return np.zeros(0, np.uint8)
        b0, b1 = start // 8, (end + 7) // 8
        u = np.unpackbits(self.packed[b0:b1], bitorder="little")
        return u[start - b0 * 8:end - b0 * 8]

    @property
    def edges(self) -> np.ndarray:
        """Sample indices k (k>=1) where value[k] != value[k-1]."""
        if self._edges is None:
            self._edges = self._compute_edges()
        return self._edges

    def _compute_edges(self) -> np.ndarray:
        out = []
        nbytes = (self.n + 7) // 8
        prev = None
        for b0 in range(0, nbytes, _CHUNK_BYTES):
            chunk = self.packed[b0:min(nbytes, b0 + _CHUNK_BYTES)]
            # fast path: constant chunk equal to the previous level
            if prev is not None and ((prev == 0 and not chunk.any()) or
                                     (prev == 1 and (chunk == 0xFF).all())):
                continue
            u = np.unpackbits(chunk, bitorder="little")
            base = b0 * 8
            if prev is not None and u[0] != prev:
                out.append(np.array([base], np.int64))
            d = np.flatnonzero(u[1:] != u[:-1]).astype(np.int64) + (base + 1)
            if len(d):
                out.append(d)
            prev = int(u[-1])
        e = np.concatenate(out) if out else np.zeros(0, np.int64)
        return e[e < self.n]

    def value_at(self, k: int) -> int:
        cnt = int(np.searchsorted(self.edges, k, side="right"))
        return self.initial ^ (cnt & 1)

    # -- statistics ----------------------------------------------------
    def stats(self, samplerate: float) -> dict:
        e = self.edges
        n = self.n
        init = self.initial
        d = {"channel": self.index, "name": self.name, "samples": n,
             "initial": init, "final": self.value_at(n - 1) if n else 0,
             "transitions": int(len(e))}
        if n == 0:
            return d
        # high time
        bounds = np.concatenate([[0], e, [n]])
        lens = np.diff(bounds)
        high = int(lens[(1 - init)::2].sum())  # segments alternate, starting at `init`
        d["duty_percent"] = round(100.0 * high / n, 3)
        if len(e):
            d["first_edge_s"] = float(e[0]) / samplerate
            d["last_edge_s"] = float(e[-1]) / samplerate
            # values after each edge alternate starting with (1 - init)
            r_idx = e[0::2] if init == 0 else e[1::2]
            if len(r_idx) >= 2:
                periods = np.diff(r_idx).astype(np.float64)
                d["frequency_hz"] = float(samplerate / np.median(periods))
                d["period_min_s"] = float(periods.min() / samplerate)
                d["period_max_s"] = float(periods.max() / samplerate)
            pulses = lens[1:-1] if len(lens) > 2 else lens
            d["min_pulse_s"] = float(pulses.min() / samplerate) if len(pulses) else None
            # a rough baud-rate guess for async serial lines (shortest stable pulse)
            if len(e) >= 8:
                p = np.sort(np.diff(e))
                k = p[max(0, len(p) // 50)]  # ~2nd percentile, robust to glitches
                if k > 0:
                    d["shortest_pulse_hz"] = float(samplerate / k)
        return d


class Capture:
    """A multi-channel logic capture.

    Attributes:
        samplerate: samples per second
        channels:   dict channel index -> Channel
        n_samples:  number of samples per channel
        trigger_sample: sample index of the trigger point (or None)
        meta:       free-form dict (source, mode, threshold, warnings, ...)
    """

    def __init__(self, samplerate: float, channels: Dict[int, Channel], n_samples: int,
                 trigger_sample: Optional[int] = None, meta: Optional[dict] = None,
                 start_time: Optional[_dt.datetime] = None):
        self.samplerate = float(samplerate)
        self.channels = dict(sorted(channels.items()))
        self.n_samples = int(n_samples)
        self.trigger_sample = trigger_sample
        self.meta = meta or {}
        self.start_time = start_time or _dt.datetime.now()
        for ch in self.channels.values():
            ch.n = min(ch.n, self.n_samples)

    # -- constructors ----------------------------------------------------
    @classmethod
    def from_bits(cls, samplerate: float, data: Dict[int, Sequence[int]], **kw) -> "Capture":
        """Build from unpacked 0/1 sequences (handy for tests/simulation)."""
        chans = {}
        n = None
        for idx, bits in data.items():
            a = np.asarray(bits, dtype=np.uint8) & 1
            n = len(a) if n is None else min(n, len(a))
            chans[idx] = Channel(idx, np.packbits(a, bitorder="little"), len(a))
        return cls(samplerate, chans, n or 0, **kw)

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.samplerate if self.samplerate else 0.0

    def channel(self, idx: int) -> Channel:
        if idx not in self.channels:
            raise KeyError("channel %d not in capture (have %s)" % (idx, list(self.channels)))
        return self.channels[idx]

    def slice(self, start: int = 0, end: Optional[int] = None) -> "Capture":
        """New capture restricted to samples [start, end)."""
        end = self.n_samples if end is None else min(end, self.n_samples)
        start = max(0, start)
        chans = {}
        for i, ch in self.channels.items():
            p = shift_packed(ch.packed[: (end + 7) // 8], start)
            chans[i] = Channel(i, p, end - start, ch.name)
        trig = None
        if self.trigger_sample is not None and start <= self.trigger_sample < end:
            trig = self.trigger_sample - start
        meta = dict(self.meta)
        meta["window_start_s"] = meta.get("window_start_s", 0.0) + start / self.samplerate
        return Capture(self.samplerate, chans, end - start, trig, meta, self.start_time)

    # -- summaries ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "samplerate_hz": self.samplerate,
            "samples": self.n_samples,
            "duration_s": self.duration_s,
            "trigger_sample": self.trigger_sample,
            "trigger_time_s": (self.trigger_sample / self.samplerate
                               if self.trigger_sample is not None and self.samplerate else None),
            "channels": [ch.stats(self.samplerate) for ch in self.channels.values()],
            "meta": self.meta,
        }

    def edges_text(self, channels: Optional[Iterable[int]] = None, max_edges: int = 200,
                   start: int = 0, end: Optional[int] = None) -> str:
        """Compact, LLM-friendly view: per channel the run lengths of each level.

        Example line:  ch0 (UART TX) init=1: 1:8.68us 0:8.68us 1:17.4us ...
        """
        end = self.n_samples if end is None else end
        lines = ["# samplerate=%s samples=%d duration=%s window=[%s, %s)" % (
            _fmt_hz(self.samplerate), self.n_samples, _fmt_s(self.duration_s),
            _fmt_s(start / self.samplerate), _fmt_s(end / self.samplerate))]
        if self.trigger_sample is not None:
            lines.append("# trigger at %s" % _fmt_s(self.trigger_sample / self.samplerate))
        ids = list(channels) if channels is not None else list(self.channels)
        for i in ids:
            ch = self.channels[i]
            e = ch.edges
            lo = int(np.searchsorted(e, start, side="right"))
            hi = int(np.searchsorted(e, end, side="left"))
            sel = e[lo:hi]
            v = ch.value_at(start)
            pts = np.concatenate([[start], sel, [end]])
            parts = []
            for k in range(len(pts) - 1):
                if k >= max_edges:
                    parts.append("... (+%d more edges)" % (len(pts) - 1 - k))
                    break
                parts.append("%d:%s" % (v, _fmt_s((pts[k + 1] - pts[k]) / self.samplerate)))
                v ^= 1
            lines.append("ch%d (%s) start=%s edges=%d: %s" % (
                i, ch.name, _fmt_s(start / self.samplerate), len(sel), " ".join(parts)))
        return "\n".join(lines)

    # -- exports -----------------------------------------------------------
    def change_points(self) -> np.ndarray:
        """Sorted union of all channels' edge positions."""
        es = [ch.edges for ch in self.channels.values()]
        if not es:
            return np.zeros(0, np.int64)
        return np.unique(np.concatenate(es))

    def to_vcd(self, path: str, timescale_ps: Optional[int] = None) -> None:
        """Value Change Dump (PulseView/sigrok-cli/GTKWave)."""
        if timescale_ps is None:
            ps = 1e12 / self.samplerate
            timescale_ps = int(round(ps)) if abs(ps - round(ps)) < 1e-6 else 1
        tick = 1e12 / self.samplerate / timescale_ps
        ids = list(self.channels)
        sym = {i: _vcd_id(k) for k, i in enumerate(ids)}
        with open(path, "w", newline="\n") as f:
            f.write("$date %s $end\n" % self.start_time.strftime("%Y-%m-%d %H:%M:%S"))
            f.write("$version atk_logic python api $end\n")
            f.write("$comment samplerate %d Hz $end\n" % int(self.samplerate))
            f.write("$timescale %d ps $end\n" % timescale_ps)
            f.write("$scope module atk_logic $end\n")
            for i in ids:
                nm = self.channels[i].name.replace(" ", "_")
                f.write("$var wire 1 %s %s $end\n" % (sym[i], nm))
            f.write("$upscope $end\n$enddefinitions $end\n")
            f.write("#0\n$dumpvars\n")
            for i in ids:
                f.write("%d%s\n" % (self.channels[i].initial, sym[i]))
            f.write("$end\n")
            # merge edges of all channels
            ev_pos, ev_ch = [], []
            for k, i in enumerate(ids):
                e = self.channels[i].edges
                ev_pos.append(e)
                ev_ch.append(np.full(len(e), k, np.int32))
            if ev_pos:
                pos = np.concatenate(ev_pos)
                chk = np.concatenate(ev_ch)
                order = np.argsort(pos, kind="stable")
                pos, chk = pos[order], chk[order]
                vals = {i: self.channels[i].initial for i in ids}
                last = None
                buf = []
                for p, k in zip(pos.tolist(), chk.tolist()):
                    i = ids[k]
                    vals[i] ^= 1
                    if p != last:
                        buf.append("#%d\n" % int(round(p * tick)))
                        last = p
                    buf.append("%d%s\n" % (vals[i], sym[i]))
                    if len(buf) > 65536:
                        f.write("".join(buf))
                        buf = []
                f.write("".join(buf))
            f.write("#%d\n" % int(round(self.n_samples * tick)))

    def to_csv(self, path: str) -> None:
        """Same layout as the GUI's CSV export (ThreadWork::SaveFileThreadPara):
        one row at time 0 and one row per sample where any channel changes."""
        ids = list(self.channels)
        mask = 0
        for i in ids:
            mask |= 1 << i
        ns_per_sample = 1e9 / self.samplerate
        t0 = self.start_time
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("; CSV, generated by atk_logic python api on %s\n" %
                    _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            f.write("; Channels (%d/16)\n" % len(ids))
            f.write("; ChannelsType: %d\n" % mask)
            f.write("; Sample rate: %s\n" % _fmt_hz(self.samplerate))
            f.write("; Sample count: %d\n" % self.n_samples)
            f.write("; Sampling time: %s\n" % t0.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
            f.write("SystemTime, Time(s)" + "".join(", " + self.channels[i].name for i in ids) + "\n")
            vals = {i: self.channels[i].initial for i in ids}
            f.write("'%s,0%s\n" % (t0.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                                  "".join(",%d" % vals[i] for i in ids)))
            pts = self.change_points()
            edge_sets = {i: self.channels[i].edges for i in ids}
            ptr = {i: 0 for i in ids}
            buf = []
            for p in pts.tolist():
                for i in ids:
                    e = edge_sets[i]
                    j = ptr[i]
                    if j < len(e) and e[j] == p:
                        vals[i] ^= 1
                        ptr[i] = j + 1
                t_ns = p * ns_per_sample
                ts = t0 + _dt.timedelta(microseconds=t_ns / 1000.0)
                buf.append("'%s,%s%s\n" % (ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
                                          _fmt_dec(t_ns / 1e9),
                                          "".join(",%d" % vals[i] for i in ids)))
                if len(buf) > 65536:
                    f.write("".join(buf))
                    buf = []
            f.write("".join(buf))

    def to_atkjson(self, path: Optional[str] = None) -> dict:
        """Lossless edge-list JSON: {samplerate, samples, channels:{i:{initial, edges}}}."""
        d = {
            "format": "atk-logic-edges/1",
            "samplerate_hz": self.samplerate,
            "samples": self.n_samples,
            "trigger_sample": self.trigger_sample,
            "start_time": self.start_time.isoformat(),
            "meta": self.meta,
            "channels": {str(i): {"name": ch.name, "initial": ch.initial,
                                  "edges": ch.edges.tolist()}
                         for i, ch in self.channels.items()},
        }
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        return d

    @classmethod
    def from_atkjson(cls, src) -> "Capture":
        if isinstance(src, str):
            with open(src, "r", encoding="utf-8") as f:
                d = json.load(f)
        else:
            d = src
        n = int(d["samples"])
        chans = {}
        for k, c in d["channels"].items():
            tog = np.zeros(n, np.uint8)
            e = np.asarray(c["edges"], np.int64)
            tog[e[e < n]] = 1
            bits = (np.cumsum(tog, dtype=np.int64) & 1).astype(np.uint8) ^ (int(c["initial"]) & 1)
            ch = Channel(int(k), np.packbits(bits, bitorder="little"), n, c.get("name"))
            chans[int(k)] = ch
        st = d.get("start_time")
        return cls(float(d["samplerate_hz"]), chans, n, d.get("trigger_sample"),
                   d.get("meta") or {},
                   _dt.datetime.fromisoformat(st) if st else None)

    def to_json_summary(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=1, ensure_ascii=False)

    def save(self, path: str) -> str:
        """Export by file extension: .vcd .csv .atkjson .json (summary)."""
        low = path.lower()
        if low.endswith(".vcd"):
            self.to_vcd(path)
        elif low.endswith(".csv"):
            self.to_csv(path)
        elif low.endswith(".atkjson"):
            self.to_atkjson(path)
        elif low.endswith(".json"):
            self.to_json_summary(path)
        else:
            raise ValueError("unknown export format for %r (use .vcd/.csv/.atkjson/.json)" % path)
        return path


# ---------------------------------------------------------------- helpers
def _vcd_id(k: int) -> str:
    chars = [chr(c) for c in range(33, 127)]
    s = ""
    k += 0
    while True:
        s += chars[k % len(chars)]
        k //= len(chars)
        if k == 0:
            return s


def _fmt_hz(hz: float) -> str:
    for unit, div in (("GHz", 1e9), ("MHz", 1e6), ("KHz", 1e3)):
        if hz >= div:
            return ("%g " % (hz / div)) + unit
    return "%g Hz" % hz


def _fmt_s(s: float) -> str:
    a = abs(s)
    if a == 0:
        return "0s"
    for unit, mul in (("s", 1), ("ms", 1e3), ("us", 1e6), ("ns", 1e9)):
        if a * mul >= 1 or unit == "ns":
            return ("%.4g" % (s * mul)) + unit
    return "%gs" % s


def _fmt_dec(x: float) -> str:
    return ("%.9f" % x).rstrip("0").rstrip(".") if x else "0"
