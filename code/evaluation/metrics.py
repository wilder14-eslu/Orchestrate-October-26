"""Scoring functions for the deletion-audit agent.

All functions are pure and stdlib-only. A "step" is compared on its seven
contract fields; step_no is checked separately because it is derived from
array position.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Dict, List, Tuple

STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
KEY_FIELDS = STEP_FIELDS[1:]  # every field except step_no

StepKey = Tuple[str, str, str, str, str, str]


def parse_steps(raw: str) -> List[dict]:
    """Parse a steps_json cell. Raises ValueError on invalid JSON or a non-list."""
    steps = json.loads(raw)
    if not isinstance(steps, list):
        raise ValueError("steps_json is not a JSON array")
    return steps


def step_key(step: dict) -> StepKey:
    return tuple(str(step.get(f, "")) for f in KEY_FIELDS)  # type: ignore[return-value]


def case_metrics(gold_status: str, gold_steps: List[dict], pred_status: str, pred_steps: List[dict]) -> dict:
    """Per-case comparison: status, multiset step overlap, positional field matches, exact sequence."""
    g = Counter(step_key(s) for s in gold_steps)
    p = Counter(step_key(s) for s in pred_steps)
    tp = sum((g & p).values())
    positions = max(len(gold_steps), len(pred_steps))
    field_hits = {f: 0 for f in STEP_FIELDS}
    for i in range(positions):
        gs = gold_steps[i] if i < len(gold_steps) else {}
        ps = pred_steps[i] if i < len(pred_steps) else {}
        for f in STEP_FIELDS:
            if i < len(gold_steps) and i < len(pred_steps) and str(gs.get(f, "")) == str(ps.get(f, "")):
                field_hits[f] += 1
    return {
        "status_ok": gold_status == pred_status,
        "tp": tp,
        "gold_n": len(gold_steps),
        "pred_n": len(pred_steps),
        "positions": positions,
        "field_hits": field_hits,
        "sequence_ok": [step_key(s) for s in gold_steps] == [step_key(s) for s in pred_steps]
        and [s.get("step_no") for s in pred_steps] == list(range(1, len(pred_steps) + 1)),
        "missing": sorted((g - p).elements()),
        "extra": sorted((p - g).elements()),
    }


def aggregate(per_case: Dict[str, dict]) -> dict:
    """Micro-averaged metrics over all cases."""
    n = len(per_case)
    if n == 0:
        return {"cases": 0}
    tp = sum(c["tp"] for c in per_case.values())
    gold_n = sum(c["gold_n"] for c in per_case.values())
    pred_n = sum(c["pred_n"] for c in per_case.values())
    positions = sum(c["positions"] for c in per_case.values())
    precision = tp / pred_n if pred_n else 1.0
    recall = tp / gold_n if gold_n else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    field_acc = {
        f: (sum(c["field_hits"][f] for c in per_case.values()) / positions if positions else 1.0)
        for f in STEP_FIELDS
    }
    return {
        "cases": n,
        "status_accuracy": sum(c["status_ok"] for c in per_case.values()) / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": f1,
        "field_accuracy": field_acc,
        "field_accuracy_mean": sum(field_acc.values()) / len(field_acc),
        "sequence_accuracy": sum(c["sequence_ok"] for c in per_case.values()) / n,
        "gold_steps": gold_n,
        "pred_steps": pred_n,
        "matched_steps": tp,
    }


def status_confusion(gold: Dict[str, str], pred: Dict[str, str]) -> Dict[str, Dict[str, int]]:
    labels = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE", "MISSING"]
    table = {g: {p: 0 for p in labels} for g in labels[:3]}
    for cid, g in gold.items():
        table.setdefault(g, {p: 0 for p in labels})[pred.get(cid, "MISSING") if pred.get(cid) in labels else "MISSING"] += 1
    return table
