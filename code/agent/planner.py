"""Step planner: decisions -> ordered findings, remediation and verification steps.

Ordering follows the contract: phase (FINDING, REMEDIATION, VERIFICATION), then the
operation order of the problem statement, then system_id, then resource_id. Within
remediation this puts DISABLE_DATA_FLOW and BLOCK_RESTORE before any record change.
"""
from __future__ import annotations

from typing import List, Tuple

from .config import OPERATIONS
from .policy import Decision

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
KEY_ORDER = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _step(op: str, system: str, resource: str, evidence: str) -> dict:
    phase, state, _ = OPERATIONS[op]
    return {"phase": phase, "system_id": system, "resource_id": resource, "operation": op, "state": state,
            "evidence_id": evidence}


def plan(decisions: List[Decision]) -> Tuple[str, List[dict], List[str]]:
    """Return (status, steps, trace notes) for an authorized deletion."""
    notes: List[str] = []
    incomplete = any(d.finding for d in decisions)
    steps: List[dict] = []
    for d in decisions:
        s = d.state
        if d.finding:
            steps.append(_step(d.finding, s.system_id, s.resource_id, s.proof))
            steps.append(_step(d.remediation, s.system_id, s.resource_id, ""))
        if not d.verification:
            continue
        if incomplete:
            steps.append(_step(d.verification, s.system_id, s.resource_id, ""))
        elif d.citable and s.proof:
            steps.append(_step(d.verification, s.system_id, s.resource_id, s.proof))
        else:
            notes.append(f"verification {d.verification} {s.resource_id} omitted: nothing citable ({d.note or s.basis})")
    unique = {(st["phase"], st["operation"], st["system_id"], st["resource_id"]): st for st in steps}
    ordered = sorted(unique.values(), key=lambda st: (PHASE_ORDER[st["phase"]], OPERATIONS[st["operation"]][2],
                                                      st["system_id"], st["resource_id"]))
    numbered = [{k: ({**st, "step_no": i}[k]) for k in KEY_ORDER} for i, st in enumerate(ordered, start=1)]
    return ("INCOMPLETE" if incomplete else "COMPLETE"), numbered, notes
