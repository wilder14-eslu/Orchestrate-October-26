"""Resource state at audit_at (approved precedence).

For every resource linked to a request:
1. Candidate observations: same resource_id, not a claim (activity results never decide
   state), from a file captured at or before audit_at, observed inside the request window
   (previous request's audit_at of the same person, audit_at], and not an uncorroborated
   OCR suspect. Files captured after audit_at are excluded here: they may link, never decide.
2. Direct observations win. The latest direct observation is taken per replica role;
   if any role's latest observation shows the record PRESENT, the record is PRESENT
   (replication drift: a stale copy anywhere is a remaining copy).
3. Without any direct observation, absence by omission: the latest complete capture of
   the resource's system (CaptureScope) at or before audit_at, of a primary-like role
   (NONE/PRIMARY), that does not contain the resource proves ABSENT.
   A direct observation that exists only in a file captured after audit_at (record time
   <= audit_at) never decides and is never cited; if it contradicts the omission (it shows
   the record present) the state is UNKNOWN (basis LATE_ONLY, late_hint=PRESENT).
4. Otherwise the state is UNKNOWN.
Citation: the latest file captured at or before audit_at that proves the decided state.
Backups: a linked, present archive whose restore permission is not stated is treated as
RESTORABLE (an available archive is an active restore path unless evidence says otherwise).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Tuple

from .config import system_and_category
from .linker import LinkResult
from .models import CaptureScope, Corpus, Observation, Request

PRIMARY_LIKE = {"NONE", "PRIMARY"}


@dataclass(frozen=True)
class ResourceState:
    case_id: str
    system_id: str
    resource_id: str
    category: Optional[str]
    presence: str                      # PRESENT, ABSENT, UNKNOWN
    state: Optional[str]               # FOUND, IDENTIFIABLE, REDACTED, ANONYMIZED, ACTIVE, DISABLED, RESTORABLE, BLOCKED, RETAINED
    proof: str                         # evidence_id to cite (empty when UNKNOWN)
    basis: str                         # DIRECT, OMISSION, NONE
    pii_fields: FrozenSet[str] = frozenset()
    retention_until: Optional[str] = None
    confidence: float = 1.0
    link_via: str = ""
    late_hint: Optional[str] = None    # presence seen only in a late file (never decides, never cited)
    category_basis: str = "default"    # "record" when an observation of the record itself shows its category
    notes: Tuple[str, ...] = ()


def _category(obs: List[Observation]) -> Optional[str]:
    ranked = Counter()
    for o in obs:
        if o.category:
            ranked[o.category] += o.confidence
    return ranked.most_common(1)[0][0] if ranked else None


class StateResolver:
    def __init__(self, corpus: Corpus):
        self.by_resource: Dict[str, List[Observation]] = defaultdict(list)
        for o in corpus.observations:
            if not o.is_claim:
                self.by_resource[o.resource_id].append(o)
        self.file_time: Dict[str, str] = {m.file_id: m.captured_at for m in corpus.manifest}
        self.scopes: Dict[str, List[CaptureScope]] = defaultdict(list)
        for s in corpus.scopes:
            if s.complete:
                self.scopes[s.system_id].append(s)
        self.present_in_capture: Dict[Tuple[str, str], set] = defaultdict(set)  # (evidence_id, role) -> resource ids
        for o in corpus.observations:
            if not o.is_claim:
                self.present_in_capture[(o.evidence_id, o.replica_role)].add(o.resource_id)

    def _eligible(self, rid: str, start: Optional[str], audit_at: str) -> List[Observation]:
        out = []
        for o in self.by_resource.get(rid, []):
            if o.file_captured_at > audit_at or o.observed_at > audit_at:
                continue
            if start is not None and o.observed_at <= start:
                continue
            if o.attrs.get("ocr_suspect") == "true" and not any(
                    x.evidence_id != o.evidence_id and x.attrs.get("ocr_suspect") != "true" for x in self.by_resource[rid]):
                continue
            out.append(o)
        return out

    def resolve(self, request: Request, system_id: str, resource_id: str, start: Optional[str], link_via: str,
                category_hint: Optional[str] = None) -> ResourceState:
        state = self._resolve(request, system_id, resource_id, start, link_via, category_hint)
        basis = "record" if any(o.attrs.get("category_basis") == "record" and o.category == state.category
                                for o in self._eligible(resource_id, start, request.audit_at)) else "default"
        return replace(state, category_basis=basis)

    def _resolve(self, request: Request, system_id: str, resource_id: str, start: Optional[str], link_via: str,
                category_hint: Optional[str] = None) -> ResourceState:
        audit_at = request.audit_at
        obs = self._eligible(resource_id, start, audit_at)
        direct = [o for o in obs if o.presence in {"PRESENT", "ABSENT_DIRECT"}]
        record_based = Counter(o.category for o in obs if o.category and o.attrs.get("category_basis") == "record")
        if "SUPPORT_ATTACHMENT" in record_based:  # attachment evidence is the more specific signal
            record_based = Counter({"SUPPORT_ATTACHMENT": record_based["SUPPORT_ATTACHMENT"]})
        category = (record_based.most_common(1)[0][0] if record_based else None) or _category(obs) or category_hint \
            or system_and_category(resource_id)[1]
        category_basis = "record" if record_based else "default"
        if direct:
            latest_by_role: Dict[str, Observation] = {}
            for o in sorted(direct, key=lambda o: (o.observed_at, o.file_captured_at, o.evidence_id)):
                latest_by_role[o.replica_role] = o
            present = [o for o in latest_by_role.values() if o.presence == "PRESENT"]
            deciding = present if present else list(latest_by_role.values())
            presence = "PRESENT" if present else "ABSENT"
            # The record state comes from the latest deciding observation; citation from the latest proving file.
            last = max(deciding, key=lambda o: (o.observed_at, o.file_captured_at, o.evidence_id))
            state = last.observed_state if presence == "PRESENT" else None
            agreeing = [o for o in direct if (o.presence == "PRESENT") == (presence == "PRESENT")
                        and (presence == "ABSENT" or o.observed_state == state)]
            proof = max(agreeing, key=lambda o: (o.file_captured_at, o.observed_at, o.evidence_id))
            notes: List[str] = []
            if presence == "PRESENT" and category == "BACKUP_ARCHIVE" and state is None:
                state = "RESTORABLE"
                notes.append("restore permission not stated on a listed archive: treated as an active restore path")
            pii = frozenset().union(*(o.pii_fields for o in deciding if o.presence == "PRESENT")) if presence == "PRESENT" else frozenset()
            return ResourceState(request.case_id, system_id, resource_id, category, presence, state, proof.evidence_id,
                                 "DIRECT", pii, last.retention_until or next((o.retention_until for o in obs if o.retention_until), None),
                                 min(o.confidence for o in deciding), link_via, None, tuple(notes))
        late = [o for o in self.by_resource.get(resource_id, []) if o.presence in {"PRESENT", "ABSENT_DIRECT"}
                and o.file_captured_at > audit_at and o.observed_at <= audit_at and (start is None or o.observed_at > start)]
        late_hint = None
        if late:
            last_late = max(late, key=lambda o: (o.observed_at, o.evidence_id))
            late_hint = "PRESENT" if last_late.presence == "PRESENT" else "ABSENT"
        scopes = [s for s in self.scopes.get(system_id, []) if s.captured_at <= audit_at and s.replica_role in PRIMARY_LIKE
                  and self.file_time.get(s.evidence_id, s.captured_at) <= audit_at  # late files never decide state
                  and (start is None or s.captured_at > start)]
        if scopes:
            latest = max(scopes, key=lambda s: (s.captured_at, s.evidence_id))
            if resource_id not in self.present_in_capture.get((latest.evidence_id, latest.replica_role), set()):
                if late_hint != "PRESENT":  # a contradicting late observation blocks omission (rule 5)
                    return ResourceState(request.case_id, system_id, resource_id, category, "ABSENT", None, latest.evidence_id,
                                         "OMISSION", frozenset(), None, 1.0, link_via, late_hint,
                                         (f"absent from complete {system_id} capture {latest.evidence_id}",))
        if late_hint:
            return ResourceState(request.case_id, system_id, resource_id, category or _category(late), "UNKNOWN", None, "",
                                 "LATE_ONLY", link_via=link_via, late_hint=late_hint,
                                 notes=(f"only direct observation is in late file(s) {sorted({o.evidence_id for o in late})}",))
        return ResourceState(request.case_id, system_id, resource_id, category, "UNKNOWN", None, "", "NONE",
                             link_via=link_via, notes=("no eligible observation or complete capture at or before audit_at",))

    def resolve_case(self, request: Request, links: LinkResult) -> List[ResourceState]:
        start, _ = links.windows.get(request.case_id, (None, request.audit_at))
        seen: Dict[Tuple[str, str], str] = {}
        for l in links.links.get(request.case_id, []):
            seen.setdefault((l.system_id, l.resource_id), l.via)
        return [self.resolve(request, s, r, start, via) for (s, r), via in sorted(seen.items())]
