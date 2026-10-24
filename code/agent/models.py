"""Typed records shared by every stage of the pipeline (approved Phase 2 schemas)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class ManifestEntry:
    file_id: str
    file_path: str
    origin: str
    captured_at: str


@dataclass(frozen=True)
class Identifiers:
    """Normalized identifiers seen in or used to reach a record (emails lowercased, phones digits-only)."""
    emails: FrozenSet[str] = frozenset()
    phones: FrozenSet[str] = frozenset()
    hashes: FrozenSet[str] = frozenset()
    user_ids: FrozenSet[str] = frozenset()
    names: FrozenSet[str] = frozenset()

    def is_empty(self) -> bool:
        return not (self.emails or self.phones or self.hashes or self.user_ids or self.names)


@dataclass(frozen=True)
class Observation:
    """One fact about one resource taken from one place in one evidence item."""
    obs_id: str                      # evidence_id#locator
    evidence_id: str                 # file_id, activity_id or audit_id
    source_kind: str                 # SQL, JSON, LOG, PDF, IMAGE, TXT, ACTIVITY
    file_captured_at: str            # manifest capture time (event time for activity rows)
    observed_at: str                 # record/line time; falls back to file_captured_at
    system_id: str
    resource_id: str
    replica_role: str                # PRIMARY, READ, REPORTING, FAILOVER, REGIONAL, NONE
    category: Optional[str]          # rule category, DATA_FLOW, or None when unknown
    presence: str                    # PRESENT, ABSENT_DIRECT, UNKNOWN
    observed_state: Optional[str]    # FOUND, IDENTIFIABLE, REDACTED, ANONYMIZED, ACTIVE, DISABLED, RESTORABLE, BLOCKED, RETAINED
    identifiers: Identifiers         # everything that ties the record (or the lookup) to a person
    pii_fields: FrozenSet[str]       # direct-identifier fields that are non-null in the stored record
    flow_enabled: Optional[bool] = None
    restore_allowed: Optional[bool] = None
    retention_until: Optional[str] = None
    extraction_method: str = "rule"  # rule, salvage, ocr, vlm, llm
    confidence: float = 1.0
    is_claim: bool = False           # activity results and workflow messages are claims, never proof
    attrs: Dict[str, str] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class CaptureScope:
    """A capture that lists a whole system, so omission of a record there means absence."""
    evidence_id: str
    system_id: str
    captured_at: str
    replica_role: str
    complete: bool


@dataclass(frozen=True)
class ParseWarning:
    evidence_id: str
    path: str
    offset: str
    message: str


@dataclass
class Corpus:
    """Everything loaded from dataset/: tables, observations, scopes and parse diagnostics."""
    identities: List[Identity]
    audit_log: List[AuditChange]
    activity: List[Activity]
    rules: List[Rule]
    manifest: List[ManifestEntry]
    observations: List[Observation] = field(default_factory=list)
    scopes: List[CaptureScope] = field(default_factory=list)
    warnings: List[ParseWarning] = field(default_factory=list)
    alias_mentions: List[dict] = field(default_factory=list)   # business references (ORD-, CS-, ...) found in documents
    stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
