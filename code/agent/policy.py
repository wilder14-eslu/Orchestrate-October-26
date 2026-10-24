"""Policy: apply deletion_rules.csv to each resource state.

The required action comes from the rule for the resource's data category; data flows
are processes and must be disabled. The decision says whether the current state
complies, which finding/remediation/verification it implies, and whether the
verification can cite evidence (COMPLETE cases cite; INCOMPLETE cases describe future work).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Rule
from .state import ResourceState


@dataclass(frozen=True)
class Decision:
    state: ResourceState
    action: str                         # DELETE, REDACT, ANONYMIZE, RETAIN, BLOCK_RESTORE, DISABLE_FLOW
    finding: Optional[str]              # finding operation when the state violates the policy
    remediation: Optional[str]
    verification: Optional[str]         # verification operation for the required final state
    citable: bool                       # the current state is proven by citable evidence
    note: str = ""


FINDING_FOR_ACTION = {"DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
                      "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
                      "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
                      "BLOCK_RESTORE": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
                      "DISABLE_FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")}
COMPLIANT_STATE = {"REDACT": {"REDACTED"}, "ANONYMIZE": {"ANONYMIZED"}, "BLOCK_RESTORE": {"BLOCKED"}, "DISABLE_FLOW": {"DISABLED"}}


class Policy:
    def __init__(self, rules: Iterable[Rule], worker_ops: Optional[Dict[str, List[Tuple[str, str]]]] = None):
        self.rules: Dict[str, Rule] = {r.data_category: r for r in rules}
        # resource_id -> [(event_time, event_type)] of deletion_worker actions: the treatment the organization applied.
        self.worker_ops = worker_ops or {}

    def support_category(self, s: ResourceState, audit_at: str) -> Optional[str]:
        """rule_05 (redact shared conversations) vs rule_06 (delete subject-only attachments).
        Evidence about the record itself decides first; the treatment the deletion worker applied before
        audit_at is only a secondary signal (activity rows are claims) used when the record shows neither."""
        if s.category_basis == "record":
            return s.category
        ops = {t for when, t in self.worker_ops.get(s.resource_id, []) if when <= audit_at}
        if ops == {"DELETE_RECORD"}:
            return "SUPPORT_ATTACHMENT"
        if ops == {"REDACT_RECORD"}:
            return "SUPPORT_MESSAGE"
        return s.category

    def required_action(self, s: ResourceState, audit_at: str) -> Tuple[Optional[str], str]:
        if s.category == "DATA_FLOW":
            return "DISABLE_FLOW", "active data flows must be disabled"
        category = self.support_category(s, audit_at) if s.category in {"SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"} else s.category
        rule = self.rules.get(category or "")
        if not rule:
            return None, f"no rule for category {s.category}"
        action = rule.required_action.upper()
        if s.category == "BACKUP_ARCHIVE":
            return "BLOCK_RESTORE", f"{rule.rule_id}: retain archive, block restore of the subject's data"
        if action == "RETAIN" and s.retention_until and s.retention_until < audit_at:
            return "DELETE", f"{rule.rule_id}: retention period ended {s.retention_until}"
        return action, rule.rule_id + ("" if category == s.category else f" (category {category} from worker action)")

    def decide(self, s: ResourceState, audit_at: str) -> Optional[Decision]:
        action, why = self.required_action(s, audit_at)
        if action is None:
            return None
        if action == "RETAIN":
            if s.presence == "PRESENT":
                return Decision(s, action, None, None, "VERIFY_RECORD_PRESENT", True, why)
            if s.presence == "UNKNOWN" and s.late_hint == "PRESENT":
                return Decision(s, action, None, None, "VERIFY_RECORD_PRESENT", False, "known only from a late file")
            return None
        finding, remediation, verification = FINDING_FOR_ACTION[action]
        if s.presence == "ABSENT":
            if action == "DELETE":
                return Decision(s, action, None, None, verification, True, why)
            return None  # an absent copy needs no redaction/anonymization/blocking
        if s.presence != "PRESENT":
            if s.category == "USER_PROFILE":
                return Decision(s, action, None, None, verification, False, "account state unknown")
            return None
        if action == "DELETE" or s.state not in COMPLIANT_STATE.get(action, set()):
            if action == "DISABLE_FLOW" and s.state is None:
                return None  # a flow whose enabled flag is unknown is not reported
            return Decision(s, action, finding, remediation, verification, True, why)
        return Decision(s, action, None, None, verification, True, why)
