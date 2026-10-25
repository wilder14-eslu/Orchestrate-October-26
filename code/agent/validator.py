"""Contract validator for every output row (runs before output.csv is written).

Separates:
- ERRORS: Schema and contract breaks (invalid status enum, NOT_APPLICABLE with steps, malformed keys,
          misnumbered steps, invalid phase/operation/state, out of order, missing evidence on findings,
          future work citing evidence, post-audit evidence leaks, COMPLETE with non-VERIFICATION phase).
          ERRORS cause pipeline failure (exit code 3).
- WARNINGS: Semantic smells (e.g. COMPLETE with zero steps, INCOMPLETE without findings).
            WARNINGS are printed loudly to stderr and recorded in case traces, but do not change
            the exit code unless --strict is specified.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .config import OPERATIONS, STATUSES

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate(status: str, steps: List[dict], audit_at: str, citable_times: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """Validate a single request output row.

    Returns:
        (errors, warnings)
        errors: contract and schema breaks that fail the run.
        warnings: semantic smells (e.g. COMPLETE with zero steps) reported loudly.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # 1. Status enum check (ERROR)
    if status not in STATUSES:
        return [f"invalid status {status}"], []

    # 2. NOT_APPLICABLE contract (ERROR)
    if status == "NOT_APPLICABLE":
        if steps:
            errors.append("NOT_APPLICABLE must have no steps")
        return errors, warnings

    # 3. Semantic smells (WARNINGS)
    if status == "COMPLETE" and not steps:
        warnings.append("COMPLETE with zero steps")
    if status == "INCOMPLETE" and not any(isinstance(s, dict) and s.get("phase") == "FINDING" for s in steps):
        warnings.append("INCOMPLETE without a finding")

    # 4. Step-level structural and contract checks (ERRORS)
    previous = None
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict) or set(s) != KEYS or s.get("step_no") != i:
            errors.append(f"step {i}: malformed or misnumbered")
            continue
        op = s.get("operation")
        phase, state, order = OPERATIONS.get(op, (None, None, None))
        if (s.get("phase"), s.get("state")) != (phase, state):
            errors.append(f"step {i}: {op} must be {phase}/{state}")
        key = (PHASE_ORDER.get(s.get("phase"), 99), order, s.get("system_id"), s.get("resource_id"))
        if previous and key < previous:
            errors.append(f"step {i}: out of order")
        previous = key
        needs = status == "COMPLETE" or s.get("phase") == "FINDING"
        ev = s.get("evidence_id")
        if needs and not ev:
            errors.append(f"step {i}: missing evidence")
        if not needs and ev:
            errors.append(f"step {i}: future work must not cite evidence")
        if ev:
            when = citable_times.get(ev)
            if when is None or when > audit_at:
                errors.append(f"step {i}: {ev} is not citable at {audit_at}")
        if status == "COMPLETE" and s.get("phase") != "VERIFICATION":
            errors.append(f"step {i}: COMPLETE allows verification steps only")

    return errors, warnings
