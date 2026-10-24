"""Identity resolution: current snapshot + audit history -> identifiers with validity intervals.

Every identifier a person ever held becomes an IdentifierSpan [valid_from, valid_to).
A request resolves to the user(s) owning the request email (current or historical).
A profile "as of" a time keeps only identifiers that already existed at that time,
so evidence before an identity change is matched with the identifiers valid then.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from . import config as C
from .models import AuditChange, Identifiers, Identity, Request

FIELD_KINDS = {"email": "email", "phone": "phone", "email_hash": "hash", "name": "name"}


def normalize(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "phone":
        return C.norm_phone(value)
    if kind == "name":
        return " ".join(value.split()).lower()
    return value.lower()


def email_hash(email: str) -> str:
    """Dataset convention (verified on all identities): sha256 of the lowercased email."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentifierSpan:
    kind: str                    # email, phone, hash, name
    value: str                   # normalized
    user_id: str
    valid_from: Optional[str]    # None = before any recorded history
    valid_to: Optional[str]      # None = still current
    source: str                  # "snapshot" or the audit_id that recorded the change

    def existed_by(self, when: str) -> bool:
        return self.valid_from is None or self.valid_from <= when

    def valid_at(self, when: str) -> bool:
        return self.existed_by(when) and (self.valid_to is None or when < self.valid_to)


@dataclass(frozen=True)
class IdentityProfile:
    user_id: str
    as_of: str
    spans: Tuple[IdentifierSpan, ...]

    def identifiers(self) -> Identifiers:
        by = defaultdict(set)
        for s in self.spans:
            by[s.kind].add(s.value)
        return Identifiers(frozenset(by["email"]), frozenset(by["phone"]), frozenset(by["hash"]),
                           frozenset({self.user_id}), frozenset(by["name"]))

    def historical(self) -> FrozenSet[str]:
        return frozenset(s.value for s in self.spans if s.valid_to is not None)


@dataclass(frozen=True)
class Resolution:
    case_id: str
    candidates: Tuple[str, ...]          # every user that ever held the request email
    user_id: Optional[str]               # chosen subject, None when unresolved or ambiguous
    via: str                             # current, historical, ambiguous, unresolved
    reason: str


class IdentityIndex:
    def __init__(self, identities: Iterable[Identity], audit_log: Iterable[AuditChange]):
        self.identities: Dict[str, Identity] = {i.user_id: i for i in identities}
        self.spans_by_user: Dict[str, List[IdentifierSpan]] = defaultdict(list)
        self.owners: Dict[Tuple[str, str], List[IdentifierSpan]] = defaultdict(list)
        changes: Dict[Tuple[str, str], List[AuditChange]] = defaultdict(list)
        for a in audit_log:
            if a.field_name in FIELD_KINDS:
                changes[(a.user_id, a.field_name)].append(a)
        for uid, ident in self.identities.items():
            for fname, kind in FIELD_KINDS.items():
                chain = sorted(changes.get((uid, fname), []), key=lambda a: (a.event_time, a.audit_id))
                start: Optional[str] = None
                for a in chain:  # each old value was valid until its change
                    self._add(IdentifierSpan(kind, normalize(kind, a.old_value), uid, start, a.event_time, a.audit_id))
                    start = a.event_time
                current = getattr(ident, fname)
                if current:
                    self._add(IdentifierSpan(kind, normalize(kind, current), uid, start, None,
                                             chain[-1].audit_id if chain else "snapshot"))
                if kind == "email":  # hash spans derived from emails cover gaps in the audit log
                    for s in [x for x in self.spans_by_user[uid] if x.kind == "email"]:
                        self._add(IdentifierSpan("hash", email_hash(s.value), uid, s.valid_from, s.valid_to, s.source))

    def _add(self, span: IdentifierSpan) -> None:
        if not span.value:
            return
        key = (span.kind, span.value)
        if any(s.user_id == span.user_id and s.valid_from == span.valid_from for s in self.owners[key]):
            return
        self.spans_by_user[span.user_id].append(span)
        self.owners[key].append(span)

    def owners_of(self, kind: str, value: str) -> List[IdentifierSpan]:
        return self.owners.get((kind, normalize(kind, value)), [])

    def resolve(self, request: Request) -> Resolution:
        spans = self.owners_of("email", request.email)
        users = tuple(sorted({s.user_id for s in spans}))
        if not users:
            return Resolution(request.case_id, (), None, "unresolved", "request email matches no current or historical email")
        if len(users) > 1:
            at_request = sorted({s.user_id for s in spans if s.valid_at(request.requested_at)})
            if len(at_request) == 1:
                users_pick = at_request[0]
                return Resolution(request.case_id, users, users_pick, self._via(spans, users_pick),
                                  "several holders; chosen by who held the email at requested_at")
            return Resolution(request.case_id, users, None, "ambiguous", "several users held this email")
        uid = users[0]
        return Resolution(request.case_id, users, uid, self._via(spans, uid), "unique owner")

    @staticmethod
    def _via(spans: List[IdentifierSpan], uid: str) -> str:
        return "current" if any(s.user_id == uid and s.valid_to is None for s in spans) else "historical"

    def profile(self, user_id: str, as_of: str) -> IdentityProfile:
        """All identifiers the user held at any time up to as_of (inclusive)."""
        spans = tuple(sorted((s for s in self.spans_by_user.get(user_id, []) if s.existed_by(as_of)),
                             key=lambda s: (s.kind, s.valid_from or "", s.value)))
        return IdentityProfile(user_id, as_of, spans)

    def owners_of_identifiers(self, ids: Identifiers, at: Optional[str] = None) -> Dict[str, FrozenSet[str]]:
        """Map each user to the kinds of identifiers in `ids` that belong to them (optionally valid at a time)."""
        hits: Dict[str, set] = defaultdict(set)
        for kind, values in (("email", ids.emails), ("phone", ids.phones), ("hash", ids.hashes), ("name", ids.names)):
            for v in values:
                for s in self.owners.get((kind, v), []):
                    if at is None or s.existed_by(at):
                        hits[s.user_id].add(kind)
        for uid in ids.user_ids:
            if uid in self.identities:
                hits[uid].add("user_id")
        return {u: frozenset(k) for u, k in hits.items()}
