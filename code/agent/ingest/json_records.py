"""JSON evidence -> raw records. Truncated files are salvaged record by record."""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

from .. import config as C
from ..models import CaptureScope, ManifestEntry, Observation, ParseWarning
from .records import KNOWN_SYSTEMS, RecordContext, interpret

CONTAINER_KEYS = {"records", "snapshots", "service_exports", "items", "entries", "data"}
SALVAGE_CONFIDENCE = 0.6


def _walk(node: Any, path: List[str], ctx_fields: dict, out: list) -> None:
    if isinstance(node, dict):
        fields = dict(ctx_fields)
        for key in ("data_store", "collection", "system", "service", "captured_at", "region"):
            if isinstance(node.get(key), str):
                fields[key] = node[key]
        if "resource_id" in node:
            out.append((node, path, fields))
            return
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _walk(value, path + [str(key)], fields, out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk(value, path + [str(i)], ctx_fields, out)


def _context(entry: ManifestEntry, path: List[str], fields: dict, locator: str, method: str, confidence: float) -> RecordContext:
    system_hint = fields.get("data_store") or fields.get("system")
    named = [p for p in path if p in KNOWN_SYSTEMS]
    if named:
        system_hint = named[-1]
    collection = fields.get("collection") or next((p for p in reversed(path) if not p.isdigit() and p not in CONTAINER_KEYS
                                                    and p not in KNOWN_SYSTEMS), None)
    return RecordContext(entry.file_id, "JSON", entry.captured_at, locator, system_hint=system_hint,
                         collection=collection, extraction_method=method, confidence=confidence)


def _scopes(entry: ManifestEntry, data: Any) -> List[CaptureScope]:
    """A snapshot that names a data_store and lists its records is a complete listing of that store."""
    scopes = []
    if isinstance(data, dict) and isinstance(data.get("snapshots"), list):
        for snap in data["snapshots"]:
            if isinstance(snap, dict) and isinstance(snap.get("records"), list) and snap.get("data_store"):
                scopes.append(CaptureScope(entry.file_id, snap["data_store"], snap.get("captured_at", entry.captured_at), "NONE", True))
    return scopes


def _salvage(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> List[Observation]:
    """Decode every complete JSON object that carries a resource_id; skip the cut-off remainder."""
    decoder = json.JSONDecoder()
    observations: List[Observation] = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict) and "resource_id" in obj:
            before = text[:start]
            store = re.findall(r'"data_store"\s*:\s*"([^"]+)"', before)
            coll = re.findall(r'"collection"\s*:\s*"([^"]+)"', before)
            fields = {"data_store": store[-1]} if store else {}
            if coll:
                fields["collection"] = coll[-1]
            ctx = _context(entry, [], fields, f"offset {start}", "salvage", SALVAGE_CONFIDENCE)
            obs = interpret(obj, ctx)
            if obs:
                observations.append(obs)
            pos = end
        else:
            pos = start + 1
    return observations


def parse(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> Tuple[List[Observation], List[CaptureScope]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        observations = _salvage(entry, text, warnings)
        warnings.append(ParseWarning(entry.file_id, entry.file_path, f"char {exc.pos}",
                                     f"invalid JSON ({exc.msg}); salvaged {len(observations)} complete record(s) "
                                     f"at confidence {SALVAGE_CONFIDENCE}"))
        return observations, []
    found: list = []
    _walk(data, [], {}, found)
    observations = []
    for i, (node, path, fields) in enumerate(found):
        obs = interpret(node, _context(entry, path, fields, f"{'/'.join(path)}:{node.get('resource_id')}", "rule", 1.0))
        if obs:
            observations.append(obs)
    return observations, _scopes(entry, data)
