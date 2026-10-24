"""SQLite-compatible snapshot exports -> raw records + capture scopes.

The whole script is tried first. If it fails, statements are replayed one by
one; a statement that never completes (for example an unterminated string) is
dropped with a warning at its line, and the following statements still load.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import List, Tuple

from .. import config as C
from ..models import CaptureScope, ManifestEntry, Observation, ParseWarning
from .records import RecordContext, interpret

STATEMENT_START = re.compile(r"^\s*(INSERT|CREATE|BEGIN|COMMIT|PRAGMA|DROP|UPDATE|DELETE|END)\b", re.I)


def _load(text: str, entry: ManifestEntry, warnings: List[ParseWarning]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.executescript(text)
        return db
    except sqlite3.Error as exc:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "script", f"whole-script load failed ({exc}); replaying statements"))
    db.close()
    db = sqlite3.connect(":memory:", isolation_level=None)
    buffer: List[str] = []
    start_line = 0

    def flush(complete_required: bool) -> None:
        nonlocal buffer
        stmt = "\n".join(buffer).strip()
        buffer = []
        if not stmt:
            return
        if complete_required and not sqlite3.complete_statement(stmt):
            warnings.append(ParseWarning(entry.file_id, entry.file_path, f"line {start_line}", "incomplete SQL statement dropped"))
            return
        if re.match(r"^\s*(BEGIN|COMMIT|END)\b", stmt, re.I):
            return
        try:
            db.execute(stmt)
        except sqlite3.Error as exc:
            warnings.append(ParseWarning(entry.file_id, entry.file_path, f"line {start_line}", f"statement rejected: {exc}"))

    for lineno, line in enumerate(text.splitlines(), start=1):
        if buffer and STATEMENT_START.match(line) and not sqlite3.complete_statement("\n".join(buffer)):
            flush(complete_required=True)  # a new statement begins while the previous one never closed
        if not buffer:
            if not line.strip() or line.lstrip().startswith("--"):
                continue
            start_line = lineno
        buffer.append(line)
        if sqlite3.complete_statement("\n".join(buffer)):
            flush(complete_required=False)
    flush(complete_required=True)
    return db


def replica_role(table: str) -> str:
    t = table.lower()
    if "failover" in t:
        return "FAILOVER"
    if "reporting" in t:
        return "REPORTING"
    if "read_replica" in t:
        return "READ"
    if re.search(r"replica_[a-z]+_[a-z]+$", t):
        return "REGIONAL"
    if t.endswith("primary"):
        return "PRIMARY"
    return "NONE"


def parse(entry: ManifestEntry, text: str, warnings: List[ParseWarning]) -> Tuple[List[Observation], List[CaptureScope]]:
    db = _load(text, entry, warnings)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    observations: List[Observation] = []
    scopes: List[CaptureScope] = []
    for table in tables:
        cur = db.execute(f'SELECT * FROM "{table}"')
        cols = [d[0].lower() for d in cur.description]
        rows = cur.fetchall()
        if table == "snapshot_scope":
            for system, captured in rows:
                scopes.append(CaptureScope(entry.file_id, system, captured, "NONE", True))
            continue
        role = replica_role(table)
        systems_seen = set()
        for i, row in enumerate(rows):
            raw = dict(zip(cols, row))
            if "record_json" in raw:
                try:
                    inner = json.loads(raw.pop("record_json"))
                    if isinstance(inner, dict):
                        raw = {**inner, **{k: v for k, v in raw.items() if k != "resource_id"}}
                except (TypeError, json.JSONDecodeError):
                    warnings.append(ParseWarning(entry.file_id, entry.file_path, f"{table}[{i}]", "record_json is not valid JSON"))
            collection = raw.get("collection_name") or table
            ctx = RecordContext(entry.file_id, "SQL", entry.captured_at, f"{table}:{raw.get('resource_id')}",
                                collection=str(collection), replica_role=role,
                                extra={"table": table})
            obs = interpret(raw, ctx)
            if obs:
                observations.append(obs)
                systems_seen.add(obs.system_id)
        if "profiles" in table and not systems_seen:
            systems_seen.add("account_service")  # empty profile table still lists the account system
        # A primary/replica table lists every row of the systems it contains (assumption noted in README).
        if role != "NONE":
            for system in sorted(systems_seen):
                scopes.append(CaptureScope(entry.file_id, system, entry.captured_at, role, True))
    db.close()
    return observations, scopes
