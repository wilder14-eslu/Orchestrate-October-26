"""JSON schemas and lightweight schema validator for model outputs.

Includes:
- SCREEN_V1: Schema for VLM screen extraction.
- INTENT_V1: Schema for LLM deletion intent classification.
- validate: Minimal stdlib JSON Schema validator for the keywords used above.
"""
from __future__ import annotations

import re
from typing import Any, List

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Validator
# -----------------------------------------------------------------------------

TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, TYPES[expected])


def validate(value: Any, schema: dict, path: str = "$") -> List[str]:
    """Minimal JSON Schema validator (the subset used by our schemas), stdlib only.

    Supported keywords: type (string or list, including "null"), enum, required, properties,
    additionalProperties (false only), items, maxItems, maxLength, pattern, minimum, maximum.
    Unknown fields are rejected when additionalProperties is false.
    """
    errors: List[str] = []
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in options):
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(value):
            errors.extend(validate(item, schema.get("items", {}), f"{path}[{i}]"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected field {key!r}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    return errors
