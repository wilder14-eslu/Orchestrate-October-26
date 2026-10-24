"""Plain-text handover records: '- <time> | <system> | <resource> | <CATEGORY> | <RESULT> | <key>'."""
from __future__ import annotations

from typing import List, Tuple

from ..models import CaptureScope, ManifestEntry, Observation, ParseWarning
from .records import RecordContext, interpret


def parse(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> Tuple[List[Observation], List[CaptureScope]]:
    observations = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        body = line.strip().lstrip("-").strip()
        parts = [p.strip() for p in body.split("|")]
        if len(parts) != 6:
            continue
        when, system, resource, category, result, key = parts
        raw = {"resource_id": resource, "system": system, "record_type": category, "result": result,
               "observed_at": when, "lookup_value": key}
        obs = interpret(raw, RecordContext(entry.file_id, "TXT", entry.captured_at, f"L{lineno}"))
        if obs:
            observations.append(obs)
        else:
            warnings.append(ParseWarning(entry.file_id, entry.file_path, f"line {lineno}", "unrecognized handover row"))
    return observations, []
