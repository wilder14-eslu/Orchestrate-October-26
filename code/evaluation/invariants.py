"""Contract invariants for any output file, independent of gold answers.

These checks run on the full 518-row output.csv as well as on sample and
custom-case predictions. Each violation is returned as a short string.
"""
from __future__ import annotations

import csv
import json
import re
from typing import Dict, List

HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
# Operation order inside each phase, as listed in the problem statement table.
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT", 0),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT", 1),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE", 2),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE", 3),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED", 0),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED", 1),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT", 2),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED", 3),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED", 4),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT", 0),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT", 1),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED", 2),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED", 3),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED", 4),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED", 5),
}
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def natural_key(case_id: str):
    m = re.search(r"(\d+)$", case_id)
    return (case_id[: m.start()] if m else case_id, int(m.group(1)) if m else -1)


def step_sort_key(step: dict):
    return (PHASE_ORDER.get(step.get("phase"), 9), OPERATIONS.get(step.get("operation"), ("", "", 9))[2],
            step.get("system_id", ""), step.get("resource_id", ""))


def check_output(path: str, requests: List[dict], evidence_times: Dict[str, str]) -> List[str]:
    """Validate an output file. evidence_times maps every citable id to its capture/event time."""
    errors: List[str] = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            rows = list(reader)
    except FileNotFoundError:
        return [f"missing output file: {path}"]
    if header != HEADER:
        errors.append(f"bad header: {header}")
    req_by_id = {r["case_id"]: r for r in requests}
    ids = [r[0] for r in rows if r]
    if len(rows) != len(requests):
        errors.append(f"row count {len(rows)} != request count {len(requests)}")
    if set(ids) != set(req_by_id):
        errors.append(f"case_id set mismatch: missing={sorted(set(req_by_id) - set(ids))[:5]} extra={sorted(set(ids) - set(req_by_id))[:5]}")
    if len(ids) != len(set(ids)):
        errors.append("duplicate case_id rows")
    if ids != sorted(ids, key=natural_key):
        errors.append("rows are not sorted by case_id (natural numeric order)")
    for row in rows:
        if len(row) != 3:
            errors.append(f"{row[:1]}: expected 3 columns, got {len(row)}")
            continue
        cid, status, raw = row
        errors.extend(f"{cid}: {e}" for e in check_row(status, raw, req_by_id.get(cid), evidence_times))
    return errors


def check_row(status: str, raw: str, request: dict | None, evidence_times: Dict[str, str]) -> List[str]:
    errs: List[str] = []
    if status not in STATUSES:
        return [f"invalid status {status!r}"]
    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"steps_json is not valid JSON ({exc.msg})"]
    if not isinstance(steps, list):
        return ["steps_json is not an array"]
    if status == "NOT_APPLICABLE":
        return [] if steps == [] else ["NOT_APPLICABLE must have []"]
    if not steps:
        errs.append(f"{status} with no steps")
    seen = set()
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict) or set(s) != STEP_KEYS:
            errs.append(f"step {i}: wrong keys {sorted(s) if isinstance(s, dict) else type(s).__name__}")
            continue
        if s["step_no"] != i:
            errs.append(f"step {i}: step_no={s['step_no']}")
        spec = OPERATIONS.get(s["operation"])
        if not spec:
            errs.append(f"step {i}: unknown operation {s['operation']}")
            continue
        if (s["phase"], s["state"]) != spec[:2]:
            errs.append(f"step {i}: {s['operation']} needs phase/state {spec[:2]}")
        key = (s["phase"], s["system_id"], s["resource_id"], s["operation"])
        if key in seen:
            errs.append(f"step {i}: duplicate step {key}")
        seen.add(key)
        ev = s["evidence_id"]
        cites_needed = status == "COMPLETE" or s["phase"] == "FINDING"
        if cites_needed and not ev:
            errs.append(f"step {i}: missing evidence_id")
        if not cites_needed and ev:
            errs.append(f"step {i}: future work must use empty evidence_id")
        if ev:
            when = evidence_times.get(ev)
            if when is None:
                errs.append(f"step {i}: unknown evidence_id {ev}")
            elif request and when > request["audit_at"]:
                errs.append(f"step {i}: cites {ev} captured after audit_at")
    if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps if isinstance(s, dict)):
        errs.append("COMPLETE must contain only VERIFICATION steps")
    if status == "INCOMPLETE" and not any(isinstance(s, dict) and s.get("phase") == "FINDING" for s in steps):
        errs.append("INCOMPLETE without findings")
    dict_steps = [s for s in steps if isinstance(s, dict)]
    if dict_steps != sorted(dict_steps, key=step_sort_key):
        errs.append("steps not in phase/operation/system/resource order")
    return errs
