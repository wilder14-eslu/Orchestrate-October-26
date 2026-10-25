"""Deterministic merge of a VLM screen extraction with the OCR text of the same image.

1. Deterministic OCR/parsed values are authoritative; the model never overrides rules or OCR.
2. VLM may only fill fields that OCR left empty or unreadable (unstated flow status, restore status,
   or unparsed identifiers).
3. Every OCR/VLM disagreement is recorded as a warning in the trace; OCR values are preserved.
4. A VLM result must never remove an observation, a record, or a step.
5. Unconfirmed VLM ids are kept as suspects (they need independent corroboration before producing steps).
6. injection_detected is recorded as a warning; instruction text is always treated as data.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple

from .. import config as C
from ..models import Identifiers, ManifestEntry, Observation, ParseWarning
from ..ingest.injection import INJECTION_RE
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


def merge_screen(
    entry: ManifestEntry,
    ocr_text: Optional[str],
    vlm: Optional[dict],
    aliases: Dict[str, str],
    known_resources: Set[str],
    known: Dict[str, Set[str]],
    warnings: List[ParseWarning],
) -> Tuple[List[Observation], List[dict]]:
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
    ocr_digits = "".join(c for c in cleaned if c.isdigit())
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

    # Deterministic OCR is authoritative: initialize merged dictionary with all OCR observations.
    # A VLM result must never remove an observation, a record, or a step.
    merged: Dict[str, Observation] = {o.resource_id: o for o in ocr_obs}

    subject = vlm.get("subject", {})
    subject_ids = [e for e in subject.get("emails", []) if keep_email(e)] + [u for u in subject.get("user_ids", []) if keep_user(u)]

    for rec in vlm.get("records", []):
        rid = rec.get("resource_id")
        if not rid:
            ref = (rec.get("reference") or "").strip()
            rid = aliases.get(ref) if ref and (not ref.startswith("user_") or single) else None
            if not rid:
                continue
            confirmed = True  # resolved through data catalog
        else:
            rid, confirmed = confirm(rid)

        # Instruction detection inside VLM text fields
        for field_val in [rec.get("status_text"), rec.get("reference")]:
            if field_val and INJECTION_RE.search(field_val):
                warnings.append(ParseWarning(entry.file_id, entry.file_path, rid or "-",
                                             f"injection_detected: prompt injection in screenshot text: {field_val[:50]}"))

        vlm_emails = [e for e in rec.get("emails", []) if keep_email(e)]
        vlm_phones = [p for p in rec.get("phones", []) if keep_phone(p)]
        vlm_users = [u for u in rec.get("user_ids", []) if keep_user(u)]
        vlm_status = rec.get("status")

        if rid in merged:
            # Deterministic OCR is authoritative; VLM only fills missing / unreadable fields
            existing = merged[rid]

            # 1. Fill missing identifiers that OCR left empty
            new_emails = set(existing.identifiers.emails)
            new_phones = set(existing.identifiers.phones)
            new_users = set(existing.identifiers.user_ids)
            for e in vlm_emails:
                new_emails.add(e.lower())
            for p in vlm_phones:
                new_phones.add(C.norm_phone(p))
            for u in vlm_users:
                new_users.add(u)

            updated_ids = Identifiers(
                emails=frozenset(new_emails),
                phones=frozenset(new_phones),
                hashes=existing.identifiers.hashes,
                user_ids=frozenset(new_users),
                names=existing.identifiers.names
            )

            # Record email disagreements (e.g. OCR split b@example.com vs a.b@example.com)
            if vlm_emails and existing.identifiers.emails:
                ocr_em = set(existing.identifiers.emails)
                vlm_em = set(e.lower() for e in vlm_emails)
                if ocr_em != vlm_em:
                    warnings.append(ParseWarning(entry.file_id, entry.file_path, rid,
                                                 f"OCR/VLM email disagreement on {rid}: OCR={sorted(ocr_em)} VLM={sorted(vlm_em)}; OCR authoritative"))

            # 2. Check flow status for DATA_FLOW: OCR authoritative; VLM fills gaps; record disagreements
            new_flow = existing.flow_enabled
            if existing.category == "DATA_FLOW":
                if existing.flow_enabled is None and vlm_status in ("enabled", "disabled"):
                    new_flow = (vlm_status == "enabled")
                elif existing.flow_enabled is not None and vlm_status in ("enabled", "disabled"):
                    v_bool = (vlm_status == "enabled")
                    if existing.flow_enabled != v_bool:
                        warnings.append(ParseWarning(entry.file_id, entry.file_path, rid,
                                                     f"OCR/VLM disagreement on {rid}: OCR={existing.flow_enabled} VLM={v_bool}; OCR authoritative"))

            # 3. Check restore status for BACKUP_ARCHIVE: OCR authoritative; VLM fills gaps; record disagreements
            new_restore = existing.restore_allowed
            if existing.category == "BACKUP_ARCHIVE":
                if existing.restore_allowed is None and vlm_status in ("restore_allowed", "restore_blocked", "enabled", "disabled"):
                    new_restore = vlm_status in ("restore_allowed", "enabled")
                elif existing.restore_allowed is not None and vlm_status in ("restore_allowed", "restore_blocked", "enabled", "disabled"):
                    v_bool = vlm_status in ("restore_allowed", "enabled")
                    if existing.restore_allowed != v_bool:
                        warnings.append(ParseWarning(entry.file_id, entry.file_path, rid,
                                                     f"OCR/VLM disagreement on {rid}: OCR={existing.restore_allowed} VLM={v_bool}; OCR authoritative"))

            attrs = dict(existing.attrs)
            if rec.get("status_text"):
                attrs["vlm_status_text"] = rec["status_text"][:80]

            merged[rid] = replace(
                existing,
                identifiers=updated_ids,
                flow_enabled=new_flow,
                restore_allowed=new_restore,
                extraction_method="vlm+ocr",
                confidence=max(existing.confidence, AGREE_CONFIDENCE if confirmed else VLM_ONLY_CONFIDENCE),
                attrs=attrs
            )
        else:
            # Resource not in OCR: VLM adds new observation ONLY when independently confirmed
            if not confirmed:
                warnings.append(ParseWarning(entry.file_id, entry.file_path, rid or "-",
                                             f"unconfirmed VLM resource {rid} kept as suspect"))
                continue
            row_lines = [l for l in lines if rid in l]
            text = (cleaned + "\n" + " ".join(subject_ids)) if single else ("\n".join(row_lines) + "\n" + " ".join(vlm_emails + vlm_users))
            _, category = C.system_and_category(rid)
            raw = {"resource_id": rid, "document_text": text, "origin": entry.origin}
            if vlm_status in ("enabled", "disabled") and category == "DATA_FLOW":
                raw["enabled"] = (vlm_status == "enabled")
            if vlm_status in ("restore_allowed", "restore_blocked") and category == "BACKUP_ARCHIVE":
                raw["restore_allowed"] = (vlm_status == "restore_allowed")

            extra = {"vlm_status_text": rec.get("status_text", "")[:80], "single_subject": str(single)}
            ctx = RecordContext(entry.file_id, "IMAGE", entry.captured_at, f"vlm:{rid}", collection=entry.file_path,
                                extraction_method="vlm+ocr", confidence=AGREE_CONFIDENCE, extra=extra)
            obs = interpret(raw, ctx)
            if obs:
                merged[rid] = obs

    return sorted(merged.values(), key=lambda o: o.resource_id), mentions
