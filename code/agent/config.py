"""Shared constants: contract tables, category maps and identifier patterns."""
from __future__ import annotations

import os
import re

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def gemini_model_name() -> str:
    """Single source of truth for the Gemini model identifier."""
    return os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL


STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# operation -> (phase, state, order within phase), exactly as in the problem statement table.
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

# Data categories from deletion_rules.csv plus the two process categories used for findings.
RULE_CATEGORIES = (
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "USER_EXPORT", "MARKETING_PROFILE",
    "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE",
)
PROCESS_CATEGORIES = ("DATA_FLOW",)
CATEGORIES = RULE_CATEGORIES + PROCESS_CATEGORIES

# resource_id prefix -> (system_id, default category). Longest prefix wins.
RESOURCE_PREFIXES = {
    "account_": ("account_service", "USER_PROFILE"),
    "search_": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics_": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support_": ("support_platform", "SUPPORT_MESSAGE"),
    "export_": ("export_service", "USER_EXPORT"),
    "data_flow_": ("integration_service", "DATA_FLOW"),
    "backup_": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record_": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone_": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "cache_": ("cache_service", "CACHE_ENTRY"),
    "email_delivery_": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "marketing_": ("marketing_platform", "MARKETING_PROFILE"),
    "legal_hold_": ("legal_service", "LEGAL_HOLD_RECORD"),
    "object_": ("object_storage", "SUPPORT_ATTACHMENT"),
    "telemetry_": ("telemetry_service", "PRODUCT_TELEMETRY"),
}

# Fields that hold a direct identifier when non-null. Stable references (user_id, customer_reference,
# subject_reference) are tracked separately because tombstones may keep them.
DIRECT_ID_FIELDS = {
    "name", "full_name", "display_name", "email", "phone", "email_hash", "requester_name", "requester_email",
    "requester_phone", "owner_email", "owner_phone", "customer_email", "matched_value", "recipient", "recipient_email",
}
STABLE_REF_FIELDS = {"user_id", "customer_reference", "subject_reference"}
FREE_TEXT_FIELDS = {"message_text", "message_excerpt", "note", "notes", "body", "document_text"}
# Fields that only carry a lookup key used by the observer, not data stored in the record.
LOOKUP_FIELDS = {"lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref", "lookup_value",
                 "correlation_key", "subject_selector", "query"}
# Fields whose hex content is a checksum, never an identifier.
NON_IDENTIFIER_FIELDS = {"integrity_seal", "checksum", "fingerprint", "short_fingerprint", "archive_key", "object_key",
                         "archive_path", "trace_id"}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d ()\-]{6,}\d")
RESOURCE_RE = re.compile(r"\b(?:" + "|".join(p.rstrip("_") for p in sorted(RESOURCE_PREFIXES, key=len, reverse=True)) + r")_\d+\b")
ISO_TS_RE = re.compile(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ\b")
NULL_VALUES = {"", "null", "none", "nil", "[redacted]", "redacted", "no direct identifier", "n/a"}


def system_and_category(resource_id: str) -> tuple[str | None, str | None]:
    for prefix in sorted(RESOURCE_PREFIXES, key=len, reverse=True):
        if resource_id.startswith(prefix):
            return RESOURCE_PREFIXES[prefix]
    return None, None


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)
