"""Command line interface: `atk-logic <command> ...` / `python -m atk_logic ...`.

Every command prints one JSON document on stdout (use --pretty for indentation).
Errors are printed as {"error": ..., "type": ..., "hint": ...} with exit code != 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional

from . import __version__
from . import protocol as P

EXIT_USAGE, EXIT_NOTFOUND, EXIT_ACCESS, EXIT_STATE, EXIT_CAPTURE, EXIT_OTHER = 2, 3, 4, 5, 6, 1

_SI = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "g": 1e9, "G": 1e9}


def parse_freq(s: str) -> float:
    """'10M', '2.5MHz', '1k', '250000' -> Hz (m/M both mean mega here)."""
    m = re.fullmatch(r"\s*([0-9.]+(?:e[-+]?\d+)?)\s*([kKmMgG]?)(?:hz|Hz|HZ)?\s*", str(s))
    if not m:
        raise ValueError("bad frequency %r (examples: 10M, 2.5MHz, 1k)" % s)
    return float(m.group(1)) * _SI[m.group(2)]


def parse_time(s: str) -> float:
    """'10ms', '2s', '100us', '500ns', '1.5' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*([0-9.]+(?:e[-+]?\d+)?)\s*(s|ms|us|µs|ns|min|m)?\s*", str(s))
    if not m:
        raise ValueError("bad time %r (examples: 10ms, 2s, 100us)" % s)
    mul = {None: 1, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "min": 60, "m": 60}[m.group(2)]
    return float(m.group(1)) * mul


def parse_count(s: str) -> int:
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?)\s*(?:sa|Sa|samples)?\s*", str(s))
    if not m:
        raise ValueError("bad sample count %r (examples: 100000, 10M)" % s)
    return int(float(m.group(1)) * _SI[m.group(2)])


def parse_channels(s: str) -> List[int]:
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def parse_kv(items: Optional[List[str]], what: str) -> Dict[str, str]:
    d = {}
    for it in items or []:
        for part in it.split(","):
            if not part.strip():
                continue
            if "=" not in part and ":" not in part:
                raise ValueError("%s must look like key=value, got %r" % (what, part))
            k, v = re.split(r"[=:]", part, maxsplit=1)
            d[k.strip()] = v.strip()
    return d


def parse_triggers(items: Optional[List[str]]) -> Dict[int, str]:
    return {int(k): v for k, v in parse_kv(items, "--trigger").items()}


def _emit(obj, args) -> None:
    txt = json.dumps(obj, indent=2 if getattr(args, "pretty", False) else None,
                     ensure_ascii=False, default=_json_default)
    sys.stdout.write(txt + "\n")


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(o)


# ------------------------------------------------------------------ helpers
def _load_file(path: str):
    from . import load
    return load(path)


def _window(cap, args):
    start = int(parse_time(args.start) * cap.samplerate) if getattr(args, "start", None) else 0
    end = int(parse_time(args.end) * cap.samplerate) if getattr(args, "end", None) else None
    if start or end is not None:
        cap = cap.slice(start, end)
    chs = getattr(args, "channels", None)
    if chs:
        keep = parse_channels(chs)
        cap.channels = {k: v for k, v in cap.channels.items() if k in keep}
    return cap


def _exports(cap, outs: Optional[List[str]]) -> List[str]:
    done = []
    for o in outs or []:
        cap.save(o)
        done.append(os.path.abspath(o))
    return done


def _stack_from_args(args, cap) -> List[dict]:
    """Build a decoder stack from --decoder/--map/--opt, or take it from the .atkdl file."""
    from .decode import load_decoder_class
    decs = []
    for d in args.decoder or []:
        decs.extend(x.strip() for x in d.split(",") if x.strip())
    if not decs:
        saved = (cap.meta or {}).get("decoders") or []
        saved = [s for s in saved if "stack" in s]
        if not saved:
            raise ValueError("no --decoder given and the input has no saved decoder configuration")
        idx = getattr(args, "saved", 0) or 0
        if idx >= len(saved):
            raise ValueError("--saved %d out of range (%d saved decoder stacks)" % (idx, len(saved)))
        return saved[idx]["stack"]
    mapping = parse_kv(args.map, "--map")
    opts = parse_kv(args.opt, "--opt")
    stack = [{"id": d, "channels": {}, "options": {}} for d in decs]
    classes = [load_decoder_class(d, args.decoders_dir) for d in decs]
    for k, v in mapping.items():
        stack[0]["channels"][k] = int(v)
    for k, v in opts.items():
        if "." in k:
            dec, opt = k.split(".", 1)
            hits = [i for i, c in enumerate(classes) if c.id == dec]
            if not hits:
                raise ValueError("--opt %s: decoder %s is not in the stack" % (k, dec))
            stack[hits[0]]["options"][opt] = v
            continue
        hits = [i for i, c in enumerate(classes) if any(o["id"] == k for o in getattr(c, "options", ()))]
        if not hits:
            raise ValueError("--opt %s: no decoder in the stack has this option" % k)
        if len(hits) > 1:
            raise ValueError("--opt %s is ambiguous; write it as <decoder>.%s" % (k, k))
        stack[hits[0]]["options"][k] = v
    return stack


def _decode_output(res: dict, args) -> dict:
    max_texts = args.max_texts
    out = {k: v for k, v in res.items() if k != "annotations"}
    rows = {}
    for k, v in res["rows"].items():
        if args.rows and not any(r in k for r in args.rows.split(",")):
            continue
        texts = v["texts"]
        rows[k] = {"count": v["count"], "texts": texts[:max_texts]}
        if len(texts) > max_texts:
            rows[k]["truncated"] = True
    if args.ann:
        rows = {}
        keys = [k.strip() for k in args.ann.split(",") if k.strip()]
        for a in res["annotations"]:
            if any(k == a["ann"] or (k.endswith("*") and a["ann"].startswith(k[:-1])) for k in keys):
                r = rows.setdefault("%s.%s" % (a["decoder"], a["ann"]), {"count": 0, "texts": []})
                r["count"] += 1
                if len(r["texts"]) < max_texts:
                    r["texts"].append(a["text"])
                else:
                    r["truncated"] = True
    out["rows"] = rows
    if args.annotations:
        anns = res["annotations"]
        if args.rows:
            keys = args.rows.split(",")
            anns = [a for a in anns if any(r in "%s.%s" % (a["decoder"], a["row"] or a["ann"]) for r in keys)]
        out["annotations"] = [
            {"t0": round(a["t0"], 9), "t1": round(a["t1"], 9), "decoder": a["decoder"],
             "row": a["row"], "ann": a["ann"], "text": a["text"]} for a in anns[:args.max_annotations]]
        if len(anns) > args.max_annotations:
            out["annotations_truncated"] = True
    return out


# ------------------------------------------------------------------ commands
def cmd_list(args):
    from .device import list_devices
    devs = list_devices()
    _emit({"devices": devs, "count": len(devs)}, args)


def _open(args):
    from .device import AtkLogic
    log = (lambda m: sys.stderr.write(m + "\n")) if args.verbose else None
    return AtkLogic.open(index=args.index, log=log)


def cmd_info(args):
    with _open(args) as la:
        _emit({"device": la.info.as_dict(), "api_version": __version__}, args)


def _capture_kwargs(args):
    if not args.channels or not args.rate:
        raise ValueError("capture needs -c/--channels and -r/--rate")
    if not args.duration and not args.samples:
        raise ValueError("give --duration (e.g. 10ms) or --samples (e.g. 1M)")
    # cheap checks before the device is opened
    P.rate_index(parse_freq(args.rate))
    for v in parse_triggers(args.trigger).values():
        P.normalize_trigger(v)
    return dict(
        channels=parse_channels(args.channels),
        samplerate=parse_freq(args.rate),
        samples=parse_count(args.samples) if args.samples else None,
        duration=parse_time(args.duration) if args.duration else None,
        threshold=args.threshold,
        mode=args.mode,
        rle=args.rle,
        trigger=parse_triggers(args.trigger),
        trigger_position=args.trigger_pos,
        timeout=parse_time(args.timeout) if args.timeout else None,
    )


def _do_capture(args, la):
    kw = _capture_kwargs(args)
    pwm_res = []
    for spec in args.pwm or []:
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("--pwm must look like INDEX:FREQ[:DUTY], e.g. 0:1M:50")
        pwm_res.append(la.pwm(int(parts[0]), parse_freq(parts[1]),
                              float(parts[2]) if len(parts) == 3 else 50))
    prog = None
    if args.verbose:
        def prog(stage, pct):
            sys.stderr.write("\r%-12s %3d%%" % (stage, pct))
            sys.stderr.flush()
    cap = la.capture(progress=prog, **kw)
    if args.verbose:
        sys.stderr.write("\n")
    return cap, pwm_res


def cmd_capture(args):
    _capture_kwargs(args)
    with _open(args) as la:
        cap, pwm_res = _do_capture(args, la)
    out = {"capture": cap.summary(), "saved": _exports(cap, args.out)}
    if pwm_res:
        out["pwm"] = pwm_res
    if args.edges:
        out["edges_text"] = cap.edges_text(max_edges=args.max_edges)
    if args.decoder:
        from .decode import decode
        res = decode(cap, _stack_from_args(args, cap), args.decoders_dir, max_annotations=args.limit)
        out["decode"] = _decode_output(res, args)
    _emit(out, args)


def cmd_open(args):
    cap = _window(_load_file(args.file), args)
    out = {"capture": cap.summary(), "saved": _exports(cap, args.out)}
    if args.edges:
        out["edges_text"] = cap.edges_text(max_edges=args.max_edges)
    _emit(out, args)


def cmd_edges(args):
    cap = _load_file(args.file)
    start = int(parse_time(args.start) * cap.samplerate) if args.start else 0
    end = int(parse_time(args.end) * cap.samplerate) if args.end else None
    chans = parse_channels(args.channels) if args.channels else None
    sys.stdout.write(cap.edges_text(channels=chans, max_edges=args.max_edges, start=start, end=end) + "\n")


def cmd_decode(args):
    from .decode import decode
    if args.file:
        cap = _window(_load_file(args.file), args)
    else:
        if not args.capture:
            raise ValueError("give an input FILE or --capture with capture options")
        _capture_kwargs(args)
        with _open(args) as la:
            cap, _ = _do_capture(args, la)
        if args.out:
            _exports(cap, args.out)
    res = decode(cap, _stack_from_args(args, cap), args.decoders_dir, max_annotations=args.limit)
    _emit(_decode_output(res, args), args)


def cmd_decoders(args):
    from .decode import describe, list_decoders, load_decoder_class
    if args.id:
        _emit(describe(load_decoder_class(args.id, args.decoders_dir)), args)
    else:
        lst = list_decoders(args.decoders_dir)
        if not args.full:
            lst = [{"id": d["id"], "name": d.get("name"), "inputs": d.get("inputs"),
                    "outputs": d.get("outputs"), "error": d.get("error")} if "error" in d else
                   {"id": d["id"], "name": d["name"], "inputs": d["inputs"], "outputs": d["outputs"]}
                   for d in lst]
        _emit({"decoders": lst, "count": len(lst)}, args)


def cmd_limits(args):
    plus = args.plus
    n = len(parse_channels(args.channels))
    _emit({
        "channels": n, "plus": plus,
        "buffer_rates_hz": P.allowed_rates("buffer", n, plus),
        "stream_rates_hz": P.allowed_rates("stream", n, plus),
        "buffer_max_samples_per_channel": (P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD) // max(1, n),
        "buffer_max_samples_per_channel_rle": (P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD)
        * P.RLE_DEPTH_FACTOR // max(1, n),
        "threshold_range_v": [P.THRESHOLD_MIN_V, P.THRESHOLD_MAX_V],
        "threshold_presets_v": {"5.0V CMOS": 2.5, "3.3V CMOS": 1.6, "3.0V CMOS": 1.5, "2.5V CMOS": 1.2,
                                "1.8V CMOS": 0.9, "1.5V CMOS": 0.7, "1.2V CMOS": 0.6},
        "pwm_range_hz": [P.PWM_MIN_HZ, P.PWM_MAX_HZ],
        "trigger_types": list(P.TRIGGER_TYPES),
    }, args)


# ------------------------------------------------------------------ parser
def _add_capture_opts(p, required=True):
    p.add_argument("-c", "--channels", required=required, help="channels, e.g. 0,1,2 or 0-3")
    p.add_argument("-r", "--rate", required=required, help="sample rate, e.g. 10M, 100M, 1G")
    p.add_argument("-d", "--duration", help="capture length as time, e.g. 10ms, 1s")
    p.add_argument("-n", "--samples", help="capture length in samples per channel, e.g. 1M")
    p.add_argument("--threshold", default=None,
                   help="switching threshold in V (-5..5) or preset 3.3V/1.8V/5V... (default 1.6 V = 3.3V CMOS)")
    p.add_argument("--mode", choices=["buffer", "stream"], default="buffer")
    p.add_argument("--rle", action="store_true", help="hardware run-length compression")
    p.add_argument("-t", "--trigger", action="append",
                   help="trigger, e.g. 0=rising or 0=falling,3=high (types: rising falling high low edge)")
    p.add_argument("--trigger-pos", type=float, default=1.0, help="pre-trigger buffer in %% (1..90)")
    p.add_argument("--timeout", help="give up after this time, e.g. 10s (default: wait for trigger forever)")
    p.add_argument("--pwm", action="append", help="also drive PWM during capture: INDEX:FREQ[:DUTY]")


def _add_decode_opts(p):
    p.add_argument("-D", "--decoder", action="append",
                   help="decoder id; repeat or comma-separate to stack (lowest first), e.g. i2c,eeprom24xx")
    p.add_argument("-m", "--map", action="append", help="channel mapping, e.g. rx=0 or scl=0,sda=1")
    p.add_argument("-O", "--opt", action="append",
                   help="decoder option, e.g. baudrate=115200 or uart.parity=even")
    p.add_argument("--saved", type=int, default=0,
                   help="when no --decoder: index of the decoder stack saved in the .atkdl")
    p.add_argument("--rows", help="only rows containing these names (comma separated), e.g. tx-data,rx-data")
    p.add_argument("--ann", help="only these annotation classes (exact, or prefix with *), e.g. tx-data or "
                                 "address,data-read (see `decoders ID`); rows are then keyed by class")
    p.add_argument("--max-texts", type=int, default=200, help="texts per row in the output")
    p.add_argument("--limit", type=int, default=100000,
                   help="stop decoding after this many annotations (default 100000)")
    p.add_argument("--annotations", action="store_true", help="include timed annotation list")
    p.add_argument("--max-annotations", type=int, default=2000)


def _add_window_opts(p):
    p.add_argument("--start", help="window start time, e.g. 1ms")
    p.add_argument("--end", help="window end time")
    p.add_argument("--channels", help="only these channels, e.g. 0,1")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="atk-logic", description="ATK-Logic (ALIENTEK DL16) logic analyzer CLI")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="indent JSON output")
    common.add_argument("-i", "--device", dest="index", type=int, default=0, help="device index (see `list`)")
    common.add_argument("-v", "--verbose", action="store_true", help="progress/log on stderr")
    common.add_argument("--decoders-dir", default=None, help="sigrok decoders directory (default: runtime/decoders)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", parents=[common], help="list connected analyzers")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("info", parents=[common], help="connect and show device info")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("limits", parents=[common], help="allowed rates/depths for a channel set (offline)")
    sp.add_argument("-c", "--channels", default="0-15")
    sp.add_argument("--plus", action="store_true", help="Plus model")
    sp.set_defaults(fn=cmd_limits)

    sp = sub.add_parser("capture", parents=[common], help="acquire and print a JSON summary")
    _add_capture_opts(sp)
    sp.add_argument("-o", "--out", action="append", help="save to .vcd/.csv/.atkjson/.json (repeatable)")
    sp.add_argument("--edges", action="store_true", help="include compact edge/run-length text")
    sp.add_argument("--max-edges", type=int, default=200)
    _add_decode_opts(sp)
    sp.set_defaults(fn=cmd_capture)

    sp = sub.add_parser("pwm", parents=[common], help="start a PWM output")
    sp.add_argument("index_pwm", metavar="OUTPUT", type=int, choices=[0, 1], help="PWM output 0 or 1")
    sp.add_argument("-f", "--freq", required=True, help="frequency, 1 Hz..20 MHz, e.g. 1k, 2.5M")
    sp.add_argument("--duty", type=float, default=50, help="duty cycle %% (1..99)")
    sp.add_argument("--hold", help="keep the program running this long then stop PWM (e.g. 10s); "
                                   "default: leave PWM running and exit")
    sp.set_defaults(fn=_run_pwm)

    sp = sub.add_parser("pwm-stop", parents=[common], help="stop PWM output(s)")
    sp.add_argument("index_pwm", metavar="OUTPUT", nargs="?", default="all", choices=["0", "1", "all"],
                    help="0, 1 or all (default)")
    sp.set_defaults(fn=_run_pwm)

    sp = sub.add_parser("open", parents=[common], help="load a .atkdl/.atkjson file, summarize/export")
    sp.add_argument("file")
    _add_window_opts(sp)
    sp.add_argument("-o", "--out", action="append", help="export to .vcd/.csv/.atkjson/.json")
    sp.add_argument("--edges", action="store_true")
    sp.add_argument("--max-edges", type=int, default=200)
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("edges", parents=[common], help="print compact run-length text of a file")
    sp.add_argument("file")
    _add_window_opts(sp)
    sp.add_argument("--max-edges", type=int, default=200)
    sp.set_defaults(fn=cmd_edges)

    sp = sub.add_parser("decode", parents=[common], help="run sigrok protocol decoders")
    sp.add_argument("file", nargs="?", help=".atkdl/.atkjson input (omit with --capture)")
    sp.add_argument("--capture", action="store_true", help="capture from the device first")
    _add_capture_opts(sp, required=False)
    sp.add_argument("-o", "--out", action="append", help="with --capture: also save the capture")
    sp.add_argument("--start", help="window start time")
    sp.add_argument("--end", help="window end time")
    _add_decode_opts(sp)
    sp.set_defaults(fn=cmd_decode)

    sp = sub.add_parser("decoders", parents=[common], help="list available protocol decoders")
    sp.add_argument("id", nargs="?", help="show details (channels/options) of one decoder")
    sp.add_argument("--full", action="store_true")
    sp.set_defaults(fn=cmd_decoders)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    from .device import AtkError, CaptureError, DeviceAccessError, DeviceNotFound, DeviceStateError
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        rc = args.fn(args)
        return rc or 0
    except DeviceNotFound as e:
        return _fail(e, EXIT_NOTFOUND, args)
    except DeviceAccessError as e:
        return _fail(e, EXIT_ACCESS, args)
    except DeviceStateError as e:
        return _fail(e, EXIT_STATE, args)
    except CaptureError as e:
        return _fail(e, EXIT_CAPTURE, args)
    except AtkError as e:
        return _fail(e, EXIT_OTHER, args)
    except (ValueError, KeyError, FileNotFoundError, ImportError) as e:
        return _fail(e, EXIT_USAGE, args)
    except KeyboardInterrupt:
        return _fail(RuntimeError("interrupted"), 130, args)


def _run_pwm(args) -> int:
    import time
    from .device import AtkLogic
    log = (lambda m: sys.stderr.write(m + "\n")) if args.verbose else None
    la = AtkLogic.open(index=args.index, log=log)
    if args.cmd == "pwm":
        try:
            res = la.pwm(int(args.index_pwm), parse_freq(args.freq), args.duty)
        except Exception:
            la.close()
            raise
        if args.hold:
            try:
                time.sleep(parse_time(args.hold))
            finally:
                la.close()
            res["state"] = "stopped after --hold"
        else:
            la.close(keep_pwm=True)
            res["state"] = "running (USB released, output left on; stop with `pwm-stop`)"
        _emit({"pwm": res}, args)
    else:
        try:
            if str(args.index_pwm) == "all":
                la.close_all_pwm()
                la.set_reset_state(0)
            else:
                la.pwm_stop(int(args.index_pwm))
        finally:
            la.close()
        _emit({"pwm_stopped": str(args.index_pwm)}, args)
    return 0


def _fail(e: Exception, code: int, args) -> int:
    hint = getattr(e, "hint", "")
    if not hint and isinstance(e, ImportError):
        hint = "pip install libusb1 numpy"
    obj = {"error": str(e), "type": type(e).__name__}
    if hint:
        obj["hint"] = hint
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
