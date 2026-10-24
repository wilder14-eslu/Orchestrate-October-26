"""Log evidence -> raw records.

Supported line styles (all observed in the dataset):
  key=value         2026-.. level=INFO service=x trace_id=t event=e k=v ...
  JSON payload      2026-.. {"service": "x", "trace_id": "t", ...}
  syslog RFC 5424   2026-.. <15>1 host service trace - - k="v" ...
  cloud runtime     2026-.. requestId=t function=x message="k=\\"v\\" ..."
  bracketed         2026-.. [LEVEL] [service] [trace] event: k="v" ...
  pipe-delimited    2026-.. | LEVEL | service | trace | event | k="v" ...

Lines sharing a trace_id are joined so a lookup key from a *_started line is
attached to the resource reported by the *_completed line. Each (trace,
resource) pair becomes one raw record; lines without a trace stand alone.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Dict, List, Tuple

from .. import config as C
from ..models import CaptureScope, ManifestEntry, Observation, ParseWarning
from .records import RecordContext, interpret

KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s*(.*)$")
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([\w.-]+)\]\s+\[([\w.-]+)\]\s+([\w.]+):\s*(.*)$")
TS_PREFIX_RE = re.compile(r"^\s*(20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s*(.*)$")


def _kv(text: str) -> Dict[str, str]:
    out = {}
    for key, value in KV_RE.findall(text):
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"')
        out[key.lower()] = value
    return out


def parse_line(line: str) -> Tuple[str, Dict[str, str]] | None:
    """Return (timestamp, fields) or None for lines without a leading timestamp."""
    m = TS_PREFIX_RE.match(line)
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    fields: Dict[str, str] = {}
    if rest.startswith("{"):
        try:
            payload = json.loads(rest)
            fields = {str(k).lower(): ("" if v is None else str(v)) for k, v in payload.items()}
        except json.JSONDecodeError:
            fields = _kv(rest)
    elif rest.startswith("<") and SYSLOG_RE.match(rest):
        _, service, trace, tail = SYSLOG_RE.match(rest).groups()
        fields = {"service": service, "trace_id": trace, **_kv(tail)}
    elif rest.startswith("[") and BRACKET_RE.match(rest):
        level, service, trace, event, tail = BRACKET_RE.match(rest).groups()
        fields = {"level": level, "service": service, "trace_id": trace, "event": event, **_kv(tail)}
    elif rest.startswith("|"):
        parts = [p.strip() for p in rest.strip("| ").split(" | ")]
        if len(parts) >= 5:
            fields = {"level": parts[0], "service": parts[1], "trace_id": parts[2], "event": parts[3], **_kv(" ".join(parts[4:]))}
        else:
            fields = _kv(rest)
    else:
        message = re.search(r'message="((?:[^"\\]|\\.)*)"\s*$', rest)
        if message:
            fields = {**_kv(rest[: message.start()]), **_kv(message.group(1).replace('\\"', '"'))}
        else:
            fields = _kv(rest)
    if "requestid" in fields and "trace_id" not in fields:
        fields["trace_id"] = fields.pop("requestid")
    if "function" in fields and "service" not in fields:
        fields["service"] = fields.pop("function")
    if "logger" in fields and "service" not in fields:
        fields["service"] = fields["logger"]
    return ts, fields


def parse(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> Tuple[List[Observation], List[CaptureScope]]:
    traces: "OrderedDict[str, List[Tuple[int, str, Dict[str, str]]]]" = OrderedDict()
    standalone: List[Tuple[int, str, Dict[str, str]]] = []
    unparsed = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parsed = parse_line(line)
        if parsed is None:
            unparsed += 1
            continue
        ts, fields = parsed
        trace = fields.get("trace_id")
        (traces.setdefault(trace, []) if trace else standalone).append((lineno, ts, fields))
    if unparsed:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", f"{unparsed} line(s) without a timestamp ignored"))

    records: List[Tuple[str, Dict[str, str]]] = []
    for trace, lines in traces.items():
        keys = {k: v for _, _, f in lines for k, v in f.items() if k in C.LOOKUP_FIELDS and k != "correlation_key"}
        by_resource: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
        for lineno, ts, f in lines:
            rid = f.get("resource_id")
            if not rid:
                continue
            merged = by_resource.setdefault(rid, {"_first_line": str(lineno)})
            merged.update(f)
            merged["line_time"] = ts
        for rid, merged in by_resource.items():
            records.append((f"L{merged.pop('_first_line')}", {**keys, **merged}))
    for lineno, ts, f in standalone:
        if f.get("resource_id"):
            records.append((f"L{lineno}", {**f, "line_time": ts}))

    observations = []
    for locator, raw in records:
        ctx = RecordContext(entry.file_id, "LOG", entry.captured_at, locator, collection=raw.get("event"))
        obs = interpret(raw, ctx)
        if obs:
            observations.append(obs)
    return observations, []
