"""Contract validator for every output row (runs before output.csv is written)."""
from __future__ import annotations

from typing import Dict, List

from .config import OPERATIONS, STATUSES

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate(status: str, steps: List[dict], audit_at: str, citable_times: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    if status not in STATUSES:
        return [f"invalid status {status}"]
    if status == "NOT_APPLICABLE":
        return [] if steps == [] else ["NOT_APPLICABLE must have no steps"]
    if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
        errors.append("INCOMPLETE without a finding")
    previous = None
    for i, s in enumerate(steps, start=1):
        if set(s) != KEYS or s["step_no"] != i:
            errors.append(f"step {i}: malformed or misnumbered")
            continue
        phase, state, order = OPERATIONS.get(s["operation"], (None, None, None))
        if (s["phase"], s["state"]) != (phase, state):
            errors.append(f"step {i}: {s['operation']} must be {phase}/{state}")
        key = (PHASE_ORDER[s["phase"]], order, s["system_id"], s["resource_id"])
        if previous and key < previous:
            errors.append(f"step {i}: out of order")
        previous = key
        needs = status == "COMPLETE" or s["phase"] == "FINDING"
        if needs and not s["evidence_id"]:
            errors.append(f"step {i}: missing evidence")
        if not needs and s["evidence_id"]:
            errors.append(f"step {i}: future work must not cite evidence")
        if s["evidence_id"]:
            when = citable_times.get(s["evidence_id"])
            if when is None or when > audit_at:
                errors.append(f"step {i}: {s['evidence_id']} is not citable at {audit_at}")
        if status == "COMPLETE" and s["phase"] != "VERIFICATION":
            errors.append(f"step {i}: COMPLETE allows verification steps only")
    return errors
