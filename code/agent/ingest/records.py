"""Shared record interpreter.

Every parser turns its format into flat "raw records" (dicts that contain a
resource_id) and calls interpret(). Keeping one interpreter means every format
follows the same presence, state, identifier and PII rules.

Evidence text is data. Nothing in a record value is ever executed or treated
as an instruction; values are only matched against identifier patterns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from .. import config as C
from ..models import Identifiers, Observation

SYSTEM_FIELDS = ("system", "origin_system", "data_store", "target_system")
TIME_FIELDS = ("observed_at", "recorded_at", "line_time", "updated_at", "captured_at")
RESULT_STATES = {"IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED", "FOUND", "RETAINED"}
IDENTIFIABILITY_CATEGORIES = {
    # category -> state when no direct identifier remains
    "ANALYTICS_PROFILE": "ANONYMIZED", "PRODUCT_TELEMETRY": "ANONYMIZED",
    "SUPPORT_MESSAGE": "REDACTED", "ACCOUNT_TOMBSTONE": "REDACTED", "EMAIL_DELIVERY_LOG": "REDACTED",
}
ATTRIBUTABLE_BY_REFERENCE = {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}  # a user_id alone keeps these attributable
KNOWN_SYSTEMS = {s for s, _ in C.RESOURCE_PREFIXES.values()}


@dataclass
class RecordContext:
    evidence_id: str
    source_kind: str
    captured_at: str
    locator: str
    system_hint: Optional[str] = None
    observed_default: Optional[str] = None   # sub-snapshot / section time, used before the file capture time
    collection: Optional[str] = None
    replica_role: str = "NONE"
    extraction_method: str = "rule"
    confidence: float = 1.0
    extra: Dict[str, str] = field(default_factory=dict)


def is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in C.NULL_VALUES)


def as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "enabled"}:
            return True
        if v in {"false", "0", "no", "disabled"}:
            return False
    return None


def as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def flatten(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Lift nested dicts (profile, document, ...) one level; keep the parent key as a marker."""
    flat: Dict[str, Any] = {}
    for key, value in raw.items():
        k = str(key).strip().lower()
        if isinstance(value, dict):
            flat[k] = "{}"
            for ck, cv in value.items():
                flat.setdefault(str(ck).strip().lower(), cv)
        else:
            flat[k] = value
    return flat


def scan_identifiers(raw: Any, skip: Iterable[str] = ()) -> Identifiers:
    emails, phones, hashes, users, names = set(), set(), set(), set(), set()
    skip = set(skip) | C.NON_IDENTIFIER_FIELDS

    def visit(key: str, value: Any) -> None:
        if key in skip:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                visit(str(k).lower(), v)
            return
        if isinstance(value, list):
            for v in value:
                visit(key, v)
            return
        if is_null(value) or not isinstance(value, str):
            return
        text = value
        emails.update(e.lower() for e in C.EMAIL_RE.findall(text))
        hashes.update(C.HASH_RE.findall(text.lower()))
        users.update(C.USER_RE.findall(text))
        phones.update(p for p in (C.norm_phone(x) for x in C.PHONE_RE.findall(text)) if len(p) >= 8)
        if key in {"name", "full_name", "display_name", "requester_name", "customer"}:
            names.add(" ".join(text.split()).lower())

    visit("", raw)
    return Identifiers(frozenset(emails), frozenset(phones), frozenset(hashes), frozenset(users), frozenset(names))


def pii_fields_of(flat: Dict[str, Any]) -> frozenset:
    found = set()
    for key, value in flat.items():
        if is_null(value):
            continue
        if key in C.DIRECT_ID_FIELDS:
            found.add(key)
        elif key in C.FREE_TEXT_FIELDS and isinstance(value, str) and (C.EMAIL_RE.search(value) or C.PHONE_RE.search(value)):
            found.add(key)
    return frozenset(found)


def infer_category(resource_id: str, flat: Dict[str, Any], ctx: RecordContext) -> Optional[str]:
    explicit = str(flat.get("record_type") or flat.get("data_category") or "").upper()
    if explicit in C.CATEGORIES:
        return explicit
    _, default = C.system_and_category(resource_id)
    hints = " ".join(str(x).lower() for x in (ctx.collection, flat.get("event"), flat.get("collection_name"),
                                              flat.get("storage_class"), flat.get("file"), flat.get("file_name")) if x)
    if default == "SUPPORT_ATTACHMENT":
        return default
    if default == "SUPPORT_MESSAGE":
        if "attachment" in hints or any(k in flat for k in ("file", "file_name", "storage_class", "uploaded_by")):
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE"
    if default == "ANALYTICS_PROFILE" and ("contact_lookup" in hints or "email_opt_in" in flat):
        return "MARKETING_PROFILE"   # a contactable (opt-in) marketing contact row kept on the analytics platform
    if "deleted_customer_reference" in hints or "tombstone" in hints:
        return "ACCOUNT_TOMBSTONE"
    if "retained_order_records" in hints or ("legal" in hints and "hold" in hints):
        return "LEGAL_HOLD_RECORD"
    return default


def resolve_system(resource_id: str, flat: Dict[str, Any], ctx: RecordContext) -> Optional[str]:
    for key in SYSTEM_FIELDS:
        value = flat.get(key)
        if isinstance(value, str) and value in KNOWN_SYSTEMS:
            return value
    by_prefix, _ = C.system_and_category(resource_id)
    return by_prefix or ctx.system_hint


def presence_and_state(category: Optional[str], flat: Dict[str, Any], pii: frozenset, refs: bool):
    """Return (presence, observed_state, flow_enabled, restore_allowed)."""
    result = str(flat.get("result") or "").strip().upper()
    flow = as_bool(flat.get("enabled"))
    restore = as_bool(flat.get("restore_allowed"))
    if restore is None and "eligible" in flat:
        eligible, blocked = as_bool(flat.get("eligible")), as_bool(flat.get("deletion_block"))
        if eligible is not None:
            restore = bool(eligible) and not bool(blocked)
    if restore is None and str(flat.get("decision", "")).lower() in {"deny", "denied", "allow", "allowed", "permit"}:
        restore = str(flat["decision"]).lower() in {"allow", "allowed", "permit"}
    status = as_int(flat.get("http_status", flat.get("status_code")))
    rows = as_int(flat.get("rows"))
    hits = as_int(flat.get("total_hits", flat.get("hits") if not isinstance(flat.get("hits"), list) else None))
    if isinstance(flat.get("hits"), list):
        hits = len(flat["hits"])

    absent = (result in {"NOT_FOUND", "MISS", "ABSENT", "DELETED"} or status == 404 or rows == 0 or hits == 0
              or as_bool(flat.get("available")) is False or as_bool(flat.get("download_available")) is False
              or as_bool(flat.get("archive_present")) is False or flat.get("profile") is None and "profile" in flat)
    if absent:
        return "ABSENT_DIRECT", None, flow, restore

    if category == "DATA_FLOW":
        if flow is None and result in {"ACTIVE", "DISABLED"}:
            flow = result == "ACTIVE"
        if flow is None and str(flat.get("last_delivery_status", "")).lower() == "stopped":
            flow = False
        return "PRESENT", (None if flow is None else ("ACTIVE" if flow else "DISABLED")), flow, restore
    if category == "BACKUP_ARCHIVE":
        if restore is None and result in {"ACTIVE", "BLOCKED"}:
            restore = result == "ACTIVE"
        return "PRESENT", (None if restore is None else ("RESTORABLE" if restore else "BLOCKED")), flow, restore
    if category in {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}:
        return "PRESENT", "RETAINED", flow, restore
    if result in RESULT_STATES:
        return "PRESENT", result, flow, restore
    if category in IDENTIFIABILITY_CATEGORIES:
        direct = as_int(flat.get("direct_identifiers"))
        identifiable = bool(pii) or (direct is not None and direct > 0) or (category in ATTRIBUTABLE_BY_REFERENCE and refs)
        return "PRESENT", ("IDENTIFIABLE" if identifiable else IDENTIFIABILITY_CATEGORIES[category]), flow, restore
    return "PRESENT", "FOUND", flow, restore


def interpret(raw: Dict[str, Any], ctx: RecordContext) -> Optional[Observation]:
    flat = flatten(raw)
    resource_id = str(flat.get("resource_id") or "").strip()
    if not C.RESOURCE_RE.fullmatch(resource_id):
        return None
    system = resolve_system(resource_id, flat, ctx)
    if not system:
        return None
    category = infer_category(resource_id, flat, ctx)
    pii = pii_fields_of(flat)
    if not any(k in flat for k in C.DIRECT_ID_FIELDS):
        # The observer printed no identifier columns, but matched the record with an identifier key:
        # the record holds that identifier (e.g. a search hit or a hash-keyed contact row).
        for key in C.LOOKUP_FIELDS:
            value = flat.get(key)
            if not is_null(value) and isinstance(value, str) and (C.EMAIL_RE.search(value) or C.HASH_RE.search(value.lower())
                                                                    or C.PHONE_RE.search(value)):
                pii = pii | {"matched_key"}
                break
    refs = any(not is_null(flat.get(k)) and C.USER_RE.search(str(flat.get(k))) for k in C.STABLE_REF_FIELDS)
    presence, state, flow, restore = presence_and_state(category, flat, pii, bool(refs))
    observed_at = next((str(flat[k]) for k in TIME_FIELDS if isinstance(flat.get(k), str) and C.ISO_TS_RE.fullmatch(flat[k])),
                       ctx.observed_default or ctx.captured_at)
    retention = next((str(flat[k]) for k in ("retention_until", "retained_until", "retain_until") if not is_null(flat.get(k))), None)
    attrs = {k: str(v)[:200] for k, v in flat.items() if not isinstance(v, (list, dict)) and not is_null(v)}
    attrs.update(ctx.extra)
    if ctx.collection:
        attrs.setdefault("collection", ctx.collection)
    return Observation(
        obs_id=f"{ctx.evidence_id}#{ctx.locator}", evidence_id=ctx.evidence_id, source_kind=ctx.source_kind,
        file_captured_at=ctx.captured_at, observed_at=observed_at, system_id=system, resource_id=resource_id,
        replica_role=ctx.replica_role, category=category, presence=presence, observed_state=state,
        identifiers=scan_identifiers(raw), pii_fields=pii if presence == "PRESENT" else frozenset(),
        flow_enabled=flow, restore_allowed=restore, retention_until=retention,
        extraction_method=ctx.extraction_method, confidence=ctx.confidence, attrs=attrs)
