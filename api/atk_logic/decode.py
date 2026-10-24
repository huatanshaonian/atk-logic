"""Run sigrok protocol decoders (runtime/decoders/*) on a Capture in pure Python.

A small re-implementation of the `sigrokdecode` C module (libsigrokdecode,
API version 3) is installed into sys.modules, so the unmodified decoder
packages shipped with ATK-Logic can be imported and executed without the
native library.  wait() follows libsigrokdecode semantics:

* conditions: dict {channel_index: 'h'|'l'|'r'|'f'|'e'|'n', 'skip': N} (AND),
  a list of dicts is OR-ed; self.matched is a tuple of booleans.
* the sample where the previous wait() matched is checked again first, but an
  edge can never match there (old pin state == current state);
* {'skip': N} matches N samples after the current one; wait() / wait({}) is
  skip 1 (skip 0 at sample 0).

Instead of iterating every sample, the engine jumps between the channels'
transition lists, so long idle periods cost nothing.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import types
import warnings
from typing import Any, Dict, List, Optional

import numpy as np

from .capture import Capture

# ------------------------------------------------------------ sigrokdecode shim
OUTPUT_ANN = 0
OUTPUT_PYTHON = 1
OUTPUT_BINARY = 2
OUTPUT_LOGIC = 3
OUTPUT_META = 4
SRD_CONF_SAMPLERATE = 10000


class _EndOfData(BaseException):
    """Raised from wait() when no more samples match; ends decode()."""


class _Truncated(BaseException):
    """Raised when the annotation limit is reached; ends the whole stack."""


class Decoder:
    """Base class exposed as sigrokdecode.Decoder."""

    api_version = 3

    def register(self, output_type, proto_id=None, meta=None):
        rt = self.__dict__["_rt"]
        oid = len(rt.outputs)
        rt.outputs.append((output_type, proto_id, meta))
        return oid

    def put(self, ss, es, output_id, data):
        self.__dict__["_rt"].put(int(ss), int(es), output_id, data)

    def wait(self, conds=None):
        return self.__dict__["_rt"].wait(conds)

    def has_channel(self, index):
        rt = self.__dict__["_rt"]
        return 0 <= index < len(rt.chmap) and rt.chmap[index] is not None


def _make_shim_module() -> types.ModuleType:
    m = types.ModuleType("sigrokdecode")
    m.Decoder = Decoder
    m.OUTPUT_ANN = OUTPUT_ANN
    m.OUTPUT_PYTHON = OUTPUT_PYTHON
    m.OUTPUT_BINARY = OUTPUT_BINARY
    m.OUTPUT_LOGIC = OUTPUT_LOGIC
    m.OUTPUT_META = OUTPUT_META
    m.SRD_CONF_SAMPLERATE = SRD_CONF_SAMPLERATE
    m.__dict__["__atk_shim__"] = True
    return m


def install_shim() -> None:
    cur = sys.modules.get("sigrokdecode")
    if cur is None or not getattr(cur, "__atk_shim__", False):
        sys.modules["sigrokdecode"] = _make_shim_module()


# --------------------------------------------------------------- decoder lookup
def default_decoders_dir() -> Optional[str]:
    env = os.environ.get("ATK_DECODERS_DIR")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "..", "runtime", "decoders"),
                 os.path.join(here, "decoders"),
                 os.path.join(os.getcwd(), "runtime", "decoders"),
                 os.path.join(os.getcwd(), "decoders")):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return None


def _prepare(decoders_dir: Optional[str]) -> str:
    d = decoders_dir or default_decoders_dir()
    if not d or not os.path.isdir(d):
        raise FileNotFoundError(
            "sigrok decoders directory not found; pass decoders_dir= / --decoders-dir "
            "or set ATK_DECODERS_DIR (it is runtime/decoders in the ATK-Logic repo)")
    install_shim()
    if d not in sys.path:
        sys.path.insert(0, d)
    return d


_ID_CACHE: Dict[str, str] = {}


def _resolve_id(dec_id: str, d: str) -> str:
    """Map a decoder id (as used by sigrok/the GUI) to its package directory."""
    if os.path.isfile(os.path.join(d, dec_id, "pd.py")):
        return dec_id
    key = d + "|" + dec_id
    if key in _ID_CACHE:
        return _ID_CACHE[key]
    names = [n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "pd.py"))]
    low = dec_id.lower()
    for n in names:
        if n.lower() == low or n.lower() == low.replace("-", "_"):
            _ID_CACHE[key] = n
            return n
    pat = re.compile(r"^\s*id\s*=\s*['\"]%s['\"]" % re.escape(dec_id), re.M)
    for n in names:
        try:
            with open(os.path.join(d, n, "pd.py"), encoding="utf-8", errors="replace") as f:
                if pat.search(f.read()):
                    _ID_CACHE[key] = n
                    return n
        except OSError:
            continue
    raise ImportError("unknown decoder %r (run `atk-logic decoders` for the list)" % dec_id)


def load_decoder_class(dec_id: str, decoders_dir: Optional[str] = None):
    d = _prepare(decoders_dir)
    pre = sys.modules.get(dec_id)
    if pre is not None and hasattr(pre, "Decoder") and not hasattr(pre, "__path__"):
        return pre.Decoder  # a decoder registered programmatically
    dec_id = _resolve_id(dec_id, d)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        mod = importlib.import_module(dec_id)
    cls = getattr(mod, "Decoder", None)
    if cls is None:
        raise ImportError("%s has no Decoder class" % dec_id)
    return cls


def list_decoders(decoders_dir: Optional[str] = None) -> List[dict]:
    d = _prepare(decoders_dir)
    out = []
    for name in sorted(os.listdir(d)):
        if not os.path.isfile(os.path.join(d, name, "pd.py")) or name == "common":
            continue
        try:
            cls = load_decoder_class(name, d)
        except Exception as e:  # noqa: BLE001 - report and continue
            out.append({"id": name, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        out.append(describe(cls))
    return out


def describe(cls) -> dict:
    return {
        "id": cls.id,
        "name": getattr(cls, "name", cls.id),
        "longname": getattr(cls, "longname", ""),
        "desc": getattr(cls, "desc", ""),
        "inputs": list(getattr(cls, "inputs", [])),
        "outputs": list(getattr(cls, "outputs", [])),
        "channels": [c["id"] for c in getattr(cls, "channels", ())],
        "optional_channels": [c["id"] for c in getattr(cls, "optional_channels", ())],
        "options": [{"id": o["id"], "default": o.get("default"),
                     "values": list(o["values"]) if "values" in o else None}
                    for o in getattr(cls, "options", ())],
        "annotations": [a[0] for a in getattr(cls, "annotations", ())],
        "annotation_rows": {r[0]: [getattr(cls, "annotations", ())[i][0] for i in r[2]]
                            for r in getattr(cls, "annotation_rows", ())},
    }


# ----------------------------------------------------------------- the engine
class _Signal:
    """Fast level/edge queries for one capture channel."""

    __slots__ = ("edges", "init", "n", "_list")

    def __init__(self, ch):
        self.edges = ch.edges
        self.init = ch.initial
        self.n = ch.n

    def value(self, s: int) -> int:
        return self.init ^ (int(np.searchsorted(self.edges, s, side="right")) & 1)

    def is_edge(self, s: int) -> bool:
        e = self.edges
        i = int(np.searchsorted(e, s))
        return i < len(e) and int(e[i]) == s

    def next_edge(self, s: int) -> int:
        """First edge position >= s, or -1."""
        e = self.edges
        i = int(np.searchsorted(e, s))
        return int(e[i]) if i < len(e) else -1


class _Runtime:
    def __init__(self, session, inst, cls, chmap, options, level):
        self.session = session
        self.inst = inst
        self.cls = cls
        self.chmap = chmap  # list: decoder channel index -> _Signal or None
        self.options = options
        self.level = level
        self.outputs: List[tuple] = []
        self.cur = 0
        self.above: List["_Runtime"] = []
        self.ann_names = [a[0] for a in getattr(cls, "annotations", ())]
        self.row_of: Dict[int, str] = {}
        for r in getattr(cls, "annotation_rows", ()):
            for c in r[2]:
                self.row_of[c] = r[0]

    # -- output --------------------------------------------------------
    def put(self, ss, es, oid, data):
        if oid < 0 or oid >= len(self.outputs):
            return
        otype = self.outputs[oid][0]
        if otype == OUTPUT_ANN:
            cls_idx, texts = data[0], list(data[1])
            self.session._ann(self, ss, es, cls_idx, texts)
        elif otype == OUTPUT_PYTHON:
            for up in self.above:
                try:
                    up.inst.decode(ss, es, data)
                except _EndOfData:
                    pass
        elif otype == OUTPUT_BINARY:
            self.session._binary(self, ss, es, data)
        # OUTPUT_META / OUTPUT_LOGIC are ignored

    # -- wait ----------------------------------------------------------
    def pins(self, s):
        return tuple(0xFF if sig is None else sig.value(s) for sig in self.chmap)

    def wait(self, conds):
        n = self.session.end
        s0 = self.cur
        if conds is None:
            conds = []
        if isinstance(conds, dict):
            conds = [conds]
        conds = [c for c in conds if c is not None]
        if not conds or all(len(c) == 0 for c in conds):
            conds = [{"skip": 1 if s0 > 0 else 0}]
        best = -1
        for c in conds:
            s = self._earliest(c, s0, n)
            if s >= 0 and (best < 0 or s < best):
                best = s
        if best < 0:
            self.cur = n
            raise _EndOfData()
        self.cur = best
        self.inst.samplenum = best
        self.inst.matched = tuple(self._matches_at(c, s0, best) for c in conds)
        self.session._progress(best)
        return self.pins(best)

    def _terms(self, cond):
        skip = 0
        terms = []
        for k, v in cond.items():
            if k == "skip":
                skip = max(skip, int(v))
                if int(v) < 0:
                    return None, 0
                continue
            idx = int(k)
            sig = self.chmap[idx] if 0 <= idx < len(self.chmap) else None
            if sig is None:
                return None, 0  # term on an unused channel never matches
            terms.append((sig, v))
        return terms, skip

    def _ok(self, sig, t, s, s0):
        if t == "h":
            return sig.value(s) == 1
        if t == "l":
            return sig.value(s) == 0
        edge = s != s0 and sig.is_edge(s)
        if t == "e":
            return edge
        if t == "r":
            return edge and sig.value(s) == 1
        if t == "f":
            return edge and sig.value(s) == 0
        if t == "n":
            return not edge
        raise ValueError("unknown wait() term %r" % (t,))

    def _matches_at(self, cond, s0, s):
        terms, skip = self._terms(cond)
        if terms is None or s < s0 + skip:
            return False
        return all(self._ok(sig, t, s, s0) for sig, t in terms)

    def _earliest(self, cond, s0, n):
        terms, skip = self._terms(cond)
        if terms is None:
            return -1
        lo = s0 + skip
        if lo >= n:
            return -1
        edge_terms = [(sig, t) for sig, t in terms if t in ("r", "f", "e")]
        if edge_terms:
            # iterate the edges of the sparsest edge-term channel
            sig0, _ = min(edge_terms, key=lambda st: len(st[0].edges))
            e = sig0.edges
            i = int(np.searchsorted(e, max(lo, s0 + 1)))
            ne = len(e)
            while i < ne:
                s = int(e[i])
                if s >= n:
                    return -1
                if all(self._ok(sig, t, s, s0) for sig, t in terms):
                    return s
                i += 1
            return -1
        # only level / no-edge terms: jump to the next transition of a failing channel
        s = lo
        while s < n:
            moved = False
            for sig, t in terms:
                if t in ("h", "l"):
                    want = 1 if t == "h" else 0
                    if sig.value(s) != want:
                        nx = sig.next_edge(s + 1)
                        if nx < 0:
                            return -1
                        s = nx
                        moved = True
                        break
                elif t == "n":
                    if s != s0 and sig.is_edge(s):
                        s += 1
                        moved = True
                        break
                else:
                    raise ValueError("unknown wait() term %r" % (t,))
            if not moved:
                return s
        return -1


def _convert_option(spec: dict, value: Any) -> Any:
    default = spec.get("default")
    if value is None:
        return default
    try:
        if isinstance(default, bool):
            return str(value).lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(float(value))
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        raise ValueError("option %s: cannot convert %r to %s"
                         % (spec["id"], value, type(default).__name__))
    return str(value)


class DecodeSession:
    """Run a decoder stack on a capture.

    stack: list of {"id": str, "channels": {name: capture_channel},
                    "options": {id: value}} — lowest (logic input) decoder first;
           each higher layer consumes the OUTPUT_PYTHON of the one below.
    """

    def __init__(self, capture: Capture, stack: List[dict], decoders_dir: Optional[str] = None,
                 start: int = 0, end: Optional[int] = None, max_annotations: int = 100000,
                 stop_when_full: bool = True):
        if not stack:
            raise ValueError("empty decoder stack")
        self.capture = capture
        self.start = max(0, int(start))
        self.end = capture.n_samples if end is None else min(int(end), capture.n_samples)
        self.max_ann = max_annotations
        self.stop_when_full = stop_when_full
        self.annotations: List[dict] = []
        self.binary: Dict[str, int] = {}
        self.truncated = False
        self.errors: List[str] = []
        self.progress = 0
        self.runtimes: List[_Runtime] = []
        d = _prepare(decoders_dir)
        signals = {i: _Signal(ch) for i, ch in capture.channels.items()}
        for level, layer in enumerate(stack):
            cls = load_decoder_class(layer["id"], d)
            if level > 0:
                below = self.runtimes[-1].cls
                if not set(getattr(cls, "inputs", [])) & set(getattr(below, "outputs", [])):
                    raise ValueError("decoder %s (inputs %s) cannot stack on %s (outputs %s)" % (
                        cls.id, list(cls.inputs), below.id, list(below.outputs)))
            chdefs = list(getattr(cls, "channels", ())) + list(getattr(cls, "optional_channels", ()))
            given = dict(layer.get("channels") or {})
            chmap: List[Optional[_Signal]] = []
            for k, cd in enumerate(chdefs):
                src = given.pop(cd["id"], given.pop(cd.get("name", "\0"), None))
                if src is None or src == "" or (isinstance(src, int) and src < 0):
                    if k < len(getattr(cls, "channels", ())):
                        raise ValueError("decoder %s: required channel '%s' is not mapped "
                                         "(use --map %s=<capture channel>)" % (cls.id, cd["id"], cd["id"]))
                    chmap.append(None)
                    continue
                src = int(src)
                if src not in signals:
                    raise ValueError("decoder %s: channel '%s' -> capture channel %d which is not in "
                                     "the capture (have %s)" % (cls.id, cd["id"], src, sorted(signals)))
                chmap.append(signals[src])
            if given:
                raise ValueError("decoder %s: unknown channel(s) %s; valid: %s" % (
                    cls.id, sorted(given), [c["id"] for c in chdefs]))
            opts_in = dict(layer.get("options") or {})
            options = {}
            for spec in getattr(cls, "options", ()):
                v = opts_in.pop(spec["id"], None)
                options[spec["id"]] = _convert_option(spec, v)
                vals = spec.get("values")
                if vals and options[spec["id"]] not in vals:
                    raise ValueError("decoder %s: option %s=%r not in %s" % (
                        cls.id, spec["id"], options[spec["id"]], list(vals)))
            if opts_in:
                raise ValueError("decoder %s: unknown option(s) %s; valid: %s" % (
                    cls.id, sorted(opts_in), [o["id"] for o in getattr(cls, "options", ())]))
            inst = cls.__new__(cls)
            rt = _Runtime(self, inst, cls, chmap, options, level)
            inst.__dict__["_rt"] = rt
            inst.options = options
            inst.samplenum = self.start
            inst.matched = None
            rt.cur = self.start
            cls.__init__(inst)
            inst.options = options
            if level > 0:
                self.runtimes[-1].above.append(rt)
            self.runtimes.append(rt)

    # callbacks from runtimes
    def _ann(self, rt, ss, es, cls_idx, texts):
        if len(self.annotations) >= self.max_ann:
            self.truncated = True
            if self.stop_when_full:
                raise _Truncated()
            return
        sr = self.capture.samplerate
        self.annotations.append({
            "decoder": rt.cls.id,
            "ss": ss, "es": es,
            "t0": ss / sr, "t1": es / sr,
            "ann": rt.ann_names[cls_idx] if 0 <= cls_idx < len(rt.ann_names) else str(cls_idx),
            "row": rt.row_of.get(cls_idx),
            "text": texts[0] if texts else "",
            "texts": texts,
        })

    def _binary(self, rt, ss, es, data):
        try:
            n = len(data[1])
        except TypeError:  # some decoders put None; libsigrokdecode ignores it too
            return
        key = "%s:%s" % (rt.cls.id, data[0])
        self.binary[key] = self.binary.get(key, 0) + n

    def _progress(self, s):
        self.progress = s

    def run(self) -> dict:
        sr = self.capture.samplerate
        for rt in self.runtimes:
            meta = getattr(rt.inst, "metadata", None)
            if meta:
                meta(SRD_CONF_SAMPLERATE, int(sr))
        for rt in self.runtimes:
            st = getattr(rt.inst, "start", None)
            if st:
                st()
        bottom = self.runtimes[0]
        try:
            bottom.inst.decode()
        except (_EndOfData, _Truncated):
            pass
        except Exception as e:  # decoder bug / unexpected data: keep partial results
            import traceback
            self.errors.append("%s: %s" % (type(e).__name__, e))
            self.errors.append(traceback.format_exc(limit=3))
        return self.result()

    def result(self) -> dict:
        rows: Dict[str, List[str]] = {}
        for a in self.annotations:
            key = "%s.%s" % (a["decoder"], a["row"] or a["ann"])
            rows.setdefault(key, []).append(a["text"])
        return {
            "decoders": [rt.cls.id for rt in self.runtimes],
            "samplerate_hz": self.capture.samplerate,
            "range": [self.start, self.end],
            "decoded_until_s": self.progress / self.capture.samplerate,
            "annotation_count": len(self.annotations),
            "truncated": self.truncated,
            "errors": self.errors,
            "rows": {k: {"count": len(v), "texts": v} for k, v in rows.items()},
            "annotations": self.annotations,
            "binary_bytes": self.binary,
        }


def decode(capture: Capture, stack, decoders_dir: Optional[str] = None, **kw) -> dict:
    """Convenience wrapper: decode(capture, [{"id": "uart", "channels": {"rx": 0},
    "options": {"baudrate": 115200}}]) -> result dict."""
    if isinstance(stack, dict):
        stack = stack.get("stack", [stack])
    return DecodeSession(capture, stack, decoders_dir, **kw).run()
