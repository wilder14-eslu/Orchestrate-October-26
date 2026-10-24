"""Deterministic merge of a VLM screen extraction with the OCR text of the same image.

1. Resource ids: a VLM id is confirmed when the OCR text has it (or a one-edit variant; the VLM
   spelling wins). Unconfirmed VLM ids are kept as suspects (they need independent corroboration
   before producing steps). An OCR suspect confirmed by the VLM loses its suspect flag.
2. Identifiers: a VLM email/phone/user_id is kept only when the OCR text shows it (emails up to one
   edit) or it matches a known identity exactly (hallucination guard).
3. Status: a status stated by one source fills the other's gap; on conflict the riskier one wins
   (enabled over disabled, restore allowed over blocked) and the conflict is recorded.
4. Confidence: 0.8 when VLM and OCR agree, 0.7 for VLM-only rows.
5. injection_detected is recorded as a warning; record fields still pass rules 1-3.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from .. import config as C
from ..models import ManifestEntry, Observation, ParseWarning
from ..ingest.pdf_text import (FLOW_OFF, FLOW_ON, RESTORE_OFF, RESTORE_ON, _edit_distance, clean_ocr,
                               document_records, ocr_vote)
from ..ingest.records import RecordContext, interpret

AGREE_CONFIDENCE, VLM_ONLY_CONFIDENCE = 0.8, 0.7


def _ocr_status(category: Optional[str], line_text: str) -> Optional[str]:
    if category == "DATA_FLOW" and (FLOW_OFF.search(line_text) or FLOW_ON.search(line_text)):
        return "disabled" if FLOW_OFF.search(line_text) else "enabled"
    if category == "BACKUP_ARCHIVE" and (RESTORE_OFF.search(line_text) or RESTORE_ON.search(line_text)):
        return "restore_blocked" if RESTORE_OFF.search(line_text) else "restore_allowed"
    return None


RISK = {"enabled": 2, "disabled": 1, "restore_allowed": 2, "restore_blocked": 1}


def merge_screen(entry: ManifestEntry, ocr_text: Optional[str], vlm: Optional[dict], aliases: Dict[str, str],
                 known: Dict[str, Set[str]], warnings: List[ParseWarning]):
    ocr_text = ocr_text or ""
    ocr_obs, mentions = document_records(entry, ocr_text, "IMAGE", "ocr", 0.5, aliases) if ocr_text else ([], [])
    if not vlm:
        return ocr_obs, mentions
    if vlm.get("injection_detected"):
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-",
                                     "VLM injection_detected: instruction-like text in screenshot treated as data"))
    cleaned, _ = ocr_vote(clean_ocr(ocr_text))
    lines = cleaned.splitlines()
    ocr_ids = set(C.RESOURCE_RE.findall(cleaned))
    ocr_emails = {e.lower() for e in C.EMAIL_RE.findall(cleaned)}
    ocr_digits = re.sub(r"\D", "", cleaned)
    by_id = {o.resource_id: o for o in ocr_obs}
    doc_users = set(C.USER_RE.findall(cleaned))
    single = len(ocr_emails) <= 1 and len(doc_users) <= 1 and bool(ocr_emails or doc_users)

    def confirm(rid: str) -> Tuple[Optional[str], bool]:
        if rid in ocr_ids:
            return rid, True
        near = [x for x in ocr_ids if x.rsplit("_", 1)[0] == rid.rsplit("_", 1)[0] and _edit_distance(x, rid) <= 1]
        return (rid, True) if near else (rid, False)

    def keep_email(e: str) -> bool:
        e = e.lower()
        return e in known["emails"] or e in ocr_emails or any(_edit_distance(e, x) <= 1 for x in ocr_emails)

    def keep_phone(p: str) -> bool:
        d = C.norm_phone(p)
        return len(d) >= 8 and (d in known["phones"] or d in ocr_digits)

    def keep_user(u: str) -> bool:
        return u in known["user_ids"] or u in cleaned

    merged: Dict[str, Observation] = dict(by_id)
    subject = vlm.get("subject", {})
    subject_ids = [e for e in subject.get("emails", []) if keep_email(e)] + [u for u in subject.get("user_ids", []) if keep_user(u)]
    for rec in vlm.get("records", []):
        rid = rec.get("resource_id")
        if not rid:
            ref = (rec.get("reference") or "").strip()
            rid = aliases.get(ref) if ref and (not ref.startswith("user_") or single) else None
            if not rid:
                continue
            confirmed = True  # resolved through the data catalog, an independent source
        else:
            rid, confirmed = confirm(rid)
        ids = [e for e in rec.get("emails", []) if keep_email(e)] + [p for p in rec.get("phones", []) if keep_phone(p)] + \
              [u for u in rec.get("user_ids", []) if keep_user(u)]
        row_lines = [l for l in lines if rid in l]
        text = (cleaned + "\n" + " ".join(subject_ids)) if single else ("\n".join(row_lines) + "\n" + " ".join(ids))
        _, category = C.system_and_category(rid)
        vlm_status, ocr_status = rec.get("status"), _ocr_status(category, "\n".join(row_lines))
        status = vlm_status if vlm_status in RISK else ocr_status
        note = ""
        if vlm_status in RISK and ocr_status in RISK and vlm_status != ocr_status:
            status = max((vlm_status, ocr_status), key=lambda s: RISK[s])
            note = f"status conflict vlm={vlm_status} ocr={ocr_status}; riskier kept"
        raw = {"resource_id": rid, "document_text": text, "origin": entry.origin}
        if status in ("enabled", "disabled") and category == "DATA_FLOW":
            raw["enabled"] = status == "enabled"
        if status in ("restore_allowed", "restore_blocked") and category == "BACKUP_ARCHIVE":
            raw["restore_allowed"] = status == "restore_allowed"
        suspect = not confirmed
        extra = {"vlm_status_text": rec.get("status_text", "")[:80], "single_subject": str(single)}
        if suspect:
            extra["ocr_suspect"] = "true"
        if note:
            extra["merge_note"] = note
        if vlm.get("injection_detected"):
            extra["injection_detected"] = "true"
        ctx = RecordContext(entry.file_id, "IMAGE", entry.captured_at, f"vlm:{rid}", collection=entry.file_path,
                            extraction_method="vlm+ocr" if confirmed else "vlm",
                            confidence=AGREE_CONFIDENCE if confirmed else VLM_ONLY_CONFIDENCE, extra=extra)
        obs = interpret(raw, ctx)
        if obs:
            merged[rid] = obs
    return sorted(merged.values(), key=lambda o: o.resource_id), mentions
