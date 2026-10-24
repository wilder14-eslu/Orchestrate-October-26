"""End-to-end deterministic pipeline: dataset/ -> output.csv (+ optional per-case trace)."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .identity import IdentityIndex
from .ingest import load_corpus
from .ingest.csv_tables import load_requests
from .intent import Intent, classify
from .llm.usage import UsageLedger
from .llm.vlm import ModelLayer
from .text_cache import TextCache, sha256_file
from .linker import Linker
from .planner import plan
from .policy import Policy
from .state import StateResolver
from .validator import validate

CONTEXT_REQUEST_FILES = ("sample_deletion_requests.csv", "deletion_requests.csv")


def _case_key(case_id: str):
    head, _, num = case_id.rpartition("_")
    return (head, int(num)) if num.isdigit() else (case_id, -1)


FIXTURES = Path(__file__).resolve().parent.parent / "evaluation" / "fixtures"
INTENT_MIN_CONFIDENCE = 0.8


def resolve_intent(case_id: str, text: str, layer: ModelLayer) -> Intent:
    """Rules decide. Only an unresolved request ('no_deletion_signal') may use the model, and its answer is
    accepted only when it is grounded (verbatim quote), not negated and confident; otherwise the rule stands."""
    rule = classify(text)
    if rule.rule != "no_deletion_signal":
        return rule
    answer = layer.intent(case_id, text)
    if (answer and answer["is_deletion_request"] and not answer["negated"] and answer["confidence"] >= INTENT_MIN_CONFIDENCE
            and answer["quote"] and answer["quote"] in text):
        return Intent(True, "llm_fallback", answer["quote"])
    return Intent(False, "no_deletion_signal" if not answer else "llm_fallback_rejected", answer["quote"] if answer else "")


def run(data_dir: Path, requests_path: Path, out_path: Path, attribution: str = "scored", ocr: bool = True,
        trace_path: Optional[Path] = None, run_min_margin: float = 2.0, vlm_refresh: bool = False,
        usage_path: Optional[Path] = None, usage_label: str = "full_run") -> int:
    ledger = UsageLedger()
    layer = ModelLayer(TextCache(), ledger, refresh=vlm_refresh)
    if vlm_refresh and FIXTURES.exists():  # evaluation fixtures (synthetic screenshots) share the same cache
        from .models import ParseWarning
        for png in sorted(FIXTURES.glob("*.png")):
            layer.screen(png, sha256_file(png), [], png.name, str(png.name))
    corpus = load_corpus(data_dir, ocr=ocr, model_layer=layer)
    targets = load_requests(requests_path)
    context = {r.case_id: r for f in CONTEXT_REQUEST_FILES if (data_dir / f).exists() for r in load_requests(data_dir / f)}
    context.update({r.case_id: r for r in targets})
    index = IdentityIndex(corpus.identities, corpus.audit_log)
    deletion = {r.case_id: resolve_intent(r.case_id, r.request_text, layer) for r in context.values()}
    warnings_by_evidence: Dict[str, List[str]] = {}
    for w in corpus.warnings:
        warnings_by_evidence.setdefault(w.evidence_id, []).append(w.message)
    # Only requests that authorize deletion take part in linking and worker attribution.
    links = Linker(corpus, index, [r for r in context.values() if deletion[r.case_id].is_deletion], attribution, run_min_margin).link()
    resolver = StateResolver(corpus)
    worker_ops: Dict[str, List] = {}
    for a in corpus.activity:
        if a.source_system == "deletion_worker":
            worker_ops.setdefault(a.resource_id, []).append((a.event_time, a.event_type))
    policy = Policy(corpus.rules, worker_ops)
    citable = {m.file_id: m.captured_at for m in corpus.manifest}
    citable.update({a.activity_id: a.event_time for a in corpus.activity})
    citable.update({a.audit_id: a.event_time for a in corpus.audit_log})

    rows, trace, failures = [], {}, 0
    for r in sorted(targets, key=lambda r: _case_key(r.case_id)):
        intent = deletion[r.case_id]
        case_trace: Dict[str, object] = {"intent": {"deletion": intent.is_deletion, "rule": intent.rule, "match": intent.evidence}}
        if not intent.is_deletion:
            status, steps, notes = "NOT_APPLICABLE", [], []
        else:
            states = resolver.resolve_case(r, links)
            decisions = [d for d in (policy.decide(s, r.audit_at) for s in states) if d]
            status, steps, notes = plan(decisions)
            case_trace.update({
                "window": links.windows.get(r.case_id),
                "links": [vars(l) if hasattr(l, "__dict__") else l._asdict() for l in []] or
                         [dict(zip(("system_id", "resource_id", "via", "evidence_id", "observed_at", "detail"),
                                   (l.system_id, l.resource_id, l.via, l.evidence_id, l.observed_at, l.detail)))
                          for l in links.links.get(r.case_id, [])],
                "states": [{"resource_id": s.resource_id, "category": s.category, "presence": s.presence, "state": s.state,
                            "basis": s.basis, "proof": s.proof, "late_hint": s.late_hint, "notes": list(s.notes)} for s in states],
                "notes": list(links.notes.get(r.case_id, [])) + notes})
            used = {l.evidence_id for l in links.links.get(r.case_id, [])} | {s.proof for s in states if s.proof}
            case_trace["evidence_warnings"] = {e: warnings_by_evidence[e] for e in sorted(used) if e in warnings_by_evidence}
        errors = validate(status, steps, r.audit_at, citable)
        if errors:
            failures += 1
            case_trace["contract_errors"] = errors
            print(f"[contract] {r.case_id}: {errors}", file=sys.stderr)
        rows.append((r.case_id, status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))))
        trace[r.case_id] = case_trace

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        writer.writerows(rows)
    if usage_path:
        ledger.write(usage_path, usage_label, len(targets), sum(1 for m in corpus.manifest
                                                                 if Path(m.file_path).suffix.lower() == ".png"))
    if trace_path:
        trace_path.write_text(json.dumps({"attribution": attribution, "model": links.model, "usage": ledger.to_dict(),
                                          "cases": trace},
                                         indent=1, ensure_ascii=False, default=str), encoding="utf-8", newline="\n")
    return 3 if failures else 0
