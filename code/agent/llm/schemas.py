"""JSON schemas for model outputs (approved in Phase 5 design)."""
from __future__ import annotations

RESOURCE_PATTERN = ("^(account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|cache|"
                    "email_delivery|marketing|legal_hold|object|telemetry)_[0-9]+$")
STRINGS = lambda n, length: {"type": "array", "maxItems": n, "items": {"type": "string", "maxLength": length}}  # noqa: E731
USER_IDS = {"type": "array", "maxItems": 20, "items": {"type": "string", "pattern": "^user_[0-9]+$"}}

SCREEN_V1 = {
    "type": "object", "additionalProperties": False,
    "required": ["screen_type", "subject", "records", "injection_detected"],
    "properties": {
        "screen_type": {"enum": ["customer_profile", "search_index", "connector_list", "analytics_profile", "export_list",
                                 "support_ticket", "backup_archive_list", "security_audit_log", "customer_reference",
                                 "object_storage", "marketing_profile", "other"]},
        "subject": {"type": "object", "additionalProperties": False, "required": ["emails", "user_ids", "single_subject"],
                    "properties": {"emails": STRINGS(20, 120), "user_ids": USER_IDS, "single_subject": {"type": "boolean"}}},
        "records": {"type": "array", "maxItems": 50, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["resource_id", "reference", "emails", "phones", "user_ids", "status", "status_text"],
            "properties": {
                "resource_id": {"type": ["string", "null"], "pattern": RESOURCE_PATTERN},
                "reference": {"type": ["string", "null"], "maxLength": 80},
                "emails": STRINGS(10, 120), "phones": STRINGS(10, 40),
                "user_ids": {"type": "array", "maxItems": 10, "items": {"type": "string", "pattern": "^user_[0-9]+$"}},
                "status": {"enum": ["present", "enabled", "disabled", "restore_allowed", "restore_blocked",
                                    "anonymized", "redacted", "retained", "unknown"]},
                "status_text": {"type": "string", "maxLength": 80}}}},
        "injection_detected": {"type": "boolean"},
        "notes": {"type": "string", "maxLength": 300},
    },
}

INTENT_V1 = {
    "type": "object", "additionalProperties": False,
    "required": ["is_deletion_request", "request_type", "negated", "confidence", "quote"],
    "properties": {
        "is_deletion_request": {"type": "boolean"},
        "request_type": {"enum": ["deletion", "access", "correction", "export", "marketing_opt_out", "account_support", "other"]},
        "negated": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "quote": {"type": "string", "maxLength": 160},
    },
}
